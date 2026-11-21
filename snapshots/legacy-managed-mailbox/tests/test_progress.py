import json
from pathlib import Path
import tempfile
import unittest

import progress
import runner


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.path = Path(self.tmp.name)/'stream'
        self.state = progress.initial()
    def tearDown(self):
        self.tmp.cleanup()
    def append(self, row):
        with self.path.open('a') as f:
            f.write(json.dumps(row)+'\n')
    def observe(self, now):
        return progress.observe(self.path, self.state, now, runner.digest)

    def test_idle_alert_once_across_serialized_restart(self):
        self.assertEqual(self.observe(0), [])
        self.assertEqual(self.observe(121)[0]['kind'], 'stream_idle')
        self.state = json.loads(json.dumps(self.state))
        self.assertEqual(self.observe(500), [])

    def test_pi_repetition_counts_across_task_and_deduplicates_tool_identity(self):
        for i in range(6):
            self.append(dict(type='tool_execution_start', toolCallId=str(i), toolName='grep', args={'pattern':'same'}))
            self.append(dict(type='tool_execution_end', toolCallId=str(i)))
        alerts = self.observe(0)
        self.assertEqual([a['kind'] for a in alerts], ['repeated_tool_call'])
        self.append(dict(type='tool_execution_start', toolCallId='5', toolName='grep', args={'pattern':'same'}))
        self.assertEqual(self.observe(1), [])
        self.assertEqual(max(self.state['counts'].values()), 6)
        self.assertNotIn('same', json.dumps(self.state))

    def test_codex_and_claude_normalize_and_tools_progress(self):
        self.append(dict(type='item.started', item=dict(type='command_execution', id='c', command='rg file')))
        self.append(dict(type='assistant', message=dict(content=[dict(type='tool_use', id='a', name='Read', input={'path':'x'})])))
        self.observe(0)
        self.assertEqual(len(self.state['tools']), 2)
        self.append(dict(type='user', message=dict(content=[dict(type='tool_result', tool_use_id='a')])))
        self.append(dict(type='item.completed', item=dict(type='command_execution', id='c')))
        self.observe(1)
        self.assertEqual(self.state['tools'], {})

    def test_partial_json_record_retried_then_tool_idle(self):
        self.path.write_text('{"type":"tool_execution_start",')
        self.observe(0); self.assertEqual(self.state['offset'], 0)
        with self.path.open('a') as f:
            f.write('"toolCallId":"slow","toolName":"read","args":{}}\n')
        self.observe(1)
        alerts = self.observe(602)
        self.assertIn('tool_idle', [a['kind'] for a in alerts])
        self.assertEqual(self.observe(603), [])


if __name__ == '__main__':
    unittest.main()
