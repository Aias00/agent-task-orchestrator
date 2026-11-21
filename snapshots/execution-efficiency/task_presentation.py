"""Read-only task projections. Observation never accepts, resumes or sends work.

Only persisted ATO records and kernel process identities are consulted here.
Native session activity/capabilities, execution leases and human approval are
not inferred from a PID, a turn ending, a message receipt or silence.
"""
import copy
import math
import os
from pathlib import Path

import execution_metrics as metrics
import execution_accounting as accounting
import runner as core


ACTIVE = frozenset({'running', 'planning', 'verifying'})
FAILED = frozenset({'blocked', 'error', 'failed', 'cleanup_blocked', 'permission_blocked',
                    'stage_limit', 'planning_limit', 'limit', 'interrupted'})


def question_view(directory, state):
    """Read canonical question records without inbox reconciliation or actors.

    These are recorded questions, not freshly authenticated user decisions.
    Exact durable workflow/origin/contract/generation/hub bindings are required;
    native identity verification remains the existing accept-answer operation.
    """
    import ato_config
    import recovery
    d = Path(directory).resolve()
    folder = ato_config.interaction_directory()/'questions'
    if state.get('kind') != 'workflow' or not folder.exists():
        return dict(questions=[], knowledge='recorded')
    hub_path = ato_config.interaction_directory()/'hub.json'
    origin_path = d/'origin.json'
    if not hub_path.exists() or not origin_path.exists():
        return dict(questions=[], knowledge='unknown')
    hub, origin = core.read(hub_path), core.read(origin_path)
    generation = recovery.generation(d, state)
    target = origin.get('origin_session_ref') or origin.get('origin_thread_id')
    result, complete = [], True
    # Bounded metadata inspection of the canonical question folder only. Never
    # open native auth, session transcripts, mailbox credentials or arbitrary paths.
    for index, path in enumerate(folder.glob('question-*.json')):
        if index >= 512:
            complete = False
            break
        try:
            if path.is_symlink() or path.stat().st_size > 128*1024:
                complete = False
                continue
            q = core.read(path)
            if (q.get('workflow') != str(d) or q.get('contract_hash') != state.get('contract_hash')
                    or q.get('origin_thread') != target or q.get('hub_thread') != hub.get('thread_id')
                    or q.get('generation') != generation or q.get('kind') == 'supervision_alert'):
                continue
            # Recovery-scoped questions need native/request checks that a query
            # cannot supply; expose their existence without inventing validity.
            valid = not q.get('request_id') and q.get('provenance') != 'controller_escalation'
            result.append(dict(id=q.get('id'), state=q.get('state'), question=q.get('question'),
                               binding='recorded' if valid else 'requires_accept_validation',
                               needs_user=q.get('state') == 'pending',
                               answer_recorded=bool(q.get('answer')), presentation=q.get('presentation')))
        except (OSError, ValueError, KeyError, TypeError):
            complete = False
    return dict(questions=result, knowledge='recorded' if complete else 'incomplete')


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


def recovery_view(directory, state):
    """Existing summary shape without opening a managed bus or native adapter.

    recovery.summary's handler lookup may initialize the execution-lease store.
    A status query must not do so. Request delivery and handler validity are
    separate: this snapshot explicitly leaves unobserved origin work unknown.
    """
    d = Path(directory)
    path = d/'recovery.json'
    if not path.exists():
        return dict(enabled=False, handler='unknown', origin_activity='unknown')
    r = core.read(path)
    watch = core.read(d/'supervisor.json') if (d/'supervisor.json').exists() else {}
    requests = r.get('requests', [])
    latest = requests[-1] if requests else None
    p = processes(state)
    handler = ('stopped' if (d/'STOP').exists() or state['status'] == 'stopped' else
               'closed' if state['status'] == 'completed' else
               'worker_active' if any(v['identity_verified'] for v in p.values()) else
               'delivery_unknown' if latest and latest.get('state') in ('dispatching', 'delivery_unknown') else
               'activity_unknown')
    supervisor = process_view(watch.get('pid'), watch.get('identity'))
    return dict(enabled=r.get('enabled', False), status=r.get('status', 'unknown'),
                requests=len(requests), max_requests=r.get('max_requests'), deadline=r.get('deadline'),
                handler=handler, origin_activity='unknown', supervisor_pid=watch.get('pid'),
                supervisor_alive=supervisor['alive'], supervisor_process=supervisor,
                heartbeat=watch.get('heartbeat'), last_error=watch.get('error'),
                latest={k: latest.get(k) for k in ('id', 'state', 'mailbox_state', 'message_id',
                    'sent_at', 'responded_at', 'resumed_at', 'note', 'notification')} if latest else None)


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


