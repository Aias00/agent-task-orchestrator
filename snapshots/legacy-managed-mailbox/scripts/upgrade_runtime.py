#!/usr/bin/env python3
"""Assess or stage an offline upgrade; never replace a runtime, launcher or live state.

Legacy JSON/controller SQLite need no conversion. Optional offline snapshots are
byte copies for rehearsal/rollback, not imports into the new mailbox. Restore
only creates a separate destination; activation and ledger reconciliation remain
explicit operator actions. No native auth/config directories are copied.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys


SOURCE = Path(__file__).resolve().parents[1]
FORBIDDEN = {'auth.json', 'credentials.json', '.credentials.json', '.env',
             'models.json', 'settings.json', 'settings.local.json'}


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def files(root):
    """Do not follow links, native auth, or special files during offline copies."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError('snapshot root must be a regular directory')
    result = []
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('symlinks are not supported in staged copies')
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError('special files are not supported in staged copies')
        if path.name in FORBIDDEN or path.name.startswith('.env.'):
            raise ValueError('native configuration/credentials are not snapshot inputs')
        if path.name.endswith(('-wal', '-shm', '-journal')):
            raise ValueError('SQLite sidecar present; stop owners and checkpoint before snapshot')
        result.append(path)
    return result


def source_files():
    paths = list(SOURCE.glob('*.py')) + list(SOURCE.glob('*.mjs')) + [SOURCE/'README.md']
    for directory, pattern in [('scripts', '*.py'), ('skills', '*.md'), ('docs', '*.md')]:
        paths.extend((SOURCE/directory).rglob(pattern))
    for path in paths:
        if path.is_symlink() or not path.is_file():
            raise ValueError('source must consist of regular files')
    return sorted(paths)


def separate(destination, roots):
    if destination.exists():
        raise ValueError('destination must not exist; no existing files are overwritten')
    for root in roots:
        root = root.resolve()
        if destination == root or destination.is_relative_to(root) or root.is_relative_to(destination):
            raise ValueError('destination must be separate from source/runtime/state')


def copy_private(source, target):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copyfile(source, target)
    target.chmod(0o600)


def assess(runtime):
    runtime = runtime.resolve(strict=True)
    if not runtime.is_dir() or not (runtime/'runner.py').is_file():
        raise ValueError('runtime must be an existing deployment containing runner.py')
    if (runtime/'runner.py').is_symlink():
        raise ValueError('runtime runner.py must be a regular file')
    return dict(schema_version=1, action='dry-run', runtime=str(runtime), source=str(SOURCE),
        runtime_runner_sha256=digest(runtime/'runner.py'),
        source_sha256={str(p.relative_to(SOURCE)): digest(p) for p in source_files()},
        activation_performed=False, legacy_conversion='none; retain JSON and controller tables',
        mailbox='additive separate schema v1; never import old outboxes as fresh deliveries',
        rollback='old runtime remains unchanged; new managed origins require this version',
        prerequisites=['independent full/baseline/native acceptance',
                       'stop and verify all runtime/state owners before installation or snapshot',
                       'preserve current cumulative budgets, STOP, failed and uncertain deliveries',
                       'retain private mailbox capabilities and native transcripts together'])


