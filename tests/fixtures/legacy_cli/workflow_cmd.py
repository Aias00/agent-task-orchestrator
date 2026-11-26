"""Workflow command handling; runtime context is supplied by the public facade."""
import json

def handle(args, core):
    if args.command == 'workflow-link':
        from ato_workflow import workflow_lineage
        print(json.dumps(workflow_lineage.link(args.predecessor, args.successor, args.evidence, args.source), ensure_ascii=False, indent=2))
        return
    if args.command == 'host-recheck':
        from ato_workflow.host_recheck import recheck
        result = recheck(args.directory, args.task, args.source)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['passed'] else 1
    if args.command == 'host-checks':
        from ato_workflow.workflow import Workflow
        Workflow(args.directory).execute(host_checks=True, source=args.source)
        print(json.dumps(core.status(args.directory), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'workflow-close':
        from ato_workflow import workflow_closure
        print(json.dumps(workflow_closure.close(args.directory, args.evidence, args.source), ensure_ascii=False, indent=2))
        return 0
    if args.command in ('workflow-create', 'workflow-validate', 'acknowledge'):
        from ato_workflow import workflow
        if args.command == 'workflow-create':
            if args.auto_recover and (not (args.origin_thread or args.origin_ref) or not args.recovery_source):
                raise ValueError('auto-recover requires verified --origin-thread or --origin-ref and --recovery-source')
            directory = workflow.create(args.repo, args.plan, args.origin_thread, args.origin_ref)
            if args.auto_recover:
                import recovery
                recovery.enable(directory, args.recovery_source)
            print(directory)
        elif args.command == 'workflow-validate':
            plan = core.read(args.plan)
            workflow.validate(plan)
            print('Workflow contract valid')
        else:
            proof = None
            if args.actor_file:
                from ato_adapters.local_session_adapter import load_actor_file
                proof, _, _ = load_actor_file(args.actor_file)
            workflow.acknowledge(args.directory, args.criterion, args.evidence, args.note, actor_proof=proof)
            print('Acceptance recorded; resume to revalidate the complete workflow')
        return 0
    raise ValueError('command outside workflow_cmd domain')
