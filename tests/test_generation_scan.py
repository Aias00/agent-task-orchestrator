"""Differential generation checks against the original forward full scan."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import recovery


def full_scan(path, state):
    last = None
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get('event') == 'workflow_started':
            last = event['time']
    return [last, state.get('execution_revision', 0)]


class GenerationScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'events.jsonl'

    def test_missing_empty_and_small_histories_keep_original_values(self):
        self.assertEqual(recovery.generation(self.root, {}), [None, 0])
        for text in ('', '\ninvalid\n', '{"event":"workflow_started","time":0}\n',
                '{"event":"workflow_started","time":19}\n{"event":"workflow_started","time":null}\nbroken',
                '{"event":"workflow_started","time":"opaque"}\r\n{"event":"other"}'):
            self.path.write_text(text)
            self.assertEqual(recovery.generation(self.root, {'execution_revision': 7}), full_scan(self.path, {'execution_revision': 7}))

    def test_large_log_skips_corrupt_partial_tail_and_preserves_last_start(self):
        self.path.write_text((json.dumps(dict(event='progress', content='x'*100))+'\n')*50000+
            '{"event":"workflow_started","time":21.5}\n'+
            '{"event":"progress","content":"'+'长'*100000+'"}\n'+
            'broken JSON\n{"event":"workflow_started","time":')
        expected = full_scan(self.path, {'execution_revision': 9})
        with patch.object(Path, 'read_text', side_effect=AssertionError('large logs must not use full read_text')):
            self.assertEqual(recovery.generation(self.root, {'execution_revision': 9}), expected)

    def test_cross_block_lines_and_all_splitlines_delimiters_match_forward_scan(self):
        starts = [json.dumps(dict(event='workflow_started', time=i)) for i in range(3)]
        for boundary in ('\n', '\r\n', '\r', '\v', '\f', '\x1c', '\x1d', '\x1e', '\x85', '\u2028', '\u2029'):
            text = boundary.join([starts[0], json.dumps(dict(event='progress', text='中文'*1000), ensure_ascii=False),
                starts[1], 'broken', starts[2], '{"truncated":'])
            self.path.write_text(text)
            expected = full_scan(self.path, {})
            for size in (7, 31, 127, 257):
                with self.subTest(boundary=repr(boundary), size=size), patch('recovery.EVENT_BLOCK_BYTES', size):
                    self.assertEqual(recovery.generation(self.root, {}), expected)

    def test_scan_stops_after_newest_valid_start_without_reading_old_prefix(self):
        self.path.write_text('{"event":"workflow_started","time":1}\n'+
            ('{"event":"progress"}\n'*100000)+'{"event":"workflow_started","time":2}\nbroken\n')
        actual_open = Path.open
        sizes = []
        class CountReads:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def seek(self, *args): return self.stream.seek(*args)
            def read(self, size): sizes.append(size); return self.stream.read(size)
        def opened(path, *args, **kwargs):
            stream = actual_open(path, *args, **kwargs)
            return CountReads(stream) if path == self.path and args == ('rb',) else stream
        with patch.object(Path, 'open', opened):
            self.assertEqual(recovery.generation(self.root, {}), [2, 0])
        self.assertLess(sum(sizes), self.path.stat().st_size//10)
