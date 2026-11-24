#!/usr/bin/env python3
"""Install the sealed ato launcher and optionally its Skill."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import stat
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ato_core import ensure_private_dir

from release_snapshot import (
    ENTRIES, PUBLIC, SURFACE, Refused, atomic_entry, atomic_bytes, entry_set,
    entrypoints, release_lock, require, safe_record, snapshot_source,
)


def install(root, bin_dir, skills_dir=None):
    source = snapshot_source(root)
    require(source['surface'] == SURFACE, 'installation requires a qualified unified snapshot')
    entry = entry_set(source)[PUBLIC]
    skill = skills_dir/'ato/SKILL.md' if skills_dir is not None else None
    skill_data = (root/'skills/ato/SKILL.md').read_bytes() if skill else None

    def preflight():
        current = entrypoints(bin_dir)
        require(all(current[name] is None for name in ENTRIES if name != PUBLIC),
                'previous entrypoints require journaled cutover')
        require(current[PUBLIC] in (None, entry), 'existing ato requires journaled cutover')
        if skill:
            require(skill.parent.resolve() == skill.parent, 'Skill target is indirect')
            try:
                data, info = safe_record(skill)
            except FileNotFoundError:
                pass
            else:
                require(data == skill_data and stat.S_IMODE(info.st_mode) == 0o644,
                        'Skill target already contains a different file')
        return current

    preflight()
    for directory in (bin_dir, skills_dir):
        if directory is not None and not directory.exists():
            ensure_private_dir(directory)
    with ExitStack() as stack:
        for directory in sorted({p for p in (bin_dir, skills_dir) if p is not None}):
            stack.enter_context(release_lock(directory))
        current = preflight()
        if skill:
            if not skill.parent.exists():
                ensure_private_dir(skill.parent)
            if not skill.exists():
                atomic_bytes(skill, skill_data, 0o644)
        if current[PUBLIC] is None:
            atomic_entry(bin_dir/PUBLIC, entry)
    return dict(launcher=str(bin_dir/PUBLIC), source=str(root), release=source['seal'],
                skill=str(skill) if skill else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('--bin-dir', type=Path, required=True)
    parser.add_argument('--skills-dir', type=Path)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    try:
        for path in (args.bin_dir, args.skills_dir):
            if path is not None:
                require(path.is_absolute() and path.resolve() == path and not path.is_relative_to(root),
                        'installation requires a canonical destination outside the snapshot')
        print(json.dumps(install(root, args.bin_dir, args.skills_dir)))
        return 0
    except (Refused, OSError, ValueError, KeyError) as exc:
        print('Install refused: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
