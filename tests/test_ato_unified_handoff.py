"""Offline controller handoff and Reviewer selection at the T3 execution fence."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from hashlib import sha256
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

import ato_binding
import ato_config
import ato_execution
import ato_process
from ato_binding import BindingDecision, BindingGrant, BindingService, committed_binding_changes, read_binding
from ato_task_store import InitialBinding, SubmissionGrant, TaskError, TaskStore, canonical_json, new_id
from hub_contract import HubAgentSpec
import recovery
import transport


WORKER = """
import json
from pathlib import Path
import sys
import time

sys.stdin.read()
home = Path.home()
with (home / "attempts").open("a") as log:
    log.write("work\\n")
mode = (home / "mode").read_text() if (home / "mode").exists() else ""
if mode == "linger":
    time.sleep(90)
elif mode == "unknown":
    sys.exit(17)
Path("artifact.txt").write_text("fixture product\\n")
Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps(dict(
    status="ready_for_checks", summary="fixture done", next_step="", blocker="", retry_after_seconds=0)))
"""

REVIEWER = """
import json
from pathlib import Path
import sys
import time

sys.stdin.read()
home = Path.home()
(home / "started").touch()
while (home / "pause").exists():
    time.sleep(.02)
print(json.dumps(dict(verdict="approve", summary="fixture only", findings=[],
                      checked_items=["delivery"], residual_risks=[])))
