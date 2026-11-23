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
    parser.add_argument('--helpers', action='store_true', help='Also install notification and compact-handoff readers; starts no services')
    parser.add_argument('--hub', action='store_true', help='Also install the independent ato-hub launcher; starts no services')
    parser.add_argument('--hub-skills-dir', type=Path, help='Optional hub orchestrator skills parent directory')
    parser.add_argument('--force', action='store_true', help='Replace an existing launcher or skill')
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error('Python 3.12 or later is required')
    root = Path(__file__).resolve().parents[1]
    launcher = args.bin_dir.expanduser().resolve()/'ctask'
    # This literal path comes from the trusted installer, never a workflow or PATH.
    reader_env = ('if [ -z "${CTASK_HANDOFF_READER:-}" ]; then\n'
                  '  CTASK_HANDOFF_READER='+shlex.quote(str(launcher.with_name('ato-read-handoff')))+'\n'
                  '  export CTASK_HANDOFF_READER\n'
                  'fi\n')
    skill = args.skills_dir.expanduser().resolve()/'codex-long-task' if args.skills_dir else None
    hub_skill = args.hub_skills_dir.expanduser().resolve()/'ato-hub-orchestrator' if args.hub_skills_dir else None
    hub_launcher = launcher.with_name('ato-hub') if args.hub else None
    helpers = {}
    if args.helpers:
        helpers = {
            launcher.with_name('ctask-notify'): (
                'case "${1-}" in\n'
                '  control|notification-policy|followups|presented) ;;\n'
                '  *) echo "Usage: ctask-notify {control|notification-policy|followups|presented} ..." >&2; exit 2 ;;\n'
                'esac\n'+reader_env+
                'exec '+shlex.quote(sys.executable)+' '+shlex.quote(str(root/'runner.py'))+' "$@"\n'),
            launcher.with_name('ato-read-handoff'): (
                'exec '+shlex.quote(sys.executable)+' '+shlex.quote(str(root/'coordination.py'))+' "$@"\n'),
        }
    if hub_launcher:
        helpers[hub_launcher] = ('if [ -z "${ATO_HOME:-}" ]; then\n'
            '  export ATO_HOME="$HOME/.local/share/ato-hub-state"\n'
            'fi\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(root/'hub_cli.py'))+' "$@"\n')
    if not args.force and (launcher.exists() or launcher.is_symlink() or (skill and skill.exists())):
        parser.error('target already exists; inspect it before explicitly using --force')
    if not args.force and any(p.exists() or p.is_symlink() for p in helpers):
        parser.error('helper already exists; inspect it before explicitly using --force')
    if hub_skill and (hub_skill.is_symlink() or (not args.force and hub_skill.exists())):
        parser.error('hub skill target already exists or is indirect; inspect it before explicitly using --force')
    if hub_skill and hub_skill.exists() and not hub_skill.is_dir():
        parser.error('hub skill target must be a directory')
    if hub_launcher and hub_launcher.is_dir():
        parser.error('hub launcher target must not be a directory')
    launcher.parent.mkdir(parents=True, exist_ok=True)
    temp = launcher.with_name('ctask.install-tmp-'+str(os.getpid()))
    try:
        temp.write_text('#!/bin/sh\n'+reader_env+'exec '+shlex.quote(sys.executable)+' '+shlex.quote(str(root/'runner.py'))+' "$@"\n')
        temp.chmod(0o755)
        os.replace(temp, launcher)
    finally:
        temp.unlink(missing_ok=True)
    for path, body in helpers.items():
        temp = path.with_name(path.name+'.install-tmp-'+str(os.getpid()))
        try:
            temp.write_text('#!/bin/sh\nset -eu\n'+body)
            temp.chmod(0o755)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    if skill:
        skill.mkdir(parents=True, exist_ok=True)
        for p in (root/'skills/codex-long-task').rglob('*.md'):
            target = skill/p.relative_to(root/'skills/codex-long-task')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(p.read_text().replace('CTASK_SOURCE', str(root)))
    if hub_skill:
        hub_skill.mkdir(parents=True, exist_ok=True)
        text = (root/'skills/ato-hub-orchestrator/SKILL.md').read_text()
        text = text.replace('(../../docs/h1-offline-implementation.md)', '(<'+str(root/'docs/h1-offline-implementation.md')+'>)')
        temp = hub_skill/('SKILL.md.install-tmp-'+str(os.getpid()))
        try:
            temp.write_text(text)
            os.replace(temp, hub_skill/'SKILL.md')
        finally:
            temp.unlink(missing_ok=True)
    result = dict(launcher=str(launcher), source=str(root), skill=str(skill) if skill else None,
                  helpers=[str(p) for p in helpers if p!=hub_launcher])
    if hub_launcher:result['hub_launcher']=str(hub_launcher)
    if hub_skill:result['hub_skill']=str(hub_skill/'SKILL.md')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
