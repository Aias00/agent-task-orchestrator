"""Unified execution with real local Git and synthetic offline CLI processes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Barrier, Event
import time
import unittest
from unittest.mock import patch

import ato_execution as execution
import ato_execution_host
import ato_config
import ato_process
from ato_task_store import InitialBinding, SubmissionGrant, TaskError, TaskStore
import recovery
import transport


_WORKER = r"""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

home = Path.home()
def read(name, default):
    path = home / name
    return path.read_text() if path.exists() else default

prompt = sys.stdin.read()
with (home / "attempts").open("a") as log:
    log.write("repair\n" if "fix_blockers" in prompt else "work\n")
mode = read("mode", "normal")
target = Path("delivery.txt")
target.write_text(target.read_text() + "update\n" if target.exists() else "implemented\n")
if mode == "linger":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(90)"])
    (home / "worker.json").write_text(json.dumps(dict(pid=os.getpid(), child=child.pid)))
    time.sleep(90)
if mode == "unknown":
    sys.exit(17)
if mode == "large-output":
    print("x" * (2 * 1024 * 1024))
if mode == "blocked":
    result = dict(status="blocked", summary="fixture blocker", next_step="repair fixture", blocker="blocked")
else:
    result = dict(status="ready_for_checks", summary="fixture done", next_step="", blocker="")
result["retry_after_seconds"] = 0
output = sys.argv[sys.argv.index("-o") + 1]
Path(output).write_text(json.dumps(result))
"""


_REVIEWER = r"""
import json
from pathlib import Path
import sys

home = Path.home()
prompt = sys.stdin.read()
counter = home / "round"
round_number = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(round_number))
mode = (home / "mode").read_text() if (home / "mode").exists() else "approve"
if mode == "mutate":
    Path(".codex/tasks/fixture-mutation").write_text("tamper")
if mode == "hold":
    __import__("time").sleep(30)
blockers = []
if mode == "repair" and round_number == 1:
    blockers.append(dict(id="b1", severity="blocker", category="correctness",
        file="delivery.txt", lines="L1", title="Finish delivery",
        evidence="Only the initial edit exists", required_fix="Make a second edit"))
print(json.dumps(dict(verdict="request_changes" if blockers else "approve",
    summary="fixture verdict", findings=blockers, checked_items=["delivery"], residual_risks=[])))
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, timeout=20, env=dict(PATH=os.defpath, LC_ALL="C", GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull, GIT_ALLOW_PROTOCOL="", GIT_AUTHOR_NAME="ATO Fixture",
        GIT_AUTHOR_EMAIL="ato@example.invalid", GIT_COMMITTER_NAME="ATO Fixture",
        GIT_COMMITTER_EMAIL="ato@example.invalid")).stdout.strip()


class UnifiedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="ato-execution-"))).resolve()
        self.repo = self.root / "repository"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / ".gitignore").write_text(".codex/tasks/\n")
        (self.repo / "source.txt").write_text("base\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "fixture base")
        self.worker_home = self.root / "worker-home"
        self.reviewer_home = self.root / "reviewer-home"
        self.worker_home.mkdir(mode=0o700)
        self.reviewer_home.mkdir(mode=0o700)
        # A fake executable with a pinned interpreter. No provider, credential,
        # ambient CLI discovery or network service is involved.
        worker = self.tool("worker", _WORKER)
        review = self.tool("review", _REVIEWER)
        self.actor = object()
        binding = InitialBinding("controller-test", "interaction-test")
        self.binding = binding
        self.store = TaskStore(self.root / "ato", native_home=self.root / "native",
            runtime_seal="1" * 64, authenticate=lambda actor: actor if actor is self.actor else None,
            can_observe=lambda principal, scope: principal is self.actor,
            authorize_submit=lambda spec, actor, initial: SubmissionGrant(
                "fixture-authorization", spec.repository.key, spec.spec_sha256, initial)
                if actor is self.actor else None)
        profile = execution.HostProfile(str(worker), str(self.worker_home),
                                        (("codex", str(review), str(self.reviewer_home)),), fixture=True)
        self.service = execution.ExecutionService(self.store, profile,
            authorize=lambda principal, scope, current, operation: principal is self.actor)
        self.processes = []
        original_spawn = self.service._spawn
        def remember(*arguments):
            process = original_spawn(*arguments)
            self.processes.append(process)
            return process
        self.service._spawn = remember
        self.addCleanup(self.reap)

    def reap(self):
        for process in self.processes:
            if process.poll() is None:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def tool(self, name, code):
        body = self.root / (name + ".py")
        body.write_text(code)
        path = self.root / name
        path.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", str(body)]) + ' "$@"\n')
        path.chmod(0o700)
        return path

    def create(self, *, reviewer=None, max_attempts=3, checks=None, preset=False):
        spec = dict(schema_version=1, repo=str(self.repo), base_ref="HEAD",
            worker_contract=dict(objective="implement the frozen work", scope="worktree only",
                authorization="fixture source", limits=dict(max_attempts=max_attempts),
                acceptance=[dict(id="command", description="frozen check", checks=checks or [["true"]]),
                            dict(id="deliver", kind="manual", description="human delivery")]))
        if reviewer is not None:
            spec["reviewer"] = reviewer
        if preset:
            spec["worker_contract"]["stages"] = [
                dict(id="frozen-first", covers=["command"], plan=dict(objective="first", tasks=[
                    dict(id="change", prompt="write the requested artifact", checks=[["true"]])]))]
        path = self.root / "spec.json"
        path.write_text(json.dumps(spec))
        normalized = self.store.normalize_spec(self.repo, path)
        return self.store.create_task(normalized, self.actor, self.binding)["task_id"]

    def status(self, task):
        return self.service.task_status(self.actor, task)

    def wait_for(self, operation, expected):
        end = time.monotonic() + 18
        latest = None
        while time.monotonic() < end:
            try:
                latest = operation()
                if expected(latest):
                    return latest
            except TaskError as exc:
                if exc.code != "BUSY":
                    raise
            time.sleep(.05)
        self.fail("Condition did not settle: " + repr(latest))

    def assert_error(self, code, method, *args):
        with self.assertRaises(TaskError) as caught:
            method(*args)
        self.assertEqual(caught.exception.code, code)

    def test_green_baseline_still_runs_frozen_first_stage_and_requires_human(self):
        task = self.create(reviewer=dict(agent=dict(kind="codex"), max_review_rounds=2), preset=True)
        status = self.service.start(self.actor, task, foreground=True)
        self.assertEqual(status["status"], "completed", status)
        self.assertEqual(status["stage"]["stage_id"], "frozen-first")
        self.assertEqual(status["stage"]["attempts"], 1)
        self.assertEqual(status["review"]["verdict"], "approve")
        self.assertEqual(status["acceptance"]["disposition"], "not_recorded")
        self.assertTrue(status["checks"][0]["receipt_id"])
        self.assertTrue(status["product"]["product_id"].startswith("product-"))
        self.assertEqual((Path(status["product"]["worktree"]) / "delivery.txt").read_text(), "implemented\n")
        self.assertFalse((self.repo / "delivery.txt").exists())
        self.assert_error("NOT_RESUMABLE", self.service.resume, self.actor, task)

    def test_batch2_worker_json_event_flood_keeps_valid_output_and_settles(self):
        task = self.create()
        (self.worker_home / "mode").write_text("large-output")
        result = self.service.start(self.actor, task, foreground=True)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["run_history"][-1]["phase"], "settled")

    def test_batch3_quarantined_unknown_task_does_not_release_itself_for_replay(self):
        task = self.create()
        (self.worker_home / "mode").write_text("unknown")
        unknown = self.service.start(self.actor, task, foreground=True)
        self.assertEqual(unknown["status"], "delivery_unknown")
        peer = self.create()
        self.assert_error("RUN_UNSETTLED", self.service.start, self.actor, peer)
        self.assertEqual(execution.quarantine_task(self.store, task, "independent fixture check")["state"],
                         "quarantined")
        self.assertEqual(self.status(task)["reason"], "RUN_QUARANTINED")
        self.assert_error("NOT_RESUMABLE", self.service.resume, self.actor, task)
        self.assert_error("NOT_RESUMABLE", self.service.reconcile, self.actor, task)
        (self.worker_home / "mode").unlink()
        self.assertEqual(self.service.start(self.actor, peer, foreground=True)["status"], "completed")

    def test_batch2_failed_launch_preserves_previous_review_round_accounting(self):
        task = self.create(reviewer=dict(agent=dict(kind="codex"), max_review_rounds=1))
        first = self.service.start(self.actor, task, foreground=True)
        self.assertEqual(first["status"], "completed", first)
        with patch.object(self.service, "_spawn_child", side_effect=FileNotFoundError):
            self.assert_error("LAUNCH_FAILED", self.service.recheck, self.actor, task)
        result = self.service.resume(self.actor, task, foreground=True)
        self.assertEqual(result["reason"], "review_loop_exhausted", result)
        self.assertEqual((self.reviewer_home / "round").read_text(), "1")

    def test_batch2_oversized_review_diff_settles_with_exact_reason(self):
        task = self.create(reviewer=dict(agent=dict(kind="codex"), max_review_rounds=2))
        current = self.store.load_task(task)
        (Path(current["worktree"]["path"]) / "large.txt").write_bytes(b"x" * (140 * 1024))
        result = self.service.start(self.actor, task, foreground=True)
        self.assertEqual(result["run_history"][-1]["phase"], "settled", result)
        self.assertEqual(result["reason"], "REVIEW_DIFF_BOUND_EXCEEDED")

    def test_batch2_darwin_reaped_pid_reuse_does_not_inspect_new_group(self):
        with patch.object(ato_process, "disposition", return_value="reused"), \
                patch.object(ato_process, "group_members", side_effect=AssertionError("unrelated group")):
            execution._verify_exit({"pid": 3241, "birth": "old-identity"})

    def test_batch2_stop_during_review_does_not_look_like_reviewer_mutation(self):
        task = self.create(reviewer=dict(agent=dict(kind="codex"), max_review_rounds=3))
        (self.reviewer_home / "mode").write_text("hold")
        self.service.start(self.actor, task)
        self.wait_for(lambda: (self.reviewer_home / "round").exists(), bool)
        self.service.request_stop(self.actor, task)
        status = self.wait_for(lambda: self.status(task), lambda row: row["status"] == "stopped")
        self.assertEqual(status["reason"], "STOPPED")
        (self.reviewer_home / "mode").unlink()
        self.assertEqual(self.service.resume(self.actor, task, foreground=True)["status"], "completed")

    def test_batch2_stopped_reserved_recheck_can_be_retried(self):
        task = self.create()
        completed = self.service.start(self.actor, task, foreground=True)
        self.assertEqual(completed["status"], "completed")
        current = self.store.load_task(task)
        run, receipts = self.store._history(current)[-1]
        reserved = execution.RunIntent(execution.new_id("run"), run["run_id"], "recheck",
            current["run_count"] + 1, "explicit_recheck", self.store.runtime_seal,
            tuple(receipts[-1][1]["generation"]))
        self.store.append_run(task, reserved, current["revision"])
        self.assertEqual(self.service.request_stop(self.actor, task)["status"], "stopped")
        result = self.service.recheck(self.actor, task, foreground=True)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["run_history"][-2]["phase"], "launch_failed")
        self.assertEqual((self.worker_home / "attempts").read_text().splitlines(), ["work"])

    def test_batch2_signal_stop_records_epoch_and_remains_resumable(self):
        task = self.create()
        (self.worker_home / "mode").write_text("linger")
        self.service.start(self.actor, task)
        self.wait_for(lambda: (self.worker_home / "worker.json").exists(), bool)
        self.processes[-1].send_signal(signal.SIGTERM)
        status = self.wait_for(lambda: self.status(task), lambda row: row["status"] == "stopped")
        self.assertEqual(status["reason"], "STOPPED")
        current = self.store.load_task(task)
        mirror = Path(current["workflow"]["directory"]) / "STOP"
        self.assertEqual(json.loads(mirror.read_text())["epoch"], current["stop_epoch"])
        (self.worker_home / "mode").unlink()
        self.assertEqual(self.service.resume(self.actor, task, foreground=True)["status"], "completed")

    def test_two_simultaneous_launches_share_the_repository_gate(self):
        first, second = self.create(), self.create()
        (self.worker_home / "mode").write_text("linger")
        barrier = Barrier(2)
        def launch(task):
            barrier.wait(timeout=5)
            try:
                return task, self.service.start(self.actor, task)["status"]
            except TaskError as exc:
                return task, exc.code
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(launch, (first, second)))
        self.assertEqual(sorted(state for _, state in results), ["BUSY", "running"])
        task = next(task for task, state in results if state == "running")
        self.assert_error("BUSY", self.service.start, self.actor, task)
        self.service.request_stop(self.actor, task)
        self.wait_for(lambda: self.status(task), lambda status: status["status"] == "stopped")

    def test_stop_cleans_group_and_resume_preserves_identity_and_attempts(self):
        task = self.create()
        (self.worker_home / "mode").write_text("linger")
        self.assertEqual(self.service.start(self.actor, task)["status"], "running")
        marker = self.worker_home / "worker.json"
        self.wait_for(marker.exists, bool)
        process = json.loads(marker.read_text())
        self.assertIsNotNone(ato_process.identity(process["child"]))
        stop = self.service.request_stop(self.actor, task)
        self.assertIn(stop["status"], ("stopping", "stopped"))
        stopped = self.wait_for(lambda: self.status(task), lambda status: status["status"] == "stopped")
        self.assertTrue(stopped["execution"]["verified"])
        self.assertEqual(ato_process.group_members(process["pid"]), {})
        self.assertIsNone(ato_process.identity(process["child"]))
        self.processes[-1].wait(timeout=5)
        (self.worker_home / "mode").unlink()
        finished = self.service.resume(self.actor, task, foreground=True)
        self.assertEqual(finished["status"], "completed", finished)
        self.assertEqual(finished["task_id"], task)
        self.assertEqual(finished["stage"]["attempts"], 2)
        self.assertEqual([row["kind"] for row in finished["run_history"]], ["initial", "resume"])
        self.assertFalse(finished["execution"]["stop_requested"])

    def test_interrupted_driver_and_reused_pid_never_authorize_replay(self):
        task = self.create()
        sibling = self.create()
        (self.worker_home / "mode").write_text("linger")
        live = self.service.start(self.actor, task)
        marker = self.worker_home / "worker.json"
        self.wait_for(marker.exists, bool)
        process = json.loads(marker.read_text())
        native_birth = ato_process.identity(process["pid"])
        def cleanup():
            if ato_process.disposition(process["pid"], native_birth) == "live":
                os.killpg(process["pid"], signal.SIGKILL)
        self.addCleanup(cleanup)
        with patch.object(ato_process, "identity", return_value="f" * 64):
            projected = self.status(task)
        self.assertEqual(projected["status"], "delivery_unknown")
        self.assertIsNone(projected["execution"]["pid"])
        self.assertEqual(live["execution"]["pid"], self.processes[-1].pid)
        self.processes[-1].kill()
        self.processes[-1].wait(timeout=5)
        self.assertEqual(self.status(task)["status"], "delivery_unknown")
        self.assert_error("RUN_UNSETTLED", self.service.resume, self.actor, task)
        self.assert_error("RUN_UNSETTLED", self.service.start, self.actor, sibling)
        self.assertEqual((self.worker_home / "attempts").read_text().splitlines(), ["work"])

    def test_unknown_worker_outcome_does_not_trigger_kernel_retry(self):
        task = self.create()
        (self.worker_home / "mode").write_text("unknown")
        status = self.service.start(self.actor, task, foreground=True)
        self.assertEqual(status["status"], "delivery_unknown", status)
        self.assertEqual(status["run_history"][0]["phase"], "unknown")
        self.assertEqual(status["stage"]["attempts"], 1)
        self.assert_error("RUN_UNSETTLED", self.service.resume, self.actor, task)

    def test_fixup_node_resolution_precedes_launch_and_pre_effect_failures_settle(self):
        for error in (ValueError("node preflight"), ValueError("invalid spawn argument")):
            with self.subTest(error=error):
                task_id = self.create()

                def resolve_node(store, task):
                    self.assertEqual(task["task_id"], task_id)
                    _, receipts = store._run(task, task["run_id"])
                    self.assertEqual(receipts, [])
                    if str(error) == "node preflight":
                        raise error
                    return None

                with patch.object(execution.HostProfile, "node", autospec=True,
                                  side_effect=lambda _, store, task: resolve_node(store, task)), \
                        patch.object(self.service, "_spawn_child", side_effect=error) as spawn:
                    self.assert_error("LAUNCH_FAILED", self.service.start, self.actor, task_id)
                self.assertEqual(spawn.call_count, int(str(error) != "node preflight"))
                status = self.status(task_id)
                self.assertEqual(status["run_history"][-1]["phase"], "launch_failed")
                self.assertEqual(status["stage"]["attempts"], 0)
                self.assertTrue(status["execution"]["verified"])

    def test_fixup_production_node_validation_falls_back_and_valid_node_is_injected(self):
        task_id = self.create()
        task = self.store.load_task(task_id)
        profile = execution.HostProfile(self.service.profile.worker_executable, str(self.worker_home))
        manifest = self.root / ".release-manifest.json"
        manifest.write_text(json.dumps({"tools": {"node": {"path": "/fixture/node", "sha256": "a" * 64}}}))
        with patch.object(ato_execution_host, "__file__", str(self.root / "ato_execution_host.py")):
            for operation, error in (("safe_record", OSError("read")),
                                     ("verify_tools", ValueError("digest changed")),
                                     ("trusted_path", ValueError("invalid path"))):
                with self.subTest(operation=operation), \
                        patch.object(ato_execution_host, "verify_tools"), \
                        patch.object(ato_execution_host, "trusted_path", return_value=Path("/fixture/node")), \
                        patch.object(ato_execution_host, operation, side_effect=error):
                    self.assertIsNone(profile.node(self.store, task))
            with patch.object(ato_execution_host, "verify_tools") as verify, \
                    patch.object(ato_execution_host, "trusted_path", return_value=Path("/fixture/node")):
                self.assertEqual(profile.node(self.store, task), Path("/fixture/node"))
                verify.assert_called_once()

    def test_fixup_oversized_review_prompt_settles_without_running_reviewer(self):
        task_id = self.create(reviewer=dict(agent=dict(kind="codex"),
            checklist=[f"{i} " + "x" * 4900 for i in range(112)]))
        status = self.service.start(self.actor, task_id, foreground=True)
        self.assertEqual((status["status"], status["reason"]), ("blocked", "INVALID_REVIEW_PROMPT"))
        self.assertEqual(status["run_history"][-1]["phase"], "settled")
        self.assertFalse((self.reviewer_home / "round").exists())

    def test_pre_effect_launcher_error_retains_attempt_and_allows_explicit_resume(self):
        task = self.create()
        spawn = subprocess.Popen
        def refuse(argv, *args, **kwargs):
            if "run-child" in argv:
                raise FileNotFoundError("synthetic missing interpreter")
            return spawn(argv, *args, **kwargs)
        with patch.object(subprocess, "Popen", side_effect=refuse):
            self.assert_error("LAUNCH_FAILED", self.service.start, self.actor, task)
        failed = self.status(task)
        self.assertEqual(failed["run_history"][0]["phase"], "launch_failed")
        self.assertEqual(failed["stage"]["attempts"], 0)
        status = self.service.resume(self.actor, task, foreground=True)
        self.assertEqual(status["status"], "completed", status)
        self.assertEqual(status["stage"]["attempts"], 1)

    def test_stop_wins_before_child_admission_and_first_work_survives_resume(self):
        task = self.create()
        pending, allow_spawn = Event(), Event()
        spawn = self.service._spawn
        def pause(*args):
            pending.set()
            if not allow_spawn.wait(timeout=10):
                raise RuntimeError("fixture did not release startup")
            return spawn(*args)
        self.service._spawn = pause
        with ThreadPoolExecutor(1) as pool:
            running = pool.submit(self.service.start, self.actor, task, True)
            try:
                self.assertTrue(pending.wait(timeout=10))
                status = self.service.request_stop(self.actor, task)
                self.assertEqual(status["status"], "stopping")
                self.assertFalse((self.worker_home / "attempts").exists())
            finally:
                allow_spawn.set()
            stopped = running.result(timeout=15)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["stage"]["attempts"], 0)
        self.service._spawn = spawn
        resumed = self.service.resume(self.actor, task, foreground=True)
        self.assertEqual(resumed["status"], "completed", resumed)
        self.assertEqual(resumed["stage"]["attempts"], 1)

    def test_startup_without_receipt_is_durable_unknown(self):
        task = self.create()
        def missing_receipt(*_):
            return subprocess.Popen(["/usr/bin/true"], start_new_session=True)
        with patch.object(self.service, "_spawn_child", side_effect=missing_receipt):
            self.assert_error("RUN_UNSETTLED", self.service.start, self.actor, task)
        status = self.status(task)
        self.assertEqual(status["status"], "delivery_unknown")
        self.assertEqual(status["run_history"][0]["phase"], "unknown")
        self.assertFalse((self.worker_home / "attempts").exists())
        self.assert_error("RUN_UNSETTLED", self.service.resume, self.actor, task)

    def test_startup_retries_a_task_read_during_atomic_replacement(self):
        task = self.create()
        await_start = self.service._await_start
        load_task = self.store.load_task

        def wait_during_replacement(*args):
            raced = False

            def load(task_id):
                nonlocal raced
                if not raced:
                    raced = True
                    raise TaskError("BUSY")
                return load_task(task_id)

            with patch.object(self.store, "load_task", side_effect=load):
                await_start(*args)

        with patch.object(self.service, "_await_start", side_effect=wait_during_replacement):
            status = self.service.start(self.actor, task, foreground=True)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["stage"]["attempts"], 1)
        self.assertEqual((self.worker_home / "attempts").read_text().splitlines(), ["work"])

    def test_current_native_lease_refuses_a_new_driver_despite_free_flock(self):
        task = self.create()
        stored = self.store.load_task(task)
        state, _, generation, _ = self.store._kernel_facts(stored)
        with ato_config.use_unified_kernel(self.store.kernel_root, self.store.native_home):
            bus = transport.managed_bus()
            resource = recovery.execution_resource(state["repo"])
            token, fence = bus.acquire_execution(resource, project=state["repo"],
                task_id=stored["workflow"]["directory"], contract_hash=state["contract_hash"],
                generation=generation, role="session_host", owner="fixture-native", source="offline test")
            self.assert_error("BUSY", self.service.start, self.actor, task)
            bus.release_execution(resource, token, fence, generation=generation)
        self.assertEqual(self.status(task)["run_history"][0]["phase"], "reserved")
        self.assertFalse((self.worker_home / "attempts").exists())

    def test_exhausted_stage_attempts_do_not_reset_on_resume(self):
        task = self.create(max_attempts=1)
        (self.worker_home / "mode").write_text("blocked")
        blocked = self.service.start(self.actor, task, foreground=True)
        self.assertEqual(blocked["status"], "blocked", blocked)
        self.assert_error("NOT_RESUMABLE", self.service.resume, self.actor, task)
        self.assertEqual(self.status(task)["stage"]["attempts"], 1)
        self.assertEqual((self.worker_home / "attempts").read_text().splitlines(), ["work"])

    def test_prepared_settlement_reconciles_the_same_run_without_replay(self):
        task = self.create()
        finished = self.service.start(self.actor, task, foreground=True)
        run_id = finished["execution"]["run_id"]
        stored = self.store.load_task(task)
        # Model interrupted publication after the exact driver had prepared
        # terminal evidence, with the last committed receipt still active.
        with self.store._mutation(task, stored["revision"]) as (fd, ledger, _):
            ledger["run_heads"][run_id].pop()
            self.store._advance(fd, ledger)
        self.assertEqual(self.status(task)["status"], "delivery_unknown")
        settled = self.service.reconcile(self.actor, task)
        self.assertEqual(settled["status"], "completed", settled)
        self.assertEqual(settled["product"], finished["product"])
        self.assertEqual(len(settled["run_history"]), 1)
        self.assertEqual(settled["stage"]["attempts"], 1)

    def test_product_mutation_during_frozen_checks_cannot_qualify_review(self):
        check = ["/bin/sh", "-c", "printf mutation >> delivery.txt"]
        task = self.create(reviewer=dict(agent=dict(kind="codex")), checks=[["true"], check], preset=True)
        status = self.service.start(self.actor, task, foreground=True)
        self.assertEqual(status["reason"], "PRODUCT_CHANGED_DURING_CHECKS", status)
        self.assertIsNone(status["product"])
        self.assertFalse((self.reviewer_home / "round").exists())

    def test_recheck_pins_product_and_does_not_restart_worker(self):
        task = self.create()
        first = self.service.start(self.actor, task, foreground=True)
        self.assertEqual(first["status"], "completed", first)
        root = Path(first["product"]["worktree"])
        (root / "delivery.txt").write_text("external edit\n")
        self.assertEqual(self.status(task)["reason"], "PRODUCT_CHANGED")
        updated = self.service.recheck(self.actor, task, checks_only=True, foreground=True)
        self.assertEqual(updated["status"], "completed", updated)
        self.assertNotEqual(first["product"]["product_id"], updated["product"]["product_id"])
        self.assertNotEqual(first["product"]["checks_sha256"], updated["product"]["checks_sha256"])
        self.assertEqual(updated["stage"]["attempts"], 1)
        with self.service.product_transaction(task) as snapshot:
            self.assertEqual(snapshot["product"], updated["product"])

    def test_review_repair_keeps_same_workflow_and_review_mutation_fences(self):
        reviewer = dict(agent=dict(kind="codex"), max_review_rounds=2)
        (self.reviewer_home / "mode").write_text("repair")
        task = self.create(reviewer=reviewer)
        status = self.service.start(self.actor, task, foreground=True)
        self.assertEqual(status["status"], "completed", status)
        self.assertEqual(status["stage"]["attempts"], 2)
        self.assertEqual(status["stage"]["stage_id"], "review-fix-1")
        self.assertEqual(status["review"]["verdict"], "approve")
        self.assertEqual(len(status["run_history"]), 1)
        (self.reviewer_home / "mode").write_text("mutate")
        other = self.create(reviewer=reviewer)
        blocked = self.service.start(self.actor, other, foreground=True)
        self.assertEqual(blocked["reason"], "REVIEWER_MUTATION_VIOLATION", blocked)
        self.assertIsNone(blocked["product"])
        self.assert_error("NOT_RESUMABLE", self.service.resume, self.actor, other)
        self.assert_error("NOT_RESUMABLE", self.service.recheck, self.actor, other)


if __name__ == "__main__":
    unittest.main()
