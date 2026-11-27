"""Offline watchdog regressions using private records and synthetic observations."""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ato_task import ato_watchdog as watchdog
from ato_task.ato_task_store import TaskStore, SubmissionOrigin, TaskError

TASK = "task-" + "a" * 32
RUN = "run-" + "b" * 32
SEAL = "a" * 64


def sample(now=1000, **changes):
    result = dict(task_id=TASK, scope=dict(revision=1, run_id=RUN, stop_epoch=0,
        binding_revision=1, spec_sha256="b" * 64, runtime_seal=SEAL), sampled_at=now,
        generation=[1.0, 0], status="running", reason=None,
        execution=dict(state="active", run_id=RUN),
        stage=dict(steps=[], remaining_stage_ids=[], elapsed_seconds=20),
        run_history=[dict(run_id=RUN, kind="initial", phase="active", outcome=None,
                          created_at=900, settled_at=None)], created_at=900,
        evidence_complete=True, diff_error=None, violations=[], progress="c" * 64)
    result.update(changes)
    if result["status"] in {"blocked", "completed", "stopped"} and "execution" not in changes:
        result["execution"] = dict(state="idle", run_id=RUN)
    return result


class WatchdogRules(unittest.TestCase):
    def test_execution_uncertain_requires_two_samples_and_recovers_then_recurs(self):
        first = sample(execution=dict(state="unknown", run_id=RUN))
        row = watchdog.reduce_sample(None, first)
        self.assertEqual(row["alerts"], [])
        row = watchdog.reduce_sample(row, first | {"sampled_at": 1060})
        alert = row["alerts"][0]
        self.assertEqual(alert["code"], "EXECUTION_UNVERIFIED")
        original = alert["id"]
        row = watchdog.reduce_sample(row, first | {"sampled_at": 1120})
        self.assertEqual([x["id"] for x in row["alerts"]], [original])
        row = watchdog.reduce_sample(row, sample(1180))
        self.assertEqual(row["alerts"][0]["resolved_at"], 1180)
        row = watchdog.reduce_sample(row, first | {"sampled_at": 1240})
        row = watchdog.reduce_sample(row, first | {"sampled_at": 1300})
        self.assertEqual(len(row["alerts"]), 2)
        self.assertNotEqual(row["alerts"][-1]["id"], original)

    def test_gap_and_run_stop_or_binding_change_reset_confirmation(self):
        first = sample(execution=dict(state="unknown", run_id=RUN))
        row = watchdog.reduce_sample(None, first)
        self.assertFalse(watchdog.reduce_sample(row, first | {"sampled_at": 1300})["alerts"])
        for key, value in (("run_id", "run-" + "d" * 32), ("stop_epoch", 1), ("binding_revision", 2)):
            changed = deepcopy(first)
            changed["sampled_at"] = 1060
            changed["scope"][key] = value
            self.assertFalse(watchdog.reduce_sample(row, changed)["alerts"])

    def test_repeat_stage_and_scope_evidence_are_advisory(self):
        current = sample(status="blocked", reason="STAGES_INCOMPLETE",
            stage=dict(steps=[dict(stage_id="one", step_id="implement", attempts=3, state="failed")],
                       remaining_stage_ids=["two"], elapsed_seconds=33),
            execution=dict(state="idle", run_id=RUN),
            violations=[dict(rule="forbid_delete_paths", path="api.py", change="deleted")])
        row = watchdog.reduce_sample(None, current)
        self.assertEqual({a["code"] for a in row["alerts"]}, {
            "REPEATED_ATTEMPTS", "STAGES_INCOMPLETE", "DECLARED_SCOPE_CHANGED", "UNHANDLED_FAILURE"})
        self.assertEqual(row["metrics"]["extra_step_attempts"], 2)
        broken = sample(1060, evidence_complete=False, diff_error="DIFF_UNAVAILABLE")
        row = watchdog.reduce_sample(row, broken)
        alert = next(a for a in row["alerts"] if a["code"] == "DECLARED_SCOPE_CHANGED")
        self.assertEqual(alert["state"], "active")
        row = watchdog.reduce_sample(row, sample(1120))
        self.assertTrue(all(a["state"] == "resolved" for a in row["alerts"]))

    def test_unchanged_diff_alone_has_no_alert_and_wait_gaps_are_excluded(self):
        row = watchdog.reduce_sample(None, sample(status="needs_input"))
        row = watchdog.reduce_sample(row, sample(1060, status="needs_input"))
        self.assertEqual(row["metrics"]["observed_wait_seconds"], 60)
        row = watchdog.reduce_sample(row, sample(2000, status="needs_input"))
        self.assertEqual(row["metrics"]["observed_wait_seconds"], 60)
        self.assertEqual(row["alerts"], [])
        self.assertEqual(row["last_progress_at"], 1000)
        self.assertEqual(row["metrics"]["efficiency_assessment"], "insufficient_baseline")
        self.assertIsNone(row["metrics"]["human_work_seconds"])

    def test_baseline_failure_is_visible_without_historical_push(self):
        row = watchdog.reduce_sample(None, sample(status="blocked", baseline=True))
        self.assertEqual(row["alerts"][0]["notification"]["state"], "baseline_only")
        row = watchdog.reduce_sample(None, sample(status="blocked", baseline=False))
        self.assertEqual(row["alerts"][0]["notification"]["state"], "pending")


