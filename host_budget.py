"""Explicit origin host execution under the existing budget and workflow guard."""
import fcntl
from pathlib import Path
import signal


def run(directory, argv, source, *, accounting, core):
    """Meter an already-authorized origin command under the same task lease/ceiling."""
    if not source.strip() or not argv:
        raise ValueError('budget-run requires actual source and argv after --')
    from workflow import Workflow
    d, state, _, _ = accounting.binding(directory)
    if state.get('kind') != 'workflow':
        raise ValueError('origin host execution must belong to a total workflow')
    with (Path(state['repo'])/'.codex/tasks/.worker.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        core.ensure_resumable(d)
        if core.read(d/'state.json')['status'] in ('stopped', 'completed'):
            raise ValueError('origin host command cannot restart a stopped or completed task')
        owner = Workflow(d)
        owner.guard()  # STOP and frozen contract checked before recording any execution.
        previous = owner.state['status']
        owner._accounted_at = core.now()
        owner.deadline = accounting.deadline(d)
        owner.state.update(owner_pid=__import__('os').getpid(), status='running')
        owner.state['execution_revision'] = owner.state.get('execution_revision', 0) + 1
        owner.persist()
        owner.event('origin_host_started', source=source)
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
        for sig in handlers:
            signal.signal(sig, lambda *_: (d/'STOP').touch())
        try:
            code, log = owner.command(argv, 'origin-host-' + __import__('uuid').uuid4().hex[:8], float('inf'))
            owner.state['status'] = previous
            owner.event('origin_host_finished', code=code, evidence=log, source=source)
            return dict(code=code, evidence=log)
        except core.Halt as exc:
            owner.state['status'] = str(exc)
            raise
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            owner.state['owner_pid'] = None
            owner.persist()
