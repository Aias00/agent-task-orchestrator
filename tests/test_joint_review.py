"""Offline joint-review acceptance cases. Providers are always synthetic.

Implementation-stage additions: intentionally not executed until user approval.
"""
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ato import executor, joint_review, process
from ato.adapters.base import ReviewResult, WorkerResult
from ato.spec import TaskError, canonical_json, normalize_spec, sha256_bytes
from ato.store import Store, _DDL, SCHEMA_VERSION
from test_core_and_store import init_repo, make_spec
from test_execution_and_cli import run_cli


def panel(count=2, **changes):
    return {"members": [{"id": f"member-{i}", "agent": {"kind": "codex"}, "timeout_seconds": 30,
                          "checklist": [f"Private role {i}"]} for i in range(count)], **changes}


def finding(identity="F1", **changes):
    return {"id": identity, "severity": "blocker", "file": "app.py", "lines": "L1",
            "category": "correctness", "title": "Fix condition", "evidence": "Wrong branch",
            "required_fix": "Handle the missing case", **changes}


def verdict(decision="approve", findings=None, **changes):
    return {"verdict": decision, "summary": decision, "findings": findings or [],
            "checked_items": ["Objective"], "residual_risks": [], **changes}


def result(decision="approve", findings=None, **kwargs):
    return ReviewResult(True, "OK", verdict(decision, findings), **kwargs)


class JointFixture(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(TemporaryDirectory(prefix="ato-joint-"))).resolve()
        self.enterContext(patch.dict(os.environ, {"ATO_HOME": str(self.base / "ato"),
                                                "CODEX_HOME": str(self.base / "codex"),
                                                "ATO_SUBMISSION_KIND": "cli"}))
        self.enterContext(patch("ato.reviewer.get_reviewer_runner", side_effect=AssertionError("Provider forbidden")))
        self.repo = init_repo(self.base / "repo")
        self.store = Store()
        self.turns = []

    def specification(self, cfg=None):
        return make_spec(self.repo, self.base / "spec.json", reviewer=panel() if cfg is None else cfg,
                         worker_contract={"objective": "Synthetic joint review", "scope": "app.py",
                             "authorization": "Local fixtures only", "max_attempts": 1,
                             "acceptance": [{"id": "manual", "kind": "manual", "description": "Human acceptance"}]})

    def worker(self, req):
        self.turns.append(req)
        (req.worktree / "app.py").write_text(f"# worker turn {len(self.turns)}\n")
        return WorkerResult("completed", "Synthetic change")

    def submit(self, runner, cfg=None):
        return executor.submit(self.store, self.repo, self.specification(cfg), foreground=True,
                               worker_runner=self.worker, reviewer_runner=runner)


class JointConfigurationTests(JointFixture):
    def test_two_to_eight_same_kind_and_timeout_provenance(self):
        for count in (2, 8):
            cfg = panel(count)
            del cfg["members"][0]["timeout_seconds"]
            norm = normalize_spec(self.repo, self.specification(cfg))
            effective = norm.preview()["reviewer"]
            self.assertEqual(effective["execution"], "sequential")
            self.assertEqual(effective["policy"], "all")
            self.assertEqual(effective["members"][0]["timeout_seconds"], 300)
            self.assertEqual(effective["members"][0]["timeout_source"], "default")
            self.assertEqual(effective["members"][1]["timeout_source"], "spec")

    def test_rejects_mixed_shapes_ids_counts_and_nested_invalid_values(self):
        bad = [panel(1), panel(9), panel(policy="majority"), panel(agent={"kind": "codex"}),
               panel(timeout_seconds=60), panel(max_review_rounds=True), panel(max_review_rounds=51)]
        for change in ({"id": "../x"}, {"id": "member-1"}, {"timeout_seconds": True},
                       {"max_review_rounds": 2}, {"design_docs": []}, {"agent": {"kind": "cursor"}},
                       {"agent": {"kind": "codex_acpx", "model": "x"}},
                       {"agent": {"kind": "codex_acpx"}, "session_mode": "require_resume"},
                       {"allowed_kinds": ["agy"]}):
            cfg = panel()
            cfg["members"][0].update(change)
            bad.append(cfg)
        for cfg in bad:
            with self.subTest(cfg=cfg), self.assertRaises(TaskError):
                normalize_spec(self.repo, self.specification(cfg))

    def test_legacy_normalized_document_and_digest_unchanged(self):
        cfg = {"agent": {"kind": "codex"}}
        norm = normalize_spec(self.repo, self.specification(cfg))
        expected = {"agent": {"kind": "codex"}, "allowed_kinds": ["codex"], "session_mode": "fresh_only",
                    "max_review_rounds": 10, "timeout_seconds": 300, "checklist": [], "design_docs": []}
        self.assertEqual(norm.document["reviewer"], expected)
        self.assertEqual(norm.spec_sha256, sha256_bytes(canonical_json({**norm.document, "reviewer": expected})))
        norm2 = normalize_spec(self.repo, self.specification({**cfg, "timeout_seconds": 300}))
        self.assertEqual(norm.spec_sha256, norm2.spec_sha256)
        self.assertNotIn("reviewer", norm.preview())


