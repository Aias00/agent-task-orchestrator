import hashlib,json,os,signal,subprocess,sys,time
from pathlib import Path
ops=Path(__file__).parent;r=json.loads((ops/'real-demo/smoke.json').read_text());root=Path(r['runtime']);sys.path.insert(0,str(root));import runner
from scripts.external_host_smoke import remember_group,signal_owned_groups
barrier=ops/'agy-admission.json';fd=os.open(barrier,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
with os.fdopen(fd,'w') as f:json.dump(dict(at=time.time(),limit=1,timeout_seconds=300,no_replay=True,scope='fresh AGY readback of same accepted ATO workflow; no new coding task'),f)
workspace=ops/'agy-readback-project';workspace.mkdir(mode=0o700,exist_ok=False)
control=str(ops/'agy-readonly-control.py');python=sys.executable
prompt=f'''你只负责核对这次已经完成的 ATO 编码任务，不新建任务、不调用模型执行端、不写代码、不调用continue。使用终端依次运行以下精确命令并核对实际输出，必须实际调用，不能根据描述编造：
{python} {control} init
{python} {control} submit --contract {r['contract']}
{python} {control} status
{python} {control} result
submit 必须返回 admission=existing，不能创建新任务。最后用中文简短报告 ATO 总任务是否 completed、accepted 是否 true。不能调用 acpx/codex 直连或人工授权命令。'''
cmd=['OPERATOR_HOME/.local/bin/agy','--mode','auto','--print-timeout','240s','--print',prompt]
p=subprocess.Popen(cmd,cwd=workspace,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True);groups={};remember_group(groups,p.pid,runner.process_identity(p.pid));started=time.time();report=dict(at=started,model_admissions=1,scope='fresh AGY coordinator validation, not old conversation')
try:
 out,err=p.communicate(timeout=300)
 report.update(exit=p.returncode,elapsed_seconds=time.time()-started,stdout_sha256=hashlib.sha256(out).hexdigest(),stderr_sha256=hashlib.sha256(err).hexdigest(),raw_output_persisted=False)
except subprocess.TimeoutExpired:
 report.update(timeout=True,elapsed_seconds=time.time()-started)
 signal_owned_groups(groups,signal.SIGTERM)
 try:p.wait(timeout=5)
 except subprocess.TimeoutExpired:signal_owned_groups(groups,signal.SIGKILL);p.wait(timeout=5)
finally:
 audit=ops/'agy-ato-command-audit.jsonl';rows=[json.loads(x) for x in audit.read_text().splitlines()] if audit.exists() else []
 report['commands']=rows
 report['actual_ato_readback_passed']=all(any(x['action']==a and x['exit']==0 for x in rows) for a in ('init','submit','status','result')) and any(x['action']=='submit' and x['admission']=='existing' for x in rows) and any(x['action']=='result' and x['accepted'] is True for x in rows)
 ps=subprocess.check_output(['/bin/ps','-axo','pid=,pgid='],text=True)
 report['remaining_owned_processes']=[int(x.split()[0]) for x in ps.splitlines() if int(x.split()[1]) in groups]
 (ops/'agy-control-verification.json').write_text(json.dumps(report,indent=2));print(json.dumps(report))
