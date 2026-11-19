"""Explicit creation of a durable coordinating conversation for a new workflow."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import appserver
import interaction
import recovery
import runner as core
import workflow


def public(d):
    d = Path(d).resolve()
    r = core.read(d/'delegation.json')
    result = {k: r.get(k) for k in ('id', 'repo', 'name', 'state', 'hub_thread',
              'origin_thread', 'workflow_dir', 'source', 'error', 'message_id',
              'accepted_at', 'started_at', 'deadline')}
    result['directory'] = str(d)
    if r.get('workflow_dir'):
        s = core.status(r['workflow_dir'])
        result['workflow'] = {k: s.get(k) for k in ('status', 'observed_status', 'remaining',
                              'owner_pid', 'completion_scope', 'recovery', 'interaction')}
    return result


def persisted(d, **fields):
    r = core.read(d/'delegation.json')
    r.update(fields, updated_at=core.now())
    core.save(d/'delegation.json', r)
    return r


def preflight(repo):
    repo = Path(repo).resolve()
    if not repo.is_dir():
        raise ValueError('target directory must already exist')
    result = subprocess.run(['git', '-C', str(repo), 'rev-parse', '--show-toplevel'],
                            capture_output=True, text=True)
    if result.returncode:
        raise ValueError('target must be an existing Git project; initialize the requested project before delegation')
    repo = Path(result.stdout.strip()).resolve()
    (repo/'.codex/tasks/delegations').mkdir(parents=True, exist_ok=True)
    return repo


def delegate(repo, plan, name, source, request_id=None, timeout=600, session_kind='codex'):
    if not name.strip() or not source.strip() or type(timeout) is not int or timeout < 30:
        raise ValueError('delegation requires a name, explicit user instruction source and timeout >=30 seconds')
    hub = interaction.config()
    if not hub:
        raise ValueError('configure and verify the interaction hub first')
    interaction.actor(hub['thread_id'])
    interaction.verify_target(hub['thread_id'])
    contract = workflow.validate(core.read(plan))
    import agent_registry
    adapter = agent_registry.get(session_kind, 'session_create')
    if not callable(getattr(adapter, 'create_session', None)):
        raise ValueError('adapter has no session creation implementation')
    if session_kind != 'codex':
        for capability, method in (('session_verify', 'verify_session'), ('session_actor', 'is_actor'),
                                   ('session_observe', 'observe_session'), ('session_observe', 'session_cursor'),
                                   ('transport', 'send')):
            agent_registry.get(session_kind, capability)
            if not callable(getattr(adapter, method, None)):
                raise ValueError('adapter lacks required delegation method: '+method)
        if contract.get('permissions') or contract['sandbox'] == 'danger-full-access':
            raise ValueError('non-Codex delegation cannot widen worker permissions')
    repo = preflight(repo)
    fingerprint_data = [str(repo), contract, name, source, hub['thread_id']]
    if session_kind != 'codex':
        fingerprint_data.append(session_kind)
    fingerprint = core.digest(fingerprint_data)
    identifier = request_id or 'delegate-'+fingerprint[:20]
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', identifier):
        raise ValueError('invalid delegation request ID')
    folder = repo/'.codex/tasks/delegations'
    d = folder/identifier
    with recovery.locked(folder/'dispatch.lock'):
        if (d/'delegation.json').exists():
            if core.read(d/'delegation.json')['fingerprint'] != fingerprint:
                raise ValueError('request ID already belongs to a different task')
            return public(d)  # No second conversation or message after an uncertain outcome.
        for p in folder.glob('*/delegation.json'):
            old = core.read(p)
            if old['state'] not in ('started', 'expired', 'start_failed'):
                raise ValueError(f'an unresolved delegation already exists: {p.parent}')
        with recovery.locked(repo/'.codex/tasks/.worker.lock', blocking=False):
            for p in (repo/'.codex/tasks/workflows').glob('*/state.json'):
                s = core.read(p)
                if s['status'] not in ('completed', 'stopped') and not (p.parent/'STOP').exists():
                    raise ValueError(f'an unfinished workflow already owns this project: {p.parent}; use its origin or an isolated worktree')
            d.mkdir()
            core.save(d/'contract-input.json', contract)
            core.save(d/'delegation.json', dict(id=identifier, repo=str(repo), name=name,
                      fingerprint=fingerprint, contract_hash=core.digest(contract), source=source,
                      hub_thread=hub['thread_id'], codex_home=str(interaction.home()),
                      created_at=core.now(), deadline=core.now()+timeout, state='prepared'))
            # No model is run by thread/start. Persist before every irreversible RPC.
            try:
                persisted(d, state='creating')
                created = adapter.create_session(repo, contract, name, identifier,
                                                 lambda **kw: persisted(d, **kw))
                if (not isinstance(created, agent_registry.CreatedSession)
                        or Path(created.project).resolve() != repo or created.sandbox != contract['sandbox']
                        or created.approval != 'never' or not created.source.strip()):
                    raise ValueError('created session did not prove the requested identity and policy')
                saved_path = created.legacy_rollout
                if saved_path:
                    if session_kind != 'codex':
                        raise ValueError('legacy Codex rollout is reserved for the Codex bridge')
                    origin = created.identity
                    w = workflow.create(repo, d/'contract-input.json', origin)
                else:
                    import session_router
                    origin = session_router.register(session_kind, created.identity, repo, created.source)
                    w = workflow.create(repo, d/'contract-input.json', origin_ref=origin)
                persisted(d, origin_thread=origin)
                core.save(w/'delegation.json', {'directory': str(d), 'hub_thread': hub['thread_id'],
                                               'origin_thread': origin, 'source': source})
                (w/'handoff.md').write_text('Independent coordinating conversation\n\n'
                    f'User delegation source: {source}\nHub: {hub["thread_id"]}\nOrigin: {origin}\n'
                    f'Delegation: {d}\nFull objective and final acceptance: contract.json\n'
                    'The dispatcher starts this exact workflow after the origin accepts. No replacement task or second worker.\n')
                recovery.enable(w, source)
                persisted(d, state='workflow_created', workflow_dir=str(w))
            except Exception as exc:
                r = core.read(d/'delegation.json')
                persisted(d, state='creation_unknown' if r['state'] == 'creating' else 'setup_failed', error=str(exc))
                return public(d)
        # Queue only after the identity, persistence, cwd and full contract are verified.
        message = f'''[ctask-delegation id={identifier}]
用户明确要求在目标工程新建独立执行/协调会话；你是该总任务的原协调会话，不是阶段worker。
用户指令来源: {source}
工作目录: {repo}
统一交互入口: {hub['thread_id']}
完整总任务: {w}
请读取 contract.json、handoff.md 和最新版 codex-long-task skill，保留完整目标、范围、授权和最终验收。
先执行 ctask delegation-accept {str(d)!r}，验证本会话ID及绑定；失败则不要执行任务。
接受后本机派发器负责启动这个已建好的总任务；你不要再 start/create、另起worker或同时改业务代码。当前轮只核实和接受，报告实际状态即可。
后续阻塞会由监督进程通知本会话，按原授权排查并登记修复证据，监督会恢复同一总任务。需要用户决定时通过 ctask ask 或 takeover needs_input 送到统一入口，不要求用户切换窗口。不使用goal，不自动提交/推送/部署或增加业务权限。
'''
        persisted(d, state='dispatching', kickoff=message,
                  rollout_offset=Path(saved_path).stat().st_size if saved_path else None)
        try:
            if saved_path:
                import codex_legacy
                result, receipt_id = codex_legacy.queue(interaction.home(), origin, message)
                state = 'queued' if receipt_id else 'delivery_unknown'
                persisted(d, state=state, message_id=receipt_id, receipt=result.stdout[-2000:])
            else:
                import session_router
                result = session_router.send(origin, message)
                persisted(d, state={'delivered': 'queued', 'failed': 'send_failed',
                                    'delivery_unknown': 'delivery_unknown'}[result.state],
                          message_id=result.external_id)
        except Exception:
            persisted(d, state='delivery_unknown', error='transport result unavailable')
        start_bridge(d)
    return public(d)


def accept(directory):
    d = Path(directory).resolve()
    with recovery.locked(d.parent/'dispatch.lock'):
        r = core.read(d/'delegation.json')
        interaction.actor(r['origin_thread'])
        if r['state'] not in ('queued', 'delivery_unknown') or core.now() >= r['deadline']:
            raise ValueError('delegation is stale, expired or already accepted')
        w = Path(r['workflow_dir']);s = core.read(w/'state.json')
        if (core.digest(core.read(w/'contract.json')) != r['contract_hash']
                or recovery.origin_reference(core.read(w/'origin.json')) != r['origin_thread']
                or (w/'STOP').exists() or s['status'] != 'created'):
            raise ValueError('workflow binding changed, stopped or already started')
        persisted(d, state='accepted', accepted_at=core.now())
    return public(d)


def start_bridge(d):
    with (d/'dispatcher.log').open('ab') as log:
        proc = subprocess.Popen(core.runtime_command('_dispatcher', d),
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
    core.save(d/'dispatcher.json', {'pid': proc.pid, 'started_at': core.now()})
    return proc


def watch(directory):
    d = Path(directory).resolve()
    with recovery.locked(d/'dispatcher.lock', blocking=False):
        while True:
            with recovery.locked(d.parent/'dispatch.lock'):
                r = core.read(d/'delegation.json')
                w = Path(r['workflow_dir']);s = core.read(w/'state.json')
                if ((w/'STOP').exists() or core.now() >= r['deadline']
                        or core.digest(core.read(w/'contract.json')) != r['contract_hash']):
                    persisted(d, state='expired', error='Stopped, expired or contract changed before launch')
                    return
                if r['state'] == 'accepted':
                    persisted(d, state='launching')
                    try:
                        result = subprocess.run(core.runtime_command('start', w),
                                                 capture_output=True, text=True, timeout=25)
                        persisted(d, launch_receipt=(result.stdout+result.stderr)[-2000:])
                        if result.returncode:
                            persisted(d, state='start_failed', error=result.stderr[-2000:])
                            return
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        persisted(d, state='start_failed', error=str(exc))
                        return
                elif r['state'] == 'launching':
                    if recovery.generation(w, s)[0] is not None:
                        persisted(d, state='started', started_at=core.now())
                        return
                elif r['state'] not in ('queued', 'dispatching', 'delivery_unknown'):
                    return
            time.sleep(1)
