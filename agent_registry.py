"""Trusted, in-process agent registration and capability negotiation.

Registration is code-owned; task files never contain import paths or commands.
"""
from dataclasses import dataclass
from typing import FrozenSet, Protocol
from pathlib import Path
import sys


@dataclass(frozen=True)
class ExecutionRequest:
    plan: dict
    directory: object
    label: str
    schema: object
    output: object
    sandbox: str
    planner: bool


@dataclass(frozen=True)
class SessionRef:
    kind: str
    identity: str
    project: str
    version: int = 1

    def __post_init__(self):
        if self.version != 1 or not all(isinstance(v, str) and v.strip() for v in
                                        (self.kind, self.identity, self.project)):
            raise ValueError('invalid session reference')


@dataclass(frozen=True)
class AgentEvent:
    kind: str
    session: SessionRef
    correlation_id: str
    source: str
    version: int = 1
    turn_id: str = ''
    text: str = ''


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    summary: str
    next_step: str = ''
    blocker: str = ''
    retry_after_seconds: int = 30
    version: int = 1

    @classmethod
    def from_mapping(cls, value):
        allowed = ('complete', 'continue', 'blocked', 'waiting', 'ready_for_checks', 'needs_host')
        if not isinstance(value, dict) or value.get('status') not in allowed:
            raise ValueError('missing/invalid structured outcome')
        retry = 30
        if value['status'] == 'waiting':
            try:
                retry = int(value.get('retry_after_seconds', 30))
            except (ValueError, TypeError, OverflowError):
                raise ValueError('invalid retry interval') from None
        return cls(value['status'], str(value.get('summary', '')), str(value.get('next_step', '')),
                   str(value.get('blocker') or ''), retry)


@dataclass(frozen=True)
class CreatedSession:
    identity: str
    project: str
    sandbox: str
    approval: str
    source: str
    legacy_rollout: str | None = None


class Adapter(Protocol):
    kind: str
    capabilities: FrozenSet[str]
    def command(self, request: ExecutionRequest) -> list[str]: ...


_adapters = {}
_runtime_entrypoint = None


def register(adapter):
    """Register a trusted adapter explicitly, never from task-controlled input."""
    kind = adapter.kind
    if not isinstance(kind, str) or not kind or kind in _adapters:
        raise ValueError('invalid or duplicate agent registration')
    if not isinstance(adapter.capabilities, frozenset):
        raise ValueError('capabilities must be an immutable set')
    _adapters[kind] = adapter


def get(kind, capability=None):
    adapter = _adapters.get(kind)
    if adapter is None:
        raise ValueError('unknown agent: '+str(kind))
    if capability is not None and capability not in adapter.capabilities:
        raise ValueError(f'{kind} does not support {capability}')
    return adapter


def kinds():
    return tuple(_adapters)


def set_runtime_entrypoint(script):
    """Set a trusted bootstrap for child runtime processes, from code only.

    This never reads a plan, environment variable, or dynamic import path.
    A bootstrap is responsible for registering the same adapters on startup.
    """
    global _runtime_entrypoint
    path = Path(script)
    if not path.is_absolute() or not path.is_file() or path.suffix != '.py':
        raise ValueError('trusted runtime entrypoint must be an existing absolute Python script')
    if _runtime_entrypoint is not None and _runtime_entrypoint != str(path.resolve()):
        raise ValueError('runtime entrypoint already set')
    _runtime_entrypoint = str(path.resolve())


def runtime_command(*arguments):
    script = _runtime_entrypoint or str(Path(__file__).with_name('runner.py').resolve())
    return [sys.executable, script, *map(str, arguments)]
