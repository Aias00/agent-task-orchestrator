"""Offline action admission, genuine fixture delivery and conflict settlement."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import permissions
import runner
import workflow


FAKE = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
prompt = sys.stdin.read()
Path('prompt.txt').write_text(prompt)
calls = Path('worker-calls')
calls.write_text(str(int(calls.read_text())+1) if calls.exists() else '1')
status = os.environ.get('FIXTURE_STATUS', 'complete')
blocker = os.environ.get('FIXTURE_BLOCKER', '')
if 'Worker may deploy only within the recorded action scope.' in prompt and not blocker:
    Path('deployed').write_text('local synthetic deployment')
out = Path(sys.argv[sys.argv.index('-o')+1])
out.write_text(json.dumps(dict(status=status, summary='Fixture output', blocker=blocker,
    next_step='Use the recorded action owner; retain prior attempts', retry_after_seconds=1)))
'''


class ExecutionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.fake = self.repo/'fake-worker'
        self.fake.write_text(FAKE)
        self.fake.chmod(0o755)
        self.env = dict(os.environ, CTASK_CODEX_BIN=str(self.fake), CODEX_THREAD_ID='',
                        CODEX_HOME=str(self.repo/'native'), ATO_HOME=str(self.repo/'ato'))
        p = patch.dict(os.environ, self.env)
        p.start()
        self.addCleanup(p.stop)

    def policy(self, owner='worker'):
        return dict(source='User approved local synthetic fixture delivery',
                    instructions='Only write the local deployed fixture; no real services or network',
                    actions={'deploy': owner}, required_actions=['deploy'])

    def plan(self, owner='worker'):
        p = dict(objective='Deliver a local synthetic deployment', memory=False,
                 tasks=[dict(id='deliver', prompt='Deliver the authorized local fixture', checks=[[
                     sys.executable, '-c', "from pathlib import Path; assert Path('deployed').read_text() == 'local synthetic deployment'"]])])
        if owner:
            p['execution_policy'] = self.policy(owner)
        if owner == 'worker':
            p.update(sandbox='danger-full-access', permissions={'source': 'Actual fixture authorization'})
        if owner == 'origin':
            p['host_handoff'] = dict(source='Actual fixture authorization', instructions='Origin writes deployed fixture')
        return p

    def create(self, plan):
        source = self.repo/'plan-input.json'
        runner.save(source, plan)
        return runner.create(self.repo, source)

    def execute(self, directory, **env):
        result = subprocess.run([sys.executable, str(Path(runner.__file__)), 'run', str(directory)],
                                env=dict(self.env, **env), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return runner.read(directory/'state.json')

    def test_explicit_worker_delivery_runs_and_independent_acceptance_passes(self):
        d = self.create(self.plan())
        s = self.execute(d)
        self.assertEqual(s['status'], 'completed')
        self.assertEqual((self.repo/'deployed').read_text(), 'local synthetic deployment')
        prompt = (self.repo/'prompt.txt').read_text()
        self.assertNotIn('Do not git commit/push, deploy, send messages', prompt)
        self.assertIn('Worker must not git push.', prompt)
        self.assertIn('Worker must not send messages.', prompt)
        self.assertTrue(s['tasks']['deliver']['evidence'])

    def test_legacy_prose_and_full_sandbox_do_not_grant_worker_actions(self):
        p = self.plan(None)
        p.update(sandbox='danger-full-access', permissions={'source': 'Authorized deployment in prose only'})
        p['tasks'][0]['prompt'] = 'Deploy the local synthetic fixture; user already authorized it'
        d = self.create(p)
        s = self.execute(d, FIXTURE_STATUS='blocked', FIXTURE_BLOCKER='No explicit action owner')
        self.assertEqual(s['status'], 'blocked')
        self.assertIn('Legacy worker action policy', (self.repo/'prompt.txt').read_text())
        self.assertFalse((self.repo/'deployed').exists())

    def test_origin_delivery_has_working_handoff_and_no_worker_deployment(self):
        d = self.create(self.plan('origin'))
        s = self.execute(d, FIXTURE_STATUS='needs_host')
        self.assertEqual(s['status'], 'needs_host')
        self.assertFalse((self.repo/'deployed').exists())
        self.assertIn('Only the origin may deploy', (self.repo/'prompt.txt').read_text())
        self.assertEqual((self.repo/'worker-calls').read_text(), '1')
        self.execute(d, FIXTURE_STATUS='needs_host')
        self.assertEqual((self.repo/'worker-calls').read_text(), '1')

    def test_invalid_action_paths_fail_before_any_worker(self):
        variants = []
        p = self.plan('origin'); p.pop('host_handoff'); variants.append(p)
        p = self.plan(); p['execution_policy']['actions'] = {'deploy': 'deny'}; variants.append(p)
        p = self.plan(); p['execution_policy']['actions'] = {'git_commit': 'worker'}; variants.append(p)
        p = self.plan(); p['execution_policy']['source'] = ''; variants.append(p)
        p = self.plan(); p['execution_policy']['instructions'] = ''; variants.append(p)
        p = self.plan(); p['sandbox'] = 'workspace-write'; variants.append(p)
        p = self.plan(); p['agent'] = {'kind': 'pi'}; variants.append(p)
        p = self.plan(); p['execution_policy'] = None; variants.append(p)
        p = self.plan(); p['execution_policy']['actions'] = {'shell': 'worker'}; variants.append(p)
        p = self.plan(); p['execution_policy']['actions'] = {'deploy': 'guess'}; variants.append(p)
        p = self.plan(); p['execution_policy']['required_actions'] = ['deploy', 'deploy']; variants.append(p)
        p = self.plan(); p['execution_policy']['required_actions'] = ['unknown']; variants.append(p)
        for p in variants:
            with self.subTest(policy=p.get('execution_policy')), self.assertRaises(ValueError):
                self.create(p)
        self.assertFalse((self.repo/'worker-calls').exists())

    def test_blocker_on_continue_or_claimed_success_stops_without_checks_or_retries(self):
        for status in ('continue', 'complete', 'ready_for_checks', 'blocked'):
            with self.subTest(status=status):
                p = self.plan(None)
                p['tasks'][0]['checks'] = [[sys.executable, '-c', "from pathlib import Path; Path('check-called').touch()"]]
                d = self.create(p)
                s = self.execute(d, FIXTURE_STATUS=status, FIXTURE_BLOCKER='Deployment instruction conflicts with action policy')
                row = s['tasks']['deliver']
                self.assertEqual(s['status'], 'blocked')
                self.assertEqual(row['attempts'], 1)
                self.assertIn('instruction conflicts', row['blocker'])
                self.assertIn('recorded action owner', row['next_step'])
                self.assertFalse((self.repo/'check-called').exists())
                again = self.execute(d)
                self.assertEqual(again['tasks']['deliver']['attempt_sequence'], 1)

    def test_continue_without_blocker_keeps_existing_independent_check_behavior(self):
        d = self.create(self.plan())
        s = self.execute(d, FIXTURE_STATUS='continue')
        self.assertEqual(s['status'], 'blocked')
        self.assertEqual(s['tasks']['deliver']['status'], 'failed')
        self.assertEqual(s['tasks']['deliver']['attempts'], 3)
        self.assertTrue(s['tasks']['deliver']['evidence'])

    def test_explicit_policy_cannot_bypass_stop(self):
        d = self.create(self.plan())
        (d/'STOP').touch()
        result = subprocess.run([sys.executable, str(Path(runner.__file__)), 'run', str(d)],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('STOP', result.stderr)
        self.assertFalse((self.repo/'deployed').exists())
        self.assertFalse((self.repo/'worker-calls').exists())
        self.assertTrue((d/'STOP').exists())

    def contract(self):
        p = self.plan()
        return dict(objective=p['objective'], scope='Local fixture only', authorization='User approved fixture only',
                    acceptance=[dict(id='delivered', description='Real fixture exists', checks=p['tasks'][0]['checks'])],
                    stages=[dict(id='deliver', covers=['delivered'], plan={
                        k: copy.deepcopy(v) for k, v in p.items() if k not in ('execution_policy', 'permissions', 'sandbox')})],
                    execution_policy=p['execution_policy'], sandbox=p['sandbox'], permissions=p['permissions'], memory=False)

    def test_workflow_inherits_policy_without_mutating_frozen_contract(self):
        c = self.contract()
        source = self.repo/'contract.json'; runner.save(source, c)
        d = workflow.create(self.repo, source)
        before = (d/'contract.json').read_bytes()
        w = workflow.Workflow(d)
        w.stage(c['stages'][0]['plan'], ['delivered'])
        child = runner.read(Path(w.state['active_run'])/'plan.json')
        self.assertEqual(child['execution_policy'], c['execution_policy'])
        self.assertEqual(child['sandbox'], c['sandbox'])
        self.assertEqual(child['permissions'], c['permissions'])
        self.assertEqual((d/'contract.json').read_bytes(), before)

    def test_preset_and_dynamic_stage_cannot_introduce_or_override_action_grants(self):
        c = self.contract()
        c['stages'][0]['plan']['execution_policy'] = self.policy('origin')
        with self.assertRaisesRegex(ValueError, 'cannot override'):
            workflow.validate(c)
        c = self.contract(); c.pop('execution_policy')
        c['stages'][0]['plan']['execution_policy'] = self.policy()
        with self.assertRaisesRegex(ValueError, 'cannot override'):
            workflow.validate(c)
        c = self.contract()
        source = self.repo/'contract.json'; runner.save(source, c)
        w = workflow.Workflow(workflow.create(self.repo, source))
        p = self.plan(); p['execution_policy']['instructions'] = 'Different scope'
        with self.assertRaisesRegex(ValueError, 'cannot override'):
            w.stage(p, ['delivered'])
        self.assertEqual(w.state['stages'], [])

    def test_external_adapter_can_use_origin_handoff_without_claiming_worker_capabilities(self):
        p = self.plan('origin'); p['agent'] = {'kind': 'pi'}
        permissions.validate(p)
