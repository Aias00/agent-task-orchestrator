"""Once-only, verified total-workflow completion notices. No business execution."""
import hashlib
import json
import re
from pathlib import Path


def _finished(directory):
    path = Path(directory)/'events.jsonl'
    with path.open('rb') as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size-262144))
        lines = stream.read().splitlines()
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if event.get('event') == 'workflow_finished':
            return event.get('time') if event.get('status') == 'completed' else None
    return None


def prior_claim(con, action, identity):
    """Find original claims, including claims predating this implementation.

    Called inside the send transaction too, so two different result IDs cannot
    race to claim one physical delivery. Duplicate bookkeeping is not a send.
    """
    import runner as core
    for member in identity['members']:
        identifier = 'result-'+core.digest([member['workflow'], member['contract_hash']])[:20]
        if identifier == action['id']:
            continue
        row = con.execute('select body from actions where id=?', (identifier,)).fetchone()
        if row:
            old = json.loads(row[0])
            if (old.get('hub') != action['hub'] or old.get('origin') != action['origin']
                    or old.get('workflow') != member['workflow'] or old.get('kind') != 'workflow_result'):
                raise ValueError('conflicting canonical completion identity')
            if old.get('notice') and not isinstance(old['notice'], dict):
                raise ValueError('invalid existing completion notice')
            if old.get('presented_at') or (old.get('notice') and
                    old['notice'].get('reason') != 'same_delivery_already_claimed'):
                return dict(action_id=identifier, basis='existing_presentation_or_notice',
                            delivery_state=old.get('notice', {}).get('state', 'presented'))
        for receipt in con.execute('select key,body from outbox where key like ?', ('followup:'+identifier+':%',)):
            claim = json.loads(receipt['body'])
            if not isinstance(claim, dict):
                raise ValueError('invalid existing completion outbox claim')
            if claim.get('reason') == 'same_delivery_already_claimed':
                continue
            if claim.get('target') not in (None, action['hub']):
                raise ValueError('completion outbox target changed')
            return dict(action_id=identifier, key=receipt['key'], basis='durable_outbox_claim',
                        delivery_state=claim.get('state', 'unknown'))
    return None


def observation(action, con=None):
    import completion_delivery
    import control
    try:
        identity = completion_delivery.identity(action['workflow'])
        if identity['validation'] == 'valid':
            if con is None:
                with control.database() as database:
                    prior = prior_claim(database, action, identity)
            else:
                prior = prior_claim(con, action, identity)
            if prior:
                identity['duplicate_of'] = prior
        return identity
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return dict(validation='invalid', error=str(exc), observation_only=True, execution_authority=False)


def eligible(action):
    import interaction, notifications, recovery
    import runner as core
    if (action.get('kind') != 'workflow_result' or action.get('state') not in ('unpresented','needs_user')
            or action.get('notice') or action.get('presented_at') or notifications.project_suppression(action)):
        return False
    policy = notifications.status()
    if policy.get('completion') != 'once' or policy.get('hub_thread') != action.get('hub'):
        return False
    since = policy.get('completion_since')
    if type(since) not in (int,float) or action.get('created_at',0) < since:
        return False
    try:
        d,state,contract,origin = interaction.binding(action['workflow'],require_open=False)
        if (action.get('id') != 'result-'+core.digest([str(d), state['contract_hash']])[:20]
                or state['status'] != 'completed' or state.get('owner_pid') or state.get('active_process')
                or (d/'STOP').exists() or action['origin'] != origin
                or not state.get('criteria') or any(r.get('status')!='passed' for r in state['criteria'].values())
                or action.get('baseline_generation') != recovery.generation(d,state)):
            return False
        # Prevent a newly watched historical workflow from becoming a new notice.
        finished = _finished(d)
        if type(finished) not in (int,float) or not since <= finished <= core.now()+5:
            return False
        # All original command/manual evidence must still be present and bound.
        import workflow_lineage
        workflow_lineage._evidence(d,state,contract)
        identity = observation(action)
        if identity['validation'] != 'valid' or identity.get('duplicate_of'):
            return False
        # A fresh successor ID cannot backfill an old physical completion.
        for member in identity['members']:
            previous = _finished(member['workflow'])
            if type(previous) in (int, float) and not since <= previous <= core.now()+5:
                return False
            if (type(previous) not in (int, float)
                    and core.read(Path(member['workflow'])/'state.json')['status'] == 'completed'):
                return False
        return True
    except (OSError,ValueError,KeyError,TypeError):
        return False


