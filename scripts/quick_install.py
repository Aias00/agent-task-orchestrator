#!/usr/bin/env python3
"""Stage the checked-out ATO commit and install its launcher for this account."""
import argparse
from itertools import count
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from install import install
from ato_core import ensure_private_dir
from release_snapshot import (
    MANIFEST, PUBLIC, Refused, entrypoints, git_commit, release_lock, require, stage, verify_snapshot,
)


def quick_install(repo, releases_dir, bin_dir, skills_dir=None, *, with_pi=False,
                  wheelhouse=None, npm_cache=None):
    require(sys.version_info >= (3, 12), 'Python 3.12 or later is required')
    commit, _ = git_commit(repo, 'HEAD')
    entries = entrypoints(bin_dir)
    require(not any(record for name, record in entries.items() if name != PUBLIC),
            'previous entrypoints require journaled cutover; see docs/runtime-upgrade.md')
    ensure_private_dir(releases_dir)
    name = f'{commit}-{"pi" if with_pi else "core"}'
    with release_lock(releases_dir):
        for attempt in count(1):
            root = releases_dir / (name if attempt == 1 else f'{name}-{attempt}')
            require(not root.is_symlink(), 'snapshot destination is a symlink')
            manifest = root / MANIFEST
            if manifest.exists() or manifest.is_symlink():
                selected = verify_snapshot(root, commit, full=False)
                require(selected.get('with_pi') is with_pi, 'snapshot dependency selection differs')
                break
            if not root.exists():
                require(entries[PUBLIC] is None,
                        'existing ato requires journaled cutover; see docs/runtime-upgrade.md')
                stage(repo, commit, root, with_pi=with_pi, wheelhouse=wheelhouse, npm_cache=npm_cache)
                break
            require(root.is_dir(), 'snapshot destination is not a directory')
            # Staging preserves failed attempts for inspection. Use a new directory on retry.
    result = install(root, bin_dir, skills_dir)
    return {**result, 'commit': commit, 'with_pi': with_pi}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    home = Path.home().resolve()
    parser.add_argument('--bin-dir', type=Path, default=home/'.local/bin')
    parser.add_argument('--releases-dir', type=Path, default=home/'.local/share/ato-releases')
    parser.add_argument('--skills-dir', type=Path)
    parser.add_argument('--with-pi', action='store_true', help='Include Pi and ACP dependencies from local caches')
    parser.add_argument('--wheelhouse', type=Path)
    parser.add_argument('--npm-cache', type=Path)
    args = parser.parse_args(argv)
    if not args.with_pi and (args.wheelhouse is not None or args.npm_cache is not None):
        parser.error('--wheelhouse and --npm-cache require --with-pi')
    try:
        for name in ('bin_dir', 'releases_dir', 'skills_dir', 'wheelhouse', 'npm_cache'):
            path = getattr(args, name)
            if path is not None:
                path = path.expanduser().absolute()
                require(path == path.resolve(), f'--{name.replace("_", "-")} must be a canonical path')
                setattr(args, name, path)
        repo = Path(__file__).resolve().parents[1]
        print(json.dumps(quick_install(repo, **vars(args))))
        return 0
    except (Refused, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f'Install refused: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
