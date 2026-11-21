import os,json,hashlib,shlex,subprocess,sys,time
from pathlib import Path
ops=Path(__file__).parent;base=Path('OPERATOR_DEMO');stage=json.loads((ops/'staged-runtime-v2.json').read_text());root=Path(stage['runtime']);sys.path.insert(0,str(root));import runner
from scripts.external_host_smoke import environment
from scripts.prepare_external_candidate import check
check(root)
assert json.loads((ops/'final-home-isolation-qualification.json').read_text())['passed']
proof=json.loads((ops/'real-demo/readback.json').read_text());assert proof['ato_checks_passed'] and proof['real_codex_acp_verified'] and proof['active_owned_processes_after_cleanup']==0
baseline=json.loads((ops/'origin-preactivation-baseline.json').read_text())
for path,row in baseline['protected_configuration'].items():
 p=Path(path);assert p.exists()==row['exists']
 if p.exists():assert hashlib.sha256(p.read_bytes()).hexdigest()==row['sha256'], 'protected config changed: '+path
main=Path('OPERATOR_HOME/.local/bin/ato-control');demo=main.with_name('ato-agy-demo')
assert not main.exists() and not main.is_symlink() and not demo.exists() and not demo.is_symlink()
contents={main:'#!/bin/sh\nset -eu\nexport ATO_ACPX_CONFIG='+shlex.quote(str(ops/'operator-paths.json'))+'\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(root/'external_coordinator.py'))+' "$@"\n',demo:'#!/bin/sh\nset -eu\nexec '+shlex.quote(sys.executable)+' OPERATOR_DEMO/demo.py "$@"\n'}
for path,content in contents.items():
 fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o700)
 with os.fdopen(fd,'w') as f:f.write(content);f.flush();os.fsync(f.fileno())
installation=dict(at=time.time(),runtime=str(root),manifest_sha256=stage['manifest_sha256'],dependency_config=str(ops/'operator-paths.json'),activated=True,entry=str(main),demo_entry=str(demo),python=sys.executable,old_global_ato_binding_preserved=True)
(base/'installation.json').write_text(json.dumps(installation,indent=2));(base/'installation.json').chmod(0o600)
receipt=json.loads((ops/'real-demo/smoke.json').read_text());env=environment(ops/'real-demo',ops/'operator-paths.json');reads=[]
for action in ('init','status','result'):
 p=subprocess.run([str(main),'--actor-file',receipt['actor_file'],action],env=env,capture_output=True,timeout=30,check=True);v=json.loads(p.stdout)
 if action=='result':assert v['accepted'] is True
 reads.append(dict(action=action,exit=p.returncode,accepted=v.get('accepted'),stdout_sha256=hashlib.sha256(p.stdout).hexdigest()))
p=subprocess.run([str(demo),'--check'],capture_output=True,check=True,timeout=30)
result=dict(**installation,actual_additive_entry_verified=True,global_binding_preserved=True,installed_readback=reads,demo_check_exit=p.returncode,entries=[dict(path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in contents],evidence='same actual accepted workflow through installed entry')
(ops/'entry-activation.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
