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
import sqlite3
import subprocess
import sys
import time
import uuid

import mailbox_routes as mailbox
import runner as core

ACTIVE = {'running', 'planning', 'verifying', 'waiting'}
REPAIR = {'blocked', 'error', 'cleanup_blocked', 'interrupted'}
REPORT = {'needs_input', 'limit', 'stage_limit', 'planning_limit', 'contract_changed'}
ALERT_STATES = {'stalled', 'blocked', 'delivery_unknown', 'send_failed', 'resume_failed'}


@contextlib.contextmanager
def locked(path, blocking=True):
    with Path(path).open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield


def alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Unknown ownership must not cause another execution.


def session_path(home, thread):
    """Read only the exact thread index; never infer from the latest session."""
    for db in sorted(Path(home).glob('state_*.sqlite'), reverse=True):
        try:
            with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True, timeout=2) as con:
                row = con.execute('select rollout_path from threads where id=?', (thread,)).fetchone()
            if row:
                path = Path(row[0]).resolve()
                with path.open() as handle:
                    meta = json.loads(handle.readline())
                if meta.get('payload', {}).get('id') == thread:
                    return str(path)
        except (OSError, ValueError, sqlite3.Error):
            continue
    raise ValueError('original session cannot be verified in the configured Codex home')


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
    thread = mailbox.origin_reference(origin)
    home = Path(origin['codex_home']).resolve()
    ep = mailbox.verify_origin(origin, state['repo'])
    rollout = session_path(home, thread) if ep['kind'] == 'codex' else None
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
    return dict(enabled=r['enabled'], status=r['status'], requests=len(r['requests']),
                max_requests=r['max_requests'], deadline=r['deadline'],
                supervisor_pid=watch.get('pid'), supervisor_alive=alive(watch.get('pid')),
                heartbeat=watch.get('heartbeat'), last_error=watch.get('error'),
                latest={k: latest.get(k) for k in ('id', 'state', 'message_id', 'sent_at',
                       'responded_at', 'resumed_at', 'note', 'notification')} if latest else None)


def generation(d, state):
    # A very fast resume can finish between watcher polls. The durable start event
    # still proves execution occurred, unlike a model's claim that it resumed.
    last = None
    path = d/'events.jsonl'
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('event') == 'workflow_started':
                last = event['time']
    return [last, state.get('execution_revision', 0)]


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
    if not r.get('rollout'):
        delivery = request.get('delivery') or {}
        if delivery.get('mailbox_id'):
            m = mailbox.bus().message(delivery['mailbox_id'])
            if m.get('accepted_at'):
                request.setdefault('responded_at', m['accepted_at'])
            if mailbox.response(m['id']):
                request.setdefault('turn_completed_at', core.now())
        return
    path = Path(r['rollout'])
    if not path.exists():
        return
    with path.open('rb') as handle:
        handle.seek(request.get('scan_offset', request['rollout_offset']))
        for _ in range(1000):
            line = handle.readline()
            if not line:
                break
            if not line.endswith(b'\n'):
                break
            request['scan_offset'] = handle.tell()
            try:
                e = json.loads(line)
            except ValueError:
                continue
            p = e.get('payload', {})
            if (e.get('type') == 'response_item' and p.get('role') == 'user'
                    and request['id'] in json.dumps(p, ensure_ascii=False)):
                request['seen_in_session'] = True
            if (e.get('type') == 'event_msg' and p.get('type') == 'user_message'
                    and request['id'] in p.get('message', '')):
                request['seen_in_session'] = True
            if request.get('seen_in_session'):
                if e.get('type') == 'response_item' and p.get('role') == 'assistant':
                    request.setdefault('responded_at', core.now())
                if e.get('type') == 'event_msg' and p.get('type') == 'task_complete':
                    request.setdefault('turn_completed_at', core.now())


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


