import json
from legacy_support import LEGACY_CLI
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from ato_workflow import task_trace as trace

ROOT = Path(__file__).resolve().parents[1]
class TraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.path = Path(self.tmp.name)
    def add(self, kind, eid, **kw): return trace.append(self.path, kind, event_id=eid, task='t', attempt=1, **kw)
    def test_tool_lifecycle_counts_one_call(self):
        for n, state in enumerate(('pending', 'in_progress', 'completed')):
            self.add('tool', str(n), tool='same', state=state, session='s', turn='t', source='acp')
        self.assertEqual(trace.query(self.path)['summary']['tool_call_count'], 1)
    def test_trace_restart_and_dedup(self):
        self.add('attempt_started', 'one'); self.add('attempt_started', 'one')
        self.assertEqual(len(trace.query(self.path)['events']), 1)
    def test_trace_cli_reads_saved_run(self):
        self.add('attempt_started', 'one')
        out = subprocess.check_output([sys.executable, str(LEGACY_CLI), 'trace', str(self.path), '--json'], text=True)
        self.assertEqual(json.loads(out)['events'][0]['kind'], 'attempt_started')
    def test_credentials_thoughts_and_bodies_are_redacted(self):
        self.add('tool', 'secret', tool='token-secret', state='token-secret', target='../token-secret', workspace=self.path)
        output = json.dumps(trace.query(self.path))
        self.assertNotIn('token-secret', output)
    def test_overflow_is_bounded_and_visible(self):
        with patch.object(trace, 'MAX_EVENTS', 2):
            for n in range(4): self.add('progress', str(n))
        data = trace.query(self.path)
        self.assertEqual(len(data['events']), 2); self.assertEqual(data['summary']['dropped'], 2)
    def test_trace_io_failure_does_not_interrupt_worker(self):
        self.assertFalse(trace.append(self.path/'missing', 'progress', event_id='x'))
    def test_filter_and_order(self):
        self.add('attempt_started', '1'); self.add('attempt_finished', '2')
        events = trace.query(self.path, 't')['events']
        self.assertLess(events[0]['sequence'], events[1]['sequence'])
        self.assertEqual(trace.query(self.path, 'other')['events'], [])
    def test_legacy_run_unavailable(self):
        self.assertFalse(trace.query(self.path)['summary']['available'])