class WatchdogRecords(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.store = TaskStore(root / "ato", native_home=root / "native", runtime_seal=SEAL)
        self.task = dict(revision=1, run_id=RUN, stop_epoch=0, creation=dict(runtime_seal=SEAL))
        self.enterContext(patch.object(self.store, "load_task", return_value=self.task))

    def test_status_read_does_not_initialize_and_failure_does_not_hide_task(self):
        self.assertEqual(watchdog.public_status(self.store, TASK)["state"], "not_observed")
        self.assertFalse(self.store.home.exists())
        with patch.object(watchdog, "read", side_effect=TaskError("STATE_CORRUPT")):
            self.assertEqual(watchdog.public_status(self.store, TASK)["state"], "unavailable")

    def test_private_record_round_trip_freshness_scope_and_redaction(self):
        record = watchdog.reduce_sample(None, sample(status="blocked"))
        record["alerts"][0]["notification"]["reservation"] = dict(body="secret-sentinel", sender="private-endpoint")
        watchdog.save(self.store, record)
        folder = self.store.root / "watchdog"
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()}
        result = watchdog.public_status(self.store, TASK, now=1060)
        self.assertEqual(result["state"], "observing")
        self.assertNotIn("secret-sentinel", str(result))
        self.assertNotIn("private-endpoint", str(result))
        self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in folder.iterdir()})
        self.assertEqual(watchdog.public_status(self.store, TASK, now=1300)["state"], "stale")
        self.task["revision"] = 2
        self.assertEqual(watchdog.public_status(self.store, TASK, now=1060)["state"], "awaiting_sample")
        self.task["creation"]["runtime_seal"] = "f" * 64
        self.assertEqual(watchdog.public_status(self.store, TASK)["state"], "awaiting_sample")

    def test_prior_release_tasks_keep_read_only_monitoring(self):
        self.store.runtime_seal = "f" * 64
        watchdog.save(self.store, watchdog.reduce_sample(None, sample()))
        self.assertEqual(watchdog.public_status(self.store, TASK, now=1060)["state"], "observing")

    def test_failed_first_sample_is_visible(self):
        watchdog.record_failure(self.store, TASK, "SAMPLE_UNAVAILABLE")
        result = watchdog.public_status(self.store, TASK)
        self.assertEqual(result["state"], "unavailable")
        self.assertEqual(result["sample_error"], "SAMPLE_UNAVAILABLE")

    def test_symlink_record_is_reported_unavailable(self):
        record = watchdog.reduce_sample(None, sample())
        watchdog.save(self.store, record)
        target = self.store.root / "watchdog" / (TASK + ".json")
        target.rename(target.with_suffix(".backup"))
        target.symlink_to(target.with_suffix(".backup"))
        self.assertEqual(watchdog.public_status(self.store, TASK)["state"], "unavailable")


class WatchdogSampling(unittest.TestCase):
    def test_timeout_kills_only_sampler_group_and_preserves_failure_diagnostic(self):
        host = SimpleNamespace(store=Mock())
        sampler = watchdog.Sampler(host, clock=lambda: 200)
        process = Mock(pid=123456)
        process.poll.return_value = None
        output = BytesIO()
        sampler.job = dict(process=process, output=output, task_id=TASK, started=0)
        with patch.object(watchdog.os, "killpg") as kill, patch.object(watchdog, "record_failure") as failed:
            self.assertEqual(sampler.poll(), TASK)
            kill.assert_called_once_with(123456, watchdog.signal.SIGKILL)
        self.assertIsNone(sampler.job)
        self.assertTrue(output.closed)
        failed.assert_called_once_with(host.store, TASK, "SAMPLE_UNAVAILABLE")
        self.assertEqual(sampler.last_error, "SAMPLE_UNAVAILABLE")
        self.assertEqual(sampler.due[TASK], 260)

    def test_due_queue_does_not_starve_second_task(self):
        second = "task-" + "e" * 32
        store = SimpleNamespace(home=Path("/tmp/fixture-ato"), native_home=Path("/tmp/fixture-native"),
            runtime_seal=SEAL, load_task=lambda _: dict(creation=dict(runtime_seal=SEAL)))
        host = SimpleNamespace(store=store, interaction=SimpleNamespace(_task_ids=lambda: [TASK, second]))
        sampler = watchdog.Sampler(host, clock=lambda: 100)
        sampler.due[TASK] = 160
        with patch.object(watchdog.subprocess, "Popen", return_value=Mock(pid=123456)) as spawn:
            sampler.poll()
            self.assertEqual(sampler.job["task_id"], second)
            self.assertIn("-IB", spawn.call_args.args[0])
            self.assertTrue(spawn.call_args.kwargs["start_new_session"])
            sampler.job["output"].close()
            sampler.job = None


