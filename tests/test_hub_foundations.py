import copy
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import hub_contract as contract
import hub_ledger as ledger
import hub_sandbox as sandbox
import hub_worktree as worktree
from hub_support import git,repository,runtime,spec,verdict


class HubFoundationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ato-h1-foundation-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.repo=repository(self.root)
        context=patch.dict(os.environ,dict(ATO_HOME=str(self.root/'ato'),CODEX_HOME=str(self.root/'native')))
        context.start();self.addCleanup(context.stop)

    def test_O1_strict_JSON_verdict_and_blocker_consistency(self):
        self.assertEqual(contract.validate_review_verdict(verdict()),verdict())
        for value in ('{"x":1,"x":2}','{"x":NaN}','{"x":Infinity}','{"x":1} trailing','{'):
            with self.subTest(value=value),self.assertRaises(contract.HubError):contract.strict_json(value)
        for value in (verdict('approve','blocker'),verdict('request_changes','minor'),dict(verdict(),unknown=True)):
            with self.assertRaises(contract.HubError):contract.validate_review_verdict(value)
        a=verdict('request_changes','blocker');b=copy.deepcopy(a);b['findings'][0]['title']='  FIX THE   fixture '
        self.assertEqual(contract.blocker_fingerprint('a'*40,a),contract.blocker_fingerprint('a'*40,b))
        for path in ('../escape','/absolute','x/../y','-flag'):
            value=copy.deepcopy(a);value['findings'][0]['file']=path
            with self.assertRaises(contract.HubError):contract.validate_review_verdict(value)

    def test_O1_spec_manual_gate_and_no_flag_injection(self):
        value=spec(self.repo);self.assertIs(contract.validate_task_spec(value),value)
        for change in (dict(schema_version=True),dict(unknown=True),dict(branch='main')):
            with self.assertRaises(contract.HubError):contract.validate_task_spec(dict(value,**change))
        value['worker_contract']['acceptance']=value['worker_contract']['acceptance'][:1]
        with self.assertRaisesRegex(ValueError,'FINAL_MANUAL'):contract.validate_task_spec(value)
        for kwargs in (dict(kind='agy',model='--model'),dict(kind='other'),dict(kind='agy',effort='--flag')):
            with self.assertRaises(ValueError):contract.HubAgentSpec(**kwargs)

    def test_task_id_matches_worktree_identifier_before_provision(self):
        for value in ('a','A0_-','x'*128):
            with self.subTest(value=value):
                self.assertEqual(contract.validate_task_spec(spec(self.repo,task=value))['task_id'],value)
        for value in ('task.one','.', '..','a/b','-start','_start','x'*129,'',None,True):
            with self.subTest(value=value),self.assertRaises(contract.HubError):
                contract.validate_task_spec(dict(spec(self.repo),task_id=value))
        self.assertEqual(contract.token('finding.1'),'finding.1')

    def test_O4_provision_fixed_commit_dependency_and_collision_refusals(self):
        (self.repo/'.venv').mkdir();(self.repo/'.venv/marker').write_text('synthetic dependency')
        base=git(self.repo,'rev-parse','HEAD')
        binding=worktree.provision(self.repo,'one','feat/one','main',shared_env_links=['.venv'])
        path=Path(binding['path'])
        self.assertEqual(git(path,'rev-parse','HEAD'),base);self.assertEqual(binding['base_commit'],base)
        self.assertTrue((path/'.venv').is_symlink());self.assertEqual((path/'.venv/marker').read_text(),'synthetic dependency')
        for task,branch in (('one','feat/two'),('two','feat/one')):
            with self.assertRaises(ValueError):worktree.provision(self.repo,task,branch,'main')
        for links in (['result.txt'],['.venv','.venv'],['integrations/pi-acp/node_modules']):
            with self.assertRaises(ValueError):worktree.provision(self.repo,'bad','feat/bad','main',shared_env_links=links)
        worktree.cleanup(binding);self.assertFalse(path.exists())
        self.assertEqual(git(self.repo,'rev-parse','refs/heads/feat/one'),base)

    def test_O4_tracked_dependency_refuses_before_creation(self):
        (self.repo/'.venv').mkdir();(self.repo/'.venv/file').write_text('not a shared dependency')
        git(self.repo,'add','-f','.venv/file');git(self.repo,'commit','-qm','tracked dependency')
        with self.assertRaises(ValueError):worktree.provision(self.repo,'bad','feat/bad','main',shared_env_links=['.venv'])
        self.assertFalse((self.root/'project-hub-bad').exists())

    def test_O3_tripwire_dirty_content_new_file_and_HEAD(self):
        before=worktree.snapshot_integrity(self.repo)
        (self.repo/'result.txt').write_text('changed')
        first=worktree.snapshot_integrity(self.repo);(self.repo/'result.txt').write_text('changed twice')
        second=worktree.snapshot_integrity(self.repo)
        self.assertEqual(first['status_porcelain'],second['status_porcelain'])
        for value in (first,second):
            with self.assertRaisesRegex(ValueError,'REVIEWER_MUTATION'):worktree.assert_integrity(before,value)
        git(self.repo,'checkout','--','result.txt');(self.repo/'new.txt').touch()
        with self.assertRaises(ValueError):worktree.assert_integrity(before,worktree.snapshot_integrity(self.repo))
        (self.repo/'new.txt').unlink();git(self.repo,'commit','--allow-empty','-qm','head moved')
        with self.assertRaises(ValueError):worktree.assert_integrity(before,worktree.snapshot_integrity(self.repo))

    def test_O4_cleanup_refuses_dirty_locked_and_mismatched_bindings(self):
        binding=worktree.provision(self.repo,'one','feat/one','main');path=Path(binding['path'])
        (path/'new').touch()
        with self.assertRaisesRegex(ValueError,'DIRTY'):worktree.cleanup(binding)
        (path/'new').unlink();lock=path/'.codex/tasks/.worker.lock';lock.parent.mkdir(parents=True)
        with lock.open('w') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX)
            with self.assertRaisesRegex(ValueError,'BUSY'):worktree.cleanup(binding)
        with self.assertRaises(ValueError):worktree.cleanup(dict(binding,branch='wrong'))
        worktree.cleanup(binding)

    def test_O3_real_macos_OS_denies_all_authority_writes_and_allows_scratch(self):
        binding=worktree.provision(self.repo,'one','feat/one','main');path=Path(binding['path']);rt=runtime(self.root)
        alias=rt/'tmp/alias';alias.symlink_to(self.repo/'result.txt')
        targets=[path/'probe',self.repo/'probe',self.repo/'.git/probe',rt/'trusted/probe',alias]
        code="import json,pathlib,sys\nr=[]\nfor p in sys.argv[1:-1]:\n try:pathlib.Path(p).write_text('bad');r.append(False)\n except PermissionError:r.append(True)\npathlib.Path(sys.argv[-1]).write_text('allowed')\nprint(json.dumps(r))\n"
        profile=sandbox.readonly_profile(rt,protected=(self.repo,path))
        result=sandbox.run(['/usr/bin/sandbox-exec','-p',profile,sys.executable,'-c',code,*map(str,targets),str(rt/'tmp/allowed')],
            workspace=path,runtime=rt,environment=sandbox.clean_environment(rt/'home',rt/'tmp'),timeout=5)
        self.assertEqual(result.code,'OK',result.stderr);self.assertEqual(json.loads(result.stdout),[True]*5)
        self.assertTrue(all(not p.exists() for p in targets[:-1]));self.assertEqual((self.repo/'result.txt').read_text(),'initial')
        self.assertEqual((rt/'tmp/allowed').read_text(),'allowed');self.assertEqual((rt/'stderr.log').stat().st_mode&0o777,0o600)

    def test_O3_stop_and_TERM_ignoring_real_owned_process_are_bounded(self):
        rt=runtime(self.root);started=time.monotonic()
        code='import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'
        result=sandbox.run([sys.executable,'-c',code],workspace=self.repo,runtime=rt,
            environment=sandbox.clean_environment(rt/'home',rt/'tmp'),timeout=10,stop=lambda:time.monotonic()-started>.4)
        self.assertEqual(result.code,'STOPPED');self.assertTrue(result.cleaned)
        self.assertLess(time.monotonic()-started,5);self.assertEqual(result.returncode,-signal.SIGKILL)

    def test_O7_private_ledger_switch_rollover_and_immutable_spec(self):
        book=ledger.HubLedger.create(self.repo,contract.HubAgentSpec('agy'),max_turns=1)
        with book.locked():
            path=book.create_task(spec(self.repo));book.record_session('agy-one','fresh_only')
            caps=contract.HubCapabilities({name:dict(state='supported',evidence='synthetic') for name in contract.CAPABILITIES})
            sid,mode,provenance=book.session(caps,'resume_if_supported')
            self.assertIsNone(sid);self.assertEqual(provenance,'ledger_rollover')
            book.record_session('agy-two',provenance);book.switch_agent(contract.HubAgentSpec('claude'),'fixture switch')
            self.assertIsNone(book.session(caps,'resume_if_supported')[0]);self.assertIn('agy',book.hub()['native_sessions'])
            book.record_session('claude-one','ledger_reconstruction_from_agy')
            book.switch_agent(contract.HubAgentSpec('codex'),'fixture switch')
            self.assertIsNone(book.session(caps,'resume_if_supported')[0])
            self.assertIn('Synthetic offline change',book.brief());self.assertTrue(book.busy())
        self.assertFalse(book.busy())
        for file in book.root.rglob('*'):
            self.assertEqual(file.stat().st_mode&0o777,0o700 if file.is_dir() else 0o600)
        value=json.loads((path/'spec.json').read_text());value['branch']='tampered';(path/'spec.json').write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'FROZEN_SPEC'):book.read_task('task-one')

    def test_O7_unknown_resume_falls_back_and_required_resume_refuses(self):
        book=ledger.HubLedger.create(self.repo,contract.HubAgentSpec('claude'))
        book.record_session('claude-one','fresh_only')
        caps=contract.HubCapabilities({name:dict(state='unknown',evidence='not qualified') for name in contract.CAPABILITIES})
        self.assertEqual(book.session(caps,'resume_if_supported')[:2],(None,'fresh_only'))
        with self.assertRaisesRegex(ValueError,'RESUME_NOT_QUALIFIED'):book.session(caps,'require_resume')

    def test_O7_symlink_ledger_and_worktree_paths_never_adopt(self):
        book=ledger.HubLedger.create(self.repo,contract.HubAgentSpec('agy'))
        actual=book.root/'hub.json';backup=book.root/'backup.json';actual.rename(backup);actual.symlink_to(backup)
        with self.assertRaises(ValueError):book.hub()
        alias=self.root/'alias';alias.symlink_to(self.repo,target_is_directory=True)
        with self.assertRaises(ValueError):worktree.repository(alias)

    def test_O7_fresh_sessions_bound_retired_history_and_diff_inline_cap(self):
        book=ledger.HubLedger.create(self.repo,contract.HubAgentSpec('claude'))
        for index in range(260):
            book.record_session(f'claude-{index}','fresh_only')
        self.assertEqual(len(book.hub()['native_sessions']['claude']['retired_sessions']),256)
        binding=worktree.provision(self.repo,'cap','feat/cap','main')
        (Path(binding['path'])/'result.txt').write_text('x'*(260*1024))
        with self.assertRaisesRegex(ValueError,'REVIEW_DIFF_BOUND_EXCEEDED'):
            worktree.diff_bundle(binding)

    def test_O8_loopback_proxy_opt_in_rejects_remote_or_credentialed_urls(self):
        self.assertEqual(sandbox.validate_loopback_proxy('http://127.0.0.1:7890'),'http://127.0.0.1:7890')
        self.assertEqual(sandbox.validate_loopback_proxy('socks5h://localhost:7890'),'socks5h://localhost:7890')
        for bad in ('http://evil.example:8080','http://@127.0.0.1:7890','http://:@127.0.0.1:7890',
                    'http://user@127.0.0.1:7890','http://:pass@127.0.0.1:7890','http://@localhost:7890',
                    'http://@[::1]:7890','http://user:pass@127.0.0.1:7890','http://127.0.0.1:7890/path',
                    'http://127.0.0.1','ftp://127.0.0.1:21','http://127.0.0.1:7890?a=1'):
            with self.subTest(bad=bad),self.assertRaisesRegex(ValueError,'INVALID_LOOPBACK_PROXY'):
                sandbox.validate_loopback_proxy(bad)
        rt=runtime(self.root)
        env=sandbox.clean_environment(rt/'home',rt/'tmp',proxy='http://127.0.0.1:7890')
        self.assertEqual(env['HTTPS_PROXY'],'http://127.0.0.1:7890')
        self.assertNotIn('HTTPS_PROXY',sandbox.clean_environment(rt/'home',rt/'tmp'))
