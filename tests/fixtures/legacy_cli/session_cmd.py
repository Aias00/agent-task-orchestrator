"""Session command handling; runtime context is supplied by the public facade."""
import json

def handle(args, core):
    if args.command == 'agent-doctor':
        from ato_adapters import agents
        print(json.dumps(agents.doctor(), ensure_ascii=False, indent=2))
        return 0
    if args.command in ('delegate', 'delegation-status', 'delegation-accept', '_dispatcher'):
        from ato_workflow import delegation
        if args.command == 'delegate':
            result = delegation.delegate(args.repo, args.plan, args.name, args.source, args.request_id, args.timeout, args.session_kind)
        elif args.command == 'delegation-status':
            result = delegation.public(args.directory)
        elif args.command == 'delegation-accept':
            result = delegation.accept(args.directory)
        else:
            delegation.watch(args.directory)
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == 'session-register':
        from ato_adapters import agents
        from ato_workflow import session_router
        print(session_router.register(args.kind, args.identity, args.project, args.source))
        return 0
    if args.command in ('local-session-create', 'local-session-grant'):
        from ato_adapters import agents
        from ato_adapters import local_session_adapter
        if args.command == 'local-session-create':
            result = local_session_adapter.create(args.project, args.role, args.source, args.credential_file, args.user_credential_file)
        else:
            result = local_session_adapter.grant(args.credential_file, args.workflow, args.output, args.source)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    raise ValueError('command outside session_cmd domain')
