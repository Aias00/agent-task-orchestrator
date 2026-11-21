#!/usr/bin/env python3
"""Synthetic Codex-shaped protocol. Never invokes a provider or native CLI."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid

args = sys.argv[1:]
prompt = sys.stdin.read()
repo = Path.cwd()
home = Path(os.environ['CODEX_HOME'])
home.mkdir(parents=True, exist_ok=True)
warm = 'resume' in args
identifier = args[-2] if warm else str(uuid.uuid4())
path = home/(identifier+'.jsonl')
source = (json.loads(path.read_text().splitlines()[0])['payload']['source'] if warm else
          args[args.index('--thread-source')+1] if '--thread-source' in args else 'fixture-legacy')

def emit(kind, payload):
    with path.open('a') as stream:
        stream.write(json.dumps(dict(type=kind, payload=payload))+'\n')

artifact_input = (repo/'artifact.txt').read_bytes()
reads = [dict(path='artifact.txt', bytes=len(artifact_input))]
if not warm:
    emit('session_meta', dict(id=identifier, cwd=str(repo), source=source))
    with sqlite3.connect(home/'state_5.sqlite') as db:
        db.execute('CREATE TABLE IF NOT EXISTS threads(id TEXT PRIMARY KEY,rollout_path TEXT)')
        db.execute('INSERT INTO threads VALUES(?,?)', (identifier, str(path)))
    # Modelled cold initialization is an actual read, counted separately from
    # protocol/prompt bytes. This says nothing about a real provider's caching.
    reads.append(dict(path='guide.txt', bytes=len((repo/'guide.txt').read_bytes())))
turn = str(uuid.uuid4())
mode = args[args.index('--sandbox')+1]
if 'sandbox_workspace_write.network_access=false' not in args or 'sandbox_workspace_write.writable_roots=[]' not in args:
    raise ValueError('fixture requires the same explicit narrow grants in every arm')
if mode != 'workspace-write' or args[args.index('--model')+1] != 'fixture-model-v1' or 'approval_policy="never"' not in args:
    raise ValueError('fixture requires identical model, sandbox and approval')
if '--last' in args or any('dangerously' in a for a in args):
    raise ValueError('forbidden native argument')
context = dict(turn_id=turn, cwd=str(repo), approval_policy='never',
    sandbox_policy=dict(type=mode, writable_roots=[], network_access=False),
    model='fixture-model-v1', model_provider='scripted-offline')
emit('event_msg', dict(type='task_started', turn_id=turn))
emit('turn_context', context)
emit('event_msg', dict(type='user_message', message=prompt))
print(json.dumps(dict(type='thread.started', thread_id=identifier)), flush=True)
print(json.dumps(dict(type='turn.started')), flush=True)
model_start = time.monotonic()
time.sleep(.01)  # Identical scripted model latency for every arm and turn.
prior = artifact_input.decode()
if prior == '':
    artifact, status = 'first\n', 'continue'
elif prior == 'first\n':
    artifact, status = 'first\nsecond\n', 'complete'
else:
    raise ValueError('unexpected/replayed fixture turn')
model_duration = time.monotonic()-model_start
(repo/'artifact.txt').write_text(artifact)
emit('event_msg', dict(type='task_complete', turn_id=turn))
Path(args[args.index('-o')+1]).write_text(json.dumps(dict(status=status,
    summary='synthetic file increment', next_step='finish second increment' if status=='continue' else 'independent checks',
    blocker='', retry_after_seconds=0)))
record = dict(argv_sha256=hashlib.sha256(json.dumps(args).encode()).hexdigest(), native_id=identifier, turn_id=turn, source=source, resumed=warm,
    model='fixture-model-v1', permissions=context['sandbox_policy'], approval='never',
    model_requests=1, scripted_model_seconds=model_duration, tool_reads=reads,
    context_bytes=len(prompt.encode()), artifact_sha256=hashlib.sha256(artifact.encode()).hexdigest())
with (repo/'fixture-receipts.jsonl').open('a') as stream:
    stream.write(json.dumps(record)+'\n')
print(json.dumps(dict(type='turn.completed', usage=dict(input_tokens=1, output_tokens=1))), flush=True)
