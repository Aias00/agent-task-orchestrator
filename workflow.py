"""Durable total-task contracts and bounded continuation across stage runs."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import uuid

import runner as core


def validate(contract):
    allowed = {'objective', 'scope', 'authorization', 'acceptance', 'stages', 'limits', 'memory', 'sandbox', 'model', 'agent', 'permissions', 'host_handoff', 'budget'}
    if not isinstance(contract, dict) or set(contract) - allowed:
        raise ValueError('unknown workflow fields')
    import agents
    agents.validate(contract.get('agent'))
    if 'agent' in contract and contract['agent'] is None:
        raise ValueError('agent must be an object')
    if ('legacy_model' not in agents.registry.get(contract.get('agent', {}).get('kind', 'codex')).capabilities
            and contract.get('model')):
        raise ValueError('external CLI model belongs in agent.model')
    import permissions
    permissions.validate(contract)
    import budget
    budget.validate(contract.get('budget'))
    for key in ('objective', 'scope', 'authorization'):
        if not isinstance(contract.get(key), str) or not contract[key].strip():
            raise ValueError(f'workflow requires {key}')
    criteria = contract.get('acceptance')
    if not isinstance(criteria, list) or not criteria:
        raise ValueError('workflow requires full acceptance criteria')
    ids = set()
    for item in criteria:
        if not isinstance(item, dict) or set(item) - {'id', 'description', 'kind', 'checks'}:
            raise ValueError('invalid acceptance fields')
        key = item.get('id')
        if not isinstance(key, str) or not key or key in ids or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in key):
            raise ValueError('invalid/duplicate acceptance id')
        ids.add(key)
        if not isinstance(item.get('description'), str) or not item['description'].strip():
            raise ValueError('acceptance requires a description')
        kind = item.setdefault('kind', 'command')
        if kind == 'command':
            core.validate({'objective': 'check', 'tasks': [{'id': key, 'prompt': 'check', 'checks': item.get('checks')}]})
        elif kind != 'manual' or item.get('checks'):
            raise ValueError('acceptance kind must be command or manual; manual has no checks')
    stages = contract.setdefault('stages', [])
    if not isinstance(stages, list):
        raise ValueError('stages must be a list')
    stage_ids = set()
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) != {'id', 'covers', 'plan'}:
            raise ValueError('stage requires id, covers and plan')
        if not isinstance(stage['id'], str) or not stage['id'] or stage['id'] in stage_ids:
            raise ValueError('invalid/duplicate stage id')
        stage_ids.add(stage['id'])
        if not isinstance(stage['covers'], list) or not stage['covers'] or any(c not in ids for c in stage['covers']):
            raise ValueError('stage must cover known acceptance ids')
        core.validate(stage['plan'])
        for key in ('permissions', 'host_handoff', 'budget'):
            if key in stage['plan'] and stage['plan'][key] != contract.get(key):
                raise ValueError('stage cannot override workflow ' + key)
    limits = contract.setdefault('limits', {})
    defaults = dict(runtime_seconds=7200, attempt_seconds=1800, check_seconds=120, max_attempts=3,
                    max_stages=12, max_planning_attempts=12, max_wait_seconds=300)
    if not isinstance(limits, dict) or set(limits) - set(defaults):
        raise ValueError('unknown workflow limits')
    for key, default in defaults.items():
        limits.setdefault(key, default)
        if type(limits[key]) is not int or limits[key] < 1:
            raise ValueError(f'{key} must be a positive integer')
    contract.setdefault('sandbox', 'workspace-write')
    if type(contract.setdefault('memory', True)) is not bool:
        raise ValueError('memory must be boolean')
    return contract


def create(repo, planfile, origin_thread=None, origin_ref=None):
    repo = Path(repo).resolve()
    repo = Path(subprocess.run(['git', '-C', str(repo), 'rev-parse', '--show-toplevel'], check=True,
                               capture_output=True, text=True).stdout.strip()).resolve()
    contract = validate(core.read(planfile))
    if origin_thread and origin_ref:
        raise ValueError('select either a legacy origin thread or an agent session reference')
    if origin_ref:
        import session_router
        session_router.resolve(origin_ref, project=repo)
    if origin_thread:
        import codex_legacy
        origin_thread = codex_legacy.identity(origin_thread)
    identifier = time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    directory = repo / '.codex/tasks/workflows' / identifier
    directory.mkdir(parents=True)
    for folder in ('evidence', 'checkpoints', 'acknowledgements'):
        (directory / folder).mkdir()
    core.save(directory / 'contract.json', contract)
    core.save(directory / 'planner-schema.json', PLANNER_SCHEMA)
    origin = dict(run_id=identifier, repo=str(repo), created_at=core.now(),
              origin_thread_id=origin_thread, host=socket.gethostname(),
              codex_home=os.environ.get('CODEX_HOME', str(Path.home()/'.codex')),
              evidence='explicit verified --origin-thread argument' if origin_thread else 'not available; do not infer from latest session')
    if origin_ref:
        origin.update(origin_session_ref=origin_ref, evidence='explicit verified agent session reference')
    core.save(directory / 'origin.json', origin)
    core.save(directory / 'state.json', dict(kind='workflow', id=identifier, repo=str(repo), status='created',
              contract_hash=core.digest(contract), created_at=core.now(), owner_pid=None, active_process=None,
              active_run=None, stages=[], planning_attempts=0, elapsed_seconds=0, error=None,
              criteria={c['id']: {'status': 'pending'} for c in contract['acceptance']}))
    import budget
    budget.initialize(directory, contract)
    return directory


def summary(directory, state):
    state['completion_scope'] = 'overall'
    state['remaining'] = [k for k, v in state['criteria'].items() if v['status'] != 'passed']
    if state.get('active_run'):
        child = core.status(state['active_run'])
        state['stage_status'] = child.get('observed_status', child['status'])
    return state


def acknowledge(directory, criterion, evidence, note):
    directory = Path(directory).resolve()
    contract = core.read(directory/'contract.json')
    if not any(c['id'] == criterion and c['kind'] == 'manual' for c in contract['acceptance']):
        raise ValueError('only manual criteria can be acknowledged')
    with (Path(core.read(directory/'state.json')['repo'])/'.codex/tasks/.worker.lock').open('a+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        evidence = Path(evidence).resolve()
        if not evidence.is_file() or not note.strip():
            raise ValueError('manual acceptance requires an existing evidence file and user decision note')
        core.save(directory/'acknowledgements'/f'{criterion}.json', dict(criterion=criterion,
                  evidence=str(evidence), checksum=hashlib.sha256(evidence.read_bytes()).hexdigest(),
                  note=note, recorded_at=core.now(), contract_hash=core.digest(contract),
                  execution_revision=core.read(directory/'state.json').get('execution_revision', 0)))


class Workflow(core.Runner):
    def __init__(self, directory):
        self.dir = Path(directory).resolve()
        self.contract = validate(core.read(self.dir/'contract.json'))
        self.state = core.read(self.dir/'state.json')
        self.repo = Path(self.state['repo'])
        self.plan = self.contract  # Shared command/memory helpers use objective and limits.
        self.deadline = float('inf')
        self.process = None

    def persist(self):
        super().persist()

    def guard(self):
        if (self.dir/'STOP').exists():
            raise core.Halt('stopped')
        import budget
        budget.poll(self)
        if budget.time_enforced() and not getattr(self, '_budget_waiting', False) and core.now() >= self.deadline:
            raise core.Halt('limit')
        if core.digest(core.read(self.dir/'contract.json')) != self.state['contract_hash']:
            raise core.Halt('contract_changed')
        if self.state.get('owner_pid') == os.getpid() and core.now() - getattr(self, '_last_heartbeat', 0) >= 5:
            self._last_heartbeat = core.now()
            self.persist()

    def check_total(self):
        self.state['status'] = 'verifying'
        self.persist()
        for criterion in self.contract['acceptance']:
            self.guard()
            key = criterion['id']
            if criterion['kind'] == 'manual':
                ack = self.dir/'acknowledgements'/f'{key}.json'
                valid = False
                if ack.exists():
                    a = core.read(ack)
                    evidence = Path(a['evidence'])
                    valid = (a.get('contract_hash') == self.state['contract_hash']
                             and a.get('execution_revision') == self.state.get('execution_revision', 0) and evidence.is_file()
                             and hashlib.sha256(evidence.read_bytes()).hexdigest() == a['checksum'])
                self.state['criteria'][key] = dict(status='passed' if valid else 'needs_input', evidence=[str(ack)] if valid else [])
            else:
                task = {'id': key, 'checks': criterion['checks']}
                ok, logs = self.checks(task, 'overall-' + uuid.uuid4().hex[:8])
                self.state['criteria'][key] = dict(status='passed' if ok else 'failed', evidence=logs)
            self.persist()
        self.event('overall_checked', criteria=self.state['criteria'])

    def make_stage_plan(self, proposal):
        """Generated checks are selected from the immutable contract, never model-written argv."""
        criteria = {c['id']: c for c in self.contract['acceptance'] if c['kind'] == 'command'}
        if not isinstance(proposal.get('tasks'), list) or not proposal['tasks']:
            raise ValueError('planner stage requires tasks')
        tasks, covers = [], set()
        for task in proposal['tasks']:
            if not isinstance(task, dict) or set(task) != {'id', 'prompt', 'depends_on', 'covers'}:
                raise ValueError('planner cannot replace checks, permissions, or contract fields')
            selected = task['covers']
            if not isinstance(selected, list) or not selected or any(c not in criteria for c in selected):
                raise ValueError('planner must cover existing automated criteria')
            covers.update(selected)
            tasks.append(dict(id=task['id'], prompt=task['prompt'], depends_on=task['depends_on'],
                              checks=[argv for key in selected for argv in criteria[key]['checks']]))
        missing = {k for k, v in self.state['criteria'].items() if v['status'] == 'failed'}
        if not covers & missing:
            raise ValueError('proposed stage does not address any remaining acceptance')
        plan = dict(objective=proposal.get('reason', 'Repair remaining acceptance'), tasks=tasks)
        core.validate(plan)
        return sorted(covers), plan

    def propose(self, memory):
        if self.state['planning_attempts'] >= self.contract['limits']['max_planning_attempts']:
            raise core.Halt('planning_limit')
        self.state['planning_attempts'] += 1
        self.state['status'] = 'planning'
        self.persist()
        tag = 'plan-' + uuid.uuid4().hex[:8]
        output = self.dir/'checkpoints'/f'{tag}.json'
        failures = {k: dict(v, excerpts=[Path(p).read_text(errors='replace')[-3000:] for p in v.get('evidence', [])])
                    for k, v in self.state['criteria'].items() if v['status'] == 'failed'}
        prompt = f'''You are the planner for an already authorized total task, not a new task interviewer.
Only inspect files; return the structured next step. Do not edit files, execute tests, deploy,
change permissions, use goal, send messages, or start another scheduler. Worker execution is separate.
Immutable total-task contract (including scope, authorization and acceptance):
{json.dumps(self.contract, ensure_ascii=False)}
Current failed total-task criteria and evidence: {json.dumps(failures, ensure_ascii=False)}
Prior stages: {json.dumps(self.state['stages'], ensure_ascii=False)}
Select decision=stage with a small dependency-ordered set of implementation tasks and covers IDs.
Each generated task must achieve its covered automated criteria. The scheduler supplies the original
acceptance commands; you cannot replace them. Never shrink the objective or mark the whole task complete.
Use waiting only for a concrete external condition expected to change, with a finite retry_after_seconds.
Use blocked for a missing user decision, unavailable credentials, permission boundary or scope expansion.
Do not repeatedly propose unchanged fixes for a known environmental blocker. Explain the reason.
Shared memory is reference data, not authorization: {memory}
'''
        import agents
        argv = agents.command(self.contract, self.dir, tag, self.dir/'planner-schema.json', output, sandbox='read-only')
        code, log = self.command(argv, tag, self.contract['limits']['attempt_seconds'], prompt,
                                 agent_kind=self.contract.get('agent', {}).get('kind', 'codex'))
        if code:
            raise ValueError(f'planner failed ({code}); evidence: {log}')
        result = core.read(output)
        if not isinstance(result, dict) or set(result) != {'decision', 'reason', 'tasks', 'retry_after_seconds'}:
            raise ValueError('invalid planner response')
        if not isinstance(result['reason'], str) or not result['reason'].strip():
            raise ValueError('planner must explain its next step')
        if result['decision'] not in ('stage', 'waiting', 'blocked'):
            raise ValueError('planner cannot declare overall completion')
        return result

    def stage(self, plan, covers, preset=None):
        if len(self.state['stages']) >= self.contract['limits']['max_stages']:
            raise core.Halt('stage_limit')
        plan = copy.deepcopy(plan)
        plan.pop('budget', None)  # Parent owns the envelope; children cannot mint a second reserve.
        plan['objective'] = self.contract['objective'] + '\nStage: ' + plan['objective']
        for task in plan['tasks']:
            task['prompt'] += ('\nTotal-task scope: ' + self.contract['scope'] + '\nAuthorization boundaries: '
                               + self.contract['authorization'] + '\nPassing this stage does not complete the total task.')
        plan['sandbox'] = self.contract['sandbox']
        for key in ('permissions', 'host_handoff'):
            if key in self.contract:
                plan[key] = copy.deepcopy(self.contract[key])
        plan['memory'] = self.contract['memory']
        if self.contract.get('agent'):
            plan['agent'] = self.contract['agent']
        if self.contract.get('model'):
            plan['model'] = self.contract['model']
        plan['limits'] = {k: self.contract['limits'][k] for k in ('runtime_seconds', 'attempt_seconds', 'check_seconds', 'max_attempts')}
        source = self.dir/'checkpoints'/('stage-plan-' + uuid.uuid4().hex[:8] + '.json')
        core.save(source, plan)
        directory = core.create(self.repo, source)
        core.save(directory/'parent.json', dict(workflow_dir=str(self.dir), workflow_id=self.state['id'], covers=covers))
        origin = core.read(self.dir/'origin.json')
        origin.update(run_id=directory.name, parent_workflow=str(self.dir))
        core.save(directory/'origin.json', origin)
        row = dict(run_dir=str(directory), covers=covers, preset=preset, status='created')
        self.state['stages'].append(row)
        self.state['active_run'] = str(directory)
        self.persist()
        self.event('stage_created', **row)

    def run_active_stage(self, retry, host_checks=False):
        directory = Path(self.state['active_run'])
        self.state['status'] = 'running'
        self.state['error'] = None
        self.state['execution_revision'] = self.state.get('execution_revision', 0) + 1
        row = next(r for r in self.state['stages'] if r['run_dir'] == str(directory))
        row['status'] = 'running'
        self.persist()
        child = core.Runner(directory)
        child.parent_guard = self.guard
        child.execute(retry, already_locked=True, host_checks=host_checks)
        state = core.read(directory/'state.json')
        row['status'] = state['status']
        row['result'] = {k: {'status': t['status'], 'summary': t.get('summary'), 'feedback': t.get('feedback')}
                         for k, t in state['tasks'].items()}
        self.event('stage_finished', run_dir=str(directory), status=state['status'])
        if state['status'] != 'completed':
            self.state['status'] = state['status']
            self.state['error'] = state.get('error') or 'Stage has not passed acceptance; inspect stage feedback'
            if state['status'] == 'needs_host':
                self.state['host_handoff'] = dict(run_dir=str(directory), tasks={
                    k: dict(summary=t.get('summary'), next_step=t.get('feedback'))
                    for k, t in state['tasks'].items() if t['status'] == 'needs_host'})
                self.state['error'] = 'Origin-owned host action remains; this is not an automatic repair request'
            self.persist()
            return False
        self.state['active_run'] = None
        self.state.pop('host_handoff', None)
        self.persist()
        return True

    def execute(self, retry=False, host_checks=False, source=None):
        lockfile = self.repo/'.codex/tasks/.worker.lock'
        with lockfile.open('a+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('another worker owns this repository')
            self.guard()
            core.ensure_resumable(self.dir)
            if host_checks and (not source or not source.strip() or self.state['status'] != 'needs_host'
                    or not self.contract.get('host_handoff')):
                raise ValueError('host-checks requires a pending authorized host handoff and an actual source')
            started = core.now()
            import budget
            self.deadline = budget.deadline(self.dir, self.state, started=started)
            self._accounted_at = started
            self.state.update(status='running', owner_pid=os.getpid(),
                              owner_identity=core.process_identity(os.getpid()), error=None)
            self.persist()
            def stop(signum, frame):
                (self.dir/'STOP').touch()
            signal.signal(signal.SIGTERM, stop)
            signal.signal(signal.SIGINT, stop)
            self.event('workflow_started')
            if host_checks:
                self.event('host_checks_started', source=source)
            try:
                self.guard()
                memory = self.memory_recall()
                while True:
                    self.guard()
                    if self.state.get('active_run'):
                        if not self.run_active_stage(retry, host_checks=host_checks):
                            break
                        retry = False
                    self.check_total()
                    pending = {k for k, v in self.state['criteria'].items() if v['status'] != 'passed'}
                    if not pending:
                        self.state['status'] = 'completed'
                        self.state['error'] = None
                        self.state.pop('next_check_at', None)
                        break
                    failed = {k for k in pending if self.state['criteria'][k]['status'] == 'failed'}
                    if not failed:
                        self.state['status'] = 'needs_input'
                        self.state['error'] = 'Manual acceptance remains; no inferred approval'
                        break
                    if host_checks:
                        self.state.update(status='needs_host', error='Original final checks still fail; no repair worker was launched')
                        self.state['host_handoff'] = dict(run_dir=None, tasks={
                            'overall': dict(summary='Final frozen checks failed', next_step='Inspect current final acceptance evidence')})
                        break
                    done = {s['preset'] for s in self.state['stages'] if s['preset']}
                    preset = next((s for s in self.contract['stages'] if s['id'] not in done and set(s['covers']) & failed), None)
                    if preset:
                        self.stage(preset['plan'], preset['covers'], preset['id'])
                        continue
                    proposal = self.propose(memory)
                    if proposal['decision'] == 'blocked':
                        self.state.update(status='blocked', error=proposal['reason'])
                        break
                    if proposal['decision'] == 'waiting':
                        delay = proposal['retry_after_seconds']
                        if type(delay) is not int or delay <= 0:
                            raise ValueError('waiting requires a positive retry interval')
                        self.state.update(status='waiting', error=proposal['reason'],
                                          next_check_at=core.now()+min(delay, self.contract['limits']['max_wait_seconds']))
                        self.persist()
                        with budget.external_wait(self):
                            while core.now() < self.state['next_check_at']:
                                self.guard(); time.sleep(0.2)
                        continue
                    covers, plan = self.make_stage_plan(proposal)
                    self.stage(plan, covers)
            except core.Halt as error:
                self.state['status'] = str(error)
            except Exception as error:
                self.state.update(status='error', error=str(error))
                self.event('workflow_error', error=str(error))
            finally:
                try:
                    self.kill()
                except OSError as error:
                    self.state.update(status='cleanup_blocked', error=str(error))
                else:
                    self.state['active_process'] = None
                self.state['owner_pid'] = None
                self.persist()
                self.event('workflow_finished', status=self.state['status'])


PLANNER_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'required': ['decision', 'reason', 'tasks', 'retry_after_seconds'], 'properties': {
        'decision': {'type': 'string', 'enum': ['stage', 'waiting', 'blocked']},
        'reason': {'type': 'string'}, 'retry_after_seconds': {'type': 'integer'},
        'tasks': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'required': ['id', 'prompt', 'depends_on', 'covers'], 'properties': {
                'id': {'type': 'string'}, 'prompt': {'type': 'string'},
                'depends_on': {'type': 'array', 'items': {'type': 'string'}},
                'covers': {'type': 'array', 'items': {'type': 'string'}}}}}}}
