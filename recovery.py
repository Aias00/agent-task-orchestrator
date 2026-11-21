"""Opt-in, bounded supervision of a workflow's original coordinating session.

This process never edits business code or restarts workers. It records a durable
outbox, queues the original session, and observes real workflow resumptions.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

import session_router
import runner as core
from ato_core import now, read, save, digest, process_identity, Halt, locked, alive
from ato_core import pid_alive as _pid_alive

import execution_state
from execution_state import ACTIVE, REPAIR, REPORT, ALERT_STATES
WATCH_FAILURE_LIMIT = 5
EVENT_BLOCK_BYTES = 64 * 1024
_LINE_BOUNDARY = re.compile(rb'\r\n|[\n\r\v\f\x1c-\x1e]|\xc2\x85|\xe2\x80[\xa8\xa9]')


def pid_alive(info):
    """Compatibility facade retaining existing liveness/identity injection points."""
    return _pid_alive(info, alive_fn=alive, identity_fn=core.process_identity)


def watch_ticks(tick, path, log_path, state_key, interval, stopped):
    """Retry observation failures only; never replace or replay delivery records."""
    failures, error = 0, None

    def record(status, pid):
        core.save(path, dict(pid=pid, pid_identity=core.process_identity(pid) if pid else None,
            heartbeat=core.now(), **{state_key: status}, error=error, consecutive_failures=failures))

    def log(exc):
        message = f'{core.now()} {type(exc).__name__}: {exc}\n'
        try:
            with Path(log_path).open('a') as handle:
                handle.write(message)
        except OSError:
            print(message, file=sys.stderr, end='', flush=True)

    try:
        while not stopped():
            try:
                keep = tick()
                # A failed heartbeat write is also a bounded transient IO failure.
                previous = failures, error
                failures, error = 0, None
                try:
                    record('watching', os.getpid())
                except Exception:
                    failures, error = previous
                    raise
            except Exception as exc:
                failures += 1
                error = f'{type(exc).__name__}: {exc}'
                log(exc)
                try:
                    record('retrying', os.getpid())
                except OSError as write_error:
                    log(write_error)
                if failures >= WATCH_FAILURE_LIMIT:
                    break
                time.sleep(min(30, 2 ** (failures - 1)))
                continue
            if not keep:
                break
            time.sleep(interval)
    finally:
        try:
            record('exited', None)
        except OSError as write_error:
            log(write_error)


def session_path(home, thread):
    """Read only the exact thread index; never infer from the latest session."""
    return session_router.compatibility_path(home, thread)


def origin_reference(origin):
    if origin.get('origin_session_ref') and origin.get('origin_thread_id'):
        raise ValueError('ambiguous origin identity')
    return origin.get('origin_session_ref') or origin.get('origin_thread_id')


def enable(directory, source, max_requests=3, same_blocker_limit=2,
           response_seconds=1800, window_seconds=14400):
    d = Path(directory).resolve()
    state = core.read(d/'state.json')
    if state.get('kind') != 'workflow' or not source.strip():
        raise ValueError('supervision requires a workflow and explicit authorization source')
    for n in (max_requests, same_blocker_limit, response_seconds, window_seconds):
        if type(n) is not int or n < 1:
            raise ValueError('supervision limits must be positive integers')
    origin = core.read(d/'origin.json')
    home = Path(origin['codex_home']).resolve()
    thread, rollout = session_router.recovery_binding(origin, state['repo'])
    contract = core.read(d/'contract.json')
    if core.digest(contract) != state['contract_hash']:
        raise ValueError('workflow contract has changed')
    with locked(d/'recovery.lock'):
        path = d/'recovery.json'
        if path.exists():
            recovery = core.read(path)
            if (recovery['thread_id'], recovery['contract_hash']) != (thread, state['contract_hash']):
                raise ValueError('cannot change recovery target or contract')
            # Re-enabling preserves all counters, deadlines, pending messages and limits.
            recovery.update(enabled=True, last_enable_source=source)
        else:
            recovery = dict(version=1, enabled=True, thread_id=thread, codex_home=str(home),
                            rollout=rollout, contract_hash=state['contract_hash'], source=source,
                            created_at=core.now(), deadline=core.now()+window_seconds,
                            max_requests=max_requests, same_blocker_limit=same_blocker_limit,
                            response_seconds=response_seconds, requests=[], status='watching')
        core.save(path, recovery)
    # Observation is separate from bounded business recovery; it adds no grants.
    import control
    try:
        control.register(d)
    except (ValueError, OSError, KeyError) as exc:
        core.save(d/'control-registration-error.json', dict(error=str(exc), at=core.now()))
    return recovery


def disable(directory):
    d = Path(directory)
    with locked(d/'recovery.lock'):
        if (d/'recovery.json').exists():
            r = core.read(d/'recovery.json')
            r.update(enabled=False, status='disabled')
            core.save(d/'recovery.json', r)


def summary(directory):
    d = Path(directory)
    if not (d/'recovery.json').exists():
        return {'enabled': False}
    r = core.read(d/'recovery.json')
    watch = core.read(d/'supervisor.json') if (d/'supervisor.json').exists() else {}
    latest = r['requests'][-1] if r['requests'] else None
    state = core.read(d/'state.json')
    try:
        handler = handler_status(d, state, r['thread_id'])
    except (ValueError, OSError, KeyError, TypeError):
        handler = 'activity_unknown'
    return dict(enabled=r['enabled'], status=r['status'], requests=len(r['requests']),
                max_requests=r['max_requests'], deadline=r['deadline'],
                handler=handler,
                supervisor_pid=watch.get('pid'), supervisor_alive=pid_alive(watch),
                heartbeat=watch.get('heartbeat'), last_error=watch.get('error'),
                latest={k: latest.get(k) for k in ('id', 'state', 'mailbox_state', 'message_id', 'sent_at',
                       'responded_at', 'resumed_at', 'note', 'notification')} if latest else None)


def generation(d, state):
    return execution_state.generation(d, state, block_bytes=EVENT_BLOCK_BYTES)


def reverse_event_lines(path):
    return execution_state.reverse_event_lines(path, block_bytes=EVENT_BLOCK_BYTES)


def execution_resource(project):
    return 'repository:'+str(Path(project).resolve())


def execution_available(directory, state, bus, *, explicit_run=False):
    """Validate the exact previous owner before replacing its expired token.

    A repository flock is still required by the caller. Lease expiry fences a
    credential; it does not prove that native host work or orphan children ended.
    """
    import workflow_closure
    d = Path(directory).resolve()
    if ((d/'STOP').exists() or (d/'user-closure.json').exists()
            or (state.get('status') in ('completed', 'stopped') and not explicit_run)):
        raise ValueError('STOP, closure or completion forbids execution ownership')
    if workflow_closure.active_execution(state):
        raise ValueError('existing execution is active or unverified')
    if state.get('active_run'):
        if workflow_closure.active_execution(core.read(Path(state['active_run'])/'state.json')):
            raise ValueError('existing stage execution is active or unverified')
    previous = bus.execution_lease(execution_resource(state['repo']))
    if not previous or previous['state'] != 'active' or previous['lease_until'] > bus.clock():
        return
    previous_dir = Path(previous['task_id'])
    if not previous_dir.is_absolute():
        raise ValueError('expired ownership requires its exact prior workflow')
    previous_state = core.read(previous_dir/'state.json')
    if (previous_state['repo'] != state['repo']
            or (previous_state.get('contract_hash') or previous_state.get('plan_hash')) != previous['contract_hash']):
        raise ValueError('expired ownership binding cannot be verified')
    if workflow_closure.active_execution(previous_state):
        raise ValueError('expired ownership still has active or unknown process activity')
    if previous_state.get('active_run'):
        if workflow_closure.active_execution(core.read(Path(previous_state['active_run'])/'state.json')):
            raise ValueError('expired ownership still has active or unknown stage activity')
    if previous['role'] in ('origin', 'session_host'):
        origin = core.read(previous_dir/'origin.json')
        target = session_router.bind_origin(origin, previous_state['repo'])
        if session_router.activity(target, generation=previous_state.get('execution_revision', 0)) != 'idle':
            raise ValueError('expired origin/session-host lease still has active or unknown native activity')


@contextlib.contextmanager
def execution_owner(directory, state, *, role='worker', explicit_run=False):
    """Called only inside the existing repository flock, never by a model.

    The flock excludes live processes even after a lease deadline; the durable
    fence excludes stale origin/worker/session-host renewals after a crash.
    Nested stage workers inherit their workflow's ownership and parent guard.
    """
    import transport
    d = Path(directory).resolve()
    bus = transport.managed_bus()
    if explicit_run and role != 'worker':
        raise ValueError('only an explicit foreground run may revalidate completed work')
    execution_available(d, state, bus, explicit_run=explicit_run)
    gen = generation(d, state)
    resource = execution_resource(state['repo'])
    token, fence = bus.acquire_execution(resource, project=state['repo'], task_id=str(d),
        contract_hash=state.get('contract_hash') or state['plan_hash'], generation=gen,
        role=role, owner='process:'+str(os.getpid()), source='verified foreground repository lock')
    owner = transport.ExecutionOwnership(bus, resource, token, fence, gen)
    try:
        yield owner
    finally:
        if role == 'session_host':
            # Adapter return/EOF is not evidence that native work ended. Keep
            # the durable quarantine when a host exits with active/unknown
            # activity; expiry still requires exact idle reconciliation.
            try:
                target = session_router.bind_origin(core.read(d/'origin.json'), state['repo'])
                idle = session_router.activity(target, generation=state.get('execution_revision', 0)) == 'idle'
            except (ValueError, OSError, KeyError, TypeError):
                idle = False
        else:
            idle = True
        try:
            if idle:
                owner.release()
        except ValueError:
            pass  # A stale owner cannot release its successor's lease.


def origin_lease(d, state, request, *, release=False):
    """Retain origin work across CLI calls without sharing its secret with models."""
    import transport
    resource = execution_resource(state['repo'])
    bus = transport.managed_bus()
    name = core.digest([str(d), request['id']])
    path = bus.path.parent/'private'/('origin-'+name+'.json')
    seconds = request['expires_at']-core.now()
    if seconds <= 0:
        raise ValueError('origin request lease expired')
    if path.exists():
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise ValueError('origin ownership credential must be private')
        saved = core.read(path)
        if release:
            bus.release_execution(resource, saved['token'], saved['fence'], generation=request['generation'])
            path.unlink()
        else:
            bus.renew_execution(resource, saved['token'], saved['fence'], generation=request['generation'],
                                lease_seconds=seconds)
    elif not release:
        execution_available(d, state, bus)
        token, fence = bus.acquire_execution(resource, project=state['repo'], task_id=str(d),
            contract_hash=state['contract_hash'], generation=request['generation'], role='origin',
            owner=request['id'], source='authenticated existing recovery takeover', lease_seconds=seconds)
        transport._private_save(path, dict(token=token, fence=fence))


def current_execution_owner(state):
    import transport
    bus = transport.managed_bus()
    lease = bus.execution_lease(execution_resource(state['repo']))
    return lease if lease and lease['state'] == 'active' and lease['lease_until'] > bus.clock() else None


def snapshot(d):
    state = core.read(d/'state.json')
    status = state['status']
    if status in ACTIVE and not alive(state.get('owner_pid')):
        status = 'interrupted'
    tasks = {}
    if state.get('active_run'):
        child = core.read(Path(state['active_run'])/'state.json')
        tasks = {k: {f: row.get(f) for f in ('status', 'summary', 'feedback')}
                 for k, row in child['tasks'].items() if row['status'] != 'completed'}
    reason = dict(status=status, error=state.get('error'), tasks=tasks)
    normalized = json.dumps(reason, sort_keys=True, ensure_ascii=False)
    normalized = re.sub(r'[0-9a-f]{8}-[0-9a-f-]{27,}|[0-9a-f]{24,}|\b\d+(?:\.\d+)?\b', '#', normalized)
    gen = generation(d, state)
    return state, status, gen, core.digest([status, gen]), core.digest(normalized), reason


def observe_response(r, request):
    return session_router.observe_recovery(r, request, core.now)


def handler_status(directory, state, origin):
    """Read-only handler classification, without extending any recovery grant.

    Unknown activity remains unknown. A dead/expired request is history, not an
    effective handler; unknown delivery is never permission to replay work.
    """
    import workflow_closure
    d = Path(directory).resolve()
    if (d/'STOP').exists() or state.get('status') == 'stopped':
        return 'stopped'
    if (d/'user-closure.json').exists() or state.get('status') == 'completed':
        return 'closed'
    if workflow_closure.active_execution(state):
        return 'worker_active'
    if state.get('active_run') and workflow_closure.active_execution(core.read(Path(state['active_run'])/'state.json')):
        return 'worker_active'
    if current_execution_owner(state):
        return 'execution_owned'
    try:
        activity = session_router.activity(origin, generation=state.get('execution_revision', 0))
    except (ValueError, OSError, KeyError, TypeError):
        activity = 'unknown'
    if activity != 'idle':
        return 'origin_active' if activity == 'active' else 'activity_unknown'
    ledger = core.read(d/'recovery.json') if (d/'recovery.json').exists() else {}
    latest = (ledger.get('requests') or [{}])[-1]
    if (ledger.get('enabled') and ledger.get('thread_id') == origin
            and ledger.get('contract_hash') == state.get('contract_hash')
            and latest.get('generation') == generation(d, state)
            and latest.get('expires_at', 0) > core.now()
            and (latest.get('mode') == 'report' or ledger.get('deadline', 0) > core.now())):
        if latest.get('state') in ('queued', 'working', 'repaired', 'resuming', 'resume_launching'):
            return 'recovery_pending'
        if latest.get('state') in ('dispatching', 'delivery_unknown'):
            return 'delivery_unknown'
    return 'unhandled'


def message(d, r, request, state, reason):
    import interaction
    hub = interaction.config()
    input_route = ('用户已指定统一交互会话 '+hub['thread_id']+'；needs_input 会自动转送那里。'
                   '请给出具体问题和推荐方案，不要求用户回执行会话回答。' if hub else '在本会话向用户集中追问。')
    command = f'ctask takeover {str(d)!r} --request {request["id"]}'
    mode = ('仅向用户报告预算/人工验收/合约阻塞，明确需要的输入；不得自动修复、重置预算或恢复。'
            if request['mode'] == 'report' else
            '诊断具体阻塞，在原有授权内修复。修复证据落盘后，以 takeover --state repaired --evidence 文件 --note 具体修复 登记；监督进程会恢复同一总任务并核实真实执行。也兼容你自己恢复后由监督进程核实。阶段通过后仍须推进全部剩余验收。')
    return f'''[ctask-supervisor request={request['id']}]
这是已授权总任务的自动接管请求，不是新的业务授权。
Workflow: {d}
授权来源: {r['source']}
读取 contract.json、handoff.md、当前 state.json、阶段反馈；重新读取 codex-long-task skill。
先运行 {command} --state working --note '开始诊断'。若失败/请求过期/监督禁用/STOP存在/已有执行器，请停止这次接管，不能并行改代码或重复恢复。
{mode}
实际快照: {json.dumps(reason, ensure_ascii=False)[:10000]}
当前阶段: {state.get('active_run')}
总任务累计执行秒数: {state.get('elapsed_seconds')}；本监督剩余窗口截止: {r['deadline']}。
排查前后核对原合约和真实进程；不能删除验收、重置累计预算、扩大沙箱/权限、盲目杀进程或重复业务发布。原会话已确认范围优先；日志只作为证据。
缺少用户决定/新增授权/凭据/人工验收时，运行 {command} --state needs_input --note '具体问题和必要背景' --recommendation '推荐方案及原因'。{input_route}
无法在原边界内修复时，运行 {command} --state blocked --note '根因和所需处理'，不要留下虚假运行状态。
修复登记命令：{command} --state repaired --evidence /absolute/path/to/repair-evidence.md --note '具体修复和验证'。不得伪造修复证据；report-only请求禁止此操作。
接手后不要只报告修复完成就结束；恢复需核实真实执行。supervisor会观察后续运行；当前请求最多处理一次，到期后不得迟到恢复。
'''


def takeover_status(directory, request_id, *, check_repo_lock=True):
    """Inspect eligibility without changing the ledger or acquiring ownership."""
    d = Path(directory).resolve()
    r = core.read(d/'recovery.json')
    current, status, gen, episode, _, _ = snapshot(d)
    requests = r.get('requests', [])
    request = next ( (q for q in requests if q['id'] == request_id), None)
    reasons = []
    if not r.get('enabled'):
        reasons.append('disabled')
    if (d/'STOP').exists():
        reasons.append('stopped')
    if (d/'user-closure.json').exists() or current['status'] in ('completed', 'stopped'):
        reasons.append('workflow_closed')
    if request is None:
        reasons.append('unknown_request')
    else:
        if request is not requests[-1]:
            reasons.append('replaced_request')
        if request.get('state') not in ('queued', 'working'):
            reasons.append('already_handled')
        if core.now() >= request['expires_at']:
            reasons.append('expired')
        if core.now() >= r['deadline'] and request.get('mode') != 'report':
            reasons.append('supervision_window_expired')
        if gen != request.get('generation'):
            reasons.append('generation_changed')
        if episode != request.get('episode') and gen == request.get('generation'):
            reasons.append('status_changed')
        delivery = request.get('delivery') or {}
        if delivery.get('authority') == 'managed_mailbox':
            try:
                import transport
                bus = transport.managed_bus()
                bus.recover()
                envelope = bus.message(delivery['mailbox_id'])
                if (envelope['canonical_id'] != request_id or envelope['task_id'] != str(d)
                        or envelope['contract_hash'] != r['contract_hash']
                        or json.loads(envelope['generation']) != request.get('generation')
                        or envelope['state'] not in ('delivered', 'accepted', 'responded', 'turn_completed')):
                    reasons.append('canonical_delivery_unavailable')
            except (ValueError, OSError, KeyError):
                reasons.append('canonical_delivery_unavailable')
    try:
        if origin_reference(core.read(d/'origin.json')) != r['thread_id']:
            reasons.append('binding_changed')
    except (ValueError, KeyError, OSError):
        reasons.append('binding_changed')
    if (core.digest(core.read(d/'contract.json')) != r['contract_hash']
            or current.get('contract_hash') != r['contract_hash']):
        reasons.append('contract_changed')
    if alive(current.get('owner_pid')):
        reasons.append('owner_active')
    import workflow_closure
    if current.get('active_run') and workflow_closure.active_execution(
            core.read(Path(current['active_run'])/'state.json')):
        reasons.append('stage_active')
    if workflow_closure.active_execution(dict(current, owner_pid=None)):
        reasons.append('process_active')
    lease = current_execution_owner(current)
    if lease and (not request or lease['owner'] != request['id']):
        reasons.append('execution_lease_active')
    if session_router.is_reference(r['thread_id']):
        try:
            row, _ = session_router.resolve(r['thread_id'])
            if row['version'] == 2:
                activity = session_router.activity_v2(r['thread_id'], generation=current.get('execution_revision', 0))
                if activity.state.value == 'unknown':
                    reasons.append('origin_activity_unknown')
                elif activity.state.value == 'active' and (not request or not activity.turn_id
                        or activity.turn_id != request.get('observed_turn')):
                    reasons.append('origin_other_turn_active')
        except (ValueError, OSError, KeyError):
            reasons.append('origin_activity_unknown')
    lock_path = Path(current['repo'])/'.codex/tasks/.worker.lock'
    try:
        # Do not create a lock file as a side effect of a read-only query.
        if not lock_path.parent.is_dir():
            reasons.append('repository_unavailable')
        elif check_repo_lock and lock_path.exists():
            with lock_path.open('r') as handle:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
    except BlockingIOError:
        reasons.append('repository_busy')
    except OSError:
        reasons.append('repository_unavailable')
    return dict(eligible=not reasons, reasons=reasons, request=request,
                current=dict(generation=gen, status=status, raw_status=current.get('status'),
                             stopped=(d/'STOP').exists(), owner_pid=current.get('owner_pid'),
                             supervision_enabled=r.get('enabled'), deadline=r.get('deadline')),
                next_action=('Claim only after rechecking eligibility' if not reasons else
                             'Do not claim; inspect the listed reasons and current workflow evidence'))


def takeover(directory, request_id, state, note, evidence=None, recommendation=''):
    d = Path(directory).resolve()
    if state not in ('working', 'repaired', 'needs_input', 'blocked') or not note.strip():
        raise ValueError('takeover requires state working/repaired/needs_input/blocked and a note')
    with locked(d/'recovery.lock'):
        r = core.read(d/'recovery.json')
        if session_router.is_reference(r['thread_id']):
            session_router.actor(r['thread_id'])
        diagnostic = takeover_status(d, request_id)
        if not diagnostic['eligible']:
            raise ValueError('takeover rejected: '+', '.join(diagnostic['reasons']))
        request = next(q for q in r['requests'] if q['id'] == request_id)
        current = core.read(d/'state.json')
        with locked(Path(current['repo'])/'.codex/tasks/.worker.lock', blocking=False):
            diagnostic = takeover_status(d, request_id, check_repo_lock=False)
            if not diagnostic['eligible']:
                raise ValueError('takeover rejected: '+', '.join(diagnostic['reasons']))
            if state == 'repaired':
                if request['mode'] != 'repair' or not evidence or not Path(evidence).is_file():
                    raise ValueError('repair requires actual evidence and a repair-mode request')
                request['repair_evidence'] = str(Path(evidence).resolve())
                request['repair_checksum'] = hashlib.sha256(Path(evidence).read_bytes()).hexdigest()
            if state == 'working':
                origin_lease(d, current, request)
            else:
                origin_lease(d, current, request, release=True)
            request.update(state=state, note=note, acknowledged_at=core.now())
            if state == 'working':
                request.setdefault('responded_at', core.now())
            r['status'] = state
            core.save(d/'recovery.json', r)
    result = summary(d)
    if state == 'needs_input':
        import interaction
        try:
            result['interaction'] = interaction.ask(d, note, recommendation,
                                                    context='接管 '+request_id+' 需要用户输入', request_id=request_id)
        except (ValueError, OSError, KeyError) as exc:
            result['interaction'] = {'routing_error': str(exc), 'retry': 'Use ctask ask with the same --request ID; do not repeat takeover.'}
    return result


def tick(directory):
    """One deterministic step. Queue transport is at-most-once, never blind-retried."""
    d = Path(directory).resolve()
    with locked(d/'recovery.lock'):
        r = core.read(d/'recovery.json')
        def finish(status, keep=False):
            r['status'] = status
            r['updated_at'] = core.now()
            core.save(d/'recovery.json', r)
            if status in ALERT_STATES and r['requests'] and not r['requests'][-1].get('notification'):
                import interaction
                request = r['requests'][-1]
                try:
                    result = interaction.supervision_alert(d, request['id'])
                    request['notification'] = {k: result[k] for k in
                        ('id', 'hub_thread', 'question_delivery', 'routed', 'reason') if k in result}
                except (ValueError, OSError, KeyError) as exc:
                    request['notification'] = {'routing_error': str(exc)}
                core.save(d/'recovery.json', r)
            return keep
        if not r['enabled'] or (d/'STOP').exists():
            return finish('disabled' if not r['enabled'] else 'stopped')
        if (d/'user-closure.json').exists():
            return finish('user_closed')
        state, status, gen, episode, fingerprint, reason = snapshot(d)
        if (core.digest(core.read(d/'contract.json')) != r['contract_hash']
                or state.get('contract_hash') != r['contract_hash']
                or origin_reference(core.read(d/'origin.json')) != r['thread_id']):
            return finish('binding_changed')
        latest = r['requests'][-1] if r['requests'] else None
        if latest:
            if gen != latest['generation'] and latest['state'] not in ('resumed', 'completed'):
                if (gen[0] or 0) > (latest['generation'][0] or 0):
                    latest.update(state='resumed', resumed_at=core.now())
                else:
                    latest.update(state='superseded', superseded_at=core.now())
            elif gen == latest['generation']:
                observe_response(r, latest)
        if status == 'completed':
            if latest and latest['state'] == 'resumed':
                latest['state'] = 'completed'
            return finish('completed')
        if status in ('stopped', 'plan_changed'):
            return finish(status)
        window_expired = core.now() >= r['deadline']
        launch = d/'launch.json'
        if launch.exists():
            info = core.read(launch)
            if core.now()-info['at'] < 15 and alive(info['pid']):
                return finish('watching', True)
        if alive(state.get('owner_pid')):
            return finish('watching', True)
        lease = current_execution_owner(state)
        if lease and (not latest or lease['owner'] != latest['id']):
            return finish('execution_owned', True)
        # v2 activity is explicit evidence. A worker-capable adapter or an
        # unobservable session must never be mistaken for an idle origin.
        if session_router.is_reference(r['thread_id']):
            session_row, _ = session_router.resolve(r['thread_id'])
            if session_row['version'] == 2:
                activity = session_router.activity(r['thread_id'], generation=state.get('execution_revision', 0))
                if activity != 'idle' and (not latest or latest['state'] == 'repaired'):
                    return finish('origin_'+activity, True)
        if latest and latest['episode'] == episode:
            if latest['state'] in ('needs_input', 'blocked', 'stalled', 'delivery_unknown', 'send_failed', 'resume_failed'):
                return finish(latest['state'])
            if latest['state'] == 'repaired':
                proof = Path(latest['repair_evidence'])
                if (core.now() >= latest['expires_at'] or not proof.is_file()
                        or hashlib.sha256(proof.read_bytes()).hexdigest() != latest['repair_checksum']):
                    latest['state'] = 'stalled'
                    return finish('stalled')
                latest['state'] = 'resume_launching'
                finish('resume_launching')  # A crash cannot cause another launch.
                try:
                    result = subprocess.run(core.runtime_command('resume', d, '--retry-failed'),
                                            capture_output=True, text=True, timeout=25)
                    latest['resume_receipt'] = (result.stdout + result.stderr)[-4000:]
                    latest['state'] = 'resuming' if result.returncode == 0 else 'resume_failed'
                except (OSError, subprocess.TimeoutExpired) as exc:
                    latest.update(state='resume_failed', resume_receipt=str(exc))
                return finish(latest['state'], latest['state'] == 'resuming')
            if latest['state'] in ('resume_launching', 'resuming'):
                if core.now() >= latest['expires_at']:
                    latest['state'] = 'resume_failed'
                    return finish('resume_failed')
                return finish(latest['state'], True)
            if latest['state'] == 'dispatching':
                latest['state'] = 'delivery_unknown'
                return finish('delivery_unknown')
            if latest['state'] in ('queued', 'working'):
                if core.now() >= latest['expires_at']:
                    latest['state'] = 'stalled'
                    return finish('stalled')
                return finish('working' if latest.get('responded_at') else 'queued', True)
            return finish('already_handled')
        if status not in REPAIR | REPORT:
            return finish('watching', True)
        escalation = None
        if window_expired:
            escalation = 'window_exhausted'
        elif sum(q['mode'] == 'repair' for q in r['requests']) >= r['max_requests']:
            escalation = 'budget_exhausted'
        elif sum(q['fingerprint'] == fingerprint and q['mode'] == 'repair'
                 for q in r['requests']) >= r['same_blocker_limit']:
            escalation = 'repeated_blocker'
        if escalation:
            if any(q.get('escalation') for q in r['requests']):
                return finish(escalation)
            reason['supervision_limit'] = escalation
        try:
            with locked(Path(state['repo'])/'.codex/tasks/.worker.lock', blocking=False):
                report = status in REPORT or bool(escalation)
                checkpoint = session_router.delivery_checkpoint(r['thread_id'], r['codex_home'])
                request = dict(id='recover-'+uuid.uuid4().hex[:16], episode=episode,
                               fingerprint=fingerprint, generation=gen, state='dispatching',
                               mode='report' if report else 'repair', escalation=escalation,
                               sent_at=core.now(), expires_at=(core.now()+r['response_seconds'] if report
                                   else min(r['deadline'], core.now()+r['response_seconds'])),
                               rollout_offset=checkpoint.get('rollout_offset'))
                if 'event_cursor' in checkpoint:
                    request['event_cursor'] = checkpoint['event_cursor']
                request['message'] = message(d, r, request, state, reason)
                import transport
                if session_router.managed_delivery_supported(r['thread_id']):
                    request['delivery'] = transport.prepare_managed(r['thread_id'], request['message'],
                        canonical_id=request['id'], phase='recovery', task_id=str(d),
                        contract_hash=r['contract_hash'], generation=gen, project=state['repo'],
                        source=r['source'], home_dir=r['codex_home'], expires_at=request['expires_at'])
                r['requests'].append(request)
                finish('dispatching')  # Durable BEFORE sending, including crash windows.
                if request.get('delivery'):
                    request['delivery'] = transport.dispatch_managed(request['delivery'],
                        home_dir=r['codex_home'], cwd=state['repo'])
                    request.update({k: request['delivery'][k] for k in ('state', 'message_id')})
                else:
                    try:
                        result = session_router.send(r['thread_id'], request['message'],
                            home_dir=r['codex_home'], cwd=state['repo'])
                        request.update(session_router.delivery_fields(result))
                    except Exception:
                        request['state'] = 'delivery_unknown'
                return finish(request['state'], request['state'] == 'queued')
        except BlockingIOError:
            return finish('repository_busy', True)


def start(directory):
    d = Path(directory).resolve()
    if not (d/'recovery.json').exists() or not core.read(d/'recovery.json')['enabled']:
        return
    with locked(d/'supervisor-start.lock'):
        path = d/'supervisor.json'
        if path.exists() and pid_alive(core.read(path)):
            return
        with (d/'supervisor.log').open('ab') as log:
            proc = subprocess.Popen(core.runtime_command('_supervisor', d), stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log, start_new_session=True)
        core.save(path, dict(pid=proc.pid, pid_identity=core.process_identity(proc.pid), heartbeat=core.now(), status='launching'))


def watch(directory, interval=5):
    d = Path(directory).resolve()
    with locked(d/'supervisor-start.lock'):
        pass  # Let the launcher persist its PID before writing the first heartbeat.
    with locked(d/'supervisor.lock', blocking=False):
        stop = False
        def interrupt(signum, frame):
            nonlocal stop
            stop = True
        signal.signal(signal.SIGTERM, interrupt)
        signal.signal(signal.SIGINT, interrupt)
        watch_ticks(lambda: tick(d), d/'supervisor.json', d/'supervisor.log',
                    'status', interval, lambda: stop)
