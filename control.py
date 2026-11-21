"""Persistent hub follow-ups and observed CLI progress. No business execution or grants."""
import contextlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time

import interaction
import notifications
import recovery
import session_router
import runner as core

TERMINAL = {'resolved', 'stale', 'stopped', 'completed', 'superseded'}


def directory():
    p = interaction.directory()/'control'
    p.mkdir(exist_ok=True)
    return p


@contextlib.contextmanager
def database():
    con = sqlite3.connect(directory()/'state.sqlite', timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('pragma journal_mode=WAL')
    con.execute('pragma synchronous=FULL')
    con.executescript('''
      create table if not exists actions(id text primary key, body text not null);
      create table if not exists events(seq integer primary key autoincrement,
        key text unique not null, at real not null, action_id text, kind text not null, body text not null);
      create table if not exists outbox(key text primary key, body text not null);
      create table if not exists workflows(path text primary key, body text not null);
    ''')
    try:
        yield con
    finally:
        con.close()


def event(con, key, identifier, kind, body):
    con.execute('insert or ignore into events(key,at,action_id,kind,body) values(?,?,?,?,?)',
                (key, core.now(), identifier, kind, json.dumps(body, ensure_ascii=False)))


def put(con, action, kind=None):
    old = con.execute('select body from actions where id=?', (action['id'],)).fetchone()
    if old and json.loads(old[0]) == action:
        return
    action['updated_at'] = core.now()
    con.execute('insert into actions values(?,?) on conflict(id) do update set body=excluded.body',
                (action['id'], json.dumps(action, ensure_ascii=False)))
    event(con, core.digest([action['id'], action]), action['id'], kind or 'action_updated', action)


def settings():
    p = directory()/'config.json'
    return core.read(p) if p.exists() else None


def enable(source, reminder_seconds=900, max_reminders=3):
    hub = interaction.config()
    if not hub or not source.strip():
        raise ValueError('control requires a verified hub and authorization source')
    interaction.actor(hub['thread_id'])
    if type(reminder_seconds) is not int or reminder_seconds < 60 or type(max_reminders) is not int or max_reminders < 0:
        raise ValueError('reminder interval must be >=60s; reminder count must be nonnegative')
    with recovery.locked(directory()/'config.lock'):
        prior = settings()
        if prior and prior['hub_thread'] != hub['thread_id']:
            process = directory()/'process.json'
            if prior['enabled'] or (process.exists() and recovery.alive(core.read(process).get('pid'))):
                raise ValueError('stop the controller and wait for exit before binding another hub')
        cfg = dict(enabled=True, hub_thread=hub['thread_id'], source=source,
                   reminder_seconds=reminder_seconds, max_reminders=max_reminders,
                   created_at=prior['created_at'] if prior else core.now())
        core.save(directory()/'config.json', cfg)
    return cfg


def disable():
    cfg = settings()
    if cfg:
        interaction.actor(cfg['hub_thread'])
        cfg['enabled'] = False
        core.save(directory()/'config.json', cfg)
    return cfg


def register(workflow):
    """Only observe a workflow already bound to this hub; no recovery authorization is added."""
    cfg = settings()
    if not cfg or not cfg['enabled']:
        return
    d, state, contract, origin = interaction.binding(workflow, require_open=False)
    if (interaction.config() or {}).get('thread_id') != cfg['hub_thread']:
        raise ValueError('controller hub binding changed')
    body = dict(workflow=str(d), origin=origin, hub=cfg['hub_thread'],
                contract_hash=state['contract_hash'], registered_at=core.now())
    with database() as con, con:
        con.execute('insert or ignore into workflows values(?,?)', (str(d), json.dumps(body)))


def send(key, target, message, *, workflow=None):
    """Mailbox authority with a compatibility projection in the controller DB."""
    import transport
    with database() as con:
        con.execute('begin immediate')
        old = con.execute('select body from outbox where key=?', (key,)).fetchone()
        if old:
            con.commit()
            row = json.loads(old[0])
            # Legacy outboxes are imported as already claimed, never replayed.
            if row.get('authority') == 'managed_mailbox':
                row = transport.project_managed(row)
            return row
        interaction.verify_target(target)
        import hashlib
        if workflow:
            d, state, _, _ = interaction.binding(workflow, require_open=False)
            task_id, contract_hash, gen, project = str(d), state['contract_hash'], recovery.generation(d, state), state['repo']
        else:
            task_id, contract_hash, gen, project = 'controller', core.digest(interaction.config()), [None, 0], str(interaction.directory())
        canonical_id, phase = key, 'followup'
        if workflow and key.startswith('host-handoff:'):
            import coordination
            # Recheck the live handoff after source binding; preserve full work
            # material privately and send only an identifier/read command.
            message = coordination.prepare(d, state, core.read(d/'contract.json'), target, key, message)
            phase = 'host_handoff'
        if workflow and key.startswith('followup:failure-'):
            import failure_notice
            try:
                message = failure_notice.prepare(key, target, workflow, message)
            except failure_notice.StaleFailure:
                row = dict(key=key, state='recorded_only', authority='none',
                    at=core.now(), reason='failure is stale or already being handled')
                con.execute('insert into outbox values(?,?)', (key, json.dumps(row)))
                event(con, 'outbox:'+key, key, 'stale_source_not_sent', row)
                con.commit()
                return row
        source = (settings() or interaction.config() or {}).get('source', 'existing controller routing')
        if key.startswith('followup:question-'):
            identifier = key.split(':')[1]
            question = core.read(interaction.question_path(identifier))
            try:
                d, state = interaction.validate_question(question)
            except ValueError:
                # Preserve the historical follow-up after an explicit resume,
                # but never mint a new envelope using its old generation.
                row = dict(key=key, state='recorded_only', authority='none',
                    at=core.now(), reason='canonical question is no longer current')
                con.execute('insert into outbox values(?,?)', (key, json.dumps(row)))
                event(con, 'outbox:'+key, key, 'stale_source_not_sent', row)
                con.commit()
                return row
            if target != question['hub_thread']:
                raise ValueError('follow-up cannot rebind the original question hub')
            canonical_id, phase = question['id'], 'followup:'+key.rsplit(':', 1)[1]
            task_id, contract_hash, gen, project = str(d), question['contract_hash'], question['generation'], state['repo']
            source = question['hub_source']
        if session_router.managed_delivery_supported(target):
            row = transport.prepare_managed(target, message, canonical_id=canonical_id, phase=phase,
                task_id=task_id, contract_hash=contract_hash, generation=gen, project=project,
                source=source)
        else:
            row = dict(state='dispatching', target=target, authority='legacy_submission')
        row.update(key=key, at=core.now(), message_sha256=hashlib.sha256(message.encode()).hexdigest())
        con.execute('insert into outbox values(?,?)', (key, json.dumps(row)))
        event(con, 'outbox:'+key, key, 'dispatching', row)
        con.commit()
    if row['authority'] == 'managed_mailbox':
        row = transport.dispatch_managed(row)
    else:
        try:
            if key.startswith('followup:result-') and message.startswith('[ctask-workflow-completed id='):
                result = session_router.send_completion(target, message, key)
            else:
                result = session_router.send(target, message)
            row.update(session_router.delivery_fields(result, 2000))
        except Exception:
            row['state'] = 'delivery_unknown'
    with database() as con, con:
        con.execute('update outbox set body=? where key=?', (json.dumps(row), key))
        event(con, 'receipt:'+key, key, 'delivery_receipt', row)
    return row


def rows(include_terminal=False):
    with database() as con:
        result = [json.loads(row[0]) for row in con.execute('select body from actions order by id')]
    return [row for row in result if include_terminal or row['state'] not in TERMINAL]


def source_error(identifier, hub, workflow, error):
    # Preserve prior delivery claims when a damaged source remains damaged.
    with database() as con, con:
        old = con.execute('select body from actions where id=?', (identifier,)).fetchone()
        action = json.loads(old[0]) if old else dict(id=identifier, created_at=core.now(), reminders=0)
        action.update(kind='source_error', hub=hub, workflow=workflow, state='source_error', error=error)
        put(con, action)


def record(identifier, state=None, note=None, evidence=None, workflow=None):
    with recovery.locked(directory()/'tick.lock'):
        with database() as con:
            found = con.execute('select body from actions where id=?', (identifier,)).fetchone()
        if not found:
            raise ValueError('unknown follow-up; reconcile first')
        action = json.loads(found[0])
        if workflow:
            # A continuation is explicit and checked; never guess one from its directory/name.
            q = core.read(interaction.question_path(identifier))
            if q['state'] != 'accepted':
                raise ValueError('continuation link requires the actual answer accepted by its origin')
            current = session_router.current_actor_locator()
            if current not in (q['origin_thread'], q['hub_thread']):
                raise ValueError('continuation bookkeeping requires its verified origin or hub')
            d, s, c, origin = interaction.binding(workflow, require_open=False)
            old_d, old_s, old_c, old_origin = interaction.binding(q['workflow'], require_open=False)
            signature = lambda x: core.digest([x['objective'], x['acceptance']])
            if (origin != old_origin or s['repo'] != old_s['repo'] or signature(c) != signature(old_c)):
                raise ValueError('continuation must retain origin, repository, objective and acceptance')
            proof = Path(evidence or '').resolve()
            if not proof.is_file() or not note or not note.strip():
                raise ValueError('continuation requires a real evidence file and explanatory note')
            if c['scope'] != old_c['scope']:
                try:
                    lineage = core.read(proof)
                except ValueError:
                    raise ValueError('changed scope text requires structured continuation lineage')
                if (not c['scope'].startswith(old_c['scope'])
                        or lineage.get('predecessor_workflow') != str(old_d)
                        or lineage.get('successor_workflow') != str(d)
                        or lineage.get('accepted_question') != identifier
                        or lineage.get('permission_expanded') is not False):
                    raise ValueError('scope annotation must retain old scope and explicit no-expansion lineage')
            action.update(workflow=str(d), linked_at=core.now(), link_note=note,
                          link_evidence=str(proof), link_checksum=core.digest(proof.read_text(errors='replace')),
                          baseline_generation=[None, 0], state='awaiting_execution')
            register(d)
            with database() as con, con:
                old = con.execute('select body from workflows where path=?', (str(old_d),)).fetchone()
                if old:
                    source = json.loads(old[0]); source['superseded_by'] = str(d)
                    con.execute('update workflows set body=? where path=?', (json.dumps(source), str(old_d)))
                    event(con, 'superseded:'+str(old_d)+':'+str(d), identifier, 'workflow_superseded',
                          dict(predecessor=str(old_d), successor=str(d), evidence=str(proof)))
        else:
            interaction.actor(action['hub'])
            if state != 'presented' or not note or not note.strip():
                raise ValueError('only explicit presentation with a user-facing source can be recorded')
            if action['state'] in TERMINAL:
                raise ValueError('closed follow-up cannot be presented as current')
            action.update(presented_at=core.now(), presentation_source=note, state='presented')
            if action['kind'] == 'workflow_result':
                d, s, c, origin = interaction.binding(action['workflow'], require_open=False)
                if s['status'] != 'completed':
                    raise ValueError('completion result is no longer current')
                action.update(state='resolved', resolution='overall completion reported in hub')
        with database() as con, con:
            put(con, action, 'continuation_linked' if workflow else 'presented')
        return action


def operational_disposition(identifier, category, blocker, tried, evidence, recommendation, source):
    """A bound human session records a specific intervention question, never an answer."""
    import hashlib
    if category not in ('handler_assignment', 'permission', 'decision', 'missing_information', 'manual_acceptance'):
        raise ValueError('specific intervention category required')
    if not all(isinstance(x, str) and x.strip() for x in (blocker, tried, recommendation, source)):
        raise ValueError('blocker, tried steps, recommendation and source are required')
    paths = [Path(p).resolve() for p in evidence]
    if not paths or any(not p.is_file() for p in paths):
        raise ValueError('existing immutable evidence files are required')
    with recovery.locked(directory()/'tick.lock'):
        with database() as con:
            found = con.execute('select body from actions where id=?', (identifier,)).fetchone()
        if not found:
            raise ValueError('unknown operational follow-up')
        action = json.loads(found[0])
        if action.get('kind') != 'operational' or action['state'] in TERMINAL:
            raise ValueError('operational follow-up is not pending')
        d, s, c, origin = interaction.binding(action['workflow'])
        hub = interaction.config()
        if not hub or hub['thread_id'] != action['hub'] or origin != action['origin']:
            raise ValueError('origin or hub binding changed')
        if recovery.generation(d, s) != action['baseline_generation'] or recovery.alive(s.get('owner_pid')):
            raise ValueError('workflow advanced or has an active owner')
        data = dict(action_id=identifier, category=category, blocker=blocker, tried=tried,
                    evidence={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
                    recommendation=recommendation, source=source)
        if action.get('escalation') and action['escalation'] != data:
            raise ValueError('this episode already has a different intervention question')
        context = json.dumps(dict(task=c['objective'], blocker=blocker, tried=tried,
                                  evidence=data['evidence'], source=source), ensure_ascii=False)
        question = interaction.ask(d, blocker, recommendation, context, _controller_escalation=data)
        if core.read(interaction.question_path(question['id'])).get('controller_escalation') != data:
            raise ValueError('existing intervention question has different immutable context')
        action.update(escalation=data, question_id=question['id'])
        with database() as con, con:
            put(con, action)
        return question


def reconcile_questions(cfg):
    """Rebuild from canonical question files; reading an inbox is never a presentation receipt."""
    for path in sorted((interaction.directory()/'questions').glob('question-*.json')):
        q = None
        try:
            q = core.read(path)
            if q['hub_thread'] != cfg['hub_thread']:
                continue
            identifier = q['id']
            with database() as con:
                old = con.execute('select body from actions where id=?', (identifier,)).fetchone()
            action = json.loads(old[0]) if old else dict(id=identifier, kind=q.get('kind') or 'question',
                workflow=q['workflow'], original_workflow=q['workflow'], origin=q['origin_thread'],
                hub=q['hub_thread'], created_at=q['created_at'], reminders=0,
                baseline_generation=q['generation'], state='unpresented')
            if action['state'] in TERMINAL:
                # A guard may have stopped this workflow just after the hub recorded
                # a valid answer. Only the explicit, checked budget resume reopens it.
                launch = Path(action['workflow'])/'launch.json'
                if (action['state']=='stopped' and q['state']=='accepted' and q.get('budget_stop_acceptance')
                        and not (Path(action['workflow'])/'STOP').exists() and launch.is_file()
                        and core.read(launch).get('approved_budget_answer') == identifier):
                    action.update(state='awaiting_execution', previous_stop_resolution=action.pop('resolution', None))
                else:
                    continue
            action.update(kind=q.get('kind') or 'question', origin=q['origin_thread'], hub=q['hub_thread'])
            action.setdefault('baseline_generation', q['generation'])
            action.setdefault('original_workflow', q['workflow'])
            d, s, c, origin = interaction.binding(action['workflow'], require_open=False)
            if origin != action['origin']:
                raise ValueError('origin binding changed')
            action.update(objective=c['objective'], question=q['question'],
                          question_state=q['state'], answer_delivery=q.get('answer_delivery'),
                          question_delivery=q.get('question_delivery'))
            generation = recovery.generation(d, s)
            import workflow_lineage
            accepted = workflow_lineage.delivery(d, s)
            if accepted['validation'] == 'user_accepted':
                action.update(state='completed', resolution='user accepted current delivery; question history retained',
                              delivery=accepted, resolved_at=core.now())
            elif s['status'] == 'completed':
                action.update(state='completed', resolution='overall workflow completed', resolved_at=core.now())
            elif (d/'STOP').exists() or s['status'] == 'stopped':
                action.update(state='stopped', resolution='workflow stopped; not completed')
            elif resumed(action, d, s, generation):
                action.update(state='resolved', resolution='live execution and subsequent progress verified; overall acceptance separate',
                              resolved_at=core.now(), resumed_generation=generation)
            elif q['state'] == 'accepted':
                action['state'] = 'awaiting_execution'
            elif q['state'] == 'answered':
                action['state'] = 'awaiting_acceptance'
            else:
                try:
                    interaction.validate_question(q)
                    action['state'] = 'needs_user' if action.get('presented_at') else 'unpresented'
                except (ValueError, OSError, KeyError) as exc:
                    action.update(state='stale', resolution=str(exc))
            with database() as con, con:
                put(con, action)
            register(d)
        except (ValueError, OSError, KeyError) as exc:
            # A damaged source stays visible; it is not silently declared healthy.
            if q and q.get('hub_thread') == cfg['hub_thread']:
                identifier = q['id']
                source_error(identifier, cfg['hub_thread'], q.get('workflow'), str(exc))


def observe_origin(action):
    """Correlate one answer-triggered turn, relay its final reply durably to the hub."""
    q = core.read(interaction.question_path(action['id']))
    if action['origin'] == action['hub']:
        return  # Already in the same conversation; do not relay to itself.
    delivery = q.get('answer_delivery') or {}
    if 'event_cursor' in delivery:
        initial = dict(cursor=delivery['event_cursor'])
    elif delivery.get('rollout_offset') is not None:
        initial = dict(offset=delivery['rollout_offset'])
    else:
        return  # No cursor: cannot safely attribute a reply.
    tracker = action.setdefault('reply_tracker', initial)
    reply = session_router.observe_reply(action['origin'], tracker, action['id'], core.now, delivery=delivery)
    if reply:
        action['origin_reply'] = reply


def live_execution(directory, state):
    """A queued answer/start event is not a running worker. Require live owner and child."""
    if state.get('status') not in recovery.ACTIVE or not recovery.alive(state.get('owner_pid')):
        return None
    if 'owner_identity' in state and (not state['owner_identity'] or
            core.process_identity(state['owner_pid']) != state['owner_identity']):
        return None
    child = state
    if state.get('active_run'):
        child = core.read(Path(state['active_run'])/'state.json')
    process = child.get('active_process')
    if not process or not recovery.alive(process.get('pid')):
        return None
    if 'identity' in process and (not process['identity'] or
            core.process_identity(process['pid']) != process['identity']):
        return None
    evidence = Path(process.get('evidence', ''))
    size = evidence.stat().st_size if evidence.is_file() else None
    # Changes in command/phase/check results/output prove progress, not just a new timestamp.
    token = core.digest([process.get('pid'), process.get('identity'), process.get('phase'), process.get('argv'), size,
                         state.get('criteria'), child.get('tasks'), child.get('status')])
    return dict(pid=process['pid'], owner_pid=state['owner_pid'], phase=process.get('phase'), token=token,
                observed_at=core.now())


def resumed(action, directory, state, generation):
    if generation == action['baseline_generation'] and action.get('kind') not in ('watchdog', 'budget_review', 'budget_watchdog'):
        return False
    probe = live_execution(directory, state)
    if not probe:
        action.pop('resume_probe', None)
        return False
    previous = action.get('resume_probe')
    action['resume_probe'] = probe
    if not previous or previous['token'] == probe['token']:
        return False
    action['resumption_evidence'] = dict(previous=previous, current=probe,
                                         generation=generation)
    return True


def observe_budget(row, directory, state, contract, generation):
    import budget
    info = budget.observation(directory, state, core.now(), recovery.alive(state.get('owner_pid')))
    row['budget'] = info
    level = info['level']
    if not level:
        return
    advisory = info['enforcement'] == 'advisory'
    identifier = 'budget-'+core.digest([str(directory), state['contract_hash'], info['hard_seconds'], level,
        info['policy_revision']] if advisory else [str(directory), state['contract_hash'], info['hard_seconds'], level])[:20]
    label = {'warning':'总执行额度已使用80%，原会话应提前复评剩余目标。',
             'critical':'总执行额度已使用90%，原会话应确认停止检查点和必要的估算。',
             'exhausted':'总执行额度已耗尽；保存停点并向用户报告，不擅自恢复或追加。',
             'overrun':'执行耗时已超过预估；继续既有目标并复评剩余工作，不因时长停工或申请追加。'}[level]
    if advisory and level != 'overrun':
        label = '执行耗时接近预估；原会话检查进展和剩余工作，继续执行，不申请时间额度。'
    with database() as con, con:
        prior = con.execute('select body from actions where id=?', (identifier,)).fetchone()
        action = json.loads(prior[0]) if prior else dict(id=identifier, kind='budget_watchdog',
            workflow=str(directory), origin=row['origin'], hub=row['hub'], state='unpresented',
            created_at=core.now(), reminders=0, baseline_generation=generation,
            objective=contract['objective'], budget_level=level,
            question=label+('' if advisory else ' 通知不代表已呈现，获批不代表真实恢复。'))
        action['budget'] = info
        if advisory:
            action.setdefault('notice', dict(state='origin_only'))
        put(con, action)
    if level != 'exhausted' and not action.get('origin_delivery'):
        instruction = ('\n本机已按用户指令启用advisory时长策略，旧soft/hard仅是估算参考，'
            '不是继续执行的许可门槛；超估继续原完整目标，无需向用户申请分钟数。'
            '保持原验收、权限、STOP与已有失败/用时/repair计数。无新证据的重复失败要换诊断或报告具体阻塞，'
            '不可无限重复。缺凭据/真实人工决定或新增业务权限仍通过ask。'
            if advisory else
            '\n既有hard以内的预留额度由原会话按证据budget-reforecast；超出hard才向hub申请。')
        message = (f'[ctask-budget-warning id={identifier}]\n{label}\nWorkflow: {directory}'
            f'\n预算快照: {json.dumps(info, ensure_ascii=False)}'
            '\n使用现有budget账本。核对真实进程和剩余验收，不并行修改、不打断健康worker。'
            + instruction +
            '缺失的宿主耗时为未知，用budget-run/有证据的budget-charge处理，不当成零。'
            '不增加接管额度/监督窗口，不重置预算或批准人工验收。')
        action['origin_delivery'] = (dict(state='same_session') if row['origin']==row['hub']
            else send('budget-origin:'+identifier, row['origin'], message, workflow=str(directory)))
        with database() as con, con:
            put(con, action)


def operational_followthrough(row, d, state, contract, generation):
    try:
        with recovery.locked(d/'recovery.lock', blocking=False):
            _operational_followthrough(row, d, state, contract, generation)
    except BlockingIOError:
        return  # A recovery handler is reconciling the same episode.


def _operational_followthrough(row, d, state, contract, generation):
    """Record a bounded diagnostic handoff; it grants no execution authority."""
    if notifications.status()['mode'] != 'user_input_only':
        return
    state = core.read(d/'state.json')
    if ((d/'STOP').exists() or recovery.generation(d, state) != generation
            or state.get('contract_hash') != row['contract_hash']
            or state['status'] not in recovery.REPAIR | recovery.ACTIVE
            or recovery.alive(state.get('owner_pid'))):
        return
    identifier = 'operational-'+core.digest([str(d), generation, state['status']])[:20]
    with database() as con, con:
        prior = con.execute('select body from actions where id=?', (identifier,)).fetchone()
        action = json.loads(prior[0]) if prior else dict(id=identifier, kind='operational',
            workflow=str(d), origin=row['origin'], hub=row['hub'], state='pending_operational',
            objective=contract['objective'], baseline_generation=generation,
            created_at=core.now(), reminders=0, provenance='controller_diagnostic')
        put(con, action)
    if action.get('origin_delivery'):
        # An observed reply is only diagnostic; it never closes the workflow.
        if action.get('origin_offset') is not None or 'origin_event_cursor' in action:
            initial = (dict(cursor=action['origin_event_cursor']) if 'origin_event_cursor' in action
                       else dict(offset=action['origin_offset']))
            tracker = action.setdefault('origin_tracker', initial)
            reply = session_router.observe_reply(action['origin'], tracker, identifier, core.now,
                                                 path=action.get('origin_rollout'), delivery=action['origin_delivery'])
            if reply:
                action['origin_reply'] = reply
        with database() as con, con:
            put(con, action)
        return
    import workflow_lineage
    delivery = workflow_lineage.delivery(d, state)
    pending_questions = []
    question_dir = interaction.directory()/'questions'
    for path in question_dir.glob('question-*.json'):
        try:
            question = core.read(path)
            if question.get('workflow') == str(d) and question.get('state') in ('pending', 'answered'):
                interaction.validate_question(question)
                pending_questions.append(question['id'])
        except (ValueError, OSError, KeyError):
            # Damaged or stale question records are ambiguous and require inspection.
            pending_questions.append(str(path))
    if delivery['validation'] == 'valid':
        action['reason'] = 'verified successor already completed'
    elif delivery['validation'] == 'invalid':
        action['reason'] = 'successor lineage invalid or ambiguous; inspect before delivery'
    elif pending_questions:
        action['reason'] = 'existing or ambiguous user question requires reconciliation'
    elif row['origin'] == row['hub']:
        action['reason'] = 'origin_is_hub; no routine self-queue'
    else:
        rpath = d/'recovery.json'
        ledger = core.read(rpath) if rpath.exists() else {}
        latest = (ledger.get('requests') or [None])[-1]
        supervisor_path = d/'supervisor.json'
        supervisor = core.read(supervisor_path) if supervisor_path.exists() else {}
        launch_path = d/'launch.json'
        launch = core.read(launch_path) if launch_path.exists() else {}
        if (recovery.alive(supervisor.get('pid')) or
                recovery.alive(launch.get('pid')) and core.now()-launch.get('at', 0) < 15):
            action['reason'] = 'supervisor or workflow launch remains active'
            with database() as con, con:
                put(con, action)
            return
        if latest and latest.get('state') in ('queued', 'working', 'stalled'):
            observed = action.get('observed_request')
            if not observed or observed.get('id') != latest.get('id'):
                observed = {k: latest[k] for k in ('id', 'generation', 'delivery', 'rollout_offset', 'scan_offset', 'event_cursor',
                            'seen_in_session', 'responded_at', 'turn_completed_at') if k in latest}
                # Pre-generation diagnostic records used an untyped completion
                # marker. Keep their public projection, never upgrade it to a
                # v2 turn receipt or use it to accept workflow criteria.
                if 'generation' not in latest:
                    observed['legacy_diagnostic_completion'] = True
            try:
                recovery.observe_response(ledger, observed)
                action['observed_request'] = observed
                latest = dict(latest, **{k: observed[k] for k in
                    ('seen_in_session', 'responded_at', 'turn_completed_at') if k in observed})
            except (ValueError, OSError, KeyError) as exc:
                action['reason'] = 'origin recovery response observation unavailable: '+str(exc)
                with database() as con, con:
                    put(con, action)
                return
        if (latest and (latest.get('state') in ('dispatching', 'delivery_unknown', 'repaired', 'resuming', 'resume_launching')
                        or (latest.get('state') in ('queued', 'working') and
                            (not latest.get('turn_completed_at') or core.now() < latest.get('expires_at', 0)))
                        or (latest.get('message_id') and not latest.get('turn_completed_at')))):
            action['reason'] = 'existing recovery delivery or handler may still be active'
        elif (ledger.get('status') == 'binding_changed' or
              ledger and (ledger.get('thread_id') != row['origin'] or ledger.get('contract_hash') != state['contract_hash'])):
            action['reason'] = 'recovery binding changed; diagnostic delivery withheld'
        else:
            if session_router.activity(row['origin'], generation=state.get('execution_revision', 0)) != 'idle':
                action['reason'] = 'origin turn active or activity unknown'
            else:
                try:
                    with recovery.locked(Path(state['repo'])/'.codex/tasks/.worker.lock', blocking=False):
                        fresh = core.read(d/'state.json')
                        if ((d/'STOP').exists() or recovery.alive(fresh.get('owner_pid'))
                                or recovery.generation(d, fresh) != generation):
                            action['reason'] = 'workflow changed while reconciling'
                        else:
                            checkpoint = session_router.delivery_checkpoint(row['origin'])
                            if checkpoint.get('rollout_offset') is not None:
                                action['origin_offset'] = checkpoint['rollout_offset']
                            elif 'event_cursor' in checkpoint:
                                action['origin_event_cursor'] = checkpoint['event_cursor']
                            with database() as con, con:
                                put(con, action)
                            action['origin_delivery'] = send('operational:'+identifier, row['origin'],
                                f'[ctask-operational id={identifier}]\nWorkflow: {d}\n'
                                '仅诊断既有阻塞与处理人状态；核对 STOP、原合约、现有请求及真实执行。'
                                '不得恢复、启动执行器、增加配额或产生业务操作；回复本诊断不代表已恢复。', workflow=str(d))
                except BlockingIOError:
                    action['reason'] = 'repository busy'
    with database() as con, con:
        put(con, action)


def workflow_observation(row):
    """Observe known, authorized workflows only. Never create or restart a business executor."""
    if row.get('superseded_by'):
        row['active_key'] = None
        return
    d, s, c, origin = interaction.binding(row['workflow'], require_open=False)
    if origin != row['origin'] or s['contract_hash'] != row['contract_hash']:
        raise ValueError('watched workflow binding changed')
    generation = recovery.generation(d, s)
    import workflow_lineage
    if workflow_lineage.delivery(d, s)['validation'] in ('valid', 'user_accepted'):
        row['active_key'] = None
        return
    if (d/'STOP').exists() or s['status'] == 'stopped':
        row['active_key'] = None
        return
    row['active_key'] = None
    if s['status'] == 'completed':
        identifier = 'result-'+core.digest([str(d), s['contract_hash']])[:20]
        with database() as con, con:
            if not con.execute('select 1 from actions where id=?', (identifier,)).fetchone():
                put(con, dict(id=identifier, kind='workflow_result', workflow=str(d), origin=origin,
                    hub=row['hub'], state='unpresented', created_at=core.now(), reminders=0,
                    objective=c['objective'], question='总任务状态已完成；核对全部最终验收证据后向用户报告。',
                    baseline_generation=generation))
        return
    observe_budget(row, d, s, c, generation)
    import budget
    forecast = budget.review(d)
    if forecast:
        identifier = 'budget-review-'+core.digest([str(d), forecast['key']])[:20]
        with database() as con, con:
            prior = con.execute('select body from actions where id=?', (identifier,)).fetchone()
            action = json.loads(prior[0]) if prior else dict(id=identifier, kind='budget_review',
                workflow=str(d), origin=origin, hub=row['hub'], state='awaiting_origin', created_at=core.now(),
                reminders=0, objective=c['objective'], baseline_generation=generation,
                question='接近任务执行预估，由原会话在既有总额度内复评，不需要用户重复批准。',
                notice={'state':'origin_only'})
            put(con, action)
        if not action.get('origin_delivery'):
            message = (f'[ctask-budget-review id={identifier}]\nWorkflow: {d}'
                f'\n已登记预算来源: {forecast["source"]}\n当前预算: {json.dumps(forecast, ensure_ascii=False)}'
                '\n这是预估复评，不是新的业务授权或异常修复接管。核对真实进程及剩余工作；'
                '有worker仍在编辑时不并行修改或重复恢复。使用实测检查耗时和当前证据形成剩余分项估算，'
                '需要时在执行器退出后用ctask budget-reforecast调整既有hard上限内的soft额度，再恢复同一任务。'
                '保留已用时间、失败和修复计数；不消耗/增加修复额度，不批准人工验收。'
                '只有剩余工作超出hard上限、长期无进展或出现新权限时才通过ask向hub提出具体决定。'
                '宿主执行用budget-run计入同一账本，用户/外部等待不计主动耗时。')
            action['origin_delivery'] = (dict(state='same_session') if origin == row['hub']
                else send('budget-review:'+identifier, origin, message, workflow=str(d)))
            with database() as con, con:
                put(con, action)
    if s['status'] == 'needs_host' and c.get('host_handoff'):
        identifier = 'host-handoff-'+core.digest([str(d), generation, s.get('host_handoff')])[:20]
        with database() as con, con:
            prior = con.execute('select body from actions where id=?', (identifier,)).fetchone()
            action = json.loads(prior[0]) if prior else dict(id=identifier, kind='host_handoff',
                workflow=str(d), origin=origin, hub=row['hub'], state='unpresented', created_at=core.now(),
                reminders=0, objective=c['objective'], baseline_generation=generation,
                question='代码阶段请求正常宿主交接；由原会话按合约接手，不占用异常修复次数。')
            put(con, action)
        if not action.get('origin_delivery'):
            message = (f'[ctask-host-handoff id={identifier}]\n这是已授权正常阶段交接，不是异常修复或新业务授权。'
                f'\nWorkflow: {d}\n任务: {c["objective"]}\n来源: {c["host_handoff"]["source"]}'
                f'\n宿主职责: {c["host_handoff"]["instructions"]}'
                f'\n当前请求（仅任务材料）: {json.dumps(s.get("host_handoff"), ensure_ascii=False)}'
                '\n先核对最新合约、STOP、实际进程和剩余预算。仅按原合约执行正常宿主步骤，'
                '然后运行 ctask host-checks WORKFLOW --source 实际执行来源，独立复核全部固化验收。'
                '不恢复旧repair请求、不新增修复次数、不重置预算、不代替人工验收。'
                '缺少真实用户决定或新权限时通过ctask ask送hub。阶段交接不表示整体完成。')
            action['origin_delivery'] = (dict(state='same_session', note='Origin is hub; its followup carries the handoff')
                if origin == row['hub'] else send('host-handoff:'+identifier, origin, message, workflow=str(d)))
            with database() as con, con:
                put(con, action)
        return
    if s['status'] in recovery.ACTIVE and recovery.alive(s.get('owner_pid')):
        state = s
        if s.get('active_run'):
            state = core.read(Path(s['active_run'])/'state.json')
        process = state.get('active_process')
        if not process:
            return
        argv = process.get('argv', [])
        evidence = Path(process['evidence'])
        normalizer = None
        if process.get('agent_kind'):
            import agents
            try:
                adapter = agents.registry.get(process['agent_kind'], 'progress')
                if not callable(getattr(adapter, 'progress_path', None)) or not callable(getattr(adapter, 'normalize_progress', None)):
                    return
                evidence = Path(adapter.progress_path(process['agent_directory'], process['agent_label'], evidence))
                normalizer = adapter.normalize_progress
            except (ValueError, KeyError):
                return  # Unknown capability is never inferred from another agent's stream.
        elif '_invoke' in argv and '--label' in argv:
            run = Path(s['active_run']) if s.get('active_run') else d
            evidence = run/'checkpoints'/(argv[argv.index('--label')+1]+'.agent-runtime')/'stdout.jsonl'
        elif 'exec' not in argv or '--json' not in argv:
            return  # Quiet host builds/checks are not model stalls.
        key = core.digest([str(d), generation, process['pid'], str(evidence)])
        row['active_key'] = key
        watch = row.setdefault('progress', {})
        if watch.get('key') != key:
            import progress
            row['progress'] = watch = dict(key=key, evidence=str(evidence), state=progress.initial())
        import progress
        for alert in progress.observe(evidence, watch['state'], core.now(), core.digest, normalizer=normalizer):
            identifier = 'watchdog-'+core.digest([key, alert['kind'], alert['key']])[:20]
            with database() as con, con:
                put(con, dict(id=identifier, kind='watchdog', workflow=str(d), origin=origin,
                    hub=row['hub'], state='unpresented', created_at=core.now(), reminders=0,
                    objective=c['objective'], alert=alert, command_key=key,
                    baseline_generation=generation, evidence=str(evidence),
                    question='执行器仍活跃，但观察到无输出、工具停滞或重复调用。先核对，勿并行修改或重启。'))
        return
    if s['status'] in recovery.REPAIR | recovery.REPORT | {'permission_blocked'} or (s['status'] in recovery.ACTIVE and not recovery.alive(s.get('owner_pid'))):
        # Works even after the bounded repair supervisor exits. Observation grants no repairs.
        identifier = 'workflow-'+core.digest([str(d), generation, s['status']])[:20]
        with database() as con, con:
            if not con.execute('select 1 from actions where id=?', (identifier,)).fetchone():
                put(con, dict(id=identifier, kind='workflow', workflow=str(d), origin=origin,
                    hub=row['hub'], state='unpresented', created_at=core.now(), reminders=0,
                    objective=c['objective'], baseline_generation=generation,
                    question='总任务未完成且无活动执行器。核对已有问题和恢复证据，在原授权内处理。'))
        operational_followthrough(row, d, s, c, generation)
        import failure_attention
        failure_attention.observe(row, d, s, c)


def tick():
    cfg = settings()
    if not cfg or not cfg['enabled']:
        return False
    hub = interaction.config()
    if not hub or hub['thread_id'] != cfg['hub_thread']:
        raise ValueError('controller hub binding changed; explicit reconfiguration required')
    with recovery.locked(directory()/'tick.lock'):
        reconcile_questions(cfg)
        with database() as con:
            sources = [json.loads(r[0]) for r in con.execute('select body from workflows')]
            sources = [r for r in sources if r['hub'] == cfg['hub_thread']]
        for row in sources:
            try:
                workflow_observation(row)
                with database() as con, con:
                    con.execute('update workflows set body=? where path=?', (json.dumps(row), row['workflow']))
                    identifier = 'source-'+core.digest(row['workflow'])[:20]
                    old = con.execute('select body from actions where id=?', (identifier,)).fetchone()
                    if old:
                        restored = json.loads(old[0])
                        if restored['state'] == 'source_error':
                            restored.update(state='resolved', resolution='source is readable and binding valid again')
                            put(con, restored)
            except (ValueError, OSError, KeyError) as exc:
                identifier = 'source-'+core.digest(row['workflow'])[:20]
                source_error(identifier, cfg['hub_thread'], row['workflow'], str(exc))
        actions = rows(include_terminal=True)
        # Existing questions already describe this blocker: avoid a second generic alert.
        questions = {a['workflow'] for a in actions if (a['kind'] in ('question', 'supervision_alert') or
            a['kind']=='budget_watchdog' and a.get('budget_level')=='exhausted') and a['state'] not in TERMINAL}
        for action in actions:
            if action['hub'] != cfg['hub_thread']:
                continue  # A new default hub never steals old questions or delivery bindings.
            if action.get('kind') in ('workflow', 'operational') and action.get('workflow'):
                try:
                    path = Path(action['workflow'])
                    if (path/'workflow-lineage.json').exists():
                        import workflow_lineage
                        action['delivery'] = workflow_lineage.delivery(path, core.read(path/'state.json'))
                except (ValueError, OSError, KeyError) as exc:
                    action['delivery'] = dict(validation='invalid', error=str(exc))
            if action['kind'] in ('question', 'supervision_alert'):
                try:
                    observe_origin(action)
                except (ValueError, OSError, KeyError) as exc:
                    action['reply_error'] = str(exc)
            if action['kind'] in ('watchdog', 'workflow', 'operational', 'host_handoff', 'budget_review', 'budget_watchdog', 'failure_attention') and action['state'] not in TERMINAL:
                try:
                    d, s, c, origin = interaction.binding(action['workflow'], require_open=False)
                    source = next((r for r in sources if r['workflow'] == str(d)), {})
                    import workflow_lineage
                    delivery = workflow_lineage.delivery(d, s)
                    if delivery['validation'] == 'user_accepted':
                        action.update(state='completed', resolution='user accepted current delivery; automated residuals retained', delivery=delivery)
                    elif delivery['validation'] == 'valid' and action['kind'] in ('workflow', 'operational'):
                        action.update(state='superseded', resolution='verified independent successor completed; predecessor history retained',
                                      delivery=delivery)
                    elif source.get('superseded_by'):
                        action.update(state='superseded', resolution='explicit continuation tracks the remaining task')
                    elif s['status'] == 'completed':
                        action.update(state='completed', resolution='overall acceptance passed')
                    elif (d/'STOP').exists() or s['status'] == 'stopped':
                        action.update(state='stopped', resolution='workflow stopped')
                    elif action['kind'] == 'budget_review' and not __import__('budget').time_enforced():
                        action.update(state='superseded', resolution='Elapsed-time policy is advisory; no budget renewal gate')
                    elif action['kind'] == 'budget_watchdog':
                        import budget
                        info=budget.observation(d, s, core.now(), recovery.alive(s.get('owner_pid')))
                        previous_enforcement = action.get('budget', {}).get('enforcement', 'enforced')
                        action['budget']=info
                        levels={'warning':1, 'critical':2, 'exhausted':3, 'overrun':3}
                        if previous_enforcement != info['enforcement']:
                            action.update(state='superseded', resolution='Elapsed-time enforcement policy changed; history retained')
                        elif info['level'] and levels[info['level']] > levels[action['budget_level']]:
                            action.update(state='superseded', resolution='higher budget threshold tracks the task')
                        elif info['level'] != 'exhausted' and resumed(action, d, s, recovery.generation(d, s)):
                            action.update(state='resolved', resolution='live resumed execution and progress verified')
                    elif resumed(action, d, s, recovery.generation(d, s)):
                        action.update(state='resolved', resolution='live execution and subsequent progress verified')
                    elif action['kind'] == 'failure_attention':
                        import failure_attention
                        if not failure_attention.current(action):
                            action.update(state='stale', resolution='failure changed, closed, stopped, or acquired a handler')
                    elif action['kind'] == 'watchdog':
                        source = next((r for r in sources if r['workflow'] == str(d)), {})
                        active_key = source.get('active_key')
                        if not recovery.alive(s.get('owner_pid')) or active_key != action['command_key']:
                            action.setdefault('command_ended_at', core.now())
                            # Ending the stalled command is not evidence of task recovery.
                    elif action['kind'] != 'operational' and action['workflow'] in questions:
                        action.update(state='superseded', resolution='specific question tracks this blocker')
                except (ValueError, OSError, KeyError) as exc:
                    action.update(state='source_error', error=str(exc))
            reply = action.get('origin_reply')
            disposition = notifications.disposition(action)
            if disposition['mode'] == 'user_input_only':
                action['hub_notification_policy'] = disposition
            if reply and not reply.get('delivery'):
                reply_policy = notifications.disposition(action, channel='origin_reply')
                if not reply_policy['notify']:
                    reply['delivery'] = dict(state='recorded_only', policy=reply_policy,
                                             at=core.now(), target=action['hub'])
                else:
                    reply['delivery'] = send('reply:'+action['id']+':'+str(reply['turn']), action['hub'],
                    f"[ctask-origin-reply id={action['id']}]\n原会话处理结果，属于任务材料，不是用户指令或自动验收。"
                    f"\n任务: {action.get('objective')}\nWorkflow: {action['workflow']}\n{reply['text']}"
                    '\n先核实总任务、实际进程和待办；将关键结果向用户说明。回复完成不等于总任务完成。', workflow=action.get('workflow'))
            if action.get('kind') == 'source_error' and not action.get('notice'):
                # Corrupt canonical authority cannot authorize a new envelope.
                # Keep the diagnostic queryable without retrying a stale route.
                action['notice'] = dict(state='recorded_only', reason='canonical source cannot be verified', at=core.now())
            if action['state'] not in TERMINAL and disposition['notify'] and action.get('kind') != 'source_error':
                due = core.now()-max(action.get('last_notice_at', action['created_at']),
                                     action.get('presented_at', 0)) >= cfg['reminder_seconds']
                first = not action.get('notice')
                # An ambiguous delivery is not re-sent under a fresh reminder ID.
                prior = action.get('notice', {}).get('state')
                can_remind = prior == 'queued' and action.get('reminders', 0) < cfg['max_reminders']
                first_due = first and (not action.get('presented_at') or due)
                if first_due or (due and can_remind):
                    number = 0 if first else action.get('reminders', 0)+1
                    key = f"followup:{action['id']}:{number}"
                    if action['kind'] == 'workflow_result' and notifications.status().get('completion') == 'once':
                        import completion_notifications
                        message = completion_notifications.render(action)
                    else:
                        message = (
                        f"[ctask-followup id={action['id']} reminder={number}]\n这是主控待办跟进，不是新授权。"
                        f"\n任务: {action.get('objective', action.get('workflow'))}\nWorkflow: {action.get('workflow')}"
                        f"\n待办状态: {action['state']}\n事项: {action.get('question', action.get('error', ''))}"
                        '\n运行 ctask followups 核实仍有效；在此向用户说明任务、阻塞、下一步。'
                        f"实际说明后运行 ctask presented {action['id']} --source '真实说明来源'。"
                        '\n用户已答复就核对原会话接收与真实恢复；不要重复问已决定的问题。'
                        '\n不得代用户批准，不扩大预算/权限、不重发未知消息，不并行改原工程；需要人工决定才提问。'
                        )
                    action['notice'] = send(key, action['hub'], message, workflow=action.get('workflow'))
                    action['last_notice_at'] = core.now()
                    action['reminders'] = number
            with database() as con, con:
                put(con, action)
    return True


def status():
    cfg = settings()
    p = directory()/'process.json'
    process = core.read(p) if p.exists() else {}
    return dict(config=cfg, process=process, process_alive=recovery.alive(process.get('pid')),
                pending=rows(), database=str(directory()/'state.sqlite'))


def start():
    with recovery.locked(directory()/'start.lock'):
        p = directory()/'process.json'
        if p.exists() and recovery.alive(core.read(p).get('pid')):
            return status()
        with (directory()/'worker.log').open('ab') as log:
            proc = subprocess.Popen(core.runtime_command('_controller'),
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        core.save(p, dict(pid=proc.pid, heartbeat=core.now(), state='launching'))
    return status()


def watch(interval=5):
    with recovery.locked(directory()/'start.lock'):
        pass
    with recovery.locked(directory()/'worker.lock', blocking=False):
        stopped = False
        def stop(signum, frame):
            nonlocal stopped
            stopped = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        error = None
        try:
            while not stopped and tick():
                core.save(directory()/'process.json', dict(pid=os.getpid(), heartbeat=core.now(), state='watching'))
                time.sleep(interval)
        except Exception as exc:
            error = str(exc)
        finally:
            core.save(directory()/'process.json', dict(pid=None, heartbeat=core.now(), state='exited', error=error))
