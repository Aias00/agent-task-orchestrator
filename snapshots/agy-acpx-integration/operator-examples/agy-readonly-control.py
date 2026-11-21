import json,os,subprocess,sys,time,hashlib
from pathlib import Path
ops=Path(__file__).parent;demo=ops/'real-demo'
r=json.loads((demo/'smoke.json').read_text())
allowed={'init':[],'submit':['--contract',r['contract']],'status':[],'events':['--cursor','0','--limit','20'],'result':[]}
args=sys.argv[1:]
if not args or args[0] not in allowed or args[1:]!=allowed[args[0]]:raise SystemExit('Only exact readback and idempotent same-contract submit allowed')
env=dict(PATH='/usr/bin:/bin',HOME=str(demo/'home'),ATO_HOME=str(demo/'ato'),CODEX_HOME=str(demo/'native-locator'),ATO_ACPX_CONFIG=r['dependency_config'])
p=subprocess.run(['OPERATOR_HOME/.local/bin/ato-control','--actor-file',r['actor_file'],*args],env=env,capture_output=True,timeout=25)
try:v=json.loads(p.stdout)
except Exception:v={}
row=dict(at=time.time(),action=args[0],exit=p.returncode,admission=v.get('admission'),accepted=v.get('accepted'),status=v.get('status'),stdout_sha256=hashlib.sha256(p.stdout).hexdigest(),installed_entry='OPERATOR_HOME/.local/bin/ato-control')
with (ops/'agy-ato-command-audit.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
sys.stdout.buffer.write(p.stdout);sys.stderr.buffer.write(p.stderr);raise SystemExit(p.returncode)
