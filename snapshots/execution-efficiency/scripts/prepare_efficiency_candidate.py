#!/usr/bin/env python3
"""Build/check an inactive, reproducible execution-efficiency source bundle. Never installs.

The archive contains portable source, tests, docs and frozen provenance only.
Checking an extracted bundle needs Python's standard library, not dependencies,
accounts, a repository, or access to the original host's private configuration.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[1]
EXTERNAL_BASELINE = Path('OPERATOR_EVIDENCE/ato-execution-efficiency-20261119/baseline.json')
BUILD = Path('build/execution-efficiency')
BASELINE = BUILD/'provenance/baseline.json'
PREDECESSOR = BUILD/'provenance/predecessor-candidate.json'
MANIFEST = BUILD/'candidate.json'
ARCHIVE = BUILD/'candidate.tar.gz'
OLD_MANIFEST = Path('build/linger-continuation/candidate.json')
FORBIDDEN = frozenset({'.git', '.codex', '.aws', '.ssh', 'node_modules', 'private', '__pycache__', '.venv', 'auth'})
PATTERNS = ('*.py', '*.mjs', 'README.md', 'AGENTS.md', 'CONTINUATION.md', '.gitignore', 'requirements*.lock',
            'bin/*', 'scripts/*.py', 'docs/**/*.md', 'docs/**/*.json', 'docs/**/*.log', 'docs/**/evidence/*.py',
            'skills/**/*.md', 'tests/**/*.py', 'tests/**/*.json', 'tests/**/*.mjs',
            '.github/workflows/*.yml', '.github/workflows/*.yaml',
            'examples/*.json', 'integrations/pi-acp/package.json', 'integrations/pi-acp/package-lock.json')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_portable(root, name):
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts or set(relative.parts) & FORBIDDEN:
        raise ValueError('unsafe candidate path: '+str(name))
    for end in range(1, len(relative.parts)+1):
        if (root/Path(*relative.parts[:end])).is_symlink():
            raise ValueError('symlink candidate input: '+str(name))
    path = root/relative
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode) or mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise ValueError('nonportable candidate input: '+str(name))
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('candidate input outside root')
    return path.read_bytes(), stat.S_IMODE(mode)


def inventory(root):
    names = {p.relative_to(root).as_posix() for pattern in PATTERNS for p in root.glob(pattern)}
    files, modes = {}, {}
    for name in sorted(names):
        data, modes[name] = read_portable(root, name)
        files[name] = digest(data)
    return files, modes


def build(root=ROOT):
    baseline_bytes, _ = read_portable(root, BASELINE)
    predecessor_bytes, _ = read_portable(root, PREDECESSOR)
    baseline = json.loads(baseline_bytes)
    if digest(predecessor_bytes) != baseline['predecessor_manifest_sha256']:
        raise ValueError('predecessor manifest identity differs from frozen baseline')
    files, modes = inventory(root)
    for name, expected in baseline['protected_tests'].items():
        if files.get(name) != expected:
            raise ValueError('protected predecessor test/fixture differs: '+name)
    if baseline['predecessor_test_count'] != 677:
        raise ValueError('unexpected qualified Linger predecessor test count')
    # Include the predecessor portable provenance, not private task state.
    for name in sorted(baseline['seed_files']):
        if name.startswith('build/'):
            data, mode = read_portable(root, name)
            files[name], modes[name] = digest(data), mode
    # Provenance is data, not executable authority or a claim of qualification.
    files.update({BASELINE.as_posix(): digest(baseline_bytes), PREDECESSOR.as_posix(): digest(predecessor_bytes)})
    modes.update({BASELINE.as_posix(): 0o644, PREDECESSOR.as_posix(): 0o644})
    for name in (BASELINE, PREDECESSOR):
        if read_portable(root, name)[1] != modes[name.as_posix()]:
            raise ValueError('provenance mode differs')
    if not set(baseline['seed_files']) <= set(files):
        raise ValueError('candidate omits seeded portable files')
    return dict(version=1, runtime_activated=False, overall_accepted=False,
        baseline_sha256=digest(baseline_bytes),
        predecessor_manifest_sha256=baseline['predecessor_manifest_sha256'],
        files=dict(sorted(files.items())), file_modes=dict(sorted(modes.items())),
        source_delta={name: dict(seed_sha256=baseline['seed_files'].get(name), candidate_sha256=sha)
                      for name, sha in sorted(files.items()) if baseline['seed_files'].get(name) != sha},
        qualification=dict(status='original_host_regression_delivery_and_origin_actual_codex_required',
            evidence='docs/execution-efficiency/validation.md',
            predecessor='Retained proof qualifies the unchanged predecessor only; no actual Codex/provider call performed by the worker.',
            full_objective='docs/execution-efficiency/task.md'),
        delivery=dict(format='deterministic-ustar-gzip-v1', state_included=False,
            dependency_link_included=False, activation='not_authorized_or_performed',
            migration='docs/execution-efficiency/migration.md',
            rollback='docs/execution-efficiency/operator-handoff.md'))


def encoded(manifest):
    return (json.dumps(manifest, indent=2, sort_keys=True)+'\n').encode()


def archive_bytes(root, manifest):
    buffer = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=buffer, mtime=0, compresslevel=9) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as bundle:
            for name in [*manifest['files'], MANIFEST.as_posix()]:
                if name == MANIFEST.as_posix():
                    data, mode = encoded(manifest), 0o644
                else:
                    data, mode = read_portable(root, name)
                    if digest(data) != manifest['files'][name] or mode != manifest['file_modes'][name]:
                        raise ValueError('candidate changed during packaging: '+name)
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(data), mode
                info.mtime = info.uid = info.gid = 0
                bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.candidate-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def output_path(root, name):
    path = root/name
    for end in range(1, len(name.parts)+1):
        if (root/Path(*name.parts[:end])).is_symlink():
            raise ValueError('symlink candidate output: '+str(name))
    return path


def prepare(root=ROOT, baseline=EXTERNAL_BASELINE):
    baseline_data = baseline.read_bytes()
    predecessor_data, _ = read_portable(root, OLD_MANIFEST)
    if digest(predecessor_data) != json.loads(baseline_data)['predecessor_manifest_sha256']:
        raise ValueError('original predecessor manifest changed')
    for name, data in ((BASELINE, baseline_data), (PREDECESSOR, predecessor_data)):
        destination = output_path(root, name)
        if destination.exists() and read_portable(root, name)[0] != data:
            raise ValueError('refusing to overwrite frozen provenance: '+str(name))
        atomic_write(destination, data)
    manifest = build(root)
    archive = archive_bytes(root, manifest)
    atomic_write(output_path(root, MANIFEST), encoded(manifest))
    atomic_write(output_path(root, ARCHIVE), archive)
    return manifest


def check(root=ROOT, baseline=None):
    expected = build(root)
    if read_portable(root, MANIFEST)[0] != encoded(expected):
        raise ValueError('candidate source/inventory/mode/manifest differs; no activation')
    if read_portable(root, ARCHIVE)[0] != archive_bytes(root, expected):
        raise ValueError('candidate archive bytes differ from reproducible source bundle')
    if baseline is not None and baseline.exists() and digest(baseline.read_bytes()) != expected['baseline_sha256']:
        raise ValueError('external baseline differs from bundled provenance')
    if (root/OLD_MANIFEST).exists() and digest(read_portable(root, OLD_MANIFEST)[0]) != expected['predecessor_manifest_sha256']:
        raise ValueError('historical predecessor manifest differs')
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--check-extracted', action='store_true', help='verify portable extracted inventory without archive nesting')
    args = parser.parse_args()
    if args.check_extracted:
        manifest = build(ROOT)
        if read_portable(ROOT, MANIFEST)[0] != encoded(manifest):
            raise ValueError('extracted source/inventory/modes differ')
    else:
        manifest = check(baseline=EXTERNAL_BASELINE) if args.check else prepare()
    print(json.dumps(dict(files=len(manifest['files']), runtime_activated=False,
        overall_accepted=False, checked=args.check, manifest=str(MANIFEST), archive=str(ARCHIVE))))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
