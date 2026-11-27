"""Tests for the lightweight Pi CLI worker, install.sh security, and acyclic package layout."""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ato.adapters import base, pi

ROOT = Path(__file__).resolve().parents[1]
DONE = {"status": "completed", "summary": "Finished", "blocker": "", "next_step": ""}

SANDBOX_SCRIPT = """import json, os, pathlib, sys
runtime = pathlib.Path(os.environ["TMPDIR"]).parent
(runtime / "sandbox-argv.json").write_text(json.dumps(sys.argv[1:]))
os.execv(sys.argv[3], sys.argv[3:])
"""

PI_SCRIPT = """import json, os, pathlib, sys
runtime = pathlib.Path(os.environ["TMPDIR"]).parent
prompt = sys.stdin.read()
(runtime / "capture.json").write_text(
    json.dumps({"argv": sys.argv[1:], "sandbox": os.environ.get("CTASK_AGENT_SANDBOX"), "prompt_len": len(prompt)})
)
mode = os.environ.get("PI_FIXTURE_MODE", "ok")
if mode == "auth":
    print("No API key found", file=sys.stderr)
    sys.exit(1)
if mode == "malformed":
    print(json.dumps({"type": "session", "id": "s1"}))
    sys.exit(0)
if mode == "nonzero":
    sys.exit(2)
result = json.loads(os.environ.get("PI_FIXTURE_RESULT", "{}"))
text = json.dumps(result)
lines = [
    {"type": "session", "id": "sess-1"},
    {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "stopReason": "stop",
            "content": [{"type": "text", "text": text}],
            "usage": {},
        },
    },
    {"type": "agent_end"},
]
for line in lines:
    print(json.dumps(line))
sys.exit(0)
"""


class PiWorkerAndInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(TemporaryDirectory(prefix="ato-pi-test-"))
        self.root = Path(self.tmp).resolve()
        self.worktree = self.root / "wt"
        self.worktree.mkdir()
        self.runtime = self.root / "runtime"
        self.exe = self._script("pi", PI_SCRIPT)
        self.sandbox = self._script("sandbox-exec", SANDBOX_SCRIPT)
        self.enterContext(patch.dict(os.environ, {"ATO_PI_BIN": str(self.exe)}, clear=False))
        self.enterContext(patch.object(pi, "SANDBOX_EXEC", self.sandbox))
        self.req = base.WorkerRequest("task-pi", "pi", self.worktree, self.runtime, "Implement the task")

    def _script(self, name: str, body: str) -> Path:
        path = self.root / name
        path.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
        path.chmod(0o700)
        return path

    def _run(self, *, sandbox: str = "workspace-write", env_extra: dict | None = None, req=None):
        req = req or base.WorkerRequest(
            self.req.task_id, self.req.kind, self.worktree, self.runtime, self.req.prompt, sandbox=sandbox
        )
        env = dict(os.environ, PI_FIXTURE_RESULT=json.dumps(DONE))
        if env_extra:
            env.update(env_extra)
        with patch.dict(os.environ, env, clear=False):
            return pi.run_worker(req)

    def test_pi_worker_completed(self):
        res = self._run()
        self.assertEqual(res.status, "completed")
        self.assertEqual(res.summary, "Finished")
        capture = json.loads((self.runtime / "capture.json").read_text())
        self.assertIn("--mode", capture["argv"])
        self.assertIn("json", capture["argv"])

    def test_pi_worker_executable_unavailable(self):
        with patch.dict(os.environ, {"ATO_PI_BIN": "/nonexistent/pi", "PATH": ""}, clear=True):
            res = pi.run_worker(self.req)
        self.assertEqual(res.status, "blocked")
        self.assertEqual(res.blocker, "EXECUTABLE_UNAVAILABLE")

    def test_pi_worker_auth_blocked(self):
        res = self._run(env_extra={"PI_FIXTURE_MODE": "auth"})
        self.assertEqual(res.status, "blocked")
        self.assertEqual(res.blocker, "AUTH_BLOCKED")

    def test_pi_worker_malformed_output(self):
        res = self._run(env_extra={"PI_FIXTURE_MODE": "malformed"})
        self.assertEqual(res.status, "failed")
        self.assertIn("invalid pi result", res.summary)

    def test_pi_sandbox_tools_read_only_vs_workspace_write(self):
        self._run(sandbox="read-only")
        ro = json.loads((self.runtime / "capture.json").read_text())
        idx = ro["argv"].index("--tools")
        self.assertEqual(ro["argv"][idx + 1], "read,grep,find,ls")
        prof = (self.runtime / "sandbox.sb").read_text()
        self.assertNotIn("workspace-write allow", prof)

        self.runtime = self.root / "runtime-ws"
        req = base.WorkerRequest(self.req.task_id, "pi", self.worktree, self.runtime, self.req.prompt)
        self._run(sandbox="workspace-write", req=req)
        ws = json.loads((self.runtime / "capture.json").read_text())
        idx = ws["argv"].index("--tools")
        self.assertEqual(ws["argv"][idx + 1], "read,grep,find,ls,edit,write")
        prof_ws = (self.runtime / "sandbox.sb").read_text()
        self.assertIn(str(self.worktree.resolve()), prof_ws)

    def test_pi_path_guard_with_node(self):
        guard = ROOT / "pi-path-guard.mjs"
        self.assertTrue(guard.is_file())
        node = shutil.which("node")
        if not node:
            return
        with TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve() / "repo"
            repo.mkdir()
            (repo / "src.py").write_text("x = 1\n", encoding="utf-8")
            script = f"""
import {{allowed as workerAllowed}} from {json.dumps(str(guard))};
const root = {json.dumps(str(repo))};
if (!workerAllowed(root, 'src.py', 'read', true)) process.exit(10);
if (workerAllowed(root, 'src.py', 'write', true)) process.exit(11);
if (workerAllowed(root, '.git/config', 'read', false)) process.exit(12);
if (workerAllowed(root, '../escape', 'read', false)) process.exit(13);
console.log('GUARDS_OK');
"""
            res = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("GUARDS_OK", res.stdout)

    def test_install_sh_syntax_decoy_defense_and_editable_install(self):
        sh_check = subprocess.run(["sh", "-n", str(ROOT / "install.sh")], capture_output=True, text=True)
        self.assertEqual(sh_check.returncode, 0, sh_check.stderr)

        with TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            (cwd / "sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (cwd / "pyproject.toml").write_text("[project]\nname='decoy'\n", encoding="utf-8")
            (cwd / "ato").mkdir()
            env = dict(os.environ, PATH="/nonexistent", ATO_INSTALL_PYTHON=sys.executable)
            proc = subprocess.run(
                ["/bin/sh"],
                input=(ROOT / "install.sh").read_text(encoding="utf-8"),
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)

        with TemporaryDirectory() as tmp:
            base_dir = Path(tmp).resolve()
            bin_dir = base_dir / "bin"
            venv_dir = base_dir / "venv"
            skills_dir = base_dir / "skills"
            env = dict(os.environ, ATO_INSTALL_PYTHON=sys.executable)
            res = subprocess.run(
                [
                    "sh",
                    str(ROOT / "install.sh"),
                    "--editable",
                    "--bin-dir",
                    str(bin_dir),
                    "--venv-dir",
                    str(venv_dir),
                    "--skills-dir",
                    str(skills_dir),
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue((bin_dir / "ato").is_file())
            self.assertTrue((skills_dir / "ato/SKILL.md").is_file())
            ver = subprocess.run([str(bin_dir / "ato"), "--version"], capture_output=True, text=True)
            self.assertEqual(ver.returncode, 0, ver.stderr)
            self.assertIn("ato 2.0.0", ver.stdout)

        with TemporaryDirectory() as tmp:
            base_dir = Path(tmp).resolve()
            bin_dir = base_dir / "bin"
            venv_dir = base_dir / "venv"
            env = dict(os.environ, ATO_INSTALL_PYTHON=sys.executable)
            res = subprocess.run(
                ["sh", str(ROOT / "install.sh"), "--bin-dir", str(bin_dir), "--venv-dir", str(venv_dir)],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue((venv_dir / "share/ato/pi-path-guard.mjs").is_file())
            self.assertFalse((venv_dir / "share/ato/integrations").exists())
            ver = subprocess.run([str(bin_dir / "ato"), "--version"], capture_output=True, text=True)
            self.assertEqual(ver.returncode, 0, ver.stderr)
            self.assertIn("ato 2.0.0", ver.stdout)

    def test_v2_package_has_no_circular_top_level_imports(self):
        graph: dict[str, set[str]] = {}
        pkg = ROOT / "ato"
        for py in sorted(pkg.rglob("*.py")):
            rel = py.relative_to(ROOT).with_suffix("")
            mod = ".".join(rel.parts[:-1] if rel.name == "__init__" else rel.parts)
            deps: set[str] = set()
            tree = ast.parse(py.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("ato"):
                    deps.add(node.module)
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("ato"):
                            deps.add(alias.name)
            graph[mod] = deps

        visiting, visited = set(), set()

        def dfs(node: str) -> None:
            visiting.add(node)
            for nxt in graph.get(node, ()):
                if nxt in visiting:
                    self.fail(f"Circular top-level import detected: {node} -> {nxt}")
                if nxt not in visited and nxt in graph:
                    dfs(nxt)
            visiting.remove(node)
            visited.add(node)

        for mod in graph:
            if mod not in visited:
                dfs(mod)


if __name__ == "__main__":
    unittest.main()