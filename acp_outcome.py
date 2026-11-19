"""Correlated native and transport verdicts; ACP end_turn is not success."""
from dataclasses import dataclass

@dataclass
class Outcome:
    session: str
    turn: str
    native: str = 'unverified'
    transport: str = 'unverified'
    checks: str = 'unverified'

    def native_receipt(self, session, turn, stop_reason, *, trusted=False):
        if not trusted or session != self.session or turn != self.turn:
            return False
        if stop_reason in ('error', 'aborted', 'cancelled'):
            self.native = 'error'
        elif stop_reason == 'stop' and self.native != 'error':
            self.native = 'success'
        return True

    def transport_receipt(self, session, turn, reason):
        if session != self.session or turn != self.turn:
            return False
        if reason == 'end_turn' and self.transport != 'error':
            self.transport = 'completed'
        else:
            self.transport = 'error'
        return True

    @property
    def verdict(self):
        if self.native == 'error' or self.transport == 'error' or self.checks == 'failed':
            return 'failed'
        if self.native == 'success' and self.transport == 'completed' and self.checks == 'passed':
            return 'complete'
        return 'unverified'
