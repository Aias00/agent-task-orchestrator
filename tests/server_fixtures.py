"""Synthetic transport fixtures; never use installed ATO state or a real provider."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ato.server.config import ServerConfig
from ato.server.service import Service
from ato.spec import new_id, normalize_inline_spec
from ato.store import Store
from test_core_and_store import init_repo

BOT_TOKEN = "synthetic-bot-credential-" + "b" * 32
HUMAN_TOKEN = "synthetic-human-credential-" + "h" * 32
READER_TOKEN = "synthetic-reader-credential-" + "r" * 32


class ServerFixture:
    def setUp(self):
        self.tmp = self.enterContext(TemporaryDirectory(prefix="ato-server-test-"))
        self.root = Path(self.tmp).resolve()
        self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "PATH": os.defpath,
            "ATO_HOME": str(self.root / "ato"), "CODEX_HOME": str(self.root / "codex"),
            "ATO_SERVER_TOKEN_BOT": BOT_TOKEN, "ATO_SERVER_TOKEN_HUMAN": HUMAN_TOKEN,
            "ATO_SERVER_TOKEN_READER": READER_TOKEN,
            "CODEX_THREAD_ID": "11111111-2222-3333-4444-555555555555",
        }, clear=True))
        self.repos = {name: init_repo(self.root / name) for name in ("demo", "other")}
        self.raw_config = {
            "repositories": {name: str(path) for name, path in self.repos.items()},
            "principals": {
                "bot": {"token_env": "ATO_SERVER_TOKEN_BOT", "repositories": ["demo"], "scopes": ["read", "control"]},
                "human": {"token_env": "ATO_SERVER_TOKEN_HUMAN", "repositories": ["demo"], "scopes": ["read", "human"]},
                "reader": {"token_env": "ATO_SERVER_TOKEN_READER", "repositories": ["other"], "scopes": ["read"]},
            },
            "allowed_hosts": ["testserver", "127.0.0.1:8765"],
            "allowed_origins": ["https://testserver"], "stdio_principal": "bot",
        }
        self.config_file = self.root / "server.json"
        self.config_file.write_text(json.dumps(self.raw_config))
        self.cfg = ServerConfig.load(self.config_file)
        self.db = self.root / "ato/v2/ato.db"
        self.store = Store(self.db)
        self.service = Service(self.cfg, self.db)
        self.bot, self.human = self.cfg.principals["bot"], self.cfg.principals["human"]
        # Every async dispatch must be captured; an accidental provider call fails loudly.
        self.spawn = self.enterContext(patch("ato.executor._spawn_background_run"))
        self.enterContext(patch("ato.executor.execute_run", side_effect=AssertionError("No worker execution in transport tests")))

    def spec(self, alias="demo"):
        return {"schema_version": 1, "repo": alias, "base_ref": "HEAD", "worker_contract": {
            "objective": "Synthetic fixture", "scope": "app.py", "authorization": "Temporary local fixture only",
            "acceptance": [{"id": "whitespace", "kind": "command", "description": "Synthetic check",
                            "checks": [["git", "diff", "--check"]]}]}}

    def task(self, alias="demo"):
        norm = normalize_inline_spec(self.repos[alias], alias, self.spec(alias))
        task_id = new_id("task")
        return self.store.create_task(task_id=task_id, repo_root=norm.repository.root, repo_key=norm.repository.key,
                                     base_commit=norm.document["base_ref"], branch="ato/" + task_id,
                                     worktree_path=str(self.root / task_id), spec_doc=norm.document,
                                     spec_sha256=norm.spec_sha256, origin_kind="cli", origin_session_id=None)

    def call(self, operation, arguments=None, principal=None, origin="api"):
        return self.service.call(principal or self.bot, operation, arguments or {}, origin=origin)
