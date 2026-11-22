"""O4/O5/O6: real Node 24, OS sandbox and loopback-only TLS/CONNECT fixtures."""
import http.server
import json
import os
from pathlib import Path
import socket
import shutil
import shlex
import ssl
import subprocess
import tempfile
import threading
import tomllib
import unittest
from unittest.mock import patch

from pi_production_egress import EgressProxy
from pi_toolchain import locate, production_sandbox

ROOT = Path(__file__).resolve().parents[1]


class ProductionTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.node = locate('node')
        cls.temp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.temp.name).resolve()
        cls.cert, cls.key = cls.base/'cert.pem', cls.base/'key.pem'
        subprocess.run(['/usr/bin/openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
            '-subj','/CN=provider.example','-addext','subjectAltName=DNS:provider.example',
            '-keyout',str(cls.key),'-out',str(cls.cert)],check=True,capture_output=True,timeout=10)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.mode='ok';self.requests=0;self.checks=0;test=self
        self.delay_gate=threading.Event();self.addCleanup(self.delay_gate.set)
        class Provider(http.server.BaseHTTPRequestHandler):
            def log_message(self,*_):pass
            def do_POST(self):
                test.requests+=1
                self.rfile.read(int(self.headers['Content-Length']))
                if test.mode=='hang':
                    test.delay_gate.wait(3)
                    return
                code=200 if test.mode not in ('503','redirect') else (503 if test.mode=='503' else 302)
                self.send_response(code)
                self.send_header('Content-Type','text/event-stream')
                self.end_headers()
                if code!=200:return
                text='synthetic-secret' if test.mode=='reflection' else '中😀'
                frame={'choices':[{'index':0,'delta':{'content':text},'finish_reason':'stop'}]}
                body=('data: '+json.dumps(frame,ensure_ascii=False)+'\r\n\r\ndata: [DONE]\r\n\r\n').encode()
                self.wfile.write(body);self.wfile.flush()
        self.provider=http.server.ThreadingHTTPServer(('127.0.0.1',0),Provider)
        tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(self.cert,self.key)
        self.provider.socket=tls.wrap_socket(self.provider.socket,server_side=True)
        self.thread=threading.Thread(target=self.provider.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.provider.server_close);self.addCleanup(self.provider.shutdown)
        self.proxy=EgressProxy('provider.example',['93.184.216.34'],check=self.check,
            resolver=lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))],
            connector=lambda address,timeout:socket.create_connection(self.provider.server_address,timeout=timeout))
        self.port=self.proxy.start();self.addCleanup(self.proxy.close)

    def check(self):self.checks+=1

    def invoke(self, *, token=None, ca=True):
        config=dict(endpoint='https://provider.example/v1',proxy_port=self.port)
        grant=dict(remaining_ms=1500,egress_grant=token or self.proxy.issue(1500))
        code="""import fs from 'node:fs';
import {request,decode} from './pi_production_transport.mjs';
const config=JSON.parse(process.env.FIXTURE_CONFIG),grant=JSON.parse(process.env.FIXTURE_GRANT);
try {const frames=await request(config,'{}',process.env.FIXTURE_KEY,grant,
  {ca:process.env.FIXTURE_CA?fs.readFileSync(process.env.FIXTURE_CA):undefined});
 const message=decode(frames,{api:'fixture',provider:'fixture',id:'fixture'},process.env.FIXTURE_KEY);
 console.log(JSON.stringify({ok:true,text:message.content[0].text}));}
catch {console.log(JSON.stringify({ok:false}));}
"""
        env=dict(HOME=str(self.base),PATH='/usr/bin:/bin',FIXTURE_KEY='synthetic-secret',
            FIXTURE_CONFIG=json.dumps(config),FIXTURE_GRANT=json.dumps(grant))
        if ca:env['FIXTURE_CA']=str(self.cert)
        p=subprocess.run([str(self.node),'--input-type=module','-e',code],cwd=ROOT,env=env,
            text=True,capture_output=True,timeout=6)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertNotIn('synthetic-secret',p.stdout+p.stderr)
        return json.loads(p.stdout)

    def test_real_node_https_uses_connect_and_utf8_crlf_sse(self):
        self.assertEqual(self.invoke(),{'ok':True,'text':'中😀'})
        self.assertEqual(self.requests,1);self.assertGreater(self.checks,0)

    def test_503_and_redirect_never_retry_or_follow(self):
        for mode in ('503','redirect'):
            self.mode=mode;before=self.requests
            self.assertFalse(self.invoke()['ok']);self.assertEqual(self.requests,before+1)

    def test_slow_real_tls_response_is_cancelled_by_absolute_deadline_without_retry(self):
        self.mode='hang'
        self.assertEqual(self.invoke(),{'ok':False})
        self.assertEqual(self.requests,1)

    def test_unknown_or_reused_connect_grant_has_no_provider_effect(self):
        self.assertFalse(self.invoke(token='unissued')['ok']);self.assertEqual(self.requests,0)
        token=self.proxy.issue(1500);self.assertTrue(self.invoke(token=token)['ok'])
        self.assertFalse(self.invoke(token=token)['ok']);self.assertEqual(self.requests,1)

    def test_tls_validation_and_secret_reflection_fail_closed(self):
        self.assertFalse(self.invoke(ca=False)['ok']);self.assertEqual(self.requests,0)
        self.mode='reflection';self.assertFalse(self.invoke()['ok'])

    def test_dns_rebinding_and_nonpublic_pins_rejected(self):
        for ip in ('127.0.0.1','::1','10.0.0.1','169.254.169.254'):
            with self.assertRaises(ValueError):EgressProxy('x',[ip],check=lambda:None)
        self.proxy.resolver=lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
        self.assertFalse(self.invoke()['ok']);self.assertEqual(self.requests,0)

    def test_egress_close_interrupts_incomplete_owned_connections(self):
        client=socket.create_connection(('127.0.0.1',self.port));self.addCleanup(client.close)
        client.sendall(b'CONNECT ')
        self.proxy.close()
        client.settimeout(2)
        try:
            self.assertEqual(client.recv(1),b'')
        except ConnectionResetError:
            pass  # A reset is also proof the owned incomplete connection was closed.

    def test_default_deny_policy_with_real_node_not_python_fixture(self):
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder).resolve();runtime=base/'runtime';project=base/'project';source=base/'source'
            package=base/'pinned-pi-package'
            for p in (runtime,project,source,package):p.mkdir()
            for name in ('trusted','adapter'):(runtime/name).mkdir()
            launcher=runtime/'trusted/pi-launcher'
            launcher.write_text('#!/bin/sh\nexec '+shlex.quote(str(self.node))+" -e 'console.log(\"fixed-launcher-ok\")'\n")
            launcher.chmod(0o700)
            secret=base/'native-auth';secret.write_text('private synthetic value')
            (project/'safe').write_text('allowed')
            (project/'.pi').mkdir();(project/'.pi/auth.json').write_text('forbidden')
            (project/'.env.production').write_text('private synthetic env')
            (source/'snapshot.py').write_text('immutable')
            (package/'bundle.js').write_text('pinned package')
            (package/'bundle.js').chmod(0o700)
            workflow=project/'.codex/tasks/workflows/selected';workflow.mkdir(parents=True)
            (workflow/'state.json').write_text('bound metadata')
            other=workflow.parent/'unbound';other.mkdir();(other/'state.json').write_text('forbidden')
            policy=production_sandbox(runtime,project,self.node,sources=source,
                proxy_port=self.port,control_socket=runtime/'control.sock',sandbox='workspace-write',
                pi_root=package,pi_entry=package/'bundle.js',workflow=workflow)
            code="""import fs from 'node:fs';import net from 'node:net';import {once} from 'node:events';import {spawnSync} from 'node:child_process';
const [r,p,s,secret,allowed,denied,pi]=process.argv.slice(1);
function blocked(fn){try{fn();}catch(e){if(['EPERM','EACCES'].includes(e.code))return;throw e;}throw Error('OS permission widened');}
if(Number(process.versions.node.split('.')[0])<24)throw Error('Node 24 required');
const child=spawnSync(r+'/trusted/pi-launcher',[],{encoding:'utf8'});
if(child.status!==0||child.stdout.trim()!=='fixed-launcher-ok')throw Error('fixed launcher denied: '+child.stderr);
if(fs.readFileSync(p+'/safe','utf8')!=='allowed')throw Error('read denied');
if(fs.readFileSync(pi+'/bundle.js','utf8')!=='pinned package')throw Error('package read denied');
fs.accessSync(pi+'/bundle.js',fs.constants.X_OK);
if(fs.readFileSync(p+'/.codex/tasks/workflows/selected/state.json','utf8')!=='bound metadata')throw Error('workflow read denied');
blocked(()=>fs.readFileSync(p+'/.codex/tasks/workflows/unbound/state.json'));
blocked(()=>fs.writeFileSync(pi+'/bundle.js','bad'));
blocked(()=>fs.writeFileSync(p+'/.codex/tasks/workflows/selected/state.json','bad'));
blocked(()=>fs.readFileSync(secret));blocked(()=>fs.readFileSync(p+'/.pi/auth.json'));blocked(()=>fs.readFileSync(p+'/.env.production'));
for(const target of [r+'/trusted/new',r+'/adapter/new',s+'/snapshot.py'])blocked(()=>fs.writeFileSync(target,'bad'));
blocked(()=>fs.renameSync(r+'/trusted',r+'/moved'));blocked(()=>fs.renameSync(r,p+'/runtime'));
fs.symlinkSync(r+'/trusted',r+'/alias');blocked(()=>fs.writeFileSync(r+'/alias/new','bad'));
fs.writeFileSync(r+'/scratch','ok');fs.writeFileSync(p+'/safe','ok');
const permitted=net.connect({host:'127.0.0.1',port:Number(allowed)});await once(permitted,'connect');permitted.destroy();
const no=net.connect({host:'127.0.0.1',port:Number(denied)});no.on('error',()=>{});
try{await once(no,'connect');throw Error('direct egress allowed');}catch(e){if(!['EPERM','EACCES'].includes(e.code))throw e;}
console.log('real-node-default-deny-passed');
"""
            p=subprocess.run(['/usr/bin/sandbox-exec','-p',policy,str(self.node),'--input-type=module','-e',code,
                str(runtime),str(project),str(source),str(secret),str(self.port),str(self.provider.server_address[1]),str(package)],
                cwd=project,env=dict(HOME=str(runtime),PATH='/usr/bin:/bin'),capture_output=True,text=True,timeout=10)
            self.assertEqual(p.returncode,0,p.stderr);self.assertIn('real-node-default-deny-passed',p.stdout)

    def test_new_production_modules_are_registered_for_distribution(self):
        from scripts.prepare_acp_process_candidate import FILES
        data=tomllib.loads((ROOT/'pyproject.toml').read_text())['tool']['setuptools']
        for name in ('pi_production_egress','pi_production_exec'):
            self.assertIn(name,data['py-modules']);self.assertIn(name+'.py',FILES)
        self.assertIn('pi_production_transport.mjs',FILES)

    def test_pinned_transcript_shape_terminal_and_tool_policy(self):
        code="""import assert from 'node:assert/strict';import {payload,decode} from './pi_production_transport.mjs';
const config={model_id:'fixture',output_tokens:128};
const p=payload(config,{messages:[{role:'system',content:'fixed'},{role:'user',content:[{type:'text',text:'synthetic'}]}]});
assert.equal(p.messages[0].content,'fixed');assert.equal(p.max_tokens,128);
assert.throws(()=>payload(config,{messages:[{role:'system',content:[]}]}));
assert.throws(()=>payload(config,{messages:[{role:'user',content:[{type:'image',data:'x'}]}]}));
const model={id:'fixture',api:'fixture',provider:'fixture'};
assert.throws(()=>decode([],model));
assert.throws(()=>decode([JSON.stringify({choices:[{index:0,delta:{},finish_reason:'length'}]}),'[DONE]'],model));
assert.throws(()=>decode([JSON.stringify({choices:[{index:0,delta:{tool_calls:[{index:0,id:'x',function:{name:'bash',arguments:'{}'}}]},finish_reason:'tool_calls'}]}),'[DONE]'],model));
console.log('transcript and terminal policy passed');
"""
        p=subprocess.run([str(self.node),'--input-type=module','-e',code],cwd=ROOT,
            env=dict(HOME=str(self.base),PATH='/usr/bin:/bin'),capture_output=True,text=True,timeout=5)
        self.assertEqual(p.returncode,0,p.stderr)

    def test_production_launcher_reuses_exact_toolchain_and_rejects_changed_source(self):
        import pi_toolchain as policy
        import pi_acp_compat as compat
        from pi_toolchain_support import tools_fixture
        with tempfile.TemporaryDirectory() as folder:
            base=Path(folder).resolve();host=base/'installation';host.mkdir()
            runtime=base/'runtime';runtime.mkdir(mode=0o700)
            project=base/'project';project.mkdir()
            tools,data=tools_fixture(base)
            for name in ('pi_toolchain.py','pi_acp_compat.py','pi_acp_exec.py','pi_coordinator_exec.py',
                    'pi-path-guard.mjs','pi-coordinator-guard.mjs','pi_production_credentials.py',
                    'pi_production_fence.py','pi_production_profile.py','pi_production_egress.py',
                    'pi_production_exec.py','pi_production_transport.mjs'):
                shutil.copyfile(ROOT/name,host/name)
            source=host/'integrations/pi-acp/node_modules/pi-acp/dist/index.js'
            source.parent.mkdir(parents=True);shutil.copyfile(policy.SOURCE,source)
            registry=host/'integrations/pi-acp/compatibility.json';registry.write_text(json.dumps(data))
            compat.derive_directory(runtime/'adapter',role='coordinator')
            with patch.multiple(policy,ROOT=host,REGISTRY=registry,SOURCE=source):
                launcher=policy.write_launch(runtime,project,'coordinator',tools,production=True)
                self.assertIn('pi_production_exec.py',launcher.read_text())
                self.assertEqual(policy.load_launch(runtime,project,'coordinator'),tools)
                p=host/'pi_production_exec.py';p.write_text(p.read_text()+'\n# changed\n')
                with self.assertRaises(ValueError):policy.load_launch(runtime,project,'coordinator')
