"""Trusted CLI bootstrap for synthetic offline tests, never production authority.

Candidate runner and local_cli adapter are real. Only the bounded worker/planner
is synthetic. All Codex native entry points fail if accidentally reached.
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import agents
import agent_registry
import codex_session_adapter
import appserver


def unavailable(*args, **kwargs):
    raise AssertionError('Codex executable/index/app-server is inaccessible in neutral CLI fixture')


for name in ('session_path', 'queue', 'create_session', 'current', 'observe_recovery', 'observe_reply'):
    setattr(codex_session_adapter, name, unavailable)
appserver.Client = unavailable

from fixture_adapter import register
register()
agent_registry.set_runtime_entrypoint(str(Path(__file__).resolve()))
import runner

if __name__ == '__main__':
    raise SystemExit(runner.main())
