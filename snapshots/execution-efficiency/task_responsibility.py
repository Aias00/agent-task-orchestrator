"""Versioned workflow responsibility in the existing controller ledger.

These records are diagnostic obligations, never execution leases or recovery
permissions. Read-only projections do not create databases or inspect native
history. Only authenticated origin/hub actors may record claims/dispositions.
"""
import hashlib
import json
import math
from pathlib import Path
import sqlite3

import ato_config
import agents  # Built-in registration only; no identity provisioning or I/O.
import interaction
import recovery
import runner as core
import session_router

MAX_CLAIM_SECONDS = 900
MAX_WAIT_SECONDS = 86400
FAILED = frozenset({'blocked', 'error', 'failed', 'cleanup_blocked', 'permission_blocked',
                    'stage_limit', 'planning_limit', 'limit', 'interrupted'})


def validate(value):
    if not isinstance(value, dict) or value != {'version': 1} or type(value.get('version')) is not int:
        raise ValueError('responsibility_policy requires exactly version=1')


def enabled(contract):
    if 'responsibility_policy' not in contract:
        return False
    validate(contract['responsibility_policy'])
    return True


def ledger_rows(directory):
    """No schema creation, migration, WAL configuration or write on queries."""
    path = ato_config.interaction_directory()/'control/state.sqlite'
    if not path.is_file():
        return []
    d = str(Path(directory).resolve())
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as con:
        return [row for (body,) in con.execute('select body from actions')
                if (row := json.loads(body)).get('workflow') == d]


def context(d, state, origin, hub):
    return dict(workflow=str(Path(d).resolve()), project=state['repo'],
                contract_hash=state['contract_hash'], generation=recovery.generation(Path(d), state),
                origin=origin, hub=hub)


