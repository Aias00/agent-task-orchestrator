"""Origin-only bounded native comparison. Importing this module never invokes a CLI.

The engine also has offline subprocess tests; those never emit an actual proof.
No authentication file is opened or copied. Native config remains native. Only
exact newly returned task IDs are read; no transcript history search is used.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import tomllib
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agents
import codex_worker_session as native
import execution_metrics
import runner
import profile_evidence
import worker_sessions
import workflow
from transport import _private_save
from scripts.benchmark_execution_efficiency_fixture import SEED

ARMS = ('foreground', 'legacy_ato', 'warm_ato')
OBJECTIVE = '''Perform exactly ONE increment per invocation in artifact.txt, using file tools.
Read guide.txt and artifact.txt. If artifact.txt is empty, write exactly first\\n
and return status=continue immediately, without writing the second line. If it
contains exactly first\\n, append exactly second\\n and return status=complete.
Never do both increments in one invocation. Preserve guide.txt and check.py.
Do not run checks, alter plans, launch agents/services, install, commit, access
credentials/config/history, use network, or touch other projects. Final
acceptance is exclusively the host check.py after both bounded turns.'''
EXECUTION = dict(version=2, max_continuations=2, max_waits=1, max_yields=1, max_no_progress=2)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(value):
    return sha(json.dumps(value, sort_keys=True, separators=(',', ':')).encode())


def require(value, reason):
    if not value:
        raise ValueError(reason)


def profile_fingerprint(value):
    # Project paths/native IDs intentionally differ; actual native model,
    # provider/policy, binary, config contents and environment must not.
    return fingerprint({k: value[k] for k in ('adapter', 'binary_sha256',
        'environment_hash', 'model', 'sandbox', 'approval', 'permissions', 'source_manifest_sha256')} |
        dict(config_hashes=sorted(value['configs'].values(), key=lambda x:x or '')))


@contextmanager
def isolated_ato(folder, binary):
    before = dict(os.environ)
    try:
        os.environ['ATO_HOME'] = str(folder/'ato-private')
        os.environ['CTASK_CODEX_BIN'] = str(binary)
        os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
        # Retain existing native home/auth/model environment. Remove only ATO
        # live-chat locators, so temporary workflows cannot bind the live origin.
        for key in ('CODEX_THREAD_ID', 'CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
            os.environ.pop(key, None)
        yield
    finally:
        os.environ.clear(); os.environ.update(before)


def protocol_log(log):
    """Legacy combines diagnostics with stdout; retain raw and derive JSONL.

    No JSON error/failed event is suppressed. Exact native append readback and
    returncode remain mandatory. Unknown/malformed protocol cannot qualify.
    """
    data = Path(log).read_bytes()
    require(len(data) <= native.MAX_BYTES, 'native output exceeds evidence bound')
    rows = []
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue  # retained in original raw evidence, never a success signal
        if isinstance(row, dict) and isinstance(row.get('type'), str):
            rows.append(row)
    path = Path(log).with_suffix('.protocol.jsonl')
    path.write_text(''.join(json.dumps(row)+'\n' for row in rows))
    return path, rows


def observed_tool(rows):
    return any(row.get('type') == 'item.completed'
        and row.get('item', {}).get('type') in ('command_execution', 'file_change')
        and row['item'].get('status') == 'completed'
        and row['item'].get('exit_code', 0) == 0 for row in rows)


def verify_check_inputs(repo, seed_manifest):
    # The native worker cannot replace the code the independent host executes.
    # Check this before every host launch, including Runner's internal checks.
    for name in ('guide.txt', 'check.py'):
        data, info = native._file(repo/name)
        require(sha(data) == seed_manifest[name]['sha256']
                and info.st_mode & 0o777 == seed_manifest[name]['mode'],
                'independent check/guide content or mode changed; refuse host execution')


def sample(folder, arm, index, model, deadline, *, require_native_tools=True):
    """Exactly two turns in one fresh temporary Git repo; never retries.

    Called by offline tests only with their explicitly synthetic environment.
    Those results cannot pass run_origin's trusted-binary gate or proof kind.
    """
    started = time.monotonic()
    repo = folder/(arm+'-'+str(index)); repo.mkdir(mode=0o700)
    import subprocess
    subprocess.run(['/usr/bin/git', 'init', '-q', str(repo)], check=True, timeout=5)
    for name, content in SEED.items():
        (repo/name).write_text(content)
    (repo/'.gitignore').write_text('.codex/\n')
    seed_manifest = {k: dict(sha256=sha((repo/k).read_bytes()), mode=(repo/k).stat().st_mode & 0o777) for k in SEED}
    initial = fingerprint(seed_manifest)
    task = dict(id='artifact', prompt=OBJECTIVE, depends_on=[], checks=[[sys.executable, 'check.py']])
    common = dict(objective=OBJECTIVE, memory=False, model=model, sandbox='workspace-write',
        permissions=dict(source='Origin bounded temporary file comparison; unchanged grant in all arms',
                         network_access=False, writable_roots=[]),
        execution_policy=copy.deepcopy(EXECUTION),
        limits=dict(runtime_seconds=1200, attempt_seconds=85, check_seconds=5, max_attempts=1))
    if arm == 'warm_ato':
        common['worker_session_policy'] = dict(version=1, mode='task_bound_native')
        common['checkpoint_policy'] = dict(version=1, closing_margin_seconds=15, interval_seconds=30)
        common['verification_policy'] = dict(version=1, checks=dict(artifact=dict(inputs=list(SEED), env=[], cacheable=True)))
    contract = dict(common, scope='This one temporary Git repo and these two file increments only',
        authorization='Origin-only <=12 native turns/1200sec; no replay, live state, auth/config mutation or production effects',
        acceptance=[dict(id='artifact', description='Exact two-line artifact and unchanged guide', checks=task['checks'])])
    runner.save(repo/'contract-input.json', contract)
    w = workflow.Workflow(workflow.create(repo, repo/'contract-input.json', pin_runtime=True))
    identity = runner.process_identity(os.getpid())
    require(identity, 'exact host owner identity unavailable; no native turn started')
    w.state.update(owner_pid=os.getpid(), owner_identity=identity, status='running')
    w.deadline = time.time()+max(0, deadline-time.monotonic()); w.persist()
    w.stage(dict(objective=OBJECTIVE, tasks=[copy.deepcopy(task)]), ['artifact'])
    r = runner.Runner(w.state['active_run']); r.parent_guard = w.guard
    r.state.update(owner_pid=os.getpid(), owner_identity=identity, status='running'); r.persist()
    r.deadline = w.deadline
    def protected_command(command):
        def launch(argv, name, timeout, input_text=None, agent_kind=None, **meter):
            if agent_kind is None:
                require(argv == task['checks'][0], 'unknown independent checker argv')
                verify_check_inputs(repo, seed_manifest)
            return command(argv, name, timeout, input_text, agent_kind=agent_kind, **meter)
        return launch
    r.command = protected_command(r.command)
    w.command = protected_command(w.command)
    cfg = native.profile(r.plan, repo); profile = profile_fingerprint(cfg)
    direct = dict(profile=cfg, native_id=None, source='ctask-benchmark/'+uuid.uuid4().hex, turns=[])
    turns = []
    original_command = r.worker_command_once
    legacy_receipts = []
    def legacy_command(current_task, argv, label, output, prompt, **meter):
        # A provenance tag and correlation do not enable warm reuse. The old
        # Runner adapter, result handling and checks still run for both turns.
        cold = dict(profile=cfg, native_id=None, source='ctask-benchmark/'+uuid.uuid4().hex, turns=[])
        correlation = 'benchmark-'+uuid.uuid4().hex
        argv = [cfg['binary'], *argv[1:-1], '--thread-source', cold['source'], '-']
        prompt = 'ATO task-bound message '+correlation+'\n'+prompt
        code, log = r.command(argv, label, 85, prompt, agent_kind='codex', **meter)
        require(code == 0, 'legacy native failure/unknown; do not replay')
        protocol, rows = protocol_log(log)
        receipt = native.receipt(cold, protocol, repo, correlation, len(legacy_receipts)+1)
        legacy_receipts.append((receipt, correlation, argv, log, rows))
        return code, log
    if arm == 'legacy_ato':
        r.worker_command_once = legacy_command
    for number in (1, 2):
        require(deadline-time.monotonic() > 90, 'insufficient remaining bounded host window; no further native turn')
        evidence_path = folder/'profile-evidence'/(arm+'-'+str(index)+'-turn-'+str(number)+'.json')
        def publish(observation):
            _private_save(evidence_path, observation)
        with profile_evidence.Turn(cfg, lambda: native.profile(r.plan, repo), publish, number):
            if arm == 'foreground':
                if direct['native_id']:
                    native.verify(direct, repo)
                correlation = 'benchmark-'+uuid.uuid4().hex
                output = r.dir/'checkpoints'/('direct-'+str(number)+'.json')
                argv = native.command(r.plan, r.dir/'result-schema.json', output, direct)
                code, log = r.command(argv, 'direct-'+str(number), 85,
                    'ATO task-bound message '+correlation+'\n'+OBJECTIVE, agent_kind='codex')
                require(code == 0, 'direct native failure/unknown; do not replay')
                protocol, rows = protocol_log(log)
                receipt = native.receipt(direct, protocol, repo, correlation, number)
                direct.update(receipt); direct['turns'].append(dict(turn_id=receipt['turn_id']))
                outcome = agents.validate_result(runner.read(output), runner.SCHEMA_V2)['status']
                r.checks(task, 'direct-turn-'+str(number))
            else:
                r.attempt(task, '')
                row = r.state['tasks']['artifact']
                require(not row.get('delivery_unknown') and not row.get('session_refused') and not row.get('checkpoint_refused'),
                        'Runner refused native identity/profile/checkpoint or delivery unknown; no replay')
                require(row['status'] == ('pending' if number == 1 else 'completed'), 'Runner did not verify the expected increment')
                if arm == 'warm_ato':
                    saved = worker_sessions.load(r, task)
                    require(saved and len(saved['turns']) == number, 'exact warm turn not durably receipted')
                    receipt = saved
                    correlation = saved['turns'][-1]['message_id']
                    log = row['worker_log']; protocol, rows = protocol_log(log)
                    events = [json.loads(line) for line in (r.dir/'events.jsonl').read_text().splitlines()]
                    argv = next(e['argv'] for e in reversed(events) if e['event'] == 'command_started' and e['evidence'] == log)
                    outcome = saved['turns'][-1]['outcome']
                else:
                    receipt, correlation, argv, log, rows = legacy_receipts[-1]
                    outcome = 'continue' if row['status'] == 'pending' else 'complete'
            require(outcome == ('continue' if number == 1 else 'complete'), 'wrong bounded native result')
            require((repo/'artifact.txt').read_text() == ('first\n' if number == 1 else 'first\nsecond\n'), 'native tool artifact wrong')
            require(not require_native_tools or observed_tool(rows), 'no successful native file/tool operation receipt')
            require(r.process is None and r.state.get('active_process') is None, 'exact process cleanup unconfirmed')
            turns.append(dict(native_id=receipt['native_id'], turn_id=receipt['turn_id'], message_id=correlation,
                cleanup_confirmed=True, delivery='delivered', model=model, profile_fingerprint=profile,
                argv_sha256=fingerprint(argv), evidence_sha256=sha(Path(log).read_bytes()),
                evidence=str(Path(log)), native_context=receipt['effective_context'],
                tool_operation_observed=observed_tool(rows),
                profile_evidence=str(evidence_path)))
    r.worker_command_once = original_command
    # Final workflow criteria execute independently and fresh in all three arms.
    for label in ('unchanged-probe-a', 'unchanged-probe-b'):
        require(r.checks(task, label)[0], 'host artifact check failed')
    w.check_total(fresh=True)
    require(w.state['criteria']['artifact']['status'] == 'passed', 'fresh full workflow acceptance failed')
    check = Path(w.state['criteria']['artifact']['evidence'][0])
    verify_check_inputs(repo, seed_manifest)
    timing = execution_metrics.view(w.dir, children=[r.dir])
    require(timing['worker_process_startups'] == 2, 'native startup count differs from observed host launches')
    require(timing['check_executions'] == int((repo/'check-count').read_text()),
            'independent checker execution counts disagree')
    # Check scripts count executions themselves and agree with host launch spans.
    counts = dict(native_turns=2, cli_process_startups=2, native_sessions=len({t['native_id'] for t in turns}),
        cold_initializations=len({t['native_id'] for t in turns}),
        native_context_resumes=1 if arm != 'legacy_ato' else 0,
        check_executions=timing['check_executions'], check_reuses=timing['check_reuses'],
        model_requests='unknown', context_read_bytes='unknown', tool_read_count='unknown')
    return dict(turns=turns, artifact_sha256=sha((repo/'artifact.txt').read_bytes()), initial_files_sha256=initial,
        contract_sha256=fingerprint(dict(objective=OBJECTIVE, seed=SEED, model=model, sandbox='workspace-write',
            approval='never', network_access=False, writable_roots=[], checks=task['checks'])),
        model=model, profile_fingerprint=profile, counts=counts, wall_seconds=time.monotonic()-started,
        provider_latency='unknown', independent_check=dict(returncode=0, fresh=True, actor='host',
            evidence=str(check), evidence_sha256=sha(check.read_bytes())),
        workflow=str(w.dir), stage=str(r.dir), active_seconds=timing['covered_seconds'],
        wait_seconds=0, active_basis='observed exclusive span union; setup gaps included only in total wall',
        phase_timing=timing)


def selected_model():
    # Read only configuration (never auth); persist only model name and hashes.
    home = native.native.home()
    config = home/'config.toml'
    data = tomllib.loads(native._file(config)[0].decode()) if config.exists() else {}
    profile = data.get('profile')
    require(not os.environ.get('CODEX_PROFILE') and not os.environ.get('CODEX_CONFIG_PROFILE'),
            'environment-selected native profile is not qualified by this executor')
    selected = data.get('profiles', {}).get(profile, {}) if profile else {}
    model = selected.get('model', data.get('model'))
    require(isinstance(model, str) and model.strip() and not model.startswith('-'),
            'current configured native model unknown; origin must inspect, no model chosen automatically')
    return model


def run_origin(output, max_turns):
    from scripts import prepare_efficiency_candidate as bundle
    from scripts.verify_execution_efficiency_local import validate_proof
    require(max_turns == 12, 'exact origin turn bound required')
    # Everything above this boundary is inert until the explicit origin command.
    require(not os.environ.get('CTASK_CODEX_BIN'), 'native binary overrides forbidden for actual qualification')
    manifest = bundle.check()
    manifest_sha = sha((ROOT/bundle.MANIFEST).read_bytes())
    launcher = shutil.which('codex')
    require(launcher and sha(Path(launcher).read_bytes()) == native.TRUSTED_QUEUE_GATE,
            'actual sample requires the frozen installed native launcher')
    binary, binary_sha, provenance = native._binary(launcher)
    model = selected_model()
    evidence_root = ROOT/'build/execution-efficiency/host-evidence'/manifest_sha
    require(not evidence_root.exists(), 'candidate already claimed/sampled; preserve evidence and obtain origin review, never replay')
    evidence_root.parent.mkdir(parents=True, exist_ok=True)
    evidence_root.mkdir(mode=0o700)  # exclusive per-candidate claim survives all failures
    journal = dict(version=1, status='claimed', candidate_manifest_sha256=manifest_sha,
        max_native_turns=12, max_wall_seconds=1200, model=model, binary_sha256=binary_sha,
        launcher=provenance, arms={}, automatic_resample=False, evidence_root=str(evidence_root))
    _private_save(evidence_root/'journal.json', journal)
    started = time.monotonic(); deadline = started+1170  # leave time for proof retention before external 1200s kill
    try:
        # Kept after success/failure for host audit, outside the source archive.
        folder = Path(tempfile.mkdtemp(prefix='temporary-git-samples-', dir=evidence_root)).resolve()
        with isolated_ato(folder, binary):
            for arm in ARMS:
                journal['arms'][arm] = dict(samples=[])
                for index in (1, 2):
                    journal.update(active_arm=arm, active_sample=index, status='delivery_claimed')
                    _private_save(evidence_root/'journal.json', journal)
                    result = sample(folder, arm, index, model, deadline)
                    journal['arms'][arm]['samples'].append(result)
                    journal.update(status='sample_verified', native_turn_count=sum(len(s['turns']) for a in journal['arms'].values() for s in a['samples']))
                    _private_save(evidence_root/'journal.json', journal)
        samples = [s for arm in ARMS for s in journal['arms'][arm]['samples']]
        require(len({s['profile_fingerprint'] for s in samples}) == 1, 'cross-arm native profile drift')
        contexts = [{k: v for k, v in turn['native_context'].items() if k != 'cwd'}
                    for s in samples for turn in s['turns']]
        require(len({fingerprint(context) for context in contexts}) == 1,
                'cross-arm effective native provider/model/policy drift')
        # The candidate must remain exactly the one reviewed before any call.
        bundle.check()
        require(sha((ROOT/bundle.MANIFEST).read_bytes()) == manifest_sha, 'sample candidate drift')
        evidence = []
        for s in samples:
            for turn in s['turns']:
                path = Path(turn['profile_evidence'])
                evidence.append(dict(path=str(path), sha256=sha(path.read_bytes())))
            for record in [*s['turns'], s['independent_check']]:
                path = Path(record['evidence'])
                evidence.append(dict(path=str(path), sha256=sha(path.read_bytes())))
        proof = dict(version=1, kind='actual_codex_worker_resume_benchmark', fixture_only=False,
            candidate_manifest_sha256=manifest_sha, codex_binary_sha256=binary_sha, model=model,
            profile_fingerprint=samples[0]['profile_fingerprint'], initial_files_sha256=samples[0]['initial_files_sha256'],
            contract_sha256=samples[0]['contract_sha256'], arms=journal['arms'], evidence=evidence,
            native_turn_count=12, wall_seconds=time.monotonic()-started, production_side_effects=False,
            auth_read_or_copied=False, native_config_mutated=False, automatic_resample=False,
            exact_native_identity_verified=True, same_profile_model_permissions=True,
            two_turns_same_session=True, tool_artifact_verified=True, independent_checks_passed=True,
            authority_preserved=True, warm_resume_not_replayed=True, all_three_arms_verified=True,
            workflow_acceptance_independent=True, provider_speedup='unknown',
            limits='Two small examples per arm; native process wall is not model latency; no latency speedup required.')
        validate_proof(proof)
        _private_save(evidence_root/'proof.json', proof)
        _private_save(Path(output), proof)
        journal.update(status='qualified', proof=str(evidence_root/'proof.json'))
        _private_save(evidence_root/'journal.json', journal)
        return 0
    except BaseException as error:
        journal.update(status='failed_or_unknown_no_replay', error_type=type(error).__name__, reason=str(error)[:1000],
            wall_seconds=time.monotonic()-started)
        _private_save(evidence_root/'journal.json', journal)
        _private_save(Path(output), dict(kind='actual_cli_qualification_not_complete', status='needs_host',
            candidate_manifest_sha256=manifest_sha, retained_evidence=str(evidence_root), automatic_resample=False))
        raise
