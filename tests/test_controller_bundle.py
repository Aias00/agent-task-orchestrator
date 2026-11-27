"""Distributed controller artifacts checked against the real service contract."""
import json
import os
from pathlib import Path
import re
import tomllib
import unittest
from unittest.mock import patch

from ato.server.config import ServerConfig
from ato.server.service import OPERATIONS, Service, validate_arguments
from server_fixtures import BOT_TOKEN, ServerFixture

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "skills/ato"
TEMPLATES = BUNDLE / "templates"


class ControllerBundleTests(ServerFixture, unittest.TestCase):
    def test_call_examples_match_manifest_and_validate_inline_spec(self):
        examples = json.loads((TEMPLATES / "controller-calls.example.json").read_text())
        self.assertEqual(set(examples), set(OPERATIONS))
        for operation, arguments in examples.items():
            with self.subTest(operation=operation):
                validate_arguments(operation, arguments)
        self.assertEqual(examples["validate"], {k: v for k, v in examples["submit"].items() if k != "request_id"})
        result = self.call("validate", examples["validate"])
        self.assertTrue(result["ok"], result)
        self.assertEqual(set(result["data"]), {"repo", "valid", "spec_sha256", "base_commit"})
        self.assertTrue(result["data"]["valid"])
        self.spawn.assert_not_called()

    def test_stdio_host_template_needs_no_human_credential(self):
        raw = json.loads((TEMPLATES / "server-stdio.example.json").read_text())
        raw["repositories"]["demo"] = str(self.repos["demo"])
        self.config_file.write_text(json.dumps(raw))
        with patch.dict(os.environ, {"ATO_SERVER_TOKEN_BOT": BOT_TOKEN, "PATH": os.defpath,
                                     "ATO_HOME": str(self.root / "ato"),
                                     "CODEX_HOME": str(self.root / "codex")}, clear=True):
            config = ServerConfig.load(self.config_file)
            principal = config.principals[config.stdio_principal]
            self.assertEqual(principal.scopes, {"read", "control"})
            self.assertEqual(config.token_envs, {"ATO_SERVER_TOKEN_BOT"})
            self.assertNotIn("ATO_SERVER_TOKEN_BOT", config.worker_environment())
            result = Service(config, self.db).call(principal, "repositories", {}, origin="mcp")
            self.assertEqual(result["data"], {"repositories": [{"alias": "demo"}]})

        http_config = TEMPLATES / "server-http.example.json"
        self.assertEqual(http_config.read_bytes(), (ROOT / "docs/server-config.example.json").read_bytes())
        raw = json.loads(http_config.read_text())
        raw["repositories"]["demo"] = str(self.repos["demo"])
        self.config_file.write_text(json.dumps(raw))
        config = ServerConfig.load(self.config_file)
        self.assertEqual(config.principals["operator"].scopes, {"read", "human"})
        self.assertEqual(config.principals["coordinator"].scopes, {"read", "control"})

    def test_client_templates_parse_and_keep_credentials_as_references(self):
        codex_stdio = tomllib.loads((TEMPLATES / "codex-stdio.toml").read_text())["mcp_servers"]["ato"]
        cursor_stdio = json.loads((TEMPLATES / "cursor-stdio.json").read_text())["mcpServers"]["ato"]
        self.assertEqual(cursor_stdio["type"], "stdio")
        for config in (codex_stdio, cursor_stdio):
            self.assertEqual(config["command"], "/srv/ato/venv/bin/ato")
            self.assertEqual(config["args"], ["mcp", "--stdio", "--config", "/srv/ato/server-stdio.json"])
            self.assertEqual(config["env"]["ATO_HOME"], "/srv/ato/state")
            self.assertEqual(config["env"]["CODEX_HOME"], "/srv/ato/codex")
        self.assertEqual(codex_stdio["env_vars"], ["ATO_SERVER_TOKEN_BOT"])
        self.assertEqual(cursor_stdio["env"]["ATO_SERVER_TOKEN_BOT"], "${env:ATO_SERVER_TOKEN_BOT}")
        codex_http = tomllib.loads((TEMPLATES / "codex-http.toml").read_text())["mcp_servers"]["ato"]
        cursor_http = json.loads((TEMPLATES / "cursor-http.json").read_text())["mcpServers"]["ato"]
        self.assertEqual(codex_http["url"], "https://ato.example.com/mcp")
        self.assertEqual(cursor_http["url"], codex_http["url"])
        self.assertEqual(codex_http["bearer_token_env_var"], "ATO_SERVER_TOKEN_BOT")
        self.assertEqual(cursor_http["headers"], {"Authorization": "Bearer ${env:ATO_SERVER_TOKEN_BOT}"})

    def test_wheel_manifest_contains_entire_skill_and_relative_links_resolve(self):
        config = tomllib.loads((ROOT / "pyproject.toml").read_text())
        self.assertEqual(config["project"]["dependencies"], [])
        destinations = config["tool"]["setuptools"]["data-files"]
        packaged = {}
        for destination, patterns in destinations.items():
            for pattern in patterns:
                for source in ROOT.glob(pattern):
                    packaged[source] = Path(destination) / source.name
        for source in BUNDLE.rglob("*"):
            if source.is_file():
                self.assertEqual(packaged.get(source),
                                 Path("share/agent-task-orchestrator/skills/ato") / source.relative_to(BUNDLE))
        for source in BUNDLE.rglob("*.md"):
            for target in re.findall(r"\]\(([^)]+)\)", source.read_text()):
                if "://" not in target:
                    self.assertTrue((source.parent / target.split("#")[0]).is_file(), (source, target))
