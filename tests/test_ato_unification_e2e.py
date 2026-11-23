"""Exercise the installed, sealed ATO CLI with local synthetic executables."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest

import agent_registry
import ato_process
from ato_host import Host
from ato_task_store import TaskStore, normalize_repository
from scripts import release_snapshot as release

from release_support import CURRENT, fake_env, legacy_entry, snapshot, unseal, write
from test_ato_unified_execution import git


def agent_ref(digit):
    return "agentref-" + digit * 32


WORKER = r'''
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

assert sys.argv[1] == "exec"
assert "--sandbox" not in sys.argv
assert 'default_permissions="ato-host"' in sys.argv
assert any(s.startswith('permissions.ato-host=') and 'network={enabled=false}' in s for s in sys.argv)
assert "ATO_ACTOR_FILE" not in os.environ and "OPENAI_API_KEY" not in os.environ
assert sys.stdin.read()
native = Path(os.environ["CODEX_HOME"])
target = Path("delivery.txt")
target.write_text((target.read_text() if target.exists() else "") + "fixture output\n")
with (native / "attempts").open("a") as log:
    log.write(str(Path.cwd()) + "\n")
mode = (native / "mode").read_text()
if mode == "unknown":
    sys.exit(17)
if mode == "hold":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-IB", "-c", "import signal,time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(35)"],
        stdout=subprocess.PIPE, text=True)
    assert child.stdout.readline().strip() == "ready"
    ready = native / "ready.tmp"
    ready.write_text(json.dumps(dict(pid=os.getpid(), child=child.pid)))
    ready.rename(native / "ready.json")
    time.sleep(35)
result = dict(status="ready_for_checks", summary="fixture done", next_step="", blocker="",
              retry_after_seconds=0)
if mode == "host":
    result.update(status="needs_host", summary="fixture needs host action",
                  next_step="Record the authorized fixture action")
Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps(result))
'''


REVIEWER = r'''
import json
import os
from pathlib import Path
import sys

assert "ATO_ACTOR_FILE" not in os.environ and "OPENAI_API_KEY" not in os.environ
if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.171.0")
    sys.exit(0)
assert sys.argv[1] == "exec"
assert "--sandbox" not in sys.argv
assert 'default_permissions="ato-host"' in sys.argv
assert "fixture output" in Path("delivery.txt").read_text()
assert "Perform a read-only technical review" in sys.stdin.read()
verdict = dict(verdict="approve", summary="Synthetic review passed", findings=[],
               checked_items=["fixture product"], residual_risks=[])
Path(sys.argv[sys.argv.index("-o") + 1]).write_text(json.dumps(verdict))
print(json.dumps(dict(type="thread.started", thread_id="private-fixture-review-session")))
'''


class InstalledATO(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="ato-e2e-"))).resolve()
        self.env = fake_env(self.root)
        self.env["OPENAI_API_KEY"] = "private-fixture-environment-token"
        self.bin = self.root / "isolated bin"
        self.skills = self.root / "isolated skills"
        self.source = snapshot(self.root / "sealed source", executable=True)
        self.addCleanup(unseal, self.source)
        self.python = self.source / ".venv/bin/python"
        self.launcher = self.bin / "ato"
        self.running = {}
        self.addCleanup(self.stop_fixtures)

    def command(self, *argv, actor=None):
        env = dict(self.env)
        if actor is not None:
            env["ATO_ACTOR_FILE"] = str(actor)
        return subprocess.run([str(arg) for arg in argv], cwd=self.root, env=env,
                              capture_output=True, text=True, timeout=45)

    def json_command(self, *argv, actor=None):
        result = self.command(*argv, actor=actor)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stderr, "")
        return json.loads(result.stdout)

    def cli(self, *argv, actor=None, error=None):
        result = self.command(self.launcher, *argv, actor=actor)
        self.assertEqual(result.stderr, "")
        response = json.loads(result.stdout)
        self.assertEqual(set(response), {"schema_version", "ok", "data", "error"})
        self.assertEqual(response["schema_version"], 1)
        self.assertIs(response["ok"], error is None, response)
        for private in (self.env["OPENAI_API_KEY"], "private-fixture-review-session",
                        "private-fixture-challenge-", str(self.root / "actors"),
                        str(self.root / "worker home"), str(self.root / "reviewer home")):
            self.assertNotIn(private, result.stdout)
        if error:
            code, name = error
            self.assertEqual((result.returncode, response["error"]["code"]), (code, name), response)
            self.assertIsNone(response["data"])
            return response["error"]
        self.assertEqual(result.returncode, 0, response)
        self.assertIsNone(response["error"])
        return response["data"]

    def install(self):
        installed = self.json_command(self.python, "-IB", self.source / "scripts/install.py",
                                      "--bin-dir", self.bin, "--skills-dir", self.skills)
        self.assertEqual(installed["launcher"], str(self.launcher))
        self.assertEqual(installed["source"], str(self.source))
        self.assertEqual({path.name for path in self.bin.iterdir()}, {"ato"})
        self.assertEqual(list(self.skills.glob("*/*")), [self.skills / "ato/SKILL.md"])
        self.store = TaskStore(self.env["ATO_HOME"], native_home=self.env["CODEX_HOME"],
                               runtime_seal=installed["release"])

    def host_setup(self, *args, actor=None):
        return self.json_command(self.python, "-IB", self.source / "scripts/ato_host_setup.py",
                                 *args, "--source", "offline operator fixture", actor=actor)

    def profile(self, mode="normal", *, via_setup=False):
        self.worker_home = self.root / "worker home"
        self.native = self.worker_home / ".codex"
        self.reviewer_home = self.root / "reviewer home"
        self.proofs = self.root / "actors"
        for home in (self.worker_home, self.native, self.reviewer_home, self.proofs):
            home.mkdir(mode=0o700)
        write(self.native, "mode", mode)
        worker = self.tool("worker", WORKER)
        reviewer = self.tool("reviewer", REVIEWER)
        profile = dict(worker_executable=str(worker), worker_home=str(self.worker_home),
                       proof_roots=[str(self.proofs)],
                       review_tools=[["codex", str(reviewer), str(self.reviewer_home)]])
        if via_setup:
            self.host_setup("configure", "--profile",
                            write(self.root, "profile-input.json", json.dumps(profile)))
        else:
            self.registry("profile", profile)
        self.host = Host(store=self.store).compose()

    def tool(self, name, script):
        body = write(self.root, name + ".py", script)
        command = shlex.join([str(Path(sys.executable).resolve()), "-IB", str(body)])
        return write(self.root, name, '#!/bin/sh\nexec ' + command + ' "$@"\n', 0o700)

    def registry(self, name, value):
        for directory in (self.store.home, self.store.root, self.store.root / "host"):
            directory.mkdir(mode=0o700, exist_ok=True)
        return write(self.store.root / "host", name + ".json", json.dumps(value))

    def enroll(self, repo, digit, role, reference, **claims):
        identifier = "credential-" + digit * 32
        challenge = "private-fixture-challenge-" + digit
        identity = agent_registry.SessionIdentity("local_cli", "fixture-" + digit, str(repo), "offline-fixture")
        self.registry(identifier, dict(enabled=True, role=role, reference=reference,
                                      identity=identity.to_mapping(),
                                      token_hash=sha256(challenge.encode()).hexdigest(), **claims))
        return write(self.root / "actors", digit + ".json", json.dumps(dict(version=2, reference=reference,
            proof=dict(method="local-cli-token", credential_ref=identifier, challenge=challenge))))

    def endpoint(self, repo_key, reference, role, revision):
        self.registry(reference, dict(enabled=True, reference=reference, repo_key=repo_key,
                                     role=role, revisions=[revision]))

    def submission(self, name, origin, interaction, *, reviewer=False, via_setup=False, host_handoff=False):
        repo = self.root / name
        repo.mkdir()
        git(repo, "init", "-q")
        write(repo, ".gitignore", ".codex/tasks/\n")
        write(repo, "source.txt", "fixture base\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "fixture base")
        check = ["test", "-s", "source.txt"]
        contract = dict(objective="Deliver the local fixture", scope="Isolated checkout",
                        authorization="Synthetic evaluation", limits=dict(max_attempts=3), acceptance=[
                            dict(id="host", kind="command", description="Baseline regression", checks=[check]),
                            dict(id="human", kind="manual", description="Accept the checked fixture product")])
        if host_handoff:
            check[-1] = "host-finished.txt"
            contract["host_handoff"] = dict(source="offline operator fixture",
                                            instructions="Record the completed local host action")
        spec = dict(schema_version=1, repo=str(repo), base_ref="HEAD", worker_contract=contract)
        if reviewer:
            spec["reviewer"] = dict(agent=dict(kind="codex"), max_review_rounds=2)
        spec_file = write(self.root, name + ".json", json.dumps(spec))
        normalized = self.cli("project", "validate", "--repo", repo, "--spec", spec_file)
        self.assertEqual(normalized["repo"], str(repo))
        if via_setup:
            enrolled = self.host_setup("submission", "--repo", repo, "--spec", spec_file)
            self.assertEqual(enrolled["spec_sha256"], normalized["spec_sha256"])
            return repo, spec_file, Path(enrolled["controller_proof"])
        repo_key = normalize_repository(repo).key
        controller, endpoint = agent_ref(origin), agent_ref(interaction)
        self.endpoint(repo_key, controller, "controller", 1)
        self.endpoint(repo_key, endpoint, "interaction", 1)
        grant = dict(reference="approved-" + origin, repo_key=repo_key,
                     spec_sha256=normalized["spec_sha256"],
                     binding=dict(controller_ref=controller, interaction_ref=endpoint, revision=1))
        actor = self.enroll(repo, origin, "controller", controller, submissions=[grant])
        return repo, spec_file, actor

    def scoped(self, task_id, digit, role, reference, revision, *operations):
        task = self.store.load_task(task_id)
        with self.store._private(self.store._task_dir(task_id)) as fd:
            scope = self.store._scope(fd, task_id)
        generation = self.host.execution.task_snapshot(task_id)["generation"]
        return self.enroll(Path(task["repo"]["root"]), digit, role, reference, scope=vars(scope),
                           binding_revision=revision, run_id=task["run_id"], generation=generation,
                           operations=list(operations))

    def ask(self, task_id, actor, question):
        row = json.loads(actor.read_text())
        proof = agent_registry.ActorProof(**row["proof"]), row["reference"], actor
        # The eight public commands intentionally have no request-creation verb.
        return self.host.interaction.ask(proof, task_id, kind="decision", question=question,
                                         reason="Synthetic controller decision")

    def evidence(self, name, **payload):
        return write(self.root, name + ".json", json.dumps(dict(schema_version=1, **payload)))

    def acceptance(self, status, name):
        return self.evidence(name, task_id=status["task_id"], spec_sha256=status["spec_sha256"],
                             product_id=status["product"]["product_id"],
                             checks_sha256=status["product"]["checks_sha256"], criteria=["human"],
                             decision="accept", source="fixture-user", note="Checked this exact fixture product")

    def wait_for(self, query, predicate, *, terminal=()):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            value = query()
            if predicate(value):
                return value
            if isinstance(value, dict) and value.get("status") in terminal:
                self.fail(f"Unexpected terminal fixture status: {value}")
            time.sleep(.1)
        self.fail(f"Fixture did not reach its expected state: {value}")

    def stop_fixtures(self):
        for task_id, actor in self.running.items():
            result = self.command(self.launcher, "stop", task_id, actor=actor)
            if result.returncode == 0:
                self.wait_for(lambda: self.cli("status", task_id, actor=actor),
                              lambda status: status["execution"]["state"] != "stopping")

    def test_frozen_surface_installs_one_launcher_and_keeps_private_commands_sealed(self):
        self.install()
        self.install()
        surface = json.loads((Path(__file__).parent / "fixtures/cli-surface.json").read_text())
        groups = ("project", "agent", "system")
        commands = [(), *((name,) for name in (*surface["daily"], *groups)),
                    *((group, name) for group in groups for name in surface[group])]
        for arguments in commands:
            with self.subTest(arguments=arguments):
                result = self.command(self.launcher, *arguments, "--help")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(result.stdout.startswith("usage: " + " ".join(("ato", *arguments))))
                self.assertEqual(result.stderr, "")
        for command in (*release.LEGACY, "task-run", "run-child", "read-handoff", "observe", "supervise"):
            with self.subTest(retired_or_private=command):
                self.cli(command, error=(2, "USAGE"))
        status = self.cli("system", "status")
        self.assertEqual(status["release"], self.store.runtime_seal)
        self.assertEqual(status["namespace"], "unified-v1")
        self.assertEqual(status["observer"]["state"], "absent")
        self.assertTrue(status["mailbox"]["idle"])
        self.cli("system", "notification-policy")
        self.assertEqual(self.cli("agent", "doctor")["qualification"], "unverified")
        self.cli("list", error=(3, "AUTH_REQUIRED"))
        private = self.source / "ato_runtime.py"
        for command in ("run-child", "observe", "supervise"):
            args = {"run-child": ("task-" + "a" * 32, "run-" + "b" * 32),
                    "supervise": ("task-" + "a" * 32, "run-" + "b" * 32)}.get(command, ())
            result = self.command(self.python, "-IB", private, command, *args)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (3, "", ""))
        self.assertEqual(self.json_command(self.python, "-IB", private, "read-handoff", "handoff-" + "a" * 32),
                         dict(valid=False, action="end_silently", reason="stale_or_unavailable"))
        self.assertFalse(self.store.home.exists())
        self.assertFalse(self.store.native_home.exists())

    def test_installed_host_enrollment_and_scoped_question_without_fixture_grants(self):
        self.install()
        self.profile(via_setup=True)
        repo, spec, bootstrap = self.submission("enrollment fixture", "1", "2", via_setup=True)
        work = self.cli("run", "--repo", repo, "--spec", spec, "--foreground", actor=bootstrap)
        task = work["task_id"]
        controller = self.host_setup("scope", task, "--role", "controller")["proof"]
        user = self.host_setup("scope", task, "--role", "user")["proof"]
        ask = ("agent", "ask", task, "--kind", "decision", "--question", "Review this candidate?",
               "--reason", "Fixture decision")
        self.cli(*ask, actor=user, error=(3, "FORBIDDEN"))
        request = self.cli(*ask, actor=controller)["request_id"]
        self.assertEqual([r["request_id"] for r in self.cli("inbox", actor=user)["items"]], [request])
        self.cli("answer", request, "--answer", "Ready to review", actor=user)
        self.assertEqual(self.cli("inbox", actor=user)["items"][0]["kind"], "manual_acceptance")
        evidence = self.acceptance(work, "enrollment acceptance")
        self.assertEqual(self.cli("accept", task, "--evidence", evidence, actor=user)["status"], "completed")

    def test_installed_host_handoff_requires_actual_attestation_and_continues_checks(self):
        self.install()
        self.profile(mode="host", via_setup=True)
        repo, spec, bootstrap = self.submission("host action fixture", "1", "2",
                                                via_setup=True, host_handoff=True)
        result = self.cli("run", "--repo", repo, "--spec", spec, "--foreground", actor=bootstrap,
                          error=(4, "RUN_UNSETTLED"))
        task_id = result["task_id"]
        status = self.cli("status", task_id, actor=bootstrap)
        self.assertEqual(status["status"], "needs_host")
        controller = self.host_setup("scope", task_id, "--role", "controller")["proof"]
        user = self.host_setup("scope", task_id, "--role", "user")["proof"]
        recheck = ("project", "recheck", task_id, "--checks-only")
        self.cli(*recheck, actor=controller, error=(4, "NOT_RESUMABLE"))
        request = self.cli("agent", "ask", task_id, "--kind", "host_handoff", "--question",
                           "Has the approved host action finished?", "--reason", "Frozen check is blocked",
                           actor=controller)["request_id"]
        self.assertEqual([r["request_id"] for r in self.cli("inbox", actor=user)["items"]], [request])
        self.cli(*recheck, actor=controller, error=(4, "NOT_RESUMABLE"))
        path = Path(self.store.load_task(task_id)["worktree"]["path"])
        write(path, "host-finished.txt", "The fixture operator completed the local action.")
        answer = "The fixture action finished"
        self.host_setup("attest", request, "--answer", answer, "--performed", actor=user)
        self.cli("answer", request, "--answer", answer, actor=user)
        delivered = self.cli(*recheck, actor=controller)
        self.assertEqual(delivered["status"], "needs_acceptance")
        self.assertEqual([r["kind"] for r in delivered["run_history"]], ["initial", "recheck"])
        self.assertEqual((self.native / "attempts").read_text().splitlines(), [str(path)])
        user = self.host_setup("scope", task_id, "--role", "user")["proof"]
        evidence = self.acceptance(delivered, "host action acceptance")
        self.assertEqual(self.cli("accept", task_id, "--evidence", evidence, actor=user)["status"], "completed")

    def test_two_repositories_with_explicit_answer_review_and_versioned_acceptance(self):
        self.install()
        self.profile()
        first, first_spec, origin = self.submission("first repo", "1", "2", reviewer=True)
        second, second_spec, other_origin = self.submission("second repo", "3", "4")
        self.assertTrue((first / "source.txt").stat().st_size)
        launched = self.cli("run", "--repo", first, "--spec", first_spec, "--foreground", actor=origin)
        other = self.cli("run", "--repo", second, "--spec", second_spec, "--foreground", actor=other_origin)
        task_id = launched["task_id"]
        self.assertRegex(task_id, r"^task-[0-9a-f]{32}$")
        self.assertNotEqual(task_id, other["task_id"])
        for repo, actor, own, foreign in ((first, origin, launched, second), (second, other_origin, other, first)):
            self.assertEqual([row["task_id"] for row in self.cli("list", "--repo", repo, actor=actor)["tasks"]],
                             [own["task_id"]])
            self.assertEqual(self.cli("list", "--repo", foreign, actor=actor)["tasks"], [])
            self.assertFalse((repo / "delivery.txt").exists())
            self.assertEqual(git(repo, "status", "--porcelain"), "")
        self.cli("status", task_id, actor=other_origin, error=(3, "NOT_FOUND"))
        status = self.cli("status", task_id, actor=origin)
        self.assertEqual(status["status"], "needs_acceptance")
        self.assertEqual(status["review"]["verdict"], "approve")
        self.assertNotEqual(status["acceptance"]["disposition"], "recorded")
        self.assertEqual(status["stage"]["attempts"], 1)
        trace = self.cli("project", "trace", task_id, actor=origin)
        self.assertEqual(trace["task_id"], task_id)
        self.assertTrue(trace["checks"])
        self.assertTrue(all(row["state"] == "passed" and row["receipt_id"] for row in trace["checks"]))

        task = self.store.load_task(task_id)
        run, receipts = self.store._run(task, task["run_id"])
        launch, = [row for _, row in receipts if row["phase"] == "launching"]
        runtime = json.loads(self.store.read_evidence(task_id, launch["evidence_sha256"]))["runtime"]
        self.assertEqual(run["runtime_seal"], self.store.runtime_seal)
        self.assertEqual((runtime["entry"], runtime["python"]),
                         (str(self.source / "ato_runtime.py"), str(self.python)))
        checkout = Path(task["worktree"]["path"])
        attempts = (self.native / "attempts").read_text().splitlines()
        self.assertEqual(attempts, [str(checkout), self.store.load_task(other["task_id"])["worktree"]["path"]])
        self.assertEqual((checkout / "delivery.txt").read_text(), "fixture output\n")
        requester = self.scoped(task_id, "5", "controller", agent_ref("1"), 1, "observe", "ask")
        user = self.scoped(task_id, "6", "user", agent_ref("2"), 1, "observe", "read", "answer", "accept")
        other_user = self.scoped(other["task_id"], "7", "user", agent_ref("4"), 1, "observe", "read", "answer")
        question = "May the fixture use option A?"
        request_id = self.ask(task_id, requester, question)["request_id"]
        pending = self.cli("status", task_id, actor=origin)
        self.assertEqual(pending["status"], "needs_input")
        self.assertEqual(pending["pending"], [])
        items = self.cli("inbox", actor=user)["items"]
        self.assertEqual([(item["task_id"], item["request_id"], item["question"]) for item in items],
                         [(task_id, request_id, question)])
        answer = "Use option A"
        self.cli("answer", request_id, "--answer", answer, actor=other_user, error=(3, "NOT_FOUND"))
        self.cli("answer", request_id, "--answer", answer, actor=origin, error=(3, "NOT_FOUND"))
        decided = self.cli("answer", request_id, "--answer", answer, actor=user)
        self.assertEqual((decided["task_id"], decided["request_id"], decided["state"]),
                         (task_id, request_id, "answered"))
        self.assertEqual(decided, self.cli("answer", request_id, "--answer", answer, actor=user))
        self.cli("answer", request_id, "--answer", "Use option B", actor=user, error=(4, "DECISION_CONFLICT"))
        manual, = self.cli("inbox", actor=user)["items"]
        self.assertEqual((manual["task_id"], manual["product_id"]), (task_id, status["product"]["product_id"]))
        evidence = self.acceptance(status, "first evidence")
        self.cli("accept", task_id, "--evidence", evidence, actor=origin, error=(3, "FORBIDDEN"))
        accepted = self.cli("accept", task_id, "--evidence", evidence, actor=user)
        self.assertEqual((accepted["task_id"], accepted["status"]), (task_id, "completed"))
        self.assertEqual(accepted["acceptance"]["disposition"], "recorded")
        self.assertEqual(self.cli("accept", task_id, "--evidence", evidence, actor=user)["acceptance"],
                         accepted["acceptance"])
        self.assertEqual(self.cli("inbox", actor=user)["items"], [])

        (checkout / "delivery.txt").write_text("fixture output revised\n")
        changed = self.cli("status", task_id, actor=origin)
        self.assertEqual((changed["status"], changed["reason"]), ("blocked", "PRODUCT_CHANGED"))
        self.cli("accept", task_id, "--evidence", evidence, actor=user, error=(4, "CHECKS_REQUIRED"))
        rechecked = self.cli("project", "recheck", task_id, actor=origin)
        self.assertEqual((rechecked["task_id"], rechecked["status"]), (task_id, "needs_acceptance"))
        self.assertNotEqual(rechecked["product"]["product_id"], status["product"]["product_id"])
        self.assertEqual(rechecked["stage"]["attempts"], 1)
        self.assertEqual(rechecked["acceptance"]["disposition"], "invalidated")
        user = self.scoped(task_id, "6", "user", agent_ref("2"), 1, "observe", "read", "accept")
        self.cli("accept", task_id, "--evidence", evidence, actor=user, error=(4, "PRODUCT_CHANGED"))
        revised = self.acceptance(rechecked, "revised evidence")
        accepted_again = self.cli("accept", task_id, "--evidence", revised, actor=user)
        self.assertEqual(accepted_again["status"], "completed")
        self.assertNotEqual(accepted_again["acceptance"]["receipt_id"], accepted["acceptance"]["receipt_id"])
        self.assertTrue(self.store.read_evidence(task_id, accepted["acceptance"]["receipt_id"]))
        self.assertEqual((self.native / "attempts").read_text().splitlines(), attempts)

    def test_live_handoff_then_stop_and_resume_keep_the_same_task_and_attempts(self):
        self.install()
        self.profile(mode="hold")
        repo, spec_file, origin = self.submission("pause repo", "1", "2")
        started = self.cli("run", "--repo", repo, "--spec", spec_file, actor=origin)
        task_id = started["task_id"]
        self.running[task_id] = origin
        ready = self.native / "ready.json"
        self.wait_for(lambda: self.cli("status", task_id, actor=origin), lambda _: ready.is_file(),
                      terminal={"cleanup_blocked", "delivery_unknown", "blocked"})
        active = self.cli("status", task_id, actor=origin)
        self.assertEqual(active["execution"]["state"], "active")
        first_run = active["execution"]["run_id"]
        driver = active["execution"]["pid"]
        pids = [driver, *json.loads(ready.read_text()).values()]
        births = {pid: ato_process.identity(pid) for pid in pids}
        self.assertTrue(all(births.values()), births)

        outgoing = self.scoped(task_id, "3", "controller", agent_ref("1"), 1, "observe", "ask", "handoff")
        old_user = self.scoped(task_id, "4", "user", agent_ref("2"), 1, "observe", "read", "handoff")
        new_controller, new_endpoint = agent_ref("5"), agent_ref("6")
        normalized = self.cli("project", "validate", "--repo", repo, "--spec", spec_file)
        repo_key = normalize_repository(repo).key
        self.endpoint(repo_key, new_controller, "controller", 2)
        self.endpoint(repo_key, new_endpoint, "interaction", 2)
        successor = self.scoped(task_id, "5", "controller", new_controller, 2, "observe", "binding", "stop", "resume")
        new_user = self.scoped(task_id, "6", "user", new_endpoint, 2, "observe", "read", "answer")
        request_id = self.ask(task_id, outgoing, "Confirm the pause fixture?")["request_id"]
        self.assertEqual([row["request_id"] for row in self.cli("inbox", actor=old_user)["items"]], [request_id])
        handoff = self.evidence("handoff", task_id=task_id, spec_sha256=normalized["spec_sha256"],
                               binding_revision=1, source="verified-fixture-handoff", **{
                                   "from": agent_ref("1"), "to": new_controller, "interaction": new_endpoint})
        args = ("agent", "handoff", task_id, "--to", new_controller, "--interaction", new_endpoint,
                "--evidence", handoff)
        self.cli(*args, actor=outgoing, error=(3, "FORBIDDEN"))
        self.registry("decision-" + sha256(handoff.read_bytes()).hexdigest(),
                      dict(source_ref="verified-fixture-handoff", operation="handoff", actor_id="credential-" + "4" * 32))
        handed = self.cli(*args, actor=outgoing)
        self.assertEqual((handed["task_id"], handed["revision"], handed["controller_ref"], handed["interaction_ref"]),
                         (task_id, 2, new_controller, new_endpoint))
        self.assertEqual((handed["run_owner"]["run_id"], handed["run_owner"]["pid"]), (first_run, driver))
        self.running[task_id] = successor
        self.assertEqual(self.cli("agent", "binding", task_id, actor=successor)["run_owner"], handed["run_owner"])
        self.assertEqual(self.cli("inbox", actor=old_user)["items"], [])
        self.assertEqual([row["request_id"] for row in self.cli("inbox", actor=new_user)["items"]], [request_id])
        self.cli("answer", request_id, "--answer", "Approved", actor=old_user, error=(3, "NOT_FOUND"))
        self.cli("stop", task_id, actor=origin, error=(3, "NOT_FOUND"))
        self.cli("resume", task_id, actor=successor, error=(4, "BUSY"))

        stopping = self.cli("stop", task_id, actor=successor)
        self.assertIn(stopping["status"], {"stopping", "stopped"})
        stopped = self.wait_for(lambda: self.cli("status", task_id, actor=successor),
                                lambda status: status["status"] == "stopped",
                                terminal={"cleanup_blocked", "delivery_unknown"})
        self.assertTrue(stopped["execution"]["verified"])
        self.assertTrue(stopped["execution"]["stop_requested"])
        self.assertEqual(stopped["stage"]["attempts"], 1)
        self.wait_for(lambda: [ato_process.disposition(pid, birth) for pid, birth in births.items()],
                      lambda states: all(state in {"absent", "reused"} for state in states))
        write(self.native, "mode", "normal")
        successor = self.scoped(task_id, "5", "controller", new_controller, 2, "observe", "binding", "stop", "resume")
        resumed = self.cli("resume", task_id, actor=successor)
        self.assertEqual(resumed["task_id"], task_id)
        finished = self.wait_for(lambda: self.cli("status", task_id, actor=successor),
                                 lambda status: status["status"] == "needs_acceptance",
                                 terminal={"blocked", "cleanup_blocked", "delivery_unknown", "stopped"})
        self.running.pop(task_id)
        self.assertFalse(finished["execution"]["stop_requested"])
        self.assertEqual(finished["stage"]["attempts"], 2)
        self.assertEqual([row["kind"] for row in finished["run_history"]], ["initial", "resume"])
        self.assertEqual(finished["run_history"][0]["run_id"], first_run)
        self.assertNotEqual(finished["execution"]["run_id"], first_run)
        self.assertFalse((repo / "delivery.txt").exists())

    def test_unknown_worker_result_fences_explicit_resume_without_replay(self):
        self.install()
        self.profile(mode="unknown")
        repo, spec_file, origin = self.submission("unknown repo", "1", "2")
        result = self.cli("run", "--repo", repo, "--spec", spec_file, "--foreground", actor=origin,
                          error=(4, "DELIVERY_UNKNOWN"))
        task_id = result["task_id"]
        before = self.cli("status", task_id, actor=origin)
        self.assertEqual((before["status"], before["stage"]["attempts"]), ("delivery_unknown", 1))
        self.assertEqual([row["phase"] for row in before["run_history"]], ["unknown"])
        self.cli("resume", task_id, actor=origin, error=(4, "RUN_UNSETTLED"))
        after = self.cli("status", task_id, actor=origin)
        self.assertEqual(after["run_history"], before["run_history"])
        attempts = self.native / "attempts"
        self.assertTrue(attempts.is_file(), "The synthetic worker did not launch")
        self.assertEqual(len(attempts.read_text().splitlines()), 1)

    def test_journaled_cutover_and_repeated_rollback_preserve_namespaces(self):
        old = snapshot(self.root / "legacy source", legacy=True)
        self.addCleanup(unseal, old)
        self.bin.mkdir(mode=0o700)
        for name, mode in (("ctask", 0o710), ("ctask-notify", 0o711), ("ato-read-handoff", 0o750)):
            legacy_entry(self.bin, name, old, mode)
        saved = release.entrypoints(self.bin)
        home, native = (Path(self.env[name]) for name in ("ATO_HOME", "CODEX_HOME"))
        paths = release.scoped_paths(self.bin, home, native)
        old_state = write(paths["legacy_state"], "retained.txt", "old fixture evidence")
        new_state = write(home / "unified-v1", "retained.txt", "new fixture evidence")
        journal = self.root / "release journal"
        script = self.source / "scripts/release_snapshot.py"
        common = ("--allow-runtime-change", "--bin-dir", self.bin, "--ato-home", home,
                  "--codex-home", native, "--transaction", journal)
        activated = self.json_command(self.python, "-IB", script, "--activate", *common,
                                      "--snapshot", self.source, "--commit", CURRENT)
        self.assertEqual(activated["state"], "activated")
        self.assertEqual({entry.name for entry in self.bin.iterdir()}, {"ato"})
        source = release.snapshot_source(self.source)
        self.assertEqual(self.cli("system", "status")["release"], source["seal"])
        self.assertEqual(self.launcher.read_bytes(), release.launcher_bytes(self.source, self.python, source["seal"]))
        self.store = TaskStore(home, native_home=native, runtime_seal=source["seal"])
        self.profile()
        repo, spec_file, origin = self.submission("rollback repo", "1", "2")
        delivered = self.cli("run", "--repo", repo, "--spec", spec_file, "--foreground", actor=origin)
        self.assertEqual(delivered["status"], "needs_acceptance")
        task = self.store.load_task(delivered["task_id"])
        artifact = Path(task["worktree"]["path"]) / "delivery.txt"
        recorded = release.read(journal / "transaction.json")
        self.assertEqual(recorded["before"]["entries"], saved)
        self.assertEqual(recorded["target"], dict(source, python_sha256=release.sha(self.python)))
        replacement = snapshot(self.root / "next source", executable=True, commit="d" * 40)
        self.addCleanup(unseal, replacement)
        with self.assertRaisesRegex(release.Refused, "unfinished Task requires its selected release"):
            release.activate(replacement, "d" * 40, paths, self.root / "refused switch")
        with self.assertRaisesRegex(release.Refused, "unfinished Task requires its selected release"):
            release.rollback(journal, paths)
        self.assertEqual(self.launcher.read_bytes(), release.launcher_bytes(self.source, self.python, source["seal"]))
        user = self.scoped(delivered["task_id"], "3", "user", agent_ref("2"), 1, "observe", "read", "accept")
        evidence = self.acceptance(delivered, "accept before rolling back")
        self.assertEqual(self.cli("accept", delivered["task_id"], "--evidence", evidence, actor=user)["status"],
                         "completed")
        task = self.store.load_task(delivered["task_id"])
        for _ in range(2):
            result = self.json_command(self.python, "-IB", script, "--rollback", *common)
            self.assertEqual(result["state"], "rolled_back")
            self.assertEqual(release.entrypoints(self.bin), saved)
            self.assertEqual(old_state.read_text(), "old fixture evidence")
            self.assertEqual(new_state.read_text(), "new fixture evidence")
            self.assertEqual(self.store.load_task(delivered["task_id"]), task)
            self.assertEqual(artifact.read_text(), "fixture output\n")


if __name__ == "__main__":
    unittest.main()
