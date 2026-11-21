"""Read-only kernel and exact parent/stage identity proofs, ported from efficiency. No runtime changes."""
import os
from pathlib import Path
import runner as core

def process_view(pid, identity=None, *, group=False):
    result = dict(pid=pid, recorded_identity=identity, observed_identity=None,
                  alive=False, identity_verified=False, status='absent')
    if pid is None:
        return result
    if type(pid) is not int or pid <= 0:
        return dict(result, alive=None, status='unknown')
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        result['status'] = 'dead'
    except OSError:
        result.update(alive=None, status='unknown')
    else:
        observed = core.process_identity(pid)
        verified = bool(identity and observed == identity)
        result.update(alive=True, observed_identity=observed, identity_verified=verified,
                      status='verified_alive' if verified else 'identity_unverified')
    if group:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            result['group_alive'] = False
        except OSError:
            result['group_alive'] = None
        else:
            result['group_alive'] = True
    return result


def processes(state):
    active = state.get('active_process') or {}
    worker = process_view(active.get('pid'), active.get('identity'), group=bool(active))
    worker.update(phase=active.get('phase'), agent_kind=active.get('agent_kind'),
                  evidence=active.get('evidence'))
    return dict(owner=process_view(state.get('owner_pid'), state.get('owner_identity')), worker=worker)


def bound_stage(directory, row, parent_dir, parent_state):
    """Validate the durable parent/stage relation before joining two task views."""
    d, parent = Path(directory).resolve(), Path(parent_dir).resolve()
    state = core.read(d/'state.json')
    plan = core.read(d/'plan.json')
    binding = core.read(d/'parent.json')
    if (state.get('kind') == 'workflow'
            or Path(binding['workflow_dir']).resolve() != parent or binding['workflow_id'] != parent_state['id']
            or Path(state['repo']).resolve() != Path(parent_state['repo']).resolve()
            or d.parent != Path(state['repo']).resolve()/'.codex/tasks'
            or state['id'] != d.name or state['plan_hash'] != core.digest(plan)
            or Path(row['run_dir']).resolve() != d or row['covers'] != binding['covers']):
        raise ValueError('stage/parent binding differs')
    return state, plan
