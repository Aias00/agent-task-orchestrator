#!/usr/bin/env python3
"""Build/check a deterministic, inert source manifest; never activate a runtime."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT/'operator-evidence/agent-abstraction/baseline.json'
OUTPUT = ROOT/'build/agent-abstraction/candidate.json'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inputs():
    # Explicit portable source groups only: no task state, databases, logs or secrets.
    groups = [ROOT.glob('*.py'), ROOT.glob('*.mjs'), (ROOT/'docs').glob('*.md'),
              (ROOT/'skills').rglob('*.md'), (ROOT/'tests/fixtures').glob('*.py'), (ROOT/'ato_cli').glob('*.py')]
    files = {p for group in groups for p in group if p.is_file()}
    files.update(ROOT/name for name in (
        'pyproject.toml', 'requirements-acp.lock', 'README.md', 'bin/ctask', 'scripts/install.py', 'scripts/prepare_agent_candidate.py',
        'integrations/pi-acp/compatibility.json',
        'build/agent-abstraction/validation-report.md',
        'tests/test_agent_registry_contract.py', 'tests/test_agent_neutral_e2e.py',
        'tests/test_agent_lifecycle_contract.py', 'tests/test_agent_transport_contract.py'))
    files.update(ROOT/name for name in ('tests/hub_support.py', 'tests/test_hub_foundations.py',
        'tests/test_hub_adapters.py', 'tests/test_hub_loop.py'))
    for p in files:
        if not p.is_file() or not p.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError('missing or nonportable input: '+str(p))
    return sorted(files, key=lambda p: p.relative_to(ROOT).as_posix())


def build(baseline):
    return dict(version=1, runtime_activated=False, baseline_manifest_sha256=sha(baseline),
                files={p.relative_to(ROOT).as_posix(): sha(p) for p in inputs()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--baseline', type=Path, default=BASELINE)
    args = parser.parse_args()
    missing = [str(path) for path in (args.baseline,) if not path.is_file()]
    if missing:
        print('Historical internal qualification packaging script; not source installation or daily tests.\n'
              'Required operator-evidence inputs are missing:\n  '+'\n  '.join(missing)+
              '\nSupply the original private qualification inputs explicitly; no runtime was activated.', file=sys.stderr)
        return 2
    expected = build(args.baseline)
    if args.check:
        actual = json.loads(OUTPUT.read_text())
        if actual != expected:
            raise ValueError('candidate manifest differs from current portable source or baseline')
        print('candidate manifest verified; runtime not activated')
    else:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(expected, indent=2, sort_keys=True)+'\n')
        print('prepared '+str(OUTPUT.relative_to(ROOT))+'; runtime not activated')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