class WatchdogDelivery(unittest.TestCase):
    def test_unknown_delivery_is_never_dispatched_again(self):
        record = watchdog.reduce_sample(None, sample(status="blocked"))
        record["alerts"][0]["notification"]["state"] = "delivery_unknown"
        host = SimpleNamespace(store=Mock(), interaction=Mock())
        host.interaction._task.return_value = ({}, {}, {})
        with patch.object(watchdog, "read", return_value=record), patch.object(watchdog, "save"), \
                patch.object(watchdog, "_deliver_alert") as deliver:
            watchdog.deliver(host, TASK, Mock())
            watchdog.deliver(host, TASK, Mock())
            deliver.assert_not_called()

    def test_agy_does_not_resolve_codex_route(self):
        record = watchdog.reduce_sample(None, sample(status="blocked"))
        host = Mock()
        host.store.submission_origin.return_value = SubmissionOrigin("/tmp/native", None, None, "agy")
        task = dict(task_id=TASK, revision=1, run_id=RUN, stop_epoch=0, spec_sha256="b" * 64,
                    creation=dict(runtime_seal=SEAL))
        with patch.object(watchdog, "_muted", return_value=False), patch.object(watchdog.time, "time", return_value=1000):
            watchdog._deliver_alert(host, task, dict(revision=1), record, record["alerts"][0], Mock())
        self.assertEqual(record["alerts"][0]["notification"]["state"], "unsupported")
        host.local_completion_route.assert_not_called()

    def test_muted_alert_has_visible_state_and_no_route(self):
        record = watchdog.reduce_sample(None, sample(status="blocked"))
        host = Mock()
        task = dict(task_id=TASK, revision=1, run_id=RUN, stop_epoch=0, spec_sha256="b" * 64,
                    creation=dict(runtime_seal=SEAL))
        with patch.object(watchdog, "_muted", return_value=True), patch.object(watchdog.time, "time", return_value=1000):
            watchdog._deliver_alert(host, task, dict(revision=1), record, record["alerts"][0], Mock())
        self.assertEqual(record["alerts"][0]["notification"]["state"], "muted")
        host.local_completion_route.assert_not_called()

    def test_resolved_queued_alert_is_cancelled(self):
        bus = Mock()
        bus.message.return_value = dict(id="msg-1", state="queued", external_possible=False)
        bus.cancel.return_value = dict(state="cancelled")
        notification = dict(message_id="msg-1", state="queued")
        watchdog._cancel_notification(bus, "sender", notification)
        self.assertEqual(notification["state"], "cancelled")
        bus.cancel.assert_called_once()

    def test_p0_1_reaped_child_is_never_killpg_and_permission_error_never_wedges(self):
        import subprocess
        import sys
        host = SimpleNamespace(store=Mock(), interaction=None)
        sampler = watchdog.Sampler(host, clock=lambda: 100)
        proc = subprocess.Popen([sys.executable, "-c", ""], stdout=subprocess.DEVNULL, start_new_session=True)
        while not watchdog._probe_exited(proc):
            pass
        calls = []
        original_wait = proc.wait
        def tracked_killpg(pid, sig):
            calls.append(("killpg", proc.returncode))
            raise PermissionError("simulated cross-uid pid reuse")
        def tracked_wait(*a, **kw):
            calls.append(("wait", proc.returncode))
            return original_wait(*a, **kw)
        output = BytesIO(b"not-json")
        sampler.job = dict(process=proc, output=output, task_id=TASK, started=90)
        with patch.object(watchdog.os, "killpg", side_effect=tracked_killpg), \
                patch.object(proc, "wait", side_effect=tracked_wait), \
                patch.object(watchdog, "record_failure"):
            sampler.poll()
        self.assertIsNone(sampler.job)
        self.assertTrue(output.closed)
        # killpg must only ever run while returncode is None (before wait reaps).
        self.assertEqual(calls, [("killpg", None), ("wait", None)])
        # close() must also survive PermissionError cleanly.
        fake = Mock(pid=999999)
        fake_out = BytesIO()
        sampler.job = dict(process=fake, output=fake_out, task_id=TASK, started=90)
        with patch.object(watchdog.os, "killpg", side_effect=PermissionError("denied")):
            sampler.close()
        self.assertIsNone(sampler.job)
        self.assertTrue(fake_out.closed)

    def test_p0_2_and_p1_2_shared_observer_lease_and_generation_advance(self):
        from ato_task import ato_observer
        from ato_workflow.message_bus import MessageBus
        from ato_workflow import transport
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        store = TaskStore(root / "ato", native_home=root / "native", runtime_seal=SEAL)
        bus = MessageBus(root / "mailbox.sqlite", clock=lambda: 1010)
        sender = bus.register_endpoint("endpoint-sender", kind="local_cli", session_id="sender", project=str(root),
                                       source="fixture")
        target = bus.register_endpoint("endpoint-target", kind="codex", session_id="target", project=str(root),
                                       source="fixture")
        # Simulate an active business _observe() lease held on the same target endpoint.
        fake_host = SimpleNamespace(store=store, folder=store.root / "host")
        biz_msg = dict(id="msg-biz", claim_owner=None)
        lease_token = ato_observer._owner(fake_host, bus, target, biz_msg, queued=True)
        ep_before = bus.authenticate(target)
        # Watchdog dispatches two alerts across advancing generations using dedicated namespace.
        dispatched = []
        class DummyAdapter:
            capabilities = {"native_queue_serialization"}

            def send(self, endpoint, message):
                dispatched.append(message["id"])
                return transport.DeliveryResult("delivered")
        codex_origin = SubmissionOrigin(str(root), "11111111-1111-1111-1111-111111111111", str(root), "codex")
        with patch.object(store, "submission_origin", return_value=codex_origin):
            for gen_idx in (1, 2):
                rec = watchdog.reduce_sample(None, sample(1000 + gen_idx, status="blocked", generation=[1.0, gen_idx]))
                task = dict(task_id=TASK, revision=1, run_id=RUN, stop_epoch=0, spec_sha256="b" * 64,
                            worktree_removed=False, creation=dict(runtime_seal=SEAL))
                route = SimpleNamespace(sender=sender, target=target, source_ref="local:fixture", local_completion=True)
                host = Mock(store=store, folder=store.root / "host")
                host.mailbox.return_value = bus
                host.local_completion_route.return_value = route
                host.interaction._task.return_value = (task, {}, dict(revision=1))
                host.interaction._scope_record.return_value = object()
                host.interaction._kernel.return_value = ([1.0, gen_idx], True)
                def do_dispatch(h, b, r, msg_id, guard):
                    msg = b.message(msg_id)
                    ato_observer._dispatch_selected(h, b, r, b.authenticate(r.target), DummyAdapter(), msg, guard)
                with patch.object(watchdog, "_muted", return_value=False), \
                        patch.object(watchdog.time, "time", return_value=1010 + gen_idx), \
                        patch.object(watchdog, "save"):
                    watchdog._deliver_alert(host, task, dict(revision=1), rec, rec["alerts"][0], do_dispatch)
                self.assertEqual(rec["alerts"][0]["notification"]["state"], "delivered")
            self.assertEqual(len(dispatched), 2)
            # Endpoint generation must not have rotated and original lease token must remain valid.
            ep_after = bus.authenticate(target)
            self.assertEqual(ep_after["generation"], ep_before["generation"])
            bus.renew_endpoint(target, lease_token, lease_seconds=60)
            # None generation must mark unavailable without writing null generation to bindings.
            rec_none = watchdog.reduce_sample(None, sample(status="blocked", generation=None))
            task = dict(task_id=TASK, revision=1, run_id=RUN, stop_epoch=0, spec_sha256="b" * 64,
                        creation=dict(runtime_seal=SEAL))
            host = Mock(store=store)
            host.mailbox.return_value = bus
            with patch.object(watchdog, "_muted", return_value=False), \
                    patch.object(watchdog.time, "time", return_value=1010):
                watchdog._deliver_alert(host, task, dict(revision=1), rec_none, rec_none["alerts"][0], Mock())
            self.assertEqual(rec_none["alerts"][0]["notification"]["state"], "unavailable")

    def test_p1_1_malformed_nested_record_rejected_and_record_failure_recovers(self):
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        store = TaskStore(root / "ato", native_home=root / "native", runtime_seal=SEAL)
        task = dict(revision=1, run_id=RUN, stop_epoch=0, creation=dict(runtime_seal=SEAL))
        for bad in (
            dict(version=1, task_id=TASK, alerts=[], scope=None, sampled_at=1000),
            dict(version=1, task_id=TASK, alerts=[dict(notification=None)], sampled_at=1000,
                 scope=dict(run_id=RUN, stop_epoch=0, revision=1, runtime_seal=SEAL)),
            dict(version=1, task_id=TASK, alerts=[], pending={"x": "bad"}),
        ):
            watchdog.save(store, bad)
            with self.assertRaises(TaskError) as caught:
                watchdog.read(store, TASK)
            self.assertEqual(caught.exception.code, "STATE_CORRUPT")
            with patch.object(store, "load_task", return_value=task):
                self.assertEqual(watchdog.public_status(store, TASK)["state"], "unavailable")
            watchdog.record_failure(store, TASK, "SAMPLE_UNAVAILABLE")
            recovered = watchdog.read(store, TASK)
            self.assertEqual(recovered["sample_error"], "SAMPLE_UNAVAILABLE")

    def test_p1_3_sampler_skips_unchanged_terminal_tasks_and_prioritizes_active(self):
        root = Path(self.enterContext(TemporaryDirectory())).resolve()
        store = TaskStore(root / "ato", native_home=root / "native", runtime_seal=SEAL)
        active = "task-" + "1" * 32
        terminals = [f"task-{i:032x}" for i in range(2, 52)]
        tasks_map = {t: dict(run_id=RUN, stop_epoch=0, revision=1, creation=dict(runtime_seal=SEAL))
                     for t in [active, *terminals]}
        for tid in terminals:
            rec = watchdog.reduce_sample(None, sample(task_id=tid, status="completed"))
            watchdog.save(store, rec)
        store.load_task = lambda tid: tasks_map[tid]
        host = SimpleNamespace(store=store, interaction=SimpleNamespace(_task_ids=lambda: [*terminals, active]))
        sampler = watchdog.Sampler(host, clock=lambda: 100)
        with patch.object(watchdog.subprocess, "Popen", return_value=Mock(pid=123456)):
            sampler.poll()
            self.assertEqual(sampler.job["task_id"], active)
            sampler.job["output"].close()
            sampler.job = None

    def test_p1_4_diff_error_and_transient_revision_or_clock_step_do_not_cancel_alerts(self):
        rec = watchdog.reduce_sample(None, sample(status="blocked", diff_error="DIFF_UNAVAILABLE", evidence_complete=False))
        self.assertIsNone(rec["sample_error"])
        self.assertEqual(rec["diff_error"], "DIFF_UNAVAILABLE")
        # Revision bump between sample and deliver defers rather than cancelling.
        host = Mock()
        host.mailbox.return_value = Mock()
        task = dict(task_id=TASK, revision=2, run_id=RUN, stop_epoch=0, spec_sha256="b" * 64,
                    creation=dict(runtime_seal=SEAL))
        with patch.object(watchdog, "_muted", return_value=False), \
                patch.object(watchdog.time, "time", return_value=1010):
            watchdog._deliver_alert(host, task, dict(revision=1), rec, rec["alerts"][0], Mock())
        self.assertEqual(rec["alerts"][0]["notification"]["state"], "pending")
        # Slight backward NTP step also defers rather than cancelling.
        task["revision"] = 1
        with patch.object(watchdog, "_muted", return_value=False), \
                patch.object(watchdog.time, "time", return_value=999.9):
            watchdog._deliver_alert(host, task, dict(revision=1), rec, rec["alerts"][0], Mock())
        self.assertEqual(rec["alerts"][0]["notification"]["state"], "pending")

    def test_p1_6_prior_release_broken_task_does_not_fail_observer_health(self):
        from ato_task import ato_observer
        old_task = "task-" + "c" * 32
        cur_task = "task-" + "d" * 32
        records = {
            old_task: dict(creation=dict(runtime_seal="0" * 64)),
            cur_task: dict(creation=dict(runtime_seal=SEAL)),
        }
        host = SimpleNamespace(store=SimpleNamespace(load_task=lambda tid: records[tid]))
        self.assertFalse(ato_observer._counts_for_health(host, SEAL, {old_task: "BINDING_CHANGED"}))
        self.assertTrue(ato_observer._counts_for_health(host, SEAL, {cur_task: "BINDING_CHANGED"}))
        self.assertTrue(ato_observer._counts_for_health(host, SEAL, None))
