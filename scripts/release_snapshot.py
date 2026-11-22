#!/usr/bin/env python3
"""Explicit source snapshot staging and reversible executable/observer cutover.

Standard library only. No task migration, ledger restoration, message dispatch,
policy configuration or forced process termination belongs in this tool.
"""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

ENTRIES = ('ctask', 'ctask-notify', 'ato-read-handoff')
BUSY = frozenset(('queued', 'claimed', 'dispatching', 'delivery_unknown'))
POLICIES = ('hub.json', 'notification-policy.json', 'notification-project-mutes.json',
            'time-policy.json', 'failure-alerts.json', 'control/config.json')
MANIFEST = '.release-manifest.json'
PATH = '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'


class Refused(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Refused(reason)


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    temp = path.with_name(path.name+'.tmp-'+uuid.uuid4().hex)
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def command(argv, *, cwd=None, env=None):
    return subprocess.check_output([str(a) for a in argv], cwd=cwd, env=env, text=True).strip()


def environment(ato_home, codex_home, bin_dir):
    # Never inherit actor/session credentials, Python/Node startup hooks or
    # platform loader settings from the invoking shell.
    env = {k: os.environ[k] for k in ('HOME', 'USER', 'TMPDIR', 'LANG') if k in os.environ}
    env.update(PATH=PATH, ATO_HOME=str(ato_home), CODEX_HOME=str(codex_home),
               PYTHONDONTWRITEBYTECODE='1', CTASK_HANDOFF_READER=str(bin_dir/'ato-read-handoff'))
    return env


def context(ato_home, codex_home, legacy=False):
    if legacy:
        require(ato_home == codex_home, 'legacy context requires identical ATO/Codex homes')
        return ato_home/'ctask-interaction'
    namespace = hashlib.sha256(str(codex_home).encode()).hexdigest()
    return ato_home/'ctask-interaction/contexts'/namespace


def policy_hashes(state):
    return {name: sha(state/name) if (state/name).is_file() else None for name in POLICIES}


def file_record(path):
    require(path.is_file() and not path.is_symlink(), 'entrypoint must be a regular file: '+str(path))
    require(path.stat().st_mode & 0o111, 'entrypoint is not executable: '+str(path))
    return dict(data=base64.b64encode(path.read_bytes()).decode(),
                mode=stat.S_IMODE(path.stat().st_mode), sha256=sha(path))


def entrypoints(bin_dir):
    return {name: file_record(bin_dir/name) for name in ENTRIES}


def launcher_source(entries):
    selected = []
    for name, row in entries.items():
        lines = base64.b64decode(row['data']).decode().splitlines()
        commands = [shlex.split(line) for line in lines if line.startswith('exec ')]
        require(len(commands) == 1 and len(commands[0]) == 4 and commands[0][-1] == '$@',
                'unrecognized installed launcher: '+name)
        _, python, script, _ = commands[0]
        root = Path(script).parent
        require(Path(python).is_absolute() and root.is_absolute() and
                Path(script).name == ('coordination.py' if name == 'ato-read-handoff' else 'runner.py'),
                'unrecognized installed source: '+name)
        require(Path(python).is_file() and Path(script).is_file(), 'previous source is unavailable')
        selected.append((str(root), python))
    require(len(set(selected)) == 1, 'entrypoints select different sources')
    return dict(root=selected[0][0], python=selected[0][1])


def mailbox_counts(con):
    require(con.execute('pragma user_version').fetchone()[0] == 2, 'unsupported mailbox schema')
    counts = dict(con.execute('select state,count(*) from messages group by state'))
    busy = {k: v for k, v in counts.items() if k in BUSY}
    # Delivered/accepted and even cancelled/expired envelopes can retain an
    # external turn slot. A terminal label alone does not prove quiescence.
    for state, count in con.execute('select state,count(*) from messages where busy != 0 group by state'):
        busy[state] = max(busy.get(state, 0), count)
    return dict(counts=counts, busy=busy)


def mailbox(state):
    """Inspect a stable private copy: even SQLite shm creation stays off runtime.

    Refuse a changing main/WAL pair rather than inventing an idle observation.
    Writers may continue normally after the cutover boundary.
    """
    path = state/'mailbox/state.sqlite'
    require(path.is_file() and not path.is_symlink(), 'mailbox database is missing or indirect')
    require(not path.with_name(path.name+'-journal').exists(), 'mailbox has a rollback journal')
    paths = [path, path.with_name(path.name+'-wal')]
    def versions():
        return [(p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None for p in paths]
    before = versions()
    with tempfile.TemporaryDirectory(prefix='ato-release-mailbox-') as folder:
        copied = Path(folder)/path.name
        for p in paths:
            if p.exists():
                shutil.copyfile(p, Path(folder)/p.name)
        require(versions() == before, 'mailbox changed during read-only inspection; retry at idle boundary')
        con = sqlite3.connect(str(copied))
        try:
            result = mailbox_counts(con)
        finally:
            con.close()
    return result


@contextmanager
def idle_boundary(state):
    """Reserve writers only after the predecessor has exited; never change rows."""
    path = state/'mailbox/state.sqlite'
    con = sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=2)
    try:
        con.execute('begin immediate')
        require(not mailbox_counts(con)['busy'], 'mailbox is not idle; no replay or queue draining')
        yield
    finally:
        con.rollback()
        con.close()


def inventory(root):
    result = {}
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in sorted(folders+files):
            path = Path(directory)/name
            relative = path.relative_to(root).as_posix()
            if relative == MANIFEST:
                continue
            mode = stat.S_IMODE(path.lstat().st_mode)
            if path.is_symlink():
                target = path.resolve(strict=True)
                external = not target.is_relative_to(root)
                require(not external or relative in ('.venv/bin/python', '.venv/bin/python3', '.venv/bin/python3.12'),
                        'unexpected external symlink: '+relative)
                require(not external or target.is_file(), 'external interpreter is not a file')
                result[relative] = dict(kind='link', target=os.readlink(path),
                    external_sha256=sha(target) if external else None)
            elif path.is_dir():
                result[relative] = dict(kind='directory', mode=mode)
            else:
                require(path.is_file(), 'non-regular snapshot material: '+relative)
                result[relative] = dict(kind='file', mode=mode, sha256=sha(path))
    return result


def seal(root):
    # lstat/skip symlinks is essential: never chmod the shared interpreter.
    for directory, folders, files in os.walk(root, topdown=False, followlinks=False):
        for name in files:
            path = Path(directory)/name
            if not path.is_symlink():
                path.chmod(0o444 | (path.stat().st_mode & 0o111))
        for name in folders:
            path = Path(directory)/name
            if not path.is_symlink():
                path.chmod(0o555)
    root.chmod(0o500)


def verify_snapshot(root, commit):
    require(root.is_dir() and not root.is_symlink(), 'snapshot must be a real directory')
    manifest = read(root/MANIFEST)
    require(manifest.get('version') == 1 and manifest.get('qualified') is True and
            manifest.get('commit') == commit and manifest.get('root') == str(root),
            'snapshot is unqualified, relocated or selects another commit')
    require(root.stat().st_uid == os.getuid() and not root.stat().st_mode & 0o222,
            'snapshot root is not owned and sealed read-only')
    require(not (root/MANIFEST).is_symlink() and not (root/MANIFEST).stat().st_mode & 0o222,
            'snapshot manifest is not sealed')
    verify_tools(manifest.get('tools', {}))
    actual = inventory(root)
    require(actual == manifest['files'], 'snapshot content, links or modes changed')
    require(all(not row.get('mode', 0) & 0o222 for row in actual.values()), 'snapshot contains writable files')
    for name in ('runner.py', 'coordination.py', '.venv/bin/python', 'scripts/install.py'):
        require((root/name).is_file(), 'incomplete snapshot: '+name)
    return manifest


def git_commit(repo, commit):
    resolved = command(['git', '-C', repo, 'rev-parse', '--verify', commit+'^{commit}'])
    tree = command(['git', '-C', repo, 'rev-parse', resolved+'^{tree}'])
    return resolved, tree


def run_logged(argv, root, env, name):
    logs = root/'.release-logs'
    logs.mkdir(exist_ok=True, mode=0o700)
    with (logs/(name+'.log')).open('wb') as stream:
        result = subprocess.run([str(a) for a in argv], cwd=root, env=env,
                                stdout=stream, stderr=subprocess.STDOUT)
    require(result.returncode == 0, name+' failed; see '+str(logs/(name+'.log')))


def host_tool(name, explicit=None, *, forbidden=()):
    candidate = Path(explicit or shutil.which(name) or '')
    require(candidate.is_absolute(), 'select an absolute trusted '+name+' executable')
    resolved = candidate.resolve(strict=True)
    forbidden = tuple(Path(p).resolve() for p in forbidden)
    for alias in (candidate, resolved):
        for part in (alias, *alias.parents):
            require(not any(part.resolve().is_relative_to(p) for p in forbidden),
                    name+' executable enters source or snapshot')
            info = part.lstat()
            sticky_root = stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            require(info.st_uid in (0, os.getuid()) and (stat.S_ISLNK(info.st_mode) or
                    not info.st_mode & 0o022 or sticky_root), 'untrusted '+name+' path ownership/permissions')
    require(resolved.is_file() and os.access(resolved, os.X_OK), name+' is not executable')
    return dict(path=str(resolved), sha256=sha(resolved))


def verify_tools(tools):
    for name, row in tools.items():
        require(name in ('node', 'npm'), 'unknown recorded dependency tool')
        selected = host_tool(name, row['path'])
        require(selected == row, 'recorded '+name+' executable changed')


def observer_environment(source, env):
    env = dict(env)
    path = Path(source['root'])/MANIFEST
    if path.is_file():
        tools = read(path).get('tools', {})
        verify_tools(tools)
        node = tools.get('node')
    else:
        node = None
    node = node or host_tool('node')
    env['CTASK_NODE_BIN'] = node['path']
    return env


def install_dependencies(root, env):
    forbidden = (root, Path(env.pop('ATO_RELEASE_SOURCE')))
    tools = {name: host_tool(name, env.pop('ATO_RELEASE_'+name.upper(), None), forbidden=forbidden)
             for name in ('node', 'npm')}
    env['CTASK_NODE_BIN'] = tools['node']['path']
    run_logged([sys.executable, '-m', 'venv', root/'.venv'], root, env, 'venv')
    run_logged([root/'.venv/bin/python', '-m', 'pip', 'install', '--no-cache-dir', '--disable-pip-version-check', '-r', root/'requirements-acp.lock'],
               root, env, 'python-dependencies')
    run_logged([tools['node']['path'], tools['npm']['path'], 'ci', '--ignore-scripts', '--no-audit', '--no-fund', '--prefix', root/'integrations/pi-acp'],
               root, env, 'node-dependencies')
    return tools


def qualify(root, env):
    python = root/'.venv/bin/python'
    run_logged([python, 'scripts/run_tests.py'], root, env, 'tests')
    # The shipped test runner rejects skips. Also require its explicit receipt.
    import re
    report = (root/'.release-logs/tests.log').read_text()
    require(re.search(r'^Ran \d+ tests in [\d.]+s$', report, re.M) and
            re.search(r'^OK$', report, re.M) and not re.search(r'^(FAILED|FAIL:|ERROR:)', report, re.M),
            'missing clean full-test receipt')
    run_logged([python, 'scripts/check_architecture.py'], root, env, 'architecture')
    # Compile tracked Python sources, not third-party code or generated caches.
    sources = command(['git', 'ls-files', '-z'], cwd=root).split('\0')
    run_logged([python, '-m', 'py_compile', *[p for p in sources if p.endswith('.py')]], root, env, 'compile')


def stage(repo, commit, root, *, node_bin=None, npm_bin=None):
    require(sys.version_info >= (3, 12), 'Python 3.12 or later is required')
    commit, tree = git_commit(repo, commit)
    require(not root.exists() and not root.is_symlink(), 'snapshot destination already exists; never reuse it')
    root.mkdir(mode=0o700, parents=False)
    try:
        with tempfile.TemporaryFile() as archive:
            subprocess.run(['git', '-C', str(repo), 'archive', commit], stdout=archive, check=True)
            archive.seek(0)
            with tarfile.open(fileobj=archive) as stream:
                members = stream.getmembers()
                for m in members:
                    parts = Path(m.name).parts
                    require(not Path(m.name).is_absolute() and '..' not in parts and
                            not any(p in ('.git', '.venv', 'node_modules') or p.startswith('.release-') for p in parts),
                            'archive contains reserved or unsafe paths')
                    require(m.isdir() or m.isfile() or m.issym(), 'unsupported archive entry')
                stream.extractall(root, filter='data')
        # An ephemeral index permits unchanged Git-based structural/doc tests.
        command(['git', 'init', '-q', str(root)])
        command(['git', 'add', '--all'], cwd=root)
        require(command(['git', 'write-tree'], cwd=root) == tree, 'archive tree differs from selected commit')
        source = {p: sha(root/p) for p in command(['git', 'ls-files', '-z'], cwd=root).split('\0') if p and not (root/p).is_symlink()}
        with tempfile.TemporaryDirectory(prefix='ato-release-qualification-') as home:
            env = environment(Path(home)/'ato', Path(home)/'native', Path(home)/'bin')
            env['npm_config_cache'] = str(Path(home)/'npm-cache')
            env['ATO_RELEASE_SOURCE'] = str(repo)
            for name, value in (('NODE', node_bin), ('NPM', npm_bin)):
                if value is not None:
                    env['ATO_RELEASE_'+name] = str(value)
            tools = install_dependencies(root, env)
            qualify(root, env)
        require(all(sha(root/p) == digest for p, digest in source.items()), 'qualification modified source files')
        shutil.rmtree(root/'.git')
        # Seal before collecting expected modes; briefly reopen just the root
        # to write its final private manifest, then close it permanently.
        seal(root)
        root.chmod(0o700)
        manifest = dict(version=1, qualified=True, root=str(root), commit=commit, tree=tree,
                        source_files=source, files=inventory(root), tools=tools or {}, created_at=time.time())
        save(root/MANIFEST, manifest)
        (root/MANIFEST).chmod(0o400)
        root.chmod(0o500)
        verify_snapshot(root, commit)
        return dict(snapshot=str(root), commit=commit, tree=tree, qualified=True, activated=False)
    except BaseException:
        # Keep failed evidence; absence of a sealed qualified manifest prevents
        # activation. Never remove an operator-selected existing directory.
        root.chmod(0o700)
        (root/MANIFEST).unlink(missing_ok=True)
        raise


class System:
    """Only this facade observes/signals processes; offline tests replace it."""
    def processes(self):
        rows = command(['/bin/ps', '-axo', 'pid=,command='])
        result = []
        for row in rows.splitlines():
            bits = row.strip().split(None, 1)
            if len(bits) == 2 and bits[1].endswith('runner.py _controller'):
                result.append(dict(pid=int(bits[0]), command=bits[1]))
        return result

    def identity(self, pid):
        try:
            start = command(['/bin/ps', '-p', str(pid), '-o', 'lstart='])
            return hashlib.sha256(start.encode()).hexdigest() if start else None
        except subprocess.CalledProcessError:
            return None

    def observer(self, state, source, *, healthy=True):
        row = read(state/'control/process.json')
        expected = source['python']+' '+str(Path(source['root'])/'runner.py')+' _controller'
        processes = self.processes()
        require(len(processes) == 1 and processes[0]['pid'] == row.get('pid') and
                ' '.join(processes[0]['command'].split()) == ' '.join(expected.split()),
                'expected exactly one controller selecting the installed source')
        require(row.get('pid_identity') is not None and self.identity(row['pid']) == row['pid_identity'],
                'controller pid_identity mismatch (possible PID reuse)')
        require(not healthy or (row.get('state') == 'watching' and row.get('consecutive_failures', 0) == 0
                and isinstance(row.get('heartbeat'), (int, float)) and 0 <= time.time()-row['heartbeat'] <= 30),
                'controller is not healthy')
        return dict(pid=row['pid'], pid_identity=row['pid_identity'], command=processes[0]['command'],
                    heartbeat=row.get('heartbeat'))

    def stop(self, observer):
        require(self.identity(observer['pid']) == observer['pid_identity'], 'PID identity changed before SIGTERM')
        require(any(p == dict(pid=observer['pid'], command=observer['command']) for p in self.processes()),
                'controller command changed before SIGTERM')
        os.kill(observer['pid'], signal.SIGTERM)
        deadline = time.monotonic()+45
        while self.identity(observer['pid']) == observer['pid_identity']:
            require(time.monotonic() < deadline, 'controller did not exit gracefully; refusing SIGKILL')
            time.sleep(.1)
        require(not self.processes(), 'another controller appeared during graceful stop')

    def start(self, source, state, env):
        require(not self.processes(), 'refusing to start a second controller')
        # start(), unlike the public enable/start CLI, preserves all policies.
        code = 'import control; control.start()'
        env = observer_environment(source, env)
        subprocess.run([source['python'], '-c', code], cwd=source['root'], env=env,
                       check=True, capture_output=True, timeout=30)
        deadline = time.monotonic()+45
        previous = None
        while time.monotonic() < deadline:
            try:
                row = self.observer(state, source)
                if (previous and row['pid'] == previous['pid'] and
                        row['pid_identity'] == previous['pid_identity'] and row['heartbeat'] > previous['heartbeat']):
                    return row
                previous = row
            except (Refused, FileNotFoundError, json.JSONDecodeError):
                pass
            time.sleep(.2)
        raise Refused('controller did not produce two independently verified healthy heartbeats')


def inspect(bin_dir, state, system):
    entries = entrypoints(bin_dir)
    source = launcher_source(entries)
    policies = policy_hashes(state)
    cfg = read(state/'control/config.json')
    hub = read(state/'hub.json')
    require(cfg.get('enabled') is True and cfg.get('hub_thread') == hub.get('thread_id'),
            'controller/hub binding is disabled or inconsistent')
    return dict(entries=entries, source=source, policies=policies,
                observer=system.observer(state, source), mailbox=mailbox(state))


def atomic_entry(path, row):
    data = base64.b64decode(row['data'], validate=True)
    require(hashlib.sha256(data).hexdigest() == row['sha256'], 'entrypoint backup checksum mismatch')
    temp = path.with_name('.'+path.name+'.release-'+uuid.uuid4().hex)
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, row['mode'])
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temp.chmod(row['mode'])
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def generated_entries(root, bin_dir):
    with tempfile.TemporaryDirectory(prefix='ato-release-launchers-') as folder:
        stage_bin = Path(folder)
        env = environment(stage_bin/'ato', stage_bin/'native', bin_dir)
        subprocess.run([str(root/'.venv/bin/python'), str(root/'scripts/install.py'),
                        '--helpers', '--bin-dir', str(stage_bin)], env=env,
                       check=True, capture_output=True, timeout=30)
        result = entrypoints(stage_bin)
        for row in result.values():
            text = base64.b64decode(row['data']).decode().replace(shlex.quote(str(stage_bin/'ato-read-handoff')), shlex.quote(str(bin_dir/'ato-read-handoff')))
            row['data'] = base64.b64encode(text.encode()).decode()
            row['sha256'] = hashlib.sha256(text.encode()).hexdigest()
        return result


