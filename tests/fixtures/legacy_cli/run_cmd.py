"""Run command handling; runtime context is supplied by the public facade."""
import json
import fcntl
import subprocess
from pathlib import Path

def handle(args, core):
    if args.command == 'trace':
        import task_trace
        print(json.dumps(task_trace.query(args.directory, args.task), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'permission-check':
        import permissions
        plan = core.read(args.plan)
        permissions.validate(plan)
        argv = permissions.probe_argv(plan, args.repo)
        result = subprocess.run(argv, cwd=args.repo, capture_output=True, text=True, timeout=60)
        print(json.dumps(dict(requested=permissions.describe(plan), probe_kind='native-sandbox-profile', passed=result.returncode == 0, stdout=result.stdout[-12000:], stderr=result.stderr[-4000:]), ensure_ascii=False, indent=2))
        return 0 if result.returncode == 0 else 1
    if args.command == 'create':
        print(core.create(args.repo, args.plan))
        return 0
    if args.command == 'validate':
        plan = core.read(args.plan)
        print(json.dumps({'order': core.validate(plan), 'limits': plan['limits']}))
        return 0
    if args.command == 'list':
        root = Path(args.repo) / '.codex/tasks'
        for d in sorted([*root.glob('*/state.json'), *root.glob('workflows/*/state.json')]):
            s = core.status(d.parent)
            print(json.dumps({'directory': str(d.parent), 'status': s.get('observed_status', s['status']), 'effective_status': s.get('delivery', {}).get('effective_status', s['status']), 'completion_basis': s.get('delivery', {}).get('completion_basis'), 'id': s['id'], 'kind': s.get('kind', 'stage'), 'completion_scope': s.get('completion_scope'), 'remaining': s.get('remaining'), 'recovery': s.get('recovery')}))
        return 0
    if args.command == 'status':
        print(json.dumps(core.status(args.directory), ensure_ascii=False, indent=2))
        return 0
    if args.command == 'stop':
        d = Path(args.directory).resolve()
        core.read(d / 'state.json')
        (d / 'STOP').touch()
        print('Stop requested; worker terminates its active process group.')
        return 0
    if args.command in ('run', '_worker'):
        if (Path(args.directory) / 'user-closure.json').exists():
            raise ValueError('workflow was explicitly closed by its user')
        if args.command == '_worker':
            import recovery
            with recovery.locked(Path(args.directory) / 'launch.lock'):
                pass
        if core.read(Path(args.directory) / 'state.json').get('kind') == 'workflow':
            import recovery
            recovery.start(args.directory)
            from workflow import Workflow
            Workflow(args.directory).execute(args.retry_failed)
        else:
            core.Runner(args.directory).execute(args.retry_failed)
        return 0
    if args.command in ('start', 'resume'):
        d = Path(args.directory).resolve()
        state = core.status(d)
        lockfile = Path(state['repo']) / '.codex/tasks/.worker.lock'
        import recovery
        with recovery.locked(d / 'launch.lock'), lockfile.open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('a worker is already running in this repository')
            import recovery
            launch = d / 'launch.json'
            if launch.exists():
                info = core.read(launch)
                if core.now() - info['at'] < 15 and recovery.alive(info['pid']):
                    raise ValueError('a worker launch is already pending')
            core.ensure_resumable(d)
            if args.command == 'resume' and args.accepted_answer:
                import budget
                budget.check_approved_resume(d, args.accepted_answer)
            (d / 'STOP').unlink(missing_ok=True)
            cmd = core.runtime_command('_worker', str(d))
            if args.retry_failed:
                cmd.append('--retry-failed')
            with (d / 'worker.log').open('ab') as log:
                p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            launch_info = dict(pid=p.pid, at=core.now())
            if args.command == 'resume' and args.accepted_answer:
                launch_info['approved_budget_answer'] = args.accepted_answer
            core.save(launch, launch_info)
        print(json.dumps({'pid': p.pid, 'directory': str(d), 'status': 'launching'}))
        return 0
    raise ValueError('command outside run_cmd domain')
