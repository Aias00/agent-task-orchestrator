#!/usr/bin/env python3
"""Explicit source snapshot staging and reversible executable/observer cutover.

Standard library only. No task migration, ledger restoration, message dispatch,
policy configuration or forced process termination belongs in this tool.
"""
import argparse
import base64
from contextlib import contextmanager
from contextlib import ExitStack, closing
import fcntl
import hashlib
import json
import os
import re
import secrets
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

PUBLIC = 'ato'
LEGACY = ('ctask', 'ctask-notify', 'ato-read-handoff', 'ato-hub')
ENTRIES = (PUBLIC, *LEGACY)
SURFACE = 'ato-v1'
BUSY = frozenset(('queued', 'claimed', 'dispatching', 'delivery_unknown'))
POLICIES = ('hub.json', 'notification-policy.json', 'notification-project-mutes.json',
            'time-policy.json', 'failure-alerts.json', 'control/config.json')
MANIFEST = '.release-manifest.json'
PATH = os.defpath


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


def environment(ato_home, codex_home):
    # Never inherit actor/session credentials, Python/Node startup hooks or
    # platform loader settings from the invoking shell.
    env = dict(PATH=PATH, HOME=str(codex_home.parent), LC_ALL='C', ATO_HOME=str(ato_home),
               CODEX_HOME=str(codex_home), PYTHONDONTWRITEBYTECODE='1')
    return env


def context(ato_home, codex_home, legacy=False):
    if legacy:
        require(ato_home == codex_home, 'legacy context requires identical ATO/Codex homes')
        return ato_home/'ctask-interaction'
    namespace = hashlib.sha256(str(codex_home).encode()).hexdigest()
    return ato_home/'ctask-interaction/contexts'/namespace


def safe_record(path):
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1
                and info.st_size <= 16 * 1024 * 1024, 'invalid release record: ' + str(path))
        data = stream.read(info.st_size + 1)
        last = path.lstat()
        require(len(data) == info.st_size and (last.st_dev, last.st_ino, last.st_mtime_ns, last.st_ctime_ns) ==
                (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns),
                'release record changed during inspection: ' + str(path))
        return data, info


def file_record(path):
    try:
        data, info = safe_record(path)
    except FileNotFoundError:
        return None
    mode = stat.S_IMODE(info.st_mode)
    require(mode & 0o111 and not mode & 0o7022, 'entrypoint is not a trusted executable: ' + str(path))
    return dict(data=base64.b64encode(data).decode(), mode=mode, sha256=hashlib.sha256(data).hexdigest())


def entrypoints(bin_dir):
    if bin_dir.exists() or bin_dir.is_symlink():
        require(bin_dir.is_dir() and not bin_dir.is_symlink() and bin_dir.resolve() == bin_dir,
                'bin-dir must be an existing real directory')
    return {name: file_record(bin_dir/name) for name in ENTRIES}


def launcher_bytes(root, python, digest):
    return ('#!/bin/sh\nexport ATO_LAUNCH_SEAL=' + digest + '\nexport ATO_LAUNCHER="$0"\nexec ' +
            shlex.join([str(python), '-IB', str(root/'ato.py')]) + ' "$@"\n').encode()


def snapshot_source(root):
    data, _ = safe_record(root/MANIFEST)
    doc = json.loads(data)
    verify_snapshot(root, doc['commit'], legacy=doc.get('surface') != SURFACE)
    return dict(root=str(root), python=str(root/'.venv/bin/python'), seal=hashlib.sha256(data).hexdigest(),
                surface=doc.get('surface', 'legacy'), commit=doc['commit'])


def verify_source(source):
    actual = snapshot_source(Path(source['root']))
    require(source['root'] == actual['root'] and source['seal'] == actual['seal']
            and source['surface'] == actual['surface'] and source['commit'] == actual['commit']
            and source['python_sha256'] == sha(source['python']), 'recorded source or interpreter changed')
    if source['surface'] == SURFACE:
        require(source['python'] == actual['python'], 'unified interpreter differs from the seal')
    return actual


def launcher_sources(entries):
    """Read the exact independently selected source for each present entry."""
    scripts = dict(ato='ato.py', ctask='runner.py', **{
        'ctask-notify': 'runner.py', 'ato-read-handoff': 'coordination.py', 'ato-hub': 'hub_cli.py'})
    selected, sources = {}, {}
    for name, row in entries.items():
        if row is None:
            continue
        data = base64.b64decode(row['data'], validate=True)
        require(hashlib.sha256(data).hexdigest() == row['sha256'], 'entrypoint backup checksum mismatch')
        commands = [shlex.split(line) for line in data.decode().splitlines() if line.startswith('exec ')]
        require(len(commands) == 1 and commands[0][-1] == '$@', 'unrecognized launcher: ' + name)
        fields = commands[0]
        require(len(fields) == (5 if name == PUBLIC else 4), 'unrecognized launcher: ' + name)
        python, script = Path(fields[1]), Path(fields[-2])
        root = script.parent
        require(python.is_absolute() and python.is_file() and os.access(python, os.X_OK)
                and root.is_absolute() and root.resolve() == root and script.name == scripts[name]
                and script.is_file() and not script.is_symlink(), 'unavailable launcher source: ' + name)
        if root not in sources:
            sources[root] = snapshot_source(root)
        source = sources[root]
        require((source['surface'] == SURFACE) == (name == PUBLIC), 'launcher selects a cross-version source')
        if name == PUBLIC:
            require(str(python) == source['python'] and data == launcher_bytes(root, python, source['seal']),
                    'unified launcher differs from the sealed selection')
        else:
            host_tool('prior interpreter', python)
        selected[name] = dict(source, python=str(python), script=str(script), python_sha256=sha(python))
    return selected


