"""Keep the frozen Pi fixture intact and expose its state if the host fails.

This is a synthetic fixture only. Nested sandbox denial remains a real failed
check, never a skip, fallback, permission bypass or fabricated timeout proof.
"""
import json
from pathlib import Path
import sys
import unittest

import test_agents as fixtures


class InheritedTimeoutDiagnosticTests(unittest.TestCase):
    def test_original_timeout_resume_with_retained_failure_context(self):
        fixture=fixtures.AdapterTests('test_timeout_bounded_and_resume_uses_new_checkpoint')
        fixture.setUp()
        try:
            fixture.test_timeout_bounded_and_resume_uses_new_checkpoint()
        except Exception:
            root=fixture.repo/'.codex/tasks'
            evidence={}
            for p in sorted(root.rglob('*')):
                if p.is_file() and (p.name in ('state.json','events.jsonl','stderr.log') or p.suffix=='.log'):
                    evidence[str(p.relative_to(root))]=p.read_text(errors='replace')[-8000:]
                if len(evidence)>=16:break
            print('FROZEN_TIMEOUT_FIXTURE_CONTEXT '+json.dumps(evidence)[:65536],file=sys.stderr,flush=True)
            raise
        finally:
            fixture.tearDown()
