"""Trusted, in-process agent registration and capability negotiation.

Registration is code-owned; task files never contain import paths or commands.
"""
from dataclasses import dataclass, field
from enum import Enum
import math
from pathlib import Path
import re
from typing import FrozenSet, Mapping, Protocol
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


# Version 1 remains the public compatibility surface used by persisted legacy
# records and existing adapters. The v2 types below are the neutral session
# contract for new callers; they deliberately do not infer authentication,
# activity, or lifecycle support from a CLI being installed.
SESSION_CAPABILITY_NAMES = (
    'session.verify',
    'session.create',
    'session.transport',
    'session.observe',
    'session.activity',
    'session.cancel',
    'session.load_resume',
    'actor.authenticate',
)
SESSION_EVENT_KINDS = frozenset({
    'session_started', 'session_stopped', 'activity', 'message_received',
    'message_accepted', 'response_started', 'response_completed', 'turn_completed',
    'tool_started', 'tool_completed',
})


class CapabilityState(str, Enum):
    SUPPORTED = 'supported'
    UNSUPPORTED = 'unsupported'
    UNKNOWN = 'unknown'


class CapabilityUnavailable(ValueError):
    """A requested operation was not explicitly declared as supported."""

    def __init__(self, adapter, capability, state):
        self.adapter = adapter
        self.capability = capability
        self.state = CapabilityState(state)
        super().__init__(f'{adapter} capability {capability} is {self.state.value}')


@dataclass(frozen=True)
class SessionCapability:
    name: str
    state: CapabilityState | str
    evidence: str = ''
    version: int = 2

    def __post_init__(self):
        if self.version != 2 or self.name not in SESSION_CAPABILITY_NAMES:
            raise ValueError('invalid versioned session capability')
        try:
            state = CapabilityState(self.state)
        except (TypeError, ValueError):
            raise ValueError('invalid capability state') from None
        object.__setattr__(self, 'state', state)
        if state != CapabilityState.UNKNOWN and (not isinstance(self.evidence, str) or not self.evidence.strip()):
            raise ValueError('known capability states require code-owned evidence')
        if not isinstance(self.evidence, str):
            raise ValueError('capability evidence must be text')


@dataclass(frozen=True)
class SessionCapabilities:
    """Code-owned capability declaration; absent entries remain unknown."""

    adapter: str
    entries: tuple[SessionCapability, ...] | Mapping[str, object] = ()
    version: int = 2

    def __post_init__(self):
        if self.version != 2 or not isinstance(self.adapter, str) or not self.adapter.strip():
            raise ValueError('invalid session capability declaration')
        raw = self.entries
        if isinstance(raw, Mapping):
            parsed = []
            for name, value in raw.items():
                if isinstance(value, SessionCapability):
                    entry = value
                    if entry.name != name:
                        raise ValueError('capability mapping key does not match entry name')
                elif isinstance(value, Mapping):
                    entry = SessionCapability(name, value.get('state', 'unknown'),
                                              value.get('evidence', ''), value.get('version', 2))
                else:
                    entry = SessionCapability(name, value)
                parsed.append(entry)
            raw = tuple(parsed)
        else:
            raw = tuple(raw)
        if any(not isinstance(entry, SessionCapability) for entry in raw):
            raise ValueError('session capabilities must contain v2 entries')
        names = [entry.name for entry in raw]
        if len(names) != len(set(names)):
            raise ValueError('duplicate session capability')
        # Materialize every known capability so serialization and lookup are
        # stable. Missing capabilities are explicitly unknown, never false.
        by_name = {entry.name: entry for entry in raw}
        ordered = tuple(by_name.get(name, SessionCapability(name, CapabilityState.UNKNOWN))
                        for name in SESSION_CAPABILITY_NAMES)
        object.__setattr__(self, 'entries', ordered)

    def state(self, name):
        if name not in SESSION_CAPABILITY_NAMES:
            raise ValueError('unknown session capability: '+str(name))
        return next(entry.state for entry in self.entries if entry.name == name)

    def require(self, name):
        state = self.state(name)
        if state != CapabilityState.SUPPORTED:
            raise CapabilityUnavailable(self.adapter, name, state)
        return next(entry for entry in self.entries if entry.name == name)

    def to_mapping(self):
        return dict(version=self.version, adapter=self.adapter, capabilities={
            entry.name: dict(state=entry.state.value, evidence=entry.evidence)
            for entry in self.entries})

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping) or value.get('version') != 2:
            raise ValueError('session capabilities require version 2')
        entries = value.get('capabilities')
        if not isinstance(entries, Mapping):
            raise ValueError('session capabilities mapping is missing')
        return cls(value.get('adapter'), entries, value['version'])


