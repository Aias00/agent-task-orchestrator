"""Synthetic protocol peer, never delegates to a native CLI or provider."""
import json
import os
from pathlib import Path
import signal
import sys
import time
import uuid

kind=Path(sys.argv[0]).name.removeprefix('fake-')
if '--version' in sys.argv:
    print({'agy':'1.5.14','claude':'2.1.325','codex':'codex-cli 0.171.0'}[kind]);raise SystemExit(0)
home=Path(os.environ['HOME']);cfg=json.loads((home/'fixture.json').read_text())
if sys.argv[1:]==['auth','status']:print(json.dumps(dict(loggedIn=False)));raise SystemExit(0)
args=sys.argv[1:]
prompt=args[args.index('-p')+1] if kind=='agy' else sys.stdin.read()
if kind=='agy':
    assert '--mode' not in args and '--sandbox' not in args and '--disable-slash-commands' in args
elif kind=='claude':
    assert all(flag in args for flag in ('--safe-mode','--restricted','--disable-slash-commands','--strict-mcp-config','--no-session-persistence'))
    assert args[args.index('--tools')+1]=='Read,Glob,Grep' and '--resume' not in args
else:
    assert args[0]=='exec' and 'approval_policy="never"' in args
    if 'default_permissions="ato-host"' in args:
        profiles = [value for value in args if value.startswith('permissions.ato-host=')]
        assert len(profiles) == 1 and '--sandbox' not in args
        policy = profiles[0]
        assert '":root"="read"' in policy and 'network={enabled=false}' in policy
        assert json.dumps(str(Path.cwd()/'.codex/tasks')) + '="deny"' in policy
        assert json.dumps(str(Path.cwd())) + '="write"' not in policy
    else:
        assert 'sandbox_mode="read-only"' in args if 'resume' in args else args[args.index('--sandbox')+1]=='read-only'
    assert '-C' not in args
for name in ('CTASK_SESSION_REF','CTASK_SESSION_TOKEN','ATO_ACTOR_FILE','PYTHONPATH','NODE_OPTIONS','DYLD_INSERT_LIBRARIES','H1_SECRET','HTTPS_PROXY'):
    assert name not in os.environ
if cfg.get('sentinel'):print(cfg['sentinel'],file=sys.stderr,flush=True)
mode=cfg.get('mode','ok')
if mode=='stall':
    signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)
if mode=='mutate':Path('result.txt').write_text('reviewer mutation')
if mode=='exit':raise SystemExit(17)
if mode=='flood':
    sys.stderr.write('x'*2000000);sys.stderr.flush()
if mode=='missing':raise SystemExit(0)
if mode=='bad-json':print('{truncated');raise SystemExit(0)
if mode=='auth':
    if kind=='claude':print(json.dumps(dict(type='result',is_error=True,structured_output=None,total_cost_usd=0)))
    else:print('not logged in',file=sys.stderr)
    raise SystemExit(1)
sid=str(uuid.uuid4())
if kind=='agy' and '--conversation' in args:sid=args[args.index('--conversation')+1]
if kind=='codex' and 'resume' in args:sid=args[args.index('resume')+1]
if kind=='claude':sid=args[args.index('--session-id')+1]
if mode=='wrong-session':sid='wrong-session'
verdict=cfg['verdict']
state=home/'.gemini/antigravity-cli/state.json'
if cfg.get('sequence'):
    sequence=json.loads(state.read_text()).get('turn',0) if state.exists() else 0
    verdict=cfg['sequence'][min(sequence,len(cfg['sequence'])-1)]
    state.write_text(json.dumps(dict(turn=sequence+1)))
if mode=='secret-output':verdict=dict(verdict,summary=cfg['sentinel'])
if cfg.get('expect_brief'):assert 'handoff-brief.md' in prompt and 'Hub ledger handoff' in prompt
if kind=='agy':print(json.dumps(dict(status='FAILURE' if mode=='bad-status' else 'SUCCESS',conversation_id=sid,structured_output=verdict)))
elif kind=='claude':print(json.dumps(dict(type='result',is_error=False,session_id=sid,structured_output=verdict)))
else:
    print(json.dumps(dict(type='thread.started',thread_id=sid)))
    Path(args[args.index('-o')+1]).write_text(json.dumps(verdict))
