#!/usr/bin/env python3
"""Prepare or check the inert, reproducible ACP source manifest."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPS = ROOT/'operator-evidence/acp-process-integration'
OUTPUT = ROOT/'build/acp-process/candidate.json'
FILES = (
    'runner.py', 'ato_workflow/workflow.py', 'ato_adapters/agents.py', 'agent_registry.py', 'ato_workflow/progress.py',
    'ato_workflow/control.py', 'ato_workflow/host_recheck.py', 'ato_workflow/permissions.py', 'ato_workflow/budget.py', 'recovery.py',
    'ato_adapters/acp_bridge.py', 'ato_adapters/acp_outcome.py', 'ato_workflow/task_trace.py', 'ato_pi/pi_acp_worker.py',
    'ato_pi/pi_acp_exec.py', 'pi-path-guard.mjs', 'ato_adapters/agent_native_events.py',
    'ato_pi/pi_toolchain.py', 'ato_pi/pi_acp_compat.py', 'integrations/pi-acp/compatibility.json',
    'ato_pi/pi_production_credentials.py', 'ato_pi/pi_production_fence.py', 'ato_pi/pi_production_profile.py',
    'ato_pi/pi_production_egress.py', 'ato_pi/pi_production_exec.py', 'pi_production_transport.mjs',
    'tests/pi_toolchain_support.py',
    'ato_review/hub_contract.py', 'ato_review/hub_sandbox.py', 'ato_review/hub_worktree.py', 'ato_review/hub_ledger.py',
    'ato_adapters/hub_adapter_agy.py', 'ato_adapters/hub_adapter_claude.py',
    'ato_adapters/hub_adapter_codex.py', 'ato_adapters/hub_registry.py',
    'ato_review/hub_loop.py', 'ato.py', 'skills/ato/SKILL.md',
    'tests/hub_support.py', 'tests/test_hub_foundations.py', 'tests/test_hub_adapters.py',
    'tests/test_hub_loop.py', 'tests/fixtures/hub_cli_fixture.py', 'tests/fixtures/hub_worker_fixture.py',
    'scripts/derive_pi_acp.py', 'scripts/create_acp_process_demo.py',
    'scripts/history/prepare_acp_process_candidate.py',
    'tests/fixtures/acp_fixture_cli.py', 'tests/fixtures/acp_fixture_agent.py',
    'tests/fixtures/pi_rpc_fixture.py', 'tests/test_acp_outcomes.py',
    'tests/test_task_trace.py', 'tests/test_acp_scheduler_integration.py',
    'docs/history/acp-process.md', 'docs/history/acp-process-migration.md', 'docs/history/acp-process-demo.md',
    'build/acp-process/validation-report.md',
)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def build():
    files = {}
    inputs = set(FILES)
    for group in (ROOT.glob('*.py'), ROOT.glob('*.mjs'), (ROOT/'tests').glob('test*.py'),
                  (ROOT/'tests/fixtures').glob('*.py'), (ROOT/'docs').glob('*.md'),
                  (ROOT/'docs/history').glob('*.md'), (ROOT/'skills').rglob('*.md'),
                  ROOT.glob('ato_*/*.py')):
        inputs.update(p.relative_to(ROOT).as_posix() for p in group if p.is_file())
    inputs.update(('pyproject.toml', 'requirements-acp.lock', 'README.md', 'bin/ato', 'scripts/install.py',
                   'scripts/history/prepare_agent_candidate.py'))
    for name in sorted(inputs):
        path = ROOT/name
        if not path.is_file() or not path.resolve().is_relative_to(ROOT):
            raise ValueError('missing or nonlocal candidate input: '+name)
        files[name] = sha(path)
    for name in ('ato_pi/pi_acp_exec.py', 'tests/fixtures/pi_rpc_fixture.py'):
        if not (ROOT/name).stat().st_mode & 0o111:
            raise ValueError('required fixture or launcher is not executable: '+name)
    return {'version': 1, 'runtime_activated': False,
            'baseline_manifest_sha256': sha(OPS/'baseline.json'),
            'recorded_pi_fixture_sha256': sha(OPS/'real-pi-replay.json'), 'files': files}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    missing = [str(path) for path in (OPS/'baseline.json', OPS/'real-pi-replay.json',) if not path.is_file()]
    if missing:
        print('Historical internal qualification packaging script; not source installation or daily tests.\n'
              'Required operator-evidence inputs are missing:\n  '+'\n  '.join(missing)+
              '\nSupply the original private qualification inputs explicitly; no runtime was activated.', file=sys.stderr)
        return 2
    expected = build()
    if args.check:
        if json.loads(OUTPUT.read_text()) != expected:
            raise ValueError('manifest differs from current source or external evidence')
        print('candidate manifest verified; runtime not activated')
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(expected, indent=2, sort_keys=True)+'\n')
        print('candidate manifest prepared; runtime not activated')

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print('Historical internal qualification packaging script (not installation or daily tests): '+str(exc), file=sys.stderr)
        raise SystemExit(1)
