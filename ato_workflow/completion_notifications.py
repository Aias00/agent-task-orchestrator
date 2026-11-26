"""Once-only, verified total-workflow completion notices. No business execution."""
from ato_common import ato_core as core
import hashlib
import json
import re
from pathlib import Path


def eligible(action):
    from ato_workflow import interaction
    import recovery
    from ato_workflow import notification_state as notifications
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
        if (state['status'] != 'completed' or state.get('owner_pid') or state.get('active_process')
                or (d/'STOP').exists() or action['origin'] != origin
                or not state.get('criteria') or any(r.get('status')!='passed' for r in state['criteria'].values())
                or action.get('baseline_generation') != recovery.generation(d,state)):
            return False
        # Prevent a newly watched historical workflow from becoming a new notice.
        path=d/'events.jsonl'
        with path.open('rb') as stream:
            size=stream.seek(0,2);stream.seek(max(0,size-262144));lines=stream.read().splitlines()
        finished=None
        for line in reversed(lines):
            try: event=json.loads(line)
            except (ValueError,UnicodeDecodeError): continue
            if event.get('event')=='workflow_finished':
                if event.get('status')=='completed':finished=event.get('time')
                break
        if type(finished) not in (int,float) or not since <= finished <= core.now()+5:
            return False
        # All original command/manual evidence must still be present and bound.
        from ato_workflow import workflow_lineage
        workflow_lineage._evidence(d,state,contract)
        return True
    except (OSError,ValueError,KeyError,TypeError):
        return False


def render(action):
    from ato_workflow import interaction
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
    from ato_workflow import control_store as control
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
