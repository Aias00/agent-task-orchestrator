"""Exercise the installed, sealed ATO CLI with local synthetic executables."""
from contextlib import closing, contextmanager
from functools import partial
from hashlib import sha256
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch
from uuid import uuid4

import agent_registry
import ato_application
import hub_worktree
import ato_observer
import ato_process
import ato_product
from ato_host import ACCEPTANCE_NOTIFICATIONS, Host
from ato_task_store import TaskError, TaskStore, normalize_repository
from message_bus import IdentityError, MessageBus
import notification_state
from scripts import ato_host_setup
from scripts import release_snapshot as release
from message_bus import EndpointCredential
from transport import DeliveryResult

from release_support import CURRENT, fake_env, legacy_entry, seal, snapshot, unseal, write
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

if sys.argv[1:] == ["--version"]:
    print("codex-cli 0.171.0")
    sys.exit(0)
assert sys.argv[1] == "exec"
assert "--sandbox" not in sys.argv
assert not any('ato-host' in value or '--ignore-user-config' == value for value in sys.argv)
assert "ATO_ACTOR_FILE" not in os.environ
assert os.environ.get("OPENAI_API_KEY") == "private-fixture-environment-token"
assert sys.stdin.read()
native = Path(os.environ["CODEX_HOME"])
mode = (native / "mode").read_text()
target = Path("delivery.txt")
if mode == "delete":
    Path("source.txt").unlink()
if mode != "clean":
    target.write_text((target.read_text() if target.exists() else "") + "fixture output\n")