def mailbox_counts(con):
    require(con.execute('pragma user_version').fetchone()[0] == 2, 'unsupported mailbox schema')
    counts = dict(con.execute('select state,count(*) from messages group by state'))
    busy = {k: v for k, v in counts.items() if k in BUSY}
    # Delivered/accepted and even cancelled/expired envelopes can retain an
    # external turn slot. A terminal label alone does not prove quiescence.
    for state, count in con.execute('select state,count(*) from messages where busy != 0 group by state'):
        busy[state] = max(busy.get(state, 0), count)
    slots = dict(con.execute('select busy != 0,count(*) from messages group by busy != 0'))
    return dict(counts=counts, busy=busy, idle=not busy,
                turn_slots=dict(idle=slots.get(0, 0), busy=slots.get(1, 0), total=sum(slots.values())))


def mailbox_exists(state):
    path = state/'mailbox/state.sqlite'
    files = [p for p in (path, path.with_name(path.name + '-wal'),
                         path.with_name(path.name + '-journal'), path.with_name(path.name + '-shm'))
             if p.exists() or p.is_symlink()]
    require(not files or path in files, 'mailbox sidecar exists without its database')
    require(all(p.is_file() and not p.is_symlink() and p.resolve() == p for p in files),
            'mailbox contains indirect material')
    return bool(files)


def empty_mailbox():
    return dict(counts={}, busy={}, idle=True, turn_slots=dict(idle=0, busy=0, total=0))


@contextmanager
def mailbox_copy(path):
    """Inspect a stable main/WAL copy outside runtime state."""

    require(path.is_file() and path.resolve() == path, 'mailbox database is missing or indirect')
    require(not path.with_name(path.name+'-journal').exists(), 'mailbox has a rollback journal')
    paths = [path, path.with_name(path.name+'-wal')]
    def versions():
        return [((p.stat().st_dev, p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns, p.stat().st_ctime_ns)
                 if p.exists() else None) for p in paths]
    before = versions()
    with tempfile.TemporaryDirectory(prefix='ato-release-mailbox-') as folder:
        copied = Path(folder)/path.name
        for p in paths:
            if p.exists() or p.is_symlink():
                require(p.is_file() and p.resolve() == p, 'indirect database sidecar')
                shutil.copyfile(p, Path(folder)/p.name)
        require(versions() == before, 'mailbox changed during read-only inspection; retry at idle boundary')
        yield copied


def mailbox(state):
    if not mailbox_exists(state):
        return empty_mailbox()
    with mailbox_copy(state/'mailbox/state.sqlite') as copied, closing(sqlite3.connect(copied)) as con:
        result = mailbox_counts(con)
        return with_leases(con, result)


def with_leases(con, result):
    names = {row[0] for row in con.execute("select name from sqlite_master where type='table'")}
    if 'execution_leases' in names:
        active = con.execute("select count(*) from execution_leases where state='active'").fetchone()[0]
        if active:
            result['busy']['execution_leases'] = active
            result['idle'] = False
    return result


@contextmanager
def idle_boundary(state):
    """Reserve writers only after the predecessor has exited; never change rows."""
    if not mailbox_exists(state):
        yield
        return
    path = state/'mailbox/state.sqlite'
    con = sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=2)
    try:
        con.execute('begin immediate')
        require(with_leases(con, mailbox_counts(con))['idle'], 'mailbox is not idle; no replay or queue draining')
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


def verify_snapshot(root, commit, *, legacy=False):
    require(root.is_dir() and not root.is_symlink(), 'snapshot must be a real directory')
    manifest = json.loads(safe_record(root/MANIFEST)[0])
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
    expected = ('runner.py', 'coordination.py') if legacy else (
        'ato.py', 'ato_runtime.py', 'scripts/install.py', 'skills/ato/SKILL.md')
    require(legacy or manifest.get('surface') == SURFACE, 'snapshot has a different public interface')
    for name in (*expected, '.venv/bin/python'):
        require((root/name).is_file(), 'incomplete snapshot: '+name)
    require(os.access(root/'.venv/bin/python', os.X_OK), 'snapshot interpreter is not executable')
    if not legacy:
        require(not any((root/'bin'/name).exists() for name in LEGACY), 'snapshot has legacy public launchers')
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
    wheelhouse = env.pop('ATO_RELEASE_WHEELHOUSE', None)
    links = ['--find-links', wheelhouse] if wheelhouse is not None else []
    run_logged([root/'.venv/bin/python', '-m', 'pip', 'install', '--no-index', '--no-cache-dir',
                '--disable-pip-version-check', *links, '-r', root/'requirements-acp.lock'],
               root, env, 'python-dependencies')
    run_logged([tools['node']['path'], tools['npm']['path'], 'ci', '--offline', '--ignore-scripts', '--no-audit', '--no-fund', '--prefix', root/'integrations/pi-acp'],
               root, env, 'node-dependencies')
    return tools


