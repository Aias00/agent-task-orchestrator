"""Service authorization, journal safety and existing engine dispatch; stdlib only."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
import sqlite3
import threading
import unittest
from unittest.mock import patch

from ato import config
from ato.cli import main
from ato.server.config import ServerConfig
from ato.server.idempotency import Journal
from ato.server.service import Service
from ato.spec import TaskError
from server_fixtures import BOT_TOKEN, ServerFixture


class ServiceTests(ServerFixture, unittest.TestCase):
    def test_authentication_and_configuration_fail_closed(self):
        for credential in (None, "", "Basic xxx", "Bearer wrong"):
            with self.assertRaises(TaskError) as ctx:
                self.cfg.authenticate(credential)
            self.assertEqual(ctx.exception.code, "AUTH_REQUIRED")
            self.assertNotIn("wrong", str(ctx.exception))
        self.assertEqual(self.cfg.authenticate("Bearer " + BOT_TOKEN), self.bot)
        with patch.dict(os.environ, {"ATO_SERVER_TOKEN_BOT": ""}):
            with self.assertRaises(TaskError) as ctx:
                ServerConfig.load(self.config_file)
            self.assertEqual(ctx.exception.code, "INVALID_SERVER_CONFIG")
        self.raw_config["principals"]["bot"]["scopes"].append("human")
        self.config_file.write_text(json.dumps(self.raw_config))
        with self.assertRaises(TaskError):
            ServerConfig.load(self.config_file)

    def test_repo_and_question_authorization_before_any_effect(self):
        own, other = self.task(), self.task("other")
        question = self.store.create_question(other["id"], "permission", "Approve?", "Synthetic")
        self.assertEqual(self.call("repositories")["data"], {"repositories": [{"alias": "demo"}]})
        self.assertEqual([r["task_id"] for r in self.call("list")["data"]["tasks"]], [own["id"]])
        for op in ("status", "diff", "trace", "inbox", "stop", "resume"):
            args = {"task_id": other["id"]}
            if op in {"stop", "resume"}:
                args["request_id"] = "denied-" + op
            self.assertEqual(self.call(op, args)["error"]["code"], "NOT_FOUND", op)
        args = {"task_id": own["id"], "question_id": question["id"], "answer": "yes", "request_id": "answer1"}
        self.assertEqual(self.call("answer", args)["error"]["code"], "FORBIDDEN")
        self.assertEqual(self.call("answer", args, self.human)["error"]["code"], "NOT_FOUND")
        self.assertEqual(self.store.get_question(question["id"])["state"], "open")
        self.assertEqual(self.call("list", {"repo": "../other"})["error"]["code"], "NOT_FOUND")
        self.spawn.assert_not_called()

    def test_inline_validation_rejects_paths_unknown_fields_and_extra_writable_roots(self):
        valid = self.call("validate", {"repo": "demo", "spec": self.spec()})
        self.assertTrue(valid["ok"], valid)
        self.assertEqual(valid["data"]["repo"], "demo")
        for raw in ({**self.spec(), "repo": str(self.repos["other"])},
                    {**self.spec(), "repo": "../other"}, {**self.spec(), "unexpected": True}):
            self.assertFalse(self.call("validate", {"repo": "demo", "spec": raw})["ok"])
        raw = self.spec()
        raw["worker_contract"]["permissions"] = {"writable_roots": [str(self.repos["other"])]}
        self.assertEqual(self.call("validate", {"repo": "demo", "spec": raw})["error"]["code"], "FORBIDDEN")
        self.assertEqual(self.call("submit", {"repo": "demo", "spec": self.spec()})["error"]["code"], "USAGE")
        self.assertFalse(self.call("resume", {"task_id": "task-" + "a" * 32,
                                              "request_id": "r", "timeout_seconds": True})["ok"])
        self.spawn.assert_not_called()

    def test_submit_is_background_neutral_and_replay_shared_with_mcp(self):
        args = {"repo": "demo", "spec": self.spec(), "request_id": "submit-1"}
        result = self.call("submit", args)
        self.assertTrue(result["ok"], result)
        task = self.store.get_task(result["data"]["task_id"])
        self.assertEqual((task["origin_kind"], task["origin_session_id"]), ("api", None))
        self.assertEqual(task["status"], "queued")
        self.spawn.assert_called_once()
        child_env = self.spawn.call_args.kwargs["child_env"]
        self.assertFalse(any(k.startswith("ATO_SERVER_") for k in child_env))
        self.assertNotIn("CODEX_THREAD_ID", child_env)
        self.assertNotIn(BOT_TOKEN, json.dumps(child_env))
        self.assertEqual(config.detect_submission_origin()[0], "codex")  # CLI behavior remains intact.
        self.service = Service(self.cfg, self.db)  # persisted replay after process reconstruction
        self.assertEqual(self.call("submit", args, origin="mcp"), result)
        self.spawn.assert_called_once()
        changed = {**args, "spec": {**args["spec"], "base_ref": task["base_commit"]}}
        self.assertEqual(self.call("submit", changed)["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.assertEqual(self.call("stop", {"task_id": task["id"], "request_id": "submit-1"})["error"]["code"],
                         "IDEMPOTENCY_CONFLICT")
        self.assertNotIn(BOT_TOKEN.encode(), self.service.journal.path.read_bytes())
        self.assertEqual(self.service.journal.path.stat().st_mode & 0o777, 0o600)

    def test_human_answer_is_explicit_and_never_resumes_or_accepts(self):
        task = self.task()
        self.store.update_task(task["id"], status="waiting_input")
        question = self.store.create_question(task["id"], "permission", "Proceed?", "Needs authority")
        args = {"task_id": task["id"], "question_id": question["id"], "answer": "Proceed within scope",
                "request_id": "human-answer"}
        self.assertEqual(self.call("answer", args)["error"]["code"], "FORBIDDEN")
        result = self.call("answer", args, self.human, "mcp")
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.call("answer", args, self.human), result)
        current = self.store.get_task(task["id"])
        self.assertEqual(current["status"], "waiting_input")
        self.assertIsNone(current["acceptance"])
        self.assertEqual(self.call("inbox", {"task_id": task["id"]})["data"]["count"], 0)
        self.spawn.assert_not_called()
        events = self.store.list_events(task["id"])
        self.assertTrue(any(e["kind"] == "transport_operation" for e in events))
        self.assertEqual(self.call("answer", args, replace(self.human, scopes=frozenset({"read"})))["error"]["code"],
                         "FORBIDDEN")

    def test_resume_uses_existing_engine_with_timeout_and_scrubbed_environment(self):
        task = self.task()
        with patch("ato.executor.resume", return_value={"task_id": task["id"], "status": "queued"}) as resume:
            args = {"task_id": task["id"], "request_id": "resume-1", "timeout_seconds": 120}
            first = self.call("resume", args, origin="mcp")
            self.assertEqual(self.call("resume", args), first)
            resume.assert_called_once()
            self.assertEqual(resume.call_args.kwargs["timeout_seconds"], 120)
            self.assertNotIn("ATO_SERVER_TOKEN_BOT", resume.call_args.kwargs["child_env"])
            self.assertNotIn("foreground", resume.call_args.kwargs)

    def test_worker_environment_removes_renamed_copies(self):
        with patch.dict(os.environ, {"COPIED_SECRET": "prefix:" + BOT_TOKEN}):
            self.assertNotIn("COPIED_SECRET", self.cfg.worker_environment())

    def test_cached_submit_does_not_revalidate_head_and_alias_changes_conflict(self):
        args = {"repo": "demo", "spec": self.spec(), "request_id": "stable"}
        result = self.call("submit", args)
        self.assertTrue(result["ok"], result)
        with patch("ato.server.service.normalize_inline_spec", side_effect=AssertionError("Replay must use the stored result")):
            self.assertEqual(self.call("submit", args), result)
        remapped = replace(self.cfg, repositories={**self.cfg.repositories, "demo": self.cfg.repositories["other"]})
        rebound = Service(remapped, self.db)
        self.assertEqual(rebound.call(self.bot, "submit", args, origin="mcp")["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.spawn.assert_called_once()

    def test_missing_optional_dependencies_and_stdio_errors_are_actionable(self):
        from ato.server.entrypoints import dependencies
        from importlib.metadata import PackageNotFoundError

        with patch("ato.server.entrypoints.version", side_effect=PackageNotFoundError("mcp")):
            with self.assertRaises(TaskError) as ctx:
                dependencies()
            self.assertEqual(ctx.exception.code, "UNSUPPORTED_CAPABILITY")
            self.assertIn(".[server]", str(ctx.exception))
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(["mcp", "--stdio"])  # invalid arguments must not corrupt the protocol stream
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["error"]["code"], "USAGE")

    def test_invalid_submission_does_not_consume_key_and_replay_rechecks_authority(self):
        args = {"repo": "demo", "spec": self.spec(), "request_id": "validated-first"}
        invalid = {**args, "spec": {**args["spec"], "unexpected": True}}
        self.assertFalse(self.call("submit", invalid)["ok"])
        with sqlite3.connect(self.service.journal.path) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM requests").fetchone()[0], 0)
        result = self.call("submit", args)
        self.assertTrue(result["ok"], result)
        restricted = replace(self.bot, repositories=frozenset({"other"}))
        cfg = replace(self.cfg, principals={**self.cfg.principals, "bot": restricted})
        restarted = Service(cfg, self.db)
        denied = restarted.call(restricted, "submit", args, origin="mcp")
        self.assertEqual(denied["error"]["code"], "NOT_FOUND")
        self.assertIsNone(denied["data"])
        read_only = replace(self.bot, scopes=frozenset({"read"}))
        cfg = replace(self.cfg, principals={**self.cfg.principals, "bot": read_only})
        self.assertEqual(Service(cfg, self.db).call(read_only, "submit", args, origin="api")["error"]["code"],
                         "FORBIDDEN")
        self.spawn.assert_called_once()

    def test_mcp_submission_has_neutral_origin_and_scrubs_all_bindings(self):
        env = {"COPIED_SECRET": "Bearer " + BOT_TOKEN, "ATO_SERVER_MISC": "private",
               "ATO_SUBMISSION_KIND": "codex", "ANTIGRAVITY_CONVERSATION_ID": "ambient",
               "ATO_ACTOR_FILE": "/synthetic/actor"}
        with patch.dict(os.environ, env):
            result = self.call("submit", {"repo": "demo", "spec": self.spec(), "request_id": "mcp-origin"},
                               origin="mcp")
        self.assertTrue(result["ok"], result)
        task = self.store.get_task(result["data"]["task_id"])
        self.assertEqual((task["origin_kind"], task["origin_session_id"]), ("mcp", None))
        child_env = self.spawn.call_args.kwargs["child_env"]
        self.assertFalse(set(env) & child_env.keys())
        self.assertNotIn("CODEX_THREAD_ID", child_env)
        self.assertFalse(any(k.startswith("ATO_SERVER_") for k in child_env))

    def test_duplicate_credentials_roots_and_missing_stdio_credentials_fail_closed(self):
        with patch.dict(os.environ, {"ATO_SERVER_TOKEN_HUMAN": BOT_TOKEN}):
            with self.assertRaises(TaskError):
                ServerConfig.load(self.config_file)
        with patch.dict(os.environ, {"ATO_SERVER_TOKEN_READER": ""}):
            with self.assertRaises(TaskError):
                ServerConfig.load(self.config_file)
        self.raw_config["repositories"]["other"] = str(self.repos["demo"])
        self.config_file.write_text(json.dumps(self.raw_config))
        with self.assertRaises(TaskError):
            ServerConfig.load(self.config_file)


class JournalTests(ServerFixture, unittest.TestCase):
    def test_concurrent_intent_precedes_effect_and_duplicates_cannot_execute(self):
        entered, finish = threading.Event(), threading.Event()
        calls = []

        def effect():
            # The durable intent is visible through a different SQLite connection before the effect.
            with sqlite3.connect(self.service.journal.path) as con:
                self.assertEqual(con.execute("SELECT state FROM requests").fetchone()[0], "unresolved")
            calls.append(1)
            entered.set()
            self.assertTrue(finish.wait(5))
            return {"ok": True}

        journal = self.service.journal
        with ThreadPoolExecutor(max_workers=2) as pool:
            future = pool.submit(journal.perform, "bot", "key", "stop", {"task": "one"}, None, effect)
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(TaskError) as ctx:
                    Journal(journal.path).perform("bot", "key", "stop", {"task": "one"}, None, effect)
                self.assertEqual(ctx.exception.code, "REQUEST_UNRESOLVED")
            finally:
                finish.set()
            self.assertEqual(future.result(), {"ok": True})
        self.assertEqual(calls, [1])
        self.assertEqual(Journal(journal.path).perform("bot", "key", "stop", {"task": "one"}, None, effect), {"ok": True})
        self.assertEqual(calls, [1])

    def test_crash_is_unresolved_after_restart_and_principals_have_independent_keys(self):
        def crash():
            raise SystemExit("simulated crash after effect")
        with self.assertRaises(SystemExit):
            self.service.journal.perform("bot", "crash", "submit", {"repo": "demo"}, "task-" + "a" * 32, crash)
        with self.assertRaises(TaskError) as ctx:
            Journal(self.service.journal.path).perform("bot", "crash", "submit", {"repo": "demo"}, None,
                                                       lambda: self.fail("must not replay"))
        self.assertEqual(ctx.exception.code, "REQUEST_UNRESOLVED")
        self.assertEqual(ctx.exception.task_id, "task-" + "a" * 32)
        result = self.service.journal.perform("human", "crash", "answer", {}, None, lambda: {"separate": True})
        self.assertEqual(result, {"separate": True})


if __name__ == "__main__":
    unittest.main()
