"""Tests for Pi/ACP derivation & PiCoordinator (2B), install.sh security, and acyclic package layout."""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from ato.adapters import pi_acp

ROOT = Path(__file__).resolve().parents[1]


class PiAcpInstallAndArchitectureTests(unittest.TestCase):
    def test_pi_acp_derivation_for_worker_and_coordinator_roles(self):
        if not pi_acp.PI_ACP_SOURCE.is_file():
            self.skipTest("optional pi-acp npm package not installed")
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            for role in ("worker", "coordinator"):
                out = pi_acp.derive_directory(base / f"derived-{role}", role=role)
                self.assertTrue(out.is_file())
                self.assertTrue((out.parent / "node_modules/@agentclientprotocol").is_symlink())

    def test_pi_path_and_coordinator_guards_with_node(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        with TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve() / "repo"
            repo.mkdir()
            (repo / "src.py").write_text("x = 1\n", encoding="utf-8")
            script = f"""
import {{allowed as workerAllowed}} from {json.dumps(str(ROOT / 'pi-path-guard.mjs'))};
import {{allowed as coordAllowed}} from {json.dumps(str(ROOT / 'pi-coordinator-guard.mjs'))};
const root = {json.dumps(str(repo))};
if (!workerAllowed(root, 'src.py', 'read', true)) process.exit(10);
if (workerAllowed(root, 'src.py', 'write', true)) process.exit(11);
if (workerAllowed(root, '.git/config', 'read', false)) process.exit(12);
if (workerAllowed(root, '../escape', 'read', false)) process.exit(13);
if (!coordAllowed(root, 'src.py', 'read', true)) process.exit(20);
if (coordAllowed(root, 'src.py', 'edit', true)) process.exit(21);
if (coordAllowed(root, '.env.local', 'read', false)) process.exit(22);
console.log('GUARDS_OK');
"""
            res = subprocess.run([node, "--input-type=module", "-e", script], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertIn("GUARDS_OK", res.stdout)

    def test_pi_coordinator_async_lifecycle_with_synthetic_acp_server(self):
        if importlib.util.find_spec("acp") is None:
            self.skipTest("optional agent-client-protocol package not installed")
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            repo = base / "repo"
            repo.mkdir()
            fake_server = base / "fake_acp.py"
            fake_server.write_text(
                """import json, sys
for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    rid, method = req.get("id"), req.get("method")
    if method == "initialize":
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {}}}
    elif method == "session/new":
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"sessionId": "pi-sess-1", "_meta": {"atoIdentity": {"nativeId": "pi-native-1"}}}}
    elif method == "session/prompt":
        turn = (req.get("params") or {}).get("_meta", {}).get("ctaskTurn")
        note = {"jsonrpc": "2.0", "method": "session/update", "params": {
            "sessionId": "pi-sess-1",
            "update": {"sessionUpdate": "session_info_update", "_meta": {"ctaskTurn": turn, "ctaskNativeReceipt": {"turn": turn, "stopReason": "stop", "result": "hello-from-pi"}}}
        }}
        sys.stdout.write(json.dumps(note) + "\\n")
        resp = {"jsonrpc": "2.0", "id": rid, "result": {"stopReason": "end_turn"}}
    else:
        resp = {"jsonrpc": "2.0", "id": rid, "result": {}}
    sys.stdout.write(json.dumps(resp) + "\\n")
    sys.stdout.flush()
""",
                encoding="utf-8",
            )

            async def exercise():
                coord = pi_acp.PiCoordinator(repo, sandbox="read-only", command_override=[sys.executable, str(fake_server)])
                try:
                    info = await coord.start()
                    self.assertEqual(info["session_id"], "pi-sess-1")
                    self.assertEqual(info["native_id"], "pi-native-1")
                    turn = await coord.prompt("say hi")
                    self.assertEqual(turn["transport_stop_reason"], "end_turn")
                    self.assertEqual(turn["native_stop_reason"], "stop")
                    self.assertEqual(turn["text"], "hello-from-pi")
                finally:
                    await coord.close()

            asyncio.run(exercise())

    def test_install_sh_syntax_decoy_defense_and_editable_install(self):
        sh_check = subprocess.run(["sh", "-n", str(ROOT / "install.sh")], capture_output=True, text=True)
        self.assertEqual(sh_check.returncode, 0, sh_check.stderr)

        # P1-8: piped `curl | sh` must ignore a decoy `./sh` file in CWD.
        with TemporaryDirectory() as tmp:
            cwd = Path(tmp).resolve()
            (cwd / "sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (cwd / "pyproject.toml").write_text("[project]\nname='decoy'\n", encoding="utf-8")
            (cwd / "ato").mkdir()
            env = dict(os.environ, PATH="/nonexistent", ATO_INSTALL_PYTHON=sys.executable)
            proc = subprocess.run(["/bin/sh"], input=(ROOT / "install.sh").read_text(encoding="utf-8"),
                                  cwd=cwd, env=env, capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)

        # Local editable install creates launcher + skill cleanly and runs `ato --version`.
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            bin_dir = base / "bin"
            venv_dir = base / "venv"
            skills_dir = base / "skills"
            env = dict(os.environ, ATO_INSTALL_PYTHON=sys.executable)
            res = subprocess.run(
                ["sh", str(ROOT / "install.sh"), "--editable",
                 "--bin-dir", str(bin_dir), "--venv-dir", str(venv_dir), "--skills-dir", str(skills_dir)],
                env=env, capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue((bin_dir / "ato").is_file())
            self.assertTrue((skills_dir / "ato/SKILL.md").is_file())
            ver = subprocess.run([str(bin_dir / "ato"), "--version"], capture_output=True, text=True)
            self.assertEqual(ver.returncode, 0, ver.stderr)
            self.assertIn("ato 2.0.0", ver.stdout)

        # Non-editable install also copies Pi/ACP asset manifests cleanly.
        with TemporaryDirectory() as tmp:
            base = Path(tmp).resolve()
            bin_dir = base / "bin"
            venv_dir = base / "venv"
            env = dict(os.environ, ATO_INSTALL_PYTHON=sys.executable)
            res = subprocess.run(
                ["sh", str(ROOT / "install.sh"), "--bin-dir", str(bin_dir), "--venv-dir", str(venv_dir)],
                env=env, capture_output=True, text=True,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue((venv_dir / "share/ato/pi-path-guard.mjs").is_file())
            self.assertTrue((venv_dir / "share/ato/integrations/pi-acp/compatibility.json").is_file())
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

        # Standard DFS cycle check on top-level imports.
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
