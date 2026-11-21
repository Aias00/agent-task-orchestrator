#!/usr/bin/env python3
"""Prepare or check the inert, reproducible ACP source manifest."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPS = Path(__file__).resolve().parents[1]/'operator-evidence/acp-process-integration'
OUTPUT = ROOT/'build/acp-process/candidate.json'
FILES = (
    'runner.py', 'workflow.py', 'agents.py', 'agent_registry.py', 'progress.py',
    'control.py', 'host_recheck.py', 'permissions.py', 'budget.py', 'recovery.py',
    'acp_bridge.py', 'acp_outcome.py', 'task_trace.py', 'pi_acp_worker.py',
    'pi_acp_exec.py', 'pi-path-guard.mjs', 'agent_native_events.py',
    'pi_toolchain.py', 'integrations/pi-acp/compatibility.json',
    'tests/pi_toolchain_support.py',
    'scripts/derive_pi_acp.py', 'scripts/create_acp_process_demo.py',
    'scripts/prepare_acp_process_candidate.py',
    'tests/fixtures/acp_fixture_cli.py', 'tests/fixtures/acp_fixture_agent.py',
    'tests/fixtures/pi_rpc_fixture.py', 'tests/test_acp_outcomes.py',
    'tests/test_task_trace.py', 'tests/test_acp_scheduler_integration.py',
    'docs/acp-process.md', 'docs/acp-process-migration.md', 'docs/acp-process-demo.md',
    'build/acp-process/validation-report.md',
)

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def build():
    files = {}
    inputs = set(FILES)
    for group in (ROOT.glob('*.py'), ROOT.glob('*.mjs'), (ROOT/'tests').glob('test*.py'),
                  (ROOT/'tests/fixtures').glob('*.py'), (ROOT/'docs').glob('*.md'),
                  (ROOT/'skills/codex-long-task').rglob('*.md'), (ROOT/'ato_cli').glob('*.py')):
        inputs.update(p.relative_to(ROOT).as_posix() for p in group if p.is_file())
    inputs.update(('pyproject.toml', 'requirements-acp.lock', 'README.md', 'bin/ctask', 'scripts/install.py', 'scripts/prepare_agent_candidate.py'))
    for name in sorted(inputs):
        path = ROOT/name
        if not path.is_file() or not path.resolve().is_relative_to(ROOT):
            raise ValueError('missing or nonlocal candidate input: '+name)
        files[name] = sha(path)
    for name in ('pi_acp_exec.py', 'tests/fixtures/pi_rpc_fixture.py'):
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
