#!/usr/bin/env python3
"""Origin readback: no model, no auth reads, no admission, no task mutation.

Executes only original independent command checks for the fresh reviewed demo.
It never installs the additive entry or fabricates external origin-delivery.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import acpx_profile
import external_coordinator as external
import runner
from scripts.prepare_external_candidate import check


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def readback(directory):
    from scripts.external_host_smoke import environment
    check(ROOT)
    directory=Path(directory).resolve();receipt=runner.read(directory/'smoke.json')
    if (receipt['directory']!=str(directory) or receipt['runtime']!=str(ROOT) or
            receipt['manifest_sha256']!=sha(ROOT/'build/external-coordinator/candidate.json')):
        raise ValueError('readback must bind exact prepared candidate and demo')
    env=environment(directory,Path(receipt['dependency_config']))
    os.environ.clear();os.environ.update(env)
    paths=acpx_profile.operator_paths();acpx_profile.dependencies(paths)
    native_hash=sha(paths['deps']/'@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex')
    if native_hash!=acpx_profile.NATIVE_SHA256:raise ValueError('native binary drift')
    workflow,state,scope=external.authorized(receipt['actor_file'],receipt['workflow'])
    if Path(scope['repo'])!=directory/'repo':raise ValueError('foreign demo scope')
    current=external.result(workflow)
    command_checks=[]
    for criterion in scope['contract']['acceptance']:
        for index,argv in enumerate(criterion.get('checks',[])):
            result=subprocess.run(argv,cwd=scope['repo'],env=env,capture_output=True,
                                  timeout=scope['contract']['limits']['check_seconds'])
            # Demo output should be empty; persist a hash/exit, never raw content.
            command_checks.append(dict(criterion=criterion['id'],index=index,exit=result.returncode,
                stdout_sha256=hashlib.sha256(result.stdout).hexdigest(),stderr_sha256=hashlib.sha256(result.stderr).hexdigest()))
    transports=[];active=[];attempts=[];transport_groups=set()
    evidence=[directory/'smoke.json',directory/'contract.json',workflow/'contract.json',workflow/'state.json']
    for stage in state['stages']:
        child=Path(stage['run_dir']);row=runner.read(child/'state.json');evidence.append(child/'state.json')
        if row.get('owner_pid') or row.get('active_process'):active.append(str(child))
        for task in row['tasks'].values():attempts.append(task.get('attempt_sequence',0))
        for path in (child/'checkpoints').glob('*.acpx-runtime/receipt.json'):
            value=runner.read(path);evidence.append(path)
            if type(value.get('process_group')) is int:transport_groups.add(value['process_group'])
            transports.append(dict(correlation=value['correlation'],session=value.get('session'),
                delivery=value['delivery'],transport=value['transport'],protocol=value['protocol'],
                schema_valid=value['result_schema_valid'],native_identity_representation='sha256 prefix, not raw native id'))
        for path in (child/'evidence').glob('*.log'):evidence.append(path)
    # Inspect PID/group numbers only, never unrelated processes' command lines.
    ps=subprocess.run(['/bin/ps','-axo','pid=,pgid='],check=True,capture_output=True,text=True,timeout=5)
    groups=set(receipt['observed_groups'])|transport_groups;remaining=[]
    for line in ps.stdout.splitlines():
        pid,group=map(int,line.split())
        if group in groups:remaining.append(pid)
    accepted=(current['accepted'] and bool(command_checks) and all(c['exit']==0 for c in command_checks)
        and len(transports)==1 and transports[0]['delivery']=='finished'
        and transports[0]['transport']=='response' and transports[0]['protocol']=='completed'
        and transports[0]['schema_valid'] is True and attempts==[1] and not active and not remaining)
    proof=dict(version=1,manifest_sha256=receipt['manifest_sha256'],workflow=str(workflow),
        ato_checks_passed=accepted,canonical_result=current,independent_readback_checks=command_checks,
        transport_receipts=transports,attempt_sequences=attempts,
        real_codex_acp_verified=accepted,admissions_including_prior=1+receipt['codex_admissions_by_script'],
        active_owned_processes_after_cleanup=len(remaining),active_owned_pids=remaining,
        native_versions=dict(node='24.22.1',acpx='0.22.0',codex_acp='1.1.5',codex='0.144.6'),
        native_binary_sha256=native_hash,
        additive_entry_verified=False,overall_delivery_complete=False,
        evidence_files=[dict(path=str(p),sha256=sha(p)) for p in evidence])
    runner.save(directory/'readback.json',proof)
    return proof


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--directory',required=True)
    args=parser.parse_args()
    try:
        result=readback(Path(args.directory));print(json.dumps(result,ensure_ascii=False))
        sys.exit(0 if result['ato_checks_passed'] else 1)
    except Exception as error:
        print(json.dumps(dict(error=type(error).__name__,detail='readback failed closed; inspect original evidence, never retry model automatically')),file=sys.stderr)
        sys.exit(1)