def takeover(directory, request_id, state, note, evidence=None, recommendation='', *, credential=None):
    d = Path(directory).resolve()
    if state not in ('working', 'repaired', 'needs_input', 'blocked') or not note.strip():
        raise ValueError('takeover requires state working/repaired/needs_input/blocked and a note')
    with locked(d/'recovery.lock'):
        r = core.read(d/'recovery.json')
        if credential is not None:
            mailbox.actor_credential(r['thread_id'], credential)
        elif r['thread_id'].startswith('managed:'):
            mailbox.actor(r['thread_id'])
        request = next((q for q in r['requests'] if q['id'] == request_id), None)
        current, status, gen, episode, _, _ = snapshot(d)
        if (not r['enabled'] or (d/'STOP').exists()
                or not request or request != r['requests'][-1]
                or (core.now() >= r['deadline'] and request['mode'] != 'report')
                or request['state'] not in ('queued', 'working')
                or core.now() >= request['expires_at'] or episode != request['episode']
                or mailbox.origin_reference(core.read(d/'origin.json')) != r['thread_id']
                or core.digest(core.read(d/'contract.json')) != r['contract_hash']):
            raise ValueError('takeover is stale, disabled, expired, changed or already resolved')
        with locked(Path(current['repo'])/'.codex/tasks/.worker.lock', blocking=False):
            if alive(current.get('owner_pid')):
                raise ValueError('workflow owner is still alive; do not take over')
            if state == 'repaired':
                if request['mode'] != 'repair' or not evidence or not Path(evidence).is_file():
                    raise ValueError('repair requires actual evidence and a repair-mode request')
                request['repair_evidence'] = str(Path(evidence).resolve())
                request['repair_checksum'] = hashlib.sha256(Path(evidence).read_bytes()).hexdigest()
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
                                                    context='接管 '+request_id+' 需要用户输入', request_id=request_id,
                                                    credential=credential)
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
            if status in ALERT_STATES and r['requests']:
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
            mailbox.invalidate(d, source='supervision stopped or disabled',
                               kind=None if (d/'STOP').exists() else 'recovery')
            return finish('disabled' if not r['enabled'] else 'stopped')
        state, status, gen, episode, fingerprint, reason = snapshot(d)
        try:
            mailbox.verify_origin(core.read(d/'origin.json'), state['repo'])
        except (ValueError, OSError, KeyError):
            mailbox.invalidate(d, source='recovery origin unavailable')
            return finish('binding_changed')
        if (core.digest(core.read(d/'contract.json')) != r['contract_hash']
                or mailbox.origin_reference(core.read(d/'origin.json')) != r['thread_id']):
            return finish('binding_changed')
        latest = r['requests'][-1] if r['requests'] else None
        if latest:
            observe_response(r, latest)
            if gen != latest['generation'] and latest['state'] not in ('resumed', 'completed'):
                latest.update(state='resumed', resumed_at=core.now())
        if status == 'completed':
            if latest and latest['state'] == 'resumed':
                latest['state'] = 'completed'
            return finish('completed')
        if status in ('stopped', 'plan_changed', 'paused'):
            return finish(status)
        window_expired = core.now() >= r['deadline']
        launch = d/'launch.json'
        if launch.exists():
            info = core.read(launch)
            if core.now()-info['at'] < 15 and alive(info['pid']):
                return finish('watching', True)
        if alive(state.get('owner_pid')):
            return finish('watching', True)
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
                    result = subprocess.run([sys.executable, str(Path(core.__file__).resolve()),
                                             'resume', str(d), '--retry-failed'],
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
                request = dict(id='recover-'+uuid.uuid4().hex[:16], episode=episode,
                               fingerprint=fingerprint, generation=gen, state='dispatching',
                               mode='report' if report else 'repair', escalation=escalation,
                               sent_at=core.now(), expires_at=(core.now()+r['response_seconds'] if report
                                   else min(r['deadline'], core.now()+r['response_seconds'])),
                               rollout_offset=mailbox.offset(r['thread_id']))
                request['message'] = message(d, r, request, state, reason)
                r['requests'].append(request)
                finish('dispatching')  # Durable BEFORE sending, including crash windows.
                delivery = mailbox.send(target=r['thread_id'], text=request['message'],
                    key='recovery:'+request['id'], task=str(d), contract_hash=r['contract_hash'],
                    source=r['source'], expires_at=request['expires_at'])
                request.update(state=delivery['state'], delivery=delivery)
                if delivery.get('message_id'):
                    request['message_id'] = delivery['message_id']
                return finish(request['state'], request['state'] == 'queued')
        except BlockingIOError:
            return finish('repository_busy', True)


def start(directory):
    d = Path(directory).resolve()
    if not (d/'recovery.json').exists() or not core.read(d/'recovery.json')['enabled']:
        return
    with locked(d/'supervisor-start.lock'):
        path = d/'supervisor.json'
        if path.exists() and alive(core.read(path).get('pid')):
            return
        with (d/'supervisor.log').open('ab') as log:
            proc = subprocess.Popen([sys.executable, str(Path(core.__file__).resolve()),
                                     '_supervisor', str(d)], stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=log, start_new_session=True)
        core.save(path, dict(pid=proc.pid, heartbeat=core.now(), status='launching'))


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
        error = None
        try:
            while not stop:
                keep = tick(d)
                core.save(d/'supervisor.json', dict(pid=os.getpid(), heartbeat=core.now(), status='watching'))
                if not keep:
                    break
                time.sleep(interval)
        except Exception as exc:
            error = str(exc)
        finally:
            core.save(d/'supervisor.json', dict(pid=None, heartbeat=core.now(), status='exited', error=error))
