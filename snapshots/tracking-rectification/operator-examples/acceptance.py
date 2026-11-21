#!/usr/bin/env python3
"""Fixed independent acceptance for a local ATO rectification candidate."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

OPS = Path(__file__).resolve().parent
BASE = json.loads((OPS / 'baseline.json').read_text())
ROOT = Path(BASE['repo'])


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def preserved():
    for name, digest in BASE['protected_tests'].items():
        assert sha(ROOT / name) == digest, 'inherited test/fixture changed: ' + name
    for name, digest in BASE['live_entrypoints_and_pending_candidate'].items():
        assert sha(name) == digest, 'live or pending external input changed: ' + name


def source_fingerprint():
    patterns = ['*.py', '*.mjs', 'README.md', 'AGENTS.md', 'requirements*.lock',
                'tests/**/*.py', 'tests/**/*.json', 'tests/**/*.mjs',
                'docs/**/*.md', 'docs/**/*.json', 'docs/**/*.py', 'bin/*',
                'skills/**/*.md', 'scripts/*.py',
                'integrations/pi-acp/package.json', 'integrations/pi-acp/package-lock.json']
    paths = {p for pattern in patterns for p in ROOT.glob(pattern) if p.is_file() and not p.is_symlink()}
    values = {str(p.relative_to(ROOT)): sha(p) for p in sorted(paths)}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def suite(mode):
    modules = {'workspace': 'test_tracking_workspace_binding',
               'approved_delivery': 'test_approved_delivery',
               'internal': 'test_internal_coordination',
               'responsibility': 'test_tracking_responsibility',
               'dedup': 'test_completion_lineage_dedup'}
    selected = None if mode == 'regression' else modules[mode]
    if selected:
        assert (ROOT / 'tests' / (selected + '.py')).is_file(), 'directed implementation test missing'
    else:
        assert all((ROOT / 'tests' / (v + '.py')).is_file() for v in modules.values())
    log = OPS / (mode + '-' + str(time.time_ns()) + '.log')
    result_path = log.with_suffix('.json')
    script = '''import json,sys,unittest
from pathlib import Path
root=Path(sys.argv[1]);sys.path[:0]=[str(root),str(root/'tests')]
loader=unittest.defaultTestLoader
suite=loader.loadTestsFromName(sys.argv[2]) if sys.argv[2]!='ALL' else loader.discover(str(root/'tests'))
result=unittest.TextTestRunner(verbosity=2).run(suite)
data=dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),skips=len(result.skipped),passed=result.wasSuccessful() and not result.skipped)
Path(sys.argv[3]).write_text(json.dumps(data,indent=2)+'\\n')
raise SystemExit(0 if data['passed'] else 1)
'''
    with tempfile.TemporaryDirectory(prefix='ato-rectification-acceptance-') as temp:
        env = {k: v for k, v in os.environ.items() if k not in ('CODEX_THREAD_ID', 'ATO_ACTOR_FILE', 'ATO_SESSION_REF')}
        env.update(ATO_HOME=str(Path(temp) / 'ato'), CODEX_HOME=str(Path(temp) / 'native'), PYTHONPATH=str(ROOT))
        with log.open('w') as output:
            proc = subprocess.run([sys.executable, '-c', script, str(ROOT), selected or 'ALL', str(result_path)],
                                  cwd=ROOT, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=1800)
    assert proc.returncode == 0, f'{mode} failed; independent evidence: {log}'
    result = json.loads(result_path.read_text())
    assert result['passed'] and result['tests'] > 0
    if mode == 'regression':
        assert result['tests'] > BASE['predecessor_test_count']
    return dict(log=str(log), result=result)


def bundle():
    script = ROOT / 'scripts/prepare_tracking_candidate.py'
    assert script.is_file(), 'candidate prepare/check command missing'
    proc = subprocess.run([sys.executable, str(script), '--check'], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    metadata = ROOT / 'build/tracking-rectification/candidate.json'
    archive = ROOT / 'build/tracking-rectification/candidate.tar.gz'
    manifest = json.loads(metadata.read_text())
    assert manifest['runtime_activated'] is False and manifest['files']
    for name, digest in manifest['files'].items():
        p = Path(name)
        assert not p.is_absolute() and '..' not in p.parts
        assert not set(p.parts) & {'.git', '.codex', 'node_modules', '__pycache__', '.venv', 'private'}
        assert sha(ROOT / p) == digest, 'candidate source changed: ' + name
    with tarfile.open(archive) as data:
        names = set()
        for member in data.getmembers():
            p = Path(member.name)
            assert not p.is_absolute() and '..' not in p.parts
            assert not member.issym() and not member.islnk()
            assert not set(p.parts) & {'.git', '.codex', 'node_modules', '.ssh', 'private', '.venv'}
            if member.isfile():
                names.add(member.name)
                if member.name in manifest['files']:
                    assert hashlib.sha256(data.extractfile(member).read()).hexdigest() == manifest['files'][member.name]
        assert set(manifest['files']) <= names
    for name in ['validation.md', 'operator-handoff.md', 'migration.md']:
        assert (ROOT / 'docs/tracking-rectification' / name).is_file()
    return dict(manifest_sha256=sha(metadata), archive_sha256=sha(archive), source_sha256=source_fingerprint())


def host():
    artifact = bundle()
    proof = json.loads((OPS / 'origin-readonly-proof.json').read_text())
    assert proof['source_sha256'] == source_fingerprint()
    assert proof['manifest_sha256'] == artifact['manifest_sha256']
    for key in ['workspace_binding_verified', 'accepted_scope_delivery_verified',
                'old_residuals_preserved', 'no_duplicate_approved_scope_diagnostic',
                'same_physical_delivery_dedup_verified', 'live_records_unchanged']:
        assert proof[key] is True, 'origin proof incomplete: ' + key
    assert proof['live_activation_performed'] is False
    assert proof['business_or_model_requests'] == 0 and proof['user_notifications_sent'] == 0
    for name, digest in proof['immutable_live_files'].items():
        assert sha(name) == digest, 'real source changed after readonly proof: ' + name
    assert proof['raw_old_workflow_status'] == 'blocked' and proof['real_delivery_status'] == 'completed'
    return proof


if __name__ == '__main__':
    mode = sys.argv[1]
    try:
        preserved()
        if mode in ('workspace', 'approved_delivery', 'internal', 'responsibility', 'dedup', 'regression'):
            evidence = suite(mode)
        elif mode == 'bundle':
            evidence = bundle()
        elif mode == 'host':
            evidence = host()
        elif mode == 'fingerprint':
            evidence = dict(source_sha256=source_fingerprint())
        else:
            raise ValueError('unknown acceptance mode')
        print(json.dumps(dict(mode=mode, passed=True, evidence=evidence), ensure_ascii=False))
    except (AssertionError, OSError, ValueError, KeyError, subprocess.SubprocessError, tarfile.TarError) as exc:
        print(json.dumps(dict(mode=mode, passed=False, error=str(exc)), ensure_ascii=False))
        raise SystemExit(1)
