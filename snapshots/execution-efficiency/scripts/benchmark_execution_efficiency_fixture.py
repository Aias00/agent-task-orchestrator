#!/usr/bin/env python3
"""Bounded foreground three-arm experiment using only a synthetic CLI.

Runs actual adapter/Runner/check subprocesses in disposable Git repositories.
No sockets, detached jobs, production config, native CLI or model provider.
The same deterministic two-turn file task and final fresh verifier run in every
arm. Counts describe this scripted protocol only, never real provider speed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agents
import codex_worker_session as native
import execution_metrics as metrics
import runner
import worker_sessions

FIXTURE = ROOT/'tests/fixtures/execution_efficiency_cli.py'
ARMS = ('foreground', 'legacy_ato', 'warm_ato')
OBJECTIVE = 'Create first and second lines in artifact.txt in two bounded increments; preserve guide.txt.'
CHECK = """from pathlib import Path
p=Path('check-count')
p.write_text(str(int(p.read_text())+1 if p.exists() else 1))
assert Path('artifact.txt').read_text()=='first\\nsecond\\n'
assert Path('guide.txt').read_text()=='Same authorized two-turn file task.\\n'
print('fresh independent artifact check passed')
"""
SEED = {'artifact.txt': '', 'guide.txt': 'Same authorized two-turn file task.\n', 'check.py': CHECK}
EXECUTION = dict(version=2, max_continuations=4, max_waits=2, max_yields=2, max_no_progress=3)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def arm(folder, name):
    repo = folder/name
    repo.mkdir()
    subprocess.run(['/usr/bin/git', 'init', '-q', str(repo)], check=True, timeout=5)
    for key, data in SEED.items():
        (repo/key).write_text(data)
    initial = {k: dict(sha256=sha((repo/k).read_bytes()), mode=(repo/k).stat().st_mode & 0o777) for k in SEED}
    env = dict(os.environ, ATO_HOME=str(folder/'ato-private'), CODEX_HOME=str(repo/'fixture-native'),
        CTASK_CODEX_BIN=str(FIXTURE), PYTHONDONTWRITEBYTECODE='1')
    for key in ('CODEX_THREAD_ID', 'CTASK_SESSION_REF', 'CTASK_SESSION_TOKEN', 'CTASK_ENDPOINT_TOKEN'):
        env.pop(key, None)
    task = dict(id='artifact', prompt=OBJECTIVE, depends_on=[], checks=[[sys.executable, 'check.py']])
    plan = dict(objective=OBJECTIVE, memory=False, model='fixture-model-v1', sandbox='workspace-write',
        permissions=dict(source='bounded offline fixture authorization', network_access=False, writable_roots=[]),
        execution_policy=EXECUTION, tasks=[task], limits=dict(attempt_seconds=6, check_seconds=4, max_attempts=2))
    if name == 'warm_ato':
        plan['worker_session_policy'] = dict(version=1, mode='task_bound_native')
        plan['verification_policy'] = dict(version=1, checks=dict(artifact=dict(
            inputs=list(SEED), env=[], cacheable=True)))
    with patch.dict(os.environ, env, clear=True), patch.object(runner, 'process_identity', side_effect=lambda pid:'fixture-kernel-'+str(pid)):
        runner.save(repo/'plan-input.json', plan)
        r = runner.Runner(runner.create(repo, repo/'plan-input.json'))
        r.state.update(owner_pid=os.getpid(), owner_identity=runner.process_identity(os.getpid()), status='running')
        r.persist()
        started = time.monotonic()
        direct = dict(profile=native.profile(r.plan, repo), source='fixture-foreground', native_id=None, turns=[])
        verified = []
        if name == 'foreground':
            r.checks(task, 'direct-preflight')
        for number in (1, 2):
            if name == 'foreground':
                correlation = 'foreground-'+str(number)
                label = 'direct-'+str(number)
                output = r.dir/'checkpoints'/(label+'.json')
                argv = native.command(r.plan, r.dir/'result-schema.json', output, direct)
                code, log = r.command(argv, label, 6, 'ATO task-bound message '+correlation+'\n'+OBJECTIVE, agent_kind='codex')
                if code:
                    raise ValueError('synthetic direct adapter failed')
                receipt = native.receipt(direct, log, repo, correlation, number)
                direct.update(receipt)
                direct['turns'].append(dict(turn_id=receipt['turn_id']))
                result = agents.validate_result(runner.read(output), runner.SCHEMA_V2)
                if result['status'] != ('continue' if number == 1 else 'complete'):
                    raise ValueError('wrong direct fixture result')
                r.checks(task, 'direct-turn-'+str(number))
                verified.append(dict(native_id=receipt['native_id'], turn_id=receipt['turn_id']))
            else:
                r.attempt(task, '')
                row = r.state['tasks']['artifact']
                if row.get('delivery_unknown') or row['status'] != ('pending' if number == 1 else 'completed'):
                    raise ValueError('synthetic Runner did not complete exact two turns: '+str(row))
                if name == 'legacy_ato':
                    # Legacy production mode has no native worker proof. The
                    # controlled fixture still independently validates each
                    # exact new synthetic identity/message/turn/profile here.
                    value = dict(profile=direct['profile'], source='fixture-legacy', native_id=None, turns=[])
                    log = Path(row['worker_log'])
                    prompt = (r.dir/'checkpoints'/(log.stem+'.input.txt')).read_text()
                    proof = native.receipt(value, log, repo, prompt, number)
                    verified.append(dict(native_id=proof['native_id'], turn_id=proof['turn_id']))
        if name == 'warm_ato':
            saved = worker_sessions.load(r, task)
            verified = [{k: t[k] for k in ('native_id', 'turn_id', 'message_id', 'sequence')} for t in saved['turns']]
        # Two unchanged intermediate verification requests, then a final fresh
        # full check in every arm. Reuse is an optimization, never final proof.
        for label in ('unchanged-probe-a', 'unchanged-probe-b', 'final-independent'):
            ok, logs = r.checks(task, label)
            if not ok:
                raise ValueError('same final artifact check failed')
        wall = time.monotonic()-started
        observations = [json.loads(line) for line in (repo/'fixture-receipts.jsonl').read_text().splitlines()]
        trace = metrics.view(r.dir)
        events = [json.loads(line) for line in (r.dir/'events.jsonl').read_text().splitlines()]
        reused = [e['receipt'] for e in events if e['event'] == 'verification_reused']
        counts = dict(native_turns=len(observations), model_requests=sum(o['model_requests'] for o in observations),
            native_sessions=len({o['native_id'] for o in observations}), cli_process_startups=len(observations),
            cold_initializations=sum(not o['resumed'] for o in observations),
            native_context_resumes=sum(o['resumed'] for o in observations),
            context_bytes=sum(o['context_bytes'] for o in observations),
            tool_read_count=sum(len(o['tool_reads']) for o in observations),
            tool_read_bytes=sum(read['bytes'] for o in observations for read in o['tool_reads']),
            check_executions=int((repo/'check-count').read_text()), check_reuses=len(reused))
        if counts['cli_process_startups'] != trace['worker_process_startups']:
            raise ValueError('trace and actual CLI startup counter disagree')
        if counts['check_executions'] != trace['check_executions'] or counts['check_reuses'] != trace['check_reuses']:
            raise ValueError('trace and actual independent check counter disagree')
        return dict(initial_files=initial, task_sha256=sha(OBJECTIVE.encode()),
            checks=task['checks'], profile=dict(model='fixture-model-v1', protocol='scripted-offline-v1',
                scripted_delay_seconds=.01, approval='never', sandbox='workspace-write', writable_roots=[], network_access=False),
            counts=counts, wall_seconds=wall, active_seconds=trace['covered_seconds'], active_basis='exclusive measured span union; no external wait in fixture',
            v2_command_seconds=r.state.get('execution_accounting', {}).get('active_seconds') if name != 'foreground' else None,
            wait_seconds=0, provider_latency='unknown', provider_speedup='unknown', timing=trace,
            artifacts={k: sha((repo/k).read_bytes()) for k in ('artifact.txt', 'guide.txt')},
            independent_checks_passed=True, final_check_fresh=True, exact_protocol_turns=verified,
            command_evidence=[dict(log=Path(e['evidence']).name, returncode=e['code'],
                sha256=sha(Path(e['evidence']).read_bytes()), bytes=Path(e['evidence']).stat().st_size)
                for e in events if e['event'] == 'command_finished'],
            observations=observations, reuse_provenance=[dict(input_sha256=x['inputs']['sha256'], source_sha256=x['inputs']['source_sha256'],
                returncode=x['returncode'], started_at=x['started_at'], finished_at=x['finished_at'],
                log_sha256=sha(Path(x['evidence']).read_bytes())) for x in reused],
            fixture_only=True, production_side_effects=False)


def benchmark():
    with tempfile.TemporaryDirectory(prefix='ato-efficiency-three-arm-') as temporary:
        folder = Path(temporary).resolve()
        arms = {name: arm(folder, name) for name in ARMS}
    if len({json.dumps(a['initial_files'], sort_keys=True) for a in arms.values()}) != 1:
        raise ValueError('different initial files/modes')
    for field in ('task_sha256', 'checks', 'profile', 'artifacts'):
        if len({json.dumps(a[field], sort_keys=True) for a in arms.values()}) != 1:
            raise ValueError('uncontrolled benchmark contract: '+field)
    if any(a['counts']['native_turns'] != 2 for a in arms.values()):
        raise ValueError('need two turns per arm')
    cold, warm = arms['legacy_ato']['counts'], arms['warm_ato']['counts']
    if not (warm['cold_initializations'] < cold['cold_initializations'] and warm['check_executions'] < cold['check_executions']):
        raise ValueError('no measured avoidable work reduction')
    return dict(version=1, kind='fixture_execution_efficiency_benchmark', arms=arms,
        fixture_only=True, actual_native_cli=False, provider_performance='unknown',
        fixture_binary_sha256=sha(FIXTURE.read_bytes()),
        implementation_sha256={p: sha((ROOT/p).read_bytes()) for p in ('runner.py', 'worker_sessions.py', 'execution_metrics.py', 'verification_cache.py', 'task_trace.py', 'task_presentation.py', 'scripts/benchmark_execution_efficiency_fixture.py')},
        same_contract_verified=True, artifacts_equal=True, measured_counts_only=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output', required=True)
    args = p.parse_args()
    result = benchmark()
    Path(args.output).write_text(json.dumps(result, indent=2, sort_keys=True)+'\n')
    print(json.dumps(dict(kind=result['kind'], fixture_only=True,
        counts={k: v['counts'] for k, v in result['arms'].items()}, provider_performance='unknown')))

if __name__ == '__main__':
    main()
