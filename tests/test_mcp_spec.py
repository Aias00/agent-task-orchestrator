"""MCP new-intent policy and historical replay compatibility; no providers."""
from copy import deepcopy
from dataclasses import replace
import json
import re
import unittest
from unittest.mock import patch

from ato.server.http import openapi
from ato.server.service import MCP_OPERATIONS, OPERATIONS, Service
from ato.spec import (
    DEFAULT_REVIEWER_TIMEOUT_SECONDS, DEFAULT_WORKER_TIMEOUT_SECONDS,
    MAX_REVIEWER_TIMEOUT_SECONDS, MAX_WORKER_TIMEOUT_SECONDS,
    MIN_REVIEWER_TIMEOUT_SECONDS, MIN_WORKER_TIMEOUT_SECONDS,
    REVIEWER_KINDS, SHARED_ENV_LINKS, WORKER_KINDS, WORKER_SANDBOXES,
)
from server_fixtures import BOT_TOKEN, ServerFixture


class SchemaTests(unittest.TestCase):
    def test_complete_nested_schema_and_rest_defaults_are_distinct(self):
        for operation in ("validate", "submit"):
            shape = MCP_OPERATIONS[operation][1]["properties"]["spec"]
            self.assertEqual(set(shape["required"]), {"schema_version", "repo", "base_ref", "worker_contract"})
            self.assertFalse(shape["additionalProperties"])
            props = shape["properties"]
            self.assertEqual(set(props), {"schema_version", "repo", "base_ref", "worker_contract", "reviewer", "worktree", "guardrails"})
            self.assertEqual(props["schema_version"]["const"], 1)
            self.assertIsNone(re.fullmatch(props["repo"]["pattern"], "/tmp/repo"))
            worker = props["worker_contract"]
            self.assertEqual(set(worker["required"]), {"objective", "scope", "authorization", "acceptance", "timeout_seconds"})
            self.assertFalse(worker["additionalProperties"])
            fields = worker["properties"]
            self.assertEqual(set(fields), {"objective", "scope", "authorization", "acceptance", "agent", "model",
                                           "sandbox", "permissions", "max_attempts", "max_no_progress_rounds", "timeout_seconds", "memory"})
            self.assertEqual(fields["agent"]["properties"]["kind"]["enum"], list(WORKER_KINDS))
            self.assertEqual(fields["sandbox"]["enum"], list(WORKER_SANDBOXES))
            self.assertEqual(fields["sandbox"]["default"], "workspace-write")
            self.assertEqual(fields["permissions"]["properties"]["writable_roots"]["maxItems"], 0)
            acceptance = fields["acceptance"]
            self.assertEqual((acceptance["minItems"], acceptance["maxItems"]), (1, 64))
            self.assertEqual(acceptance["items"]["allOf"][0]["then"]["required"], ["checks"])
            checks = acceptance["items"]["properties"]["checks"]["anyOf"][0]
            self.assertEqual((checks["maxItems"], checks["items"]["maxItems"]), (32, 128))
            reviewer = props["reviewer"]["oneOf"][0]
            self.assertEqual(set(reviewer["required"]), {"agent", "timeout_seconds"})
            self.assertIn("null", reviewer["type"])
            self.assertEqual(reviewer["properties"]["agent"]["properties"]["kind"]["enum"], list(REVIEWER_KINDS))
            self.assertEqual(props["worktree"]["properties"]["shared_env_links"]["items"]["enum"], sorted(SHARED_ENV_LINKS))
            self.assertEqual(set(props["guardrails"]["properties"]), {"protected_paths", "forbid_delete_paths"})
            for name, minimum, maximum, default in (
                    ("worker_contract", MIN_WORKER_TIMEOUT_SECONDS, MAX_WORKER_TIMEOUT_SECONDS, DEFAULT_WORKER_TIMEOUT_SECONDS),
                    ("reviewer", MIN_REVIEWER_TIMEOUT_SECONDS, MAX_REVIEWER_TIMEOUT_SECONDS, DEFAULT_REVIEWER_TIMEOUT_SECONDS)):
                branch = props[name]["oneOf"][0] if name == "reviewer" else props[name]
                timeout = branch["properties"]["timeout_seconds"]
                self.assertEqual((timeout["type"], timeout["minimum"], timeout["maximum"]), ("integer", minimum, maximum))
                self.assertNotIn("default", timeout)
                rest = OPERATIONS[operation][1]["properties"]["spec"]["properties"][name]
                if name == "reviewer":
                    rest = rest["oneOf"][0]
                self.assertNotIn("timeout_seconds", rest["required"])
                self.assertEqual(rest["properties"]["timeout_seconds"]["default"], default)
        rest_body = openapi()["paths"]["/api/v1/tasks"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertNotIn("timeout_seconds", rest_body["properties"]["spec"]["properties"]["worker_contract"]["required"])
        self.assertNotIn("timeout_seconds", MCP_OPERATIONS["resume"][1]["required"])


class MCPAdmissionTests(ServerFixture, unittest.TestCase):
    def journal_rows(self):
        with self.service.journal.connection() as con:
            return [tuple(row) for row in con.execute("SELECT * FROM requests ORDER BY key_hash")]

    def legacy_args(self, key):
        raw = self.spec()
        del raw["worker_contract"]["timeout_seconds"]
        raw["reviewer"] = {"agent": {"kind": "codex"}}
        return {"repo": "demo", "spec": raw, "request_id": key}

    def test_invalid_timeouts_fail_before_normalization_journal_admission_or_spawn(self):
        missing = object()
        before = self.journal_rows()
        tasks = self.store.list_tasks()
        worktrees = list((self.root / "ato").rglob("worktrees/*"))
        for field, code, maximum in (
                ("worker_contract", "MCP_WORKER_TIMEOUT_REQUIRED", MAX_WORKER_TIMEOUT_SECONDS),
                ("reviewer", "MCP_REVIEWER_TIMEOUT_REQUIRED", MAX_REVIEWER_TIMEOUT_SECONDS)):
            for value in (missing, None, False, True, 0, 4, maximum + 1, "120", 120.0, [], {}, BOT_TOKEN):
                raw = self.spec()
                if field == "reviewer":
                    raw[field] = {"agent": {"kind": "codex"}}
                raw[field].pop("timeout_seconds", None)
                if value is not missing:
                    raw[field]["timeout_seconds"] = value
                for operation in ("validate", "submit"):
                    args = {"repo": "demo", "spec": raw}
                    if operation == "submit":
                        args["request_id"] = "not-consumed"
                    with self.subTest(field=field, value=value, operation=operation), \
                         patch("ato.server.service.normalize_inline_spec") as normalize, \
                         patch("ato.executor.submit_normalized") as submit:
                        result = self.call(operation, args, origin="mcp")
                        self.assertFalse(result["ok"], result)
                        self.assertEqual(result["error"]["code"], code)
                        self.assertIn("spec." + field + ".timeout_seconds", result["error"]["message"])
                        self.assertNotIn(BOT_TOKEN, json.dumps(result))
                        normalize.assert_not_called()
                        submit.assert_not_called()
        self.assertEqual(self.journal_rows(), before)
        self.assertEqual(self.store.list_tasks(), tasks)
        self.assertEqual(list((self.root / "ato").rglob("worktrees/*")), worktrees)
        self.spawn.assert_not_called()
        result = self.call("submit", {"repo": "demo", "spec": self.spec(), "request_id": "not-consumed"}, origin="mcp")
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(self.journal_rows()), 1)
        self.spawn.assert_called_once()

    def test_validate_effective_settings_boundaries_optional_reviewer_and_no_effect(self):
        for seconds in (MIN_WORKER_TIMEOUT_SECONDS, MAX_WORKER_TIMEOUT_SECONDS):
            for reviewer in (None, {"agent": {"kind": "codex"}, "timeout_seconds": MIN_REVIEWER_TIMEOUT_SECONDS},
                             {"agent": {"kind": "codex_acpx"}, "timeout_seconds": MAX_REVIEWER_TIMEOUT_SECONDS}):
                raw = self.spec()
                raw["worker_contract"].update(timeout_seconds=seconds, agent={"kind": "pi"}, sandbox="read-only")
                raw["reviewer"] = reviewer
                original = deepcopy(raw)
                result = self.call("validate", {"repo": "demo", "spec": raw}, origin="mcp")
                self.assertTrue(result["ok"], result)
                data = result["data"]
                self.assertEqual((data["worker_kind"], data["sandbox"], data["timeout_seconds"], data["timeout_source"]),
                                 ("pi", "read-only", seconds, "spec"))
                if reviewer:
                    self.assertEqual(data["reviewer_timeout_seconds"], reviewer["timeout_seconds"])
                    self.assertEqual(data["reviewer_timeout_source"], "spec")
                else:
                    self.assertNotIn("reviewer_timeout_seconds", data)
                self.assertNotIn(str(self.root), json.dumps(result))
                self.assertEqual(raw, original)
        self.assertEqual(self.journal_rows(), [])
        self.assertEqual(self.store.list_tasks(), [])
        self.spawn.assert_not_called()

    def test_other_required_fields_and_inline_security_still_apply(self):
        for field in ("objective", "scope", "authorization", "acceptance"):
            raw = self.spec()
            del raw["worker_contract"][field]
            result = self.call("submit", {"repo": "demo", "spec": raw, "request_id": "invalid"}, origin="mcp")
            self.assertEqual(result["error"]["code"], "INVALID_SPEC")
        for key, value, code in (("repo", str(self.repos["demo"]), "INVALID_REPO"),
                                 ("worker_contract", {**self.spec()["worker_contract"],
                                   "permissions": {"writable_roots": [str(self.root)]}}, "FORBIDDEN")):
            raw = {**self.spec(), key: value}
            result = self.call("submit", {"repo": "demo", "spec": raw, "request_id": "invalid"}, origin="mcp")
            self.assertEqual(result["error"]["code"], code)
        self.assertEqual(self.journal_rows(), [])
        self.spawn.assert_not_called()

    def test_optional_choices_manual_acceptance_and_full_inline_fields(self):
        # Manual acceptance remains usable without command checks or optional
        # agent/sandbox/reviewer fields; validate is never acceptance or execution.
        raw = self.spec()
        raw["worker_contract"]["acceptance"] = [{"id": "review", "kind": "manual", "description": "Human review"}]
        result = self.call("validate", {"repo": "demo", "spec": raw}, origin="mcp")
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["data"]["worker_kind"], result["data"]["sandbox"]), ("codex", "workspace-write"))
        for kind in WORKER_KINDS:
            raw["worker_contract"].update(
                agent={"kind": kind, "model": "fixture/model"}, model="fallback-model", sandbox="read-only",
                permissions={"network_access": False, "writable_roots": []}, max_attempts=2,
                max_no_progress_rounds=2, memory={"legacy": "ignored"})
            raw["reviewer"] = {
                "agent": {"kind": "claude", "model": "fixture/model", "effort": "high"},
                "allowed_kinds": ["claude", "codex"], "session_mode": "resume_if_supported",
                "timeout_seconds": 300, "max_review_rounds": 2, "checklist": ["Inspect changes"],
                "design_docs": ["app.py"],
            }
            raw["worktree"] = {"shared_env_links": ["node_modules"]}
            raw["guardrails"] = {"protected_paths": ["docs/"], "forbid_delete_paths": ["tests"]}
            result = self.call("validate", {"repo": "demo", "spec": raw}, origin="mcp")
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["data"]["worker_kind"], kind)
            self.assertEqual(result["data"]["reviewer_timeout_source"], "spec")
        self.assertEqual(self.journal_rows(), [])
        self.assertEqual(self.store.list_tasks(), [])
        self.spawn.assert_not_called()

    def test_rest_defaults_and_legacy_completed_replay_collision_reauthorization(self):
        args = self.legacy_args("legacy-complete")
        preview = self.call("validate", {k: v for k, v in args.items() if k != "request_id"})
        self.assertTrue(preview["ok"], preview)
        self.assertEqual((preview["data"]["timeout_seconds"], preview["data"]["timeout_source"]), (600, "default"))
        self.assertEqual((preview["data"]["reviewer_timeout_seconds"], preview["data"]["reviewer_timeout_source"]), (300, "default"))
        first = self.call("submit", args)
        self.assertTrue(first["ok"], first)
        task = self.store.get_task(first["data"]["task_id"])
        rows = self.journal_rows()
        self.service = Service(self.cfg, self.db)
        with patch("ato.server.service.require_mcp_timeouts", side_effect=AssertionError("Must replay before new policy")), \
             patch("ato.server.service.normalize_inline_spec", side_effect=AssertionError("Must not resolve HEAD")):
            self.assertEqual(self.call("submit", args, origin="mcp"), first)
            changed = deepcopy(args)
            changed["spec"]["worker_contract"]["timeout_seconds"] = 600
            self.assertEqual(self.call("submit", changed, origin="mcp")["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.journal_rows(), rows)
        self.assertEqual(self.store.get_task(task["id"])["spec"], task["spec"])
        restricted = replace(self.bot, repositories=frozenset({"other"}))
        cfg = replace(self.cfg, principals={**self.cfg.principals, "bot": restricted})
        self.assertEqual(Service(cfg, self.db).call(restricted, "submit", args, origin="mcp")["error"]["code"], "NOT_FOUND")
        rebound = replace(self.cfg, repositories={**self.cfg.repositories, "demo": self.cfg.repositories["other"]})
        self.assertEqual(Service(rebound, self.db).call(self.bot, "submit", args, origin="mcp")["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.call("submit", {**args, "request_id": "brand-new"}, origin="mcp")["error"]["code"],
                         "MCP_WORKER_TIMEOUT_REQUIRED")
        self.assertEqual(self.journal_rows(), rows)
        self.spawn.assert_called_once()

    def test_legacy_unresolved_replay_keeps_reserved_task_and_digest(self):
        args = self.legacy_args("legacy-unresolved")
        # Simulate interruption before task creation, not just a missing response.
        with patch("ato.executor.submit_normalized", side_effect=RuntimeError("private upstream secret")):
            first = self.call("submit", args)
        self.assertEqual(first["error"]["code"], "REQUEST_UNRESOLVED")
        self.assertIsNotNone(first["error"]["task_id"])
        self.assertNotIn("private upstream secret", json.dumps(first))
        rows = self.journal_rows()
        self.service = Service(self.cfg, self.db)
        with patch("ato.server.service.require_mcp_timeouts", side_effect=AssertionError("No new validation on unresolved")):
            self.assertEqual(self.call("submit", args, origin="mcp"), first)
            changed = deepcopy(args)
            changed["spec"]["worker_contract"]["timeout_seconds"] = 600
            self.assertEqual(self.call("submit", changed, origin="mcp")["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.journal_rows(), rows)
        self.assertEqual(self.store.list_tasks(), [])
        self.spawn.assert_not_called()

    def test_explicit_submit_stop_resume_uses_frozen_spec_without_override(self):
        args = {"repo": "demo", "spec": self.spec(), "request_id": "explicit"}
        first = self.call("submit", args, origin="mcp")
        self.assertTrue(first["ok"], first)
        self.assertEqual(self.call("submit", args), first)
        task_id = first["data"]["task_id"]
        frozen = self.store.get_task(task_id)["spec"]
        for index, override in enumerate((300, None)):
            stopped = self.call("stop", {"task_id": task_id, "request_id": f"stop-{index}"}, origin="mcp")
            self.assertTrue(stopped["ok"], stopped)
            resume_args = {"task_id": task_id, "request_id": f"resume-{index}"}
            if override is not None:
                resume_args["timeout_seconds"] = override
            resumed = self.call("resume", resume_args, origin="mcp")
            self.assertTrue(resumed["ok"], resumed)
            execution = resumed["data"]["execution"]
            self.assertEqual(execution["timeout_seconds"], override if override is not None else 120)
            self.assertEqual(execution["timeout_source"], "resume_override" if override is not None else "spec")
            self.assertEqual(self.store.get_task(task_id)["spec"], frozen)


if __name__ == "__main__":
    unittest.main()
