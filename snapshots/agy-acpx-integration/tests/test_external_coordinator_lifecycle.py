import json
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

from external_support import ExternalCase, CLI, ROOT
import external_coordinator as external
import runner


class ExternalLifecycleTests(ExternalCase):
    def acpx_contract(self, scenario='success', check_passes=True):
        import os
        value = 'external_'+os.urandom(8).hex()
        expected = value if check_passes else 'independent-failure'
        def mutate(c):
            c['agent'] = dict(kind='acpx_codex')
            checks = [[sys.executable, '-I', '-c',
                "from pathlib import Path; assert Path('result.txt').read_text() == "+repr(expected)]]
            c['acceptance'][0]['checks'] = checks
            c['stages'][0]['plan']['tasks'][0].update(
                prompt=f'scenario:{scenario} fixture-value:{value}', checks=checks)
        self.prepare(mutate=mutate)
        return value

    def test_external_ato_acpx_runtime_fake_agent_and_final_check_end_to_end(self):
        value = self.acpx_contract()
        entry = ROOT/'tests/fixtures/external_acpx_fixture_cli.py'
        data = self.cli('submit', '--contract', self.plan, '--run', entry=entry)
        self.assertEqual(data['status'], 'completed')
        self.assertEqual((self.repo/'result.txt').read_text(), value)
        self.assertTrue(self.cli('result', entry=entry)['accepted'])
        d = Path(data['workflow'])
        child = Path(data['stages'][0]['directory'])
        receipts = list((child/'checkpoints').glob('*.acpx-runtime/receipt.json'))
        self.assertEqual(len(receipts), 1)
        transport = runner.read(receipts[0])
        self.assertEqual(transport['correlation']['workflow'], d.name)
        self.assertEqual(transport['correlation']['stage'], child.name)
        self.assertEqual(transport['delivery'], 'finished')
        self.assertEqual(transport['business_validation'], 'unknown')
        self.assertEqual(data['criteria']['result']['status'], 'passed')

    def test_external_acpx_end_turn_cannot_override_failed_frozen_check(self):
        value = self.acpx_contract(check_passes=False)
        entry = ROOT/'tests/fixtures/external_acpx_fixture_cli.py'
        data = self.cli('submit', '--contract', self.plan, '--run', entry=entry)
        self.assertEqual((self.repo/'result.txt').read_text(), value)
        self.assertNotEqual(data['status'], 'completed')
        self.assertFalse(self.cli('result', entry=entry)['accepted'])
        child = Path(data['stages'][0]['directory'])
        receipt = runner.read(next((child/'checkpoints').glob('*.acpx-runtime/receipt.json')))
        self.assertEqual(receipt['protocol'], 'completed')
        self.assertTrue(receipt['result_schema_valid'])

    def test_external_acpx_eof_blocks_continuation_without_prompt_replay(self):
        self.acpx_contract(scenario='eof')
        entry = ROOT/'tests/fixtures/external_acpx_fixture_cli.py'
        data = self.cli('submit', '--contract', self.plan, '--run', entry=entry)
        self.assertNotEqual(data['status'], 'completed')
        self.cli('continue', entry=entry, ok=False)
        again = self.cli('submit', '--contract', self.plan, '--run', entry=entry)
        self.assertEqual(again['admission'], 'existing')
        self.assertEqual(len((self.repo/'fixture-admissions').read_text().splitlines()), 1)
        self.assertEqual(again['stages'][0]['tasks']['write']['attempts'], 1)

    def test_fake_agent_real_git_edit_canonical_stage_and_total_checks(self):
        data = self.cli('submit', '--contract', self.plan, '--run')
        self.assertEqual(data['status'], 'completed')
        final = self.cli('result')
        self.assertTrue(final['accepted'])
        self.assertEqual((self.repo/'result').read_text(), 'fixture output')
        d = Path(data['workflow'])
        state = runner.read(d/'state.json')
        self.assertEqual(state['planning_attempts'], 0)
        self.assertEqual(state['criteria']['result']['status'], 'passed')
        child = Path(state['stages'][0]['run_dir'])
        self.assertEqual(runner.read(child/'parent.json')['workflow_dir'], str(d))
        self.assertTrue(all(Path(p).is_file() for p in state['criteria']['result']['evidence']))
        self.assertFalse(state['owner_pid'])
        self.assertFalse(runner.read(child/'state.json')['active_process'])

    def test_assistant_complete_cannot_override_independently_failing_checks(self):
        self.prepare('wrong-fixture')
        data = self.cli('submit', '--contract', self.plan, '--run')
        self.assertNotEqual(data['status'], 'completed')
        self.assertFalse(self.cli('result')['accepted'])
        task = data['stages'][0]['tasks']['write']
        self.assertEqual(task['attempts'], 1)
        child = Path(data['stages'][0]['directory'])
        self.assertIn('AssertionError', ''.join(Path(p).read_text() for p in runner.read(child/'state.json')['tasks']['write']['evidence']))
        before = (self.repo/'fixture-admissions').read_bytes()
        self.cli('continue', ok=False)
        self.assertEqual((self.repo/'fixture-admissions').read_bytes(), before)
        self.assertEqual(self.cli('status')['stages'][0]['tasks']['write']['attempts'], 1)

    def test_manual_acceptance_stays_needs_input_and_no_user_authority_is_exposed(self):
        self.prepare(mutate=lambda c: c['acceptance'].append(dict(id='human', description='Human review', kind='manual')))
        data = self.cli('submit', '--contract', self.plan, '--run')
        self.assertEqual(data['status'], 'needs_input')
        self.assertEqual(data['criteria']['human']['status'], 'needs_input')
        self.assertFalse(self.cli('result')['accepted'])
        self.cli('continue')
        self.assertEqual(self.cli('status')['criteria']['human']['status'], 'needs_input')

    def test_stop_survives_idempotent_submit_and_continuation_cannot_erase_it(self):
        d = self.workflow()
        self.cli('stop')
        before = (d/'state.json').read_bytes()
        self.cli('submit', '--contract', self.plan, '--run')
        self.cli('continue', ok=False)
        self.assertTrue((d/'STOP').exists())
        self.assertEqual(before, (d/'state.json').read_bytes())
        self.assertTrue(self.cli('status')['stopped'])
        self.assertFalse((self.repo/'result').exists())

    def test_concurrent_submit_admits_only_one_canonical_workflow(self):
        argv = [sys.executable, str(CLI), '--actor-file', str(self.actor), 'submit', '--contract', str(self.plan)]
        processes = [subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(3)]
        self.processes.extend(processes)
        replies = []
        for p in processes:
            out, err = p.communicate(timeout=15)
            self.assertEqual(p.returncode, 0, err)
            replies.append(json.loads(out))
        self.assertEqual(len({r['workflow'] for r in replies}), 1)
        self.assertEqual(sum(r['admission'] == 'created' for r in replies), 1)

    def test_canonical_repo_writer_lock_prevents_second_executor(self):
        import fcntl
        d = self.workflow()
        with (self.repo/'.codex/tasks/.worker.lock').open('a+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.cli('continue', ok=False)
        self.assertEqual(runner.read(d/'state.json')['status'], 'created')
        self.assertFalse((self.repo/'result').exists())

    def test_running_unknown_delivery_and_unreconciled_attempt_are_not_replayed(self):
        data = self.cli('submit', '--contract', self.plan, '--run')
        d = Path(data['workflow'])
        state = runner.read(d/'state.json')
        state['status'] = 'running'; runner.save(d/'state.json', state)
        self.cli('continue', ok=False)
        state['status'] = 'blocked'; runner.save(d/'state.json', state)
        child = Path(state['stages'][0]['run_dir'])
        row = runner.read(child/'state.json')
        row['tasks']['write']['acpx_delivery_unknown'] = True
        runner.save(child/'state.json', row)
        self.cli('continue', ok=False)
        self.assertEqual((self.repo/'fixture-admissions').read_text(), 'one\n')

    def test_completed_result_revalidation_does_not_infer_success_after_regression(self):
        data = self.cli('submit', '--contract', self.plan, '--run')
        self.assertTrue(self.cli('result')['accepted'])
        (self.repo/'result').write_text('later regression')
        self.cli('continue')
        final = self.cli('result')
        self.assertFalse(final['accepted'])
        self.assertEqual(final['criteria']['result']['status'], 'failed')
        self.assertEqual((self.repo/'fixture-admissions').read_text(), 'one\n')
        self.assertEqual(final['planning_attempts'], 0)

    def test_stop_terminates_active_stage_under_canonical_process_group(self):
        self.prepare('sleep-fixture')
        self.workflow()
        argv = [sys.executable, str(CLI), '--actor-file', str(self.actor), 'continue']
        p = subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.processes.append(p)
        deadline = time.monotonic()+8
        while not (self.repo/'fixture-entered').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue((self.repo/'fixture-entered').exists())
        self.cli('continue', ok=False)
        self.cli('stop')
        out, err = p.communicate(timeout=8)
        self.assertEqual(p.returncode, 0, err)
        state = self.cli('status')
        self.assertEqual(state['status'], 'stopped')
        self.assertTrue(state['stopped'])
        self.assertFalse((self.repo/'result').exists())
        child = runner.read(Path(state['stages'][0]['directory'])/'state.json')
        self.assertIsNone(child['active_process'])
        self.assertIsNone(child['owner_pid'])

    def test_event_cursor_excludes_sensitive_bodies_and_retries_partial_append(self):
        d = self.workflow()
        path = d/'events.jsonl'
        first = dict(time=1, event='fixture', prompt='private model body', challenge='secret')
        path.write_text(json.dumps(first)+'\n'+json.dumps(dict(event='partial')))
        result = self.cli('events', '--limit', 1)
        self.assertEqual(result['events'], [dict(time=1, event='fixture')])
        cursor = result['cursor']
        self.assertEqual(self.cli('events', '--cursor', cursor)['cursor'], cursor)
        with path.open('a') as stream:
            stream.write('\n')
        self.assertEqual(self.cli('events', '--cursor', cursor)['events'], [dict(event='partial')])
        self.assertNotIn('secret', json.dumps(result))
        self.assertFalse(result['human_read_recorded'])


if __name__ == '__main__':
    import unittest
    unittest.main()
