"""Read scope, nested discovery and journal compatibility for joint reviews."""
import json
import unittest
from unittest.mock import patch

from ato.server.http import openapi
from ato.server.service import MCP_OPERATIONS, OPERATIONS
from server_fixtures import ServerFixture
from test_joint_review import panel


class JointServiceTests(ServerFixture, unittest.TestCase):
    def test_nested_member_schema_and_required_mcp_timeouts(self):
        for operation in ("validate", "submit"):
            spec = MCP_OPERATIONS[operation][1]["properties"]["spec"]
            single, group = spec["properties"]["reviewer"]["oneOf"]
            members = group["properties"]["members"]
            self.assertEqual((members["minItems"], members["maxItems"]), (2, 8))
            member = members["items"]
            self.assertEqual(set(member["required"]), {"id", "agent", "timeout_seconds"})
            self.assertFalse(member["additionalProperties"])
            self.assertNotIn("max_review_rounds", member["properties"])
            self.assertEqual(group["properties"]["policy"]["enum"], ["all"])
            rest = OPERATIONS[operation][1]["properties"]["spec"]["properties"]["reviewer"]["oneOf"][1]
            self.assertNotIn("timeout_seconds", rest["properties"]["members"]["items"]["required"])
        before = self.store.list_tasks()
        for index in (0, 1):
            for value in (None, True, 4, 3601, "30", 30.0):
                raw = self.spec()
                raw["reviewer"] = panel()
                raw["reviewer"]["members"][index]["timeout_seconds"] = value
                for op in ("validate", "submit"):
                    args = {"repo": "demo", "spec": raw}
                    if op == "submit":
                        args["request_id"] = "not-admitted"
                    with patch("ato.server.service.normalize_inline_spec") as normalize:
                        response = self.call(op, args, origin="mcp")
                        self.assertEqual(response["error"]["code"], "MCP_REVIEWER_TIMEOUT_REQUIRED")
                        self.assertIn(f"members[{index}].timeout_seconds", response["error"]["message"])
                        normalize.assert_not_called()
        self.assertEqual(self.store.list_tasks(), before)
        self.spawn.assert_not_called()

    def test_default_sources_and_legacy_panel_replay_before_new_validation(self):
        raw = self.spec()
        raw["reviewer"] = panel()
        del raw["reviewer"]["members"][0]["timeout_seconds"]
        args = {"repo": "demo", "spec": raw, "request_id": "rest-history"}
        rest = self.call("submit", args, origin="api")
        self.assertTrue(rest["ok"], rest)
        replay = self.call("submit", args, origin="mcp")
        self.assertEqual(replay, rest)
        self.spawn.assert_called_once()
        validation = self.call("validate", {"repo": "demo", "spec": raw})
        members = validation["data"]["reviewer"]["members"]
        self.assertEqual(members[0]["timeout_source"], "default")
        self.assertEqual(members[1]["timeout_source"], "spec")
        self.assertEqual(self.call("submit", {**args, "request_id": "new-intent"}, origin="mcp")["error"]["code"], "MCP_REVIEWER_TIMEOUT_REQUIRED")

    def test_history_uses_same_read_scope_repo_binding_and_integer_bounds(self):
        task = self.task()
        tid = task["id"]
        for origin in ("api", "mcp"):
            response = self.call("reviews", {"task_id": tid}, origin=origin)
            self.assertTrue(response["ok"], response)
            self.assertEqual(response["data"]["items"], [])
            denied = self.call("reviews", {"task_id": tid}, principal=self.cfg.principals["reader"], origin=origin)
            self.assertEqual(denied["error"]["code"], "NOT_FOUND")
            for args in ({"limit": 21}, {"limit": True}, {"cursor": -1}, {"cursor": 2**63},
                         {"section": "session"}, {"member_id": "../escape"}, {"section": "members"}):
                response = self.call("reviews", {"task_id": tid, **args}, origin=origin)
                self.assertFalse(response["ok"], response)
        self.assertIn("/api/v1/tasks/{task_id}/reviews", openapi()["paths"])
        self.assertEqual(OPERATIONS["reviews"][0], "read")

    def test_group_lookup_cannot_cross_tasks_and_legacy_remains_readable(self):
        task = self.task()
        other = self.task("other")
        with self.store.tx() as con:
            run = "run-" + "a" * 32
            con.execute("INSERT INTO runs(id, task_id, kind, phase, created_at) VALUES (?, ?, 'initial', 'settled', 1)", (run, other["id"]))
            gid = con.execute("""INSERT INTO review_groups(task_id, run_id, round, product_id, product_fingerprint,
                                 config_sha256, policy, created_at) VALUES (?, ?, 1, 'p', 'f', 'c', 'all', 1)""", (other["id"], run)).lastrowid
        denied = self.call("reviews", {"task_id": task["id"], "group_id": gid})
        self.assertEqual(denied["error"]["code"], "NOT_FOUND")
        from test_joint_review import verdict, finding
        self.store.add_review(task["id"], "run-" + "b" * 32, 1, "codex", verdict("request_changes", [finding()]),
                              session_id="never-public", product_id="p", reviewer_sha256="c")
        history = self.call("reviews", {"task_id": task["id"], "section": "legacy"})["data"]
        self.assertEqual(history["total"], 1)
        detail = self.call("reviews", {"task_id": task["id"], "review_id": history["items"][0]["review_id"], "section": "findings"})
        self.assertEqual(detail["data"]["items"][0]["evidence"], "Wrong branch")
        self.assertNotIn("never-public", json.dumps([history, detail]))