class JointAggregationTests(unittest.TestCase):
    def members(self, receipts):
        return [{"member_id": f"member-{i}", "state": "completed", "verdict": v}
                for i, v in enumerate(receipts)]

    def test_priority_and_exact_dedup_keep_sources(self):
        one = finding()
        same = finding("renamed", title=" Fix   condition ")
        conflict = finding(required_fix="Keep the original branch")
        receipts = self.members([verdict("request_changes", [one]), verdict("request_changes", [same, {**conflict, "id": "F2"}])])
        merged = joint_review.aggregate(receipts)
        self.assertEqual(merged["verdict"], "request_changes")
        self.assertEqual(len(merged["findings"]), 2)
        self.assertEqual(merged["source_finding_count"], 3)
        self.assertEqual(merged["findings"][0]["sources"], [
            {"member_id": "member-0", "finding_id": "F1"}, {"member_id": "member-1", "finding_id": "renamed"}])
        self.assertEqual(receipts[0]["verdict"]["findings"][0], one)
        self.assertEqual(joint_review.aggregate(self.members([verdict("blocked"), verdict("request_changes", [one])]))["verdict"], "blocked")
        self.assertEqual(joint_review.aggregate(self.members([verdict(), verdict()]))["verdict"], "approve")

    def test_file_spacing_and_required_fix_indentation_are_not_deduplicated(self):
        receipts = self.members([
            verdict("request_changes", [finding(file="a  b.py", required_fix="  return x")]),
            verdict("request_changes", [finding(file="a b.py", required_fix=" return x")]),
        ])
        self.assertEqual(len(joint_review.aggregate(receipts)["findings"]), 2)
        receipts[1]["verdict"]["findings"][0]["file"] = "a  b.py"
        self.assertEqual(len(joint_review.aggregate(receipts)["findings"]), 2)

    def test_all_1024_findings_survive_and_ids_are_stable(self):
        members = self.members([verdict("request_changes", [finding(f"F{i}", evidence=f"member {m} fact {i}")
                               for i in range(128)]) for m in range(8)])
        merged = joint_review.aggregate(members)
        self.assertEqual(len(merged["findings"]), 1024)
        self.assertEqual(len({f["id"] for f in merged["findings"]}), 1024)
        self.assertEqual(joint_review.aggregate(members), merged)
        members[-1]["state"] = "pending"
        with self.assertRaises(TaskError):
            joint_review.aggregate(members)


