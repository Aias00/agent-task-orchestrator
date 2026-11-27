"""Offline Cursor worker contracts and lifecycle tests using synthetic executables."""
from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import re
import signal
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from ato import config, process
from ato.adapters import base, cursor
from ato.spec import MODEL_RE, WORKER_KINDS, TaskError, normalize_spec
from ato.store import Store
from test_core_and_store import init_repo, make_spec
from test_execution_and_cli import run_cli


MODEL = "claude-opus-4-8[context=1m,effort=high,fast=false]"
DONE = {"status": "completed", "summary": "Finished", "blocker": "", "next_step": ""}
ORIGIN_KEYS = ("CODEX_THREAD_ID", "ATO_SUBMISSION_KIND", "ANTIGRAVITY_CONVERSATION_ID", "ATO_ACTOR_FILE")

# The shim records the outer sandbox invocation and executes only our local fixture.
# Profile tests below inspect policy explicitly; this shim does not emulate Seatbelt.
SANDBOX_SCRIPT = '''import json, os, pathlib, sys
runtime = pathlib.Path(os.environ["TMPDIR"]).parent
(runtime / "sandbox-argv.json").write_text(json.dumps(sys.argv[1:]))
os.execv(sys.argv[3], sys.argv[3:])
'''

CURSOR_SCRIPT = '''import json, os, pathlib, signal, subprocess, sys, time
if "--version" in sys.argv:
    print("cursor-fixture 1.0")
    sys.exit(0)
runtime = pathlib.Path(os.environ["TMPDIR"]).parent
prompt = sys.stdin.read()
origin_keys = ("CODEX_THREAD_ID", "ATO_SUBMISSION_KIND", "ANTIGRAVITY_CONVERSATION_ID", "ATO_ACTOR_FILE")
(runtime / "capture.json").write_text(json.dumps({
    "argv": sys.argv[1:], "prompt": prompt, "cwd": os.getcwd(),
    "origin": {key: os.environ[key] for key in origin_keys if key in os.environ},
    "credential_present": "CURSOR_API_KEY" in os.environ,
}))
mode = os.environ.get("CURSOR_FIXTURE_MODE", "output")
done = {"status": "completed", "summary": "Finished", "blocker": "", "next_step": ""}
if mode == "sleep":
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
    child = subprocess.Popen([sys.executable, "-c", child_code], stdout=subprocess.PIPE)
    child.stdout.readline()
    (runtime / "pids.json").write_text(json.dumps([os.getpid(), child.pid]))
    time.sleep(60)
elif mode == "question":
    if "Use 4 spaces" in prompt:
        pathlib.Path("answer.txt").write_text("Use 4 spaces")
        print(json.dumps({"type": "result", "result": json.dumps(done)}))
    else:
        question = dict(done, status="needs_input", summary="Use tabs or spaces?", blocker="Style choice")
        print(json.dumps({"type": "result", "result": json.dumps(question)}))
elif mode in ("retry-json", "retry-exit"):
    if "Worker failed:" in prompt:
        pathlib.Path("repaired.txt").write_text("ok")
        print(json.dumps({"type": "result", "result": json.dumps(done)}))
    elif mode == "retry-json":
        print(json.dumps({"type": "result", "result": "I have finished."}))
    else:
        print(json.dumps({"type": "result", "result": json.dumps(done)}))
        sys.exit(9)
else:
    print(os.environ.get("CURSOR_FIXTURE_OUTPUT", ""))
    print(os.environ.get("CURSOR_FIXTURE_STDERR", ""), file=sys.stderr)
    sys.exit(int(os.environ.get("CURSOR_FIXTURE_EXIT", "0")))
'''


class CursorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(TemporaryDirectory(prefix="ato-cursor-test-"))
        self.root = Path(self.tmp).resolve()
        self.worktree = self.root / "wt"
        self.worktree.mkdir()
        self.runtime = self.root / "runtime"
        self.exe = self.script("cursor-agent", CURSOR_SCRIPT)
        self.sandbox = self.script("sandbox-exec", SANDBOX_SCRIPT)
        self.enterContext(patch.dict(os.environ, {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "ATO_HOME": str(self.root / "ato"), "CODEX_HOME": str(self.root / "codex"),
            "ATO_CURSOR_BIN": str(self.exe), "ATO_SUBMISSION_KIND": "cursor",
            "CURSOR_FIXTURE_OUTPUT": self.event(DONE),
        }, clear=True))
        self.enterContext(patch.object(cursor, "SANDBOX_EXEC", self.sandbox))
        self.enterContext(patch.object(Path, "home", return_value=self.root / "home"))
        self.enterContext(patch.object(config, "MANAGED_CLI_BIN", self.root / "managed"))
        self.req = base.WorkerRequest("task-fixture", "cursor", self.worktree, self.runtime, "Implement the task")

    def script(self, name, body):
        path = self.root / name
        path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        path.chmod(0o700)
        return path

    @staticmethod
    def event(value):
        return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(value)})

    def output(self, stdout, stderr="", rc=0):
        os.environ.update(CURSOR_FIXTURE_OUTPUT=stdout, CURSOR_FIXTURE_STDERR=stderr, CURSOR_FIXTURE_EXIT=str(rc))
        return cursor.run_worker(self.req)

    def capture(self):
        return json.loads((self.runtime / "capture.json").read_text())

    def spec(self, *, attempts=2, checks=None):
        repo = init_repo(self.root / "repo")
        sp = make_spec(repo, self.root / "spec.json", worker_contract={
            "objective": "Implement fixture", "scope": "Fixture files", "authorization": "Local only",
            "agent": {"kind": "cursor", "model": MODEL}, "max_attempts": attempts,
            "acceptance": [{"id": "host-check", "kind": "command", "description": "Validate output",
                            "checks": checks or [["true"]]}],
        })
        return repo, sp

    def test_completed_raw_fenced_assistant_and_result(self):
        text = json.dumps(DONE)
        fence = "```json\n" + text + "\n```"
        assistant = lambda value: json.dumps({"type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "text", "text": value}]}})
        cases = [text, json.dumps(DONE, indent=2), fence, self.event(DONE),
                 json.dumps({"type": "result", "result": DONE}), assistant(text), assistant(fence),
                 json.dumps({"type": "result", "result": fence}),
                 assistant(text) + '\n{"type":"result","subtype":"success"}',
                 '{"type":"system","subtype":"init"}\n' + assistant("working") + "\n" + self.event(DONE)]
        for payload in cases:
            with self.subTest(payload=payload):
                result = self.output(payload)
                self.assertEqual((result.status, result.summary), ("completed", "Finished"))

    def test_workspace_argv_origin_cleanup_schema_and_stdin_secrets(self):
        for key in ORIGIN_KEYS:
            os.environ[key] = "ambient-cross-vendor-value"
        os.environ["CURSOR_API_KEY"] = "synthetic-api-key-123456"
        req = replace(self.req, model=MODEL, prompt="Do work with synthetic-api-key-123456",
                      controller_args=("--worktree", "anb", "--dangerously-bypass-approvals-and-sandbox"))
        result = cursor.run_worker(req)
        self.assertEqual(result.status, "completed")
        capture = self.capture()
        argv = capture["argv"]
        self.assertEqual(argv[:3], ["-p", "--output-format", "stream-json"])
        for flag, value in (("--workspace", str(self.worktree)), ("--sandbox", "disabled"), ("--model", MODEL)):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--trust", argv)
        self.assertIn("--force", argv)
        for forbidden in ("-w", "--worktree", "anb", "--dangerously-bypass-approvals-and-sandbox", "synthetic-api-key-123456"):
            self.assertNotIn(forbidden, argv)
        self.assertEqual(capture["origin"], {})
        self.assertTrue(capture["credential_present"])
        self.assertEqual(capture["cwd"], str(self.worktree))
        self.assertIn(json.dumps(cursor.WORKER_RESULT_SCHEMA), capture["prompt"])
        self.assertIn(req.prompt, capture["prompt"])
        self.assertEqual(json.loads((self.runtime / "sandbox-argv.json").read_text())[:3],
                         ["-f", str(self.runtime / "sandbox.sb"), str(self.exe)])
        for name in ("stdout.jsonl", "stderr.log", "sandbox.sb"):
            self.assertNotIn(os.environ["CURSOR_API_KEY"], (self.runtime / name).read_text())

    def test_sandbox_profiles_sensitive_exclusions_and_readonly(self):
        self.assertEqual(cursor.run_worker(self.req).status, "completed")
        profile = (self.runtime / "sandbox.sb").read_text()
        self.assertIn("(deny file-write*)", profile)
        self.assertIn(f'(subpath "{self.runtime}")', profile)
        self.assertIn(f'(allow file-write* (subpath "{self.root / "home/.cursor"}"))', profile)
        self.assertNotIn(f'(subpath "{self.root / "home"}")', profile)
        self.assertNotIn('(subpath "/tmp")', profile)
        pattern = json.loads(re.search(r'\(regex ("(?:\\.|[^"\\])*")\)', profile).group(1))
        for name in base.SENSITIVE_PATHS:
            self.assertIn(f'(require-not (subpath "{self.worktree / name}"))', profile)
            self.assertRegex(str(self.worktree / "nested" / name / "config"), pattern)
        for name in (".cursor", ".git", ".env"):
            self.assertIn(name, base.SENSITIVE_PATHS)
        self.assertNotRegex(str(self.worktree / "source.py"), pattern)
        result = cursor.run_worker(replace(self.req, sandbox="read-only", permissions={"network_access": False}))
        self.assertEqual(result.status, "completed")
        argv = self.capture()["argv"]
        self.assertEqual(argv[argv.index("--mode") + 1], "plan")
        self.assertNotIn("--force", argv)
        self.assertNotIn("--trust", argv)
        profile = (self.runtime / "sandbox.sb").read_text()
        self.assertIn(f'(deny file-write* (subpath "{self.worktree}"))', profile)
        self.assertIn("(deny network*)", profile)

    def test_unavailable_executable_sandbox_and_launch_race(self):
        with patch.dict(os.environ, {"ATO_CURSOR_BIN": str(self.root / "missing")}):
            result = cursor.run_worker(self.req)
            self.assertEqual((result.status, result.blocker), ("blocked", "EXECUTABLE_UNAVAILABLE"))
        with patch.object(cursor, "SANDBOX_EXEC", self.root / "no-sandbox"):
            result = cursor.run_worker(self.req)
            self.assertEqual((result.status, result.blocker), ("blocked", "UNSUPPORTED_CAPABILITY"))
        self.assertFalse(self.runtime.exists())
        with patch.object(cursor, "run_bounded_subprocess", side_effect=FileNotFoundError):
            self.assertEqual(cursor.run_worker(self.req).blocker, "EXECUTABLE_UNAVAILABLE")
        self.assertEqual(cursor.run_worker(replace(self.req, sandbox="unrestricted")).blocker, "UNSUPPORTED_CAPABILITY")

    def test_auth_provider_and_sandbox_failures(self):
        cases = [("Not logged in", "AUTH_BLOCKED"), ("authentication required", "AUTH_BLOCKED"),
                 ("Auth required", "AUTH_BLOCKED"), ("quota exhausted", "PROVIDER_UNAVAILABLE"),
                 ("rate limit exceeded", "PROVIDER_UNAVAILABLE"), ("Too many requests", "PROVIDER_UNAVAILABLE"),
                 ("sandbox-exec: sandbox_init: Operation not permitted", "UNSUPPORTED_CAPABILITY")]
        for diagnostic, code in cases:
            with self.subTest(diagnostic=diagnostic):
                for output, stderr, rc in (("", diagnostic, 1), (diagnostic, "", 1),
                                           (json.dumps({"type": "result", "is_error": True, "result": diagnostic}), "", 0)):
                    result = self.output(output, stderr, rc)
                    self.assertEqual((result.status, result.blocker), ("blocked", code))
        # Ordinary assistant text mentioning quota is not a provider error.
        self.assertEqual(self.output(self.event(dict(DONE, summary="Fixed quota handling"))).status, "completed")

    def test_invalid_results_and_nonzero_never_succeed(self):
        text = json.dumps(DONE)
        values = ["", "done", "prefix " + text, "```json\n" + text + "\n```\nextra", "[]",
                  json.dumps(dict(DONE, extra="field")), json.dumps(dict(DONE, summary=" ")),
                  json.dumps(dict(DONE, status="failed")), json.dumps(dict(DONE, next_step=3)),
                  json.dumps(dict(DONE, summary="x" * 8193)),
                  '{"status":"blocked",' + text[1:], text.replace('"Finished"', 'NaN'),
                  json.dumps({"type": "tool_result", "result": text}),
                  self.event(DONE) + '\n{"type":"result","result":"bad final result"}',
                  self.event(DONE) + '\n{"type":"error","message":"unknown failure"}',
                  self.event(DONE) + "\nnot json"]
        for value in values:
            with self.subTest(value=value[:150]):
                self.assertEqual(self.output(value).status, "failed")
        self.assertEqual(self.output(self.event(DONE), rc=7).status, "failed")
        self.assertEqual(self.output(json.dumps({"type": "result", "is_error": True, "result": text})).status, "failed")

    def test_secret_output_is_suppressed_and_secret_argv_is_not_launched(self):
        secret = "synthetic-cursor-secret-12345"
        os.environ["CURSOR_API_KEY"] = secret
        escaped = "".join("\\u%04x" % ord(char) for char in secret)
        for stdout, stderr in ((self.event(dict(DONE, summary=secret)), ""),
                               (self.event(DONE), secret),
                               (self.event(dict(DONE, summary=secret)).replace(secret, escaped), "")):
            with self.subTest(stderr=bool(stderr)):
                result = self.output(stdout, stderr)
                self.assertEqual((result.status, result.blocker), ("failed", "SECRET_IN_WORKER_OUTPUT"))
                self.assertNotIn(secret, repr(result))
                for name in ("stdout.jsonl", "stderr.log"):
                    self.assertEqual((self.runtime / name).read_text(), "[REDACTED]\n")
        with patch.object(cursor, "run_bounded_subprocess") as launch:
            result = cursor.run_worker(replace(self.req, model=secret))
            self.assertEqual(result.blocker, "SECRET_IN_ARGUMENTS")
            launch.assert_not_called()

    def test_model_validation_worker_registration_and_origin(self):
        for model in (MODEL, "auto", "vendor/model:tag", "a" * 256, "model[context=1m]"):
            self.assertIsNotNone(MODEL_RE.fullmatch(model))
        bad_models = ["-model", "model fast", "model\tfast", "model\n", "model\x00", 'model"', "model'",
                      "model;id", "model$(id)", "model`id`", "model[]", "model[x=]", "model[x=1,]",
                      "model[x=1]tail", "model[x=[1]]", "a" * 257]
        for model in bad_models:
            with self.subTest(model=model):
                self.assertIsNone(MODEL_RE.fullmatch(model))
        self.assertIn("cursor", WORKER_KINDS)
        self.assertIs(base.get_worker_runner("cursor"), cursor.run_worker)
        with self.assertRaises(TaskError):
            base.get_reviewer_runner("cursor")
        repo, sp = self.spec()
        document = json.loads(sp.read_text())
        self.assertEqual(normalize_spec(repo, sp).document["worker_contract"]["agent"]["model"], MODEL)
        for model in bad_models:
            document["worker_contract"]["agent"]["model"] = model
            sp.write_text(json.dumps(document))
            with self.assertRaises(TaskError):
                normalize_spec(repo, sp)
        self.assertEqual(config.detect_submission_origin({
            "ATO_SUBMISSION_KIND": "cursor", "CODEX_THREAD_ID": "11111111-2222-3333-4444-555555555555",
            "ANTIGRAVITY_CONVERSATION_ID": "ambient",
        }), ("cursor", None))

    def test_resolution_precedence_never_generic_agent_and_doctor(self):
        legacy = self.script("legacy-cursor", "pass\n")
        os.environ["CTASK_CURSOR_BIN"] = str(legacy)
        self.assertEqual(config.resolve_executable("cursor"), str(self.exe))
        del os.environ["ATO_CURSOR_BIN"]
        self.assertEqual(config.resolve_executable("cursor"), str(legacy))
        del os.environ["CTASK_CURSOR_BIN"]
        os.environ["PATH"] = str(self.root)
        self.assertEqual(config.resolve_executable("cursor"), str(self.exe))
        self.exe.unlink()
        self.script("agent", "raise AssertionError('generic agent must not execute')\n")
        self.script("cursor", "raise AssertionError('editor must not execute')\n")
        self.assertIsNone(config.resolve_executable("cursor"))
        managed = config.MANAGED_CLI_BIN
        managed.mkdir()
        managed_exe = managed / "cursor-agent"
        managed_exe.write_text(f"#!{sys.executable}\nprint('cursor-fixture 1.0')\n")
        managed_exe.chmod(0o700)
        self.assertEqual(config.resolve_executable("cursor"), str(managed_exe))
        doc = base.doctor_all(probe_versions=True)
        self.assertEqual(doc["worker"]["cursor"], {
            "kind": "cursor", "available": True, "executable": str(managed_exe), "version": "cursor-fixture 1.0",
        })
        self.assertNotIn("cursor", doc["reviewers"])
        os.environ["ATO_CURSOR_BIN"] = str(self.root / "missing")
        self.assertFalse(base.doctor_all()["worker"]["cursor"]["available"])

    def test_needs_input_inbox_answer_resume_with_real_cursor_runner(self):
        os.environ.update(CURSOR_FIXTURE_MODE="question", CODEX_THREAD_ID="11111111-2222-3333-4444-555555555555")
        repo, sp = self.spec(checks=[["sh", "-c", 'test "$(cat answer.txt)" = "Use 4 spaces"']])
        store = Store()
        code, result = run_cli(["run", "--repo", str(repo), "--spec", str(sp), "--foreground"], store=store)
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["status"], "waiting_input")
        tid = result["data"]["task_id"]
        task = store.get_task(tid)
        self.assertEqual((task["origin_kind"], task["origin_session_id"]), ("cursor", None))
        code, inbox = run_cli(["inbox"], store=store)
        self.assertEqual(code, 0)
        self.assertEqual(inbox["data"]["count"], 1)
        question = inbox["data"]["requests"][0]
        self.assertEqual(question["question"], "Use tabs or spaces?")
        code, result = run_cli(["resume", tid, "--foreground"], store=store)
        self.assertEqual(code, 4)
        self.assertEqual(result["error"]["code"], "NOT_RESUMABLE")
        code, answer = run_cli(["answer", question["request_id"], "--answer", "Use 4 spaces"], store=store)
        self.assertEqual((code, answer["data"]["state"]), (0, "answered"))
        code, done = run_cli(["resume", tid, "--foreground"], store=store)
        self.assertEqual((code, done["data"]["status"]), (0, "completed"))
        self.assertEqual(done["data"]["checks"][0]["state"], "passed")
        self.assertEqual((Path(task["worktree_path"]) / "answer.txt").read_text(), "Use 4 spaces")
        captures = [json.loads(path.read_text()) for path in config.runtime_dir().rglob("capture.json")]
        self.assertEqual(len(captures), 2)
        self.assertTrue(any("Use 4 spaces" in row["prompt"] for row in captures))
        self.assertTrue(all(row["origin"] == {} for row in captures))

    def test_malformed_json_and_nonzero_exit_retry_to_host_checked_completion(self):
        repo, sp = self.spec(checks=[["sh", "-c", 'test "$(cat repaired.txt)" = "ok"']])
        store = Store()
        for mode in ("retry-json", "retry-exit"):
            with self.subTest(mode=mode):
                os.environ["CURSOR_FIXTURE_MODE"] = mode
                code, result = run_cli(["run", "--repo", str(repo), "--spec", str(sp), "--foreground"], store=store)
                self.assertEqual((code, result["data"]["status"]), (0, "completed"))
                self.assertEqual(result["data"]["checks"][0]["state"], "passed")
                tid = result["data"]["task_id"]
                captures = list((config.runtime_dir() / tid).rglob("capture.json"))
                self.assertEqual(len(captures), 2)
                self.assertTrue(any("Worker failed:" in json.loads(path.read_text())["prompt"] for path in captures))

    def test_blocked_results_propagate_without_retry(self):
        repo, sp = self.spec()
        store = Store()
        for code, diagnostic in (("AUTH_BLOCKED", "Not logged in"), ("PROVIDER_UNAVAILABLE", "Quota exhausted"),
                                 ("EXECUTABLE_UNAVAILABLE", "")):
            with self.subTest(code=code):
                os.environ.update(CURSOR_FIXTURE_STDERR=diagnostic, CURSOR_FIXTURE_EXIT="1")
                os.environ["ATO_CURSOR_BIN"] = str(self.root / "missing") if not diagnostic else str(self.exe)
                rc, result = run_cli(["run", "--repo", str(repo), "--spec", str(sp), "--foreground"], store=store)
                self.assertEqual((rc, result["data"]["status"], result["data"]["reason"]), (0, "blocked", code))
                tid = result["data"]["task_id"]
                self.assertEqual(len(list((config.runtime_dir() / tid).rglob("capture.json"))), int(bool(diagnostic)))

    def test_stop_and_timeout_terminate_entire_process_group(self):
        os.environ["CURSOR_FIXTURE_MODE"] = "sleep"
        for stop in (True, False):
            with self.subTest(stop=stop):
                runtime = self.root / ("stopped" if stop else "timed-out")
                ready = runtime / "pids.json"
                req = replace(self.req, runtime_dir=runtime, timeout_seconds=5 if stop else 1,
                              stop_check=(lambda: ready.exists()) if stop else None)
                try:
                    started = time.monotonic()
                    result = cursor.run_worker(req)
                    self.assertEqual((result.status, result.blocker), ("blocked", "STOPPED" if stop else "TIMEOUT"))
                    self.assertLess(time.monotonic() - started, 5)
                    pids = json.loads(ready.read_text())
                    deadline = time.monotonic() + 2
                    while any(process.identity(pid) is not None for pid in pids) and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertTrue(all(process.identity(pid) is None for pid in pids))
                finally:
                    if ready.exists():
                        # Fixture-only cleanup if an assertion exposes a termination regression.
                        for pid in json.loads(ready.read_text()):
                            if process.identity(pid):
                                try:
                                    os.kill(pid, signal.SIGKILL)
                                except ProcessLookupError:
                                    pass


if __name__ == "__main__":
    unittest.main()
