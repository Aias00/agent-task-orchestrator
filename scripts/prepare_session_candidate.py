#!/usr/bin/env python3
"""Prepare/check an inert, complete session-decoupling source candidate.

Uses the external actual-live baseline, never the historical source-manifest.
Does not install, migrate state, launch services, or activate the candidate.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import stat
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT/'operator-evidence/session-decoupling/baseline.json'
BUILD = ROOT/'build/session-decoupling'
OUTPUT = BUILD/'candidate.json'
ARCHIVE = BUILD/'candidate.tar.gz'
SEED = BUILD/'stage-start.json'
PATTERNS = ('ato_cli/**/*.py', 'pyproject.toml', 'requirements-acp.lock', '*.py', '*.mjs', 'README.md', 'AGENTS.md', 'bin/*', 'scripts/*.py',
            'docs/**/*.md', 'skills/**/*.md', 'tests/**/*.py', 'tests/**/*.json',
            'integrations/pi-acp/package.json', 'integrations/pi-acp/package-lock.json')
FORBIDDEN = {'.git', '.codex', 'node_modules', 'private', '__pycache__', '.aws', '.ssh'}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files(root):
    result = {}
    for pattern in PATTERNS:
        for path in root.glob(pattern):
            relative = path.relative_to(root)
            if (any((root/Path(*relative.parts[:end])).is_symlink() for end in range(1, len(relative.parts)+1))
                    or not path.is_file() or set(relative.parts) & FORBIDDEN):
                raise ValueError('nonportable/unsafe candidate input: '+str(relative))
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('candidate input outside source root')
            result[relative.as_posix()] = sha(path)
    return dict(sorted(result.items()))


def delta(before, after):
    return {name: dict(seeded_sha256=before.get(name), candidate_sha256=after.get(name))
            for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)}


def build(root=ROOT, baseline=BASELINE, seed=SEED):
    base = json.loads(baseline.read_text())
    initial = json.loads(seed.read_text())
    current = files(root)
    for name, expected in base['inherited_tests'].items():
        if current.get(name) != expected:
            raise ValueError('protected inherited test/fixture differs: '+name)
    if not set(base['actual_runtime_files']) <= set(current):
        raise ValueError('candidate omits actual-live runtime files')
    patch = root/'build/session-decoupling/seeded-diff.patch'
    return dict(version=3, runtime_activated=False, baseline_sha256=sha(baseline),
        files=current, file_modes={name: stat.S_IMODE((root/name).stat().st_mode) for name in current},
        seeded_git_head=initial['git_head'], seeded_checkpoint_sha256=sha(seed),
        seeded_dirty_diff_sha256=sha(patch),
        actual_live_to_candidate_delta=delta(base['actual_runtime_files'],
            {name: current[name] for name in base['actual_runtime_files']}),
        qualification_stage_delta=delta(initial['files'], current),
        qualification=dict(offline='See docs/session-decoupling/validation.md; logs are evidence, not acceptance',
            actual_pi_local_model='origin_host_required; retained external proof is bound to this manifest hash',
            cloud_model='not_tested', native_load='unsupported; checkpoint_reconstruction only'),
        delivery=dict(activation='not_authorized_or_performed',
            migration='docs/session-decoupling/migration.md',
            rollback='docs/session-decoupling/operator-handoff.md',
            state_included=False, dependency_link_included=False))


def encoded(manifest):
    return (json.dumps(manifest, indent=2, sort_keys=True)+'\n').encode()


def archive_members(manifest):
    return [*manifest['files'], 'build/session-decoupling/candidate.json']


def write_archive(path, manifest, root=ROOT):
    """Deterministic review bundle: source and manifest only, never state."""
    with path.open('wb') as output, gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as bundle:
            for name in archive_members(manifest):
                data = (root/name).read_bytes() if name in manifest['files'] else encoded(manifest)
                if name in manifest['files'] and hashlib.sha256(data).hexdigest() != manifest['files'][name]:
                    raise ValueError('candidate changed during packaging: '+name)
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = manifest['file_modes'].get(name, 0o644)
                info.mtime = info.uid = info.gid = 0
                bundle.addfile(info, io.BytesIO(data))


def check_archive(path, manifest):
    """Inspect bytes without extracting or executing any bundle member."""
    with tarfile.open(path, 'r:gz') as bundle:
        members = bundle.getmembers()
        if [member.name for member in members] != archive_members(manifest):
            raise ValueError('candidate archive inventory differs')
        for member in members:
            if (not member.isfile() or member.uid or member.gid or member.mtime
                    or member.mode != manifest['file_modes'].get(member.name, 0o644)):
                raise ValueError('candidate archive metadata differs')
            data = bundle.extractfile(member).read()
            expected = manifest['files'].get(member.name)
            matches = hashlib.sha256(data).hexdigest() == expected if expected else data == encoded(manifest)
            if not matches:
                raise ValueError('candidate archive content differs: '+member.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--archive', action='store_true', help='also write/check deterministic source tar.gz')
    args = parser.parse_args()
    missing = [str(path) for path in (BASELINE, SEED,) if not path.is_file()]
    if missing:
        print('Historical internal qualification packaging script; not source installation or daily tests.\n'
              'Required operator-evidence inputs are missing:\n  '+'\n  '.join(missing)+
              '\nSupply the original private qualification inputs explicitly; no runtime was activated.', file=sys.stderr)
        return 2
    expected = build()
    if args.check:
        if json.loads(OUTPUT.read_text()) != expected:
            raise ValueError('candidate inventory/hash/baseline/delta changed; no activation')
        if args.archive or ARCHIVE.exists():
            check_archive(ARCHIVE, expected)
        print('complete source candidate independently checked; runtime_activated=false')
    else:
        BUILD.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_bytes(encoded(expected))
        if args.archive:
            write_archive(ARCHIVE, expected)
            check_archive(ARCHIVE, expected)
        print(str(OUTPUT))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
