"""Opt-in task-bound worker turns under Runner's existing ownership/STOP guards.

This is a receipt ledger, not another scheduler or native session registry. It
never adopts an existing conversation or retries a claimed/uncertain delivery.
Vendor protocol and exact native readback stay in the registered adapter.
"""
import execution_metrics as metrics
import profile_evidence
import hashlib
import json
import os
from pathlib import Path
import uuid

from transport import _private_save


class WorkerDeliveryError(ValueError):
    def __init__(self, code):
        self.returncode = code
        super().__init__('native worker exit '+str(code))


def enabled(plan):
    return 'worker_session_policy' in plan


def validate(policy):
    if (not isinstance(policy, dict) or set(policy) != {'version', 'mode'}
            or type(policy['version']) is not int
            or (policy['version'], policy['mode']) not in ((1, 'task_bound_native'), (2, 'checkpoint_reconstruction'))):
        raise ValueError('worker_session_policy requires v1 task_bound_native or explicit v2 checkpoint_reconstruction')


def capabilities(kind):
    import agents
    adapter = agents.registry.get(kind)
    value = getattr(adapter, 'worker_session_capabilities', None)
    return value or dict(version=1, native_continuation='unknown',
                         persistent_process='unknown', checkpoint_request='unknown',
                         qualification='unknown')


def _path(r):
    import ato_config
    root = ato_config.ato_home()/'worker-sessions-v1'
    if root.resolve().is_relative_to(r.repo.resolve()):
        raise ValueError('worker receipt must be outside model-writable project')
    return root/(hashlib.sha256(str(r.dir).encode()).hexdigest()+'.json')


def initialize(directory):
    from types import SimpleNamespace
    import runner
    directory = Path(directory).resolve()
    state = runner.read(directory/'state.json')
    r = SimpleNamespace(dir=directory, repo=Path(state['repo']))
    path = _path(r)
    if path.exists():
        raise ValueError('worker receipt already exists; no rebinding')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.parent.stat()
    if path.parent.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('worker receipt directory ownership/mode changed')
    _private_save(path, dict(version=1, directory=str(directory), project=str(r.repo),
        plan_hash=state['plan_hash'], tasks={key: None for key in state['tasks']}))


def _store(r):
    import runtime_selection
    path = _path(r)
    info = path.parent.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077 or path.parent.is_symlink():
        raise ValueError('worker receipt directory ownership/mode changed')
    value = runtime_selection._json(path, private=True)
    if (value.get('version') != 1 or value.get('directory') != str(r.dir)
            or value.get('project') != str(r.repo) or value.get('plan_hash') != r.state['plan_hash']
            or set(value.get('tasks', {})) != set(r.state['tasks'])):
        raise ValueError('worker receipt task/project/contract mismatch')
    return value


def load(r, task):
    return _store(r)['tasks'][task['id']]


def _binding(r, task):
    import runner
    import runtime_selection
    r.guard()
    if (r.state.get('owner_pid') != os.getpid() or not r.state.get('owner_identity')
            or runner.process_identity(os.getpid()) != r.state['owner_identity']
            or r.state.get('active_process') is not None or r.process is not None):
        raise ValueError('worker session requires exact current Runner owner and confirmed cleanup')
    if r.state['repo'] != str(r.repo.resolve()) or r.dir.parent != r.repo/'.codex/tasks':
        raise ValueError('worker session project/task directory mismatch')
    plan = runner.read(r.dir/'plan.json')
    if runner.digest(plan) != r.state['plan_hash'] or plan != r.plan:
        raise ValueError('worker session frozen plan mismatch')
    selected = runtime_selection.require_current(r.dir)
    parent = None
    if (r.dir/'parent.json').exists():
        ref = runner.read(r.dir/'parent.json')
        directory = Path(ref['workflow_dir']).resolve(strict=True)
        state = runner.read(directory/'state.json')
        contract = runner.read(directory/'contract.json')
        if (directory != r.repo/'.codex/tasks/workflows'/ref['workflow_id']
                or state['id'] != ref['workflow_id'] or state['repo'] != str(r.repo)
                or state.get('active_run') != str(r.dir)
                or not getattr(r, 'parent_guard', None)
                or state.get('owner_pid') != os.getpid()
                or state.get('owner_identity') != r.state['owner_identity']
                or state['contract_hash'] != runner.digest(contract)
                or contract.get('worker_session_policy') != plan.get('worker_session_policy')
                or (directory/'STOP').exists()):
            raise ValueError('worker session parent project/contract/STOP mismatch')
        parent = dict(directory=str(directory), contract=state['contract_hash'],
                      generation=state.get('execution_revision', 0))
    return dict(project=str(r.repo), directory=str(r.dir), task=task['id'],
                contract=r.state['plan_hash'], parent=parent,
                runtime=selected['runtime']['manifest_sha256'] if selected else 'legacy-explicit-worker-v1',
                actor='runner-owned-worker-v1')


