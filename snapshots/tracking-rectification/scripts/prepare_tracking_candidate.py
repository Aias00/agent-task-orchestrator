#!/usr/bin/env python3
"""Prepare/check the inactive, portable tracking candidate; never install it.

No live home, Git command, dependency install or external baseline is needed
for --check or for rebuilding an extracted archive. Provenance is vendored as
read-only reference bytes under docs/tracking-rectification/provenance.
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
BUILD = 'build/tracking-rectification'
MANIFEST = BUILD + '/candidate.json'
ARCHIVE = BUILD + '/candidate.tar.gz'
PROVENANCE = 'docs/tracking-rectification/provenance'
PATTERNS = ('*.py', '*.mjs', 'README.md', 'AGENTS.md', '.gitignore',
            '.github/workflows/*.yml', 'requirements*.lock', 'bin/*',
            'scripts/*.py', 'docs/**/*.md', 'docs/**/*.json', 'docs/**/*.py',
            'docs/tracking-rectification/evidence/*.log', 'skills/**/*.md',
            'tests/**/*.py', 'tests/**/*.json', 'tests/**/*.mjs',
            'examples/*.json', 'integrations/pi-acp/package.json',
            'integrations/pi-acp/package-lock.json')
FORBIDDEN = {'.git', '.codex', '.agents', '.aws', '.ssh', '.venv', 'node_modules',
             '__pycache__', 'private', 'state', 'credentials'}
# The docs contain this implementation identity; the full manifest separately
# pins their exact bytes. Keeping the two hashes distinct avoids a self-hash.
DELIVERY_DOCS = {f'docs/tracking-rectification/{n}.md' for n in
                 ('validation', 'migration', 'operator-handoff')}


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)+'\n').encode()


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def portable(name):
    path = Path(name)
    if (not name or path.is_absolute() or '..' in path.parts or
            path.as_posix() != name or set(path.parts) & FORBIDDEN):
        raise ValueError('unsafe candidate member: '+name)
    return path


def inventory(root):
    values, modes = {}, {}
    for pattern in PATTERNS:
        for path in root.glob(pattern):
            name = path.relative_to(root).as_posix()
            rel = portable(name)
            if any((root/Path(*rel.parts[:i])).is_symlink() for i in range(1, len(rel.parts)+1)):
                raise ValueError('symlink candidate input: '+name)
            if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('nonregular candidate input: '+name)
            values[name] = sha(path)
            # Normalize timestamps, owner and permissions across checkout/host.
            modes[name] = 0o755 if path.stat().st_mode & stat.S_IXUSR else 0o644
    return dict(sorted(values.items())), dict(sorted(modes.items()))


def implementation_sha256(values):
    return hashlib.sha256(encoded({n: h for n, h in values.items() if n not in DELIVERY_DOCS})).hexdigest()


def expected(root=ROOT):
    values, modes = inventory(root)
    basepath = root/PROVENANCE/'baseline.json'
    base = json.loads(basepath.read_text())
    for name, digest in base['protected_tests'].items():
        if values.get(name) != digest:
            raise ValueError('protected inherited test/fixture differs: '+name)
    # Every seeded portable source/test must remain; historical build outputs
    # are evidence of an earlier task and are deliberately not repackaged.
    required = {n for n in base['seed_files'] if not n.startswith('build/')}
    if not required <= values.keys():
        raise ValueError('seed source omitted: '+repr(sorted(required-values.keys())))
    return dict(version=1, purpose='ato-tracking-rectification-inactive-source',
        runtime_activated=False, files=values, file_modes=modes,
        implementation_sha256=implementation_sha256(values),
        provenance=dict(baseline_sha256=sha(basepath),
            acceptance_sha256=sha(root/PROVENANCE/'acceptance.py'),
            predecessor_tests=base['predecessor_test_count'], protected_files=len(base['protected_tests'])),
        changed_seed_files={n: dict(seed_sha256=base['seed_files'][n], candidate_sha256=values[n])
                            for n in sorted(required) if base['seed_files'][n] != values[n]},
        added_files=sorted(values.keys()-base['seed_files'].keys()),
        delivery=dict(state_included=False, dependencies_included=False,
            activation='not_performed_or_authorized', original_efficiency_qualification='independent_and_pending',
            host_regression='scheduler_owned', real_record_probe='final_origin_stage_only'))


def archive_bytes(manifest, root=ROOT):
    output = io.BytesIO()
    with gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as zipped:
        with tarfile.open(fileobj=zipped, mode='w', format=tarfile.USTAR_FORMAT) as bundle:
            for name in [*manifest['files'], MANIFEST]:
                data = encoded(manifest) if name == MANIFEST else (root/name).read_bytes()
                if name != MANIFEST and hashlib.sha256(data).hexdigest() != manifest['files'][name]:
                    raise ValueError('source changed during packaging: '+name)
                entry = tarfile.TarInfo(name)
                entry.size, entry.mode = len(data), manifest['file_modes'].get(name, 0o644)
                entry.mtime = entry.uid = entry.gid = 0
                bundle.addfile(entry, io.BytesIO(data))
    return output.getvalue()


def check(root=ROOT, archive=None):
    manifest = expected(root)
    if (root/MANIFEST).read_bytes() != encoded(manifest):
        raise ValueError('candidate manifest differs; prepare again, never activate')
    target = Path(archive) if archive else root/ARCHIVE
    if target.read_bytes() != archive_bytes(manifest, root):
        raise ValueError('archive bytes/inventory/metadata differ')
    receipt = dict(runtime_activated=False, implementation_sha256=manifest['implementation_sha256'],
                   manifest_sha256=sha(root/MANIFEST), archive_sha256=sha(target))
    if (root/BUILD/'hashes.json').exists():
        if json.loads((root/BUILD/'hashes.json').read_text()) != receipt:
            raise ValueError('delivery hash receipt differs')
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=('prepare',), help='prepare the inactive source bundle (default)')
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--archive', type=Path, help='check original archive after extraction; no live home needed')
    args = parser.parse_args(argv)
    if args.action and args.check:
        parser.error('prepare and --check are separate operations')
    if not args.check:
        if args.archive:
            parser.error('--archive is only valid with --check')
        manifest = expected()
        (ROOT/BUILD).mkdir(parents=True, exist_ok=True)
        (ROOT/MANIFEST).write_bytes(encoded(manifest))
        (ROOT/ARCHIVE).write_bytes(archive_bytes(manifest))
        # Detached final hashes avoid making the manifest hash itself. The
        # archive contains the manifest; this receipt travels beside both.
        receipt = dict(runtime_activated=False, implementation_sha256=manifest['implementation_sha256'],
                       manifest_sha256=sha(ROOT/MANIFEST), archive_sha256=sha(ROOT/ARCHIVE))
        (ROOT/BUILD/'hashes.json').write_bytes(encoded(receipt))
    print(json.dumps(check(archive=args.archive), sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
