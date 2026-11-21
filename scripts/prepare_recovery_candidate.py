#!/usr/bin/env python3
"""Pin the inert source candidate; never read or package private task state."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPS = Path(__file__).resolve().parents[1]/'operator-evidence/recovery-escalation'
OUTPUT = ROOT/'build/recovery-escalation/candidate.json'
EXTRA = {'workflow_lineage.py', 'docs/recovery-escalation-task.md',
         'docs/recovery-escalation.md', 'docs/recovery-escalation-migration.md',
         'build/recovery-escalation/validation-report.md',
         'scripts/prepare_recovery_candidate.py', 'tests/test_recovery_escalation.py',
         'tests/test_workflow_lineage.py'}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build():
    baseline = json.loads((OPS/'baseline.json').read_text())
    for name, digest in baseline['tests'].items():
        if sha(ROOT/name) != digest:
            raise ValueError('inherited test changed: '+name)
    files = {}
    runtime_sources = {p.name for p in ROOT.glob('*.py')} | {p.relative_to(ROOT).as_posix() for p in (ROOT/'ato_cli').glob('*.py')} | {'pyproject.toml', 'requirements-acp.lock', 'integrations/pi-acp/compatibility.json'}
    for name in sorted(set(baseline['files']) | EXTRA | runtime_sources):
        relative = Path(name)
        path = ROOT/relative
        if relative.is_absolute() or '..' in relative.parts or '.codex' in relative.parts or not path.is_file() or not path.resolve().is_relative_to(ROOT):
            raise ValueError('invalid candidate file: '+name)
        files[name] = sha(path)
    return dict(version=1, runtime_activated=False, baseline_manifest_sha256=sha(OPS/'baseline.json'),
                incident_sha256=sha(OPS/'incident.json'), files=files)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--check', action='store_true'); args = parser.parse_args()
    missing = [str(path) for path in (OPS/'baseline.json', OPS/'incident.json',) if not path.is_file()]
    if missing:
        print('Historical internal qualification packaging script; not source installation or daily tests.\n'
              'Required operator-evidence inputs are missing:\n  '+'\n  '.join(missing)+
              '\nSupply the original private qualification inputs explicitly; no runtime was activated.', file=sys.stderr)
        return 2
    data = build()
    if args.check:
        if json.loads(OUTPUT.read_text()) != data:
            raise ValueError('candidate differs from pinned source and evidence')
        print('candidate manifest verified; runtime not activated')
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(data, sort_keys=True, indent=2)+'\n')
        print('candidate manifest prepared; runtime not activated')


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError) as exc:
        print('Historical internal qualification packaging script (not installation or daily tests): '+str(exc), file=sys.stderr)
        raise SystemExit(1)
