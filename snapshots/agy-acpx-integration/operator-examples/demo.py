#!/usr/bin/env python3
"""Human-invoked fresh isolated AGY->ATO->Codex coding trial. Never auto-start."""
import argparse,json,os,shlex,subprocess,sys,tempfile
from pathlib import Path
BASE=Path(__file__).resolve().parent

def prepare():
    installation=json.loads((BASE/'installation.json').read_text())
    runtime=Path(installation['runtime']);sys.path.insert(0,str(runtime))
    import acpx_profile,external_operator,runner
    from scripts.prepare_external_candidate import check
    check(runtime)
    (BASE/'demos').mkdir(mode=0o700,exist_ok=True)
    directory=Path(tempfile.mkdtemp(prefix='trial-',dir=BASE/'demos'))
    for name in ('repo','ato','native-locator'):(directory/name).mkdir(mode=0o700)
    env=dict(ATO_HOME=str(directory/'ato'),CODEX_HOME=str(directory/'native-locator'),ATO_ACPX_CONFIG=installation['dependency_config'])
    os.environ.update(env)
    acpx_profile.dependencies(acpx_profile.operator_paths())
    acpx_profile.check_clean_home(acpx_profile.operator_paths()['native_home'])
    subprocess.run(['/usr/bin/git','init','-q',str(directory/'repo')],check=True)
    contract=runner.read(runtime/'docs/external-coordinator/demo-contract.json')
    for row in contract['acceptance']:
        for argv in row.get('checks',[]):argv[0]=sys.executable
    for stage in contract['stages']:
        for task in stage['plan']['tasks']:
            for argv in task['checks']:argv[0]=sys.executable
    contract_file=directory/'contract.json';runner.save(contract_file,contract)
    actor=directory/'ato/actors/agy.json'
    external_operator.provision(directory/'repo',contract_file,actor,'User manually invoked ato-agy-demo for this fresh local greeting trial; no Git publication/deployment')
    control=directory/'control'
    lines=['#!/bin/sh','set -eu']
    lines += ['export '+key+'='+shlex.quote(value) for key,value in env.items()]
    lines += ['exec OPERATOR_HOME/.local/bin/ato-control --actor-file '+shlex.quote(str(actor))+' "$@"']
    control.write_text('\n'.join(lines)+'\n');control.chmod(0o700)
    prompt=f'''你是本次试用的主控，通过 ATO 指挥 Codex 编码。不要直接写代码，不直接运行 acpx/codex，不修改合约、ATO状态或权限，不代表用户验收。
本次已授权且隔离的合约：{contract_file}。请先阅读。
依次调用终端：
{control} init
{control} submit --contract {contract_file}
{control} continue
{control} result
continue 是前台任务，最多300秒。若工具返回运行中的终端session，等待原session结束；不要再次调用continue或重新发送任务。任务完成后按需调用 {control} events --cursor 0 --limit 30。
仅在 result 的 accepted=true、criteria 全部passed时报告通过，并给出 greeting.py 和证据位置。若失败/STOP/未知投递，保留原任务并报告，不能重试、更改次数或绕过限制。
任务只创建 greet(name) 中文问候函数，由 ATO 独立 Python 检查验证。请用中文报告你管理的 ATO 总任务状态。'''
    (directory/'AGY-INSTRUCTIONS.txt').write_text(prompt)
    runner.save(directory/'trial.json',dict(runtime=str(runtime),repo=str(directory/'repo'),actor_file=str(actor),contract=str(contract_file),scope='fresh greeting demo only',model_started=False))
    print('试用目录：'+str(directory),flush=True)
    print('查询/停止：'+str(control)+' status | result | stop',flush=True)
    return directory,prompt

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--prepare-only',action='store_true');p.add_argument('--check',action='store_true');a=p.parse_args()
    if a.check:
        i=json.loads((BASE/'installation.json').read_text());sys.path.insert(0,i['runtime'])
        from scripts.prepare_external_candidate import check
        check(Path(i['runtime']));print('Installed ATO bundle verified; no model or task started.');return
    directory,prompt=prepare()
    if a.prepare_only:
        print('在已有 AGY 对话粘贴：请严格按 '+str(directory/'AGY-INSTRUCTIONS.txt')+' 通过 ATO 执行这次隔离试用。');return
    os.chdir(directory/'repo')
    os.execv('OPERATOR_HOME/.local/bin/agy',['agy','--prompt-interactive',prompt])

if __name__=='__main__':main()
