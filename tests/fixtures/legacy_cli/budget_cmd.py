"""Budget command handling; runtime context is supplied by the public facade."""
import json

def handle(args, core):
    if args.command == 'budget-policy':
        from ato_workflow import budget
        if args.action == 'set':
            if not args.mode or not args.source or (not args.request):
                raise ValueError('budget-policy set requires --mode --source --request')
            result = budget.set_time_policy(args.mode, args.source, args.request)
        else:
            result = budget.time_policy()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command in ('budget-estimate', 'budget-configure', 'budget-reforecast', 'budget-charge', 'budget-run'):
        from ato_workflow import budget
        if args.command == 'budget-estimate':
            result = budget.estimate(core.read(args.estimate))
        elif args.command == 'budget-configure':
            budget.configure(args.directory, {'source': args.source, 'estimate': core.read(args.estimate)}, request=args.request, hard_limit_seconds=args.hard_limit_seconds, accepted_answer=args.accepted_answer)
            result = budget.summary(args.directory)
        elif args.command == 'budget-reforecast':
            budget.reforecast(args.directory, core.read(args.estimate), args.source, args.request)
            result = budget.summary(args.directory)
        elif args.command == 'budget-charge':
            budget.charge(args.directory, args.seconds, args.source, args.evidence, args.request)
            result = budget.summary(args.directory)
        else:
            result = budget.host_run(args.directory, args.argv[1:] if args.argv[:1] == ['--'] else args.argv, args.source)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result.get('code', 0)
    raise ValueError('command outside budget_cmd domain')
