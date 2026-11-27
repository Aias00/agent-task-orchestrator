"""T6 public CLI and release acceptance using only local isolated fixtures."""
from dataclasses import asdict
from contextlib import redirect_stderr, redirect_stdout
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import agent_registry
from ato_common import ato_config
import ato_cli
from ato_task.ato_application import Application, project_diff
from ato_task.ato_execution import HostProfile
from ato_task.ato_host import Host
from ato_common.ato_config import private_runtime_command
from ato_task.ato_private import read_handoff, render_notice
from ato_task.ato_task_store import TaskError, TaskStore
from ato_task.ato_observer import tick
from ato_workflow.message_bus import MessageBus
from scripts import release_snapshot as release

from release_support import CURRENT, ROOT, FakeSystem, fake_env, legacy_entry, snapshot, unseal, write
from test_ato_unified_execution import _WORKER, git


CONTROLLER = "agentref-" + "1"*32
INTERACTION = "agentref-" + "2"*32


class PublicCLI(unittest.TestCase):
    def test_batch0_foreground_interrupt_returns_completed_after_worker_finishes(self):
        complete = {"task_id": "task-" + "a" * 32, "status": "completed"}
        execution = Mock()
        execution.task_status.return_value = complete
        interaction = Mock()
        interaction.task_status.return_value = complete
        app = Application(SimpleNamespace(execution=execution, interaction=interaction))
        proof = object()
        with patch("ato_task.ato_application.time.sleep") as sleep:
            self.assertEqual(app.wait(proof, complete["task_id"], Mock(side_effect=KeyboardInterrupt)),
                             complete)
            sleep.assert_not_called()
        execution.request_stop.assert_called_once_with(proof, complete["task_id"])

    def setUp(self):
        self.fixture = Path(self.enterContext(TemporaryDirectory(prefix="ato-cli-"))).resolve()
        self.env = fake_env(self.fixture)
        self.store = TaskStore(self.fixture/"ato", native_home=self.fixture/"native", runtime_seal="5"*64)
        self.repo = self.fixture/"repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / ".gitignore").write_text(".codex/tasks/\n")
        (self.repo / "tracked.txt").write_text("base\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "fixture")
        self.spec_file = write(self.fixture, "spec.json", json.dumps(dict(schema_version=1, repo=str(self.repo),
            base_ref="HEAD", worker_contract=dict(objective="local CLI fixture", scope="temporary checkout",
            authorization="fixture only", acceptance=[
                dict(id="host", kind="command", description="independent result", checks=[["true"]]),
                dict(id="user", kind="manual", description="accept exact product")]))))

    def invoke(self, *words, actor=None, app=None):
        env = dict(self.env)
        if actor:
            env["ATO_ACTOR_FILE"] = str(actor)
        with patch.dict(os.environ, env, clear=True), redirect_stdout(io.StringIO()) as out,                 redirect_stderr(io.StringIO()) as err:
            code = ato_cli.main(list(map(str, words)), application=app)
        self.assertEqual(err.getvalue(), "")
        return code, json.loads(out.getvalue())

    def registry(self, name, value):
        path = write(self.store.root/"host", name + ".json", json.dumps(value))
        for directory in (self.store.home, self.store.root, path.parent):
            directory.chmod(0o700)
        return path

    def enroll(self, digit, role, reference, **claims):
        credential = "credential-" + digit*32
        token = "offline proof " + digit
        session = agent_registry.SessionIdentity("local_cli", "fixture-" + digit, str(self.repo), "offline fixture")
        self.registry(credential, dict(enabled=True, reference=reference, role=role,
            identity=session.to_mapping(), token_hash=sha256(token.encode()).hexdigest(), **claims))
        return write(self.fixture/"private actors", digit + ".json", json.dumps(dict(version=2,
            reference=reference, proof=dict(method="local-cli-token", credential_ref=credential, challenge=token))))

    def app(self):
        worker_home = self.fixture/"worker home"
        worker_home.mkdir(mode=0o700)
        script = write(self.fixture, "fake.py", _WORKER)
        executable = write(self.fixture, "fake codex",
                           "#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", str(script)]) + ' "$@"\n', 0o700)
        return Application(Host(store=self.store,
                                profile=HostProfile(str(executable), str(worker_home), fixture=True)).compose(
                                    render_notice=render_notice))

    def test_visible_diff_failures_preserve_task_id_and_redact_host_errors(self):
        task_id = "task-" + "1" * 32
        host = Mock()
        host.interaction.task_status.return_value = {"status": "running"}
        host.store.load_task.return_value = dict(worktree_removed=False, worktree=dict(base_commit="base", branch="ref"))
        for failure, expected in ((OSError("private path"), "BINDING_CHANGED"),
                                  (ValueError("git failed"), "BINDING_CHANGED"),
                                  (subprocess.TimeoutExpired("git", 30), "BINDING_CHANGED"),
                                  (TaskError("PRODUCT_CHANGED"), "PRODUCT_CHANGED")):
            with self.subTest(failure=failure), patch("ato_task.ato_product.collect_diff", side_effect=failure):
                with self.assertRaises(TaskError) as caught:
                    project_diff(host, object(), task_id)
                self.assertEqual((caught.exception.exit_code, caught.exception.code, caught.exception.task_id),
                                 (4, expected, task_id))

    def test_help_and_read_only_diagnostics_do_not_touch_homes(self):
        for argv in (("--help",), ("project", "--help"), ("agent", "--help"), ("system", "--help")):
            result = subprocess.run([sys.executable, "-IB", ROOT/"ato.py", *argv], env=self.env,
                                    capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
        code, status = self.invoke("system", "status")
        self.assertEqual(code, 0, status)
        self.assertEqual(status["data"]["observer"]["state"], "absent")
        self.assertTrue(status["data"]["mailbox"]["idle"])
        code, preview = self.invoke("project", "validate", "--repo", self.repo, "--spec", self.spec_file)
        self.assertEqual(code, 0, preview)
        self.assertEqual(preview["data"]["repo"], str(self.repo))
        code, failure = self.invoke("list")
        self.assertEqual((code, failure["error"]["code"]), (3, "AUTH_REQUIRED"))
        self.assertFalse(self.store.home.exists())
        self.assertFalse(self.store.native_home.exists())

    def test_batch4_system_status_retries_observer_record_replacement(self):
        app = self.app()
        with patch("ato_task.ato_application.host_record", side_effect=[TaskError("BUSY"), FileNotFoundError]) as read:
            code, response = self.invoke("system", "status", app=app)
        self.assertEqual(code, 0, response)
        self.assertEqual(response["data"]["observer"]["state"], "absent")
        self.assertEqual(read.call_count, 2)
        self.assertFalse(self.store.home.exists())

    def submission(self):
        normalized = self.store.normalize_spec(self.repo, self.spec_file)
        self.registry(CONTROLLER, dict(enabled=True, reference=CONTROLLER, role="controller",
            revisions=[1], repo_key=normalized.repository.key))
        self.registry(INTERACTION, dict(enabled=True, reference=INTERACTION, role="interaction",
            revisions=[1], repo_key=normalized.repository.key))
        origin = self.enroll("3", "controller", CONTROLLER, submissions=[dict(reference="approved-fixture",
            repo_key=normalized.repository.key, spec_sha256=normalized.spec_sha256,
            binding=dict(controller_ref=CONTROLLER, interaction_ref=INTERACTION, revision=1))])
        return normalized, origin

    def test_cli_stops_and_removes_only_eligible_worktree_idempotently(self):
        app = self.app()
        normalized, origin = self.submission()
        proof_path = dict(self.env, ATO_ACTOR_FILE=str(origin))
        with patch.dict(os.environ, proof_path, clear=True):
            proof = app.host.actor_file()
            grant = app.host.initial(normalized, self.store._principal(proof))
            created = self.store.create_task(normalized, proof, grant.binding)
        task_id = created["task_id"]
        binding = self.store.load_task(task_id)["worktree"]
        code, result = self.invoke("stop", task_id, actor=origin, app=app)
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["status"], "stopped")
        checkout = Path(binding["path"])
        stray = write(checkout, "uncommitted.txt")
        code, result = self.invoke("project", "cleanup", task_id, "--confirm", actor=origin, app=app)
        self.assertEqual((code, result["error"]["code"]), (4, "CLEANUP_BLOCKED"))
        self.assertTrue(stray.is_file())
        self.assertFalse((self.store._task_dir(task_id)/"cleanup.json").exists())
        stray.unlink()
        for _ in range(2):
            code, result = self.invoke("project", "cleanup", task_id, "--confirm", actor=origin, app=app)
            self.assertEqual(code, 0, result)
            self.assertEqual(result["data"]["status"], "stopped")
            self.assertEqual(result["data"]["worktree_state"], "removed")
        self.assertFalse(checkout.exists())
        self.assertTrue(git(self.repo, "rev-parse", "--verify", "refs/heads/" + binding["branch"]))
        code, result = self.invoke("resume", task_id, actor=origin, app=app)
        self.assertEqual((code, result["error"]["code"]), (3, "FORBIDDEN"))
        task = self.store.load_task(task_id)
        with self.store._private(self.store._task_dir(task_id)) as fd:
            scope = self.store._scope(fd, task_id)
        scoped = self.enroll("8", "controller", CONTROLLER, scope=asdict(scope), binding_revision=1,
            run_id=task["run_id"], generation=app.host.interaction._removed_generation(task),
            operations=["observe", "resume"])
        code, result = self.invoke("resume", task_id, actor=scoped, app=app)
        self.assertEqual((code, result["error"]["code"]), (4, "NOT_RESUMABLE"))

    def test_private_locator_selects_same_source_and_has_no_ambient_authority(self):
        ref = "handoff-" + "a"*32
        env = dict(self.env, CTASK_HANDOFF_READER=str(self.fixture/"foreign"), PATH="")
        argv = private_runtime_command("read-handoff", ref)
        self.assertEqual(argv, [sys.executable, "-IB", str(ROOT/"ato_runtime.py"), "read-handoff", ref])
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout),
                         {"valid": False, "action": "end_silently", "reason": "stale_or_unavailable"})
        refused = subprocess.run(private_runtime_command("observe"), env=env,
                                 capture_output=True, text=True, timeout=15)
        self.assertEqual(refused.returncode, 3)
        self.assertFalse(self.store.home.exists())

    def test_compact_question_fences_old_binding_and_reissues_only_unsent_delivery(self):
        app = self.app()
        normalized, submitter = self.submission()
        with patch.dict(os.environ, dict(self.env, ATO_ACTOR_FILE=str(submitter)), clear=True):
            proof = app.host.actor_file()
            grant = app.host.initial(normalized, self.store._principal(proof))
            task_id = self.store.create_task(normalized, proof, grant.binding)["task_id"]
        task = self.store.load_task(task_id)
        repo_key = normalized.repository.key
        scope = dict(task_id=task_id, repo_key=repo_key, spec_sha256=normalized.spec_sha256,
                     authorization_ref=grant.reference)
        generation = app.host.execution.task_snapshot(task_id)["generation"]
        scoped = dict(scope=scope, run_id=task["run_id"], generation=generation)
        origin = self.enroll("4", "controller", CONTROLLER, **scoped, binding_revision=1,
                             operations=["observe", "ask", "handoff"])
        user = self.enroll("5", "user", INTERACTION, **scoped, binding_revision=1,
                           operations=["observe", "read", "handoff"])
        successor = "agentref-" + "8"*32
        next_user = self.enroll("6", "user", successor, **scoped, binding_revision=2,
                                operations=["observe", "read", "answer"])
        interaction = self.store.kernel_root/"ctask-interaction"
        path = interaction/"mailbox/state.sqlite"
        with ato_config.use_unified_kernel(self.store.kernel_root, self.store.native_home):
            bus = MessageBus(path)
        endpoint = {}
        for ref, role, revisions in ((CONTROLLER, "controller", [1, 2]),
                                     (INTERACTION, "interaction", [1]),
                                     (successor, "interaction", [2])):
            credential = bus.register_endpoint("fixture-" + ref, kind="local_cli", session_id=ref,
                                               project=self.repo, source="offline fixture")
            endpoint[ref] = credential
            self.registry(ref, dict(enabled=True, reference=ref, role=role, repo_key=repo_key,
                                    revisions=revisions, mailbox=asdict(credential)))
        self.registry("observation", dict(enabled=True, source_ref="fixture-observation", transport_kinds=["local_cli"]))
        hub = write(interaction, "hub.json", json.dumps(dict(thread_id=INTERACTION)))

        def proof_for(actor):
            with patch.dict(os.environ, dict(self.env, ATO_ACTOR_FILE=str(actor)), clear=True):
                return app.host.actor_file()

        service = app.host.interaction
        request = service.ask(proof_for(origin), task_id, kind="decision",
                              question="Confirm fixture option?", reason="offline proof")["request_id"]
        self.assertEqual(service.dispatch_delivery(task_id, request, "question"), "queued")
        reference = service.delivery_reference(task_id, request, "question")
        old_message = bus.message(reference["mailbox_id"])
        command = shlex.split(old_message["body"].splitlines()[1])
        old_locator = command[-1]
        self.assertEqual(command, private_runtime_command("read-handoff", old_locator))
        self.assertNotIn("Confirm fixture option?", old_message["body"])
        self.assertEqual(read_handoff(app.host, proof_for(user), old_locator)["request_id"], request)
        with patch.object(app.host.store, "runtime_seal", "f" * 64):
            self.assertEqual(read_handoff(app.host, proof_for(user), old_locator)["request_id"], request)
            # Distinct snapshot path in render_notice() (real multi-snapshot upgrade, P1-5).
            with patch("ato_task.ato_private.render_notice",
                       return_value=f"[ato handoff {old_locator}]\n/snapshots/release-2/.venv/bin/python -IB /snapshots/release-2/ato_runtime.py read-handoff {old_locator}"):
                self.assertEqual(read_handoff(app.host, proof_for(user), old_locator)["request_id"], request)
        from ato_task import ato_private
        self.assertFalse(ato_private._valid_notice_body(f"[ato handoff {old_locator}]\n/bin/sh -c evil", old_locator))
        self.assertFalse(ato_private._valid_notice_body(
            f"[ato handoff handoff-{'0'*32}]\n/p/python -IB /p/ato_runtime.py read-handoff {old_locator}", old_locator))
        # P1-5: reconcile_delivery does not reject prior-release tasks with RUNTIME_MISMATCH.
        from scripts import ato_host_setup
        with patch.object(app.host.store, "runtime_seal", "f" * 64), self.assertRaises(TaskError) as caught:
            ato_host_setup.reconcile_delivery(
                app.host, SimpleNamespace(task_id=task_id, message_id=old_message["id"], source="fixture"))
        self.assertEqual(caught.exception.code, "FORBIDDEN")  # Passed seal check; stopped only because external_possible=False.

        evidence = write(self.fixture, "handoff.json", json.dumps({
            "schema_version": 1, "task_id": task_id, "spec_sha256": normalized.spec_sha256,
            "binding_revision": 1, "source": "verified-handoff", "from": CONTROLLER, "to": CONTROLLER,
            "interaction": successor,
        }))
        self.registry("decision-" + sha256(evidence.read_bytes()).hexdigest(), dict(source_ref="verified-handoff",
            operation="handoff", actor_id="credential-" + "5"*32))
        code, handed = self.invoke("agent", "handoff", task_id, "--to", CONTROLLER, "--interaction",
                                  successor, "--evidence", evidence, actor=origin, app=app)
        self.assertEqual(code, 0, handed)
        with self.assertRaises(TaskError):
            read_handoff(app.host, proof_for(user), old_locator)
        hub.write_text(json.dumps(dict(thread_id=successor)))
        tick(app.host)
        current = service.delivery_reference(task_id, request, "question")
        self.assertNotEqual(current["mailbox_id"], old_message["id"])
        self.assertEqual(bus.message(old_message["id"])["state"], "cancelled")
        current_message = bus.message(current["mailbox_id"])
        self.assertEqual(current_message["target"], endpoint[successor].endpoint_id)
        locator = shlex.split(current_message["body"].splitlines()[1])[-1]
        self.assertEqual(read_handoff(app.host, proof_for(next_user), locator)["request_id"], request)

    def test_cli_admits_verified_task_and_accepts_only_exact_user_evidence(self):
        app = self.app()
        normalized, origin = self.submission()
        code, launched = self.invoke("run", "--repo", self.repo, "--spec", self.spec_file, "--foreground",
                                     actor=origin, app=app)
        self.assertEqual(code, 0, launched)
        status = launched["data"]
        self.assertEqual(status["status"], "completed")
        task_id = status["task_id"]
        code, listed = self.invoke("list", "--repo", self.repo, actor=origin, app=app)
        self.assertEqual(code, 0, listed)
        self.assertEqual([task["task_id"] for task in listed["data"]["tasks"]], [task_id])
        scope = dict(task_id=task_id, repo_key=normalized.repository.key, spec_sha256=status["spec_sha256"],
                     authorization_ref="approved-fixture")
        user = self.enroll("4", "user", INTERACTION, scope=scope, binding_revision=1,
            generation=app.host.execution.task_snapshot(task_id)["generation"], run_id=status["execution"]["run_id"],
            operations=["observe", "read", "answer", "accept"])
        code, inbox = self.invoke("inbox", actor=user, app=app)
        self.assertEqual(code, 0, inbox)
        self.assertEqual(inbox["data"]["items"], [])
        self.assertIsNone(status["acceptance"]["receipt_id"])
        product = status["product"]
        evidence = write(self.fixture, "accept.json", json.dumps(dict(schema_version=1, task_id=task_id,
            spec_sha256=status["spec_sha256"], product_id=product["product_id"],
            checks_sha256=product["checks_sha256"], criteria=["user"], decision="accept",
            source="fixture human", note="accepted exact fixture bytes")))
        code, refused = self.invoke("accept", task_id, "--evidence", evidence, actor=origin, app=app)
        self.assertEqual((code, refused["error"]["code"]), (3, "FORBIDDEN"))
        code, accepted = self.invoke("accept", task_id, "--evidence", evidence, actor=user, app=app)
        self.assertEqual(code, 0, accepted)
        self.assertEqual(accepted["data"]["status"], "completed")
        code, repeated = self.invoke("accept", task_id, "--evidence", evidence, actor=user, app=app)
        self.assertEqual(code, 0, repeated)
        self.assertEqual(repeated["data"]["acceptance"], accepted["data"]["acceptance"])


