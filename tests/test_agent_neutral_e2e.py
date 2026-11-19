import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import agent_registry
import agents
import interaction
import runner
import session_router
import workflow
sys.path.insert(0, str(Path(__file__).parent/'fixtures'))
from fixture_adapter import FixtureAdapter, register
register()
FIXTURE_CLI = Path(__file__).parent/'fixtures/fixture_ctask.py'


class NeutralE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)/'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.env = patch.dict(os.environ, {'CODEX_HOME': str(Path(self.tmp.name)/'home')}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def contract(self):
        return dict(objective='Create a fixture file', scope='isolated fixture', authorization='fixture only',
                    memory=False, agent={'kind': FixtureAdapter.kind},
                    acceptance=[dict(id='result', description='fixture exists',
                                     checks=[[sys.executable, '-c', "from pathlib import Path; assert Path('result').read_text() == 'fixture output'"]])],
                    limits=dict(runtime_seconds=30, attempt_seconds=10, check_seconds=3,
                                max_attempts=1, max_stages=2, max_planning_attempts=2, max_wait_seconds=1))

    def test_worker_and_planner_real_subprocess(self):
        plan = self.repo/'contract.json'
        runner.save(plan, self.contract())
        created = subprocess.run([sys.executable, str(FIXTURE_CLI), 'workflow-create', '--repo', str(self.repo),
                                  '--plan', str(plan)], capture_output=True, text=True, check=True)
        directory = Path(created.stdout.strip())
        subprocess.run([sys.executable, str(FIXTURE_CLI), 'run', str(directory)],
                       capture_output=True, text=True, check=True, timeout=30)
        state = runner.read(directory/'state.json')
        self.assertEqual(state['status'], 'completed')
        self.assertEqual(state['planning_attempts'], 1)
        self.assertEqual(len(state['stages']), 1)
        self.assertEqual((self.repo/'result').read_text(), 'fixture output')
        self.assertEqual(state['criteria']['result']['status'], 'passed')

    def test_non_codex_origin_hub_roundtrip(self):
        def cli(*args, actor=None, ref=None):
            env = dict(os.environ)
            if actor is not None:
                env.update(FIXTURE_ACTOR=actor, CTASK_SESSION_REF=ref)
            return subprocess.run([sys.executable, str(FIXTURE_CLI), *map(str, args)],
                                  env=env, capture_output=True, text=True, check=True, timeout=10).stdout.strip()
        origin = cli('session-register', '--kind', FixtureAdapter.kind, '--identity', 'origin:opaque',
                     '--project', self.repo, '--source', 'fixture identity proof')
        hub = cli('session-register', '--kind', FixtureAdapter.kind, '--identity', 'hub:opaque',
                  '--project', self.repo, '--source', 'fixture identity proof')
        plan = self.repo/'contract.json'; runner.save(plan, self.contract())
        directory = Path(cli('workflow-create', '--repo', self.repo, '--plan', plan, '--origin-ref', origin))
        cli('hub', '--session-ref', hub, '--source', 'fixture user chose hub', actor='hub:opaque', ref=hub)
        question = json.loads(cli('ask', directory, '--question', 'Choose a fixture?', '--recommendation', 'A',
                                  actor='origin:opaque', ref=origin))
        inbox = json.loads(cli('inbox', actor='hub:opaque', ref=hub))
        self.assertEqual(inbox[0]['id'], question['id'])
        cli('answer', question['id'], '--answer', 'A', '--source', 'actual fixture user response',
            actor='hub:opaque', ref=hub)
        accepted = json.loads(cli('accept-answer', question['id'], actor='origin:opaque', ref=origin))
        self.assertEqual(accepted['state'], 'accepted')
        sent = [json.loads(line) for line in (self.repo/'sent.jsonl').read_text().splitlines()]
        self.assertEqual([x['target'] for x in sent], ['hub:opaque', 'origin:opaque'])
        self.assertEqual(runner.read(directory/'state.json')['status'], 'created')

    def test_legacy_frozen_state_read_only(self):
        plan = self.repo/'contract.json'; runner.save(plan, self.contract())
        directory = workflow.create(self.repo, plan)
        files = [directory/'contract.json', directory/'origin.json', directory/'state.json']
        before = [p.read_bytes() for p in files]
        runner.status(directory)
        self.assertEqual(before, [p.read_bytes() for p in files])

    def test_unknown_reference_rejected_before_workflow_created(self):
        plan = self.repo/'contract.json'; runner.save(plan, self.contract())
        with self.assertRaises((ValueError, FileNotFoundError)):
            workflow.create(self.repo, plan, origin_ref='agentref-'+'a'*32)
        self.assertFalse((self.repo/'.codex/tasks/workflows').exists())


if __name__ == '__main__':
    unittest.main()
