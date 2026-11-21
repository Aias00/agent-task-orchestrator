"""Once-only notices for newly watched terminal failures without a live handler."""
import hashlib
from pathlib import Path

import interaction
import recovery
import runner as core


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
    """Legacy activity projection; never a versioned workflow handling proof."""
    try:
        import session_router
        return session_router.activity(origin) != 'idle'
    except (OSError, ValueError, KeyError, TypeError):
        return True  # Unavailable activity evidence cannot establish no handler.


def _legacy_evaluate(action):
    """Preserve historical validity, while recording the exact failed predicate."""
    def result(code, valid=False, **fields):
        return dict(current=valid, reason_code=code,
                    obligation='unresolved' if valid else 'unknown', **fields)
    try:
        settings = policy()
        if not settings.get('enabled') or settings.get('hub_thread') != action.get('hub'):
            return result('legacy_failure_policy_binding_changed')
        d, state, _, origin = interaction.binding(action['workflow'])
        if action.get('kind') != 'failure_attention' or origin != action['origin']:
            return result('legacy_failure_origin_binding_changed')
        if state['status'] not in {'blocked', 'error', 'failed', 'cleanup_blocked', 'permission_blocked', 'stage_limit', 'planning_limit'}:
            return result('legacy_workflow_not_terminal_failure')
        if recovery.alive(state.get('owner_pid')):
            return result('legacy_workflow_owner_active')
        if recovery.generation(d, state) != action['baseline_generation']:
            return result('legacy_execution_generation_changed')
        import workflow_lineage
        if workflow_lineage.delivery(d, state)['effective_status'] == 'completed':
            return result('legacy_completed_delivery')
        for name, digest in action['failure_evidence'].items():
            if hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
                return result('legacy_failure_checksum_uncomparable', evidence_path=name,
                              invalidated_predicate='failure_evidence_sha256')
        child = core.read(Path(state['active_run'])/'state.json') if state.get('active_run') else state
        import workflow_closure
        if workflow_closure.active_execution(child):
            return result('legacy_stage_process_active_or_unverified')
        if child.get('status') == 'completed' and (state.get('error') or '').startswith('Selected host recheck passed;'):
            return result('legacy_host_recheck_passed_requires_continuation')
        ledger = core.read(d/'recovery.json') if (d/'recovery.json').exists() else {}
        latest = (ledger.get('requests') or [{}])[-1]
        if latest.get('state') in ('dispatching', 'delivery_unknown'):
            return result('legacy_claimed_recovery_delivery_no_replay')
        handler = recovery.handler_status(d, state, origin)
        import session_router
        if handler != 'unhandled' and not (handler == 'activity_unknown'
                and session_router.legacy_failure_notice_compatible(origin)):
            return result('legacy_handler_'+handler)
        return result('legacy_current', True)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return result('legacy_failure_source_unavailable', error=str(exc))



def failure_episode(directory, state):
    """Comparable semantic failure evidence, independent of clocks/activity.

    Attempt sequence, failed checks/criteria and their bytes are substantive.
    Generation/accounting/heartbeat changes are deliberately excluded. Missing
    evidence stays explicit; this is never an acceptance or authority receipt.
    """
    d = Path(directory).resolve()
    child = core.read(Path(state['active_run'])/'state.json') if state.get('active_run') else state
    root = Path(state.get('repo') or d).resolve()
    def checks(row):
        result = {}
        for name in row.get('evidence', []) or []:
            path = Path(name)
            result[str(path)] = (hashlib.sha256(path.read_bytes()).hexdigest()
                if path.resolve().is_relative_to(root) and path.is_file()
                and not any(p.is_symlink() for p in (path, *path.parents))
                and path.stat().st_size <= 4*1024*1024 else 'unverified')
        return result
    def semantic(row, attempt=False):
        keys = ('status', 'attempts', 'attempt_sequence', 'failures', 'check_results') if attempt else ('status', 'check_results')
        return dict(**{k:row.get(k) for k in keys}, evidence=checks(row))
    signature = dict(workflow=str(d), contract_hash=state.get('contract_hash'),
        status=state.get('status'), active_run=state.get('active_run'),
        planning_attempts=state.get('planning_attempts'), error=core.digest(state.get('error')),
        criteria={k:semantic(v) for k,v in state.get('criteria', {}).items()},
        tasks={k:semantic(v, True) for k,v in child.get('tasks', {}).items()})
    return dict(version=1, signature=core.digest(signature), failure=signature)