def remaining(state, plan):
    overall = state.get('kind') == 'workflow'
    rows = state.get('criteria' if overall else 'tasks', {})
    definitions = plan.get('acceptance' if overall else 'tasks', [])
    terminal = 'passed' if overall else 'completed'
    return [dict(id=item['id'], description=item.get('description', item.get('prompt')),
                 kind=item.get('kind', 'command'), status=rows.get(item['id'], {}).get('status', 'unknown'),
                 checks=item.get('checks', []), evidence=rows.get(item['id'], {}).get('evidence', []))
            for item in definitions if rows.get(item['id'], {}).get('status') != terminal]


def wait_view(key, row, timestamp):
    deadline = row.get('next_check_at')
    reason = row.get('wait_reason')
    return dict(id=key, status=row['status'], reason=reason, next_check_at=deadline,
                valid=bool(isinstance(reason, str) and reason.strip()
                           and type(deadline) in (int, float)
                           and math.isfinite(deadline) and deadline > timestamp))


def ledger_view(rows, versioned, timestamp, errors):
    if not versioned:
        invocations = [r.get('attempt_sequence', r.get('attempts')) for r in rows]
        known = not errors and all(type(n) is int and n >= 0 for n in invocations)
        return dict(version=None, knowledge='legacy_unknown',
                    **{k: None for k in accounting.COUNTERS if k != 'invocations'},
                    invocations=sum(invocations) if known else None,
                    invocation_basis='recorded sequence or current attempts; historical resets may be unknown')
    values = []
    for row in rows:
        value = row.get('execution_accounting', {})
        if (not isinstance(value, dict) or value.get('version') != 2 or any(type(value.get(k)) not in (int, float)
                or not math.isfinite(value[k]) or value[k] < 0 for k in accounting.COUNTERS)):
            errors.append('v2 accounting missing or invalid; no historical migration inferred')
            continue
        values.append(value)
    result = dict(version=2, knowledge='unknown' if errors else 'recorded',
                  **{k: sum(v[k] for v in values) if not errors else None for k in accounting.COUNTERS})
    # In-flight command time has no durable start watermark; never invent active
    # time from a heartbeat or the age of a worker invocation.
    result.update(active_time_basis='settled command durations only', unsettled_wait_seconds=0,
                  unsettled_yield_seconds=0)
    for row in rows:
        mark = row.get('wait_accounted_at')
        if row.get('status') in ('waiting', 'yielded', 'capacity_wait') and type(mark) in (int, float) and math.isfinite(mark):
            key = 'unsettled_yield_seconds' if row['status'] == 'yielded' else 'unsettled_wait_seconds'
            result[key] += max(0, timestamp-mark)
    return result


