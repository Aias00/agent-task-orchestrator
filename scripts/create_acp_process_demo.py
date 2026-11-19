#!/usr/bin/env python3
"""Create one retained, foreground, offline ACP fixture run in a scratch repo."""
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT/'build/acp-process'
REPO = BUILD/'demo-repo'
CLI = ROOT/'tests/fixtures/acp_fixture_cli.py'

def main():
    BUILD.mkdir(parents=True, exist_ok=True)
    REPO.mkdir(exist_ok=False)
    subprocess.run(['git', 'init', '-q', str(REPO)], check=True)
    plan = {'objective': 'Exercise the offline ACP bridge and independent host check',
            'agent': {'kind': 'offline_acp_fixture'}, 'memory': False,
            'tasks': [{'id': 'demo', 'prompt': 'create the offline fixture result', 'depends_on': [],
                       'checks': [[sys.executable, '-c', "from pathlib import Path; assert Path('result').read_text() == 'fixture output'"]]}],
            'limits': {'runtime_seconds': 30, 'attempt_seconds': 10, 'check_seconds': 3, 'max_attempts': 1}}
    path = REPO/'demo-plan.json'
    path.write_text(json.dumps(plan, indent=2)+'\n')
    env = dict(os.environ, CODEX_HOME=str(BUILD/'demo-config'))
    def cli(*args):
        return subprocess.check_output([sys.executable, str(CLI), *map(str, args)], env=env, text=True).strip()
    run = Path(cli('create', '--repo', REPO, '--plan', path))
    cli('run', run)
    state = json.loads((run/'state.json').read_text())
    if state['status'] != 'completed':
        raise RuntimeError('demo did not complete; inspect saved run')
    trace = json.loads(subprocess.check_output([sys.executable, str(ROOT/'runner.py'), 'trace', str(run), '--json'], text=True))
    if trace['summary']['tool_call_count'] != 1 or not any(e['kind'] == 'check_passed' for e in trace['events']):
        raise RuntimeError('demo trace did not contain required fixture and check events')
    (BUILD/'demo.json').write_text(json.dumps({'run_dir': str(run.relative_to(ROOT))}, indent=2)+'\n')
    print(json.dumps({'run_dir': str(run.relative_to(ROOT)), 'trace': trace}))

if __name__ == '__main__':
    main()