class JointExecutionTests(JointFixture):
    def test_rework_runs_entire_panel_and_keeps_round_session_identity(self):
        calls = []
        def review(req):
            calls.append(req)
            self.assertIn("Common obligation", req.prompt)
            self.assertIn("Private role " + req.member_id[-1], req.prompt)
            self.assertNotIn("Private role " + str(1 - int(req.member_id[-1])), req.prompt)
            self.assertEqual(req.resume_session_id, None if req.round_num == 1 else req.member_id + "-session")
            if req.round_num == 1 and req.member_id == "member-0":
                return result("request_changes", [finding(evidence="first-member-secret-finding")], session_id=req.member_id + "-session")
            if req.round_num == 1:
                self.assertNotIn("first-member-secret-finding", req.prompt)
            else:
                self.assertIn("first-member-secret-finding", req.prompt)
            return result(session_id=req.member_id + "-session")
        cfg = panel(checklist=["Common obligation"])
        for member in cfg["members"]:
            member["session_mode"] = "resume_if_supported"
        view = self.submit(review, cfg)
        self.assertEqual(view["status"], "completed", view)
        self.assertEqual([(r.round_num, r.member_id) for r in calls], [(1, "member-0"), (1, "member-1"), (2, "member-0"), (2, "member-1")])
        self.assertEqual(len({r.runtime_dir for r in calls}), 4)
        self.assertEqual(len(self.turns), 2)
        self.assertIn("first-member-secret-finding", self.turns[1].prompt)
        self.assertEqual(view["stage"]["execution_failures"], 0)
        self.assertEqual(view["review_panel"]["verdict"], "approve")
        self.assertIsNone(view["review"])
        self.assertEqual(view["acceptance"]["disposition"], "not_recorded")
        self.assertEqual(joint_review.reviews(self.store, view["task_id"])["total"], 2)

    def test_blocked_collects_remaining_members_without_worker_repair(self):
        calls = []
        def review(req):
            calls.append(req.member_id)
            return result("blocked") if req.member_id == "member-0" else result("request_changes", [finding()])
        view = self.submit(review)
        self.assertEqual(calls, ["member-0", "member-1"])
        self.assertEqual(len(self.turns), 1)
        self.assertEqual(view["reason"], "REVIEW_BLOCKED")
        self.assertEqual(view["review_panel"]["state"], "completed")
        self.assertEqual(view["review_panel"]["verdict"], "blocked")

    def test_errors_stop_next_member_and_preserve_partial_evidence(self):
        for failure in ("TIMEOUT", "INVALID_PROTOCOL", "ACPX_DELIVERY_UNKNOWN"):
            calls = []
            def review(req):
                calls.append(req.member_id)
                return ReviewResult(False, failure) if req.member_id == "member-1" else result()
            view = self.submit(review, panel(3))
            self.assertEqual(view["status"], "blocked", view)
            self.assertEqual(view["reason"], failure)
            self.assertNotIn("member-2", calls)
            group = self.store.review_group(view["task_id"])
            self.assertEqual([m["state"] for m in group["members"]], ["completed", "error", "not_run"])
            self.assertIsNone(self.store.latest_review(view["task_id"]))
            self.assertIsNone(view["review_panel"]["verdict"])

    def test_mutation_and_stop_revoke_approval(self):
        for mode in ("mutation", "stop"):
            calls = []
            def review(req):
                calls.append(req.member_id)
                if mode == "mutation":
                    (req.worktree / "app.py").write_text("rogue write")
                else:
                    self.store.update_task(req.task_id, stop_requested=True)
                return result()
            view = self.submit(review)
            self.assertEqual(calls, ["member-0"])
            self.assertEqual(view["reason"], "MUTATION_VIOLATION" if mode == "mutation" else "STOPPED")
            self.assertFalse(view["review_panel"]["complete"])
            self.assertIsNone(self.store.latest_review(view["task_id"]))

    def test_format_retry_is_member_local_and_session_collision_blocks(self):
        calls = []
        def retry(req):
            calls.append(req)
            if len(calls) == 1:
                return ReviewResult(False, "INVALID_PROTOCOL", session_id="own-session", detail="Missing verdict")
            if req.member_id == "member-0":
                self.assertIn("Missing verdict", req.prompt)
            else:
                self.assertNotIn("Missing verdict", req.prompt)
            return result(session_id="same-session")
        view = self.submit(retry)
        self.assertEqual(view["reason"], "SESSION_MISMATCH")
        self.assertEqual(len({r.runtime_dir for r in calls}), 3)
        self.assertEqual([r.resume_session_id for r in calls], [None, None, None])

    def test_large_feedback_pauses_without_loss_or_second_worker(self):
        # Each field is within the wire maximum while the combined feedback is too large.
        def bounded(req):
            return result("request_changes", [finding(f"F{i}", evidence=req.member_id + "x" * 1000, title=f"Issue {i}") for i in range(128)])
        view = self.submit(bounded)
        self.assertEqual(view["reason"], "REVIEW_FEEDBACK_TOO_LARGE", view)
        self.assertEqual(len(self.turns), 1)
        group = self.store.review_group(view["task_id"])
        self.assertEqual(len(group["verdict"]["findings"]), 256)
        cursor, collected = 0, []
        while cursor is not None:
            page = joint_review.reviews(self.store, view["task_id"], group_id=group["id"], section="findings", cursor=cursor, limit=7)
            collected.extend(page["items"])
            cursor = page["next_cursor"]
        self.assertEqual(len(collected), 256)
        self.assertEqual({f["id"] for f in collected}, {f["id"] for f in group["verdict"]["findings"]})

    def test_crash_history_not_approved_and_resume_restarts_all_members(self):
        def crash(req):
            if req.member_id == "member-1":
                raise SystemExit("synthetic process death")
            return result()
        with self.assertRaises(SystemExit):
            self.submit(crash)
        task = self.store.list_tasks()[0]
        self.store.update_run(task["active_run_id"], pid=999999, pid_identity="not-live")
        view = executor.task_status_view(self.store, task["id"])
        self.assertEqual(view["review_panel"]["state"], "incomplete")
        self.assertIsNone(view["review_panel"]["verdict"])
        history = joint_review.reviews(self.store, task["id"])
        self.assertEqual(history["items"][0]["state"], "incomplete")
        self.store.update_task(task["id"], status="blocked", reason="OWNER_NOT_LIVE")
        calls = []
        def fresh(req):
            calls.append(req.member_id)
            self.assertIsNone(req.resume_session_id)
            return result()
        done = executor.resume(self.store, task["id"], foreground=True, worker_runner=self.worker, reviewer_runner=fresh)
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(calls, ["member-0", "member-1"])
        self.assertEqual(joint_review.reviews(self.store, task["id"])["total"], 2)

    def test_partial_recheck_cannot_reuse_prior_approved_group(self):
        first = self.submit(lambda req: result())
        tid = first["task_id"]
        def crash(req):
            if req.member_id == "member-1":
                raise SystemExit("crash after one approval")
            return result()
        with self.assertRaises(SystemExit):
            executor.recheck(self.store, tid, reviewer_runner=crash)
        task = self.store.get_task(tid)
        self.store.update_run(task["active_run_id"], phase="settled", outcome="failed")
        self.assertEqual(self.store.latest_review(tid)["verdict"], "approve")
        view = executor.task_status_view(self.store, tid)
        self.assertIsNone(view["review"])
        self.assertFalse(view["review_panel"]["complete"])
        self.assertEqual(executor.recheck(self.store, tid, checks_only=True)["reason"], "REVIEW_REQUIRED")

    def test_stop_between_members_prevents_next_launch(self):
        save = self.store.save_review_member
        calls = []
        def stop_after_save(task_id, group_id, member_id, receipt):
            saved = save(task_id, group_id, member_id, receipt)
            self.store.update_task(task_id, stop_requested=True)
            return saved
        def review(req):
            calls.append(req.member_id)
            return result()
        with patch.object(self.store, "save_review_member", side_effect=stop_after_save):
            view = self.submit(review)
        self.assertEqual(calls, ["member-0"])
        self.assertEqual(view["status"], "stopped")
        group = self.store.review_group(view["task_id"])
        self.assertEqual([m["state"] for m in group["members"]], ["completed", "not_run"])
        self.assertIsNone(group["verdict"])

    def test_revision_fences_edit_and_revalidates_full_member_configuration(self):
        view = self.submit(lambda req: result())
        task = self.store.get_task(view["task_id"])
        cfg = deepcopy(task["reviewer"])
        self.store.update_task(task["id"], next_step="revision bumped")
        with self.assertRaises(TaskError) as error:
            self.store.set_reviewer(task["id"], cfg, expected_revision=task["revision"])
        self.assertEqual(error.exception.code, "BUSY")
        code, response = run_cli(["agent", "reviewer", task["id"], "--reviewer-id", "member-0",
                                  "--kind", "codex_acpx", "--model", "not-allowed"], store=self.store)
        self.assertEqual(response["error"]["code"], "INVALID_SPEC")
        self.assertEqual(self.store.get_task(task["id"])["reviewer"], cfg)

    def test_owner_fence_and_config_edit_during_review(self):
        def review(req):
            code, response = run_cli(["agent", "reviewer", req.task_id, "--reviewer-id", "member-0", "--kind", "agy"], store=self.store)
            self.assertEqual(response["error"]["code"], "BUSY")
            self.store.update_run(req.run_id, pid_identity="stolen-owner")
            return result()
        view = self.submit(review)
        self.assertNotEqual(view["status"], "completed")
        self.assertIsNone(self.store.latest_review(view["task_id"]))

    def test_recheck_checks_only_config_product_and_completed_run_gates(self):
        view = self.submit(lambda req: result())
        tid = view["task_id"]
        checked = executor.recheck(self.store, tid, checks_only=True)
        self.assertEqual(checked["status"], "completed", checked)
        group = self.store.review_group(tid)
        code, err = run_cli(["agent", "reviewer", tid, "--kind", "agy"], store=self.store)
        self.assertEqual(err["error"]["code"], "USAGE")
        code, changed = run_cli(["agent", "reviewer", tid, "--reviewer-id", "member-0", "--kind", "codex", "--model", "new-model"], store=self.store)
        self.assertEqual(code, 0, changed)
        self.assertEqual(self.store.get_task(tid)["reason"], "REVIEW_REQUIRED")
        self.assertEqual(executor.recheck(self.store, tid, checks_only=True)["reason"], "REVIEW_REQUIRED")
        calls = []
        def review(req):
            calls.append(req.member_id)
            return result()
        done = executor.recheck(self.store, tid, reviewer_runner=review)
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(calls, ["member-0", "member-1"])
        group = self.store.review_group(tid)
        self.store.update_run(group["run_id"], outcome="blocked")
        self.assertEqual(executor.recheck(self.store, tid, checks_only=True)["reason"], "REVIEW_REQUIRED")
        executor.recheck(self.store, tid, reviewer_runner=review)
        (Path(self.store.get_task(tid)["worktree_path"]) / "app.py").write_text("changed later")
        self.assertEqual(executor.recheck(self.store, tid, checks_only=True)["reason"], "REVIEW_REQUIRED")

    def test_frozen_design_documents_and_public_redaction(self):
        def review(req):
            self.assertIn("Frozen design document README.md:\n# Demo", req.prompt)
            return result("request_changes", [finding(evidence=str(req.runtime_dir) + " bearer ABCDEFGHIJKLMNOPQRST")], session_id=req.member_id + "-private-session")
        cfg = panel(design_docs=["README.md"], max_review_rounds=1)
        view = self.submit(review, cfg)
        group = self.store.review_group(view["task_id"])
        page = joint_review.reviews(self.store, view["task_id"], group_id=group["id"], member_id="member-0", section="findings")
        encoded = json.dumps(page)
        self.assertNotIn(str(self.base), encoded)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRST", encoded)
        self.assertNotIn("private-session", encoded)


