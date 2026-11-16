#!/usr/bin/env python3
"""Install a local launcher and optionally the bundled skill. No services or credentials."""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path, default=Path.home()/'.local/bin')
    parser.add_argument('--skills-dir', type=Path, help='Optional Codex skills parent directory')
    parser.add_argument('--force', action='store_true', help='Replace an existing launcher or skill')
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error('Python 3.12 or later is required')
    root = Path(__file__).resolve().parents[1]
    launcher = args.bin_dir.expanduser().resolve()/'ctask'
    skill = args.skills_dir.expanduser().resolve()/'codex-long-task' if args.skills_dir else None
    if not args.force and (launcher.exists() or launcher.is_symlink() or (skill and skill.exists())):
        parser.error('target already exists; inspect it before explicitly using --force')
    launcher.parent.mkdir(parents=True, exist_ok=True)
    temp = launcher.with_name('ctask.install-tmp-'+str(os.getpid()))
    try:
        temp.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(root/'runner.py'))+' "$@"\n')
        temp.chmod(0o755)
        os.replace(temp, launcher)
    finally:
        temp.unlink(missing_ok=True)
    if skill:
        skill.mkdir(parents=True, exist_ok=True)
        for p in (root/'skills/codex-long-task').rglob('*.md'):
            target = skill/p.relative_to(root/'skills/codex-long-task')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(p.read_text().replace('CTASK_SOURCE', str(root)))
    print(json.dumps(dict(launcher=str(launcher), source=str(root), skill=str(skill) if skill else None)))


if __name__ == '__main__':
    main()