"""


class UnifiedHandoffTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="ato-handoff-"))).resolve()
        self.repo = self.root / "repository"
        self.repo.mkdir()
        git_env = dict(PATH=os.defpath, LC_ALL="C", GIT_CONFIG_NOSYSTEM="1",
                       GIT_CONFIG_GLOBAL=os.devnull, GIT_ALLOW_PROTOCOL="", GIT_AUTHOR_NAME="ATO Fixture",
                       GIT_AUTHOR_EMAIL="ato@example.invalid", GIT_COMMITTER_NAME="ATO Fixture",
                       GIT_COMMITTER_EMAIL="ato@example.invalid")
        for args in (("init", "-q"), ("add", "."), ("commit", "-qm", "fixture base")):
            if args[0] == "add":
                (self.repo / ".gitignore").write_text(".codex/tasks/\n")
                (self.repo / "source.txt").write_text("base\n")
            subprocess.run(["git", "-C", str(self.repo), *args], env=git_env,
                           check=True, capture_output=True, timeout=15)
        self.identities = {
            "old": ("controller", "controller-old"), "new": ("controller", "controller-new"),
            "other": ("controller", "controller-other"), "inbox": ("interaction", "inbox-old"),
            "destination": ("interaction", "inbox-new"), "user": ("user", "actual-user"),
            "unrelated": ("controller", "controller-unrelated"),
        }
        self.credentials = {name: object() for name in self.identities}
        self.enrollments = {identity for name, identity in self.identities.items()
                            if name not in {"unrelated", "user"}}
        self.revoked = set()
        self.endpoint_override = {}
        self.events = {}
        self.binding_revision_overrides = {}
        self.pending_ref = new_id("request")
        self.store = TaskStore(self.root / "ato", native_home=self.root / "native", runtime_seal="5" * 64,
            authenticate=lambda proof: next((name for name, key in self.credentials.items() if key is proof), None),
            can_observe=lambda actor, scope: actor in self.credentials and scope.repo_key == str(self.repo / ".git"),
            authorize_submit=lambda spec, actor, binding: SubmissionGrant(
                "fixture-authorization", spec.repository.key, spec.spec_sha256, binding)
                if actor is self.credentials["user"] else None)
        hooks = ato_execution.ExecutionHooks(
            binding=partial(read_binding, self.store),
            pending=lambda task, binding: [{"request_id": self.pending_ref, "text": "private human prompt"}])
        self.execution = ato_execution.ExecutionService(self.store, hooks=hooks, authorize=self.authorize_execution)
        self.binding = BindingService(self.execution, actor_grant=self.actor_grant,
            endpoint_grant=self.endpoint_grant, verify_decision=self.verify_decision,
            reviewer_supported=lambda scope, agent, mode: (
                agent.kind in {"codex", "claude"} and agent.model != "unsupported" and mode != "require_resume"))

    def actor_grant(self, actor, scope, binding, operation):
        role, ref = self.identities[actor]
        if ref in self.revoked:
            return None
        revision = self.binding_revision_overrides.get(actor, binding["revision"])
        return BindingGrant(scope, ref, role, revision,
                            frozenset({"binding", "handoff", "reviewer", "initial", "stop", "resume", "recheck"}))

    def endpoint_grant(self, ref, scope, role, revision):
        if (role, ref) not in self.enrollments or ref in self.revoked:
            return None
        grant = BindingGrant(scope, ref, role, revision)
        return replace(grant, **self.endpoint_override.get((ref, revision), {}))

    def verify_decision(self, scope, operation, evidence, digest):
        event = self.events.get(digest)
        if event != (operation, evidence, scope):
            return None
        grant = BindingGrant(scope, "actual-user", "user", evidence["binding_revision"], frozenset({operation}))
        return BindingDecision(grant, evidence["source"], digest)

    def authorize_execution(self, actor, scope, binding, operation):
        grant = self.actor_grant(actor, scope, binding, operation)
        return bool(grant and grant.role == "controller" and grant.ref == binding["controller_ref"]
                    and grant.revision == binding["revision"] and operation in grant.operations)

    def create(self, *, reviewer=False):
        spec = dict(schema_version=1, repo=str(self.repo), base_ref="HEAD",
            worker_contract=dict(objective="make a local artifact", scope="worktree only",
                authorization="fixture user event",
                acceptance=[dict(id="check", description="fixture check", checks=[["true"]]),
                            dict(id="manual", kind="manual", description="human acceptance")],
                stages=[dict(id="work", covers=["check"], plan=dict(objective="write artifact", tasks=[
                    dict(id="artifact", prompt="write the requested artifact", checks=[["true"]])]))]))
        if reviewer:
            spec["reviewer"] = dict(agent=dict(kind="codex", model="initial-review"),
                                    allowed_kinds=["codex", "claude"], max_review_rounds=3)
        spec_file = self.root / "spec.json"
        spec_file.write_bytes(canonical_json(spec))
        normalized = self.store.normalize_spec(self.repo, spec_file)
        return self.store.create_task(normalized, self.credentials["user"],
            InitialBinding("controller-old", "inbox-old"))["task_id"]

    def evidence(self, task_id, /, operation="handoff", *, authorized=True, **fields):
        task = self.store.load_task(task_id)
        scope = self.store._visible_scope(task_id, "user")
        binding = read_binding(self.store, task, self.store.load_spec(task_id))
        evidence = dict(schema_version=1, task_id=task_id, spec_sha256=scope.spec_sha256,
                        binding_revision=binding["revision"], source="fixture-event-" + str(len(self.events)))
        if operation == "handoff":
            evidence.update({"from": binding["controller_ref"], "to": "controller-new", "interaction": "inbox-new"})
        else:
            evidence["reviewer"] = dict(kind="claude", model="next-review")
        evidence.update(fields)
        path = self.root / ("decision-" + str(len(list(self.root.glob("decision-*")))) + ".json")
        data = canonical_json(evidence)
        path.write_bytes(data)
        if authorized:
            self.events[sha256(data).hexdigest()] = (operation, evidence, scope)
        return path

    def handoff(self, task, path, *, actor="old", target="controller-new", route="inbox-new"):
        return self.binding.handoff(self.credentials[actor], task, target, route, path)

    def select(self, task, path, *, actor="old", agent=None):
        return self.binding.select_reviewer(self.credentials[actor], task,
                                            agent or HubAgentSpec("claude", "next-review"), path)

    def records(self, task):
        return sorted((self.store._task_dir(task) / "bindings").glob("change-*.json"))

    def assert_error(self, code, method, *args, **kwargs):
        with self.assertRaises(TaskError) as caught:
            method(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def test_scope_role_identity_and_exact_actual_user_decision(self):
        task = self.create()
        evidence = self.evidence(task)
        self.assert_error("FORBIDDEN", self.handoff, task, evidence, actor="unrelated")
        self.assert_error("FORBIDDEN", self.handoff, task, evidence, actor="inbox")
        forged = self.evidence(task, authorized=False)
        self.assert_error("FORBIDDEN", self.handoff, task, forged)
        # A grant for the same repo with a different task, a grant for a clone,
        # a stale grant and a wrong-role grant all fail destination checks.
        scope = self.store._visible_scope(task, "user")
        for override in ({"scope": replace(scope, task_id=new_id("task"))},
                         {"scope": replace(scope, repo_key=str(self.root / "clone/.git"))},
                         {"scope": replace(scope, spec_sha256="f" * 64)},
                         {"revision": 1}, {"role": "interaction"}):
            with self.subTest(override=override):
                self.endpoint_override[("controller-new", 2)] = override
                self.assert_error("FORBIDDEN", self.handoff, task, evidence)
        self.endpoint_override.clear()
        self.revoked.add("inbox-old")
        self.assert_error("FORBIDDEN", self.handoff, task, evidence)
        self.revoked.clear()
        self.assertEqual(self.records(task), [])
        self.assert_error("FORBIDDEN", self.handoff, task, evidence, actor="other")
        self.assert_error("FORBIDDEN", self.handoff, task, forged, actor="new")
        self.assertEqual(self.handoff(task, evidence, actor="new")["controller_ref"], "controller-new")

    def test_read_and_replay_use_the_committed_binding_and_current_grant(self):
        task = self.create(reviewer=True)
        old_task = self.store.load_task(task)
        evidence = self.evidence(task)
        snapshot = self.handoff(task, evidence)
        self.assertEqual(snapshot["revision"], 2)
        self.assertEqual(snapshot["interaction_ref"], "inbox-new")
        self.assertEqual(snapshot["review_revision"], 1)
        self.assertEqual(snapshot["reviewer"]["kind"], "codex")
        self.assertEqual(snapshot["run_owner"]["run_id"], old_task["run_id"])
        self.assertEqual(snapshot["run_owner"]["state"], "idle")
        self.assertNotIn("unresolved_request_ids", snapshot)
        self.assert_error("FORBIDDEN", self.binding.binding, self.credentials["old"], task)
        self.assert_error("FORBIDDEN", self.binding.binding, self.credentials["inbox"], task)
        self.assertEqual(self.binding.binding(self.credentials["destination"], task), snapshot)
        self.binding_revision_overrides["new"] = 1
        self.assert_error("FORBIDDEN", self.binding.binding, self.credentials["new"], task)
        self.binding_revision_overrides.clear()
        self.assert_error("BINDING_CHANGED", self.handoff, task, evidence, actor="new")
        record, = self.records(task)
        row = json.loads(record.read_bytes())
        self.assertEqual(row["before"]["controller_ref"], "controller-old")
        self.assertEqual(row["after"]["controller_ref"], "controller-new")
        self.assertEqual(row["observation"]["unresolved_request_ids"], [self.pending_ref])
        self.assertNotIn("private human prompt", record.read_text())
        self.assertNotIn("fixture-event-", record.read_text())
        self.assertEqual(self.store.load_task(task)["binding"], old_task["binding"])
        self.assertEqual(self.store.load_task(task)["run_hashes"], old_task["run_hashes"])
        # A new reader reconstructs the same committed binding and fails
        # closed if the retained immutable bytes no longer match their name.
        self.assertEqual(read_binding(self.store, old_task, self.store.load_spec(task))["revision"], 2)
        row["after"]["controller_ref"] = "controller-other"
        record.write_bytes(canonical_json(row))
        self.assert_error("BINDING_CHANGED", self.binding.binding, self.credentials["new"], task)

    def test_evidence_file_cli_and_binding_must_all_agree(self):
        task = self.create()
        valid = self.evidence(task)
        for overrides in ({"task_id": new_id("task")}, {"binding_revision": True}, {"from": "controller-other"},
                          {"interaction": "inbox-old"}, {"source": "../../event"}, {"extra": "unexpected"}):
            with self.subTest(overrides=overrides):
                self.assert_error("INVALID_EVIDENCE", self.handoff, task, self.evidence(task, **overrides))
        duplicate = self.root / "duplicate.json"
        duplicate.write_bytes(valid.read_bytes().replace(b'{"', b'{"schema_version":1,"', 1))
        self.assert_error("INVALID_EVIDENCE", self.handoff, task, duplicate)
        link = self.root / "linked-evidence"
        link.symlink_to(valid)
        self.assert_error("INVALID_EVIDENCE", self.handoff, task, link)
        self.assert_error("INVALID_EVIDENCE", self.handoff, task, valid, route=None)
        keep = self.evidence(task, interaction="inbox-old")
        self.assertEqual(self.handoff(task, keep, route=None)["interaction_ref"], "inbox-old")

    def test_route_only_handoff_keeps_controller_and_requires_current_grants(self):
        task = self.create()
        decision = self.evidence(task, to="controller-old")
        self.endpoint_override[("controller-old", 2)] = {"revision": 1}
        self.assert_error("FORBIDDEN", self.handoff, task, decision, target="controller-old")
        self.endpoint_override.clear()
        moved = self.handoff(task, decision, target="controller-old")
        self.assertEqual((moved["controller_ref"], moved["interaction_ref"]), ("controller-old", "inbox-new"))
        self.assertEqual(moved["review_revision"], 1)
        self.assert_error("FORBIDDEN", self.binding.binding, self.credentials["inbox"], task)
        self.assertEqual(self.binding.binding(self.credentials["destination"], task), moved)

    def test_interrupted_publication_keeps_old_binding_and_missing_commit_fails_closed(self):
        task = self.create()
        evidence = self.evidence(task)
        save = ato_binding._save

        def interrupt(fd, name, value, **options):
            if name == "current.json" and value["receipt_id"] is not None:
                raise OSError("simulated failure before pointer publication")
            return save(fd, name, value, **options)

        with patch.object(ato_binding, "_save", side_effect=interrupt):
            self.assert_error("STATE_CORRUPT", self.handoff, task, evidence)
        self.assertEqual(len(self.records(task)), 1)
        before = self.binding.binding(self.credentials["old"], task)
        self.assertEqual(before["revision"], 1)
        self.assertIsNone(before["receipt_id"])
        self.assertEqual(committed_binding_changes(self.store, self.store.load_task(task),
                                                  self.store.load_spec(task)), [])
        committed = self.handoff(task, evidence)
        self.assertEqual(committed["revision"], 2)
        history = committed_binding_changes(self.store, self.store.load_task(task), self.store.load_spec(task))
        self.assertEqual([row["receipt_id"] for row in history], [committed["receipt_id"]])
        record = next(path for path in self.records(task) if committed["receipt_id"] in path.name)
        record.unlink()
        self.assert_error("BINDING_CHANGED", self.binding.binding, self.credentials["old"], task)

    def test_simultaneous_handoffs_only_publish_one_revision(self):
        task = self.create()
        first = self.evidence(task)
        second = self.evidence(task, to="controller-other")
        barrier = Barrier(2)
        observe = self.binding._observe

        def pause(*args):
            result = observe(*args)
            barrier.wait(timeout=10)
            return result

        def compete(path, target):
            try:
                return self.handoff(task, path, target=target)["controller_ref"]
            except TaskError as exc:
                return exc.code

        with patch.object(self.binding, "_observe", side_effect=pause), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(compete, path, target) for path, target in
                       ((first, "controller-new"), (second, "controller-other"))]
            outcomes = [future.result(timeout=15) for future in futures]
        self.assertEqual(len(self.records(task)), 1)
        self.assertEqual(sum(outcome in {"controller-new", "controller-other"} for outcome in outcomes), 1)
        self.assertEqual(sum(outcome in {"BUSY", "BINDING_CHANGED"} for outcome in outcomes), 1)

    def test_stop_wins_against_an_observed_handoff_and_remains_effective(self):
        task = self.create()
        evidence = self.evidence(task)
        observed, proceed = Event(), Event()
        observe = self.binding._observe

        def pause(*args):
            result = observe(*args)
            observed.set()
            if not proceed.wait(timeout=10):
                raise RuntimeError("test did not release handoff")
            return result

        with patch.object(self.binding, "_observe", side_effect=pause), ThreadPoolExecutor(1) as pool:
            future = pool.submit(self.handoff, task, evidence)
            try:
                self.assertTrue(observed.wait(timeout=10))
                stopped = self.execution.request_stop(self.credentials["old"], task)
                self.assertTrue(stopped["execution"]["stop_requested"])
            finally:
                proceed.set()
            self.assert_error("BUSY", future.result, timeout=10)
        self.assertEqual(self.records(task), [])
        self.assertTrue(self.handoff(task, evidence)["run_owner"]["stop_requested"])
        self.assertEqual(self.store.load_task(task)["stop_epoch"], 1)
        self.assert_error("FORBIDDEN", self.execution.request_stop, self.credentials["old"], task)

    def test_reviewer_requires_allowed_kind_supported_profile_and_preserves_routes(self):
        task = self.create(reviewer=True)
        before = self.store.load_spec(task)["worker_contract"]["agent"]
        for spec, code in ((dict(kind="agy"), "FORBIDDEN"),
                           (dict(kind="claude", model="unsupported"), "UNSUPPORTED_CAPABILITY")):
            self.assert_error(code, self.select, task, self.evidence(task, "reviewer", reviewer=spec),
                              agent=HubAgentSpec(**spec))
        self.assert_error("FORBIDDEN", self.select, task, self.evidence(task, "reviewer", authorized=False))
        new = self.select(task, self.evidence(task, "reviewer"))
        self.assertEqual((new["revision"], new["review_revision"]), (2, 2))
        self.assertEqual((new["controller_ref"], new["interaction_ref"]), ("controller-old", "inbox-old"))
        self.assertEqual(new["reviewer"], dict(kind="claude", model="next-review", effort=None))
        self.assertEqual(self.store.load_spec(task)["worker_contract"]["agent"], before)
        path = self.evidence(task, interaction="inbox-old")
        after = self.handoff(task, path, route=None)
        self.assertEqual(after["reviewer"], new["reviewer"])
        self.assertEqual((after["revision"], after["review_revision"]), (3, 2))
        first, second = [json.loads(path.read_bytes()) for path in self.records(task)]
        self.assertEqual(second["previous_receipt"], new["receipt_id"])
        self.assertEqual(second["before"], first["after"])

    def fake_tool(self, name, code):
        script = self.root / (name + ".py")
        script.write_text(code)
        executable = self.root / name
        executable.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", str(script)]) + ' "$@"\n')
        executable.chmod(0o700)
        home = self.root / (name + "-home")
        home.mkdir(mode=0o700)
        return str(executable), home

    def enable_offline_execution(self):
        worker, self.worker_home = self.fake_tool("fixture-worker", WORKER)
        codex, self.codex_home = self.fake_tool("fixture-review-codex", REVIEWER)
        claude, self.claude_home = self.fake_tool("fixture-review-claude", REVIEWER)
        self.execution.profile = ato_execution.HostProfile(worker, str(self.worker_home),
            (("codex", codex, str(self.codex_home)), ("claude", claude, str(self.claude_home))), fixture=True)
        # Add the T6-equivalent read hook to a private temporary bootstrap.
        # The production driver still checks the bootstrap identity and uses
        # its real inherited pipe, Repo flock and kernel lease.
        entry = self.root / "fixture_runtime.py"
        source = Path(ato_execution.__file__).resolve().parent
        entry.write_text(
            "from functools import partial\nfrom pathlib import Path\nimport sys\n"
            f"sys.path.insert(0, {str(source)!r})\n"
            "import ato_execution, ato_runtime\nfrom ato_binding import read_binding\n"
            "ato_execution._ENTRY = Path(__file__).resolve()\n"
            "ato_runtime.runtime_hooks = lambda store: ato_execution.ExecutionHooks("
            "binding=partial(read_binding, store))\n"
            "sys.exit(ato_runtime.main(sys.argv[1:]))\n")
        self.enterContext(patch.object(ato_execution, "_ENTRY", entry))
        self.children = []
        spawn = self.execution._spawn

        def remember(*args):
            child = spawn(*args)
            self.children.append(child)
            return child

        self.execution._spawn = remember
        self.addCleanup(self.reap)

    def reap(self):
        for child in self.children:
            if child.poll() is not None:
                continue
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=4)

    def wait_for(self, operation, condition=bool):
        deadline = time.monotonic() + 16
        while time.monotonic() < deadline:
            try:
                result = operation()
                if condition(result):
                    return result
            except TaskError as exc:
                if exc.code != "BUSY":
                    raise
            time.sleep(.03)
        self.fail("Offline fixture did not settle")

    def status(self, task):
        return self.execution.task_status(self.credentials["user"], task)

    def lease(self, task):
        record = self.store.load_task(task)
        with ato_config.use_unified_kernel(self.store.kernel_root, self.store.native_home):
            return transport.managed_bus().execution_lease(recovery.execution_resource(record["worktree"]["path"]))

    def test_live_run_remains_with_its_actual_owner_until_stop_release_and_resume(self):
        self.enable_offline_execution()
        task = self.create()
        (self.worker_home / "mode").write_text("linger")
        self.execution.start(self.credentials["old"], task)
        self.wait_for((self.worker_home / "attempts").exists)
        active = self.status(task)
        owner = active["execution"]
        self.assertEqual(owner["pid"], self.children[-1].pid)
        original_lease = self.lease(task)
        self.assertEqual(original_lease["state"], "active")
        transferred = self.handoff(task, self.evidence(task))
        self.assertEqual(transferred["run_owner"], owner)
        lease = self.lease(task)
        for field in ("resource", "task_id", "fence", "generation", "role", "owner", "state"):
            self.assertEqual(lease[field], original_lease[field])
        self.assert_error("BUSY", self.execution.resume, self.credentials["new"], task)
        self.assert_error("FORBIDDEN", self.execution.request_stop, self.credentials["old"], task)
        self.execution.request_stop(self.credentials["new"], task)
        self.children[-1].wait(timeout=10)
        stopped = self.wait_for(lambda: self.status(task), lambda status: status["status"] == "stopped")
        released = self.lease(task)
        self.assertEqual(released["state"], "released")
        self.assertIsNone(ato_process.identity(owner["pid"]))
        self.assertEqual(stopped["stage"]["attempts"], 1)
        handoff, = self.records(task)
        self.assertEqual(json.loads(handoff.read_bytes())["observation"]["stage"]["attempts"],
                         active["stage"]["attempts"])
        (self.worker_home / "mode").unlink()
        resumed = self.execution.resume(self.credentials["new"], task, foreground=True)
        self.assertEqual(resumed["task_id"], task)
        self.assertEqual(resumed["status"], "completed")
        self.assertEqual(resumed["stage"]["attempts"], 2)
        self.assertEqual([row["kind"] for row in resumed["run_history"]], ["initial", "resume"])
        self.assertNotEqual(resumed["execution"]["run_id"], owner["run_id"])
        self.assertGreater(self.lease(task)["fence"], released["fence"])

    def test_unknown_run_ownership_stays_fenced_after_handoff(self):
        self.enable_offline_execution()
        task = self.create()
        (self.worker_home / "mode").write_text("unknown")
        unknown = self.execution.start(self.credentials["old"], task, foreground=True)
        self.assertEqual(unknown["status"], "delivery_unknown")
        transferred = self.handoff(task, self.evidence(task))
        self.assertEqual(transferred["run_owner"]["state"], "unknown")
        self.assertFalse(transferred["run_owner"]["verified"])
        self.assertEqual(transferred["run_owner"]["run_id"], unknown["execution"]["run_id"])
        self.assert_error("RUN_UNSETTLED", self.execution.resume, self.credentials["new"], task)
        self.assertEqual((self.worker_home / "attempts").read_text().splitlines(), ["work"])

    def test_active_review_refuses_switch_and_new_revision_requires_new_review(self):
        self.enable_offline_execution()
        task = self.create(reviewer=True)
        (self.codex_home / "pause").touch()
        self.execution.start(self.credentials["old"], task)
        self.wait_for((self.codex_home / "started").exists)
        evidence = self.evidence(task, "reviewer")
        try:
            self.assert_error("BUSY", self.select, task, evidence)
            self.assertFalse(self.records(task))
        finally:
            (self.codex_home / "pause").unlink()
        self.children[-1].wait(timeout=10)
        approved = self.status(task)
        self.assertEqual(approved["status"], "completed")
        switched = self.select(task, evidence)
        self.assertEqual(switched["review_revision"], 2)
        check, = self.records(task)
        observation = json.loads(check.read_bytes())["observation"]
        self.assertEqual(observation["stage"]["attempts"], 1)
        self.assertEqual(observation["review_receipt_id"], approved["review"]["receipt_id"])
        self.assertEqual(observation["product"]["checks_sha256"], approved["product"]["checks_sha256"])
        invalidated = self.status(task)
        self.assertEqual(invalidated["reason"], "CHECKS_REQUIRED")
        self.assertIsNone(invalidated["product"])
        fresh = self.execution.recheck(self.credentials["old"], task, foreground=True)
        self.assertEqual(fresh["status"], "completed")
        self.assertTrue((self.claude_home / "started").exists())
        self.assertEqual((self.worker_home / "attempts").read_text().splitlines(), ["work"])
        self.assertNotEqual(fresh["review"]["receipt_id"], approved["review"]["receipt_id"])