def local_view(directory, state, plan, timestamp):
    d = Path(directory).resolve()
    workflow = state.get('kind') == 'workflow'
    timing_children = []
    errors, stages, rows = [], [], list(state.get('tasks', {}).values())
    if workflow:
        rows = ([state.get('planning_execution', {}), state.get('host_execution', {})]
                if accounting.enabled(plan) else [dict(state.get('capacity_planning', {}),
                                                       attempts=state.get('planning_attempts', 0))])
        seen = set()
        for row in state.get('stages', []):
            try:
                path = Path(row['run_dir']).resolve()
                if path in seen:
                    raise ValueError('duplicate stage reference')
                seen.add(path)
                child, child_plan = bound_stage(path, row, d, state)
                rows.extend(child['tasks'].values())
                timing_children.append(path)
                stage = local_view(path, child, child_plan, timestamp)
                stages.append({**{k:stage[k] for k in ('directory', 'id', 'objective', 'raw_status',
                                'effective_status', 'process_status', 'processes', 'remaining', 'execution_accounting', 'phase_timing')},
                               'covers': row['covers'], 'tasks': copy.deepcopy(child['tasks'])})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append('stage unavailable: '+str(exc))
        active = next((s for s in stages if s['directory'] == state.get('active_run')), None)
        if state.get('active_run') and active is None:
            errors.append('active stage is not a verified recorded stage')
    else:
        active = dict(directory=str(d), id=state['id'], objective=plan['objective'],
                      raw_status=state['status'], processes=processes(state), tasks=copy.deepcopy(state.get('tasks', {})))
    counts = ledger_view(rows, accounting.enabled(plan), timestamp, errors)
    capacity_view = None
    if 'capacity' in plan:
        import ato_config
        import capacity
        # No configure/enqueue/claim/schema initialization during a query.
        path = ato_config.ato_home()/'capacity.sqlite3'
        try:
            capacity_view = capacity.CapacityStore(path, read_only=True).snapshot(plan['capacity']['endpoint'])
            capacity_view['selection'] = copy.deepcopy(plan['capacity'])
        except (OSError, ValueError, __import__('sqlite3').Error) as exc:
            capacity_view = dict(selection=copy.deepcopy(plan['capacity']), knowledge='unknown', reason=str(exc))
    proc = processes(state)
    observed = [*proc.values()]
    if workflow:
        observed.extend(p for s in stages for p in s['processes'].values())
    verified = any(p['identity_verified'] for p in observed)
    uncertain = any(p['status'] in ('unknown', 'identity_unverified') or p.get('group_alive') for p in observed
                    if not p['identity_verified'])
    live_status = 'verified_active' if verified else 'unknown' if uncertain else 'no_verified_process'
    pending = remaining(state, plan)
    waits = [wait_view(key, row, timestamp) for key, row in state.get('tasks', {}).items()
        if row.get('status') in ('waiting', 'yielded', 'capacity_wait')]
    if workflow:
        # Workflow (both legacy and v2) persists the planner's deadline on
        # state, not planning_execution. Project that existing record without
        # migrating it or manufacturing any legacy wait accounting.
        planner = dict(state.get('planning_execution', state.get('capacity_planning', {})))
        if state['status'] == 'waiting' and not state.get('active_run'):
            planner.update(status='waiting', next_check_at=state.get('next_check_at'),
                           wait_reason=planner.get('wait_reason') or state.get('error'))
        if planner.get('status') in ('waiting', 'yielded', 'capacity_wait'):
            waits.append(wait_view('planner', planner, timestamp))
    if workflow and active:
        waits.extend(wait_view(key, row, timestamp) for key, row in active['tasks'].items()
                     if row.get('status') in ('waiting', 'yielded', 'capacity_wait'))
    delivery = state.get('delivery', {})
    effective = delivery.get('effective_status', state['status'])
    if effective == 'completed' and pending and delivery.get('validation') not in ('valid', 'user_accepted'):
        effective = 'acceptance_incomplete'
    stop = (d/'STOP').exists()
    unknown_delivery = any(row.get('delivery_unknown') for row in rows)
    recovery = state.get('recovery') or recovery_view(d, state)
    unknown_delivery |= recovery.get('handler') == 'delivery_unknown'
    if effective != 'completed':
        if stop or state['status'] == 'stopped':
            effective = 'stop_requested' if verified else 'stopped'
        elif unknown_delivery:
            effective = 'delivery_unknown'
        elif state['status'] == 'capacity_wait' or any(w['status'] == 'capacity_wait' for w in waits):
            effective = 'capacity_wait' if verified else 'capacity_wait_no_verified_owner'
        elif waits and all(w['valid'] for w in waits) and not any(r.get('status') in FAILED | ACTIVE for r in rows):
            effective = 'waiting' if any(w['status'] == 'waiting' for w in waits) else 'yielded'
        elif state['status'] in ACTIVE:
            effective = state['status'] if verified else 'process_unverified' if uncertain else 'interrupted'
        elif state['status'] == 'waiting' and (not waits or not all(w['valid'] for w in waits)):
            effective = 'wait_unverified'
        elif state['status'] in FAILED and not verified:
            effective = 'failed_no_verified_handler' if recovery.get('handler') not in (
                'delivery_unknown', 'worker_active') else state['status']
    interaction = question_view(d, state)
    needs_user = effective != 'completed' and (state['status'] == 'needs_input' or
                 any(p['kind'] == 'manual' and p['status'] == 'needs_input' for p in pending) or
                 any(r.get('status') == 'needs_input' for r in rows) or
                 any(q['needs_user'] for q in interaction['questions']))
    blockers = [state['error']] if state.get('error') else []
    blockers.extend(r.get('feedback') or r.get('wait_reason') or r.get('summary') for r in rows
                    if r.get('status') in FAILED | {'needs_host', 'needs_input', 'waiting', 'yielded', 'capacity_wait'})
    blockers = list(dict.fromkeys(x for x in blockers if x))
    if effective == 'completed':
        next_step = 'No execution requested; retain acceptance evidence and history.'
    elif stop:
        next_step = 'Retain STOP and inspect process evidence; query grants no resume authority.'
    elif needs_user:
        next_step = 'Await the actual user decision or manual acceptance through the existing canonical ask flow.'
    elif unknown_delivery or uncertain:
        next_step = 'Inspect exact delivery/process identity and cleanup evidence; do not replay or resume from this query.'
    elif effective in ('capacity_wait', 'capacity_wait_no_verified_owner'):
        next_step = 'Wait for bounded endpoint admission under the existing owner; no preemption, automatic takeover or new permission.'
    elif effective in ('waiting', 'yielded'):
        next_step = 'Wait until the recorded next check; independent work remains subject to the existing owner.'
    elif state['status'] == 'needs_host':
        next_step = 'Original host may run only the frozen authorized handoff checks; no user approval is inferred.'
    elif effective in ('interrupted', 'failed_no_verified_handler', 'wait_unverified'):
        next_step = 'Inspect recorded blocker, remaining acceptance and recovery; no verified executor is established by this query.'
    elif verified:
        next_step = 'Observe the verified executor; all remaining acceptance still applies.'
    else:
        next_step = 'Remaining checks require authorized execution; this query starts no work.'
    return dict(version=1, directory=str(d), id=state['id'], kind='workflow' if workflow else 'stage',
                objective=plan['objective'], completion_scope='overall' if workflow else 'stage_only',
                raw_status=state['status'], effective_status=effective, process_status=live_status,
                processes=proc, current_stage=active, stages=stages, remaining=pending,
                phase_timing=metrics.view(d, counts, children=timing_children),
                execution_accounting=counts, legacy_attempts={k:r.get('attempts') for k,r in state.get('tasks', {}).items()},
                elapsed_seconds=state.get('elapsed_seconds'), planning_attempts=state.get('planning_attempts'),
                waits=waits, capacity=capacity_view, blocker='; '.join(blockers),
                next_step=next_step, needs_user=bool(needs_user), recovery=recovery, interaction=interaction,
                delivery=delivery, stop_requested=stop, observation_errors=errors,
                capabilities=dict(session_activity='unknown', resumable='unknown',
                                  reason='query does not probe native sessions or infer adapter capabilities'),
                read_only=True, acceptance_basis='persisted checks; query does not run or accept checks')


