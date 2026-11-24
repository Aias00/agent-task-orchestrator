"""Offline release fixtures. No observer facade method reaches the host."""
from pathlib import Path
import os
import shlex
import shutil
import subprocess
import sys

from scripts import release_snapshot as release
from ato_core import ensure_private_dir


ROOT = Path(__file__).resolve().parents[1]
CURRENT = 'a' * 40
OLD = 'b' * 40


def unseal(root):
    for directory, _, files in os.walk(root):
        Path(directory).chmod(0o700)
        for name in files:
            path = Path(directory)/name
            if not path.is_symlink():
                path.chmod(0o600)


def write(root, name, data='offline fixture', mode=0o600):
    path = root/name
    if not path.parent.exists():
        ensure_private_dir(path.parent)
    path.write_text(data)
    path.chmod(mode)
    return path


def seal(root, commit, *, legacy=False, tree='fixture', tools=None):
    root.chmod(0o700)
    manifest = root/release.MANIFEST
    manifest.unlink(missing_ok=True)
    release.seal(root)
    root.chmod(0o700)
    doc = dict(version=1, qualified=True, root=str(root), commit=commit, tree=tree,
               files=release.inventory(root), tools=tools or {})
    if not legacy:
        doc['surface'] = release.SURFACE
    release.save(manifest, doc)
    manifest.chmod(0o400)
    root.chmod(0o500)
    return doc


def snapshot(root, *, legacy=False, executable=False, commit=None, legacy_delivery_outcome=False):
    if legacy:
        for name in ('runner.py', 'coordination.py', 'hub_cli.py'):
            write(root, name)
    elif executable:
        for source in ROOT.glob('*.py'):
            body = source.read_text()
            if legacy_delivery_outcome and source.name == 'ato_execution.py':
                body = body.replace('return "completed"', 'return "needs_acceptance"')
            write(root, source.name, body)
        for name in ('ato_cli/__init__.py', 'scripts/install.py', 'scripts/release_snapshot.py', 'scripts/ato_host_setup.py',
                     'skills/ato/SKILL.md', 'integrations/pi-acp/compatibility.json'):
            write(root, name, (ROOT/name).read_text())
    else:
        for name in ('ato.py', 'ato_runtime.py', 'scripts/install.py', 'skills/ato/SKILL.md'):
            write(root, name)
    if executable:
        python = root/'.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.symlink_to(Path(sys.executable).resolve())
    else:
        write(root, '.venv/bin/python', '#!/bin/sh\nexit 91\n', 0o700)
    seal(root, commit or (OLD if legacy else CURRENT), legacy=legacy)
    return root


def legacy_entry(bin_dir, name, root, mode=0o711, *, python=None):
    scripts = {'ctask': 'runner.py', 'ctask-notify': 'runner.py',
               'ato-read-handoff': 'coordination.py', 'ato-hub': 'hub_cli.py'}
    command = shlex.join([str(python or root/'.venv/bin/python'), str(root/scripts[name])])
    return write(bin_dir, name, '#!/bin/sh\n# local saved wrapper\nexec ' + command + ' "$@"\n', mode)


def fake_env(root):
    return dict(PATH=os.defpath, HOME=str(root/'home'), ATO_HOME=str(root/'ato'),
                CODEX_HOME=str(root/'native'), LC_ALL='C', PYTHONDONTWRITEBYTECODE='1')


class FakeSystem:
    def __init__(self):
        self.rows = {}
        self.calls = []
        self.pid = 27100
        self.fail_start = False
        self.fail_after_spawn = False
        self.after_stop = lambda: None

    def processes(self):
        return [dict(pid=row['pid'], command=row['command']) for row in self.rows.values()]

    def matches_observer(self, observer):
        return any(release.same_observer(row, observer) for row in self.rows.values())

    def observer(self, state, source, *, healthy=True):
        row = self.rows.get(state)
        if row is None:
            return None
        release.require(source and row['command'] == ' '.join(release.observer_argv(source)),
                        'ambiguous fixture observer source')
        release.require(not healthy or row['heartbeat'] > 0, 'unhealthy fixture observer')
        return row

    def seed(self, state, source):
        self.pid += 1
        row = dict(pid=self.pid, pid_identity='birth-' + str(self.pid), heartbeat=1000,
                   surface=source['surface'], command=' '.join(release.observer_argv(source)))
        self.rows[state] = row
        return row

    def stop(self, observer):
        self.calls.append(('stop', observer['surface'], observer['pid']))
        state, = (state for state, row in self.rows.items() if release.same_observer(row, observer))
        del self.rows[state]
        self.after_stop()

    def start(self, source, state, env, record, intent=None):
        self.calls.append(('start', source['surface'], dict(env)))
        if source['surface'] == release.SURFACE and self.fail_start:
            raise release.Refused('fixture target startup failure')
        row = self.seed(state, source)
        record(row)
        if source['surface'] == release.SURFACE and self.fail_after_spawn:
            raise release.Refused('fixture failure after observer admission')
        return row
