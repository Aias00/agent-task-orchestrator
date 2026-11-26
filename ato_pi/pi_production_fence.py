"""Per-action production fence. Host injects its existing budget/lease guard."""
import hashlib
import json
from pathlib import Path
import threading
import time

from ato_common.ato_core import digest


class ProductionFence:
    def __init__(self, binding, *, owner_guard, profile_hash, max_cost_microusd,
                 hard_timeout_seconds, max_requests=1, clock=time.monotonic):
        if (not callable(owner_guard) or type(binding) is not dict
                or set(binding) != {'workflow_id', 'project', 'contract_hash', 'generation'}
                or not Path(binding['workflow_id']).is_absolute()
                or not Path(binding['project']).is_absolute()
                or type(binding['generation']) is not int or binding['generation'] < 0
                or type(max_cost_microusd) is not int or max_cost_microusd < 1
                or type(max_requests) is not int or not 1 <= max_requests <= 16
                or type(hard_timeout_seconds) is not int or not 1 <= hard_timeout_seconds <= 60):
            raise ValueError('invalid production fence')
        self.binding = dict(binding)
        self.owner_guard = owner_guard
        self.profile_hash = profile_hash
        self.clock = clock
        self.deadline = clock() + hard_timeout_seconds
        self.limit = max_cost_microusd
        self.max_requests = max_requests
        self.reserved = self.requests = 0
        self.revoked = False
        self.lock = threading.RLock()

    def check(self, profile_hash):
        with self.lock:
            try:
                if self.revoked or self.clock() >= self.deadline or profile_hash != self.profile_hash:
                    raise ValueError('production fence closed')
                d = Path(self.binding['workflow_id'])
                state = json.loads((d/'state.json').read_text())
                contract = json.loads((d/'contract.json').read_text())
                if ((d/'STOP').exists() or (d/'user-closure.json').exists()
                        or state['status'] in ('stopped', 'completed', 'cleanup_blocked')
                        or state['repo'] != self.binding['project']
                        or state['contract_hash'] != self.binding['contract_hash']
                        or digest(contract) != self.binding['contract_hash']
                        or state.get('execution_revision', 0) != self.binding['generation']):
                    raise ValueError('production workflow stale')
                # Includes W1 deadline progress recheck; never duplicate budget math here.
                self.owner_guard()
                if self.clock() >= self.deadline:
                    raise ValueError('production request deadline')
            except BaseException:
                self.revoked = True
                raise

    def authorize(self, profile_hash, *, request=False, cost_microusd=0):
        with self.lock:
            self.check(profile_hash)
            if request:
                if (type(cost_microusd) is not int or cost_microusd < 1
                        or self.requests >= self.max_requests
                        or self.reserved + cost_microusd > self.limit):
                    self.revoked = True
                    raise ValueError('production request allowance exhausted')
                self.requests += 1
                self.reserved += cost_microusd
            return {'allowed': True, 'remaining_ms': max(1, int((self.deadline-self.clock())*1000))}

    def revoke(self):
        with self.lock:
            self.revoked = True

    def snapshot(self):
        # No refund on missing usage, EOF, abort or unknown delivery.
        with self.lock:
            return dict(requests=self.requests, reserved_microusd=self.reserved, revoked=self.revoked)
