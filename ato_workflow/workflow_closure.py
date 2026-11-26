"""Explicit user acceptance of current delivery; never rewrites automated evidence."""
import hashlib
import os
from pathlib import Path

from ato_workflow import interaction
from ato_common import ato_core as recovery
from ato_common import ato_core as core
from ato_workflow import session_router


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def actor(origin):
    hub = interaction.config() or {}
    who = session_router.current_actor_locator()
    if who not in (origin, hub.get('thread_id')):
        raise ValueError('user closure requires the bound origin or current hub')
    return who


def active_execution(state):
    if recovery.alive(state.get('owner_pid')):
        return True
    process = state.get('active_process')
    if not process:
        return False
    pid = process.get('pid')
    if type(pid) is not int or pid <= 0:
        return True  # Malformed execution identity is unknown, not idle.
    if recovery.alive(pid):
        return True
    try:
        os.killpg(pid, 0)
        return True  # The original leader may have exited with children remaining.
    except ProcessLookupError:
        return False
    except OSError:
        return True


def close(directory, evidence, source):
    d, state, contract, origin = interaction.binding(directory, require_open=False)
    who = actor(origin)
    if not source or not source.strip():
        raise ValueError('actual user decision source required')
    evidence = Path(evidence).resolve()
    data = core.read(evidence)
    if (data.get('decision') != 'accept_current_delivery_and_close'
            or str(d) not in data.get('workflows', []) or not data.get('user_text')
            or data.get('hub_thread') != (interaction.config() or {}).get('thread_id')):
        raise ValueError('user decision evidence does not match this workflow')
    with recovery.locked(Path(state['repo'])/'.codex/tasks/.worker.lock', blocking=False):
        state = core.read(d/'state.json')
        if active_execution(state):
            raise ValueError('cannot close an active executor')
        if state.get('active_run'):
            child = core.read(Path(state['active_run'])/'state.json')
            if active_execution(child):
                raise ValueError('cannot close an active stage')
        record = dict(version=1, completion_basis='user_acceptance', workflow=str(d),
            contract_sha256=sha(d/'contract.json'), origin_sha256=sha(d/'origin.json'),
            state_sha256=sha(d/'state.json'), evidence=str(evidence), evidence_sha256=sha(evidence),
            source=source, actor=who, origin=origin, hub=(interaction.config() or {}).get('thread_id'),
            raw_status=state['status'], unresolved_criteria=[k for k,v in state.get('criteria', {}).items()
                                                          if v.get('status') != 'passed'])
        path = d/'user-closure.json'
        if path.exists() and core.read(path) != record:
            raise ValueError('existing user closure differs; history is immutable')
        core.save(path, record)
    return record


def projection(directory, state):
    d = Path(directory).resolve()
    path = d/'user-closure.json'
    if not path.exists():
        return None
    try:
        row = core.read(path)
        if (row['workflow'] != str(d) or row['contract_sha256'] != sha(d/'contract.json')
                or row['origin_sha256'] != sha(d/'origin.json') or row['state_sha256'] != sha(d/'state.json')
                or row['evidence_sha256'] != sha(row['evidence'])):
            raise ValueError('user closure evidence or workflow has changed')
        return dict(raw_status=state['status'], effective_status='completed', validation='user_accepted',
                    completion_basis='user_acceptance', successor=None,
                    unresolved_criteria=row['unresolved_criteria'], automated_acceptance_complete=not row['unresolved_criteria'],
                    evidence=dict(path=row['evidence'], sha256=row['evidence_sha256']),
                    historical=dict(stop=(d/'STOP').exists()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return dict(raw_status=state['status'], effective_status=state['status'], validation='invalid',
                    completion_basis='user_acceptance', successor=None, error=str(exc),
                    historical=dict(stop=(d/'STOP').exists()))