def project(directory, state, plan):
    view = local_view(directory, state, plan, core.now())
    if state.get('kind') == 'workflow' or not (Path(directory)/'parent.json').exists():
        return view
    try:
        binding = core.read(Path(directory)/'parent.json')
        parent = Path(binding['workflow_dir']).resolve()
        ps, contract = core.read(parent/'state.json'), core.read(parent/'contract.json')
        if (ps.get('kind') != 'workflow' or ps['contract_hash'] != core.digest(contract)
                or parent != Path(ps['repo']).resolve()/'.codex/tasks/workflows'/ps['id']):
            raise ValueError('parent workflow identity differs')
        row = next(r for r in ps['stages'] if Path(r['run_dir']).resolve() == Path(directory).resolve())
        bound_stage(directory, row, parent, ps)
        import workflow_lineage
        ps['delivery'] = workflow_lineage.delivery(parent, ps)
        parent_view = local_view(parent, ps, contract, core.now())
        view.update(stage_objective=plan['objective'], objective=contract['objective'],
                    overall=parent_view, parent_binding='verified', needs_user=parent_view['needs_user'])
        if parent_view['effective_status'] != 'completed':
            view.update(stage_next_step=view['next_step'], next_step=parent_view['next_step'])
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        view.update(parent_binding='invalid', overall=None)
        view['observation_errors'].append('full objective unavailable: '+str(exc))
    return view


