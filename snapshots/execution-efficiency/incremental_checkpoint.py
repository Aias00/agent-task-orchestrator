"""Versioned, host-observed checkpoints inside the existing foreground Runner.

Private receipts are evidence, never delivery, retry, owner or STOP authority.
Running snapshots cannot be resumed. Only a terminal, cleaned invocation whose
workspace, output, native receipt and cumulative ledger still match is eligible.
No prompt, stderr, configuration contents or native transcript is copied here.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time

import execution_metrics as metrics
import execution_accounting as accounting
import runtime_selection
from transport import _private_save

MAX_JSON = 2 * 1024 * 1024
MAX_PERIODIC = 64
MAX_RECORDS = 2048
MAX_CHAIN_BYTES = 32 * 1024 * 1024
EXTRA_SUFFIXES = {'.txt', '.json', '.toml', '.yaml', '.yml', '.lock', '.ini', '.cfg'}
DIAGNOSTIC_SUFFIXES = {'.log', '.jsonl', '.sqlite', '.sqlite3', '.db', '.pyc', '.pyo'}
PRIVATE_NAMES = re.compile(r'(?i)(history|transcript|prompt|stderr)')


def enabled(plan):
    return 'checkpoint_policy' in plan


def validate(policy, plan):
    required = {'version', 'closing_margin_seconds', 'interval_seconds'}
    if (not isinstance(policy, dict) or not required <= set(policy) or set(policy)-required-{'max_age_seconds'}
            or type(policy['version']) is not int or policy['version'] != 1):
        raise ValueError('checkpoint_policy requires version=1, closing_margin_seconds, interval_seconds')
    if not accounting.enabled(plan):
        raise ValueError('checkpoint_policy requires explicit execution_policy v2; no historical migration')
    if plan.get('sandbox') == 'danger-full-access':
        raise ValueError('private host checkpoint authority requires a bounded worker filesystem grant')
    margin, interval = policy['closing_margin_seconds'], policy['interval_seconds']
    if type(margin) is not int or not 1 <= margin < plan['limits']['attempt_seconds']:
        raise ValueError('checkpoint closing margin must be positive and smaller than attempt_seconds')
    if type(interval) is not int or not 1 <= interval <= 300:
        raise ValueError('checkpoint interval must be between 1 and 300 seconds')
    age = policy.get('max_age_seconds', 86400)
    if type(age) is not int or not 1 <= age <= 604800:
        raise ValueError('checkpoint max_age_seconds must be between 1 and 604800')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _regular(path, *, private=False):
    """Bounded, no-link read, including every ancestor supplied by the caller."""
    path = Path(path).absolute()
    parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent);parent = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_JSON
                or private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600)):
            raise ValueError('checkpoint/evidence file ownership, mode, size or type refused')
        data = stream.read(MAX_JSON+1)
        if len(data) > MAX_JSON:
            raise ValueError('checkpoint/evidence exceeded read bound')
        final = os.fstat(stream.fileno())
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode) != (final.st_size, final.st_mtime_ns, final.st_ctime_ns, final.st_mode):
            raise ValueError('checkpoint/evidence changed during read')
        return dict(sha256=hashlib.sha256(data).hexdigest(), mode=stat.S_IMODE(info.st_mode), size=len(data)), data


def snapshot(repo):
    """Bounded safe workspace bytes/modes; excluded private paths are not read.

    This is an explicit safe workspace scope, not a claim to know undeclared
    external dependencies. Checkpoints never turn these receipts into a cache.
    """
    result = dict(version=1, scope='safe-workspace-v1', complete=True, files={})
    try:
        with __import__('tempfile').TemporaryFile() as stream:
            subprocess.run(['git', '-C', str(repo), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
                stdout=stream, stderr=subprocess.DEVNULL, check=True, timeout=5)
            stream.seek(0); data = stream.read(1024*1024+1)
        if len(data) > 1024*1024:
            raise ValueError('checkpoint file inventory too large')
        selected = []
        for name in sorted(set(data.decode('utf-8').split('\0')) - {''}):
            p = Path(name)
            # A source-only suffix list silently loses extensionless artifacts.
            # Inventory every bounded nonprivate artifact, while diagnostic
            # streams never become progress and sensitive files are not read.
            safe = accounting.safe_source(str(p.with_suffix('.py')))
            if p.suffix in accounting.SOURCE_SUFFIXES- {'.md', '.rst'} and not p.name.startswith('.'):
                safe = accounting.safe_source(str(p.with_name('source.py')))
            safe = safe and p.suffix not in DIAGNOSTIC_SUFFIXES and not PRIVATE_NAMES.search(name)
            if safe:
                selected.append(name)
        if len(selected) > accounting.MAX_FILES:
            raise ValueError('checkpoint file count exceeded')
        total = 0
        for name in selected:
            value, _ = _regular(Path(repo)/name)
            total += value['size']
            if value['size'] > accounting.MAX_FILE_BYTES or total > accounting.MAX_TOTAL_BYTES:
                raise ValueError('checkpoint workspace byte bound exceeded')
            result['files'][name] = value
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        result['complete'] = False
    result['fingerprint'] = _hash(result['files'])
    return result


def _root(r, create=False):
    import ato_config
    root = ato_config.ato_home()/'incremental-checkpoints-v1'/_hash(str(r.dir))
    grants = [r.repo, *getattr(r, 'plan', {}).get('permissions', {}).get('writable_roots', [])]
    if any(root.resolve().is_relative_to(Path(grant).resolve()) for grant in grants):
        raise ValueError('checkpoint authority must be outside model-writable project')
    if create:
        root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.mkdir(exist_ok=False, mode=0o700)
    for path in (root.parent, root):
        info = path.stat()
        if path.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('checkpoint private directory ownership/mode changed')
    return root


def _identity(r):
    return dict(project=str(r.repo.resolve()), directory=str(r.dir), contract=r.state['plan_hash'])


def initialize(directory):
    import runner
    from types import SimpleNamespace
    directory = Path(directory).resolve()
    state = runner.read(directory/'state.json')
    r = SimpleNamespace(dir=directory, repo=Path(state['repo']), state=state, plan=runner.read(directory/'plan.json'))
    root = _root(r, create=True)
    _private_save(root/'head.json', dict(version=1, identity=_identity(r), tasks={
        key: dict(sequence=0, last=None, pending=None, checks=[]) for key in state['tasks']}))


def _head(r):
    _, raw = _regular(_root(r)/'head.json', private=True)
    value = json.loads(raw)
    if (value.get('version') != 1 or value.get('identity') != _identity(r)
            or set(value.get('tasks', {})) != set(r.state['tasks'])):
        raise ValueError('checkpoint project/task/contract changed')
    return value


def _binding(r, *, running=False):
    import runner
    r.guard()
    if (r.state.get('owner_pid') != os.getpid() or not r.state.get('owner_identity')
            or runner.process_identity(os.getpid()) != r.state['owner_identity']):
        raise ValueError('checkpoint requires exact current Runner owner')
    if not running and (r.state.get('active_process') is not None or r.process is not None):
        raise ValueError('checkpoint requires confirmed exact process cleanup')
    if running and (r.process is None or r.state.get('active_process', {}).get('pid') != r.process.pid
                    or not r.state['active_process'].get('identity')):
        raise ValueError('running checkpoint has no exact invocation process identity')
    if (r.state['repo'] != str(r.repo.resolve()) or r.dir.parent != r.repo/'.codex/tasks'
            or runner.read(r.dir/'plan.json') != r.plan):
        raise ValueError('checkpoint frozen project/plan mismatch')
    selected = runtime_selection.require_current(r.dir)
    parent = None
    if (r.dir/'parent.json').exists():
        ref = runner.read(r.dir/'parent.json')
        directory = Path(ref['workflow_dir']).resolve(strict=True)
        state = runner.read(directory/'state.json'); contract = runner.read(directory/'contract.json')
        if (directory != r.repo/'.codex/tasks/workflows'/ref['workflow_id']
                or state['id'] != ref['workflow_id'] or state['repo'] != str(r.repo)
                or state.get('active_run') != str(r.dir) or not getattr(r, 'parent_guard', None)
                or state.get('owner_pid') != os.getpid() or state.get('owner_identity') != r.state['owner_identity']
                or state['contract_hash'] != runner.digest(contract)
                or contract.get('checkpoint_policy') != r.plan.get('checkpoint_policy')
                or (directory/'STOP').exists()):
            raise ValueError('checkpoint parent project/contract/generation/STOP mismatch')
        parent = dict(directory=str(directory), contract=state['contract_hash'], generation=state.get('execution_revision', 0))
    return dict(**_identity(r), parent=parent, actor='runner-owned-checkpoint-v1',
                runtime=selected['runtime']['manifest_sha256'] if selected else _hash(runtime_selection._sources(runtime_selection.ROOT)))


def _profile(r):
    import agents
    kind = r.plan.get('agent', {}).get('kind', 'codex')
    adapter = agents.registry.get(kind, 'worker')
    profile = (adapter.worker_profile(r.plan, r.repo) if hasattr(adapter, 'worker_profile') else
               dict(kind=kind, agent=r.plan.get('agent'), model=r.plan.get('model'),
                    sandbox=r.plan.get('sandbox', 'workspace-write'), permissions=r.plan.get('permissions'), approval='never'))
    # Values (including secrets) never leave this hash. Unknown external state is
    # not certified by this fingerprint; it is still freshly checked by Runner.
    return dict(adapter=_hash(profile), environment=_hash(dict(os.environ)))


def _native(r, task):
    if 'worker_session_policy' not in r.plan:
        return dict(mode='checkpoint_reconstruction', native_identity='unavailable')
    import worker_sessions
    receipt = worker_sessions.load(r, task)
    if receipt is None:
        return None
    return dict(mode=receipt['mode'], native_id=receipt['native_id'], source=receipt['source'],
        turn_id=receipt.get('turn_id'), message_id=receipt.get('correlation_id'),
        generation=receipt.get('generation'), turns=len(receipt['turns']),
        receipt_sha256=_hash(receipt), state=receipt['state'], cleanup_confirmed=receipt['cleanup_confirmed'])


def check_inputs(r, argv):
    """Bind actual workspace, argv, executable, environment and runtime boundary.

    Arbitrary external/online effects are deliberately not declared reusable.
    These are observations of the original host check, never cache permission.
    """
    import shutil
    workspace = snapshot(r.repo)
    tools = {}
    for i, arg in enumerate(argv):
        candidate = shutil.which(arg) if i == 0 else str(r.repo/arg) if not Path(arg).is_absolute() else arg
        if not candidate or not Path(candidate).is_file():
            continue
        # Interpreters may use a legitimate selected symlink. Resolve it, then
        # pin actual bytes; project input symlinks remain rejected by snapshot.
        actual = Path(candidate).resolve(strict=True)
        info = actual.stat()
        if info.st_size > 128*1024*1024:
            return dict(complete=False)
        tools[str(actual)] = dict(sha256=hashlib.sha256(actual.read_bytes()).hexdigest(), mode=stat.S_IMODE(info.st_mode))
    return dict(complete=workspace['complete'] and bool(tools), workspace=workspace['fingerprint'],
        argv_sha256=_hash(argv), tools=tools, profile=_profile(r), contract=r.state['plan_hash'])


def record_checks(r, task, observations):
    head = _head(r); entry = head['tasks'][task['id']]
    if entry['pending'] is not None:
        raise ValueError('checkpoint atomic publication incomplete; no evidence replacement')
    entry['checks'] = observations
    _private_save(_root(r)/'head.json', head)


def check_observation(r, argv, code, log, before):
    path = Path(log)
    if not path.is_relative_to(r.dir/'evidence'):
        raise ValueError('checkpoint host evidence outside exact task')
    proof, _ = _regular(path)
    after = check_inputs(r, argv)
    return dict(argv_sha256=_hash(argv), returncode=code, evidence=str(path), proof=proof,
        inputs=after, stable_inputs=before == after and before.get('complete', False),
        observed_at=time.time(), authority='host-command-returncode')


def _results(r, task):
    entry = _head(r)['tasks'][task['id']]
    result = []
    for i, receipt in enumerate(entry['checks']):
        valid = False
        try:
            proof, _ = _regular(receipt['evidence'])
            valid = (i < len(task['checks']) and receipt['argv_sha256'] == _hash(task['checks'][i])
                     and receipt['stable_inputs'] and receipt['inputs'] == check_inputs(r, task['checks'][i])
                     and proof == receipt['proof'])
            if valid and 'verification_reuse' in receipt:
                import verification_cache
                reused = receipt['verification_reuse']
                valid = (reused['returncode'] == 0 and reused['evidence'] == receipt['evidence']
                         and reused['inputs'] == verification_cache.fingerprint(r, task, task['checks'][i])
                         and 0 <= time.time()-reused['finished_at'] <= verification_cache.MAX_AGE_SECONDS)
        except (OSError, ValueError, KeyError, TypeError, UnicodeError, SyntaxError, RuntimeError):
            pass
        result.append(dict(index=i, status=('passed' if receipt['returncode'] == 0 else 'failed') if valid else 'invalidated',
                           receipt=copy.deepcopy(receipt)))
    return result


@metrics.task_scope
@metrics.measure("checkpoint")
def publish(r, task, phase, *, output=None):
    """Reserve -> atomically write immutable record -> atomically publish head.

    A crash anywhere before the final head commit leaves an orphan/pending
    record, never a resumable terminal proof. No sequence is reused.
    """
    running = phase == 'running'
    binding = _binding(r, running=running)
    head = _head(r); entry = head['tasks'][task['id']]
    if entry['pending'] is not None:
        raise ValueError('checkpoint interrupted atomic publication; no replay')
    row = r.state['tasks'][task['id']]
    sequence = entry['sequence'] + 1
    if sequence > MAX_RECORDS:
        raise ValueError('checkpoint retained-record bound reached; no history deletion or reset')
    source = snapshot(r.repo)
    native = _native(r, task)
    results = _results(r, task)
    cleaned = r.process is None and r.state.get('active_process') is None
    outcome = accounting.ledger(row)['history'][-1]['outcome'] if accounting.ledger(row)['history'] else None
    terminal = phase == 'terminal' and cleaned and not row.get('delivery_unknown') and outcome not in (None, 'running', 'failed', 'timeout', 'cleanup_unknown')
    payload = dict(version=1, sequence=sequence, previous=entry['last'], binding=binding,
        task=task['id'], goal=r.plan['objective'], invocation=row.get('attempt_sequence', 0), phase=phase,
        resumable=terminal and source['complete'], source=source, profile=_profile(r), native=native,
        current_step=dict(task=task['id'], outcome=outcome),
        remaining=dict(tasks=[k for k, v in r.state['tasks'].items() if v['status'] != 'completed'],
                       checks=[i for i in range(len(task['checks'])) if not any(c['index'] == i and c['status'] == 'passed' for c in results)]),
        results=results, ledger=copy.deepcopy(accounting.ledger(row)),
        cleanup_confirmed=cleaned, output=None, observed_at=time.time())
    if output is not None:
        if Path(output).parent != r.dir/'checkpoints':
            raise ValueError('checkpoint output outside exact invocation')
        proof, _ = _regular(output)
        payload['output'] = dict(path=str(output), proof=proof)
    if terminal and (not payload['output'] or 'worker_session_policy' in r.plan and
            (not native or native['state'] != 'turn_completed' or not native['cleanup_confirmed'])):
        payload['resumable'] = False
    if len(json.dumps(payload).encode()) > MAX_JSON:
        raise ValueError('checkpoint payload exceeds bounded private receipt')
    filename = task['id']+'-'+str(sequence)+'.json'
    reference = dict(sequence=sequence, file=filename, sha256=_hash(payload))
    root = _root(r)
    if (root/filename).exists():
        raise ValueError('checkpoint sequence replay refused')
    entry.update(sequence=sequence, pending=reference)
    _private_save(root/'head.json', head)
    _private_save(root/filename, payload)
    entry.update(last=reference, pending=None)
    _private_save(root/'head.json', head)
    row['incremental_checkpoint'] = dict(**reference, phase=phase, resumable=payload['resumable'])
    r.persist()
    return payload


def preflight(r, task):
    """Validate receipt/state/files/results before any new invocation is spent."""
    binding = _binding(r)
    head = _head(r); entry = head['tasks'][task['id']]; row = r.state['tasks'][task['id']]
    if entry['pending'] is not None:
        raise ValueError('checkpoint interrupted atomic publication; no automatic recovery')
    reference = entry['last']
    if reference is None:
        if row.get('attempt_sequence', 0) or accounting.ledger(row)['invocations']:
            raise ValueError('checkpoint history missing; counters may not be reconstructed from summary')
        return None
    # Retain and validate the complete immutable chain, including earlier
    # running snapshots. Neither old IDs nor old failed evidence are cleared.
    cursor = reference; saved = None; size = 0; expected = entry['sequence']
    if not 1 <= expected <= MAX_RECORDS:
        raise ValueError('checkpoint retained history bound invalid')
    while cursor is not None:
        if cursor.get('sequence') != expected or cursor.get('file') != task['id']+'-'+str(expected)+'.json':
            raise ValueError('checkpoint history sequence replay')
        _, raw = _regular(_root(r)/cursor['file'], private=True)
        size += len(raw)
        if size > MAX_CHAIN_BYTES:
            raise ValueError('checkpoint retained history exceeds validation bound; no deletion')
        record = json.loads(raw)
        if (_hash(record) != cursor['sha256'] or record.get('sequence') != expected
                or record.get('binding') != binding or record.get('task') != task['id']):
            raise ValueError('checkpoint retained history digest/binding changed')
        saved = record if saved is None else saved
        cursor = record['previous']; expected -= 1
    if expected != 0:
        raise ValueError('checkpoint retained history truncated')
    if not 0 <= time.time()-saved['observed_at'] <= r.plan['checkpoint_policy'].get('max_age_seconds', 86400):
        raise ValueError('checkpoint terminal receipt expired or from the future')
    if (saved.get('sequence') != entry['sequence'] or reference['sha256'] != _hash(saved)
            or saved.get('binding') != binding or saved.get('task') != task['id']
            or saved.get('goal') != r.plan['objective'] or not saved.get('resumable')
            or saved.get('phase') != 'terminal' or not saved.get('cleanup_confirmed')
            or row.get('delivery_unknown') or saved['invocation'] != row.get('attempt_sequence')
            or row.get('incremental_checkpoint') != dict(**reference, phase='terminal', resumable=True)):
        raise ValueError('checkpoint replay/project/generation/terminal/cleanup mismatch')
    if saved['source'] != snapshot(r.repo) or saved['profile'] != _profile(r) or saved['native'] != _native(r, task):
        raise ValueError('checkpoint workspace/profile/native evidence changed')
    live = accounting.ledger(row)
    for key, value in saved['ledger'].items():
        if key in ('active_seconds', 'wait_seconds', 'yield_seconds'):
            if live[key] < value:
                raise ValueError('checkpoint cumulative accounting decreased')
        elif live.get(key) != value:
            raise ValueError('checkpoint history/counters changed; no reset or replay')
    output = saved['output']; proof, _ = _regular(output['path'])
    if proof != output['proof']:
        raise ValueError('checkpoint terminal worker output changed')
    for result in saved['results']:
        receipt = result['receipt']; proof, _ = _regular(receipt['evidence'])
        if proof != receipt['proof']:
            raise ValueError('checkpoint original host evidence changed')
        if result['status'] in ('passed', 'failed') and receipt['inputs'] != check_inputs(r, task['checks'][result['index']]):
            raise ValueError('checkpoint check inputs changed')
    return saved


def prompt(r, task, saved):
    policy = r.plan['checkpoint_policy']; seconds = r.plan['limits']['attempt_seconds']
    text = (f'\nCheckpoint policy v1: this invocation has a hard {seconds}s protection. '
            f'Close work and return valid structured continue by {seconds-policy["closing_margin_seconds"]}s '
            f'after invocation start, reserving {policy["closing_margin_seconds"]}s for bounded closing. '
            'Save actual files frequently. The host records bounded fingerprints while you work. '
            'No checkpoint.request/steer capability is qualified; no live input or extra wait will be sent.\n')
    if saved:
        context = dict(goal=saved['goal'], invocation=saved['invocation'], current_step=saved['current_step'],
            remaining=saved['remaining'], files=saved['source']['files'],
            checks=[dict(index=c['index'], status=c['status']) for c in saved['results']], native=saved['native'],
            counters={k: saved['ledger'][k] for k in accounting.COUNTERS}, history=saved['ledger']['history'])
        encoded = json.dumps(context, sort_keys=True)
        # No truncation that could silently omit the goal or remaining work.
        if len(encoded) > 128*1024:
            raise ValueError('checkpoint reconstruction context exceeds bound')
        text += 'Host-validated checkpoint (evidence, no additional authority):\n'+encoded+'\n'
    return text


def poll(r, task):
    """Called only by Runner's existing command loop; never sends native input."""
    monitor = r._checkpoint_monitor
    elapsed = time.monotonic()-monitor['started']
    if not monitor['closing'] and elapsed >= monitor['close_after']:
        monitor['closing'] = True
        r.event('checkpoint_closing_margin', task=task['id'], invocation=monitor['invocation'],
                delivery='prompt_only', request_sent=False)
    if elapsed < monitor['next'] or monitor['published'] >= MAX_PERIODIC:
        return
    monitor['next'] = elapsed+r.plan['checkpoint_policy']['interval_seconds']
    source = snapshot(r.repo)
    if source['fingerprint'] != monitor['fingerprint']:
        publish(r, task, 'running')
        monitor['published'] += 1; monitor['fingerprint'] = source['fingerprint']