@dataclass(frozen=True)
class SessionIdentity:
    """Verified native identity plus the exact project and provenance."""

    adapter: str
    native_id: str
    project: str
    provenance: str
    version: int = 2

    def __post_init__(self):
        values = (self.adapter, self.native_id, self.project, self.provenance)
        if (self.version != 2 or not all(isinstance(value, str) and value.strip() for value in values)
                or not Path(self.project).is_absolute()):
            raise ValueError('invalid versioned session identity')

    def to_mapping(self):
        return dict(version=self.version, adapter=self.adapter, native_id=self.native_id,
                    project=self.project, provenance=self.provenance)

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping):
            raise ValueError('session identity must be an object')
        return cls(value.get('adapter'), value.get('native_id'), value.get('project'),
                   value.get('provenance'), value.get('version', 0))


@dataclass(frozen=True)
class SessionReference:
    """Opaque ATO reference bound to a verified native identity."""

    reference: str
    identity: SessionIdentity
    version: int = 2

    def __post_init__(self):
        if (self.version != 2 or not isinstance(self.reference, str)
                or not re.fullmatch(r'agentref-[0-9a-f]{32}', self.reference)
                or not isinstance(self.identity, SessionIdentity)):
            raise ValueError('invalid versioned session reference')

    def to_mapping(self):
        return dict(version=self.version, reference=self.reference, identity=self.identity.to_mapping())

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping) or value.get('version') != 2:
            raise ValueError('session reference requires version 2')
        return cls(value.get('reference'), SessionIdentity.from_mapping(value.get('identity', {})),
                   value['version'])


class ActivityState(str, Enum):
    ACTIVE = 'active'
    IDLE = 'idle'
    UNKNOWN = 'unknown'


@dataclass(frozen=True)
class SessionActivity:
    session: SessionIdentity
    state: ActivityState | str
    source: str
    observed_at: float
    generation: int
    turn_id: str = ''
    version: int = 2

    def __post_init__(self):
        try:
            state = ActivityState(self.state)
        except (TypeError, ValueError):
            raise ValueError('invalid activity state') from None
        object.__setattr__(self, 'state', state)
        if (self.version != 2 or not isinstance(self.session, SessionIdentity)
                or not isinstance(self.source, str) or not self.source.strip()
                or isinstance(self.observed_at, bool) or not isinstance(self.observed_at, (int, float))
                or not math.isfinite(self.observed_at) or type(self.generation) is not int
                or self.generation < 0 or not isinstance(self.turn_id, str)):
            raise ValueError('invalid versioned session activity')

    def to_mapping(self):
        return dict(version=self.version, session=self.session.to_mapping(), state=self.state.value,
                    source=self.source, observed_at=self.observed_at, generation=self.generation,
                    turn_id=self.turn_id)

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping) or value.get('version') != 2:
            raise ValueError('session activity requires version 2')
        return cls(SessionIdentity.from_mapping(value.get('session', {})), value.get('state'),
                   value.get('source', ''), value.get('observed_at'), value.get('generation'),
                   value.get('turn_id', ''), value['version'])


