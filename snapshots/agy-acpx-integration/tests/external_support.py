"""Private synthetic fixture setup shared by directed external CLI tests."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'tests/fixtures')]
from external_fixture_cli import register
register()
import external_coordinator as external
import external_operator
import local_session_adapter as local
import runner

CLI = ROOT/'tests/fixtures/external_fixture_cli.py'


class ExternalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-external-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root/'repo'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        # No ambient actor/session, real hub, model executable or native home.
        self.env = {k: v for k, v in os.environ.items() if k not in
            ('CODEX_THREAD_ID', 'ATO_ACTOR_FILE', 'ATO_SESSION_REF', 'CTASK_SESSION_REF')}
        self.env.update(ATO_HOME=str(self.root/'ato'), CODEX_HOME=str(self.root/'native'))
        guard = self.root/'forbidden-model'
        guard.write_text('#!/bin/sh\nexit 97\n'); guard.chmod(0o700)
        self.env['CTASK_CODEX_BIN'] = str(guard)
        self.environment = patch.dict(os.environ, self.env, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.plan = self.root/'contract.json'
        self.actor = self.root/'ato/actors/agy.json'
        self.signals = {n: signal.getsignal(n) for n in (signal.SIGINT, signal.SIGTERM)}
        self.addCleanup(self.restore_signals)
        self.processes = []
        self.addCleanup(self.cleanup_processes)
        self.prepare()

    def cleanup_processes(self):
        for p in self.processes:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    p.kill(); p.wait(timeout=3)

    def restore_signals(self):
        for n, handler in self.signals.items():
            signal.signal(n, handler)

    def contract(self, prompt='create fixture output'):
        checks = [[sys.executable, '-c',
            "from pathlib import Path; assert Path('result').read_text() == 'fixture output'"]]
        return dict(objective='Write and independently verify the fixture result',
            scope='Only this fresh temporary Git fixture repository',
            authorization='Synthetic fixture authorization; no real user decision or provider',
            agent=dict(kind='external_fixture_worker'), memory=False, sandbox='workspace-write',
            acceptance=[dict(id='result', description='Exact artifact content verified independently', checks=checks)],
            stages=[dict(id='edit', covers=['result'], plan=dict(objective='Write result', memory=False,
                tasks=[dict(id='write', prompt=prompt, checks=checks)]))],
            limits=dict(runtime_seconds=40, attempt_seconds=10, check_seconds=3,
                max_attempts=1, max_stages=2, max_planning_attempts=1, max_wait_seconds=1))

    def prepare(self, prompt='create fixture output', mutate=None):
        contract = self.contract(prompt)
        if mutate:
            mutate(contract)
        runner.save(self.plan, contract)
        if self.actor.exists():
            self.actor = self.actor.with_name('agy-'+str(len(list(self.actor.parent.glob('*.json'))))+'.json')
        self.provision = external_operator.provision(self.repo, self.plan, self.actor,
            'synthetic human bootstrap fixture; not production authority')

    def cli(self, *args, ok=True, actor=None, entry=CLI, env=None):
        argv = [sys.executable, str(entry), '--actor-file', str(actor or self.actor), *map(str, args)]
        p = subprocess.run(argv, env=env or self.env, capture_output=True, text=True, timeout=20)
        if ok:
            self.assertEqual(p.returncode, 0, p.stderr)
            return json.loads(p.stdout)
        self.assertNotEqual(p.returncode, 0, p.stdout)
        return p

    def submit(self, run=False):
        return external.submit(self.actor, self.plan, run=run)

    def workflow(self):
        return Path(self.submit()['workflow'])
