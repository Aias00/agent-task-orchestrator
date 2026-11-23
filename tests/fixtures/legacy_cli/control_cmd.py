"""Control command handling; runtime context is supplied by the public facade."""
import json

def handle(args, core):
    if args.command == 'operational-disposition':
        import control
        print(json.dumps(control.operational_disposition(args.identifier, args.category, args.blocker, args.tried, args.evidence, args.recommendation, args.source), ensure_ascii=False, indent=2))
        return
    if args.command == 'notification-policy':
        import notifications
        if args.action in ('mute', 'unmute'):
            if not args.repo:
                raise ValueError('project notification preference requires --repo')
            result = notifications.configure_project(args.repo, args.action == 'mute', args.source)
        else:
            result = notifications.configure(args.mode, args.source, completion=args.completion) if args.action == 'set' else notifications.status()
            result['project_mutes'] = notifications.project_mutes()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command in ('control', 'followups', 'presented', 'followup-link', '_controller'):
        import control
        if args.command == '_controller':
            control.watch()
            return 0
        if args.command == 'followups':
            result = control.rows(args.all)
        elif args.command == 'presented':
            result = control.record(args.identifier, state='presented', note=args.source)
        elif args.command == 'followup-link':
            result = control.record(args.identifier, workflow=args.workflow, note=args.note, evidence=args.evidence)
        elif args.action == 'start':
            if not args.source:
                raise ValueError('control start requires a user instruction source')
            control.enable(args.source, args.reminder_seconds, args.max_reminders)
            result = control.start()
        elif args.action == 'stop':
            control.disable()
            result = control.status()
        elif args.action == 'tick':
            control.tick()
            result = control.status()
        else:
            result = control.status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.command == 'watch':
        import control
        import interaction
        import workflow_closure
        d, _, _, origin = interaction.binding(args.directory, require_open=False)
        workflow_closure.actor(origin)
        if not args.source.strip():
            raise ValueError('observation source required')
        control.register(d)
        print(json.dumps(dict(workflow=str(d), observation_only=True, source=args.source)))
        return 0
    if args.command == 'failure-alerts':
        import notifications
        print(json.dumps(notifications.configure_failure_alerts(args.source), ensure_ascii=False))
        return 0
    raise ValueError('command outside control_cmd domain')
