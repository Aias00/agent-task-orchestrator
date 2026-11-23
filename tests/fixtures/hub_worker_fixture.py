"""Standalone synthetic worker. It never invokes a native CLI or provider."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

args=sys.argv[1:];prompt=sys.stdin.read()
cfg=json.loads(Path(sys.argv[0]+'.json').read_text())
for key in ('CTASK_SESSION_REF','CODEX_THREAD_ID','PYTHONPATH','NODE_OPTIONS','DYLD_INSERT_LIBRARIES','H1_SECRET'):
    assert key not in os.environ
assert Path(os.environ['ATO_HOME']).name=='worker-ato'
assert Path(os.environ['HOME']).name=='worker-home'
schema=json.loads(Path(args[args.index('--output-schema')+1]).read_text())
mode=cfg.get('mode','ok')
if mode=='stall':
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])
    Path(cfg['pids']).write_text(json.dumps([os.getpid(),child.pid]))
    time.sleep(60)
if 'decision' in schema.get('properties',{}):
    result=dict(decision='blocked',reason='Synthetic checks still fail',tasks=[],retry_after_seconds=0)
else:
    if mode!='blocked':
        Path('result.txt').write_text('bad' if mode=='fail' else
            'done-fixed' if mode!='unchanged' and 'Fix only these blocker findings' in prompt else 'done')
        if cfg.get('artifact'):Path(cfg['artifact']).write_text('synthetic implementation')
    result=dict(status='blocked' if mode=='blocked' else 'ready_for_checks',summary='Synthetic worker',
        blocker='Synthetic blocker' if mode=='blocked' else '',next_step='Inspect synthetic evidence',retry_after_seconds=0)
Path(args[args.index('-o')+1]).write_text(json.dumps(result))
