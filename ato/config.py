"""Local single-user paths, runtime settings, and submission origin detection."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import uuid


MANAGED_CLI_BIN = Path.home() / ".local/share/ctask-agent-clis/node_modules/.bin"


def ato_home() -> Path:
    raw = os.environ.get("ATO_HOME")
    return Path(raw).expanduser().resolve() if raw else (Path.home() / ".local/share/ato").resolve()


def state_dir() -> Path:
    return ato_home() / "v2"


def db_path() -> Path:
    return state_dir() / "ato.db"


def worktrees_dir() -> Path:
    return state_dir() / "worktrees"


def runtime_dir() -> Path:
    return state_dir() / "runtime"


def observer_state_path() -> Path:
    return state_dir() / "observer.json"


def observer_lock_path() -> Path:
    return state_dir() / "observer.lock"


def settings_path() -> Path:
    return state_dir() / "settings.json"


def codex_home() -> Path:
    raw = os.environ.get("CODEX_HOME")
    return Path(raw).expanduser().resolve() if raw else (Path.home() / ".codex").resolve()


def pi_agent_dir() -> Path:
    raw = os.environ.get("PI_CODING_AGENT_DIR")
    return Path(raw).expanduser().resolve() if raw else (Path.home() / ".pi/agent").resolve()


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def load_settings() -> dict:
    path = settings_path()
    defaults = {
        "notifications_enabled": True,
        "muted_repos": [],
        "codex_queue_bin": None,
        "time_policy": "advisory",
        "advisory_checks": True,
    }
    if not path.is_file():
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            defaults.update(data)
    except (OSError, ValueError):
        pass
    return defaults


def save_settings(values: dict) -> Path:
    ensure_private_dir(state_dir())
    path = settings_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def is_repo_muted(repo_root: str, settings: dict | None = None) -> bool:
    cfg = load_settings() if settings is None else settings
    try:
        target = str(Path(repo_root).resolve())
    except OSError:
        target = repo_root
    for entry in cfg.get("muted_repos") or []:
        raw = entry.get("repo") if isinstance(entry, dict) else entry
        if isinstance(raw, str):
            try:
                if str(Path(raw).resolve()) == target:
                    return True
            except OSError:
                if raw == target:
                    return True
    return False


def detect_submission_origin(env: dict[str, str] | None = None) -> tuple[str, str | None]:
    """Return (origin_kind, origin_session_id) with AGY taking precedence over ambient Codex vars."""
    environ = os.environ if env is None else env
    kind_env = (environ.get("ATO_SUBMISSION_KIND") or "").strip().lower()
    if kind_env == "agy" or (not kind_env and environ.get("ANTIGRAVITY_CONVERSATION_ID")):
        return "agy", None
    thread = (environ.get("CODEX_THREAD_ID") or "").strip()
    if kind_env == "codex" or (not kind_env and thread):
        if thread:
            try:
                return "codex", str(uuid.UUID(thread))
            except ValueError:
                return "codex", None
        return "codex", None
    return "cli", None


def resolve_executable(kind: str, override: str | None = None) -> str | None:
    """Find an executable binary for codex/claude/agy/pi/node."""
    env_key = f"ATO_{kind.upper()}_BIN"
    legacy_key = f"CTASK_{kind.upper()}_BIN"
    candidate = override or os.environ.get(env_key) or os.environ.get(legacy_key)
    if candidate:
        found = shutil.which(candidate)
        return str(Path(found).resolve()) if found else None
    found = shutil.which(kind)
    if found:
        return str(Path(found).resolve())
    managed = MANAGED_CLI_BIN / kind
    if managed.is_file() and os.access(managed, os.X_OK):
        return str(managed.resolve())
    return None