def _save(r, task, value):
    store = _store(r)
    store['tasks'][task['id']] = value
    _private_save(_path(r), store)
    r.state['tasks'][task['id']]['worker_session'] = dict(
        version=1, mode=value.get('mode'), native_id=value.get('native_id'),
        state=value['state'], turns=len(value['turns']), process_reuse=False,
        native_context_reuse=len(value['turns']) > 1 and value.get('mode') != 'checkpoint_reconstruction',
        qualification='protocol verified; actual installed CLI qualification is origin-owned')
    r.persist()


@metrics.task_scope
@metrics.measure("warmup")
def preflight(r, task, *, current=False):
    """Read-only eligibility before capacity admission or invocation accounting."""
    import agents
    import runner
    kind = r.plan.get('agent', {}).get('kind', 'codex')
    adapter = agents.registry.get(kind, 'worker')
    declaration = capabilities(kind)
    if declaration.get('native_continuation') != 'supported':
        raise ValueError('task-bound native continuation capability is '+declaration.get('native_continuation', 'unknown'))
    binding = _binding(r, task)
    expected_schema = runner.SCHEMA_V2 if runner.accounting.enabled(r.plan) else runner.SCHEMA
    if runner.read(r.dir/'result-schema.json') != expected_schema:
        raise ValueError('task-bound worker result schema changed')
    profile = adapter.worker_profile(r.plan, r.repo)
    old = load(r, task)
    if old:
        if old['binding'] != binding:
            raise ValueError('worker session project/contract/generation/runtime/profile changed')
        if old['profile'] != profile:
            raise profile_evidence.ProfileDriftError(
                'worker session project/contract/generation/runtime/profile changed', old['profile'], profile)
        if old['state'] != 'turn_completed' or not old.get('cleanup_confirmed'):
            raise ValueError('worker session claimed/delivery_unknown/cleanup barrier; no replay')
        adapter.worker_verify(old, r.repo)
        for ancestor in old.get('ancestors', []):
            adapter.worker_verify(ancestor, r.repo)
    sequence = r.state['tasks'][task['id']].get('attempt_sequence', 0) + (0 if current else 1)
    if type(sequence) is not int or sequence != (old['turns'][-1]['sequence']+1 if old else 1):
        raise ValueError('worker session invocation generation was reset or skipped')
    return adapter, binding, profile, old