def evaluate(action):
    """Reasoned notice validity; a false notice never means business resolved."""
    def result(code, current=False, obligation='unresolved', **fields):
        return dict(current=current, reason_code=code, obligation=obligation, **fields)
    if not action.get('failure_episode'):
        # Historical records lack comparable semantics. Preserve their exact
        # claimed/presented IDs and conservative byte checks, never upgrade them.
        return _legacy_evaluate(action)
    try:
        settings = policy()
        if not settings.get('enabled') or settings.get('hub_thread') != action.get('hub'):
            return result('failure_policy_binding_changed')
        d, state, _, origin = interaction.binding(action['workflow'], require_open=False)
        if action.get('kind') != 'failure_attention' or origin != action.get('origin'):
            return result('failure_origin_binding_changed')
        if (d/'STOP').exists() or state.get('status') == 'stopped':
            return result('stop_requested', obligation='stopped')
        import workflow_lineage
        delivered = workflow_lineage.delivery(d, state)
        import task_responsibility
        if delivered.get('effective_status') == 'completed':
            if task_responsibility.completed_evidence(d, state, delivered):
                return result('verified_task_completion', obligation='resolved')
            return result('completion_evidence_requires_reconciliation', obligation='unknown')
        if state.get('status') not in {'blocked', 'error', 'failed', 'cleanup_blocked', 'permission_blocked', 'stage_limit', 'planning_limit', 'limit', 'interrupted'}:
            return result('workflow_not_terminal_failure')
        baseline = action.get('baseline_generation')
        generation = recovery.generation(d, state)
        if not isinstance(baseline, list) or len(baseline) != 2:
            return result('failure_generation_unverifiable', obligation='unknown')
        if baseline[0] != generation[0]:
            return result('execution_start_generation_changed')
        if action['failure_episode'] != failure_episode(d, state):
            return result('semantic_failure_episode_changed')
        # Only state/child-state hashes may drift under comparable semantics.
        # Contract/origin/other evidence must still match their original bytes.
        mutable = {str(d/'state.json')}
        if state.get('active_run'):
            mutable.add(str(Path(state['active_run'])/'state.json'))
        for name, digest in action.get('failure_evidence', {}).items():
            if str(Path(name)) not in mutable and hashlib.sha256(Path(name).read_bytes()).hexdigest() != digest:
                return result('immutable_failure_evidence_changed')
        child = core.read(Path(state['active_run'])/'state.json') if state.get('active_run') else state
        if child.get('status') == 'completed' and (state.get('error') or '').startswith('Selected host recheck passed;'):
            return result('host_recheck_passed_requires_continuation')
        import task_responsibility
        handling = task_responsibility.handler(d, state, origin)
        if handling['status'] != 'unhandled':
            return result(handling['reason_code'], handler=handling)
        return result(handling['reason_code'], True, handler=handling)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return result('failure_evidence_unavailable', obligation='unknown', error=str(exc))


def current(action):
    return evaluate(action)['current']

def observe(row, d, state, contract):
    import control
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
    import task_responsibility
    versioned = task_responsibility.enabled(contract)
    if versioned:
        episode = failure_episode(d, state)
    identifier = 'failure-'+core.digest([str(d), state['contract_hash'], episode])[:20]
    paths = [d/'state.json', d/'contract.json', d/'origin.json']
    if state.get('active_run'):
        paths.append(Path(state['active_run'])/'state.json')
    action = dict(id=identifier, kind='failure_attention', workflow=str(d), origin=row['origin'], hub=row['hub'],
        state='unpresented', created_at=core.now(), reminders=0, objective=contract['objective'],
        baseline_generation=generation, policy_source=settings['source'],
        failure_evidence={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        handler_status=(task_responsibility.handler(d, state, row['origin'])['status'] if versioned
                        else recovery.handler_status(d, state, row['origin'])),
        question='任务已失败或阻塞，未发现已验证的活动执行器或有效接管者；原会话活动可能未知，不能据此认定闲置。请主控核对失败及活动证据并确定处理路径；不自动清零尝试、扩大权限或重放业务。')
    if versioned:
        action['failure_episode'] = episode
        action['responsibility_version'] = 1
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
                if not versioned:
                    action['created_at'] = previous['created_at']
                    control.put(con, action)
                else:
                    previous.update(failure_evidence=action['failure_evidence'],
                                    validity=evaluate(action))
                    if previous.get('state') == 'stale':
                        previous['state'] = 'unpresented'
                    control.put(con, previous)


def pending(action):
    return (not action.get('presented_at') and not action.get('notice')
            and action.get('state') not in {'resolved','stopped','completed','superseded'} and current(action))
