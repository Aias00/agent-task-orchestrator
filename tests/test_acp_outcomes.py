import json
import unittest
from pathlib import Path
from acp_outcome import Outcome

REPLAY = Path(__file__).parent/'fixtures/pi-error-end-turn.json'

class OutcomeTests(unittest.TestCase):
    def fresh(self): return Outcome('session', 'turn')
    def test_pi_native_error_beats_end_turn(self):
        replay = json.loads(REPLAY.read_text())
        o = self.fresh()
        o.native_receipt('session', 'turn', replay['actual_native_first_stop_reason'], trusted=True)
        o.transport_receipt('session', 'turn', replay['raw_acp_first_stop_reason'])
        o.checks = 'passed'
        self.assertEqual(o.verdict, 'failed')
    def test_end_turn_cannot_complete_task_before_checks(self):
        o = self.fresh(); o.native_receipt('session', 'turn', 'stop', trusted=True)
        o.transport_receipt('session', 'turn', 'end_turn')
        self.assertEqual(o.verdict, 'unverified')
    def test_model_text_cannot_forge_native_receipt(self):
        o = self.fresh(); self.assertFalse(o.native_receipt('session', 'turn', 'stop'))
        self.assertEqual(o.verdict, 'unverified')
    def test_missing_terminal_receipt_stays_unverified(self):
        o = self.fresh(); o.transport_receipt('session', 'turn', 'end_turn'); o.checks = 'passed'
        self.assertEqual(o.verdict, 'unverified')
    def test_identity_mismatch_is_rejected(self):
        o = self.fresh()
        self.assertFalse(o.native_receipt('other', 'turn', 'stop', trusted=True))
        self.assertFalse(o.native_receipt('session', 'old', 'stop', trusted=True))
        self.assertEqual(o.native, 'unverified')
    def test_late_error_is_sticky(self):
        o = self.fresh(); o.native_receipt('session', 'turn', 'stop', trusted=True)
        o.native_receipt('session', 'turn', 'error', trusted=True)
        o.native_receipt('session', 'turn', 'stop', trusted=True)
        self.assertEqual(o.native, 'error')
    def test_checks_failure_wins(self):
        o = self.fresh(); o.native_receipt('session', 'turn', 'stop', trusted=True)
        o.transport_receipt('session', 'turn', 'end_turn'); o.checks = 'failed'
        self.assertEqual(o.verdict, 'failed')
    def test_all_three_required(self):
        o = self.fresh(); o.native_receipt('session', 'turn', 'stop', trusted=True)
        o.transport_receipt('session', 'turn', 'end_turn'); o.checks = 'passed'
        self.assertEqual(o.verdict, 'complete')
