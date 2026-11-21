"""The distributed guard retains the live project mute and once-only rules."""
import os
import unittest
from unittest.mock import patch

import test_hub_queue_gate as fixtures
from test_interaction import HUB
import interaction
import notifications
import runner
import control

gate = fixtures.gate

class ProjectPolicyGateTests(unittest.TestCase):
    setUp = fixtures.QueueGateTests.setUp
    tearDown = fixtures.QueueGateTests.tearDown
    transport = fixtures.QueueGateTests.transport
    enable = fixtures.QueueGateTests.enable
    options = fixtures.QueueGateTests.options

    def question(self):
        self.enable()
        with patch('interaction.subprocess.run', side_effect=self.transport):
            return interaction.ask(self.d, 'A real fixture decision?')

    def test_project_mute_blocks_a_canonical_pending_question_without_answering_it(self):
        question = self.question()
        text = '[ctask-question id='+question['id']+']\nA real fixture decision?'
        self.assertTrue(gate.pending_user_question(text, HUB))
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            notifications.configure_project(self.repo, True, 'User silenced this project')
        before = interaction.question_path(question['id']).read_bytes()
        with patch('os.execv') as native:
            self.assertEqual(gate.main(['queue', '--thread', HUB, '--message', text]), 0)
            native.assert_not_called()
        self.assertEqual(interaction.question_path(question['id']).read_bytes(), before)
        self.assertEqual(runner.read(interaction.question_path(question['id']))['state'], 'pending')

    def test_once_only_blocks_duplicate_channels_but_allows_the_initial_dispatch(self):
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            control.enable('User requested at most one notice', max_reminders=0)
        question = self.question()
        ask = '[ctask-question id='+question['id']+']\nA real fixture decision?'
        followup = '[ctask-followup id='+question['id']+' reminder=0]\nSame decision'
        self.assertFalse(gate.pending_user_question(ask, HUB))
        self.assertFalse(gate.pending_user_question(followup, HUB))
        canonical = runner.read(interaction.question_path(question['id']))
        canonical['question_delivery']['state'] = 'dispatching'
        runner.save(interaction.question_path(question['id']), canonical)
        self.assertTrue(gate.pending_user_question(ask, HUB))
        self.assertFalse(gate.pending_user_question(followup, HUB))

if __name__ == '__main__':
    unittest.main()