with (native / "attempts").open("a") as log:
    log.write(str(Path.cwd()) + "\n")
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
if mode == "blocked":
    result.update(status="blocked", summary="fixture blocked", blocker="fixture blocker")
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
        self.native = self.store.native_home
        self.reviewer_home = self.root / "reviewer home"
        self.proofs = self.root / "actors"
        for home in (self.worker_home, self.native, self.reviewer_home, self.proofs):
            home.mkdir(mode=0o700, exist_ok=True)
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

    def task_spec(self, name, *, reviewer=False, host_handoff=False, ignore_runtime=True):
        repo = self.root / name
        repo.mkdir()
        git(repo, "init", "-q")
        if ignore_runtime:
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
        return repo, spec_file, normalized

    def submission(self, name, origin, interaction, *, reviewer=False, via_setup=False, host_handoff=False):
        repo, spec_file, normalized = self.task_spec(name, reviewer=reviewer, host_handoff=host_handoff)
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
        self.assertEqual(self.cli("list"), {"tasks": [], "warnings": []})
        self.cli("list", actor=self.root / "invalid-proof.json", error=(3, "AUTH_REQUIRED"))
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

    def test_installed_local_dispatch_and_queries_need_no_submission_proof(self):
        self.install()
        self.profile(via_setup=True)
        repo, spec, _ = self.task_spec("local submission")
        command_only = json.loads(spec.read_text())
        command_only["worker_contract"]["acceptance"] = [command_only["worker_contract"]["acceptance"][0]]
        spec.write_text(json.dumps(command_only))
        self.assertEqual(self.cli("project", "validate", "--repo", repo, "--spec", spec)["repo"], str(repo))
        result = self.cli("run", "--repo", repo, "--spec", spec, "--foreground")
        task = result["task_id"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["acceptance"], dict(disposition="not_recorded", criteria=[], receipt_id=None))
        self.assertEqual(self.cli("status", task)["product"], result["product"])
        self.assertEqual([row["task_id"] for row in self.cli("list", "--repo", repo)["tasks"]], [task])
        self.assertEqual(self.cli("agent", "binding", task)["revision"], 1)
        self.assertEqual(list(self.proofs.iterdir()), [])
        self.assertEqual(list(self.store.root.glob("host/credential-*.json")), [])

        evidence = write(self.root, "local acceptance.txt", "Explicit local fixture acceptance")
        accepted = self.cli("accept", task, "--evidence", evidence)
        self.assertEqual(accepted["status"], "completed")
        self.assertEqual(accepted["acceptance"]["method"], "local_owner")
        self.assertEqual(accepted["checks"], result["checks"])
        receipt = json.loads(self.store._read_blob(task, accepted["acceptance"]["receipt_id"]))
        self.assertEqual(self.store._read_blob(task, receipt["attachment_sha256"]), evidence.read_bytes())
        self.assertEqual(self.cli("accept", task)["acceptance"], accepted["acceptance"])
        self.cli("resume", task, error=(4, "NOT_RESUMABLE"))
        rechecked = self.cli("project", "recheck", task, "--checks-only")
        self.assertEqual(rechecked["status"], "completed")
        self.assertEqual(rechecked["acceptance"], accepted["acceptance"])
        self.assertNotEqual(rechecked["product"]["checks_sha256"], result["product"]["checks_sha256"])
        self.assertEqual(self.cli("status", task)["status"], "completed")
        self.assertEqual(list(self.proofs.iterdir()), [])

    def test_local_acceptance_records_decision_before_checks_and_across_releases(self):
        self.install()
        self.profile(mode="host", via_setup=True)
        repo, spec, _ = self.task_spec("local acceptance across releases", host_handoff=True)
        failed = self.cli("run", "--repo", repo, "--spec", spec, "--foreground", error=(4, "RUN_UNSETTLED"))
        task = failed["task_id"]
        before = self.cli("status", task)
        self.assertEqual(before["status"], "needs_host")
        self.assertIsNone(before["product"])
        self.assertTrue(before["execution"]["verified"])
        self.assertEqual(before["execution"]["state"], "idle")

        replacement = snapshot(self.root / "next acceptance source", executable=True, commit="d" * 40)
        self.addCleanup(unseal, replacement)
        paths = release.scoped_paths(self.bin, self.store.home, self.store.native_home)
        release.activate(replacement, "d" * 40, paths, self.root / "local acceptance cutover")
        self.assertEqual(self.cli("status", task)["status"], "needs_host")

        accepted = self.cli("accept", task)
        self.assertEqual(accepted["status"], "needs_host")
        self.assertIsNone(accepted["product"])
        self.assertEqual(accepted["acceptance"]["method"], "local_owner")
        self.assertEqual(accepted["checks"], before["checks"])
        self.assertEqual(accepted["execution"], before["execution"])
        self.assertEqual(accepted["run_history"], before["run_history"])
        self.assertEqual(self.cli("status", task)["acceptance"], accepted["acceptance"])
        self.assertEqual(self.cli("list")["tasks"][0]["status"], "needs_host")

    def test_local_acceptance_keeps_active_execution_visible(self):
        self.install()
        self.profile(mode="hold")
        repo, spec, _ = self.task_spec("local acceptance during execution")
        task = self.cli("run", "--repo", repo, "--spec", spec)["task_id"]
        self.running[task] = None
        before = self.wait_for(lambda: self.cli("status", task),
                               lambda status: status["execution"]["state"] == "active")
        accepted = self.cli("accept", task)
        self.assertEqual(accepted["acceptance"]["method"], "local_owner")
        self.assertEqual(accepted["execution"]["pid"], before["execution"]["pid"])
        self.assertEqual(accepted["execution"]["state"], "active")
        self.assertEqual(accepted["status"], "running")
        self.cli("stop", task)
        finished = self.wait_for(lambda: self.cli("status", task), lambda status: status["status"] == "stopped")
        self.assertEqual(finished["execution"]["state"], "idle")
        self.assertTrue(finished["execution"]["stop_requested"])
        self.assertEqual(finished["run_history"][-1]["outcome"], "stopped")
        self.assertEqual(finished["acceptance"], accepted["acceptance"])
        self.running.pop(task)

    def acceptance_mailbox(self, *projects):
        self.registry("observation", dict(enabled=True, source_ref="fixture-observation", transport_kinds=[]))
        native = self.store.native_home
        sessions = [str(uuid4()) for _ in projects]
        for session_id, project in zip(sessions, projects, strict=True):
            write(native, session_id + ".jsonl", json.dumps(dict(
                type="session_meta", payload=dict(id=session_id, cwd=str(project)))) + "\n")
        with closing(sqlite3.connect(native / "state_1.sqlite")) as index, index:
            index.execute("create table threads (id text, rollout_path text)")
            index.executemany("insert into threads values (?,?)",
                              [(session_id, str(native / (session_id + ".jsonl"))) for session_id in sessions])
        packages = native / "packages/standalone"
        binary = write(packages, "releases/fixture/bin/codex", "#!/bin/sh\nexit 97\n", 0o700)
        (packages / "current").symlink_to("releases/fixture", target_is_directory=True)
        mailbox = self.store.kernel_root / "ctask-interaction/mailbox/state.sqlite"
        with self.store._private(mailbox.parent, create=True):
            pass
        return sessions, binary, MessageBus(mailbox)

    def test_batch0_completed_stop_and_cross_release_cleanup_keep_terminal_state(self):
        self.install()
        self.profile(mode="clean", via_setup=True)
        repo, spec, _ = self.task_spec("completed stop and cleanup")
        before = self.cli("run", "--repo", repo, "--spec", spec, "--foreground")
        task_id = before["task_id"]
        task_dir = self.store._task_dir(task_id)
        self.assertEqual(before["status"], "completed")
        self.assertEqual(self.cli("stop", task_id), before)
        evidence = write(self.root, "explicit evidence", "original")
        accepted = self.cli("accept", task_id, "--evidence", evidence)
        self.assertEqual(self.cli("stop", task_id), accepted)
        self.assertEqual(self.cli("accept", task_id, "--evidence", evidence)["acceptance"], accepted["acceptance"])
        evidence.write_text("changed")
        self.cli("accept", task_id, "--evidence", evidence, error=(4, "DECISION_CONFLICT"))

        replacement = snapshot(self.root / "next cleanup release", executable=True, commit="d" * 40)
        self.addCleanup(unseal, replacement)
        paths = release.scoped_paths(self.bin, self.store.home, self.store.native_home)
        release.activate(replacement, "d" * 40, paths, self.root / "cleanup cutover")
        cleaned = self.cli("project", "cleanup", task_id, "--confirm")
        self.assertEqual((cleaned["status"], cleaned["worktree_state"]), ("completed", "removed"))
        self.assertEqual(cleaned["product"], before["product"])
        self.assertEqual(self.cli("stop", task_id), cleaned)
        self.assertEqual(self.cli("status", task_id), cleaned)
        self.assertFalse((task_dir / "STOP.json").exists())

    def test_batch2_cleanup_retains_uncommitted_verified_product_after_cutover(self):
        self.install()
        self.profile(via_setup=True)
        repo, spec, _ = self.task_spec("uncommitted retention", ignore_runtime=False)
        result = self.cli("run", "--repo", repo, "--spec", spec, "--foreground")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["acceptance"]["disposition"], "not_recorded")
        task_id = result["task_id"]
        task = self.store.load_task(task_id)
        worktree = Path(task["worktree"]["path"])
        branch = task["worktree"]["branch"]
        self.assertIn("?? delivery.txt", git(worktree, "status", "--porcelain"))

        replacement = snapshot(self.root / "retention release", executable=True, commit="d" * 40)
        self.addCleanup(unseal, replacement)
        paths = release.scoped_paths(self.bin, self.store.home, self.store.native_home)
        release.activate(replacement, "d" * 40, paths, self.root / "retention cutover")
        cleaned = self.cli("project", "cleanup", task_id, "--confirm")
        self.assertEqual((cleaned["status"], cleaned["worktree_state"]), ("completed", "removed"))
        self.assertEqual(cleaned["product"], result["product"])
        diff = self.cli("project", "diff", task_id)
        self.assertEqual(diff["worktree_state"], "removed")
        self.assertEqual(diff["summary"], result["product"]["diff_summary"])
        self.assertIn("+fixture output", diff["patch"])
        self.assertFalse(worktree.exists())
        self.assertEqual(git(repo, "show", "-s", "--format=%an <%ae>", branch), "ATO Host <ato-host@local>")
        self.assertEqual(git(repo, "ls-tree", "-r", "--name-only", branch), "delivery.txt\nsource.txt")
        self.assertEqual(git(repo, "show", branch + ":delivery.txt"), "fixture output")
        self.assertEqual(git(repo, "status", "--porcelain"), "")
        self.assertEqual(self.cli("project", "cleanup", task_id, "--confirm"), cleaned)
        git(repo, "branch", "-D", branch)
        self.cli("project", "diff", task_id, error=(4, "BINDING_CHANGED"))

    def test_fixup_installed_run_can_start_after_optional_node_digest_changes(self):
        node = write(self.root, "node", "#!/bin/sh\nexit 0\n", 0o700)
        unseal(self.source)
        seal(self.source, CURRENT, tools={"node": release.host_tool("node", node)})
        self.install()
        self.profile(via_setup=True)
        node.write_text("#!/bin/sh\nexit 17\n")
        with self.assertRaisesRegex(release.Refused, "recorded node executable changed"):
            release.verify_snapshot(self.source, CURRENT)
        repo, spec, _ = self.task_spec("optional Node digest drift")
        result = self.cli("run", "--repo", repo, "--spec", spec, "--foreground")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["run_history"][-1]["phase"], "settled")

    def test_fixup_retention_stop_race_and_unchanged_git_blobs(self):
        self.install()
        self.profile(via_setup=True)
        repo, spec, _ = self.task_spec("stop during retention")
        git(repo, "config", "user.useConfigOnly", "true")
        status = self.cli("run", "--repo", repo, "--spec", spec, "--foreground")
        task_id = status["task_id"]
        task = self.store.load_task(task_id)
        head = task["worktree"]["base_commit"]
        host = self.host
        remove = hub_worktree.cleanup
        observed = []

        def stop_during_removal(binding, **options):
            self.assertNotEqual(git(binding["path"], "rev-parse", "HEAD"), head)
            self.assertTrue((self.store._task_dir(task_id) / "cleanup.json").is_file())
            observed.append(host.execution.request_stop(host._local_owner, task_id))
            self.assertFalse((self.store._task_dir(task_id) / "STOP.json").exists())
            return remove(binding, **options)

        with patch.object(hub_worktree, "cleanup", side_effect=stop_during_removal), \
                patch.object(ato_product, "_git", wraps=ato_product._git) as git_calls:
            cleaned = ato_application.cleanup(host, host._local_owner, task_id)
        self.assertEqual(len(observed), 1)
        self.assertEqual(cleaned["status"], "completed")
        self.assertEqual(cleaned["product"], status["product"])
        self.assertEqual(self.store.load_task(task_id)["stop_epoch"], 0)
        writes = [call.kwargs["input_data"] for call in git_calls.call_args_list
                  if call.args[1:2] == ("hash-object",)]
        self.assertEqual(writes, [b"fixture output\n"])
        branch = task["worktree"]["branch"]
        self.assertEqual(git(repo, "show", "-s", "--format=%cn <%ce>", branch), "ATO Host <ato-host@local>")

    def test_fixup_local_completion_reconciliation_and_stale_fifo_cleanup(self):
        self.install()
        self.profile(via_setup=True)
        repo, spec, _ = self.task_spec("local completion mailbox recovery")
        (session_id,), _, bus = self.acceptance_mailbox(repo)
        self.env["CODEX_THREAD_ID"] = session_id
        with (self.store.native_home / (session_id + ".jsonl")).open("a") as roll:
            roll.write(json.dumps(dict(type="event_msg", payload=dict(type="task_complete"))) + "\n")
        host = Host(store=self.store).compose(render_notice=ato_observer.render_notice)

        def queue_completion():
            finished = self.cli("run", "--repo", repo, "--spec", spec, "--foreground")
            task_id = finished["task_id"]
            saved = self.store.load_task(task_id)
            correlation = saved["run_heads"][saved["run_id"]][-1]
            self.assertEqual(host.interaction.dispatch_delivery(task_id, correlation, "completion"), "queued")
            _, notice = host.interaction._reservation(saved, correlation, "completion")
            self.assertTrue(notice["local_completion"])
            return task_id, notice

        stale_task, stale = queue_completion()
        self.cli("project", "recheck", stale_task, "--checks-only")
        task_id, notice = queue_completion()
        scope = host.interaction._scope_record(self.store.load_task(task_id))
        receiver = host.delivery_target(scope, notice)
        owner = bus.acquire_endpoint(receiver, owner="fixture")
        self.assertIsNone(bus.claim(receiver, owner, message_id=notice["mailbox_id"]))
        self.assertEqual(ato_observer.tick(host, {}), {})
        self.assertEqual(bus.message(stale["mailbox_id"])["state"], "cancelled")
        self.assertEqual(bus.message(stale["mailbox_id"])["busy"], 0)
        claim = bus.claim(receiver, owner, message_id=notice["mailbox_id"])
        started = bus.begin_dispatch(receiver, owner, claim["id"], claim["claim_token"])
        bus.finish_dispatch(started["id"], started["attempt_id"], state="delivery_unknown")
        bus.release_endpoint(receiver, owner)
        self.assertEqual(self.host_setup("reconcile-delivery", task_id, started["id"]),
                         dict(message_id=started["id"], state="failed", busy=False))
        self.assertEqual(bus.message(started["id"])["attempt_id"], started["attempt_id"])
        self.assertEqual(self.cli("status", task_id)["status"], "completed")

    def test_fixup_mailbox_projection_refreshes_after_busy(self):
        self.install()
        self.profile(via_setup=True)
        repo, spec, _ = self.task_spec("mailbox snapshot retries")
        _, _, bus = self.acceptance_mailbox(repo)
        task_id = self.cli("run", "--repo", repo, "--spec", spec, "--foreground")["task_id"]
        host = Host(store=self.store, read_only=True).compose()
        self.addCleanup(host.close_mailbox_snapshot)
        original = host.interaction._view
        endpoint_id = "retry-visible"
        first = True

        def retry_with_new_endpoint(*args):
            nonlocal first
            if first:
                first = False
                with self.assertRaises(IdentityError):
                    host.mailbox().endpoint(endpoint_id)
                bus.register_endpoint(endpoint_id, kind="fake", session_id="fixture",
                                      project=str(repo), source="fixture")
                raise TaskError("BUSY")
            self.assertEqual(host.mailbox().endpoint(endpoint_id)["state"], "active")
            return original(*args)

        with patch.object(host.interaction, "_view", side_effect=retry_with_new_endpoint) as view:
            self.assertEqual(host.interaction.task_status(host._local_owner, task_id)["status"], "completed")
        self.assertEqual(view.call_count, 2)

    def test_batch0_local_annotation_does_not_hide_product_invalidation(self):
        self.install()
        self.profile(via_setup=True)
        repo, spec, _ = self.task_spec("local product integrity")
        finished = self.cli("run", "--repo", repo, "--spec", spec, "--foreground")
        task_id = finished["task_id"]
        self.assertEqual(self.cli("accept", task_id)["acceptance"]["method"], "local_owner")
        (Path(finished["product"]["worktree"]) / "delivery.txt").write_text("changed")
        status = self.cli("status", task_id)
        self.assertEqual((status["status"], status["reason"], status["acceptance"]["disposition"]),
                         ("blocked", "PRODUCT_CHANGED", "invalidated"))

    def test_batch0_origin_eligibility_is_read_only_and_revocation_survives_observation(self):
        self.install()
        self.profile(via_setup=True)
        repo, spec, _ = self.task_spec("closed completion endpoint")
        (session_id,), _, bus = self.acceptance_mailbox(repo)
        self.env["CODEX_THREAD_ID"] = session_id
        result = self.cli("run", "--repo", repo, "--spec", spec, "--foreground")
        service = self.host.interaction
        task, frozen, binding = service._task(result["task_id"])
        status, generation, _ = service._view(task, frozen)
        _, item = service.completion_item(task, status, generation)
        self.assertTrue(service._notification_eligible(task, binding, "completion", item))
        self.assertEqual(list(self.host.folder.glob("acceptance-origin-*.json")), [])
        with bus._transaction() as connection:
            self.assertEqual(connection.execute("select count(*) from endpoints").fetchone()[0], 0)
        route = self.host.local_completion_route(service._scope_record(task), binding, item)
        bus.set_endpoint_state(route.target, "closed", source="fixture operator")
        with patch.object(self.host, "register_acceptance_route") as register:
            self.assertEqual(ato_observer.tick(self.host), {})
            register.assert_not_called()
        self.assertEqual(bus.endpoint(route.target.endpoint_id)["state"], "closed")
        self.assertEqual(self.cli("status", result["task_id"])["delivery"], [])

    def test_batch0_recheck_keeps_observing_the_original_completion(self):
        self.install()
        self.profile(via_setup=True)
        repo, spec, _ = self.task_spec("completion observation across Runs")
        (session_id,), _, bus = self.acceptance_mailbox(repo)
        self.env["CODEX_THREAD_ID"] = session_id
        result = self.cli("run", "--repo", repo, "--spec", spec, "--foreground")
        attempts = []
        events_available = False

        class Adapter:
            capabilities = {"native_queue_serialization"}

            def session_cursor(self, native_id, project):
                return {"offset": 0}

            def send(self, endpoint, message):
                attempts.append(message)
                return DeliveryResult("delivered", str(uuid4()))

            def observe_session_v2(self, identity, correlation, generation, cursor):
                if not events_available:
                    return [], cursor
                events = [agent_registry.SessionEvent("fixture-" + kind, identity, correlation,
                    generation, kind, "offline fixture", time.time(),
                    turn_id="fixture-turn", message_id=correlation)
                    for kind in ("message_received", "turn_completed")]
                return events, {"offset": 1}

        self.host = Host(store=self.store).compose(render_notice=ato_observer.render_notice)
        self.host.interaction.dispatch_envelope = partial(ato_observer._dispatch, self.host)
        self.host.interaction.observe_envelope = partial(ato_observer._dispatch, self.host, observe_only=True)
        with patch.object(ato_observer, "CodexSessionAdapter", return_value=Adapter()):
            self.assertEqual(ato_observer.tick(self.host), {})
            message, = attempts
            self.assertEqual(bus.message(message["id"])["state"], "delivered")
            rechecked = self.cli("project", "recheck", result["task_id"], "--checks-only")
            self.assertNotEqual(rechecked["execution"]["run_id"], result["execution"]["run_id"])
            events_available = True
            cache = {}
            with patch.object(ato_product, "measure", side_effect=AssertionError("observation must not remeasure")):
                self.assertEqual(ato_observer.tick(self.host, cache), {})
                self.assertEqual(ato_observer.tick(self.host, cache), {})
        self.assertEqual(bus.message(message["id"])["state"], "turn_completed")
        cursor = self.host.folder / "observer-ledger" / ("message-" + sha256(message["id"].encode()).hexdigest() + ".json")
        self.assertFalse(cursor.exists())
        self.assertEqual(len(attempts), 1)

    def test_configured_local_completion_notifies_once_without_an_acceptance_record(self):
        self.install()
        self.profile(via_setup=True)
        repo, spec, _ = self.task_spec("local acceptance notifications")
        historical = self.cli("run", "--repo", repo, "--spec", spec, "--foreground")
        self.assertEqual(historical["status"], "completed")
        (session_id,), binary, bus = self.acceptance_mailbox(self.root)

        args = ("bind-acceptance", "--repo", repo, "--session-id", session_id, "--session-project", self.root)
        wrong = self.command(self.python, "-IB", self.source / "scripts/ato_host_setup.py",
                             *args[:-1], repo, "--source", "fixture wrong project")
        self.assertEqual((wrong.returncode, json.loads(wrong.stdout)), (3, {"error": "FORBIDDEN"}))
        self.assertIsNone(self.host.acceptance_notifications())

        original_transaction = MessageBus._transaction

        @contextmanager
        def rollback_after_record(instance):
            with original_transaction(instance) as connection:
                yield connection
                raise RuntimeError("fixture interrupted endpoint publication")

        with patch.object(MessageBus, "_transaction", rollback_after_record):
            with self.assertRaisesRegex(RuntimeError, "interrupted endpoint publication"):
                ato_host_setup.bind_acceptance(self.host, SimpleNamespace(
                    repo=str(repo), session_id=session_id, session_project=str(self.root), source="fixture retry"))
        interrupted = self.host.acceptance_notifications()
        with self.assertRaises(IdentityError):
            bus.endpoint(interrupted["target"]["endpoint_id"])
        self.assertEqual(self.host_setup(*args)["notify"], "completion")
        bound = self.host.acceptance_notifications()
        self.assertTrue(self.host_setup(*args)["configured"])
        self.assertEqual(self.host.acceptance_notifications(), bound)

        current = self.cli("run", "--repo", repo, "--spec", spec, "--foreground")
        other_repo, other_spec, _ = self.task_spec("local acceptance without route")
        unrelated = self.cli("run", "--repo", other_repo, "--spec", other_spec, "--foreground")

        class Adapter:
            capabilities = {"native_queue_serialization"}

            def send(self, endpoint, message):
                attempts.append((endpoint["session_id"], message))
                return DeliveryResult("delivery_unknown")

        attempts = []
        with patch.object(ato_observer, "CodexSessionAdapter", return_value=Adapter()) as adapter:
            self.host = Host(store=self.store).compose(render_notice=ato_observer.render_notice)
            self.host.interaction.dispatch_envelope = partial(ato_observer._dispatch, self.host)
            self.host.interaction.observe_envelope = partial(ato_observer._dispatch, self.host)
            with patch.object(notification_state, "project_mutes", return_value=[{"repo": str(repo)}]):
                self.assertEqual(ato_observer.tick(self.host), {})
                self.assertEqual(attempts, [])
            for _ in range(2):
                self.assertEqual(ato_observer.tick(self.host), {})
            self.assertTrue(adapter.call_args_list)
            for call in adapter.call_args_list:
                self.assertEqual(call.kwargs, {"queue_binary": str(binary)})

        self.assertEqual(len(attempts), 1)
        recipient, message = attempts[0]
        self.assertEqual(recipient, session_id)
        self.assertEqual(message["task_id"], current["task_id"])
        self.assertEqual(message["kind"], "ato_completion")
        self.assertIn("已完成", message["body"])
        for path_or_token in (str(self.root), bound["sender"]["token"], bound["target"]["token"]):
            self.assertNotIn(path_or_token, message["body"])
        self.assertEqual(bus.message(message["id"])["state"], "delivery_unknown")
        observed = self.cli("status", current["task_id"])
        self.assertEqual(observed["status"], "completed")
        self.assertEqual(observed["acceptance"]["disposition"], "not_recorded")
        self.assertEqual(observed["delivery"], [
            dict(request_id=None, phase="completion", state="delivery_unknown")])
        for task in (historical, unrelated):
            self.assertEqual(self.cli("status", task["task_id"])["delivery"], [])
        self.assertTrue((self.store.root / "host" / (ACCEPTANCE_NOTIFICATIONS + ".json")).is_file())
        self.assertIsNone(observed["acceptance"]["receipt_id"])

    def test_local_tasks_from_two_sessions_route_to_their_own_verified_origin(self):
        self.install()
        self.profile(via_setup=True)
        repo, spec, _ = self.task_spec("shared dispatch repository")
        sessions, binary, bus = self.acceptance_mailbox(self.root, repo)
        self.host_setup("bind-acceptance", "--repo", repo, "--session-id", sessions[0],
                        "--session-project", self.root)
        legacy = self.host.acceptance_notifications()
        unverified = str(uuid4())
        task_ids = []
        for locator, project in zip([*sessions, unverified], [self.root, repo, None], strict=True):
            self.env["CODEX_THREAD_ID"] = locator
            result = self.cli("run", "--repo", repo, "--spec", spec, "--foreground")
            self.assertEqual(result["status"], "completed")
            self.assertNotIn(locator, json.dumps(result))
            task_id = result["task_id"]
            task_ids.append(task_id)
            origin = self.store.tasks / task_id / "origin-session.json"
            self.assertEqual(origin.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(origin.read_text()), dict(version=1, task_id=task_id,
                native_home=str(self.store.native_home), session_id=locator if project else None,
                project=str(project) if project else None))
            self.assertEqual(list(self.host.folder.glob("acceptance-origin-*.json")), [])

        attempts = {}

        class Adapter:
            capabilities = {"native_queue_serialization"}

            def send(self, endpoint, message):
                attempts.setdefault(message["task_id"], []).append((endpoint["session_id"], message))
                return DeliveryResult("delivery_unknown")

        self.host = Host(store=self.store).compose(render_notice=ato_observer.render_notice)
        self.host.interaction.dispatch_envelope = partial(ato_observer._dispatch, self.host)
        self.host.interaction.observe_envelope = partial(ato_observer._dispatch, self.host)
        with patch.object(ato_observer, "CodexSessionAdapter", return_value=Adapter()) as adapter:
            with patch.object(notification_state, "project_mutes", return_value=[{"repo": str(repo)}]):
                self.assertEqual(ato_observer.tick(self.host), {})
                self.assertEqual(attempts, {})
                self.assertEqual(list(self.host.folder.glob("acceptance-origin-*.json")), [])
            for _ in range(2):
                self.assertEqual(ato_observer.tick(self.host), {})
            self.assertTrue(adapter.call_args_list)
            self.assertTrue(all(call.kwargs == {"queue_binary": str(binary)} for call in adapter.call_args_list))

        self.assertEqual(set(attempts), set(task_ids[:2]))
        for task_id, expected_session in zip(task_ids[:2], sessions, strict=True):
            self.assertEqual(len(attempts[task_id]), 1)
            recipient, message = attempts[task_id][0]
            self.assertEqual(recipient, expected_session)
            self.assertIn(task_id, message["body"])
            self.assertNotIn(expected_session, message["body"])
            self.assertEqual(bus.message(message["id"])["state"], "delivery_unknown")
            result = self.cli("status", task_id)
            self.assertEqual(result["acceptance"]["disposition"], "not_recorded")
            self.assertEqual([row["state"] for row in result["delivery"]], ["delivery_unknown"])
        self.assertEqual(attempts[task_ids[0]][0][1]["target"], legacy["target"]["endpoint_id"])
        self.assertEqual(self.host.acceptance_notifications(), legacy)
        self.assertEqual(self.cli("status", task_ids[2])["delivery"], [])

    def test_installed_host_enrollment_and_scoped_question_without_fixture_grants(self):
        self.install()
        self.profile(via_setup=True)
        repo, spec, bootstrap = self.submission("enrollment fixture", "1", "2", via_setup=True)
        work = self.cli("run", "--repo", repo, "--spec", spec, "--foreground", actor=bootstrap)
        task = work["task_id"]
        self.assertEqual(self.cli("status", task)["task_id"], task)
        controller = self.host_setup("scope", task, "--role", "controller")["proof"]
        user = self.host_setup("scope", task, "--role", "user")["proof"]
        ask = ("agent", "ask", task, "--kind", "decision", "--question", "Review this candidate?",
               "--reason", "Fixture decision")
        self.cli(*ask, actor=user, error=(3, "FORBIDDEN"))
        request = self.cli(*ask, actor=controller)["request_id"]
        self.assertEqual([r["request_id"] for r in self.cli("inbox", actor=user)["items"]], [request])
        self.cli("answer", request, "--answer", "Ready to review", actor=user)
        self.assertEqual(self.cli("inbox", actor=user)["items"], [])
        evidence = self.acceptance(work, "enrollment acceptance")
        self.cli("accept", task, "--evidence", evidence, error=(3, "FORBIDDEN"))
        self.cli("accept", task, actor=user, error=(2, "USAGE"))
        self.assertEqual(self.cli("accept", task, "--evidence", evidence, actor=user)["status"], "completed")

    def test_observer_diagnostics_report_missing_and_configured_queue_executable(self):
        self.install()
        self.registry("observation", dict(enabled=True, transport_kinds=["codex"]))
        for command in (("system", "status"), ("agent", "doctor")):
            self.assertEqual(self.cli(*command)["observer"]["codex_queue"], "executable_unavailable")
        self.profile()
        for command in (("system", "status"), ("agent", "doctor")):
            self.assertEqual(self.cli(*command)["observer"]["codex_queue"], "ready")

    def test_batch4_diagnostics_probe_versions_and_validate_notification_files(self):
        self.install()
        self.assertEqual(self.cli("system", "status")["time_policy"], "advisory")
        policy = self.cli("system", "notification-policy")
        self.assertFalse(policy["configured"])
        self.assertEqual(policy["disposition"], "recorded_only")
        self.profile(via_setup=True)
        diagnostics = self.cli("agent", "doctor", "--probe-versions")
        self.assertEqual(diagnostics["worker"]["version"], "0.171.0")
        reviewer = diagnostics["reviewers"]["codex"]
        self.assertTrue(reviewer["version_match"])
        self.assertEqual(reviewer["capabilities"]["read_only_cli"]["state"], "supported")
        self.assertEqual(diagnostics["qualification"], "unverified")
        state = self.store.kernel_root / "ctask-interaction"
        write(state, "fixture")
        (state / "notification-policy.json").symlink_to(self.root / "missing-policy")
        self.cli("system", "notification-policy", error=(5, "STATE_CORRUPT"))

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
        self.cli("answer", request, "--answer", "The fixture action has not finished", actor=user)
        self.cli(*recheck, actor=controller, error=(4, "STALE_REQUEST"))
        request_again = self.cli("agent", "ask", task_id, "--kind", "host_handoff", "--question",
                           "Has the approved host action finished?", "--reason", "Frozen check is blocked",
                           actor=controller)["request_id"]
        self.assertNotEqual(request_again, request)
        request = request_again
        path = Path(self.store.load_task(task_id)["worktree"]["path"])
        write(path, "host-finished.txt", "The fixture operator completed the local action.")
        answer = "The fixture action finished"
        self.host_setup("attest", request, "--answer", answer, "--performed", actor=user)
        self.cli("answer", request, "--answer", answer, actor=user)
        delivered = self.cli(*recheck, actor=controller)
        self.assertEqual(delivered["status"], "completed")
        self.assertEqual([r["kind"] for r in delivered["run_history"]], ["initial", "recheck"])
        self.assertEqual((self.native / "attempts").read_text().splitlines(), [str(path)])
        user = self.host_setup("scope", task_id, "--role", "user")["proof"]
        evidence = self.acceptance(delivered, "host action acceptance")
        self.assertEqual(self.cli("accept", task_id, "--evidence", evidence, actor=user)["status"], "completed")

    def test_batch3_user_approves_reviewer_then_migrates_from_revoked_controller(self):
        self.install()
        self.profile(via_setup=True)
        repo, spec, bootstrap = self.submission("binding fixture", "1", "2", reviewer=True, via_setup=True)
        status = self.cli("run", "--repo", repo, "--spec", spec, "--foreground", actor=bootstrap)
        task = status["task_id"]
        user = self.host_setup("scope", task, "--role", "user")["proof"]
        before = self.cli("agent", "binding", task, actor=user)
        for role in ("controller", "interaction"):
            self.host_setup("enroll-endpoint", "--repo", repo, "--role", role, "--revision", 2,
                            "--reference", before[role + "_ref"])
        review = self.host_setup("approve-binding", task, "reviewer", "--kind", "codex",
                                 "--model", "fixture-model", actor=user)
        changed = self.cli("agent", "reviewer", task, "--kind", "codex", "--model", "fixture-model",
                           "--evidence", review["decision"], actor=user)
        self.assertEqual(changed["review_revision"], 2)
        user = self.host_setup("scope", task, "--role", "user")["proof"]
        controller = self.host_setup("scope", task, "--role", "controller")["proof"]
        new = self.host_setup("enroll-endpoint", "--repo", repo, "--role", "controller", "--revision", 3)["reference"]
        self.host_setup("enroll-endpoint", "--repo", repo, "--role", "interaction", "--revision", 3,
                        "--reference", before["interaction_ref"])
        self.host_setup("revoke", before["controller_ref"])
        self.cli("status", task, actor=controller, error=(3, "NOT_FOUND"))
        self.cli("resume", task, actor=controller, error=(3, "NOT_FOUND"))
        transfer = self.host_setup("approve-binding", task, "handoff", "--to", new, actor=user)
        changed = self.cli("agent", "handoff", task, "--to", new, "--evidence", transfer["decision"], actor=user)
        self.assertEqual((changed["controller_ref"], changed["revision"]), (new, 3))
        new_user = self.host_setup("scope", task, "--role", "user")["proof"]
        self.assertEqual(self.cli("agent", "binding", task, actor=new_user)["revision"], 3)
        corrupted = self.store._task_dir(task) / "task.json"
        corrupted.write_text("{")
        self.cli("status", task, actor=new_user, error=(5, "STATE_CORRUPT"))

    def test_batch3_host_reconciles_exact_mailbox_after_verifying_local_idle(self):
        self.install()
        self.profile(via_setup=True)
        repo, spec, bootstrap = self.submission("mailbox fixture", "1", "2", via_setup=True)
        status = self.cli("run", "--repo", repo, "--spec", spec, "--foreground", actor=bootstrap)
        task_id = status["task_id"]
        controller = self.host_setup("scope", task_id, "--role", "controller")["proof"]
        user = self.host_setup("scope", task_id, "--role", "user")["proof"]
        binding = self.cli("agent", "binding", task_id, actor=controller)
        self.store.native_home.mkdir(mode=0o700, exist_ok=True)
        with sqlite3.connect(self.store.native_home / "state_1.sqlite") as db:
            db.execute("create table threads (id text, rollout_path text)")
            for role in ("controller", "interaction"):
                session = str(uuid.uuid4())
                rollout = self.store.native_home / (role + ".jsonl")
                rollout.write_text(json.dumps({"type": "session_meta", "payload": {"id": session, "cwd": str(repo)}}) + "\n"
                    + json.dumps({"type": "event_msg", "payload": {"type": "task_complete"}}) + "\n")
                db.execute("insert into threads values (?, ?)", (session, str(rollout)))
                db.commit()
                self.host_setup("enroll-endpoint", "--repo", repo, "--role", role, "--revision", 1,
                    "--reference", binding[role + "_ref"], "--native-kind", "codex", "--native-session", session)
        self.registry("observation", dict(enabled=True, source_ref="fixture-mailbox"))
        request = self.cli("agent", "ask", task_id, "--kind", "decision", "--question", "Continue?",
                           "--reason", "Offline fixture", actor=controller)["request_id"]
        self.cli("answer", request, "--answer", "Yes", actor=user)
        self.assertEqual(self.host.interaction.dispatch_delivery(task_id, request, "answer"), "queued")
        service = self.host.interaction
        reference = service.delivery_reference(task_id, request, "answer")["mailbox_id"]
        receiver = EndpointCredential(**self.host.record(binding["controller_ref"])["mailbox"])
        bus = self.host.mailbox()
        owner = bus.acquire_endpoint(receiver, owner="test-owner")
        claimed = bus.claim(receiver, owner, message_id=reference)
        started = bus.begin_dispatch(receiver, owner, reference, claimed["claim_token"])
        bus.finish_dispatch(reference, started["attempt_id"], state="delivery_unknown")
        bus.release_endpoint(receiver, owner)
        self.assertEqual(bus.message(reference)["busy"], 1)
        result = self.host_setup("reconcile-delivery", task_id, reference)
        self.assertEqual(result, dict(message_id=reference, state="failed", busy=False))
        self.assertEqual(bus.message(reference)["attempt_id"], started["attempt_id"])
        self.assertEqual(sum(row["kind"] == "native_reconciled" for row in bus.events(message_id=reference)), 1)
        trace = self.cli("project", "trace", task_id, actor=bootstrap)
        self.assertEqual(trace["delivery"], [dict(correlation_id=request, phase="answer", state="failed")])

    def test_batch3_scope_proofs_validate_the_selected_root_at_both_depths(self):
        self.env["ATO_HOME"] = str(self.root / "private-state" / "nested" / "ato")
        self.install()
        self.profile(via_setup=True)
        deeper = self.root / "outside" / "nested" / "tree" / "of" / "proofs"
        deeper.mkdir(parents=True, mode=0o700)
        deeper.parent.chmod(0o755)
        profile = self.host.record("profile")
        profile["proof_roots"].append(str(deeper))
        self.host_setup("configure", "--profile", write(self.root, "updated-profile.json", json.dumps(profile)), "--update")
        repo, spec, bootstrap = self.submission("proof fixture", "1", "2", via_setup=True)
        status = self.cli("run", "--repo", repo, "--spec", spec, "--foreground", actor=bootstrap)
        for directory in (self.proofs, deeper):
            proof = Path(self.host_setup("scope", status["task_id"], "--role", "user",
                                         "--proof-root", directory)["proof"])
            self.assertEqual(proof.parent, directory)
            self.assertEqual(proof.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.cli("status", status["task_id"], actor=proof)["status"], "completed")

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
        self.assertEqual(status["status"], "completed")
        self.assertIsNone(status["review"])
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
        self.assertEqual(pending["status"], "completed")
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
        self.assertEqual(self.cli("inbox", actor=user)["items"], [])
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
        self.cli("project", "recheck", task_id, actor=origin, error=(3, "FORBIDDEN"))
        recheck_actor = self.scoped(task_id, "8", "controller", agent_ref("1"), 1, "observe", "recheck")
        rechecked = self.cli("project", "recheck", task_id, actor=recheck_actor)
        self.assertEqual((rechecked["task_id"], rechecked["status"]), (task_id, "completed"))
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
                                 lambda status: status["status"] == "completed",
                                 terminal={"blocked", "cleanup_blocked", "delivery_unknown", "stopped"})
        self.running.pop(task_id)
        self.assertFalse(finished["execution"]["stop_requested"])
        self.assertEqual(finished["stage"]["attempts"], 2)
        self.assertEqual([row["kind"] for row in finished["run_history"]], ["initial", "resume"])
        self.assertEqual(finished["run_history"][0]["run_id"], first_run)
        self.assertNotEqual(finished["execution"]["run_id"], first_run)
        self.assertFalse((repo / "delivery.txt").exists())

    def test_linked_worktrees_in_the_same_repository_can_execute_independently(self):
        self.install()
        self.profile(mode="hold")
        repo, spec_file, _ = self.task_spec("parallel worktrees")
        run_args = ("run", "--repo", repo, "--spec", spec_file)
        first = self.cli(*run_args)["task_id"]
        self.running[first] = None
        self.wait_for(lambda: self.cli("status", first),
                      lambda _: (self.native / "ready.json").is_file())
        write(self.native, "mode", "normal")
        second = self.cli(*run_args, "--foreground")
        self.assertEqual(second["status"], "completed")
        self.assertEqual(self.cli("status", first)["execution"]["state"], "active")
        self.assertNotEqual(second["task_id"], first)
        self.assertEqual((self.native / "attempts").read_text().splitlines(), [
            self.store.load_task(task_id)["worktree"]["path"] for task_id in (first, second["task_id"])])
        self.cli("stop", first)
        stopped = self.wait_for(lambda: self.cli("status", first), lambda status: status["status"] == "stopped")
        self.assertTrue(stopped["execution"]["verified"])
        self.running.pop(first)

    def test_project_diff_readonly_visibility_and_parsing(self):
        self.install()
        missing = "task-" + "0"*32
        self.cli("project", "diff", missing, error=(3, "NOT_FOUND"))
        self.cli("project", "diff", "bad-id", error=(2, "INVALID_ID"))
        self.cli("project", "diff", missing, "--stat", "--stat", error=(2, "USAGE"))
        self.assertFalse(self.store.root.exists())
        self.profile(via_setup=True)
        repo, spec, bootstrap = self.submission("diff scoped fixture", "1", "2", via_setup=True)
        work = self.cli("run", "--repo", repo, "--spec", spec, "--foreground", actor=bootstrap)
        task = work["task_id"]
        proof = self.host_setup("scope", task, "--role", "user")["proof"]
        stat = self.cli("project", "diff", task, "--stat", actor=proof)
        self.assertEqual(stat["summary"], work["product"]["diff_summary"])
        self.assertEqual((stat["patch"], stat["patch_truncated"]), (None, False))
        self.assertIn("+fixture output", self.cli("project", "diff", task)["patch"])
        self.cli("project", "diff", missing, actor=proof, error=(3, "NOT_FOUND"))
        other_repo, other_spec, _ = self.task_spec("other diff fixture")
        other = self.cli("run", "--repo", other_repo, "--spec", other_spec, "--foreground")
        self.cli("project", "diff", other["task_id"], actor=proof, error=(3, "NOT_FOUND"))

    def test_declared_guardrails_block_advisory_settlement_and_recheck(self):
        self.install()
        self.profile(mode="delete")
        repo, path, _ = self.task_spec("opt-in deletion")
        value = json.loads(path.read_text())
        value["worker_contract"]["acceptance"][0]["checks"] = [["true"]]
        for rules, expected in ((None, None), ({"forbid_delete_paths": ["delivery.txt"]}, None),
                                ({"forbid_delete_paths": ["source.txt"]}, "deleted"),
                                ({"protected_paths": ["delivery.txt"]}, "added")):
            with self.subTest(rules=rules):
                path.write_text(json.dumps(dict(value, guardrails=rules)))
                args = ("run", "--repo", repo, "--spec", path, "--foreground")
                if expected is None:
                    result = self.cli(*args)
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(result["product"]["diff_summary"]["files_deleted"], ["source.txt"])
                    continue
                task = self.cli(*args, error=(4, "GUARDRAIL_VIOLATION"))["task_id"]
                status = self.cli("status", task)
                self.assertEqual(status["run_history"][-1]["phase"], "settled")
                self.assertEqual((status["status"], status["reason"], status["next_step"]),
                                 ("blocked", "GUARDRAIL_VIOLATION", "REVIEW_GUARDRAIL_VIOLATION"))
                self.assertEqual(status["guardrail_violations"][0]["change"], expected)
                self.assertEqual(status["checks"][0]["state"], "passed")
                self.cli("project", "recheck", task, error=(4, "GUARDRAIL_VIOLATION"))

    def test_existing_file_edit_passes_delete_only_rule_and_blocks_protected_rule(self):
        self.install()
        self.profile()
        repo, path, _ = self.task_spec("opt-in modifications")
        write(repo, "delivery.txt", "existing content\n")
        git(repo, "add", "delivery.txt")
        git(repo, "commit", "-qm", "track delivery")
        value = json.loads(path.read_text())
        run = ("run", "--repo", repo, "--spec", path, "--foreground")
        for rule in ("forbid_delete_paths", "protected_paths"):
            path.write_text(json.dumps(dict(value, guardrails={rule: ["delivery.txt"]})))
            if rule == "protected_paths":
                result = self.cli(*run, error=(4, "GUARDRAIL_VIOLATION"))
                violations = self.cli("status", result["task_id"])["guardrail_violations"]
                self.assertEqual(violations, [dict(rule=rule, path="delivery.txt", change="modified")])
            else:
                self.assertEqual(self.cli(*run)["product"]["diff_summary"]["files_modified"], ["delivery.txt"])

    def test_failed_independent_check_is_retained_after_worker_completion(self):
        self.install()
        self.profile()
        repo, spec_file, _ = self.task_spec("advisory checks", reviewer=True, host_handoff=True)
        delivered = self.cli("run", "--repo", repo, "--spec", spec_file, "--foreground")
        self.assertEqual(len((self.native / "attempts").read_text().splitlines()), 1)
        self.assertEqual((delivered["status"], delivered["reason"]), ("completed", "CHECKS_FAILED"))
        self.assertEqual([row["state"] for row in delivered["checks"]], ["failed"])
        self.assertTrue(delivered["checks"][0]["receipt_id"])
        self.assertIsNone(delivered["review"])
        self.assertEqual(self.cli("status", delivered["task_id"])["status"], "completed")

    def test_resume_links_a_new_release_after_a_settled_worker_block(self):
        self.install()
        self.profile(mode="blocked")
        repo, spec_file, _ = self.task_spec("cross-release resume")
        result = self.cli("run", "--repo", repo, "--spec", spec_file, "--foreground", error=(4, "RUN_UNSETTLED"))
        task_id = result["task_id"]
        before = self.cli("status", task_id)
        self.assertEqual(before["status"], "blocked")
        old_run = before["execution"]["run_id"]
        replacement = snapshot(self.root / "next source", executable=True, commit="d" * 40)
        self.addCleanup(unseal, replacement)
        release.activate(replacement, "d" * 40, release.scoped_paths(self.bin, self.store.home, self.store.native_home),
                         self.root / "resume cutover")
        write(self.native, "mode", "normal")
        self.cli("resume", task_id)
        self.running[task_id] = None
        status = self.wait_for(lambda: self.cli("status", task_id), lambda value: value["status"] == "completed")
        self.running.pop(task_id)
        self.assertEqual([row["kind"] for row in status["run_history"]], ["initial", "resume"])
        task = self.store.load_task(task_id)
        previous, _ = self.store._run(task, old_run)
        current, _ = self.store._run(task, task["run_id"])
        self.assertNotEqual(previous["runtime_seal"], current["runtime_seal"])

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
        self.cli("resume", task_id, actor=origin, error=(3, "FORBIDDEN"))
        scoped = self.scoped(task_id, "8", "controller", agent_ref("1"), 1, "observe", "resume")
        self.cli("resume", task_id, actor=scoped, error=(4, "RUN_UNSETTLED"))
        after = self.cli("status", task_id, actor=origin)
        self.assertEqual(after["run_history"], before["run_history"])
        attempts = self.native / "attempts"
        self.assertTrue(attempts.is_file(), "The synthetic worker did not launch")
        self.assertEqual(len(attempts.read_text().splitlines()), 1)

    def test_journaled_cutover_and_repeated_rollback_preserve_namespaces(self):
        old = snapshot(self.root / "legacy source", legacy=True)
        self.addCleanup(unseal, old)
        self.source = snapshot(self.root / "legacy outcome source", executable=True, legacy_delivery_outcome=True)
        self.addCleanup(unseal, self.source)
        self.python = self.source / ".venv/bin/python"
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
        self.assertEqual(delivered["status"], "completed")
        self.assertEqual(delivered["run_history"][-1]["outcome"], "needs_acceptance")
        task = self.store.load_task(delivered["task_id"])
        artifact = Path(task["worktree"]["path"]) / "delivery.txt"
        recorded = release.read(journal / "transaction.json")
        self.assertEqual(recorded["before"]["entries"], saved)
        self.assertEqual(recorded["target"], dict(source, python_sha256=release.sha(self.python)))
        replacement = snapshot(self.root / "next source", executable=True, commit="d" * 40)
        self.addCleanup(unseal, replacement)
        newer_journal = self.root / "retained idle task switch"
        switched = release.activate(replacement, "d" * 40, paths, newer_journal)
        self.assertEqual(switched["state"], "activated")
        retained = self.cli("status", delivered["task_id"], actor=origin)
        self.assertEqual(retained["status"], "completed")
        self.assertIsNone(retained["next_step"])
        self.assertIsNone(retained["acceptance"]["receipt_id"])
        self.assertEqual(self.store.load_task(delivered["task_id"]), task)
        self.assertEqual(artifact.read_text(), "fixture output\n")
        self.assertEqual(release.rollback(newer_journal, paths)["state"], "rolled_back")
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