def execute(r, task, label, schema, output, prompt, **meter):
    """One foreground delivery; claims survive crashes, STOP and invalid output."""
    import agents
    import runner
    adapter, binding, profile, old = preflight(r, task, current=True)
    kind = r.plan.get('agent', {}).get('kind', 'codex')
    value = old or dict(version=1, binding=binding, profile=profile, turns=[],
                        source='ctask-worker/'+uuid.uuid4().hex, native_id=None)
    reconstruct = r.plan['worker_session_policy']['mode'] == 'checkpoint_reconstruction'
    if reconstruct and old:
        # Explicit new-ID mode, only after exact prior terminal/cleanup/readback
        # and host checkpoint validation. Never an escape from unknown delivery.
        import copy
        value = copy.deepcopy(old)
        value.setdefault('ancestors', []).append({key: old[key] for key in
            ('native_id', 'source', 'rollout_path', 'offset', 'rollout_sha256', 'file_identity')})
        for key in ('native_id', 'rollout_path', 'offset', 'rollout_sha256', 'file_identity'):
            value.pop(key, None)
        value.update(native_id=None, source='ctask-worker/'+uuid.uuid4().hex)
    correlation = 'worker-turn-'+uuid.uuid4().hex
    generation = r.state['tasks'][task['id']]['attempt_sequence']
    prompt = ('ATO task-bound message '+correlation+'\n'+prompt)
    if old and not reconstruct:
        # Native context contains the previous turn. The complete objective and
        # current stage remain explicit; historical memory/scans aren't replayed.
        prompt += '\nContinue the exact task-owned native context; preserve prior verified work.\n'
    elif old:
        prompt += '\nExplicit checkpoint reconstruction: this is a new task-owned native session, not a native resume. Use the host-validated files/results/counters and full goal supplied above.\n'
    value.update(state='claimed', cleanup_confirmed=False, correlation_id=correlation,
                 generation=generation, owner=dict(pid=os.getpid(), identity=r.state['owner_identity']),
                 mode='checkpoint_reconstruction' if reconstruct else 'native_resume' if old else 'cold_native')
    _save(r, task, value)
    def publish(observation):
        value['profile_observation'] = observation
        _save(r, task, value)
    try:
        with profile_evidence.Turn(profile, lambda: adapter.worker_profile(r.plan, r.repo),
                                   publish, generation):
            argv = adapter.worker_command(r.plan, schema, output, value)
            code, log = r.command(argv, label, r.plan['limits']['attempt_seconds'], prompt,
                                  agent_kind=kind, **meter)
            if code:
                raise WorkerDeliveryError(code)
            # Result and receipt validation are independent: a valid JSON answer
            # cannot replace the native session/turn/profile readback.
            import runtime_selection
            result_bytes = runtime_selection._read(output)
            if len(result_bytes) > 1024*1024:
                raise ValueError('task-bound worker result too large')
            result = agents.validate_result(json.loads(result_bytes), runner.read(schema))
            receipt = adapter.worker_receipt(value, log, r.repo, correlation, generation)
            if r.state.get('active_process') is not None or r.process is not None:
                raise ValueError('native worker cleanup unconfirmed')
            r.guard()
        value.update(receipt)
        value['turns'].append(dict(sequence=generation, correlation_id=correlation,
            message_id=correlation, native_id=receipt['native_id'], turn_id=receipt['turn_id'],
            mode=value['mode'], profile_observation=value['profile_observation'], output_sha256=hashlib.sha256(result_bytes).hexdigest(),
            outcome=result.get('status'), cleanup_confirmed=True))
        value.update(state='turn_completed', cleanup_confirmed=True)
        _save(r, task, value)
        return code, log
    except BaseException:
        value.update(state='delivery_unknown', cleanup_confirmed=r.state.get('active_process') is None and r.process is None)
        r.state['tasks'][task['id']].update(delivery_unknown=True, status='blocked',
            feedback='Task-bound native delivery is unknown; no automatic replay or replacement session.')
        _save(r, task, value)
        raise


def quarantined(r, task):
    """Check durable claims before retries AND host-only acceptance promotion."""
    value = load(r, task)
    return value is not None and (value['state'] != 'turn_completed' or not value.get('cleanup_confirmed'))


def host_acceptable(r, task):
    value = load(r, task)
    row = r.state['tasks'][task['id']]
    if not (value and value['state'] == 'turn_completed' and value.get('cleanup_confirmed')
            and value['turns'] and value['turns'][-1]['sequence'] == row.get('attempt_sequence')
            and not row.get('delivery_unknown') and not row.get('session_refused')):
        return False
    import agents
    adapter = agents.registry.get(r.plan.get('agent', {}).get('kind', 'codex'), 'worker')
    if (value['binding']['project'] != str(r.repo) or value['binding']['directory'] != str(r.dir)
            or value['binding']['task'] != task['id'] or value['binding']['contract'] != r.state['plan_hash']
            or value['profile'] != adapter.worker_profile(r.plan, r.repo)):
        return False
    try:
        adapter.worker_verify(value, r.repo)
        for ancestor in value.get('ancestors', []):
            adapter.worker_verify(ancestor, r.repo)
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return True