def same_policies(state, expected):
    require(policy_hashes(state) == expected, 'user policy changed; refusing to overwrite or guess it')


@contextmanager
def release_lock(bin_dir):
    require(bin_dir.is_dir() and not bin_dir.is_symlink(), 'bin-dir must be an existing real directory')
    path = bin_dir/'.ato-release.lock'
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def restore(journal, bin_dir, state, env, system):
    same_policies(state, journal['before']['policies'])
    old = journal['before']
    # A crash can leave a partial set. Restore only our old/new bytes, never
    # overwrite a third party's later installation.
    current = entrypoints(bin_dir)
    for name in ENTRIES:
        require(current[name]['sha256'] in (old['entries'][name]['sha256'], journal['after_entries'][name]['sha256']),
                'entrypoint changed outside this transaction: '+name)
    processes = system.processes()
    if processes:
        source = journal['target'] if all(current[n]['sha256'] == journal['after_entries'][n]['sha256'] for n in ENTRIES) else old['source']
        # A partially replaced wrapper set may coexist with either source;
        # independently inspect the process against each exact trusted source.
        try:
            observer = system.observer(state, source, healthy=False)
        except Refused:
            observer = system.observer(state, old['source'], healthy=False)
        system.stop(observer)
    require(not mailbox(state)['busy'], 'mailbox is busy; rollback does not restore or replay it')
    with idle_boundary(state):
        same_policies(state, old['policies'])
        for name in ENTRIES:
            atomic_entry(bin_dir/name, old['entries'][name])
    observer = system.start(old['source'], state, env)
    same_policies(state, old['policies'])
    return observer