class JointACPTests(JointFixture):
    def test_two_acp_members_share_real_round_but_not_requests(self):
        cfg = panel()
        for member in cfg["members"]:
            member["agent"] = {"kind": "codex_acpx"}
        def review(req):
            key = "synthetic-" + req.member_id
            self.assertTrue(self.store.begin_acpx_review(req.task_id, req.run_id, req.round_num, key,
                             {"pid": 999999, "birth": "fixture"}, member_id=req.member_id))
            self.store.record_acpx_receipt(key, {"delivery": "finished"})
            self.store.record_acpx_cleanup(key, "no_process", bridge_closed=True)
            return result()
        view = self.submit(review, cfg)
        self.assertEqual(view["status"], "completed", view)
        with self.store.tx(write=False) as con:
            rows = con.execute("SELECT round, member_id, request_id FROM acpx_reviews ORDER BY rowid").fetchall()
        self.assertEqual([r["round"] for r in rows], [1, 1])
        self.assertEqual(len({r["member_id"] for r in rows}), 2)
        self.assertEqual(len({r["request_id"] for r in rows}), 2)
        self.assertEqual(view["acpx_reviewer"]["member_id"], "member-1")

    def test_unknown_acp_outcome_blocks_next_member_resume_and_edit(self):
        cfg = panel()
        cfg["members"][0]["agent"] = {"kind": "codex_acpx"}
        calls = []
        def review(req):
            calls.append(req.member_id)
            self.assertTrue(self.store.begin_acpx_review(req.task_id, req.run_id, req.round_num, "unknown-request",
                             {"pid": 999999, "birth": "fixture"}, member_id=req.member_id))
            self.store.record_acpx_receipt("unknown-request", {"delivery": "unknown"})
            self.store.record_acpx_cleanup("unknown-request", "no_process", bridge_closed=True)
            return ReviewResult(False, "ACPX_DELIVERY_UNKNOWN")
        view = self.submit(review, cfg)
        self.assertEqual(calls, ["member-0"])
        self.assertEqual(view["reason"], "ACPX_DELIVERY_UNKNOWN")
        with self.assertRaises(TaskError):
            executor.resume(self.store, view["task_id"], foreground=True, worker_runner=self.worker, reviewer_runner=review)
        code, err = run_cli(["agent", "reviewer", view["task_id"], "--reviewer-id", "member-0", "--kind", "codex"], store=self.store)
        self.assertEqual(err["error"]["code"], "ACPX_DELIVERY_UNKNOWN")
        self.assertEqual(calls, ["member-0"])

    def test_old_acp_unique_key_migration_is_lossless_and_idempotent(self):
        db = self.base / "old.db"
        ddl = _DDL.replace("  member_id TEXT NOT NULL DEFAULT '',\n  UNIQUE (run_id, round, member_id)", "  UNIQUE (run_id, round)")
        start, end = ddl.index("CREATE TABLE IF NOT EXISTS review_groups"), ddl.index("CREATE TABLE IF NOT EXISTS acpx_reviews")
        ddl = ddl[:start] + ddl[end:]
        tid, rid = "task-" + "a" * 32, "run-" + "b" * 32
        with sqlite3.connect(db) as con:
            con.executescript(ddl)
            con.execute("""INSERT INTO tasks(id, repo_root, repo_key, base_commit, branch, spec_json,
                           spec_sha256, status, created_at, updated_at) VALUES (?, '/repo', '/git', 'base', 'branch', '{}', 'sha', 'blocked', 1, 1)""", (tid,))
            con.execute("INSERT INTO runs(id, task_id, kind, phase, created_at) VALUES (?, ?, 'initial', 'settled', 1)", (rid, tid))
            con.execute("""INSERT INTO acpx_reviews(request_id, task_id, run_id, round, bridge_json, dispatch_json,
                           receipt_json, cleanup, bridge_closed) VALUES ('historical', ?, ?, 1, '{}', '{}', '{"delivery":"unknown"}', 'verified_gone', 1)""", (tid, rid))
            before = con.execute("SELECT * FROM acpx_reviews").fetchone()
        for _ in range(2):
            store = Store(db)
            with store.tx(write=False) as con:
                row = con.execute("SELECT * FROM acpx_reviews").fetchone()
                self.assertEqual(tuple(row)[:-1], before)
                self.assertEqual(row["member_id"], "")
                self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertIsNotNone(store.acpx_review(tid, unresolved=True))
        with store.tx() as con:
            for mid in ("one", "two"):
                con.execute("INSERT INTO acpx_reviews(request_id, task_id, run_id, round, bridge_json, member_id) VALUES (?, ?, ?, 1, '{}', ?)", (mid, tid, rid, mid))
