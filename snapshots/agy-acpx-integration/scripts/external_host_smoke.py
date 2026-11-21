#!/usr/bin/env python3
"""ORIGIN ONLY: prepare a fresh demo, then explicitly admit ONE Codex ACP turn.

Never run automatically from a worker/test/installer. No login or package fetch.
The task already consumed one incidental admission; <=1 Codex +1 optional AGY
remain. This script never admits AGY or retries a Codex turn.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import acpx_profile
import external_coordinator as external
import external_operator
import runner
from scripts.prepare_external_candidate import check


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def remember_group(groups, pid, identity):
    """Persist observed groups, but never adopt an unverifiable/reused leader."""
    if type(pid) is not int or pid <= 0:
        return
    groups.setdefault(pid, None)
    if groups[pid] and groups[pid] != identity:
        return
    if not identity or runner.process_identity(pid) != identity:
        return
    try:
        if os.getpgid(pid) == pid:
            groups[pid] = identity
    except ProcessLookupError:
        pass


def signal_owned_groups(groups, signum):
    # Recheck birth and group leadership before EACH signal. A missing leader
    # can leave uncertain descendants; readback reports those, never kills by
    # historical numeric PGID alone. No helper silently clears that evidence.
    for pid, identity in groups.items():
        if not identity or runner.process_identity(pid) != identity:
            continue
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signum)
        except ProcessLookupError:
            pass


def environment(directory,config):
    # No ambient native thread, actor, model, account or plugin environment.
    return dict(PATH='/usr/bin:/bin',HOME=str(directory/'home'),
        ATO_HOME=str(directory/'ato'),CODEX_HOME=str(directory/'native-locator'),
        ATO_ACPX_CONFIG=str(config),CTASK_CODEX_BIN='/nonexistent-forbidden-native-fallback')


def prepare(directory,config,source):
    check(ROOT)
    global_native=(Path.home()/'.codex').resolve()
    if (directory.exists() or directory.is_symlink() or not source.strip()
            or ROOT in directory.parents or directory in ROOT.parents
            or global_native == directory or global_native in directory.parents):
        raise ValueError('fresh unused demo directory and actual authorization source required')
    directory.mkdir(mode=0o700,parents=True)
    for name in ('home','ato','native-locator','repo'):(directory/name).mkdir(mode=0o700)
    env=environment(directory,config)
    os.environ.clear();os.environ.update(env)
    acpx_profile.operator_paths()  # locator validation; no auth content or model
    subprocess.run(['/usr/bin/git','init','-q',str(directory/'repo')],env=env,check=True,timeout=15)
    contract=runner.read(ROOT/'docs/external-coordinator/demo-contract.json')
    # Exact host interpreter, reviewed before scope provisioning. No PATH model.
    for row in contract['acceptance']:
        for argv in row.get('checks',[]):argv[0]=sys.executable
    for row in contract['stages']:
        for task in row['plan']['tasks']:
            for argv in task['checks']:argv[0]=sys.executable
    contract_path=directory/'contract.json';runner.save(contract_path,contract)
    actor=directory/'ato/actors/agy.json'
    provision=external_operator.provision(directory/'repo',contract_path,actor,source)
    submitted=external.submit(actor,contract_path)
    receipt=dict(version=1,runtime=str(ROOT),manifest_sha256=sha(ROOT/'build/external-coordinator/candidate.json'),
        dependency_config=str(config),directory=str(directory),actor_file=str(actor),
        contract=str(contract_path),workflow=submitted['workflow'],source=source,
        previous_incidental_admissions=1,codex_admissions_by_script=0,
        optional_agy_admissions_by_script=0,real_model_executed=False,observed_groups=[])
    runner.save(directory/'smoke.json',receipt)
    return dict(prepared=True,workflow=submitted['workflow'],receipt=str(directory/'smoke.json'),
                model_admissions=0,next_step='origin reviews contract/policy/regression then explicitly admits once')


def run(directory,admit,host_passed):
    check(ROOT)
    receipt=runner.read(directory/'smoke.json')
    if (not admit or not host_passed or receipt['runtime']!=str(ROOT) or
            receipt['manifest_sha256']!=sha(ROOT/'build/external-coordinator/candidate.json')):
        raise ValueError('explicit remaining admission and passing host qualification for exact candidate required')
    env=environment(directory,Path(receipt['dependency_config']))
    os.environ.clear();os.environ.update(env)
    # Missing credentials/dependencies/policy never cause a model or installation.
    with tempfile.TemporaryDirectory(prefix='ato-origin-preflight-') as tmp:
        acpx_profile.codex_profile(directory/'repo',Path(tmp),[directory/'ato',ROOT], 'workspace-write')
    barrier=directory/'codex-admission.json'
    fd=os.open(barrier,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        json.dump(dict(at=time.time(),model='Codex ACP',limit=1,no_replay=True,
                       workflow=receipt['workflow'],manifest_sha256=receipt['manifest_sha256']),stream)
        stream.flush();os.fsync(stream.fileno())
    receipt.update(codex_admissions_by_script=1,admission_outcome='unknown',real_model_executed=False)
    runner.save(directory/'smoke.json',receipt)
    command=[sys.executable,str(ROOT/'external_coordinator.py'),'--actor-file',receipt['actor_file'],'continue']
    groups={};process=None
    try:
        with (directory/'codex-cli.log').open('wb') as out:
            process=subprocess.Popen(command,env=env,stdout=out,stderr=subprocess.STDOUT,start_new_session=True)
            remember_group(groups,process.pid,runner.process_identity(process.pid))
            deadline=time.monotonic()+390
            while process.poll() is None:
                state=runner.read(Path(receipt['workflow'])/'state.json')
                for stage in state['stages']:
                    child=runner.read(Path(stage['run_dir'])/'state.json')
                    if child.get('active_process'):
                        active=child['active_process']
                        remember_group(groups,active['pid'],active.get('identity'))
                receipt['observed_groups']=sorted(groups)
                receipt['observed_group_identities']={str(pid):identity for pid,identity in groups.items()}
                runner.save(directory/'smoke.json',receipt)
                if time.monotonic()>deadline:
                    external.stop(receipt['actor_file'])
                    raise TimeoutError('origin hard bound; STOP retained; admission never replayed')
                time.sleep(.1)
        receipt['cli_exit']=process.returncode
        receipt['admission_outcome']='returned'
    finally:
        if process and process.poll() is None:
            external.stop(receipt['actor_file'])
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:pass
        # Canonical STOP and wait precede emergency cleanup. Never signal a
        # reused numeric PGID or adopt a missing leader's surviving descendants.
        signal_owned_groups(groups,signal.SIGTERM)
        time.sleep(.2)
        signal_owned_groups(groups,signal.SIGKILL)
        if process:process.wait(timeout=5)
        receipt['observed_groups']=sorted(groups)
        receipt['observed_group_identities']={str(pid):identity for pid,identity in groups.items()}
        runner.save(directory/'smoke.json',receipt)
    # Readback independently determines success; a CLI exit/reply never does.
    from scripts.external_host_readback import readback
    return readback(directory)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    sub=parser.add_subparsers(dest='action',required=True)
    p=sub.add_parser('prepare');p.add_argument('--directory',required=True);p.add_argument('--dependency-config',required=True);p.add_argument('--source',required=True)
    p=sub.add_parser('run');p.add_argument('--directory',required=True)
    p.add_argument('--admit-real-codex',action='store_true');p.add_argument('--confirm-host-regression-passed',action='store_true')
    args=parser.parse_args();directory=Path(args.directory).resolve()
    value=prepare(directory,Path(args.dependency_config).resolve(),args.source) if args.action=='prepare' else run(directory,args.admit_real_codex,args.confirm_host_regression_passed)
    print(json.dumps(value,ensure_ascii=False))
    if args.action=='run' and not value['ato_checks_passed']:sys.exit(1)


if __name__=='__main__':
    try:main()
    except Exception as error:
        # No native stderr/login/config dumps. The origin inspects owned evidence.
        print(json.dumps(dict(error=type(error).__name__,detail='origin smoke refused/failed; retain original admission and workflow; no replay')),file=sys.stderr)
        sys.exit(1)
