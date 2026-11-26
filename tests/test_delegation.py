import base64
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ato_adapters import appserver
from ato_workflow import delegation
from ato_workflow import interaction
import recovery
import runner
from ato_workflow import workflow
from test_workflow import PLANNER

HUB = '11111111-1111-4111-8111-000000000002'
from legacy_support import LEGACY_CLI, legacy_runtime


class DaemonFixture:
    def __init__(self, home):
        self.home = home;self.threads = {};self.calls = [];self.mode = 'normal';self.stopped = False
        path = home/'app-server-control/app-server-control.sock';path.parent.mkdir()
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM);self.socket.bind(str(path));self.socket.listen();self.socket.settimeout(.1)
        self.thread = threading.Thread(target=self.serve, daemon=True);self.thread.start()

    def close(self):
        self.stopped = True;self.thread.join(2);self.socket.close()

    def exact(self, stream, n):
        buf=b''
        while len(buf)<n:
            data=stream.recv(n-len(buf))
            if not data:raise EOFError()
            buf+=data
        return buf

    def send(self, stream, data, opcode=1):
        payload=json.dumps(data).encode() if opcode==1 else data
        header=bytes([0x80|opcode])
        header+=bytes([len(payload)]) if len(payload)<126 else bytes([126])+struct.pack('!H',len(payload))
        stream.sendall(header+payload)

    def serve(self):
        while not self.stopped:
            try:conn,_=self.socket.accept()
            except socket.timeout:continue
            try:
                with conn:
                    conn.settimeout(3);header=b''
                    while b'\r\n\r\n' not in header:header+=self.exact(conn,1)
                    key=next(x.split(':',1)[1].strip() for x in header.decode().split('\r\n') if x.startswith('Sec-WebSocket-Key:'))
                    accept=base64.b64encode(hashlib.sha1((key+'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
                    if self.mode=='bad-handshake':accept='invalid'
                    conn.sendall(('HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: '+accept+'\r\n\r\n').encode())
                    while True:
                        first,second=self.exact(conn,2);size=second&127
                        if size==126:size=struct.unpack('!H',self.exact(conn,2))[0]
                        elif size==127:size=struct.unpack('!Q',self.exact(conn,8))[0]
                        mask=self.exact(conn,4);raw=self.exact(conn,size);raw=bytes(b^mask[i%4] for i,b in enumerate(raw))
                        if first&15==10:continue
                        req=json.loads(raw);method=req['method'];self.calls.append(method)
                        if 'id' not in req:continue
                        params=req.get('params',{});result={}
                        if method=='thread/start':
                            tid=str(uuid.uuid4());cwd=params['cwd']
                            if self.mode=='wrong-cwd':cwd=str(Path(cwd).parent)
                            path=self.home/(tid+'.jsonl');path.write_text(json.dumps({'type':'session_meta','payload':{'id':tid,'cwd':cwd}})+'\n')
                            with sqlite3.connect(self.home/'state_5.sqlite') as con:con.execute('insert into threads values (?,?)',(tid,str(path)))
                            thread=dict(id=tid,cwd=cwd,path=str(path),status={'type':'idle'},source='vscode');self.threads[tid]=thread
                            if self.mode=='lost-create':break
                            sandbox={'type':'readOnly' if params['sandbox']=='read-only' else 'workspaceWrite'}
                            if self.mode=='wrong-policy':sandbox={'type':'dangerFullAccess'}
                            result={'thread':thread,'sandbox':sandbox,'approvalPolicy':'never'}
                        elif method=='thread/read':result={'thread':self.threads[params['threadId']]}
                        if self.mode=='ping':self.send(conn,b'ping',9)
                        self.send(conn,{'id':req['id'],'result':result})
            except (EOFError,OSError,ValueError):pass


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.children=[]
        self.enterContext(legacy_runtime())
        self.tmp=tempfile.TemporaryDirectory(prefix='ctask-',dir='/tmp');self.root=Path(self.tmp.name)
        self.repo=self.root/'project';self.repo.mkdir();subprocess.run(['git','init','-q',str(self.repo)],check=True)
        self.home=self.root/'home';self.home.mkdir();path=self.home/'hub.jsonl';path.write_text(json.dumps({'payload':{'id':HUB}})+'\n')
        with sqlite3.connect(self.home/'state_5.sqlite') as con:
            con.execute('create table threads(id text,rollout_path text)');con.execute('insert into threads values (?,?)',(HUB,str(path)))
        self.server=DaemonFixture(self.home)
        self.fake=self.root/'fake-codex';self.fake.write_text(PLANNER);self.fake.chmod(0o755)
        self.queue=self.root/'queue'
        self.queue.write_text(f'''#!{sys.executable}
import sys,json,os,subprocess,re
from pathlib import Path
thread=sys.argv[sys.argv.index('--thread')+1];message=sys.argv[sys.argv.index('--message')+1]
with open({str(self.root/'queue-calls')!r},'a') as f:f.write(json.dumps(dict(thread=thread,message=message))+'\\n')
if os.environ.get('TEST_ACCEPT')=='1' and '[ctask-delegation' in message:
 d=re.search(r'ctask delegation-accept (.+?)，',message)[1]
 import ast
 with open({str(self.root/'accept.log')!r},'ab') as log:
  subprocess.Popen([sys.executable,{str(LEGACY_CLI)!r},'delegation-accept',ast.literal_eval(d)],env=dict(os.environ,CODEX_THREAD_ID=thread),stdout=log,stderr=log,start_new_session=True)
if os.environ.get('TEST_QUEUE_UNKNOWN')=='1':sys.exit(1)
print('Queued message '+thread+' for thread '+thread+'.')
''');self.queue.chmod(0o755)
        self.env=dict(os.environ,CODEX_HOME=str(self.home),CODEX_THREAD_ID=HUB,CTASK_QUEUE_BIN=str(self.queue),CTASK_CODEX_BIN=str(self.fake))
        self.patch=patch.dict(os.environ,self.env);self.patch.start();interaction.configure(HUB,'User explicitly delegated a new project conversation')
        self.plan=self.repo/'plan.json';self.contract=dict(objective='Deliver both outputs',scope='Isolated fixture repo',authorization='Local tests only',memory=False,
            acceptance=[dict(id=k,description=k,checks=[[sys.executable,'-c',f'from pathlib import Path; assert Path({k!r}).exists()']]) for k in ['first','second']],
            limits=dict(runtime_seconds=60,attempt_seconds=10,check_seconds=3,max_attempts=1))
        runner.save(self.plan,self.contract)

    def tearDown(self):
        for proc in self.children:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=5)
        for p in (self.repo/'.codex/tasks/workflows').glob('*/recovery.json'):recovery.disable(p.parent)
        limit=time.monotonic()+7
        while time.monotonic()<limit:
            live=[p for p in (self.repo/'.codex/tasks/workflows').glob('*/supervisor.json') if recovery.alive(runner.read(p).get('pid'))]
            if not live:break
            time.sleep(.1)
        self.server.close();self.patch.stop();self.tmp.cleanup()

    def create(self, **kw):
        return delegation.delegate(self.repo,self.plan,'Fixture execution conversation','Explicit user request: create a new coordinator',**kw)

    def test_create_binds_new_persistent_thread_and_full_workflow(self):
        with patch('ato_workflow.delegation.start_bridge'):r=self.create()
        self.assertEqual(r['state'],'queued');self.assertNotEqual(r['origin_thread'],HUB)
        w=Path(r['workflow_dir']);self.assertEqual(runner.read(w/'state.json')['status'],'created')
        self.assertEqual(runner.read(w/'origin.json')['origin_thread_id'],r['origin_thread'])
        self.assertEqual(runner.read(w/'contract.json')['objective'],'Deliver both outputs')
        self.assertEqual(len(self.server.threads),1)
        with patch.dict(os.environ,CODEX_THREAD_ID=r['origin_thread']):
            self.assertEqual(delegation.accept(r['directory'])['state'],'accepted')
            with self.assertRaises(ValueError):delegation.accept(r['directory'])

    def test_repeating_same_request_does_not_create_or_dispatch_twice(self):
        with patch('ato_workflow.delegation.start_bridge'):
            first=self.create();second=self.create()
        self.assertEqual(first['origin_thread'],second['origin_thread'])
        self.assertEqual(len(self.server.threads),1)
        self.assertEqual(len((self.root/'queue-calls').read_text().splitlines()),1)

    def test_lost_create_result_is_not_blindly_retried(self):
        self.server.mode='lost-create'
        first=self.create();second=self.create()
        self.assertEqual(first['state'],'creation_unknown');self.assertEqual(second['state'],'creation_unknown')
        self.assertEqual(len(self.server.threads),1);self.assertFalse((self.root/'queue-calls').exists())

    def test_unknown_queue_receipt_retains_one_identity_and_message(self):
        with patch.dict(os.environ,TEST_QUEUE_UNKNOWN='1'),patch('ato_workflow.delegation.start_bridge'):
            first=self.create();second=self.create()
        self.assertEqual(first['state'],'delivery_unknown');self.assertEqual(second['origin_thread'],first['origin_thread'])
        self.assertEqual(len((self.root/'queue-calls').read_text().splitlines()),1)
        with patch.dict(os.environ,CODEX_THREAD_ID=first['origin_thread']):
            self.assertEqual(delegation.accept(first['directory'])['state'],'accepted')

    def test_changed_request_and_pending_other_task_are_rejected(self):
        with patch('ato_workflow.delegation.start_bridge'):self.create(request_id='one')
        with self.assertRaises(ValueError):delegation.delegate(self.repo,self.plan,'Other task','Different intent',request_id='one')
        with self.assertRaises(ValueError):self.create(request_id='two')
        self.assertEqual(len(self.server.threads),1)

    def test_unfinished_workflow_and_repository_lock_prevent_creation(self):
        self.repo.joinpath('.codex/tasks').mkdir(parents=True)
        with recovery.locked(self.repo/'.codex/tasks/.worker.lock'):
            with self.assertRaises(BlockingIOError):self.create()
        workflow.create(self.repo,self.plan,HUB)
        with self.assertRaises(ValueError):self.create()
        self.assertEqual(len(self.server.threads),0)

    def test_wrong_cwd_or_expanded_policy_never_dispatches(self):
        for mode in ['wrong-cwd','wrong-policy']:
            self.server.mode=mode
            # Separate project state prevents an unresolved earlier creation being bypassed.
            if mode=='wrong-policy':
                self.repo=self.root/'other';self.repo.mkdir();subprocess.run(['git','init','-q',str(self.repo)],check=True)
            r=self.create();self.assertEqual(r['state'],'setup_failed')
        self.assertFalse((self.root/'queue-calls').exists())

    def test_accept_checks_target_stop_deadline_and_contract(self):
        with patch('ato_workflow.delegation.start_bridge'):r=self.create()
        with self.assertRaises(ValueError):delegation.accept(r['directory'])
        w=Path(r['workflow_dir']);(w/'STOP').touch()
        with patch.dict(os.environ,CODEX_THREAD_ID=r['origin_thread']):
            with self.assertRaises(ValueError):delegation.accept(r['directory'])
            (w/'STOP').unlink();delegation.persisted(Path(r['directory']),deadline=0)
            with self.assertRaises(ValueError):delegation.accept(r['directory'])

    def test_wrong_actor_missing_path_and_incomplete_goal_do_not_create(self):
        with patch.dict(os.environ,CODEX_THREAD_ID=str(uuid.uuid4())):
            with self.assertRaises(ValueError):self.create()
        with self.assertRaises(ValueError):delegation.delegate(self.root/'missing',self.plan,'x','explicit request')
        runner.save(self.plan,{'objective':'Only a vague goal'})
        with self.assertRaises(ValueError):self.create()
        self.assertEqual(len(self.server.threads),0)

    def test_websocket_ping_and_handshake_validation(self):
        self.server.mode='ping'
        with appserver.Client(self.home) as client:
            self.assertEqual(client.request('test/read',{}),{})
        self.server.mode='bad-handshake'
        with self.assertRaises(ValueError):appserver.Client(self.home)

    def test_bridge_starts_only_after_accept_and_continues_two_stages(self):
        start=delegation.start_bridge
        def capture(d):
            proc=start(d);self.children.append(proc);return proc
        with patch.dict(os.environ,TEST_ACCEPT='1'),patch('ato_workflow.delegation.start_bridge',side_effect=capture):
            r=self.create()
        d=Path(r['directory']);w=Path(r['workflow_dir']);limit=time.monotonic()+30
        while time.monotonic()<limit:
            record=runner.read(d/'delegation.json');state=runner.read(w/'state.json')
            if record['state']=='started' and state['status']=='completed':break
            time.sleep(.1)
        evidence=(self.root/'accept.log').read_text() if (self.root/'accept.log').exists() else str(record)
        self.assertEqual(record['state'],'started',evidence)
        self.assertEqual(state['status'],'completed',(w/'worker.log').read_text())
        self.assertEqual(len(state['stages']),2)
        self.assertTrue((self.repo/'first').exists() and (self.repo/'second').exists())
        self.assertEqual(len(self.server.threads),1)
        self.assertEqual(runner.read(w/'origin.json')['origin_thread_id'],record['origin_thread'])


if __name__ == '__main__':unittest.main()