def reserve(con, key, target, text):
    """Recheck equivalence under the existing durable outbox transaction."""
    match = re.fullmatch(r'followup:(result-[0-9a-f]{20}):0', key)
    if not match:
        raise ValueError('invalid once-only completion key')
    row = con.execute('select body from actions where id=?', (match[1],)).fetchone()
    if not row:
        raise ValueError('canonical completion action missing')
    action = json.loads(row[0])
    if target != action['hub'] or text != render(action):
        raise ValueError('canonical completion recipient or body changed')
    identity = observation(action, con)
    if identity['validation'] != 'valid':
        raise ValueError('completion identity cannot be verified')
    if identity.get('duplicate_of'):
        return dict(key=key, target=target, state='recorded_only', authority='none',
                    reason='same_delivery_already_claimed', duplicate_of=identity['duplicate_of'])
    return None


def render(action):
    import interaction
    d,state,contract,_=interaction.binding(action['workflow'],require_open=False)
    title=' '.join(contract['objective'].split())[:240]
    count=len(state['criteria'])
    return (f"[ctask-workflow-completed id={action['id']}]\n"
            f"任务完成：{title}\n总体验收 {count}/{count} 通过。交付位置：{state['repo']}\n"
            f"Workflow: {d}\n"
            "按用户明确设置的完成一次通知：核实后仅简短告知结果和交付位置；"
            "阶段完成不算总任务完成，不重复通知、不解释为已读或新增授权。")


def validate_claim(key,target,text, *, envelope=None, endpoint=None):
    """Validate canonical action and exact durable claim before native delivery.

    The legacy user-input queue gate stays installed and unchanged. This is a
    separate code-owned route only for the user's newly allowed completion kind;
    arbitrary text/prefixes/environment variables cannot grant this route.
    """
    import control
    match=re.fullmatch(r'followup:(result-[0-9a-f]{20}):0',key)
    if not match:raise ValueError('not a once-only completion claim')
    with control.database() as con:
        row=con.execute('select body from actions where id=?',(match[1],)).fetchone()
        claim=con.execute('select body from outbox where key=?',(key,)).fetchone()
    if not row or not claim:raise ValueError('missing canonical completion or durable send claim')
    action,receipt=json.loads(row[0]),json.loads(claim[0])
    if (not eligible(action) or action['hub']!=target or receipt.get('target')!=target
            or receipt.get('state')!='dispatching' or receipt.get('authority') not in ('legacy_submission','managed_mailbox')
            or receipt.get('message_sha256')!=hashlib.sha256(text.encode()).hexdigest()
            or text!=render(action)):
        raise ValueError('completion claim, authority, recipient or body does not match')
    if receipt['authority']=='managed_mailbox':
        if (not isinstance(envelope,dict) or not isinstance(endpoint,dict)
                or receipt.get('mailbox_id')!=envelope.get('id')
                or envelope.get('canonical_id')!=key or envelope.get('correlation_id')!=key
                or envelope.get('state')!='dispatching' or envelope.get('protocol')!=2
                or envelope.get('task_id')!=action['workflow']
                or endpoint.get('session_id')!=target or endpoint.get('kind')!='codex'):
            raise ValueError('completion mailbox envelope or destination does not match claim')
    return action
