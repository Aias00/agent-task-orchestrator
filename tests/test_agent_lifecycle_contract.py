import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ato_adapters import agents
from ato_workflow import control
from ato_workflow import delegation
from ato_workflow import interaction
from ato_workflow import notifications
from ato_workflow import progress
import recovery
import runner
from ato_workflow import session_router
from ato_workflow import workflow
import test_agent_neutral_e2e as fixture


class LifecycleContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)/'repo'; self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        env = patch.dict(os.environ, {'CODEX_HOME': str(Path(self.tmp.name)/'home')}, clear=False)
        env.start(); self.addCleanup(env.stop)
        self.origin = session_router.register(fixture.FixtureAdapter.kind, 'origin:opaque', self.repo, 'fixture')
        self.hub = session_router.register(fixture.FixtureAdapter.kind, 'hub:opaque', self.repo, 'fixture')
        plan = self.repo/'contract.json'
        runner.save(plan, dict(objective='fixture', scope='fixture', authorization='fixture', memory=False,
                              acceptance=[dict(id='x', description='fixture', checks=[['true']])]))
        self.directory = workflow.create(self.repo, plan, origin_ref=self.origin)
        with patch.dict(os.environ, {'FIXTURE_ACTOR': 'hub:opaque'}):
            interaction.configure_session(self.hub, 'fixture user')

    def ask(self):
        with patch.dict(os.environ, {'FIXTURE_ACTOR': 'origin:opaque'}):
            return interaction.ask(self.directory, 'Actual fixture question?')

    def test_permission_refused_before_spawn(self):
        marker = self.repo/'spawned'
        plan = {'agent': {'kind': fixture.FixtureAdapter.kind},
                'permissions': {'source': 'fixture', 'network_access': True}}
        with self.assertRaises(ValueError):
            argv = agents.command(plan, self.directory, 'bad', self.repo/'schema', marker)
            subprocess.run(argv, check=True)
        self.assertFalse(marker.exists())

    def test_unsupported_capability_before_effect(self):
        folder = interaction.directory()/'agent-sessions'
        before = {p.name for p in folder.iterdir()}
        with self.assertRaises(ValueError):
            session_router.register('pi', 'some-opaque-id', self.repo, 'fixture source')
        self.assertEqual({p.name for p in folder.iterdir()}, before)
        self.assertFalse((self.repo/'sent.jsonl').exists())

    def test_identity_mismatch_cannot_answer(self):
        q = self.ask()
        with patch.dict(os.environ, {'FIXTURE_ACTOR': 'origin:opaque'}):
            with self.assertRaises(ValueError):
                interaction.answer(q['id'], 'A', 'fixture user')
        self.assertEqual(runner.read(interaction.question_path(q['id']))['state'], 'pending')

    def test_unknown_delivery_stays_unknown_in_interaction(self):
        with patch.dict(os.environ, {'FIXTURE_SEND_UNKNOWN': '1'}):
            first = self.ask()
            second = self.ask()
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['question_delivery']['state'], 'delivery_unknown')
        self.assertEqual(len((self.repo/'sent.jsonl').read_text().splitlines()), 1)

    def test_same_registered_transport_serves_durable_mailbox(self):
        from ato_workflow.message_bus import MessageBus
        from ato_workflow.transport import dispatch_registered
        bus = MessageBus(Path(self.tmp.name)/'mailbox.sqlite')
        sender = bus.register_endpoint('sender', kind='codex', session_id='hub',
                                       project=self.repo, source='fixture hub')
        receiver = bus.register_endpoint('receiver', kind=fixture.FixtureAdapter.kind,
                                         session_id='origin:opaque', project=self.repo, source='fixture origin')
        route = bus.bind_route(sender='sender', target='receiver', task_id='task',
                               contract_hash='hash', source='fixture binding')['id']
        msg = bus.enqueue(sender, binding_id=route, target='receiver', kind='request', body='mailbox text',
                          idempotency_key='once', task_id='task', contract_hash='hash')
        owner = bus.acquire_endpoint(receiver, owner='fixture')
        self.assertEqual(dispatch_registered(bus, receiver, owner)['state'], 'delivered')
        self.assertIsNone(dispatch_registered(bus, receiver, owner))
        self.assertEqual(bus.message(msg['id'])['state'], 'delivered')
        self.assertIn('mailbox text', (self.repo/'sent.jsonl').read_text())

    def test_project_mismatch_refused(self):
        other = Path(self.tmp.name)/'other'; other.mkdir()
        with self.assertRaises(ValueError):
            session_router.resolve(self.origin, project=other)

    def test_stop_preserves_history(self):
        q = self.ask()
        path = interaction.question_path(q['id'])
        before = path.read_bytes()
        (self.directory/'STOP').touch()
        with patch.dict(os.environ, {'FIXTURE_ACTOR': 'hub:opaque'}):
            with self.assertRaises(ValueError):
                interaction.answer(q['id'], 'A', 'fixture user')
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue((self.directory/'STOP').exists())

        plan = self.repo/'run.json'
        runner.save(plan, dict(objective='stop fixture', memory=False,
                               agent={'kind': fixture.FixtureAdapter.kind},
                               tasks=[dict(id='sleep', prompt='sleep-fixture', checks=[['test', '-f', 'result']])],
                               limits=dict(runtime_seconds=40, attempt_seconds=35, check_seconds=2, max_attempts=1)))
        run = runner.create(self.repo, plan)
        process = subprocess.Popen([sys.executable, str(fixture.FIXTURE_CLI), 'run', str(run)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic()+8
            while not (self.repo/'fixture-entered').exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue((self.repo/'fixture-entered').exists())
            (run/'STOP').touch()
            process.communicate(timeout=8)
            state = runner.read(run/'state.json')
            self.assertEqual(state['status'], 'stopped')
            self.assertEqual(state['tasks']['sleep']['attempts'], 1)
            self.assertTrue((run/'STOP').exists())
            self.assertFalse((self.repo/'result').exists())
            self.assertIn('command_started', (run/'events.jsonl').read_text())
        finally:
            if process.poll() is None:
                process.terminate(); process.communicate(timeout=5)

    def test_notification_mute_and_once(self):
        with patch.dict(os.environ, {'FIXTURE_ACTOR': 'hub:opaque'}):
            notifications.configure_project(self.repo, True, 'fixture user muted project')
        first = self.ask(); second = self.ask()
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(first['question_delivery']['state'], 'recorded_only')
        self.assertFalse((self.repo/'sent.jsonl').exists())
        self.assertEqual(runner.read(interaction.question_path(first['id']))['state'], 'pending')

    def test_recovery_uses_correlated_session_events(self):
        state = runner.read(self.directory/'state.json')
        state.update(status='blocked', owner_pid=None)
        runner.save(self.directory/'state.json', state)
        recovery.enable(self.directory, 'fixture explicit recovery authorization', response_seconds=30)
        self.assertTrue(recovery.tick(self.directory))
        record = runner.read(self.directory/'recovery.json')
        request = record['requests'][-1]
        self.assertEqual(request['state'], 'queued')
        with (self.repo/'fixture-events.jsonl').open('a') as f:
            import json
            for correlation, kind in [('unrelated', 'received'), ('unrelated', 'responded')]:
                f.write(json.dumps(dict(correlation_id=correlation, kind=kind, turn_id='unrelated'))+'\n')
        recovery.tick(self.directory)
        self.assertNotIn('responded_at', runner.read(self.directory/'recovery.json')['requests'][-1])
        with (self.repo/'fixture-events.jsonl').open('a') as f:
            for kind in ('received', 'responded'):
                f.write(json.dumps(dict(correlation_id=request['id'], kind=kind, turn_id='fixture-turn'))+'\n')
        recovery.tick(self.directory)
        self.assertIn('responded_at', runner.read(self.directory/'recovery.json')['requests'][-1])
        with patch.dict(os.environ, {'FIXTURE_ACTOR': 'origin:opaque'}):
            recovery.takeover(self.directory, request['id'], 'working', 'fixture inspecting')
        self.assertEqual(runner.read(self.directory/'state.json')['status'], 'blocked')

    def test_registered_delegation_requires_acceptance_and_deduplicates(self):
        target = Path(self.tmp.name)/'delegate-repo'; target.mkdir()
        subprocess.run(['git', 'init', '-q', str(target)], check=True)
        plan = target/'plan.json'
        runner.save(plan, dict(objective='fixture delegation', scope='isolated target',
                              authorization='local fixture', memory=False,
                              acceptance=[dict(id='x', description='fixture', checks=[['true']])]))
        with patch.dict(os.environ, {'FIXTURE_ACTOR': 'hub:opaque'}), patch('ato_workflow.delegation.start_bridge'):
            first = delegation.delegate(target, plan, 'fixture', 'explicit fixture delegation',
                                        session_kind=fixture.FixtureAdapter.kind)
            again = delegation.delegate(target, plan, 'fixture', 'explicit fixture delegation',
                                        session_kind=fixture.FixtureAdapter.kind)
        self.assertEqual(first['state'], 'queued')
        self.assertEqual(first['origin_thread'], again['origin_thread'])
        self.assertTrue((target/'fixture-created.json').exists())
        self.assertEqual(len((target/'sent.jsonl').read_text().splitlines()), 1)
        with patch.dict(os.environ, {'FIXTURE_ACTOR': 'created:opaque'}):
            accepted = delegation.accept(first['directory'])
        self.assertEqual(accepted['state'], 'accepted')
        self.assertEqual(runner.read(Path(first['workflow_dir'])/'state.json')['status'], 'created')

    def test_fourth_agent_progress_uses_adapter_events(self):
        import json
        log = self.repo/'events.jsonl'
        log.write_text(json.dumps(dict(event='fixture_tool_start', id='one', tool='read',
                                       arguments={'path': 'private'}))+'\n')
        state = progress.initial()
        adapter = agents.registry.get(fixture.FixtureAdapter.kind, 'progress')
        self.assertEqual(progress.observe(log, state, 0, runner.digest,
                                          normalizer=adapter.normalize_progress), [])
        self.assertIn('one', state['tools'])
        with log.open('a') as f:
            f.write(json.dumps(dict(event='fixture_tool_end', id='one'))+'\n')
        progress.observe(log, state, 1, runner.digest, normalizer=adapter.normalize_progress)
        self.assertEqual(state['tools'], {})
        self.assertNotIn('private', json.dumps(state))

    def test_controller_relays_only_correlated_completed_turn(self):
        import json
        with patch.dict(os.environ, {'FIXTURE_ACTOR': 'hub:opaque'}):
            control.enable('fixture user enabled observation', max_reminders=0)
        control.register(self.directory)
        q = self.ask()
        with patch.dict(os.environ, {'FIXTURE_ACTOR': 'hub:opaque'}):
            interaction.answer(q['id'], 'A', 'actual fixture user answer')
        events = self.repo/'fixture-events.jsonl'
        with events.open('a') as f:
            for correlation, turn, kind, text in [
                    ('wrong', 'old', 'received', ''), ('wrong', 'old', 'completed', 'wrong reply'),
                    (q['id'], 'correct', 'received', ''), (q['id'], 'other', 'completed', 'wrong turn'),
                    (q['id'], 'correct', 'responded', 'fixture response'),
                    (q['id'], 'correct', 'completed', '')]:
                f.write(json.dumps(dict(correlation_id=correlation, turn_id=turn, kind=kind, text=text))+'\n')
        control.tick(); control.tick()
        rows = [json.loads(line) for line in (self.repo/'sent.jsonl').read_text().splitlines()]
        replies = [row for row in rows if '[ctask-origin-reply' in row['text']]
        self.assertEqual(len(replies), 1)
        self.assertIn('fixture response', replies[0]['text'])
        self.assertNotIn('wrong reply', replies[0]['text'])
        self.assertEqual(runner.read(self.directory/'state.json')['status'], 'created')


if __name__ == '__main__':
    unittest.main()
