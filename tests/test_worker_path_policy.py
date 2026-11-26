"""Real Node guard and OS write-policy probes in a disposable repository."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ato_adapters import agents

ROOT = Path(__file__).resolve().parents[1]


class WorkerPathPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base/'repo'
        self.repo.mkdir()
        self.runtime = self.base/'runtime'
        self.runtime.mkdir(mode=0o700)
        (self.repo/'src').mkdir()
        (self.repo/'src/code.py').write_text('original')
        (self.repo/'subdir').mkdir()
        for name in agents.SENSITIVE_PATHS:
            if name == '.env':
                (self.repo/name).write_text('synthetic secret')
                (self.repo/'subdir'/name).write_text('synthetic secret')
            else:
                for parent in (self.repo, self.repo/'subdir'):
                    (parent/name).mkdir()
                    (parent/name/'private').write_text('synthetic secret')

    def test_node_rejects_sensitive_segments_aliases_and_dangling_links_for_every_tool(self):
        (self.repo/'safe-link').symlink_to(self.repo/'src', target_is_directory=True)
        (self.repo/'secret-alias').symlink_to(self.repo/'.git', target_is_directory=True)
        (self.repo/'dangling').symlink_to(self.repo/'.env-missing'/'../.aws/new')
        (self.repo/'.git/back').symlink_to(self.repo/'src', target_is_directory=True)
        (self.repo/'detour').symlink_to(self.repo/'.git/back', target_is_directory=True)
        outside = self.base/'outside'
        outside.mkdir()
        (self.repo/'escape').symlink_to(outside, target_is_directory=True)
        script = '''
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {allowed} from './pi-path-guard.mjs';
const root=process.argv[1];
const tools=['read','ls','find','grep','edit','write'];
const denied=['.env','.git/config','.codex/tasks/state.json','subdir/.env',
 '.ato/private','.pi/private','.aws/private','.ssh/private','secret-alias/private',
 'dangling','detour/code.py','escape/file','.git/../src/code.py'];
for(const segment of ['.git','.codex','.ato','.pi','.aws','.ssh']) denied.push('subdir/'+segment+'/private');
for(const tool of tools) {
 for(const target of denied) assert.equal(allowed(root,target,tool,false),false,tool+':'+target);
 assert.equal(allowed(root,'src/code.py',tool,false),true,tool);
 assert.equal(allowed(root,'safe-link/code.py',tool,false),true,tool+':safe-link');
}
for(const tool of ['ls','find','grep']) assert.equal(allowed(root,'.',tool,false),true,tool+':root');
assert.equal(allowed(root,'src/code.py','write',true),false);
assert.equal(allowed(root,'src/code.py','read',true),true);
assert.equal(allowed(root,'src/code.py','bash',false),false);
fs.unlinkSync(root+'/.env');fs.symlinkSync(root+'/src/code.py',root+'/.env');
assert.equal(allowed(root,'.env','read',false),false,'sensitive lexical alias must remain forbidden');
console.log('all path-policy assertions passed');
'''
        result = subprocess.run([os.environ.get('CTASK_NODE_BIN', 'node'), '--input-type=module', '-e', script, str(self.repo)],
            cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_tool_call_hook_blocks_explicit_sensitive_paths_and_keeps_root_search(self):
        script = '''
import assert from 'node:assert/strict';
import guard from './pi-path-guard.mjs';
let call;
guard({on:(kind,handler)=>{assert.equal(kind,'tool_call');call=handler;}});
for(const name of ['read','ls','find','grep','edit','write']) {
 assert.equal((await call({toolName:name,input:{path:'.env'}})).block,true);
 assert.equal(await call({toolName:name,input:{path:'src/code.py'}}),undefined);
}
for(const name of ['ls','find','grep']) assert.equal(await call({toolName:name,input:{path:'.'}}),undefined);
assert.equal((await call({toolName:'bash',input:{}})).block,true);
console.log('hook policy passed');
'''
        result = subprocess.run([os.environ.get('CTASK_NODE_BIN', 'node'), '--input-type=module', '-e', script], cwd=ROOT,
            env=dict(os.environ, CTASK_AGENT_REPO=str(self.repo), CTASK_AGENT_SANDBOX='workspace-write'),
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_os_sandbox_denies_all_root_and_nested_sensitive_writes(self):
        targets = [str(p.relative_to(self.repo)) for p in self.repo.rglob('*') if p.is_file() and p.name != 'code.py']
        probe = '''
import errno,json,sys
from pathlib import Path
for name in json.loads(sys.argv[1]):
    try: Path(name).write_text('forbidden')
    except PermissionError: pass
    else: raise AssertionError('sensitive write allowed: '+name)
Path('src/code.py').write_text('allowed')
'''
        profile = agents.sandbox_profile(self.repo, self.runtime, 'workspace-write')
        result = subprocess.run(['/usr/bin/sandbox-exec', '-p', profile, sys.executable, '-c', probe, json.dumps(targets)],
            cwd=self.repo, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.repo/'src/code.py').read_text(), 'allowed')
        self.assertTrue(all((self.repo/name).read_text() == 'synthetic secret' for name in targets))

    def test_doctor_explains_legacy_native_write_grants_without_claiming_acp_equivalence(self):
        fake = subprocess.CompletedProcess([], 0, '{"loggedIn":false}', '')
        with patch('ato_adapters.agents.executable', return_value='/synthetic/agent'), \
                patch('ato_adapters.agents.subprocess.run', return_value=fake), patch('ato_adapters.agents.Path.home', return_value=self.base/'home'):
            rows = {row['kind']: row for row in agents.doctor()}
        for kind in ('claude', 'pi'):
            self.assertIn('OS-writable', rows[kind]['worker_guard'])
            self.assertIn('acp_pi keeps native credentials read-only', rows[kind]['worker_guard'])
        self.assertEqual(rows['codex']['worker_guard'], 'native')
