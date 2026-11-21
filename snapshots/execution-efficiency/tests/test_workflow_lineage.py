"""Metadata-only lineage acceptance against separate isolated workflows."""
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_interaction
import runner
import workflow
import workflow_lineage as lineage


class LineageTests(unittest.TestCase):
    def setUp(self):
        self.f = test_interaction.InteractionTests('runTest'); self.f.setUp()
        contract = runner.read(self.f.d/'contract.json'); contract['objective'] = 'Reworded objective'
        p = self.f.repo/'successor-contract.json'; runner.save(p, contract)
        self.successor = workflow.create(self.f.repo, p, test_interaction.ORIGIN)
        proof = self.f.repo/'check.log'; proof.write_text('fixture passed check')
        state = runner.read(self.successor/'state.json')
        state.update(status='completed', criteria={'file': {'status':'passed', 'evidence':[str(proof)]}})
        runner.save(self.successor/'state.json', state)
        self.data = dict(predecessor_workflow=str(self.f.d), successor_workflow=str(self.successor),
            predecessor_contract_sha256=lineage.sha(self.f.d/'contract.json'),
            successor_contract_sha256=lineage.sha(self.successor/'contract.json'), permission_expanded=False,
            origin_thread=test_interaction.ORIGIN, hub_thread=test_interaction.HUB,
            acceptance_mapping={'file':'file'}, source='fixture evidence')
        self.proof = self.f.repo/'lineage.json'; runner.save(self.proof, self.data)

    def tearDown(self):
        self.f.tearDown()

    def link(self):
        return lineage.link(self.f.d, self.successor, self.proof, 'fixture verification')

    def test_link_projects_completed_but_retains_blocked_and_stop(self):
        (self.f.d/'STOP').touch(); self.link()
        status = runner.status(self.f.d)
        self.assertEqual(status['status'], 'blocked')
        self.assertEqual(status['delivery']['effective_status'], 'completed')
        self.assertTrue((self.f.d/'STOP').exists())

    def test_cli_link_and_idempotency(self):
        argv = [sys.executable, 'runner.py', 'workflow-link', str(self.f.d), '--successor', str(self.successor),
                '--evidence', str(self.proof), '--source', 'fixture verification']
        for _ in range(2):
            result = subprocess.run(argv, env=self.f.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_changed_evidence_invalidates_projection(self):
        self.link(); self.proof.write_text('{}')
        self.assertEqual(runner.status(self.f.d)['delivery']['validation'], 'invalid')
        self.assertEqual(runner.status(self.f.d)['delivery']['effective_status'], 'blocked')

    def test_changed_completion_evidence_invalidates_projection(self):
        self.link(); (self.f.repo/'check.log').write_text('changed')
        self.assertEqual(runner.status(self.f.d)['delivery']['validation'], 'invalid')

    def test_dropped_acceptance_rejected(self):
        c = runner.read(self.successor/'contract.json'); c['acceptance'][0]['checks'] = [['true']]
        runner.save(self.successor/'contract.json', c)
        s = runner.read(self.successor/'state.json'); s['contract_hash'] = runner.digest(c)
        runner.save(self.successor/'state.json', s)
        self.data['successor_contract_sha256'] = lineage.sha(self.successor/'contract.json'); runner.save(self.proof, self.data)
        with self.assertRaises(ValueError): self.link()

    def test_changed_authority_rejected(self):
        c = runner.read(self.successor/'contract.json'); c['authorization'] += ' extra'
        runner.save(self.successor/'contract.json', c)
        s = runner.read(self.successor/'state.json'); s['contract_hash'] = runner.digest(c)
        runner.save(self.successor/'state.json', s)
        self.data['successor_contract_sha256'] = lineage.sha(self.successor/'contract.json'); runner.save(self.proof, self.data)
        with self.assertRaises(ValueError): self.link()

    def test_wrong_origin_rejected(self):
        o = runner.read(self.successor/'origin.json'); o['origin_thread_id'] = test_interaction.HUB
        runner.save(self.successor/'origin.json', o)
        with self.assertRaises(ValueError): self.link()

    def test_incomplete_successor_rejected(self):
        s = runner.read(self.successor/'state.json'); s['status'] = 'blocked'; runner.save(self.successor/'state.json', s)
        with self.assertRaises(ValueError): self.link()

    def test_cycle_rejected(self):
        runner.save(self.successor/'workflow-lineage.json', {'successor':str(self.f.d)})
        with self.assertRaisesRegex(ValueError, 'cycle'): self.link()

    def test_changed_successor_contract_invalidates_projection(self):
        self.link()
        with (self.successor/'contract.json').open('a') as f: f.write(' ')
        self.assertEqual(runner.status(self.f.d)['delivery']['validation'], 'invalid')

    def test_unrelated_actor_cannot_register(self):
        import os
        with patch.dict(os.environ, CODEX_THREAD_ID='unrelated'):
            with self.assertRaises(ValueError): self.link()

    def test_link_does_not_modify_raw_workflow_state(self):
        before = (self.f.d/'state.json').read_bytes()
        self.link()
        self.assertEqual(before, (self.f.d/'state.json').read_bytes())

    def test_controller_retains_stopped_action_and_attaches_delivery(self):
        import control
        import notifications
        import os
        with patch.dict(os.environ, CODEX_THREAD_ID=test_interaction.HUB):
            control.enable('fixture control', 60, 0)
            notifications.configure('user_input_only', 'fixture quiet')
        control.register(self.f.d)
        with patch('control.send', return_value={'state':'queued'}): control.tick()
        (self.f.d/'STOP').touch()
        with patch('control.send', return_value={'state':'queued'}): control.tick()
        self.link()
        with patch('control.send', return_value={'state':'queued'}): control.tick()
        action = next(a for a in control.rows(True) if a['kind'] == 'workflow')
        self.assertEqual(action['state'], 'stopped')
        self.assertEqual(action['delivery']['effective_status'], 'completed')
        self.assertTrue((self.f.d/'STOP').exists())

    def test_scope_annotation_requires_explicit_observational_evidence(self):
        c = runner.read(self.successor/'contract.json'); c['scope'] += ' Additional observation only.'
        runner.save(self.successor/'contract.json', c)
        s = runner.read(self.successor/'state.json'); s['contract_hash'] = runner.digest(c)
        runner.save(self.successor/'state.json', s)
        self.data['successor_contract_sha256'] = lineage.sha(self.successor/'contract.json')
        runner.save(self.proof, self.data)
        with self.assertRaises(ValueError): self.link()
        self.data.update(scope_annotation=' Additional observation only.', scope_annotation_observation_only=True)
        runner.save(self.proof, self.data)
        self.link()
        self.assertEqual(runner.status(self.f.d)['delivery']['validation'], 'valid')

    def test_changed_grants_are_rejected(self):
        c = runner.read(self.successor/'contract.json'); c['sandbox'] = 'danger-full-access'
        runner.save(self.successor/'contract.json', c)
        s = runner.read(self.successor/'state.json'); s['contract_hash'] = runner.digest(c)
        runner.save(self.successor/'state.json', s)
        self.data['successor_contract_sha256'] = lineage.sha(self.successor/'contract.json')
        runner.save(self.proof, self.data)
        with self.assertRaises(ValueError): self.link()
