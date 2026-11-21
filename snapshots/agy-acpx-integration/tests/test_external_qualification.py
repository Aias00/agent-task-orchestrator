"""Qualification regressions: real public transport and native policy, no model."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import acpx_profile
import runner
import test_acpx_worker as worker_tests
from external_support import ExternalCase, ROOT
import external_coordinator as external


class FinalMessageQualification(unittest.TestCase):
    def test_pinned_phase_parser_rejects_missing_changed_and_multiple_finals(self):
        program = '''
import assert from 'node:assert/strict';
import { CodexFinalText } from './acpx_bridge.mjs';
const chunk = (id, phase, text) => ({sessionUpdate:'agent_message_chunk', messageId:id,
  _meta:{codex:{phase}},content:{type:'text',text}});
const ok = new CodexFinalText();
ok.accept(chunk('c','commentary','editing')); ok.accept(chunk('f','final_answer','{"status":'));
ok.accept(chunk('f','final_answer','"complete"}'));
assert.equal(ok.result(), '{"status":"complete"}');
for (const chunks of [[], [chunk('f',undefined,'{}')],
  [chunk('a','final_answer','{}'),chunk('b','final_answer','{}')],
  [chunk('f','commentary','{}'),chunk('f','final_answer','{}')],
  [chunk('f','final_answer','{}'),chunk('c','commentary','late')]]) {
  const p = new CodexFinalText(); for (const c of chunks) p.accept(c);
  assert.throws(()=>p.result());
}
'''
        subprocess.run([str(acpx_profile.NODE), '--input-type=module', '-e', program],
                       cwd=ROOT, check=True, capture_output=True, timeout=10)

    def test_native_gateway_replaces_legacy_grants_and_refuses_replay(self):
        program = '''
import assert from 'node:assert/strict';
import { NativePolicy } from './acpx_native_proxy.mjs';
const p = new NativePolicy({repo:'/fixture',extends:':workspace',native_config:{frozen:true},schema:{type:'object'}});
const start = p.outbound({id:1,method:'thread/start',params:{cwd:'/fixture',config:{evil:true}}});
assert.equal(start.params.permissions,'ato-worker'); assert.deepEqual(start.params.config,{frozen:true});
assert.throws(()=>p.outbound({id:2,method:'turn/start',params:{threadId:'x',input:[]}}));
p.inbound({id:1,result:{activePermissionProfile:{id:'ato-worker',extends:':workspace'},approvalPolicy:'never',cwd:'/fixture',thread:{id:'x'}}});
const turn = p.outbound({id:3,method:'turn/start',params:{threadId:'x',input:[{type:'text',text:'fixture'}],
  sandboxPolicy:{type:'dangerFullAccess'},cwd:'/evil',runtimeWorkspaceRoots:['/evil'],approvalPolicy:'on-request'}});
assert.equal(turn.params.permissions,'ato-worker');assert.equal(turn.params.approvalPolicy,'never');
assert.equal(turn.params.sandboxPolicy,undefined);assert.equal(turn.params.cwd,undefined);
assert.deepEqual(turn.params.runtimeWorkspaceRoots,['/fixture']);
assert.deepEqual(turn.params.outputSchema,{type:'object'});
assert.throws(()=>p.outbound({id:4,method:'turn/start',params:{threadId:'x',input:[]}}));
for(const method of ['account/login/start','account/logout','thread/resume','config/value/write','review/start','command/exec'])
  assert.throws(()=>p.outbound({id:5,method,params:{}}));
assert.ok(p.inbound({id:7,method:'item/permissions/requestApproval',params:{}}).deny.error);
const bad = new NativePolicy({repo:'/fixture',extends:':workspace',native_config:{},schema:{}});
bad.outbound({id:1,method:'thread/start',params:{cwd:'/fixture'}});
assert.throws(()=>bad.inbound({id:1,result:{thread:{id:'x'}}}));
'''
        subprocess.run([str(acpx_profile.NODE), '--input-type=module', '-e', program],
                       cwd=ROOT, check=True, capture_output=True, timeout=10)

    def test_private_dependency_config_is_a_locator_and_pins_remain_code_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp).resolve()/'paths.json'
            p.write_text(json.dumps(dict(version=1,node=str(Path('/bin/sh').resolve()),archive=str(Path('/tmp/missing').resolve()),deps=str(Path('/tmp').resolve()),native_home=str(Path('/tmp/native').resolve()))))
            p.chmod(0o600)
            with patch.dict(os.environ, {'ATO_ACPX_CONFIG':str(p)}):
                paths = acpx_profile.operator_paths()
                with self.assertRaises(acpx_profile.ProfileUnavailable):
                    acpx_profile.dependencies(paths)
                p.chmod(0o644)
                with self.assertRaises(acpx_profile.ProfileUnavailable):
                    acpx_profile.operator_paths()

    def test_native_named_profile_actual_filesystem_boundary(self):
        # Host runs this unchanged. Nested worker Seatbelt cannot apply a second
        # sandbox; that restriction is an explicit host check, never a skip.
        with tempfile.TemporaryDirectory(prefix='ato-named-policy-') as tmp:
            root = Path(tmp).resolve(); repo = root/'repo'; repo.mkdir()
            subprocess.run(['git','init','-q',str(repo)],check=True,capture_output=True,timeout=5)
            home = root/'native'; home.mkdir(mode=0o700)
            authority = root/'authority'; authority.mkdir(); (authority/'proof').write_text('private')
            for name in ('.git', '.codex', '.agents'):
                (repo/name).mkdir(exist_ok=True); (repo/name/'proof').write_text('private')
            (repo/'alias').symlink_to(authority, target_is_directory=True)
            native = acpx_profile.DEPS/'@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex'
            self.assertEqual(acpx_profile.sha256(native), acpx_profile.NATIVE_SHA256)
            for mode in ('workspace-write','read-only'):
                cfg = acpx_profile.native_policy(repo,[authority,repo/'.codex'],mode,
                                                acpx_profile.trusted_tool_path(acpx_profile.NODE))
                args = acpx_profile.native_args(cfg)[1:]
                code = '''from pathlib import Path
import sys, subprocess, os
repo,authority,mode=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
for path in [authority/'proof',repo/'alias/proof',repo/'.codex/proof']:
 try:path.read_text()
 except PermissionError:pass
 else:raise AssertionError('authority readable')
for path in [authority/'write',repo/'.git/write',repo/'.codex/write',repo/'.agents/write']:
 try:path.write_text('bad')
 except PermissionError:pass
 else:raise AssertionError('authority writable')
for name in ('.git','.agents'):assert (repo/name/'proof').read_text()=='private'
git=subprocess.run(['/usr/bin/git','status','--porcelain'],cwd=repo,capture_output=True)
assert git.returncode==0
node=subprocess.run(['node','-e','process.stdout.write(String(1+1))'],capture_output=True,text=True)
assert node.returncode==0 and node.stdout=='2'
if mode=='workspace-write':(repo/'normal.txt').write_text('allowed')
else:
 try:(repo/'readonly.txt').write_text('bad')
 except PermissionError:pass
 else:raise AssertionError('read-only writable')
'''
                env=dict(PATH=acpx_profile.trusted_tool_path(acpx_profile.NODE),
                         HOME=str(root),CODEX_HOME=str(home),TMPDIR=str(root))
                result=subprocess.run([str(native),'sandbox','--permission-profile','ato-worker',*args,
                    '-C',str(repo),'--',sys.executable,'-I','-c',code,str(repo),str(authority),mode],
                    env=env,capture_output=True,text=True,timeout=15)
                self.assertEqual(result.returncode,0,result.stderr)


class OriginCleanupQualification(unittest.TestCase):
    def test_reused_missing_or_nonleader_groups_are_never_signalled(self):
        from scripts import external_host_smoke as smoke
        import signal
        for actual, group in [('reused', 71), (None, 71), ('original', 72)]:
            with self.subTest(actual=actual, group=group), \
                    patch.object(runner,'process_identity',return_value=actual), \
                    patch.object(smoke.os,'getpgid',return_value=group), \
                    patch.object(smoke.os,'killpg') as kill:
                smoke.signal_owned_groups({71:'original'},signal.SIGTERM)
                kill.assert_not_called()
        groups={71:'original'}
        with patch.object(runner,'process_identity',return_value='reused'), \
                patch.object(smoke.os,'getpgid',return_value=71):
            smoke.remember_group(groups,71,'reused')
        self.assertEqual(groups,{71:'original'})

    def test_cleanup_rechecks_birth_between_term_and_kill(self):
        from scripts import external_host_smoke as smoke
        import signal
        with patch.object(runner,'process_identity',side_effect=['original','reused']), \
                patch.object(smoke.os,'getpgid',return_value=71), \
                patch.object(smoke.os,'killpg') as kill:
            smoke.signal_owned_groups({71:'original'},signal.SIGTERM)
            smoke.signal_owned_groups({71:'original'},signal.SIGKILL)
        kill.assert_called_once_with(71,signal.SIGTERM)
        with patch.object(runner,'process_identity',return_value='original'), \
                patch.object(smoke.os,'getpgid',side_effect=ProcessLookupError), \
                patch.object(smoke.os,'killpg') as kill:
            smoke.signal_owned_groups({71:'original'},signal.SIGTERM)
        kill.assert_not_called()


class ScopeQualification(ExternalCase):
    def test_nested_stage_grants_handoffs_budgets_and_worker_overrides_are_rejected(self):
        for key,value in [('host_handoff',dict(source='model',instructions='host work')),
                          ('permissions',dict(source='model',network_access=True)),
                          ('budget',dict()),('sandbox','danger-full-access'),
                          ('memory',True),('agent',dict(kind='codex'))]:
            with self.subTest(key=key):
                contract=self.contract();contract['stages'][0]['plan'][key]=value
                with self.assertRaises(ValueError):external.reviewed_contract(contract)


# Reuse fixture lifecycle setup without inheriting its entire test class twice.
class TransportQualification(unittest.TestCase):
    setUp = worker_tests.AcpxWorkerTests.setUp
    cli = worker_tests.AcpxWorkerTests.cli
    create = worker_tests.AcpxWorkerTests.create
    run_case = worker_tests.AcpxWorkerTests.run_case
    def test_commentary_before_final_through_actual_public_runtime(self):
        directory,state=self.run_case('commentary')
        self.assertEqual(state['status'],'completed')
        receipt=runner.read(next((directory/'checkpoints').glob('*.acpx-runtime/receipt.json')))
        self.assertTrue(receipt['result_schema_valid'])

class GatewayProcessQualification(unittest.TestCase):
    def test_real_proxy_subprocess_forwards_only_frozen_native_turn(self):
        with tempfile.TemporaryDirectory(prefix='ato-proxy-fixture-') as tmp:
            root=Path(tmp).resolve()
            fake=root/'native.mjs'
            fake.write_text('''import fs from 'node:fs';import readline from 'node:readline';
readline.createInterface({input:process.stdin}).on('line',line=>{
 const m=JSON.parse(line); let result={};
 if(m.method==='thread/start') result={activePermissionProfile:{id:'ato-worker',extends:':workspace'},approvalPolicy:'never',cwd:process.cwd(),thread:{id:'fixture-native'}};
 if(m.method==='turn/start'){fs.writeFileSync('received.json',JSON.stringify(m.params));result={turn:{id:'fixture-turn'}};}
 if(m.id!==undefined)process.stdout.write(JSON.stringify({id:m.id,result})+'\\n');
});''')
            program='''import {proxy} from './acpx_native_proxy.mjs';
await proxy(JSON.parse(process.argv[1]));'''
            cfg=dict(version=1,native=str(acpx_profile.NODE),repo=str(root),schema={'type':'object'},
                extends=':workspace',native_config={'frozen':True},args=[str(fake)],env={'PATH':'/usr/bin:/bin'})
            # Wait for acknowledged thread before sending turn, as the pinned
            # adapter does. Pipes exercise actual gateway lifecycle/EOF cleanup.
            p=subprocess.Popen([str(acpx_profile.NODE),'--input-type=module','-e',program,json.dumps(cfg)],
                cwd=ROOT,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
            try:
                p.stdin.write(json.dumps(dict(id=1,method='thread/start',params={'cwd':str(root)}))+'\n');p.stdin.flush()
                self.assertEqual(json.loads(p.stdout.readline())['result']['thread']['id'],'fixture-native')
                p.stdin.write(json.dumps(dict(id=2,method='turn/start',params=dict(threadId='fixture-native',
                    input=[dict(type='text',text='fixture')],sandboxPolicy={'type':'dangerFullAccess'})))+'\n');p.stdin.flush()
                self.assertIn('result',json.loads(p.stdout.readline()))
                p.stdin.close();p.wait(timeout=5)
                self.assertEqual(p.returncode,0,p.stderr.read())
                received=json.loads((root/'received.json').read_text())
                self.assertEqual(received['permissions'],'ato-worker')
                self.assertNotIn('sandboxPolicy',received)
                self.assertEqual(received['approvalPolicy'],'never')
            finally:
                if p.poll() is None:p.kill();p.wait(timeout=5)
                p.stdout.close();p.stderr.close()


class BundleQualification(unittest.TestCase):
    def test_portable_bundle_checks_source_and_rejects_tamper_or_symlinks(self):
        import importlib.util
        import shutil
        spec=importlib.util.spec_from_file_location('external_candidate',ROOT/'scripts/prepare_external_candidate.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='ato-portable-bundle-') as tmp:
            root=Path(tmp).resolve()
            for name in module.collect(ROOT):
                target=root/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,target)
            manifest=module.prepare(root);module.check(root)
            import tarfile
            extracted=root/'extracted'
            with tarfile.open(root/'build/external-coordinator/candidate.tar.gz') as bundle:
                bundle.extractall(extracted,filter='data')
            module.check(extracted,archive=root/'build/external-coordinator/candidate.tar.gz')
            first=(root/'build/external-coordinator/candidate.tar.gz').read_bytes()
            module.prepare(root)
            self.assertEqual(first,(root/'build/external-coordinator/candidate.tar.gz').read_bytes())
            self.assertFalse(manifest['delivery']['state_included'])
            (root/'runner.py').write_text('changed')
            with self.assertRaises(ValueError):module.check(root)
            (root/'runner.py').unlink();(root/'runner.py').symlink_to(ROOT/'runner.py')
            with self.assertRaises(ValueError):module.collect(root)

class PinnedAdapterQualification(unittest.TestCase):
    def test_actual_pinned_codex_acp_gateway_and_final_metadata_with_offline_native(self):
        import shlex
        with tempfile.TemporaryDirectory(prefix='ato-pinned-adapter-') as tmp:
            root=Path(tmp).resolve();home=root/'native';home.mkdir()
            policy=root/'policy.json'
            policy.write_text(json.dumps(dict(version=1,native=str(acpx_profile.NODE),repo=str(root),
                args=[str(ROOT/'tests/fixtures/native_appserver_fixture.mjs')],
                env=dict(HOME=str(root),CODEX_HOME=str(home),PATH='/usr/bin:/bin'),
                native_config={'frozen':True},extends=':workspace',schema=runner.SCHEMA)))
            launcher=root/'native-proxy'
            launcher.write_text('#!/bin/sh\nexec '+shlex.quote(str(acpx_profile.NODE))+' '+shlex.quote(str(ROOT/'acpx_native_proxy.mjs'))+' "$@"\n');launcher.chmod(0o700)
            cfg=dict(**acpx_profile.dependencies(),version=1,agent='pinned-fixture',
                correlation=dict(turn='offline-pinned-fixture'),timeout_ms=10000,
                cwd=str(root),stop_file=str(root/'STOP'),text='Offline synthetic protocol qualification.',
                final_parser='codex-1.1.5',required_mode='agent',
                argv=[str(acpx_profile.NODE),str(acpx_profile.DEPS/'@agentclientprotocol/codex-acp/dist/index.js')],
                agent_env=dict(HOME=str(root),CODEX_HOME=str(home),PATH='/usr/bin:/bin',
                    CODEX_PATH=str(launcher),ATO_NATIVE_POLICY=str(policy),INITIAL_AGENT_MODE='agent',
                    CODEX_CONFIG='{}'))
            result=subprocess.run([str(acpx_profile.NODE),str(ROOT/'acpx_bridge.mjs')],
                input=json.dumps(cfg),capture_output=True,text=True,cwd=root,
                env=dict(HOME=str(root),CODEX_HOME=str(home),PATH='/usr/bin:/bin'),timeout=18)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            payload=json.loads(result.stdout)
            self.assertEqual(payload['protocol'],'completed')
            self.assertEqual(json.loads(payload['text'])['status'],'ready_for_checks')
            turn=runner.read(root/'native-turn.json')
            self.assertEqual(turn['permissions'],'ato-worker')
            self.assertNotIn('sandboxPolicy',turn)
            self.assertEqual(turn['outputSchema'],runner.SCHEMA)
            self.assertNotIn('thread/goal/get',(root/'native-methods.jsonl').read_text())
