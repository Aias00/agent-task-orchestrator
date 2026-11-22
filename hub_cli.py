"""Independent source-only H1 control plane; does not extend the ctask parser."""
import argparse
import json
import os
from pathlib import Path
import sys

from hub_contract import HubAgentSpec, KINDS, require, strict_json
from hub_ledger import HubLedger, canonical
from hub_loop import HubLoop
from hub_registry import HubRegistry
import hub_worktree


def parser():
    cli=argparse.ArgumentParser(description=__doc__)
    sub=cli.add_subparsers(dest='command',required=True)
    sub.add_parser('doctor')
    create=sub.add_parser('hub-create');create.add_argument('--repo',required=True)
    create.add_argument('--kind',choices=KINDS,required=True);create.add_argument('--model');create.add_argument('--effort')
    create.add_argument('--max-turns',type=int,default=20)
    for name in ('hub-status','hub-attach-cmd','hub-switch-agent','hub-rollover','task-create',
                 'task-status','task-run','task-review-only','task-stop','worktree-cleanup'):
        item=sub.add_parser(name);item.add_argument('hub_id')
        if name in ('hub-switch-agent','hub-rollover'):item.add_argument('--source',required=True)
        if name=='hub-switch-agent':
            item.add_argument('--kind',choices=KINDS,required=True);item.add_argument('--model');item.add_argument('--effort')
        if name=='task-create':item.add_argument('--spec',required=True)
        if name.startswith('task-') and name!='task-create' or name=='worktree-cleanup':item.add_argument('task_id')
        if name=='worktree-cleanup':item.add_argument('--confirm',action='store_true',required=True)
    return cli


def dispatch(args,*,registry=None,worker_tools=None):
    registry=registry if registry is not None else HubRegistry()
    if args.command=='doctor':return registry.doctor_all()
    require(os.environ.get('ATO_HOME'),'EXPLICIT_ATO_HOME_REQUIRED')
    if args.command=='hub-create':
        repo=hub_worktree.repository(args.repo)
        ledger=HubLedger.create(repo,HubAgentSpec(args.kind,args.model,args.effort),max_turns=args.max_turns)
        return dict(hub_id=ledger.identifier)
    ledger=HubLedger(args.hub_id);loop=HubLoop(ledger,registry,worker_tools=worker_tools)
    if args.command=='hub-status':return dict(hub=ledger.hub(),hub_busy=ledger.busy())
    if args.command=='task-status':
        _,state=ledger.read_task(args.task_id);return dict(state=state,hub_busy=ledger.busy())
    if args.command=='task-stop':return loop.stop(args.task_id)
    if args.command=='hub-switch-agent':
        ledger.switch_agent(HubAgentSpec(args.kind,args.model,args.effort),args.source);return ledger.hub()
    if args.command=='hub-rollover':ledger.rollover(args.source);return ledger.hub()
    if args.command=='task-create':
        path=canonical(args.spec);require(path.stat().st_size<=1024*1024,'SPEC_BOUND_EXCEEDED')
        task=loop.create_task(strict_json(path.read_bytes()));return dict(task_id=task.name)
    if args.command in ('task-run','task-review-only'):
        return loop.run(args.task_id,review_only=args.command=='task-review-only')
    with ledger.locked():
        if args.command=='hub-attach-cmd':
            hub=ledger.hub();kind=hub['active_agent']['kind'];adapter=registry.get(kind)
            require(adapter.capabilities().supported('interactive_attach'),'ATTACH_NOT_QUALIFIED')
            session=hub['native_sessions'].get(kind,{})
            require(session.get('state')=='active' and session.get('session_id'),'NO_ACTIVE_SESSION')
            workspace=hub_worktree.repository(session.get('workspace') or hub['repo'])
            return dict(argv=adapter.attach_argv(session['session_id'],workspace),cwd=str(workspace),hub_busy=False)
        if args.command=='worktree-cleanup':
            spec,state=ledger.read_task(args.task_id);require(state['status'] not in ('running_worker','fixing','reviewing'),'TASK_BUSY')
            loop.barriers(spec,state);hub_worktree.cleanup(state['worktree'])
            loop.transition(args.task_id,state,'cleaned','EXPLICIT_OPERATOR_CLEANUP');return state
    raise ValueError('UNKNOWN_HUB_COMMAND')


def main(argv=None):
    args=parser().parse_args(argv)
    try:result=dispatch(args)
    except (ValueError,OSError,KeyError,TypeError):
        print(json.dumps(dict(status='refused',code='HUB_COMMAND_REFUSED')));return 2
    print(json.dumps(result,ensure_ascii=False,allow_nan=False));return 0


if __name__=='__main__':sys.exit(main())