def activate(root, commit, bin_dir, state, transaction, env, system=None):
    system = system or System()
    verify_snapshot(root, commit)
    with release_lock(bin_dir):
        before = inspect(bin_dir, state, system)
        require(not before['mailbox']['busy'], 'mailbox is not idle; queued/unknown messages are never replayed')
        after = generated_entries(root, bin_dir)
        target = launcher_source(after)
        require(target['root'] == str(root), 'generated launchers select another snapshot')
        transaction.mkdir(mode=0o700, parents=False)
        journal = dict(version=1, state='prepared', bin_dir=str(bin_dir), interaction=str(state),
                       environment={k: env.get(k) for k in ('ATO_HOME', 'CODEX_HOME')},
                       before=before, after_entries=after, target=target, commit=commit)
        path = transaction/'transaction.json'
        save(path, journal)
        try:
            same_policies(state, before['policies'])
            require(entrypoints(bin_dir) == before['entries'], 'entrypoints changed during preparation')
            system.stop(before['observer'])
            journal['state'] = 'observer_stopped'
            save(path, journal)
            with idle_boundary(state):
                same_policies(state, before['policies'])
                for name in ENTRIES:
                    atomic_entry(bin_dir/name, after[name])
            journal['state'] = 'entries_switched'
            save(path, journal)
            journal['observer'] = system.start(target, state, env)
            same_policies(state, before['policies'])
            require(entrypoints(bin_dir) == after, 'entrypoints changed during cutover')
            journal['state'] = 'activated'
            save(path, journal)
            return dict(state='activated', transaction=str(path), observer=journal['observer'])
        except BaseException as exc:
            journal['error_type'] = type(exc).__name__
            try:
                journal['rollback_observer'] = restore(journal, bin_dir, state, env, system)
                journal['state'] = 'rolled_back'
            except BaseException as recovery_error:
                journal['state'] = 'recovery_required'
                journal['recovery_error_type'] = type(recovery_error).__name__
            save(path, journal)
            raise