@dataclass(frozen=True)
class SessionEvent:
    """Neutral correlated activity; a completed turn is not workflow acceptance."""

    event_id: str
    session: SessionIdentity
    correlation_id: str
    generation: int
    kind: str
    source: str
    observed_at: float
    turn_id: str = ''
    message_id: str = ''
    text: str = ''
    version: int = 2

    def __post_init__(self):
        if (self.version != 2 or not isinstance(self.session, SessionIdentity)
                or not all(isinstance(value, str) and value.strip() for value in
                           (self.event_id, self.correlation_id, self.kind, self.source))
                or self.kind not in SESSION_EVENT_KINDS
                or type(self.generation) is not int or self.generation < 0
                or isinstance(self.observed_at, bool) or not isinstance(self.observed_at, (int, float))
                or not math.isfinite(self.observed_at)
                or not all(isinstance(value, str) for value in (self.turn_id, self.message_id, self.text))):
            raise ValueError('invalid versioned session event')

    def to_mapping(self):
        return dict(version=self.version, event_id=self.event_id, session=self.session.to_mapping(),
                    correlation_id=self.correlation_id, generation=self.generation, kind=self.kind,
                    source=self.source, observed_at=self.observed_at, turn_id=self.turn_id,
                    message_id=self.message_id, text=self.text)

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping) or value.get('version') != 2:
            raise ValueError('session event requires version 2')
        return cls(value.get('event_id'), SessionIdentity.from_mapping(value.get('session', {})),
                   value.get('correlation_id'), value.get('generation'), value.get('kind'),
                   value.get('source'), value.get('observed_at'), value.get('turn_id', ''),
                   value.get('message_id', ''), value.get('text', ''), value['version'])


class ActorRole(str, Enum):
    USER = 'user'
    HUB = 'hub'
    ORIGIN = 'origin'
    WORKER = 'worker'
    SESSION_HOST = 'session_host'


@dataclass(frozen=True)
class ActorProof:
    """Opaque credential locator and challenge; never persist secret material."""

    method: str
    credential_ref: str
    challenge: str
    version: int = 2

    def __post_init__(self):
        if (self.version != 2 or not all(isinstance(value, str) and value.strip()
                                         for value in (self.method, self.credential_ref, self.challenge))):
            raise ValueError('actor proof requires an explicit v2 credential challenge')


@dataclass(frozen=True)
class ActorContext:
    """Authenticated actor bound to a session, project, contract and generation."""

    actor_id: str
    role: ActorRole | str
    session: SessionReference
    project: str
    workflow_id: str
    contract_hash: str
    generation: int
    authentication_source: str
    version: int = 2
    _seal: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        try:
            role = ActorRole(self.role)
        except (TypeError, ValueError):
            raise ValueError('invalid actor role') from None
        object.__setattr__(self, 'role', role)
        if (self.version != 2 or self._seal is not _ACTOR_CONTEXT_SEAL
                or not isinstance(self.actor_id, str) or not self.actor_id.strip()
                or not isinstance(self.session, SessionReference)
                or not isinstance(self.project, str) or not Path(self.project).is_absolute()
                or self.project != self.session.identity.project
                or not isinstance(self.workflow_id, str) or not self.workflow_id.strip()
                or not isinstance(self.contract_hash, str) or not self.contract_hash.strip()
                or type(self.generation) is not int or self.generation < 0
                or not isinstance(self.authentication_source, str) or not self.authentication_source.strip()):
            raise ValueError('invalid authenticated actor context')

    @classmethod
    def _from_verified_adapter(cls, actor_id, role, session, project, workflow_id,
                               contract_hash, generation, authentication_source):
        """Internal constructor used only after a trusted adapter proof succeeds."""
        return cls(actor_id, role, session, project, workflow_id, contract_hash,
                   generation, authentication_source, 2, _ACTOR_CONTEXT_SEAL)

    def to_mapping(self):
        """Return audit claims only; this mapping cannot be rehydrated as authority."""
        return dict(version=self.version, actor_id=self.actor_id, role=self.role.value,
                    session=self.session.to_mapping(), project=self.project,
                    workflow_id=self.workflow_id, contract_hash=self.contract_hash,
                    generation=self.generation, authentication_source=self.authentication_source)


_ACTOR_ACTIONS = {
    ActorRole.USER: frozenset({'decision.answer', 'decision.approve', 'decision.accept',
                               'workflow.stop', 'workflow.close', 'criteria.acknowledge'}),
    ActorRole.HUB: frozenset({'interaction.present', 'interaction.inbox', 'responsibility.claim', 'responsibility.disposition'}),
    ActorRole.ORIGIN: frozenset({'question.ask', 'answer.accept', 'workflow.report', 'recovery.respond',
                                 'responsibility.claim', 'responsibility.disposition'}),
    ActorRole.WORKER: frozenset({'progress.report', 'task.result'}),
    ActorRole.SESSION_HOST: frozenset({'session.transport', 'session.observe', 'session.cancel_owned',
                                       'session.resume_owned'}),
}
_ACTOR_CONTEXT_SEAL = object()