def evidence(paths):
    result = {}
    if not paths:
        raise ValueError('existing diagnostic evidence required')
    for name in paths:
        path = Path(name)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4*1024*1024:
            raise ValueError('evidence must be a bounded regular file')
        result[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def evidence_valid(value):
    try:
        return bool(value) and evidence(value) == value
    except (OSError, ValueError, TypeError):
        return False


def authenticated(d, state, origin, role, proof=None, reference=None, operation='responsibility.claim'):
    if role not in ('origin', 'hub'):
        raise ValueError('responsibility requires origin or hub role')
    hub = (interaction.config() or {}).get('thread_id')
    target = origin if role == 'origin' else hub
    if not target or reference is not None and reference != target:
        raise ValueError('responsibility actor binding changed')
    # The environment is a locator only. The existing native/adapter verifier
    # authenticates it; v2 always requires an explicit scoped credential proof.
    if session_router.is_reference(target) and session_router.resolve(target)[0]['version'] == 2:
        actor = session_router.authenticate_actor(target, proof, role=role, project=state['repo'],
            workflow_id=str(d), contract_hash=state['contract_hash'],
            generation=state.get('execution_revision', 0))
        session_router.authorize_actor(actor, operation, project=state['repo'],
            workflow_id=str(d), contract_hash=state['contract_hash'],
            generation=state.get('execution_revision', 0))
        return dict(reference=target, role=role, verified=actor.to_mapping())
    if session_router.current_actor_locator() != target:
        raise ValueError('responsibility requires its bound actor')
    interaction.actor(target)
    return dict(reference=target, role=role, verified='existing native actor verifier')


def identifier(d, state):
    import failure_attention
    return 'responsibility-'+core.digest([str(d), state['contract_hash'],
                                           failure_attention.failure_episode(d, state)])[:20]


def ensure(row, d, state, contract):
    """Called by the existing tick, not by a new observer/supervisor."""
    import control
    import failure_attention
    key = identifier(d, state)
    with control.database() as con, con:
        old = con.execute('select body from actions where id=?', (key,)).fetchone()
        if old:
            previous = json.loads(old[0])
            for field, expected in context(d, state, row['origin'], row['hub']).items():
                if field != 'generation' and previous.get(field) != expected:
                    raise ValueError('responsibility binding changed: '+field)
            if previous.get('failure_episode') != failure_attention.failure_episode(d, state):
                raise ValueError('responsibility episode evidence changed')
            return previous
        action = dict(id=key, kind='responsibility', version=1, state='pending_disposition',
            **context(d, state, row['origin'], row['hub']), baseline_generation=recovery.generation(d, state),
            failure_episode=failure_attention.failure_episode(d, state), objective=contract['objective'],
            created_at=core.now(), reminders=0, execution_grant=False,
            question='Record a verified action, bounded external wait, canonical human question or evidenced closure.',
            reason_code='disposition_required', dispositions=[])
        control.put(con, action)
    return action


def bound(record, d, state, origin, hub):
    return all(record.get(k) == v for k, v in context(d, state, origin, hub).items())


def claim_reason(action, d, state, origin, hub):
    import failure_attention
    claim = action.get('claim')
    if not claim:
        return 'no_workflow_claim'
    if not bound(claim, d, state, origin, hub):
        return 'claim_binding_changed'
    if claim.get('failure_episode') != failure_attention.failure_episode(d, state):
        return 'claim_episode_changed'
    if (claim.get('actor', {}).get('role') not in ('origin', 'hub')
            or claim['actor'].get('reference') != (origin if claim['actor']['role'] == 'origin' else hub)):
        return 'claim_actor_changed'
    if not evidence_valid(claim.get('evidence')):
        return 'claim_evidence_changed'
    if not finite_deadline(claim.get('expires_at'), claim.get('created_at'), MAX_CLAIM_SECONDS):
        return 'claim_deadline_invalid'
    if core.now() >= claim['expires_at']:
        return 'claim_expired'
    return 'workflow_claim_valid'


def finite_deadline(deadline, start, maximum):
    return (type(deadline) in (int, float) and math.isfinite(deadline)
            and type(start) in (int, float) and math.isfinite(start)
            and 0 < deadline-start <= maximum)


def claim(directory, role, next_action, paths, source, seconds=300, *, actor_proof=None, reference=None):
    import control
    if type(seconds) is not int or not 1 <= seconds <= MAX_CLAIM_SECONDS:
        raise ValueError('claim must be finite and at most 900 seconds')
    if not all(isinstance(x, str) and x.strip() for x in (source, next_action)):
        raise ValueError('claim requires source and concrete next_action')
    with recovery.locked(control.directory()/'tick.lock'):
        d, state, contract, origin = interaction.binding(directory)
        if not enabled(contract):
            raise ValueError('workflow requires explicit responsibility_policy version=1')
        actor = authenticated(d, state, origin, role, actor_proof, reference)
        hub = (interaction.config() or {}).get('thread_id')
        if not hub:
            raise ValueError('verified hub required')
        row = ensure(dict(origin=origin, hub=hub), d, state, contract)
        # One claim per semantic episode. Repeating the exact request is a
        # read of its original deadline, never heartbeat renewal or re-grant.
        proposal = dict(**context(d, state, origin, hub), actor=actor, next_action=next_action,
            evidence=evidence(paths), source=source, seconds=seconds, failure_episode=row['failure_episode'])
        old = row.get('claim')
        if old:
            if any(old.get(k) != v for k, v in proposal.items()):
                raise ValueError('episode already claimed; no renewal or replacement')
            return old
        if row['state'] in control.TERMINAL:
            raise ValueError('responsibility already closed')
        created = core.now()
        value = dict(proposal, created_at=created, expires_at=created+seconds, execution_grant=False)
        row.update(claim=value, reason_code='workflow_claim_valid')
        with control.database() as con, con:
            control.put(con, row, 'responsibility_claimed')
        return value



def active_child(d, state):
    """Return actual stage state plus exact persisted workflow/plan binding."""
    if not state.get('active_run'):
        return state, True
    from task_presentation import bound_stage
    path = Path(state['active_run']).resolve()
    child = core.read(path/'state.json')
    rows = [r for r in state.get('stages', []) if Path(r['run_dir']).resolve() == path]
    if len(rows) != 1:
        return child, False
    try:
        verified, _ = bound_stage(path, rows[0], d, state)
        return verified, True
    except (OSError, ValueError, KeyError, TypeError):
        return child, False

def handler(directory, state, origin, *, inspect_owner=True):
    """Task-scoped evidence only. Session activity never supplies a handler."""
    import workflow_closure
    d = Path(directory).resolve()
    if (d/'STOP').exists() or state.get('status') == 'stopped':
        return dict(status='stopped', reason_code='stop_requested')
    from task_presentation import processes
    child, child_bound = active_child(d, state)
    process_rows = list(processes(state).values()) + (list(processes(child).values()) if child_bound else [])
    verified_process = any(p['identity_verified'] for p in process_rows)
    cleanup_fenced = workflow_closure.active_execution(state) or workflow_closure.active_execution(child)
    if verified_process:
        phase = (child.get('active_process') or {}).get('phase')
        return dict(status='host_active' if phase in ('host', 'host_check', 'check') else 'worker_active',
                    reason_code='task_process_identity_verified', phase=phase)
    owner_unknown = False
    try:
        owner = recovery.current_execution_owner(state) if inspect_owner else None
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        owner, owner_unknown, cleanup_fenced = None, True, True
    if owner:
        try:
            owner_generation = json.loads(owner['generation']) if isinstance(owner.get('generation'), str) else owner.get('generation')
        except (ValueError, TypeError):
            owner_generation = None
        exact = (owner.get('project') == state.get('repo') and owner.get('task_id') == str(d)
            and owner.get('contract_hash') == state.get('contract_hash')
            and owner_generation == recovery.generation(d, state)
            and owner.get('role') in ('worker', 'origin', 'session_host') and bool(owner.get('owner'))
            and type(owner.get('fence')) is int and owner['fence'] > 0)
        if exact:
            return dict(status='execution_owned', reason_code='exact_execution_lease', role=owner['role'])
    hub = (interaction.config() or {}).get('thread_id')
    reasons = []
    for row in ledger_rows(d):
        if row.get('kind') == 'responsibility' and row.get('state') not in ('resolved', 'stopped', 'completed', 'superseded') and row.get('claim'):
            reason = claim_reason(row, d, state, origin, hub)
            if reason == 'workflow_claim_valid':
                return dict(status='workflow_claimed', reason_code=reason,
                            actor=row['claim']['actor'], expires_at=row['claim']['expires_at'],
                            next_action=row['claim']['next_action'])
            reasons.append(reason)
    for row in ledger_rows(d):
        if row.get('kind') == 'responsibility' and row.get('state') not in ('resolved', 'stopped', 'completed', 'superseded') and row.get('dispositions'):
            reason = disposition_reason(row['dispositions'][-1], row, d, state, origin, hub)
            if reason in ('bounded_external_wait', 'canonical_question_pending'):
                return dict(status='external_wait' if reason == 'bounded_external_wait' else 'awaiting_user',
                            reason_code=reason)
    # A genuine pending human question already owns its notification route.
    # Unknown/stale questions remain inspection material, not handling proof.
    for path in (ato_config.interaction_directory()/'questions').glob('question-*.json'):
        try:
            q = core.read(path)
            if (q.get('workflow') != str(d) or q.get('origin_thread') != origin or q.get('hub_thread') != hub
                    or q.get('kind') or q.get('state') not in ('pending', 'answered')):
                continue
            interaction.validate_question(q)
            return dict(status='awaiting_user', reason_code='canonical_question_pending')
        except (ValueError, OSError, KeyError, TypeError):
            continue
    ledger = core.read(d/'recovery.json') if (d/'recovery.json').exists() else {}
    latest = (ledger.get('requests') or [{}])[-1]
    exact = (ledger.get('thread_id') == origin and ledger.get('contract_hash') == state.get('contract_hash')
             and latest.get('generation') == recovery.generation(d, state))
    if latest.get('state') in ('dispatching', 'delivery_unknown'):
        return dict(status='delivery_unknown', reason_code='claimed_recovery_delivery_no_replay')
    if (exact and ledger.get('enabled') and latest.get('expires_at', 0) > core.now()
            and (latest.get('mode') == 'report' or ledger.get('deadline', 0) > core.now())
            and latest.get('state') in ('queued', 'working', 'repaired', 'resuming', 'resume_launching')):
        return dict(status='recovery_pending', reason_code='bounded_exact_recovery_request',
                    expires_at=latest['expires_at'])
    return dict(status='unhandled', cleanup_fenced=cleanup_fenced,
                reason_code=reasons[-1] if reasons else 'execution_ownership_unknown' if owner_unknown
                else 'task_process_identity_unverified' if cleanup_fenced else 'no_task_bound_handler')


def disposition_reason(value, row, d, state, origin, hub):
    import failure_attention
    if row['failure_episode'] != failure_attention.failure_episode(d, state):
        return 'disposition_episode_changed'
    if not bound(value, d, state, origin, hub):
        return 'disposition_binding_changed'
    if value.get('failure_episode') != row['failure_episode']:
        return 'disposition_episode_changed'
    if not evidence_valid(value.get('evidence')):
        return 'disposition_evidence_changed'
    if value['outcome'] == 'external_wait':
        if not finite_deadline(value.get('recheck_at'), row['created_at'], MAX_WAIT_SECONDS):
            return 'external_wait_deadline_invalid'
        return 'external_wait_due' if core.now() >= value['recheck_at'] else 'bounded_external_wait'
    if value['outcome'] == 'human_question':
        try:
            q = core.read(interaction.question_path(value['question_id']))
            interaction.validate_question(q)
            if q['workflow'] != str(d) or q['hub_thread'] != hub or q['origin_thread'] != origin:
                return 'canonical_question_binding_changed'
            return 'canonical_question_pending' if q['state'] in ('pending', 'answered') else 'answer_requires_followthrough'
        except (OSError, ValueError, KeyError, TypeError):
            return 'canonical_question_stale'
    return 'action_requires_verified_progress' if value['outcome'] == 'authorized_action' else 'closure_requires_verification'




def completed_evidence(d, state, delivery):
    """Use existing independent completion/ack validation; never accept a label."""
    if delivery.get('effective_status') != 'completed':
        return False
    if delivery.get('validation') in ('valid', 'user_accepted'):
        return True
    if state.get('status') != 'completed':
        return False
    try:
        import workflow_lineage
        contract = core.read(Path(d)/'contract.json')
        if core.digest(contract) != state['contract_hash']:
            return False
        workflow_lineage._evidence(Path(d), state, contract)
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False

def verified_execution(d, state):
    """New-mode progress needs kernel identity for both owner and actual child."""
    from task_presentation import processes
    child, child_bound = active_child(d, state)
    return (child_bound and processes(state)['owner']['identity_verified']
            and processes(child)['worker']['identity_verified'])

def dispose(directory, role, outcome, next_action, paths, source, *, condition='', recheck_at=None,
            question_id=None, actor_proof=None, reference=None):
    import control
    if outcome not in ('authorized_action', 'external_wait', 'human_question', 'closed'):
        raise ValueError('specific diagnostic outcome required')
    if not all(isinstance(x, str) and x.strip() for x in (source, next_action)):
        raise ValueError('disposition requires source and concrete next_action')
    with recovery.locked(control.directory()/'tick.lock'):
        d, state, contract, origin = interaction.binding(directory, require_open=outcome != 'closed')
        if not enabled(contract) or (d/'STOP').exists():
            raise ValueError('STOP or missing versioned responsibility policy')
        actor = authenticated(d, state, origin, role, actor_proof, reference, 'responsibility.disposition')
        hub = (interaction.config() or {}).get('thread_id')
        # Use the current episode; do not repurpose an old diagnostic receipt.
        prior = [r for r in ledger_rows(d) if r.get('kind') == 'responsibility'
                 and r.get('contract_hash') == state['contract_hash'] and r.get('state') not in control.TERMINAL]
        row = (max(prior, key=lambda r:r['created_at']) if prior and outcome in ('authorized_action', 'closed')
               else ensure(dict(origin=origin, hub=hub), d, state, contract))
        value = dict(**context(d, state, origin, hub), failure_episode=row['failure_episode'],
            actor=actor, outcome=outcome, next_action=next_action, evidence=evidence(paths), source=source,
            condition=condition, recheck_at=recheck_at, question_id=question_id, execution_grant=False)
        if outcome == 'external_wait':
            if not condition.strip() or not finite_deadline(recheck_at, row['created_at'], MAX_WAIT_SECONDS) or recheck_at <= core.now():
                raise ValueError('external wait requires condition and finite future recheck trigger')
            if any(v['outcome'] == outcome and v != value for v in row['dispositions']):
                raise ValueError('external wait cannot be renewed for this episode')
        if outcome == 'human_question' and disposition_reason(value, row, d, state, origin, hub) not in (
                'canonical_question_pending', 'answer_requires_followthrough'):
            raise ValueError('current exact canonical question required; use ask first')
        if outcome == 'authorized_action':
            probe = control.live_execution(d, state)
            if not probe or not verified_execution(d, state) or recovery.generation(d, state) == row['baseline_generation']:
                raise ValueError('authorized action requires actual subsequent execution evidence')
            value['execution_evidence'] = probe
        if outcome == 'closed':
            import workflow_lineage
            delivery = workflow_lineage.delivery(d, state)
            if not completed_evidence(d, state, delivery):
                raise ValueError('closure requires real completed acceptance or verified closure/successor')
            value['closure_evidence'] = delivery
        # Idempotent immutable outcomes. No counters, task state or recovery file
        # are edited. A question/answer or diagnostic reply cannot resolve work.
        if value not in row['dispositions']:
            if len(row['dispositions']) >= 16:
                raise ValueError('bounded diagnostic outcome history exhausted')
            row['dispositions'].append(value)
        row['reason_code'] = disposition_reason(value, row, d, state, origin, hub)
        with control.database() as con, con:
            control.put(con, row, 'responsibility_disposition')
        return value


def refresh(action, d, state, origin):
    """Update obligation independently of whether a notification is stale."""
    import control
    import failure_attention
    import workflow_lineage
    delivery = workflow_lineage.delivery(d, state)
    if not verified_execution(d, state):
        action.pop('resume_probe', None)
    action['delivery'] = delivery
    if completed_evidence(d, state, delivery):
        action.update(state='completed', reason_code='verified_task_closure')
    elif (d/'STOP').exists() or state['status'] == 'stopped':
        action.update(state='stopped', reason_code='stop_requested')
    elif verified_execution(d, state) and control.resumed(action, d, state, recovery.generation(d, state)):
        action.update(state='resolved', reason_code='verified_subsequent_execution_progress')
    elif state['status'] in FAILED and failure_attention.failure_episode(d, state) != action['failure_episode']:
        # A different failed attempt is a new obligation, not a successful one.
        action.update(state='superseded', reason_code='new_failure_episode_requires_disposition')
    else:
        action['state'] = 'pending_disposition'
        handler_value = handler(d, state, origin)
        action['handler'] = handler_value
        action['reason_code'] = handler_value['reason_code']
        if action.get('dispositions'):
            reason = disposition_reason(action['dispositions'][-1], action, d, state, origin, action['hub'])
            action['reason_code'] = reason
            if reason == 'bounded_external_wait':
                action['state'] = 'external_wait'
            elif reason == 'canonical_question_pending':
                action['state'] = 'awaiting_user'
        elif action.get('diagnostic_reply'):
            action['reason_code'] = 'diagnostic_reply_requires_disposition'
    return action


def view(directory):
    """Read-only responsibility and all unresolved follow-ups, even with no inbox."""
    rows = ledger_rows(directory)
    pending = [r for r in rows if r.get('state') not in {'resolved', 'stale', 'stopped', 'completed', 'superseded'}]
    d = Path(directory).resolve()
    state = core.read(d/'state.json')
    origin_row = core.read(d/'origin.json')
    origin = origin_row.get('origin_session_ref') or origin_row.get('origin_thread_id')
    hub = (interaction.config() or {}).get('thread_id')
    obligations = []
    for row in pending:
        if row.get('kind') == 'responsibility':
            value = dict(row, claim_reason=claim_reason(row, d, state, origin, hub))
            if row.get('dispositions'):
                value['disposition_reason'] = disposition_reason(row['dispositions'][-1], row, d, state, origin, hub)
            obligations.append(value)
    return dict(version=1, read_only=True, execution_grant=False, obligations=obligations,
                followups=pending, inbox_is_not_task_health=True)


def diagnostic(row, d, state, contract, generation):
    """Existing controller's bounded diagnostic route, with a required outcome.

    A queued or late reply is material, never disposition or resumed execution.
    No activity test suppresses the obligation; no second diagnostic is sent.
    """
    import control
    obligation = ensure(row, d, state, contract)
    key = 'operational-'+core.digest([str(d), obligation['failure_episode']])[:20]
    with control.database() as con:
        found = con.execute('select body from actions where id=?', (key,)).fetchone()
    action = json.loads(found[0]) if found else dict(id=key, kind='operational', version=1,
        responsibility_id=obligation['id'], workflow=str(d), origin=row['origin'], hub=row['hub'],
        contract_hash=state['contract_hash'], baseline_generation=generation,
        failure_episode=obligation['failure_episode'], state='pending_operational',
        created_at=core.now(), reminders=0, objective=contract['objective'],
        disposition_due_at=core.now()+MAX_CLAIM_SECONDS, provenance='controller_diagnostic', execution_grant=False)
    if action.get('origin_delivery'):
        if action.get('origin_offset') is not None or 'origin_event_cursor' in action:
            initial = dict(cursor=action['origin_event_cursor']) if 'origin_event_cursor' in action else dict(offset=action['origin_offset'])
            tracker = action.setdefault('origin_tracker', initial)
            try:
                reply = session_router.observe_reply(row['origin'], tracker, key, core.now,
                    path=action.get('origin_rollout'), delivery=action['origin_delivery'])
                if reply:
                    action['origin_reply'] = reply
                    obligation['diagnostic_reply'] = dict(at=core.now(), diagnostic_id=key)
            except (ValueError, OSError, KeyError, TypeError) as exc:
                action['reply_error'] = str(exc)
        action['reason_code'] = ('diagnostic_disposition_overdue' if core.now() >= action['disposition_due_at']
                                 else 'diagnostic_reply_requires_disposition' if action.get('origin_reply')
                                 else 'diagnostic_delivery_requires_disposition')
    else:
        handling = handler(d, state, row['origin'])
        if handling['status'] != 'unhandled' or handling.get('cleanup_fenced'):
            action['reason_code'] = handling['reason_code']
        elif row['origin'] == row['hub']:
            action['reason_code'] = 'origin_is_hub_no_self_queue'
        else:
            # All canonical questions, including ambiguous records, prevent a
            # second parallel route. They do not erase responsibility.
            questions = []
            for path in (interaction.directory()/'questions').glob('question-*.json'):
                q = core.read(path)
                if q.get('workflow') == str(d) and q.get('state') in ('pending', 'answered'):
                    questions.append(q['id'])
            if questions:
                action['reason_code'] = 'canonical_question_requires_followthrough'
            else:
                try:
                    with recovery.locked(Path(state['repo'])/'.codex/tasks/.worker.lock', blocking=False):
                        fresh = core.read(d/'state.json')
                        fresh_handler = handler(d, fresh, row['origin'])
                        if ((d/'STOP').exists() or recovery.generation(d, fresh) != generation
                                or fresh['contract_hash'] != row['contract_hash']
                                or fresh_handler['status'] != 'unhandled' or fresh_handler.get('cleanup_fenced')):
                            action['reason_code'] = 'workflow_changed_before_diagnostic'
                        else:
                            checkpoint = session_router.delivery_checkpoint(row['origin'])
                            if checkpoint.get('rollout_offset') is not None:
                                action['origin_offset'] = checkpoint['rollout_offset']
                            elif 'event_cursor' in checkpoint:
                                action['origin_event_cursor'] = checkpoint['event_cursor']
                            with control.database() as con, con:
                                control.put(con, action)
                            action['origin_delivery'] = control.send('operational:'+key, row['origin'],
                                f'[ctask-operational id={key}]\nWorkflow: {d}\n'
                                '仅诊断既有阻塞，不授予执行/重试/恢复权限。必须登记任务级 disposition：'
                                '已验证行动、有界外部等待、canonical ask 或有据关闭。回复或心跳不解除责任。', workflow=str(d))
                            action['reason_code'] = 'diagnostic_delivery_requires_disposition'
                except BlockingIOError:
                    action['reason_code'] = 'repository_busy_no_execution_grant'
    obligation = refresh(obligation, d, state, row['origin'])
    with control.database() as con, con:
        control.put(con, action)
        control.put(con, obligation)