def rollback(transaction, bin_dir, state, env, system=None):
    system = system or System()
    path = transaction/'transaction.json'
    journal = read(path)
    require(journal.get('version') == 1 and journal.get('bin_dir') == str(bin_dir) and
            journal.get('interaction') == str(state) and
            journal.get('environment') == {k: env.get(k) for k in ('ATO_HOME', 'CODEX_HOME')},
            'rollback context differs from recorded transaction')
    require(journal.get('state') in ('activated', 'prepared', 'observer_stopped', 'entries_switched', 'recovery_required'),
            'transaction is already rolled back or unsupported')
    with release_lock(bin_dir):
        require(not mailbox(state)['busy'], 'mailbox is not idle; rollback never restores ledgers')
        observer = restore(journal, bin_dir, state, env, system)
        journal.update(state='rolled_back', rollback_observer=observer)
        save(path, journal)
    return dict(state='rolled_back', transaction=str(path), observer=observer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    for name in ('dry-run', 'stage', 'activate', 'rollback'):
        mode.add_argument('--'+name, action='store_true')
    for name in ('repo', 'snapshot', 'bin-dir', 'ato-home', 'codex-home', 'transaction'):
        parser.add_argument('--'+name, type=Path)
    parser.add_argument('--legacy-context', action='store_true', help='Explicit old CODEX_HOME-only layout; requires equal home paths')
    parser.add_argument('--node-bin', type=Path, help='Trusted host Node path for staging (otherwise resolve operator PATH once)')
    parser.add_argument('--npm-bin', type=Path, help='Trusted host npm CLI for staging (otherwise resolve operator PATH once)')
    parser.add_argument('--commit', help='Explicit trusted Git revision; no implicit HEAD')
    parser.add_argument('--allow-runtime-change', action='store_true', help='Required for activate/rollback; starts/stops only the observer')
    args = parser.parse_args(argv)
    def need(*names):
        for name in names:
            if getattr(args, name) is None:
                parser.error('--'+name.replace('_', '-')+' is required for this mode')
    try:
        need('snapshot', 'commit') if not args.rollback else need('transaction')
        if args.stage or args.dry_run:
            need('repo')
        if not args.stage:
            need('bin_dir', 'ato_home', 'codex_home')
        if args.activate or args.rollback:
            need('transaction')
            if not args.allow_runtime_change:
                parser.error('activation/rollback require explicit --allow-runtime-change')
        for key in ('repo', 'snapshot', 'bin_dir', 'ato_home', 'codex_home', 'transaction'):
            value = getattr(args, key)
            if value is not None:
                require(value.expanduser().is_absolute() and not value.is_symlink(), '--'+key+' must be an absolute non-symlink path')
                setattr(args, key, value.expanduser().resolve())
        if args.stage:
            result = stage(args.repo, args.commit, args.snapshot, node_bin=args.node_bin, npm_bin=args.npm_bin)
        else:
            state = context(args.ato_home, args.codex_home, args.legacy_context)
            env = environment(args.ato_home, args.codex_home, args.bin_dir)
            if args.legacy_context:
                env.pop('ATO_HOME', None)
            if args.transaction is not None:
                require(not args.transaction.is_relative_to(args.ato_home) and
                        not args.transaction.is_relative_to(args.codex_home) and
                        not args.transaction.is_relative_to(args.snapshot or args.bin_dir) and
                        not args.transaction.is_relative_to(args.bin_dir),
                        'transaction backups must be outside runtime homes, snapshot and bin-dir')
            if args.dry_run:
                commit, tree = git_commit(args.repo, args.commit)
                report = inspect(args.bin_dir, state, System())
                if args.snapshot.exists():
                    verify_snapshot(args.snapshot, commit)
                result = dict(mode='dry-run', commit=commit, tree=tree, snapshot=str(args.snapshot),
                    entrypoints={k: dict(sha256=v['sha256'], mode=v['mode']) for k, v in report['entries'].items()},
                    source=report['source'], observer=report['observer'], policies=report['policies'],
                    mailbox=report['mailbox'], stage_required=not args.snapshot.exists(),
                    activation_ready=args.snapshot.exists() and not report['mailbox']['busy'],
                    plan=['git archive, isolated dependencies, full qualification, read-only seal',
                          'explicit activation: backup entrypoints, SIGTERM observer, idle writer barrier, atomic per-entry replace, verify observer',
                          'rollback: executable entrypoints and observer only; no task or ledger restoration'])
            elif args.activate:
                require(len(args.commit) == 40 and all(c in '0123456789abcdef' for c in args.commit), 'activate requires the full commit hash')
                result = activate(args.snapshot, args.commit, args.bin_dir, state, args.transaction, env)
            else:
                result = rollback(args.transaction, args.bin_dir, state, env)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (Refused, OSError, ValueError, KeyError, subprocess.SubprocessError, sqlite3.Error, tarfile.TarError) as exc:
        print('Release refused: '+str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