def qualify(root, env):
    python = root/'.venv/bin/python'
    run_logged([python, 'scripts/run_tests.py'], root, env, 'tests')
    # The shipped test runner rejects skips. Also require its explicit receipt.
    report = (root/'.release-logs/tests.log').read_text()
    require(re.search(r'^Ran \d+ tests in [\d.]+s$', report, re.M) and
            re.search(r'^OK$', report, re.M) and not re.search(r'^(FAILED|FAIL:|ERROR:)', report, re.M),
            'missing clean full-test receipt')
    run_logged([python, 'scripts/check_architecture.py'], root, env, 'architecture')
    # Compile tracked Python sources, not third-party code or generated caches.
    sources = command(['git', 'ls-files', '-z'], cwd=root).split('\0')
    run_logged([python, '-m', 'py_compile', *[p for p in sources if p.endswith('.py')]], root, env, 'compile')


def stage(repo, commit, root, *, node_bin=None, npm_bin=None, wheelhouse=None, npm_cache=None):
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
            env = environment(Path(home)/'ato', Path(home)/'native')
            env['npm_config_cache'] = str(npm_cache or Path(home)/'npm-cache')
            if wheelhouse is not None:
                env['ATO_RELEASE_WHEELHOUSE'] = str(wheelhouse)
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
        manifest = dict(version=1, surface=SURFACE, qualified=True, root=str(root), commit=commit, tree=tree,
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


def observer_argv(source):
    python, root = source['python'], Path(source['root'])
    return ([python, '-IB', str(root/'ato_runtime.py'), 'observe'] if source.get('surface') == SURFACE
            else [python, str(root/'runner.py'), '_controller'])


class System:
    """Local process inspection and the only release-owned signaling boundary."""

    def processes(self):
        rows = command(['/bin/ps', '-axo', 'pid=,command='], env={'PATH': PATH, 'LC_ALL': 'C'})
        return [dict(pid=int(pid), command=line) for row in rows.splitlines()
                if len(parts := row.strip().split(None, 1)) == 2 for pid, line in [parts]]

    def identity(self, pid):
        return self._start_identity(pid, utc=True)

    def _start_identity(self, pid, *, utc):
        env = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}
        if utc:
            env['TZ'] = 'UTC'
        try:
            start = command(['/bin/ps', '-p', str(pid), '-o', 'lstart='], env=env)
            return hashlib.sha256(start.encode()).hexdigest() if start else None
        except subprocess.CalledProcessError:
            return None

    _native_identity = identity

    def matches_identity(self, pid, expected):
        identity = self.identity
        return expected is not None and (identity(pid) == expected or (
            getattr(identity, '__func__', None) is System._native_identity
            and self._start_identity(pid, utc=False) == expected))

    def matches_observer(self, observer):
        if observer.get('surface') == SURFACE:
            import ato_process
            return ato_process.disposition(observer['pid'], observer['pid_identity']) == 'live'
        return self.matches_identity(observer['pid'], observer['pid_identity'])

    def observer(self, state, source, *, healthy=True):
        path = state/'control/process.json'
        expected = ' '.join(observer_argv(source)) if source else None
        processes = self.processes()
        matching = [p for p in processes if p['command'] == expected]
        try:
            row = read_private(path)
        except FileNotFoundError:
            require(not matching, 'observer has no durable process record')
            return None
        # Legacy recovery.watch_ticks clears the PID and birth record on a
        # graceful exit. A live matching command still requires an identity.
        if row.get('state') == 'exited' and (source or {}).get('surface') != SURFACE:
            require('pid' in row and row['pid'] is None and 'pid_identity' in row
                    and row['pid_identity'] is None and not matching,
                    'invalid legacy observer exit record')
            return None
        pid = row.get('pid')
        require(type(pid) is int and pid > 0, 'invalid observer process record')
        occupied = next((p for p in processes if p['pid'] == pid), None)
        if not occupied:
            require(not matching, 'observer command exists with a different PID')
            return None
        require(len(matching) == 1 and occupied == matching[0],
                'expected exactly one observer selecting the recorded source')
        observer = dict(occupied, pid_identity=row.get('pid_identity'), heartbeat=row.get('heartbeat'),
                        surface=source.get('surface', 'legacy'))
        require(observer['pid_identity'] is not None and self.matches_observer(observer),
                'observer pid_identity mismatch (possible PID reuse)')
        if observer['surface'] == SURFACE:
            require(row.get('release') == source['seal'], 'observer release does not match the sealed source')
        require(not healthy or (row.get('state') == 'watching' and row.get('consecutive_failures', 0) == 0
                and isinstance(row.get('heartbeat'), (int, float)) and 0 <= time.time()-row['heartbeat'] <= 30),
                'observer is not healthy')
        return observer

    def stop(self, observer):
        require(self.matches_observer(observer), 'PID identity changed before SIGTERM')
        current = dict(pid=observer['pid'], command=observer['command'])
        require(current in self.processes(), 'observer command changed before SIGTERM')
        os.kill(observer['pid'], signal.SIGTERM)
        deadline = time.monotonic()+30
        while self.matches_observer(observer):
            require(time.monotonic() < deadline, 'observer did not exit gracefully')
            time.sleep(.1)
        require(not any(row['command'] == observer['command'] for row in self.processes()),
                'another observer appeared during graceful stop')

    def start(self, source, state, env, record, intent=None):
        require(not any(p['command'] == ' '.join(observer_argv(source)) for p in self.processes()),
                'refusing to start another observer of this source')
        unified = source.get('surface') == SURFACE
        if unified:
            self._start_unified(source, env, record, intent)
        else:
            # The prior release owns its observer bootstrap and original policies.
            code = 'import control; control.start()'
            subprocess.run([source['python'], '-c', code], cwd=source['root'],
                           env=observer_environment(source, env), check=True, capture_output=True, timeout=25)
        deadline = time.monotonic()+30
        previous = None
        while time.monotonic() < deadline:
            try:
                row = self.observer(state, source)
                if row is not None:
                    record(row)
                    if (previous and same_observer(previous, row)
                            and row['heartbeat'] > previous['heartbeat']):
                        return row
                    previous = row
            except (Refused, FileNotFoundError, json.JSONDecodeError):
                pass
            time.sleep(.2)
        raise Refused('observer did not produce two independently verified healthy heartbeats')

    def _start_unified(self, source, env, record, intent):
        import ato_process
        rd, wr = os.pipe()
        try:
            parent = os.getpid()
            birth = ato_process.identity(parent)
            require(birth is not None, 'release owner identity is unverifiable')
            argv = observer_argv(source)
            child = subprocess.Popen(argv, cwd=source['root'], env=dict(env, ATO_OBSERVER_PIPE=str(rd)),
                                     pass_fds=(rd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, start_new_session=True, umask=0o077)
            child_birth = ato_process.identity(child.pid)
            require(child_birth is not None, 'observer startup identity is unverifiable')
            record(dict(pid=child.pid, pid_identity=child_birth, command=' '.join(argv), surface=SURFACE))
            payload = json.dumps(dict(parent=parent, birth=birth, seal=source['seal'], **intent)).encode()
            require(len(payload) <= os.fpathconf(wr, 'PC_PIPE_BUF'), 'observer capability is too large')
            os.write(wr, payload)
        finally:
            os.close(rd)
            os.close(wr)


def same_observer(a, b):
    return all(a.get(key) == b.get(key) for key in ('pid', 'pid_identity', 'command'))


def read_private(path):
    require(path.resolve() == path, 'indirect runtime record: ' + str(path))
    data, info = safe_record(path)
    require(not info.st_mode & 0o022, 'writable runtime record: ' + str(path))
    return json.loads(data)


def _directory_entries(path):
    if not path.exists() and not path.is_symlink():
        return []
    require(path.is_dir() and path.resolve() == path, 'indirect runtime directory: ' + str(path))
    return sorted(path.iterdir())


def _no_live_lock(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), 'unsafe legacy lock')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def active_tasks(paths, system, sources, *, departing_seal=None):
    """Conservative checks over both ledgers and the exact selected process commands."""
    from ato_host import Host
    from ato_task_store import TaskStore

    prefixes = [str(Path(row['root'])/name) for row in sources.values()
                for name in ('runner.py', 'ato_runtime.py', 'hub_cli.py', 'pi_coordinator_exec.py',
                             'pi_production_exec.py', 'pi_acp_exec.py')]
    active = [row for row in system.processes() if any(path in row['command'] for path in prefixes)
              and not row['command'].endswith(('runner.py _controller', 'ato_runtime.py observe'))]
    blockers = ['selected runtime has active commands'] if active else []
    store = TaskStore(paths['ato_home'], native_home=paths['codex_home'])
    Host(store=store, read_only=True).compose()
    for directory in _directory_entries(store.tasks):
        if not re.fullmatch(r'task-[0-9a-f]{32}', directory.name):
            continue
        task, status, _ = store._read_projection(directory.name)
        if (departing_seal is not None and task['creation']['runtime_seal'] == departing_seal
                and status['status'] != 'completed' and not task['worktree_removed']):
            blockers.append('unfinished Task requires its selected release: ' + directory.name)
        if (task['creation']['phase'] != 'ready' or status['execution']['state'] != 'idle'
                or not status['execution']['verified'] or status['status'] == 'delivery_unknown'):
            blockers.append('unified Task execution or provisioning is unresolved')
        if any(row['phase'] in ('launching', 'active', 'unknown') for row in status['run_history'][-1:]):
            blockers.append('unified Run evidence is unsettled')
    workflows = set()
    legacy = paths['legacy_state']/'control/state.sqlite'
    if legacy.exists() or legacy.is_symlink():
        with mailbox_copy(legacy) as copied, closing(sqlite3.connect(copied)) as con:
            workflows.update(Path(row[0]) for row in con.execute('select path from workflows'))
    for hub in _directory_entries(paths['legacy_hub_home']/'hubs'):
        _no_live_lock(hub/'.hub.lock')
        for directory in _directory_entries(hub/'tasks'):
            row = read_private(directory/'state.json')
            if (row.get('owner_pid') or row.get('active_process') or
                    row.get('status') in ('running', 'launching', 'verifying', 'reviewing', 'cleanup_blocked')):
                blockers.append('legacy H1 task has unresolved activity')
            workflows.update(Path(path) for path in row.get('workflow_history', ()))
            if row.get('active_workflow_dir'):
                workflows.add(Path(row['active_workflow_dir']))
    for directory in workflows:
        require(directory.is_absolute(), 'unverifiable legacy workflow path')
        row = read_private(directory/'state.json')
        _no_live_lock(directory.parent.parent/'.worker.lock')
        if (row.get('owner_pid') or row.get('active_process') or
                row.get('status') in ('running', 'launching', 'cleanup_blocked', 'delivery_unknown')):
            blockers.append('legacy workflow has unresolved activity')
        if row.get('active_run'):
            child = read_private(Path(row['active_run'])/'state.json')
            if child.get('owner_pid') or child.get('active_process'):
                blockers.append('legacy workflow stage has unresolved activity')
    return sorted(set(blockers))


