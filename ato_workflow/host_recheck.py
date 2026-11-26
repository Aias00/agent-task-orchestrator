"""Re-run one failed stage task's frozen checks without granting worker retries."""
import copy
import fcntl
import os
from pathlib import Path
import signal
import uuid

from ato_workflow import budget
import runner as core
import recovery
from ato_workflow.workflow import Workflow


def recheck(directory, task_id, source):
    if not isinstance(source, str) or not source.strip():
        raise ValueError('host-recheck requires the actual host repair source')
    workflow = Workflow(directory)
    with (workflow.repo / '.codex/tasks/.worker.lock').open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another worker owns this repository')
        # Reload under the repository lock before checking authoritative state.
        workflow = Workflow(directory)
        workflow.guard()
        core.ensure_resumable(workflow.dir)
        if workflow.state['status'] not in ('blocked', 'needs_host'):
            raise ValueError('host-recheck requires a blocked or needs_host workflow')
        if not workflow.contract.get('host_handoff'):
            raise ValueError('host-recheck requires an existing authorized host_handoff')
        stage = workflow.state.get('active_run')
        rows = [r for r in workflow.state['stages'] if r['run_dir'] == stage]
        if not stage or len(rows) != 1:
            raise ValueError('host-recheck requires one current workflow stage')
        child = core.Runner(stage)
        if (child.repo.resolve() != workflow.repo.resolve()
                or Path(core.read(child.dir / 'parent.json')['workflow_dir']).resolve() != workflow.dir):
            raise ValueError('stage does not belong to this workflow and repository')
        child.parent_guard = workflow.guard
        child.guard()
        if child.plan.get('host_handoff') != workflow.contract['host_handoff']:
            raise ValueError('stage host_handoff differs from the frozen workflow')
        task = next((t for t in child.plan['tasks'] if t['id'] == task_id), None)
        if task is None:
            raise ValueError('unknown active-stage task')
        row = child.state['tasks'][task_id]
        if row['status'] not in ('failed', 'blocked', 'needs_host'):
            raise ValueError('only a failed, blocked or needs_host task can be rechecked')
        if (child.plan.get('agent', {}).get('kind') == 'acp_pi' and
                (not row.get('attempt_sequence') or row.get('native_proof_attempt') != row['attempt_sequence'])):
            raise ValueError('current ACP attempt lacks native terminal proof; host checks cannot supply it')
        if any(child.state['tasks'][d]['status'] != 'completed' for d in task.get('depends_on', [])):
            raise ValueError('selected task has an incomplete dependency')
        with recovery.execution_owner(workflow.dir, workflow.state, role='origin') as ownership:
            workflow._execution_owner = ownership
            previous = copy.deepcopy(row)
            record = dict(id=uuid.uuid4().hex, task=task_id, source=source,
                          started_at=core.now(), previous=previous,
                          plan_hash=child.state['plan_hash'], contract_hash=workflow.state['contract_hash'],
                          status='running', evidence=[])
            # Save the previous row durably before running checks. Never edit the
            # plan, retry counters, recovery grants, manual criteria or STOP files.
            child.state.setdefault('host_rechecks', []).append(record)
            started = core.now()
            workflow.deadline = budget.deadline(workflow.dir, workflow.state, started=started)
            child.deadline = workflow.deadline
            workflow._accounted_at = child._accounted_at = started
            identity = core.process_identity(os.getpid())
            workflow.state.update(status='running', owner_pid=os.getpid(), owner_identity=identity, error=None)
            child.state.update(status='running', owner_pid=os.getpid(), owner_identity=identity)
            workflow.state['execution_revision'] = workflow.state.get('execution_revision', 0) + 1
            ownership.advance(recovery.generation(workflow.dir, workflow.state))
            workflow.persist()
            child.persist()
            workflow.event('host_task_recheck_started', task=task_id, source=source, recheck=record['id'])
            old_handlers = {}

            def stopped(signum, frame):
                (workflow.dir / 'STOP').touch()

            passed = False
            outcome = 'failed'
            label = 'host-recheck-' + record['id']
            try:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    old_handlers[sig] = signal.signal(sig, stopped)
                passed, logs = child.checks(task, label)
                child.guard()
                record['evidence'] = logs
                if passed:
                    row.update(status='completed', feedback='Frozen host checks passed; worker attempts preserved.')
                    outcome = 'passed'
            except core.Halt as error:
                passed = False
                outcome = str(error)
            except Exception as error:
                passed = False
                outcome = 'error'
                record['error'] = str(error)
            finally:
                try:
                    child.kill()
                except OSError as error:
                    outcome, passed = 'cleanup_blocked', False
                    record['error'] = str(error)
                    # A passing check cannot be promoted if its process is not reaped.
                    row.clear()
                    row.update(previous)
                else:
                    child.state['active_process'] = None
                for sig, handler in old_handlers.items():
                    signal.signal(sig, handler)
                record['evidence'] = sorted(set(record['evidence']) | {
                    str(p) for p in (child.dir / 'evidence').glob(task_id + '-' + label + '-check-*.log')})
                row['evidence'] = list(dict.fromkeys(previous.get('evidence', []) + record['evidence']))
                record.update(status=outcome, finished_at=core.now())
                interrupted = outcome not in ('passed', 'failed')
                child.state['status'] = (outcome if interrupted else
                    'completed' if all(r['status'] == 'completed' for r in child.state['tasks'].values()) else 'blocked')
                child.state['owner_pid'] = None
                child.persist()
                rows[0]['status'] = child.state['status']
                rows[0]['result'] = {k: {n: r.get(n) for n in ('status', 'summary', 'feedback')}
                                     for k, r in child.state['tasks'].items()}
                # Continuation and overall acceptance remain separate actions by
                # the origin; this command never launches a worker or planner.
                workflow.state.update(status=outcome if interrupted else 'blocked', owner_pid=None,
                    error='Selected host recheck ' + outcome + '; inspect evidence and resume remaining work.')
                workflow.persist()
                workflow.event('host_task_recheck_finished', task=task_id, recheck=record['id'],
                               status=outcome, evidence=record['evidence'], attempts=row['attempts'])
            return dict(passed=passed, status=outcome, task=task_id, attempts=row['attempts'],
                        evidence=record['evidence'], workflow=str(workflow.dir), stage=str(child.dir))
