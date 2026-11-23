"""Recovery command handling; runtime context is supplied by the public facade."""
import json

def handle(args, core):
    if args.command in ('supervise', 'unsupervise', '_supervisor', 'takeover', 'takeover-status'):
        import recovery
        if args.command == 'supervise':
            recovery.enable(args.directory, args.source, args.max_requests, args.same_blocker_limit, args.response_seconds, args.window_seconds)
            recovery.start(args.directory)
            print(json.dumps(recovery.summary(args.directory), ensure_ascii=False))
        elif args.command == 'unsupervise':
            recovery.disable(args.directory)
            print('Supervision disabled; already queued messages must check takeover before acting.')
        elif args.command == '_supervisor':
            recovery.watch(args.directory)
        elif args.command == 'takeover-status':
            print(json.dumps(recovery.takeover_status(args.directory, args.request), ensure_ascii=False, indent=2))
        else:
            print(json.dumps(recovery.takeover(args.directory, args.request, args.state, args.note, args.evidence, args.recommendation), ensure_ascii=False))
        return 0
    raise ValueError('command outside recovery_cmd domain')
