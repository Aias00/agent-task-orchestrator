"""Offline T4 acceptance with real Task/Run state, Git and the managed mailbox."""
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import agent_registry
import ato_config
import ato_process
from ato_acceptance import InteractionService, MailboxRoute, VerifiedEvent
from ato_execution import ExecutionService, HostProfile, _initial_binding, _publish_receipt
from ato_task_store import InitialBinding, RunIntent, SubmissionGrant, TaskError
from ato_task_store import TaskStore, new_id
from message_bus import MessageBus
import notification_state
import recovery
from test_ato_unified_execution import _REVIEWER, _WORKER, git
from transport import DeliveryResult, TransportEvent, receive_event


_CONTROLLER = "agentref-" + "1" * 32
_INTERACTION = "agentref-" + "2" * 32
_NEXT = "agentref-" + "3" * 32


class UnifiedAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory(prefix="ato-acceptance-"))).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        (self.repo / ".gitignore").write_text(".codex/tasks/\n")
        (self.repo / "source.txt").write_text("fixture base\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "base")
        self.proofs = {}
        self.events = {}
        self.bindings = {}
        self.clock = 1000
        self.fixture_submit = object()
        self.store = TaskStore(self.root / "ato", native_home=self.root / "native", runtime_seal="4" * 64,
            authenticate=self.proofs.get, can_observe=self.can_observe,
            authorize_submit=lambda spec, proof, binding: SubmissionGrant(
                "fixture-grant", spec.repository.key, spec.spec_sha256, binding)
            if proof is self.fixture_submit else None)
        worker = self.tool("worker", _WORKER)
        reviewer = self.tool("reviewer", _REVIEWER)
        self.execution = ExecutionService(self.store, HostProfile(str(worker), str(self.root / "worker-home"),
            (("codex", str(reviewer), str(self.root / "reviewer-home")),), fixture=True),
            authorize=lambda actor, scope, binding, op: actor.role == "origin"
            and actor.session.reference == binding["controller_ref"])
        self.execution.hooks = replace(self.execution.hooks, binding=self.binding)
        self.interaction = InteractionService(self.store, self.execution, authorize=self.authorize,
            verify_event=self.verify_event, verify_continuation=lambda scope, binding, item, answer: True,
            clock=lambda: self.clock)
        self.execution.hooks = self.interaction.execution_hooks()

    def tool(self, name, body):
        (self.root / (name + "-home")).mkdir(mode=0o700)
        path = self.root / name
        script = path.with_suffix(".py")
        script.write_text(body)
        path.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, "-I", str(script)]) + ' "$@"\n')
        path.chmod(0o700)
        return path

    def binding(self, task, spec):
        return dict(self.bindings.get(task["task_id"], _initial_binding(task, spec)))

    def can_observe(self, principal, scope):
        task = self.store.load_task(scope.task_id)
        return principal.workflow_id == task["workflow"]["directory"]

    def authorize(self, principal, scope, binding, action, item):
        # The fixture grant registry pins the current route revision to this actor credential.
        return self.proofs.get((principal.actor_id, principal.workflow_id, principal.generation,
            binding["revision"], principal.session.reference, principal.role)) is principal

    def verify_event(self, principal, scope, binding, action, item, value, event):
        ref = item.get("request_id", scope.task_id)
        row = self.events.get(event)
        if row is not None and row[:5] == (principal.actor_id, binding["revision"], action, ref, value):
            return row[5]
        return None

    def proof(self, task_id, role, actor_id=None, *, reference=None):
        task = self.store.load_task(task_id)
        binding = self.binding(task, self.store.load_spec(task_id))
        revision = self.execution.task_snapshot(task_id)["generation"][1]
        ref = reference or binding["controller_ref" if role == "origin" else "interaction_ref"]
        identity = agent_registry.SessionIdentity("local", ref + "-fixture", str(self.repo), "fixture")
        session = agent_registry.SessionReference(ref, identity)
        actor_id = actor_id or role + "-fixture"
        actor = agent_registry.ActorContext._from_verified_adapter(actor_id, role, session, str(self.repo),
            task["workflow"]["directory"], task["workflow"]["contract_hash"], revision, "fixture-proof")
        self.proofs[actor] = actor
        self.proofs[(actor_id, actor.workflow_id, actor.generation,
                     binding["revision"], ref, actor.role)] = actor
        return actor

    def event(self, proof, action, reference, value, *, binding_revision=1, grant_ref=None, performed=False):
        token = object()
        verified = VerifiedEvent("fixture-event-" + str(len(self.events)), grant_ref, performed)
        self.events[token] = (proof.actor_id, binding_revision, action, reference, value, verified)
        return token

    def create(self, *, review=False):
        spec = dict(schema_version=1, repo=str(self.repo), base_ref="HEAD", worker_contract=dict(
            objective="produce the frozen output", scope="temporary worktree only", authorization="fixture",
            acceptance=[dict(id="check", description="Independent host check", checks=[["true"]]),
                        dict(id="user-z", kind="manual", description="Verify delivery"),
                        dict(id="user-a", kind="manual", description="Accept the exact version")]))
        if review:
            spec["reviewer"] = dict(agent=dict(kind="codex"), max_review_rounds=4)
        config = self.root / "spec.json"
        config.write_text(json.dumps(spec))
        return self.store.create_task(self.store.normalize_spec(self.repo, config), self.fixture_submit,
                                      InitialBinding(_CONTROLLER, _INTERACTION))["task_id"]

    def ask(self, task, *, continuation=None, kind="decision", purpose_ref=None, expires_at=None):
        return self.interaction.ask(self.proof(task, "origin"), task, kind=kind,
            question="Which frozen option should the host use?", reason="fixture question",
            continuation=continuation, purpose_ref=purpose_ref, expires_at=expires_at)["request_id"]

    def switch(self, task_id):
        task = self.store.load_task(task_id)
        binding = self.binding(task, self.store.load_spec(task_id))
        with self.execution.transaction(task_id, task["revision"], task["stop_epoch"], binding["revision"]):
            self.bindings[task_id] = dict(binding, revision=binding["revision"] + 1, interaction_ref=_NEXT)

    def assert_code(self, code, call, *args, **kwargs):
        with self.assertRaises(TaskError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)

    def qualify(self, task):
        status = self.execution.start(self.proof(task, "origin"), task, foreground=True)
        self.assertEqual(status["status"], "needs_acceptance", status)
        return status

    def evidence(self, status, **changes):
        values = dict(schema_version=1, task_id=status["task_id"], spec_sha256=status["spec_sha256"],
            product_id=status["product"]["product_id"], checks_sha256=status["product"]["checks_sha256"],
            criteria=["user-z", "user-a"], decision="accept", source="verified human action",
            note="I accept these exact bytes") | changes
        path = self.root / "accept.json"
        path.write_text(json.dumps(values, indent=2))
        return path, dict(values, criteria=sorted(values["criteria"]))

    def test_inbox_read_and_presentation_are_scoped_and_separate_from_reply(self):
        task = self.create()
        request = self.ask(task)
        self.assertEqual(self.ask(task), request)
        user = self.proof(task, "user")
        hub = self.proof(task, "hub")
        origin = self.proof(task, "origin")
        other_user = self.proof(self.create(), "user")
        leaf = self.store._task_dir(task) / "interaction"
        before = {p.name: p.read_bytes() for p in leaf.iterdir()}
        item = self.interaction.read(hub, request)
        self.assertEqual(self.interaction.inbox(user), {"items": [item]})
        self.assertEqual(self.interaction.inbox(origin), {"items": []})
        self.assertEqual(self.interaction.inbox(other_user), {"items": []})
        self.assert_code("NOT_FOUND", self.interaction.read, other_user, request)
        self.assertEqual(before, {p.name: p.read_bytes() for p in leaf.iterdir()})
        self.assert_code("FORBIDDEN", self.interaction.record_answer, hub, request, "yes", None)
        self.assert_code("FORBIDDEN", self.interaction.record_answer, user, request, "yes", "claimed-source")

        receipt = self.interaction.record_presentation(hub, request, self.event(hub, "present", request, None))
        self.assertEqual(receipt, self.interaction.record_presentation(
            hub, request, self.event(hub, "present", request, None)))
        self.assertEqual(self.interaction.inbox(hub), {"items": [item]})
        text = " keep  exact spacing\n"
        answer = self.interaction.record_answer(user, request, text, self.event(user, "answer", request, text))
        self.assertEqual(answer, self.interaction.record_answer(
            user, request, text, self.event(user, "answer", request, text)))
        self.assertEqual(answer["delivery"], "recorded_only")
        self.assertEqual(self.interaction.read_answer(origin, request)["text"], text)
        self.assertEqual(self.interaction.inbox(user), {"items": []})
        conflicting = self.event(user, "answer", request, "different")
        self.assert_code("DECISION_CONFLICT", self.interaction.record_answer, user, request, "different", conflicting)
        self.assertEqual(self.execution.task_status(origin, task)["status"], "preparing")

    def test_handoff_keeps_pending_id_but_invalidates_old_route_and_events(self):
        task = self.create()
        request = self.ask(task)
        user = self.proof(task, "user")
        old_event = self.event(user, "answer", request, "old reply")
        self.switch(task)
        self.assertEqual(self.interaction.inbox(user), {"items": []})
        self.assert_code("NOT_FOUND", self.interaction.record_answer, user, request, "old reply", old_event)
        new_user = self.proof(task, "user")
        self.assertEqual(self.interaction.inbox(new_user)["items"][0]["request_id"], request)
        self.assertEqual(self.ask(task), request)
        self.assert_code("FORBIDDEN", self.interaction.record_answer, new_user, request, "old reply", old_event)
        result = self.interaction.record_answer(new_user, request, "actual reply",
            self.event(new_user, "answer", request, "actual reply", binding_revision=2))
        self.assertEqual(result["state"], "answered")

    def test_expiry_stop_and_grant_purpose_fence_answers_and_execution(self):
        task = self.create()
        request = self.ask(task, continuation="resume", kind="permission",
                           purpose_ref="frozen-capability", expires_at=1001)
        user = self.proof(task, "user")
        wrong = self.event(user, "answer", request, "yes", grant_ref="unrelated")
        self.assert_code("FORBIDDEN", self.interaction.record_answer, user, request, "yes", wrong)
        self.interaction.record_answer(user, request, "yes", self.event(user, "answer", request, "yes"))
        task_record = self.store.load_task(task)
        binding = self.binding(task_record, self.store.load_spec(task))
        self.assertEqual(self.interaction.decision_barrier(task_record, binding, "resume"), "STALE_REQUEST")
        self.clock = 1002
        self.assert_code("STALE_REQUEST", self.interaction.record_answer, user, request, "yes",
                         self.event(user, "answer", request, "yes"))
        self.assertEqual(self.interaction.decision_barrier(task_record, binding, "resume"), "STALE_REQUEST")
        other = self.ask(task)
        self.execution.request_stop(self.proof(task, "origin"), task)
        self.assertEqual(self.interaction.inbox(user), {"items": []})
        self.assert_code("STALE_REQUEST", self.interaction.record_answer, user, other, "yes",
                         self.event(user, "answer", other, "yes"))

    def test_review_checks_and_authentic_user_must_all_match_current_product(self):
        task = self.create(review=True)
        qualified = self.qualify(task)
        self.assertEqual(qualified["review"]["verdict"], "approve")
        self.assertEqual(qualified["checks"][0]["state"], "passed")
        self.assertEqual(qualified["acceptance"]["disposition"], "pending")
        user, hub = self.proof(task, "user"), self.proof(task, "hub")
        manual, = self.interaction.inbox(user)["items"]
        path, normalized = self.evidence(qualified)
        user_event = self.event(user, "accept", task, normalized)
        self.assert_code("FORBIDDEN", self.interaction.accept_product, hub, task, path, source_event=user_event)
        worker = self.proof(task, "worker")
        self.assert_code("FORBIDDEN", self.interaction.accept_product, worker, task, path, source_event=user_event)
        self.assert_code("FORBIDDEN", self.interaction.accept_product, user, task, path, source_event="forged")
        self.assert_code("FORBIDDEN", self.interaction.record_answer, user, manual["request_id"], "I approve",
                         self.event(user, "answer", manual["request_id"], "I approve"))
        invalid, _ = self.evidence(qualified, criteria=["user-a"])
        self.assert_code("INVALID_EVIDENCE", self.interaction.accept_product, user, task, invalid)
        path, normalized = self.evidence(qualified)
        accepted = self.interaction.accept_product(user, task, path, source_event=user_event)
        self.assertEqual(accepted["status"], "completed", accepted)
        ref = accepted["acceptance"]["receipt_id"]
        record = json.loads(self.store.read_evidence(task, ref))
        self.assertEqual(record["request_id"], manual["request_id"])
        self.assertEqual(record["review_receipt"], qualified["review"]["receipt_id"])
        self.assertEqual(self.store.read_evidence(task, record["source_sha256"]), path.read_bytes())
        self.assertEqual(record["decision"], normalized)
        self.assertEqual(self.interaction.inbox(hub), {"items": []})
        path.write_text(json.dumps(normalized))
        self.assertEqual(self.interaction.accept_product(user, task, path,
            source_event=self.event(user, "accept", task, normalized))["acceptance"]["receipt_id"], ref)
        self.assert_code("STALE_REQUEST", self.ask, task)

        (Path(qualified["product"]["worktree"]) / "delivery.txt").write_text("updated product\n")
        invalidated = self.interaction.task_status(user, task)
        self.assertEqual((invalidated["status"], invalidated["acceptance"]["disposition"]), ("blocked", "invalidated"))
        self.assert_code("CHECKS_REQUIRED", self.interaction.accept_product, user, task, path, source_event=user_event)
        rechecked = self.execution.recheck(self.proof(task, "origin"), task, foreground=True)
        self.assertEqual(rechecked["status"], "needs_acceptance", rechecked)
        self.assertEqual(rechecked["acceptance"]["disposition"], "invalidated")
        user = self.proof(task, "user")
        self.assert_code("PRODUCT_CHANGED", self.interaction.accept_product, user, task, path,
                         source_event=self.event(user, "accept", task, normalized))
        path, updated = self.evidence(rechecked)
        new = self.interaction.accept_product(user, task, path, source_event=self.event(user, "accept", task, updated))
        self.assertEqual(new["status"], "completed")
        self.assertNotEqual(new["acceptance"]["receipt_id"], ref)
        self.assertEqual(json.loads(self.store.read_evidence(task, ref)), record)

    def test_same_product_recheck_requires_its_new_independent_check_set(self):
        task = self.create()
        original = self.qualify(task)
        path, decision = self.evidence(original)
        rechecked = self.execution.recheck(self.proof(task, "origin"), task, foreground=True)
        self.assertEqual(rechecked["status"], "needs_acceptance")
        self.assertEqual(original["product"]["product_id"], rechecked["product"]["product_id"])
        self.assertNotEqual(original["product"]["checks_sha256"], rechecked["product"]["checks_sha256"])
        user = self.proof(task, "user")
        self.assert_code("CHECKS_REQUIRED", self.interaction.accept_product, user, task, path,
                         source_event=self.event(user, "accept", task, decision))
        path, decision = self.evidence(rechecked)
        accepted = self.interaction.accept_product(user, task, path,
            source_event=self.event(user, "accept", task, decision))
        self.assertEqual(accepted["status"], "completed")

    def test_stop_between_evidence_verification_and_acceptance_commit_wins(self):
        task = self.create()
        qualified = self.qualify(task)
        user, origin = self.proof(task, "user"), self.proof(task, "origin")
        path, decision = self.evidence(qualified)
        retain = self.interaction._retain

        def retain_and_stop(task_id, kind, **fields):
            receipt = retain(task_id, kind, **fields)
            if kind == "acceptance":
                self.execution.request_stop(origin, task_id)
            return receipt

        with patch.object(self.interaction, "_retain", side_effect=retain_and_stop):
            self.assert_code("CHECKS_REQUIRED", self.interaction.accept_product, user, task, path,
                             source_event=self.event(user, "accept", task, decision))
        status = self.interaction.task_status(user, task)
        self.assertEqual(status["status"], "stopped")
        self.assertIsNone(status["acceptance"]["receipt_id"])
        self.assertEqual(self.interaction.inbox(user), {"items": []})

    def test_continuation_claim_is_bound_to_exact_run_and_live_kernel_owner(self):
        task = self.create()
        # Seed a verified retryable predecessor and exercise the T3 child
        # callback under the real existing kernel lease in this test process.
        record = self.store.load_task(task)
        previous, _ = self.store._run(record, record["run_id"])
        generation = [None, 0]
        _publish_receipt(self.store, previous, "launching", generation, {})
        _publish_receipt(self.store, previous, "settled", generation, {"cleanup_verified": True}, "blocked")
        request = self.ask(task, continuation="resume", expires_at=1002)
        user = self.proof(task, "user")
        self.interaction.record_answer(user, request, "continue", self.event(user, "answer", request, "continue"))
        record = self.store.load_task(task)
        self.assertEqual(record["run_id"], previous["run_id"])
        self.assertEqual(self.interaction.task_status(user, task)["status"], "waiting")
        next_run = new_id("run")
        intent = RunIntent(next_run, previous["run_id"], "resume", 2, "explicit_resume",
                           self.store.runtime_seal, tuple(generation))
        record = self.store.append_run(task, intent, record["revision"])
        run, _ = self.store._run(record, next_run)
        _publish_receipt(self.store, run, "launching", generation, {})
        record = self.store.load_task(task)
        binding = self.binding(record, self.store.load_spec(task))
        self.assertIsNone(self.interaction.decision_barrier(record, binding, "resume"))
        self.assert_code("RUN_UNSETTLED", self.interaction.claim_continuation,
                         record, binding, run, generation)
        state, _, _, _ = self.store._kernel_facts(record)
        with ato_config.use_unified_kernel(self.store.kernel_root, self.store.native_home):
            with recovery.execution_owner(run["workflow_dir"], state, explicit_run=True) as lease:
                owner = dict(task_id=task, run_id=next_run, runtime_seal=self.store.runtime_seal,
                    pid=os.getpid(), birth=ato_process.identity(os.getpid()), lease_fence=lease.fence,
                    generation=generation)
                record = _publish_receipt(self.store, run, "active", generation, owner)
                self.clock = 1003
                self.assert_code("STALE_REQUEST", self.interaction.claim_continuation,
                                 record, binding, run, generation)
                self.clock = 1001
                receipt = self.interaction.claim_continuation(record, binding, run, generation)
                self.assertEqual(receipt, self.interaction.claim_continuation(record, binding, run, generation))
                retained = json.loads(self.store.read_evidence(task, receipt))
                self.assertEqual((retained["run_id"], retained["generation"], retained["lease_fence"]),
                                 (next_run, generation, lease.fence))
        self.assert_code("RUN_UNSETTLED", self.interaction.claim_continuation,
                         record, binding, run, generation)

    def mailbox(self):
        with ato_config.use_unified_kernel(self.store.kernel_root, self.store.native_home):
            bus = MessageBus(ato_config.interaction_directory() / "mailbox" / "state.sqlite")
        self.interaction.mailbox = bus
        return bus

    def test_unknown_answer_is_a_barrier_and_correlated_reconciliation_does_not_replay(self):
        task = self.create()
        request = self.ask(task)
        user = self.proof(task, "user")
        bus = self.mailbox()
        sender = bus.register_endpoint("hub-endpoint", kind="fake", session_id="hub",
                                       project=str(self.repo), source="fixture")
        target = bus.register_endpoint("origin-endpoint", kind="fake", session_id="origin",
                                       project=str(self.repo), source="fixture")
        owner = bus.acquire_endpoint(target, owner="offline-fixture")
        calls = []

        class Adapter:
            def send(self, endpoint, message):
                calls.append(message)
                return DeliveryResult("delivery_unknown")

        self.interaction.route = lambda *args: MailboxRoute(sender, target, "fixture-route", owner,
            {"fake": Adapter()}) if args[2] == "answer" else None
        event = self.event(user, "answer", request, "choice")
        recorded = self.interaction.record_answer(user, request, "choice", event)
        self.assertEqual(recorded["delivery"], "delivery_unknown")
        self.assertEqual(calls, [])
        self.assertEqual(self.interaction.dispatch_delivery(task, request, "answer"), "delivery_unknown")
        self.assertEqual(self.interaction.dispatch_delivery(task, request, "answer"), "delivery_unknown")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.interaction.record_answer(user, request, "choice", event), recorded)
        self.switch(task)
        self.assertEqual(self.interaction.supersede_unsubmitted(task, request, "answer", sender),
                         "delivery_unknown")
        self.assertEqual(self.interaction.dispatch_delivery(task, request, "answer"), "delivery_unknown")
        task_record = self.store.load_task(task)
        self.assertEqual(self.interaction.decision_barrier(
            task_record, self.binding(task_record, self.store.load_spec(task)), "resume"), "DELIVERY_UNKNOWN")
        message = bus.message(calls[0]["id"])
        event_kwargs = dict(message_id=message["id"], endpoint_id=target.endpoint_id,
            session_id=target.session_id, source="fixture lifecycle", version=2,
            project=target.project, correlation_id=request, generation=json.loads(message["generation"]),
            route_generation=message["route_generation"], endpoint_generation=message["endpoint_generation"],
            attempt_id=message["attempt_id"], turn_id="fixture-turn")
        receive_event(bus, target, TransportEvent("accepted", receipt_id="receipt-accept", **event_kwargs))
        receive_event(bus, target, TransportEvent("turn_completed", receipt_id="receipt-complete", **event_kwargs))
        self.assertEqual(self.interaction.dispatch_delivery(task, request, "answer"), "turn_completed")
        self.assertEqual(len(calls), 1)
        task_record = self.store.load_task(task)
        self.assertIsNone(self.interaction.decision_barrier(
            task_record, self.binding(task_record, self.store.load_spec(task)), "initial"))
        self.assertEqual(self.interaction.task_status(self.proof(task, "user"), task)["acceptance"]["disposition"],
                         "pending")

    def test_handoff_supersedes_only_the_unsent_reservation(self):
        task = self.create()
        request = self.ask(task)
        user = self.proof(task, "user")
        bus = self.mailbox()
        sender = bus.register_endpoint("hub-endpoint", kind="fake", session_id="hub",
                                       project=str(self.repo), source="fixture")
        target = bus.register_endpoint("origin-endpoint", kind="fake", session_id="origin",
                                       project=str(self.repo), source="fixture")
        self.interaction.route = lambda *args: MailboxRoute(sender, target, "fixture-route")
        self.interaction.record_answer(user, request, "choice", self.event(user, "answer", request, "choice"))
        # Simulate losing the CAS after the canonical enqueue, before the
        # leaf links the mailbox ID. The same envelope remains recoverable.
        with patch.object(self.interaction, "_save_delivery", side_effect=TaskError("BUSY")):
            self.assert_code("BUSY", self.interaction.dispatch_delivery, task, request, "answer")
        self.assertEqual(self.interaction.delivery_reference(task, request, "answer")["state"], "queued")
        self.switch(task)
        self.assertEqual(self.interaction.supersede_unsubmitted(task, request, "answer", sender), "cancelled")
        reference = self.interaction.delivery_reference(task, request, "answer")
        self.assertEqual(bus.message(reference["mailbox_id"])["external_possible"], 0)
        self.assertEqual(self.interaction.dispatch_delivery(task, request, "answer"), "cancelled")
        record = self.store.load_task(task)
        self.assertIsNone(self.interaction.decision_barrier(
            record, self.binding(record, self.store.load_spec(task)), "initial"))

    def test_notification_policy_and_completion_once_keep_human_receipt_separate(self):
        task = self.create()
        qualified = self.qualify(task)
        user = self.proof(task, "user")
        item, = self.interaction.inbox(user)["items"]
        bus = self.mailbox()
        origin = bus.register_endpoint("origin-endpoint", kind="fake", session_id="origin",
                                       project=str(self.repo), source="fixture")
        hub = bus.register_endpoint("hub-endpoint", kind="fake", session_id="hub",
                                    project=str(self.repo), source="fixture")
        owner = bus.acquire_endpoint(hub, owner="fixture")
        calls = []

        class Adapter:
            def send(self, endpoint, message):
                calls.append(message["kind"])
                return DeliveryResult("delivery_unknown")

        self.interaction.route = lambda *args: MailboxRoute(origin, hub, "fixture-route", owner,
                                                           {"fake": Adapter()})
        policy = dict(hub_thread=_INTERACTION, mode="user_input_only", completion="once", completion_since=900)
        with patch.object(notification_state, "status", return_value=policy):
            with patch.object(notification_state, "project_mutes", return_value=[{"repo": str(self.repo)}]):
                self.assertEqual(self.interaction.dispatch_delivery(task, item["request_id"], "question"),
                                 "recorded_only")
            self.assertEqual(self.interaction.dispatch_delivery(task, item["request_id"], "question"),
                             "delivery_unknown")
            self.assertEqual(self.interaction.inbox(user)["items"][0]["delivery"], "delivery_unknown")
            path, decision = self.evidence(qualified)
            completed = self.interaction.accept_product(user, task, path,
                source_event=self.event(user, "accept", task, decision))
            receipt_id = completed["acceptance"]["receipt_id"]
            self.assertEqual(completed["status"], "completed")
            self.assertIn(dict(request_id=item["request_id"], phase="question", state="delivery_unknown"),
                          completed["delivery"])
            message_id = self.interaction.delivery_reference(task, item["request_id"], "question")["mailbox_id"]
            message = bus.message(message_id)
            bus.reconcile_correlated(hub, owner, message_id, source="verified absent synthetic turn",
                outcome="not_running", generation=json.loads(message["generation"]),
                endpoint_generation=message["endpoint_generation"])
            self.assertEqual(self.interaction.dispatch_delivery(task, receipt_id, "completion"), "delivery_unknown")
            self.assertEqual(self.interaction.dispatch_delivery(task, receipt_id, "completion"), "delivery_unknown")
        self.assertEqual(calls, ["ato_question", "ato_completion"])
        self.assertEqual(self.interaction.task_status(user, task)["acceptance"]["receipt_id"], receipt_id)