def render_text(view):
    def show(value):
        return 'unknown' if value is None else str(value)
    lines = [f"Task: {view['id']} ({view['kind']}; {view['completion_scope']})", f"Objective: {view['objective']}",
             f"Status: raw={view['raw_status']}; effective={view['effective_status']}; process={view['process_status']}"]
    if view.get('overall'):
        parent = view['overall']
        lines.append(f"Overall: {parent['id']}; raw={parent['raw_status']}; effective={parent['effective_status']}")
        lines.append(f"Overall next step: {parent['next_step']}")
    current = view.get('current_stage')
    lines.append('Current stage: '+(f"{current['id']}: {current['objective']} ({current['raw_status']})" if current else 'none active'))
    if view['stages']:
        last = view['stages'][-1]
        lines.append(f"Latest stage: {last['id']}; raw={last['raw_status']}; effective={last['effective_status']}")
    for role, proc in view['processes'].items():
        lines.append(f"{role.capitalize()}: pid={show(proc['pid'])}; {proc['status']}; identity_verified={proc['identity_verified']}")
    if view['kind'] == 'workflow' and current:
        lines.append(f"Stage effective status: {current['effective_status']}; process={current['process_status']}")
        for role, proc in current['processes'].items():
            lines.append(f"Stage {role}: pid={show(proc['pid'])}; {proc['status']}; identity_verified={proc['identity_verified']}")
    counts = view['execution_accounting']
    lines.append('Accounting ('+counts['knowledge']+'): '+', '.join(k+'='+show(counts[k]) for k in accounting.COUNTERS))
    if counts.get('version') == 2:
        lines.append('Unsettled wait/yield seconds (projection only): '+
                     show(counts['unsettled_wait_seconds'])+'/'+show(counts['unsettled_yield_seconds']))
    timing = view.get('phase_timing', {})
    lines.append('Phase timing (exclusive observed wall; provider latency '+timing.get('model_latency', 'unknown')+'): '+
                 str(timing.get('phases_seconds', {})))
    lines.append('Observed wall seconds: '+show(timing.get('observed_wall_seconds'))+
                 '; cumulative ledger time is separate, not additive.')
    lines.append('Legacy attempts (not failure counts): '+str(view['legacy_attempts']))
    lines.append('Remaining acceptance: '+(', '.join(p['id']+'='+p['status'] for p in view['remaining']) or 'none recorded'))
    if view.get('overall'):
        lines.append('Overall remaining acceptance: '+(', '.join(p['id']+'='+p['status'] for p in view['overall']['remaining']) or 'none recorded'))
    for wait in view['waits']:
        lines.append(f"{wait['status']}: {wait['id']}; reason={show(wait['reason'])}; next_check_at={show(wait['next_check_at'])}; valid={wait['valid']}")
    if view.get('capacity'):
        cap = view['capacity']
        lines.append('Capacity: '+str(cap.get('selection'))+'; active='+str(len(cap.get('active', [])))+
                     '; queued='+str(len(cap.get('pending', [])))+'; oldest background wait='+
                     show(cap.get('oldest_background_wait_seconds'))+'; '+cap.get('fairness', cap.get('reason', 'unknown')))
    lines.extend([f"Recovery: {view['recovery'].get('status', 'not enabled')}; handler={view['recovery'].get('handler', 'unknown')}",
                  f"Delivery: validation={view['delivery'].get('validation', 'unlinked')}; successor={show(view['delivery'].get('successor'))}",
                  f"STOP: {view['stop_requested']}; needs_user={view['needs_user']}", 'Blocker: '+(view['blocker'] or 'none recorded'),
                  'Next step: '+view['next_step'], 'Capabilities: activity=unknown; resumable=unknown (not probed)'])
    if view.get('responsibility'):
        responsibility = view['responsibility']
        lines.append('Unresolved controller follow-ups: '+str(len(responsibility['followups'])))
        for row in responsibility['obligations']:
            claim = row.get('claim') or {}
            outcome = (row.get('dispositions') or [{}])[-1]
            lines.append('Responsibility: '+row['id']+'; state='+row['state']+
                         '; reason='+row.get('disposition_reason', row.get('reason_code', 'unknown'))+
                         '; claim='+row['claim_reason']+'; actor='+str(claim.get('actor', {}).get('reference'))+
                         '; expires_at='+str(claim.get('expires_at'))+
                         '; next_action='+str(outcome.get('next_action') or claim.get('next_action') or row.get('question')))
    for q in view['interaction']['questions']:
        lines.append(f"Canonical question: {q['id']}; state={q['state']}; {q['question']}")
    if view['observation_errors']:
        lines.append('Observation errors: '+'; '.join(view['observation_errors']))
    return '\n'.join(lines)
