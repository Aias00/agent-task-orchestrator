import ast
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import hub_cli
from hub_contract import HubAgentSpec
from hub_ledger import HubLedger
from hub_loop import HubLoop
from hub_registry import HubRegistry
from hub_adapter_agy import AgyHubAdapter
from hub_adapter_claude import ClaudeHubAdapter
from hub_adapter_codex import CodexHubAdapter
import hub_worktree
import runner
from hub_support import repository,spec,verdict,git
from test_hub_adapters import adapter_fixture

ROOT=Path(__file__).resolve().parents[1]


class HubLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ato-h1-loop-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.repo=repository(self.root)
        self.secret='synthetic-H1-loop-private-secret-42993'
        self.scope=patch.dict(os.environ,dict(ATO_HOME=str(self.root/'ato'),H1_SECRET=self.secret,
            CODEX_THREAD_ID=self.secret,CTASK_SESSION_REF=self.secret,PYTHONPATH=self.secret,
            NODE_OPTIONS=self.secret,DYLD_INSERT_LIBRARIES=self.secret))
        self.scope.start();self.addCleanup(self.scope.stop)
        self.worker=self.root/'synthetic-worker'
        self.worker.write_text('#!'+str(Path(sys.executable).resolve())+'\n'+(ROOT/'tests/fixtures/hub_worker_fixture.py').read_text())
        self.worker.chmod(0o700);self.worker_config(mode='ok')
        self.adapter=adapter_fixture(self.root,AgyHubAdapter,dict(verdict=verdict(),expect_brief=True,sentinel=self.secret))
        self.registry=HubRegistry([self.adapter])
        self.ledger=HubLedger.create(self.repo,HubAgentSpec('agy'))
        self.loop=HubLoop(self.ledger,self.registry,worker_tools=dict(CTASK_CODEX_BIN=str(self.worker)))
        self.original=git(self.repo,'rev-parse','HEAD')

    def worker_config(self,**config):Path(str(self.worker)+'.json').write_text(json.dumps(config))

    def reviewer(self,**config):
        (self.adapter.home/'fixture.json').write_text(json.dumps(dict(verdict=verdict(),expect_brief=True,sentinel=self.secret,**config)))

    def create(self,**options):
        value=spec(self.repo,**options)
        value['worker_contract']['limits']=dict(runtime_seconds=20,attempt_seconds=8,check_seconds=3,
            max_attempts=1,max_stages=3,max_planning_attempts=1,max_wait_seconds=1)
        self.loop.create_task(value);return value

    def state(self,identifier='task-one'):return self.ledger.read_task(identifier)[1]

    def test_O5_real_workflow_host_checks_review_fix_then_human_terminal(self):
        self.reviewer(sequence=[verdict('request_changes','blocker'),verdict()])
        original=self.create();handlers=[signal.getsignal(s) for s in (signal.SIGINT,signal.SIGTERM)]
        state=self.loop.run('task-one');self.assertEqual(state['status'],'ready_for_user_review',state)
        self.assertEqual(state['review_round'],2);self.assertEqual(len(state['workflow_history']),2)
        worktree=Path(state['worktree']['path']);self.assertEqual((worktree/'result.txt').read_text(),'done-fixed')
        frozen=self.ledger.read_task('task-one')[0]['worker_contract']
        for name in state['workflow_history']:
            directory=Path(name);contract=json.loads((directory/'contract.json').read_text())
            for key in ('acceptance','authorization','sandbox','agent','memory'):
                self.assertEqual(contract[key],frozen[key])
            workflow_state=json.loads((directory/'state.json').read_text())
            self.assertEqual(workflow_state['status'],'needs_input')
            self.assertEqual(workflow_state['criteria']['human']['status'],'needs_input')
            self.assertEqual(list((directory/'acknowledgements').iterdir()),[])
        self.assertEqual(git(self.repo,'rev-parse','main'),self.original)
        self.assertEqual(git(self.repo,'status','--porcelain'),'')
        self.assertEqual(self.loop.run('task-one'),self.state())
        self.assertEqual([signal.getsignal(s) for s in (signal.SIGINT,signal.SIGTERM)],handlers)
        self.assertTrue(worktree.exists());self.assertEqual(state['notice'],'recorded_only')
        self.assertEqual(original['worker_contract']['acceptance'],frozen['acceptance'])

    def test_O6_round_cap_and_repeated_source_blockers_stop(self):
        self.reviewer(sequence=[verdict('request_changes','blocker')])
        self.create(rounds=1);state=self.loop.run('task-one')
        self.assertEqual((state['status'],state['code']),('blocked','review_loop_exhausted'))
        self.assertEqual(len(state['workflow_history']),1)
        self.worker_config(mode='unchanged');self.create(task='repeated',rounds=4)
        state=self.loop.run('repeated')
        self.assertEqual(state['code'],'repeated_blockers',state);self.assertEqual(state['review_round'],2)
        self.assertEqual(len(state['workflow_history']),2)

    def test_O6_worker_failures_and_explicit_blocker_never_call_reviewer(self):
        for mode in ('fail','blocked'):
            with self.subTest(mode=mode):
                self.worker_config(mode=mode);self.create(task=mode)
                with patch.object(self.adapter,'run_turn',side_effect=AssertionError('Reviewer must not run')):
                    state=self.loop.run(mode)
                self.assertEqual(state['status'],'blocked',state);self.assertEqual(state['review_round'],0)

    def test_O6_reviewer_blocked_auth_and_mutation_immediately_stop(self):
        for mode in ('blocked','auth','mutation'):
            with self.subTest(mode=mode):
                self.create(task=mode)
                if mode=='blocked':self.reviewer(sequence=[verdict('blocked')])
                elif mode=='auth':self.reviewer(mode='auth')
                else:
                    codex=adapter_fixture(self.root,CodexHubAdapter,dict(verdict=verdict(),mode='mutate'))
                    self.registry.register(codex);self.ledger.switch_agent(HubAgentSpec('codex'),'synthetic mutation test')
                state=self.loop.run(mode);self.assertEqual(state['status'],'blocked',state)
                if mode=='mutation':self.assertEqual(state['code'],'REVIEWER_MUTATION_VIOLATION')
                self.assertEqual(state['review_round'],1)

    def test_O6_STOP_reaches_real_TERM_ignoring_worker_group(self):
        pids=self.root/'worker-pids.json';self.worker_config(mode='stall',pids=str(pids));self.create()
        errors=[]
        def stop_when_started():
            try:
                deadline=time.monotonic()+8
                while not pids.exists() and time.monotonic()<deadline:time.sleep(.02)
                if not pids.exists():raise AssertionError('worker did not start')
                self.loop.stop('task-one')
            except Exception as error:errors.append(error)
        thread=threading.Thread(target=stop_when_started);thread.start()
        started=time.monotonic()
        try:state=self.loop.run('task-one')
        finally:thread.join(timeout=10)
        self.assertFalse(errors,errors);self.assertFalse(thread.is_alive())
        self.assertIn(state['status'],('stopped','cleanup_blocked'),state)
        self.assertLess(time.monotonic()-started,12)
        for pid in json.loads(pids.read_text()):
            deadline=time.monotonic()+2
            while runner.alive(pid) and time.monotonic()<deadline:time.sleep(.02)
            self.assertFalse(runner.alive(pid))
        self.assertEqual(state['review_round'],0)
        self.assertEqual(self.loop.run('task-one')['status'],state['status'])

    def test_O6_interrupted_saved_operation_does_not_replay(self):
        self.create();state=self.state();state['status']='running_worker';self.ledger.update_task('task-one',state)
        with patch('hub_worktree.provision',side_effect=AssertionError('must not replay')):
            state=self.loop.run('task-one')
        self.assertEqual(state['code'],'INTERRUPTED_OPERATION');self.assertIsNone(state['worktree'])
        with patch('hub_worktree.provision',side_effect=AssertionError('must not replay via review-only')):
            self.assertEqual(self.loop.run('task-one',review_only=True)['code'],'INTERRUPTED_OPERATION')

    def test_O6_unknown_reviewer_cleanup_cannot_be_replayed_via_review_only(self):
        self.create();state=self.state();state['status']='cleanup_blocked';state['code']='CLEANUP_BLOCKED'
        self.ledger.update_task('task-one',state)
        with patch.object(self.adapter,'run_turn',side_effect=AssertionError('unknown process must stay blocked')):
            self.assertEqual(self.loop.run('task-one',review_only=True)['status'],'cleanup_blocked')

    def test_O7_real_adapter_switches_and_rollover_keep_vendor_sessions_separate(self):
        self.create(task='first');self.assertEqual(self.loop.run('first')['status'],'ready_for_user_review')
        first=self.ledger.hub()['native_sessions']['agy']['session_id']
        claude=adapter_fixture(self.root,ClaudeHubAdapter,dict(verdict=verdict(),expect_brief=True));self.registry.register(claude)
        self.ledger.switch_agent(HubAgentSpec('claude'),'synthetic switch')
        self.create(task='second');self.assertEqual(self.loop.run('second')['status'],'ready_for_user_review')
        row=json.loads((self.ledger.task('second')/'reviews/round-1.json').read_text())
        self.assertEqual(row['provenance'],'ledger_reconstruction_from_agy');self.assertEqual(row['session_mode_effective'],'fresh_only')
        codex=adapter_fixture(self.root,CodexHubAdapter,dict(verdict=verdict(),expect_brief=True));self.registry.register(codex)
        self.ledger.switch_agent(HubAgentSpec('codex'),'synthetic switch')
        self.create(task='third');self.assertEqual(self.loop.run('third')['status'],'ready_for_user_review')
        hub=self.ledger.hub();self.assertEqual(hub['native_sessions']['agy']['session_id'],first)
        self.assertEqual(hub['native_sessions']['agy']['state'],'parked')
        self.assertNotEqual(hub['native_sessions']['codex']['session_id'],first)
        self.ledger.rollover('synthetic operator rollover');self.create(task='fourth')
        self.assertEqual(self.loop.run('fourth')['status'],'ready_for_user_review')
        self.assertTrue(self.ledger.hub()['native_sessions']['codex']['retired_sessions'])

    def test_O8_public_ledger_reviews_events_brief_contain_no_secret(self):
        self.create();state=self.loop.run('task-one');self.assertEqual(state['status'],'ready_for_user_review')
        for path in self.ledger.root.rglob('*'):
            if path.is_file() and ('runtime' not in path.parts) and path.suffix in ('.json','.jsonl','.md'):
                self.assertNotIn(self.secret,path.read_text(),str(path))
        self.assertNotIn(self.secret,json.dumps(state))
        errors=list((self.ledger.task('task-one')/'runtime').rglob('stderr.log'))
        self.assertTrue(any(self.secret in p.read_text() for p in errors))

    def test_O9_reviewer_extra_authority_rejected_original_spec_untouched(self):
        bad=dict(verdict(),checks=[['true']],sandbox='danger-full-access',permissions={'grant':'all'})
        self.reviewer(sequence=[bad]);self.create()
        frozen=(self.ledger.task('task-one')/'spec.json').read_bytes()
        state=self.loop.run('task-one');self.assertEqual(state['status'],'blocked')
        self.assertEqual((self.ledger.task('task-one')/'spec.json').read_bytes(),frozen)
        self.assertIsNone(state['last_verdict'])
        self.assertEqual(list(self.ledger.root.rglob('*.grant')),[])

    def test_O9_symlink_repo_unknown_contract_and_missing_manual_refused_before_provision(self):
        value=spec(self.repo);link=self.root/'link';link.symlink_to(self.repo,target_is_directory=True)
        value['repo']=str(link)
        with self.assertRaises(ValueError):self.loop.create_task(value)
        value=spec(self.repo);value['worker_contract']['permissions_override']=True
        with self.assertRaises(ValueError):self.loop.create_task(value)
        value=spec(self.repo);value['worker_contract']['acceptance'].pop()
        with self.assertRaises(ValueError):self.loop.create_task(value)
        self.assertEqual(git(self.repo,'branch','--list','feat/*'),'')

    def test_O9_worker_state_symlink_refused_before_any_external_write(self):
        self.create();specification,state=self.ledger.read_task('task-one')
        binding=hub_worktree.provision(str(self.repo),'task-one',specification['branch'],'main')
        state['worktree']=binding;self.ledger.update_task('task-one',state)
        external=self.root/'outside';external.mkdir()
        (Path(binding['path'])/'.codex').symlink_to(external,target_is_directory=True)
        with patch.object(runner.Runner,'attempt',side_effect=AssertionError('must not execute')):
            result=self.loop.run('task-one')
        self.assertEqual(result['status'],'blocked');self.assertEqual(list(external.iterdir()),[])

    def test_O9_review_only_cannot_bypass_failed_checks(self):
        self.reviewer(sequence=[verdict('blocked')]);self.create(rounds=3);state=self.loop.run('task-one')
        root=Path(state['worktree']['path']);(root/'result.txt').write_text('failed check')
        with patch.object(self.adapter,'run_turn',side_effect=AssertionError('checks must pass')):
            state=self.loop.run('task-one',review_only=True)
        self.assertEqual(state['status'],'blocked');self.assertEqual(state['review_round'],1)
        (root/'result.txt').write_text('done')
        with patch.object(self.adapter,'run_turn',side_effect=AssertionError('prior failed workflow must stay blocked')):
            self.assertEqual(self.loop.run('task-one',review_only=True)['status'],'blocked')

    def test_O9_prior_STOP_or_owner_refuse_review_only(self):
        self.reviewer(sequence=[verdict('blocked')])
        for barrier in ('STOP','owner'):
            self.create(task=barrier,rounds=3);state=self.loop.run(barrier)
            directory=Path(state['active_workflow_dir'])
            if barrier=='STOP':(directory/'STOP').touch()
            else:
                path=directory/'state.json';row=json.loads(path.read_text());row['owner_pid']=os.getpid()
                path.write_text(json.dumps(row))
            with patch.object(self.adapter,'run_turn',side_effect=AssertionError('prior barrier must hold')):
                state=self.loop.run(barrier,review_only=True)
            self.assertEqual(state['status'],'blocked');self.assertEqual(state['review_round'],1)

    def test_O3_source_fingerprint_tracks_uncommitted_fixes_not_ignored_receipts(self):
        before=hub_worktree.snapshot_integrity(self.repo)
        (self.repo/'.codex/tasks').mkdir(parents=True);(self.repo/'.codex/tasks/receipt').write_text('receipt')
        receipt=hub_worktree.snapshot_integrity(self.repo)
        self.assertEqual(before['tree_sha'],receipt['tree_sha']);self.assertNotEqual(before['files_sha'],receipt['files_sha'])
        (self.repo/'result.txt').write_text('uncommitted source fix')
        after=hub_worktree.snapshot_integrity(self.repo)
        self.assertEqual(before['head_sha'],after['head_sha']);self.assertNotEqual(before['tree_sha'],after['tree_sha'])

    def test_O6_notification_unknown_is_durable_and_never_retried(self):
        calls=[]
        def unknown(event):
            calls.append(event)
            self.assertEqual(self.state()['notice'],'delivery_unknown')
            raise OSError(self.secret)
        self.loop.notifier=unknown;self.create();state=self.loop.run('task-one')
        self.assertEqual(state['notice'],'delivery_unknown');self.assertEqual(len(calls),1)
        self.loop.run('task-one');self.assertEqual(len(calls),1)

    def test_O9_review_only_runs_checks_but_no_worker_or_repair(self):
        self.reviewer(sequence=[verdict('blocked')]);self.create(rounds=3);state=self.loop.run('task-one')
        self.assertEqual(state['status'],'blocked')
        self.reviewer(sequence=[verdict('request_changes','blocker')])
        with patch.object(runner.Runner,'attempt',side_effect=AssertionError('no worker')),patch.object(
                workflow_type(),'propose',side_effect=AssertionError('no planner')):
            state=self.loop.run('task-one',review_only=True)
        self.assertEqual(state['code'],'REVIEW_ONLY_CHANGES_REQUIRED',state)
        self.assertEqual(len(state['workflow_history']),2)
        self.assertEqual(json.loads((Path(state['active_workflow_dir'])/'state.json').read_text())['stages'],[])

    def test_O10_independent_CLI_busy_attach_STOP_and_terminal_authority(self):
        self.create();self.loop.run('task-one')
        def dispatch(argv):return hub_cli.dispatch(hub_cli.parser().parse_args(argv),registry=self.registry)
        args=['hub-attach-cmd',self.ledger.identifier]
        self.assertIn('--conversation',dispatch(args)['argv'])
        self.assertEqual(dispatch(args)['cwd'],self.state()['worktree']['path'])
        with self.ledger.locked():
            with self.assertRaises(ValueError):dispatch(args)
            self.assertEqual(dispatch(['task-stop',self.ledger.identifier,'task-one'])['status'],'stop_requested')
        commands=next(a for a in hub_cli.parser()._actions if a.dest=='command').choices
        self.assertNotIn('acknowledge',commands);self.assertNotIn('merge',commands);self.assertNotIn('push',commands)

    def test_O10_core_AST_vendor_imports_and_frozen_CLI_bytes(self):
        fixture=json.loads((ROOT/'tests/fixtures/w2-core-ast.json').read_text())
        # Existing fixture format is intentionally not rewritten by H1.
        for name in ('control','recovery','interaction','delegation','workflow','session_router','message_bus','transport'):
            tree=ast.parse((ROOT/(name+'.py')).read_text())
            self.assertEqual(hashlib.sha256(ast.dump(tree,include_attributes=False).encode()).hexdigest(),fixture[name])
            for node in ast.walk(tree):
                imported=[a.name for a in node.names] if isinstance(node,ast.Import) else [node.module or ''] if isinstance(node,ast.ImportFrom) else []
                self.assertFalse(any(value.startswith('hub_adapter_') for value in imported),name)


def workflow_type():
    import workflow
    return workflow.Workflow
