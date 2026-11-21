"""Once-only notices for newly watched terminal failures without a live handler."""
import hashlib
from pathlib import Path

import interaction
import recovery
import ato_core as core


def policy():
    path = interaction.directory()/'failure-alerts.json'
    return core.read(path) if path.exists() else {}


def configure(source):
    hub = interaction.config() or {}
    interaction.actor(hub.get('thread_id'))
    if not source or not source.strip() or not hub.get('thread_id'):
        raise ValueError('failure alerts require a verified hub and actual user source')
    path = interaction.directory()/'failure-alerts.json'
    with recovery.locked(interaction.directory()/'failure-alerts.lock'):
        prior = policy()
        if prior.get('hub_thread') == hub['thread_id']:
            return prior
        row = dict(enabled=True, hub_thread=hub['thread_id'], configured_at=core.now(), source=source)
        core.save(path, row)
        return row


def origin_busy(origin):
    """A live origin turn may own host work while the workflow owner is empty."""
    try:
        import session_router
        return session_router.activity(origin) != 'idle'
    except (OSError, ValueError, KeyError, TypeError):
        return True  # Unavailable activity evidence cannot establish no handler.


def current(action):
    try:
        settings = policy()
        if not settings.get('enabled') or settings.get('hub_thread') != action.get('hub'):
            return False
        d, s, _, origin = interaction.binding(action['workflow'])
        if action.get('kind') != 'failure_attention' or origin != action['origin']:
            return False
        if s['status'] not in {'blocked', 'error', 'failed', 'cleanup_blocked', 'permission_blocked', 'stage_limit', 'planning_limit'}:
            return False
        if recovery.alive(s.get('owner_pid')) or recovery.generation(d, s) != action['baseline_generation']:
            return False
        import workflow_lineage
        if workflow_lineage.delivery(d, s)['effective_status'] == 'completed':
            return False
        for name, digest in action['failure_evidence'].items():
            if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
                return False
        child = core.read(Path(s['active_run'])/'state.json') if s.get('active_run') else s
        import workflow_closure
        if workflow_closure.active_execution(child):
            return False
        if (child.get('status') == 'completed'
                and (s.get('error') or '').startswith('Selected host recheck passed;')):
            return False  # Awaiting normal origin continuation, not a new failure.
        # A terminal failure still needs one notice when native activity cannot
        # be observed. This is uncertainty to report, never an idle proof or a
        # recovery grant. A pending unknown delivery remains a no-replay barrier.
        ledger = core.read(d/'recovery.json') if (d/'recovery.json').exists() else {}
        latest = (ledger.get('requests') or [{}])[-1]
        if latest.get('state') in ('dispatching', 'delivery_unknown'):
            return False
        handler = recovery.handler_status(d, s, origin)
        import session_router
        if handler != 'unhandled' and not (handler == 'activity_unknown'
                and session_router.legacy_failure_notice_compatible(origin)):
            return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def observe(row, d, state, contract):
    import control_store as control
    settings = policy()
    # Do not flood the hub with old historical tasks when opting into this policy.
    if not settings.get('enabled') or row.get('registered_at', 0) < settings['configured_at']:
        return
    generation = recovery.generation(d, state)
    child = core.read(Path(state['active_run'])/'state.json') if state.get('active_run') else state
    # Host accounting can advance generation without introducing a new failure.
    # Only a different failed attempt/check/plan is a new notification episode.
    episode = dict(status=state['status'], active_run=state.get('active_run'),
        planning_attempts=state.get('planning_attempts'), error=core.digest(state.get('error')),
        criteria={k:dict(status=v.get('status'), evidence=v.get('evidence')) for k,v in state.get('criteria',{}).items()},
        tasks={k:{n:v.get(n) for n in ('status','attempts','attempt_sequence','evidence')}
               for k,v in child.get('tasks',{}).items()})
    identifier = 'failure-'+core.digest([str(d), state['contract_hash'], episode])[:20]
    paths = [d/'state.json', d/'contract.json', d/'origin.json']
    if state.get('active_run'):
        paths.append(Path(state['active_run'])/'state.json')
    action = dict(id=identifier, kind='failure_attention', workflow=str(d), origin=row['origin'], hub=row['hub'],
        state='unpresented', created_at=core.now(), reminders=0, objective=contract['objective'],
        baseline_generation=generation, policy_source=settings['source'],
        failure_evidence={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        handler_status=recovery.handler_status(d, state, row['origin']),
        question='任务已失败或阻塞，未发现已验证的活动执行器或有效接管者；原会话活动可能未知，不能据此认定闲置。请主控核对失败及活动证据并确定处理路径；不自动清零尝试、扩大权限或重放业务。')
    if not current(action):
        return
    with control.database() as con, con:
        old = con.execute('select body from actions where id=?', (identifier,)).fetchone()
        if not old:
            control.put(con, action)
        else:
            import json
            previous = json.loads(old[0])
            if (not previous.get('notice') and not previous.get('presented_at')
                    and previous.get('state') not in {'resolved', 'stopped', 'completed', 'superseded'}):
                # Accounting/heartbeat changes can invalidate file checksums
                # without creating a new failed attempt. Refresh the pending
                # evidence under the same episode ID; never send a second notice.
                action['created_at'] = previous['created_at']
                control.put(con, action)


def pending(action):
    return (not action.get('presented_at') and not action.get('notice')
            and action.get('state') not in {'resolved','stopped','completed','superseded'} and current(action))