class ReleaseCLI(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(TemporaryDirectory(prefix="ato-cutover-"))).resolve()
        self.paths = release.scoped_paths(self.base/"bin", self.base/"ato", self.base/"native")
        self.paths["bin_dir"].mkdir()
        self.target = snapshot(self.base/"sealed target")
        self.addCleanup(unseal, self.target)
        self.old = snapshot(self.base/"sealed legacy", legacy=True)
        self.addCleanup(unseal, self.old)
        self.transaction = self.base/"release journal"
        self.system = FakeSystem()
        self.bin = self.paths["bin_dir"]

    def activate(self, **options):
        return release.activate(self.target, CURRENT, self.paths, self.transaction, self.system, **options)

    def test_exact_mixed_cutover_removes_old_launchers_and_rollback_is_idempotent(self):
        other = snapshot(self.base/"separate reader", legacy=True)
        self.addCleanup(unseal, other)
        for index, name in enumerate(release.LEGACY[:-1]):
            legacy_entry(self.bin, name, other if name == "ato-read-handoff" else self.old, (0o710, 0o711, 0o750)[index])
        before = release.entrypoints(self.bin)
        sentinel = write(self.paths["ato_home"], "unified-v1/retained.txt", "retained evidence")
        with self.assertRaisesRegex(release.Refused, "explicit convergence"):
            self.activate()
        result = self.activate(converge_mixed=True)
        self.assertEqual(result["state"], "activated")
        self.assertEqual(set(p.name for p in self.bin.iterdir()), {"ato"})
        metadata = release.read(self.transaction/"transaction.json")
        self.assertEqual(metadata["before"]["entries"], before)
        self.assertEqual(metadata["before"]["sources"]["ato-read-handoff"]["root"], str(other))
        self.assertEqual(metadata["before"]["entries"]["ato"], None)
        self.assertEqual(metadata["before"]["entries"]["ato-hub"], None)
        for _ in range(2):
            result = release.rollback(self.transaction, self.paths, self.system)
            self.assertEqual(result["state"], "rolled_back")
            self.assertEqual(release.entrypoints(self.bin), before)
        self.assertEqual(sentinel.read_text(), "retained evidence")
        self.assertEqual(self.system.calls, [])

    def test_interrupted_write_restores_old_executable_and_removes_new_one(self):
        legacy_entry(self.bin, "ctask", self.old)
        before = release.entrypoints(self.bin)
        atomic = release.atomic_entry

        def interrupted(path, row):
            atomic(path, row)
            if path.name == "ctask" and row is None:
                raise KeyboardInterrupt

        with patch.object(release, "atomic_entry", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            self.activate()
        self.assertEqual(release.entrypoints(self.bin), before)
        self.assertEqual(release.read(self.transaction/"transaction.json")["state"], "rolled_back")

    def test_rollback_refuses_a_changed_executable_without_overwriting_it(self):
        self.activate()
        journal = self.transaction/"transaction.json"
        recorded = journal.read_bytes()
        launcher = self.bin/"ato"
        launcher.chmod(0o711)
        before = launcher.read_bytes()
        with self.assertRaisesRegex(release.Refused, "changed outside"):
            release.rollback(self.transaction, self.paths, self.system)
        self.assertEqual(launcher.read_bytes(), before)
        self.assertEqual(launcher.stat().st_mode & 0o777, 0o711)
        self.assertEqual(journal.read_bytes(), recorded)
