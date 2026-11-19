"""Explicit, verified native session references for trusted registered adapters.

Legacy Codex references remain owned by interaction/recovery. This module
never discovers a session, loads plugins, starts one, or grants tools.
"""
import os
from pathlib import Path
import re
import uuid

import agent_registry


def is_reference(value):
    return isinstance(value, str) and bool(re.fullmatch(r'agentref-[0-9a-f]{32}', value))


def _path(reference):
    if not is_reference(reference):
        raise ValueError('invalid agent session reference')
    import interaction
    return interaction.directory() / 'agent-sessions' / (reference + '.json')


def register(kind, identity, project, source):
    """Register only after the trusted adapter verifies the exact session/project."""
    import runner
    if not all(isinstance(x, str) and x.strip() for x in (kind, identity, source)):
        raise ValueError('session registration requires kind, identity and source')
    project = str(Path(project).resolve(strict=True))
    adapter = agent_registry.get(kind, 'session_verify')
    if not callable(getattr(adapter, 'verify_session', None)):
        raise ValueError('adapter has no session verification implementation')
    if adapter.verify_session(identity, project) is not True:
        raise ValueError('native session identity or project could not be verified')
    reference = 'agentref-' + uuid.uuid4().hex
    path = _path(reference)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    runner.save(path, dict(version=1, reference=reference, kind=kind, identity=identity,
                           project=project, source=source))
    return reference


def resolve(reference, project=None, capability='session_verify'):
    import runner
    row = runner.read(_path(reference))
    if row.get('version') != 1 or row.get('reference') != reference:
        raise ValueError('invalid stored session reference')
    if project is not None and row['project'] != str(Path(project).resolve()):
        raise ValueError('session project mismatch')
    adapter = agent_registry.get(row['kind'], capability)
    verifier = agent_registry.get(row['kind'], 'session_verify')
    if not callable(getattr(verifier, 'verify_session', None)) or verifier.verify_session(row['identity'], row['project']) is not True:
        raise ValueError('session verification failed')
    return row, adapter


def actor(reference):
    row, adapter = resolve(reference, capability='session_actor')
    if not callable(getattr(adapter, 'is_actor', None)) or adapter.is_actor(row['identity'], row['project']) is not True:
        raise ValueError('operation requires its verified bound session')


def current():
    reference = os.environ.get('CTASK_SESSION_REF')
    if reference:
        actor(reference)
    return reference


def preflight(reference):
    row, adapter = resolve(reference, capability='transport')
    if not callable(getattr(adapter, 'send', None)):
        raise ValueError('adapter has no session transport implementation')
    if ('session_observe' in adapter.capabilities and
            (not callable(getattr(adapter, 'session_cursor', None))
             or not callable(getattr(adapter, 'observe_session', None)))):
        raise ValueError('adapter has incomplete session observation implementation')
    return row, adapter


def send(reference, text):
    row, adapter = preflight(reference)
    from transport import DeliveryResult
    endpoint = dict(id=reference, kind=row['kind'], session_id=row['identity'], project=row['project'])
    result = adapter.send(endpoint, dict(body=text))
    if not isinstance(result, DeliveryResult):
        raise ValueError('invalid session transport result')
    return result


def cursor(reference):
    row, adapter = resolve(reference, capability='session_observe')
    if not callable(getattr(adapter, 'session_cursor', None)):
        raise ValueError('adapter has no session cursor implementation')
    value = adapter.session_cursor(row['identity'], row['project'])
    import json
    if value is None:
        raise ValueError('session observation requires a concrete cursor')
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        raise ValueError('session cursor must be finite JSON data') from None
    return value


def observe(reference, correlation_id, cursor):
    """Read only adapter-provided events for the exact registered session/turn."""
    row, adapter = resolve(reference, capability='session_observe')
    if not callable(getattr(adapter, 'observe_session', None)):
        raise ValueError('adapter has no session observation implementation')
    events, next_cursor = adapter.observe_session(row['identity'], row['project'], correlation_id, cursor)
    if not isinstance(events, list):
        raise ValueError('adapter returned invalid session events')
    for event in events:
        if (not isinstance(event, agent_registry.AgentEvent) or event.version != 1
                or event.session.kind != row['kind'] or event.session.identity != row['identity']
                or str(Path(event.session.project).resolve()) != row['project']
                or event.correlation_id != correlation_id or not event.source.strip()
                or event.kind not in ('received', 'responded', 'completed')
                or not isinstance(event.turn_id, str) or not isinstance(event.text, str)):
            raise ValueError('session event identity or correlation mismatch')
    return events, next_cursor