def atomic_entry(path, row):
    if row is None:
        path.unlink(missing_ok=True)
        sync_directory(path.parent)
        return
    data = base64.b64decode(row['data'], validate=True)
    require(hashlib.sha256(data).hexdigest() == row['sha256'], 'entrypoint backup checksum mismatch')
    atomic_bytes(path, data, row['mode'])


def atomic_bytes(path, data, mode):
    temp = path.with_name('.'+path.name+'.release-'+uuid.uuid4().hex)
    try:
        fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def release_lock(bin_dir, *, shared=False):
    require(bin_dir.is_dir() and bin_dir.resolve() == bin_dir, 'bin-dir must be an existing real directory')
    fd = os.open(bin_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        require(info.st_uid == os.getuid() and not info.st_mode & 0o022, 'invalid release directory')
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def scoped_paths(bin_dir, ato_home, codex_home, *, legacy_home=None, legacy_codex=None,
                 legacy_hub_home=None, legacy_context=False):
    legacy_home = legacy_home or (codex_home if legacy_context else ato_home)
    legacy_codex = legacy_codex or codex_home
    require(ato_home != codex_home and not ato_home.is_relative_to(codex_home)
            and not codex_home.is_relative_to(ato_home), 'unified ATO and native homes must be separate')
    require(not legacy_home.is_relative_to(ato_home/'unified-v1'), 'legacy home overlaps unified namespace')
    return dict(bin_dir=bin_dir, ato_home=ato_home, codex_home=codex_home,
                legacy_home=legacy_home, legacy_codex=legacy_codex,
                legacy_hub_home=legacy_hub_home or legacy_home, legacy_context=legacy_context,
                legacy_hub_explicit=legacy_hub_home is not None,
                legacy_state=context(legacy_home, legacy_codex, legacy_context),
                unified_state=ato_home/'unified-v1/kernel/ctask-interaction')


def selected_state(paths, source):
    return paths['unified_state' if source.get('surface') == SURFACE else 'legacy_state']


def selected_environment(paths, source):
    if source.get('surface') == SURFACE:
        return environment(paths['ato_home'], paths['codex_home'])
    env = environment(paths['legacy_home'], paths['legacy_codex'])
    if paths['legacy_context']:
        env.pop('ATO_HOME')
    env['CTASK_HANDOFF_READER'] = str(paths['bin_dir']/'ato-read-handoff')
    return env


def policy_identity(paths):
    result = {}
    for key in ('legacy_state', 'unified_state'):
        state = paths[key]
        rows = {}
        for name in POLICIES:
            path = state/name
            if path.exists() or path.is_symlink():
                read_private(path)
                rows[name] = sha(path)
        result[key] = rows
    observer = paths['ato_home']/'unified-v1/host/observation.json'
    if observer.exists() or observer.is_symlink():
        read_private(observer)
        result['observation'] = sha(observer)
    return result


def departing_seal(previous, target):
    if previous and previous.get('surface') == SURFACE and previous['seal'] != (target or {}).get('seal'):
        return previous['seal']
    return None


def inspect(paths, system, target=None):
    entries = entrypoints(paths['bin_dir'])
    sources = launcher_sources(entries)
    if entries['ato-hub'] is not None:
        require(paths['legacy_hub_explicit'], 'select the exact legacy H1 home with --legacy-hub-home')
    mixed = len({(s['root'], s['python']) for s in sources.values()}) > 1
    lookup = [(paths['legacy_state'], sources.get('ctask-notify')),
              (paths['unified_state'], sources.get(PUBLIC) or target)]
    observers = []
    for state, source in lookup:
        row = system.observer(state, source)
        if row is not None:
            require(source is not None, 'observer has no verified source')
            require(state != paths['unified_state'] or PUBLIC in sources,
                    'candidate observer is already active without an installed selection')
            observers.append((source, row))
    commands = {' '.join(observer_argv(row)) for row in (sources.get('ctask-notify'), sources.get(PUBLIC), target)
                if row}
    require(len(observers) <= 1 and sum(p['command'] in commands for p in system.processes()) <= len(observers),
            'more than one or unrecorded observer at the selected boundary')
    source, observer = observers[0] if observers else (None, None)
    policy = policy_identity(paths)
    if observer and source.get('surface') != SURFACE:
        cfg = read_private(paths['legacy_state']/'control/config.json')
        hub = read_private(paths['legacy_state']/'hub.json')
        require(cfg.get('enabled') is True and cfg.get('hub_thread') == hub.get('thread_id'),
                'legacy observer policy or binding is inconsistent')
    mailboxes = {key: mailbox(paths[key]) for key in ('legacy_state', 'unified_state')}
    departure = departing_seal(sources.get(PUBLIC), target) if target else None
    blockers = active_tasks(paths, system, sources | ({'target': target} if target else {}),
                            departing_seal=departure)
    selected = sources.get(PUBLIC)
    if (target and selected and not mixed
            and all(selected[key] == target[key] for key in ('root', 'python', 'seal'))):
        blockers.append('snapshot is already selected')
    if not all(box['idle'] for box in mailboxes.values()):
        blockers.append('mailbox is busy; release does not drain or replay it')
    if observer:
        config = paths['ato_home']/'unified-v1/host/observation.json'
        if 'observation' not in policy or read_private(config).get('enabled') is not True:
            blockers.append('target observer has no authorized configuration')
        elif not mailbox_exists(paths['unified_state']):
            blockers.append('target observer has no selected mailbox')
    return dict(entries=entries, sources=sources, mixed_sources=mixed, observer=observer,
                observer_source=source, mailboxes=mailboxes, policies=policy, blockers=blockers)


def entry_set(source):
    data = launcher_bytes(Path(source['root']), source['python'], source['seal'])
    row = dict(data=base64.b64encode(data).decode(), sha256=hashlib.sha256(data).hexdigest(), mode=0o755)
    return dict.fromkeys(ENTRIES) | {PUBLIC: row}


def contexts(paths):
    return {key: str(value) if isinstance(value, Path) else value for key, value in paths.items()}


def _write_entries(path, journal, paths, desired):
    bin_dir = paths['bin_dir']
    for name in ENTRIES:
        current = file_record(bin_dir/name)
        require(current in (journal['before']['entries'][name], journal['after_entries'][name]),
                'entrypoint changed outside this transaction: ' + name)
        if current == desired[name]:
            continue
        journal['entry_intent'] = name
        save(path, journal)
        atomic_entry(bin_dir/name, desired[name])
        journal['entry_intent'] = None
        save(path, journal)
    require(entrypoints(bin_dir) == desired, 'entrypoint readback differs from transaction')


def _matches_entries(paths, journal):
    for name, current in entrypoints(paths['bin_dir']).items():
        require(current in (journal['before']['entries'][name], journal['after_entries'][name]),
                'entrypoint changed outside this transaction: ' + name)


def _quiescent(paths, system, journal, *, departure=None):
    _matches_entries(paths, journal)
    require(policy_identity(paths) == journal['before']['policies'], 'release policies changed')
    blockers = active_tasks(paths, system, journal['before']['sources'] | {'target': journal['target']},
                            departing_seal=departure)
    require(not blockers, 'task activity at the release boundary: ' + '; '.join(blockers))
    require(all(mailbox(paths[key])['idle'] for key in ('legacy_state', 'unified_state')),
            'mailbox is busy; release does not drain or replay it')


@contextmanager
def _writer_boundaries(paths):
    with ExitStack() as stack:
        for key in ('legacy_state', 'unified_state'):
            stack.enter_context(idle_boundary(paths[key]))
        yield


def _start(path, journal, paths, system, source, key):
    journal['observer_starting'] = key
    nonce = secrets.token_hex(32)
    journal['observer_nonce_sha256'] = hashlib.sha256(nonce.encode()).hexdigest()
    save(path, journal)

    def record(row):
        if prior := journal.get(key):
            require(same_observer(prior, row), 'another observer appeared during startup')
        journal[key] = row
        save(path, journal)

    row = system.start(source, selected_state(paths, source), selected_environment(paths, source), record,
                       dict(journal=str(path), nonce=nonce))
    record(row)
    journal['observer_starting'] = None
    journal.pop('observer_nonce_sha256', None)
    save(path, journal)
    return row


def _observers(paths, journal, system):
    """Resolve only recorded observer identities, including a failed startup."""
    old, new = journal['before']['observer_source'], journal['target']
    legacy = old if old and old.get('surface') != SURFACE else None
    unified = [new, old] if old and old.get('surface') == SURFACE else [new]
    allowed = [journal['before']['observer'], journal.get('new_observer'), journal.get('rollback_observer')]
    processes = system.processes()
    current = []
    for state, candidates in ((paths['legacy_state'], [legacy]), (paths['unified_state'], unified)):
        commands = {' '.join(observer_argv(source)): source for source in candidates if source}
        matching = [row for row in processes if row['command'] in commands]
        require(len(matching) <= 1, 'ambiguous observer at release boundary')
        source = commands[matching[0]['command']] if matching else candidates[0]
        launched = journal.get(journal.get('observer_starting'))
        try:
            observer = system.observer(state, source, healthy=False)
        except Refused:
            # Before its first heartbeat, only a child with this transaction's
            # exact recorded spawn receipt can be stopped.
            if (not launched or not matching or launched['surface'] != SURFACE
                    or matching[0] != dict(pid=launched['pid'], command=launched['command'])
                    or not system.matches_observer(launched)):
                raise
            observer = launched
        if observer is not None:
            require(any(row and same_observer(row, observer) for row in allowed),
                    'unrelated observer at release boundary')
            current.append(observer)
    require(len(current) <= 1, 'ambiguous observer at release boundary')
    return current[0] if current else None


def restore(path, journal, paths, system):
    for source in journal['before']['sources'].values():
        verify_source(source)
    verify_source(journal['target'])
    current = entrypoints(paths['bin_dir'])[PUBLIC]
    old_source = journal['before']['sources'].get(PUBLIC)
    departure = departing_seal(journal['target'], old_source) if current == journal['after_entries'][PUBLIC] else None
    _quiescent(paths, system, journal, departure=departure)
    observer = _observers(paths, journal, system)
    old = journal['before']['observer']
    restored = journal.get('rollback_observer')
    keep = observer is not None and ((old and same_observer(old, observer))
                                     or (restored and same_observer(restored, observer)))
    if journal['state'] == 'rolled_back':
        require(entrypoints(paths['bin_dir']) == journal['before']['entries']
                and (keep if old else observer is None), 'rolled back state changed')
        return observer
    if observer and not keep:
        system.stop(observer)
    with _writer_boundaries(paths):
        current = _observers(paths, journal, system)
        require((current is None and not keep) or (keep and current and same_observer(current, observer)),
                'observer changed at rollback boundary')
        require(policy_identity(paths) == journal['before']['policies'], 'release policies changed')
        require(not active_tasks(paths, system, journal['before']['sources'] | {'target': journal['target']},
                                 departing_seal=departure),
                'task activity at rollback boundary')
        _write_entries(path, journal, paths, journal['before']['entries'])
        journal['state'] = 'entries_restored'
        save(path, journal)
    if old and not keep:
        journal.pop('rollback_observer', None)
        observer = _start(path, journal, paths, system, journal['before']['observer_source'], 'rollback_observer')
    require(policy_identity(paths) == journal['before']['policies'], 'release policies changed')
    journal.update(state='rolled_back', rollback_observer=observer if old else None)
    save(path, journal)
    return journal['rollback_observer']


def activate(root, commit, paths, transaction, system=None, *, converge_mixed=False):
    system = system or System()
    target = snapshot_source(root)
    require(target['surface'] == SURFACE and target['commit'] == commit, 'wrong cutover snapshot')
    target['python_sha256'] = sha(target['python'])
    with release_lock(paths['bin_dir']):
        before = inspect(paths, system, target)
        require(not before['blockers'], '; '.join(before['blockers']))
        require(not before['mixed_sources'] or converge_mixed, 'mixed entries require explicit convergence')
        transaction.mkdir(mode=0o700, parents=False)
        sync_directory(transaction.parent)
        path = transaction/'transaction.json'
        journal = dict(version=2, state='prepared', paths=contexts(paths), before=before,
                       after_entries=entry_set(target), target=target)
        save(path, journal)
        try:
            departure = departing_seal(before['sources'].get(PUBLIC), target)
            _quiescent(paths, system, journal, departure=departure)
            require(entrypoints(paths['bin_dir']) == before['entries'], 'entrypoints changed during preparation')
            observed = _observers(paths, journal, system)
            same = observed is None if before['observer'] is None else same_observer(observed or {}, before['observer'])
            require(same, 'observer changed during preparation')
            if before['observer']:
                system.stop(before['observer'])
            journal['state'] = 'observer_stopped'
            save(path, journal)
            with _writer_boundaries(paths):
                require(_observers(paths, journal, system) is None, 'observer appeared at cutover boundary')
                verify_source(target)
                for source in before['sources'].values():
                    verify_source(source)
                require(policy_identity(paths) == before['policies'], 'release policies changed')
                require(not active_tasks(paths, system, before['sources'] | {'target': target},
                                         departing_seal=departure),
                        'task activity at cutover boundary')
                _write_entries(path, journal, paths, journal['after_entries'])
            journal['state'] = 'entries_switched'
            save(path, journal)
            observer = _start(path, journal, paths, system, target, 'new_observer') if before['observer'] else None
            require(entrypoints(paths['bin_dir']) == journal['after_entries']
                    and policy_identity(paths) == before['policies'], 'entrypoints or policies changed during cutover')
            journal['state'] = 'activated'
            save(path, journal)
            return dict(state='activated', transaction=str(path), observer=observer)
        except BaseException as exc:
            journal['error_type'] = type(exc).__name__
            try:
                restore(path, journal, paths, system)
            except BaseException as recovery_error:
                journal.update(state='recovery_required', recovery_error_type=type(recovery_error).__name__)
                save(path, journal)
            raise


def rollback(transaction, paths, system=None):
    system = system or System()
    path = transaction/'transaction.json'
    with release_lock(paths['bin_dir']):
        require(transaction.resolve() == transaction and transaction.stat().st_uid == os.getuid()
                and stat.S_IMODE(transaction.stat().st_mode) == 0o700, 'transaction directory is not private')
        _, info = safe_record(path)
        require(stat.S_IMODE(info.st_mode) == 0o600, 'transaction journal is not private')
        journal = read_private(path)
        require(journal.get('version') == 2 and journal.get('paths') == contexts(paths),
                'rollback context differs from the recorded transaction')
        try:
            observer = restore(path, journal, paths, system)
        except BaseException as exc:
            journal.update(state='recovery_required', recovery_error_type=type(exc).__name__)
            save(path, journal)
            raise
    return dict(state='rolled_back', transaction=str(path), observer=observer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    modes = parser.add_mutually_exclusive_group(required=True)
    for name in ('dry-run', 'stage', 'activate', 'rollback'):
        modes.add_argument('--'+name, action='store_true')
    for name in ('repo', 'snapshot', 'bin-dir', 'ato-home', 'codex-home', 'transaction',
                 'legacy-ato-home', 'legacy-codex-home', 'legacy-hub-home',
                 'node-bin', 'npm-bin', 'wheelhouse', 'npm-cache'):
        parser.add_argument('--'+name, type=Path)
    parser.add_argument('--legacy-context', action='store_true', help='Old CODEX_HOME-only layout')
    parser.add_argument('--commit', help='Full Git commit for stage, diagnosis or activation')
    parser.add_argument('--allow-runtime-change', action='store_true')
    parser.add_argument('--converge-mixed-entrypoints', action='store_true')
    args = parser.parse_args(argv)

    def need(*names):
        for name in names:
            if getattr(args, name) is None:
                parser.error('--'+name.replace('_', '-')+' is required')

    try:
        if not args.activate and args.converge_mixed_entrypoints:
            parser.error('--converge-mixed-entrypoints requires --activate')
        if not args.rollback:
            need('snapshot', 'commit')
            require(re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', args.commit), 'select a full Git commit')
        if args.stage or args.dry_run:
            need('repo')
        for value in vars(args).values():
            if isinstance(value, Path):
                require(value.is_absolute() and value == value.resolve(), 'select absolute canonical paths')
        if args.stage:
            need('wheelhouse', 'npm_cache')
            result = stage(args.repo, args.commit, args.snapshot, node_bin=args.node_bin, npm_bin=args.npm_bin,
                           wheelhouse=args.wheelhouse, npm_cache=args.npm_cache)
        else:
            need('bin_dir', 'ato_home', 'codex_home')
            paths = scoped_paths(args.bin_dir, args.ato_home, args.codex_home, legacy_home=args.legacy_ato_home,
                                 legacy_codex=args.legacy_codex_home, legacy_hub_home=args.legacy_hub_home,
                                 legacy_context=args.legacy_context)
            if args.activate or args.rollback:
                need('transaction')
                require(args.allow_runtime_change, 'explicit --allow-runtime-change is required')
                for key, value in paths.items():
                    if isinstance(value, Path):
                        require(not args.transaction.is_relative_to(value) and not value.is_relative_to(args.transaction),
                                'transaction is outside executable and state directories')
                if args.activate:
                    require(not args.transaction.is_relative_to(args.snapshot)
                            and not args.snapshot.is_relative_to(args.transaction), 'transaction overlaps snapshot')
                    result = activate(args.snapshot, args.commit, paths, args.transaction,
                                      converge_mixed=args.converge_mixed_entrypoints)
                else:
                    result = rollback(args.transaction, paths)
            else:
                commit, tree = git_commit(args.repo, args.commit)
                target = snapshot_source(args.snapshot) if args.snapshot.exists() else None
                if target:
                    require(target['commit'] == commit and target['surface'] == SURFACE
                            and read(args.snapshot/MANIFEST)['tree'] == tree, 'snapshot differs from selected Git tree')
                result = inspect(paths, System(), target)
                result['entrypoints'] = {name: (dict(present=True, mode=row['mode'], sha256=row['sha256'])
                                                if row else dict(present=False))
                                         for name, row in result.pop('entries').items()}
                blockers = result['blockers']
                if not target:
                    blockers.append('snapshot must be staged and qualified')
                if result['mixed_sources']:
                    blockers.append('explicit convergence required')
                result.update(mode='dry-run', commit=commit, tree=tree, snapshot=str(args.snapshot),
                              activation_ready=not blockers)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, sqlite3.Error, tarfile.TarError) as exc:
        print('Release refused: '+str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    raise SystemExit(main())