def authorize_actor(actor, action, *, project, workflow_id, contract_hash, generation):
    """Fail closed on stale bindings and keep human decisions user-only."""
    if not isinstance(actor, ActorContext) or actor._seal is not _ACTOR_CONTEXT_SEAL:
        raise ValueError('operation requires an authenticated v2 actor context')
    if (actor.project != str(Path(project).resolve()) or actor.workflow_id != workflow_id
            or actor.contract_hash != contract_hash or actor.generation != generation):
        raise ValueError('actor project, contract or generation binding mismatch')
    if action not in _ACTOR_ACTIONS[actor.role]:
        raise ValueError('actor role is not authorized for '+str(action))
    return True


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


def session_capabilities(kind):
    """Return the adapter's v2 declaration without upgrading absence to denial.

    Existing v1 registrations are extended only when a concrete method and its
    old code-owned capability marker both exist. Actor authentication is never
    inferred from the legacy ``is_actor`` hook because it may rely on ambient
    environment state; adapters must make a v2 declaration explicitly.
    """
    adapter = get(kind)
    declared = getattr(adapter, 'session_capabilities_v2', None)
    if declared is not None:
        if isinstance(declared, SessionCapabilities):
            if declared.adapter != kind:
                raise ValueError('session capabilities belong to another adapter')
            result = declared
        elif isinstance(declared, Mapping) and 'capabilities' in declared:
            result = SessionCapabilities.from_mapping(declared)
            if result.adapter != kind:
                raise ValueError('session capabilities belong to another adapter')
        else:
            result = SessionCapabilities(kind, declared)
        methods = {
            'session.verify': ('verify_session',),
            'session.create': ('create_session',),
            'session.transport': ('send',),
            'session.observe': ('observe_session', 'session_cursor', 'observe_session_v2'),
            'session.activity': ('session_activity',),
            'session.cancel': ('cancel_session',),
            'session.load_resume': ('load_or_resume_session',),
            'actor.authenticate': ('authenticate_actor',),
        }
        for name, method_names in methods.items():
            if name == 'session.observe':
                implemented = (callable(getattr(adapter, 'observe_session_v2', None))
                               or (callable(getattr(adapter, 'observe_session', None))
                                   and callable(getattr(adapter, 'session_cursor', None))))
            else:
                implemented = any(callable(getattr(adapter, method, None)) for method in method_names)
            if (result.state(name) == CapabilityState.SUPPORTED
                    and not implemented):
                raise ValueError(f'{kind} declares {name} supported without an implementation')
        return result

    legacy = getattr(adapter, 'capabilities', frozenset())
    rows = {}
    implementations = {
        'session.verify': ('session_verify', 'verify_session'),
        'session.create': ('session_create', 'create_session'),
        'session.transport': ('transport', 'send'),
        'session.observe': ('session_observe', 'observe_session'),
        'session.activity': ('session_activity', 'session_activity'),
        'session.cancel': ('session_cancel', 'cancel_session'),
        'session.load_resume': ('session_load_resume', 'load_or_resume_session'),
    }
    for name, (legacy_name, method_name) in implementations.items():
        method_present = callable(getattr(adapter, method_name, None))
        if name == 'session.observe':
            method_present = (method_present and callable(getattr(adapter, 'session_cursor', None)))
        if legacy_name in legacy and method_present:
            rows[name] = dict(state='supported', evidence=
                              f'registered {legacy_name} declaration and {method_name} implementation')
    return SessionCapabilities(kind, rows)


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


def runtime_entrypoint():
    """The code-owned bootstrap; never selected by a plan or environment."""
    return _runtime_entrypoint or str(Path(__file__).with_name('runner.py').resolve())


def runtime_command(*arguments):
    if len(arguments) >= 2 and arguments[0] in {
            'run', 'start', 'resume', '_worker', '_supervisor', '_dispatcher', 'host-checks', 'host-recheck'}:
        import runtime_selection
        selected = runtime_selection.command(arguments[1], *arguments)
        if selected is not None:
            return selected
    return [sys.executable, runtime_entrypoint(), *map(str, arguments)]