def stage(runtime, destination, state_roots, offline):
    if destination.is_symlink() or any(p.is_symlink() for p in state_roots):
        raise ValueError('stage destination and state roots must not be symlinks')
    destination = destination.resolve()
    report = assess(runtime)
    if state_roots and not offline:
        raise ValueError('state snapshots require explicit --offline after owners are stopped')
    roots = [p.resolve(strict=True) for p in state_roots]
    for root in roots:
        if not root.is_dir() or root.name not in ('tasks', 'ctask-interaction'):
            raise ValueError('state roots must be ctask tasks or ctask-interaction directories')
        if root == Path('/').resolve():
            raise ValueError('invalid state root')
    if len(set(roots)) != len(roots) or any(a != b and a.is_relative_to(b) for a in roots for b in roots):
        raise ValueError('state roots must be distinct and nonoverlapping')
    separate(destination, [SOURCE, runtime, *roots])
    inputs = [(root, files(root)) for root in roots]
    code = source_files()
    destination.mkdir(mode=0o700, parents=True)
    try:
        for path in code:
            copy_private(path, destination/'runtime'/path.relative_to(SOURCE))
        snapshots = []
        for index, (root, paths) in enumerate(inputs):
            relative = 'state/'+str(index)
            (destination/relative).mkdir(parents=True, mode=0o700)
            hashes = {}
            for path in paths:
                name = str(path.relative_to(root))
                copy_private(path, destination/relative/name)
                hashes[name] = digest(path)
                if digest(destination/relative/name) != hashes[name]:
                    raise ValueError('state changed during snapshot; discard and quiesce owners')
            snapshots.append(dict(original=str(root), path=relative, sha256=hashes))
        # A source change while copying invalidates the staging manifest.
        for name, expected in report['source_sha256'].items():
            if digest(destination/'runtime'/name) != expected:
                raise ValueError('source changed during staging; rerun with stable inputs')
        report.update(action='stage', stage=str(destination), snapshots=snapshots)
        manifest = destination/'upgrade-manifest.json'
        manifest.write_text(json.dumps(report, indent=2)+'\n'); manifest.chmod(0o600)
        return report
    except BaseException:
        shutil.rmtree(destination)
        raise


def restore(stage_path, destination, offline):
    if destination.is_symlink():
        raise ValueError('restore destination must not be a symlink')
    destination = destination.resolve()
    if not offline:
        raise ValueError('restore rehearsal requires --offline; no live state is overwritten')
    if stage_path.is_symlink() or (stage_path/'upgrade-manifest.json').is_symlink():
        raise ValueError('restore manifest must not be a symlink')
    manifest = json.loads((stage_path/'upgrade-manifest.json').read_text())
    if manifest.get('schema_version') != 1 or manifest.get('action') != 'stage':
        raise ValueError('unsupported staging manifest')
    separate(destination, [stage_path, SOURCE, Path(manifest['runtime']),
                           *(Path(row['original']) for row in manifest['snapshots'])])
    copies = []
    for index, row in enumerate(manifest['snapshots']):
        if row['path'] != 'state/'+str(index):
            raise ValueError('invalid snapshot location')
        if (stage_path/'state').is_symlink():
            raise ValueError('snapshot directory must not be a symlink')
        root = stage_path/row['path']
        paths = files(root)
        actual = {str(p.relative_to(root)): digest(p) for p in paths}
        if actual != row['sha256']:
            raise ValueError('snapshot integrity mismatch; nothing restored')
        copies.extend((p, Path(str(index))/p.relative_to(root)) for p in paths)
    destination.mkdir(parents=True, mode=0o700)
    try:
        for source, relative in copies:
            copy_private(source, destination/relative)
        # Preserve a map without re-binding original paths or identities.
        target = destination/'restore-map.json'
        target.write_text(json.dumps(manifest['snapshots'], indent=2)+'\n'); target.chmod(0o600)
    except BaseException:
        shutil.rmtree(destination)
        raise
    return dict(schema_version=1, action='restore-rehearsal', destination=str(destination),
                files=len(copies), activation_performed=False,
                warning='No live paths changed. Reconcile later usage/history before any manual rollback.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--stage', type=Path, help='Create a NEW isolated staging directory')
    mode.add_argument('--restore', type=Path, help='Verify staged snapshots and restore to a NEW directory')
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--state-root', action='append', type=Path, default=[])
    parser.add_argument('--destination', type=Path, help='NEW restore rehearsal directory')
    parser.add_argument('--offline', action='store_true', help='Operator confirms snapshot owners stopped')
    args = parser.parse_args()
    try:
        if args.restore:
            if not args.destination or args.runtime or args.state_root:
                raise ValueError('--restore requires only --destination and --offline')
            result = restore(args.restore.expanduser().absolute(), args.destination.expanduser().absolute(), args.offline)
        else:
            if not args.runtime or args.destination:
                raise ValueError('--runtime is required; --destination is restore-only')
            if args.dry_run and (args.state_root or args.offline):
                raise ValueError('dry-run does not open/snapshot state; use --stage for offline rehearsal')
            runtime = args.runtime.expanduser().resolve(strict=True)
            result = (assess(runtime) if args.dry_run else
                      stage(runtime, args.stage.expanduser().absolute(), args.state_root, args.offline))
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, 'upgrade_runtime: '+str(exc)+'\n')


if __name__ == '__main__':
    main()
