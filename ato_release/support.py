"""Shared release verification and read-only runtime observations."""
import base64
from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time

LEGACY = ('ctask', 'ctask-notify', 'ato-read-handoff', 'ato-hub')
SURFACE = 'ato-v1'
BUSY = frozenset(('queued', 'claimed', 'dispatching', 'delivery_unknown'))
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


def command(argv, *, cwd=None, env=None):
    return subprocess.check_output([str(a) for a in argv], cwd=cwd, env=env, text=True).strip()


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


def launcher_bytes(root, python, digest):
    return ('#!/bin/sh\nexport ATO_LAUNCH_SEAL=' + digest + '\nexport ATO_LAUNCHER="$0"\nexec ' +
            shlex.join([str(python), '-IB', str(root/'ato.py')]) + ' "$@"\n').encode()


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
                with os.fdopen(os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as source:
                    require(stat.S_ISREG(os.fstat(source.fileno()).st_mode), 'indirect database sidecar')
                    with (Path(folder)/p.name).open('xb') as target:
                        shutil.copyfileobj(source, target)
        require(versions() == before and not os.path.lexists(path.with_name(path.name+'-journal')),
                'mailbox changed during read-only inspection; retry at idle boundary')
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
                require(not external or re.fullmatch(r'\.venv/bin/python(?:3(?:\.\d+)?)?', relative),
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


_PYTHON_ENTRY = re.compile(r'python(?:3(?:\.\d+)?)?')


def _verify_venv(root, files):
    def verify_file(path, row):
        require(row.get('kind') == 'file' and not path.is_symlink() and path.is_file(),
                'sealed interpreter file changed')
        mode = stat.S_IMODE(path.stat().st_mode)
        require(mode == row['mode'] and not mode & 0o222 and sha(path) == row['sha256'],
                'sealed interpreter file changed')

    for name in ('.venv', '.venv/bin'):
        path = root/name
        require(not path.is_symlink() and path.is_dir(), 'sealed interpreter directory changed')
        mode = stat.S_IMODE(path.stat().st_mode)
        require(not mode & 0o222 and files.get(name) == dict(kind='directory', mode=mode),
                'sealed interpreter directory changed')

    config = root/'.venv/pyvenv.cfg'
    config_record = files.get('.venv/pyvenv.cfg')
    if config_record is not None or os.path.lexists(config):
        verify_file(config, config_record or {})

    bin_dir = root/'.venv/bin'
    entries = {path.name for path in bin_dir.iterdir() if _PYTHON_ENTRY.fullmatch(path.name)}
    prefix = '.venv/bin/'
    recorded = {name[len(prefix):] for name in files
                if name.startswith(prefix) and _PYTHON_ENTRY.fullmatch(name[len(prefix):])}
    require(entries == recorded and 'python' in entries, 'sealed interpreter entries changed')
    for name in recorded:
        path = bin_dir/name
        row = files['.venv/bin/'+name]
        if row.get('kind') != 'link':
            verify_file(path, row)
            continue
        require(path.is_symlink() and os.readlink(path) == row['target'] and path.is_file(),
                'sealed interpreter link changed')
        resolved = path.resolve()
        if resolved.is_relative_to(root):
            require(row.get('external_sha256') is None, 'sealed interpreter link changed')
            verify_file(resolved, files.get(resolved.relative_to(root).as_posix(), {}))
        else:
            require(sha(resolved) == row.get('external_sha256'), 'sealed interpreter changed')


def verify_snapshot(root, commit, *, legacy=False, full=True, allow_interpreter_change=False):
    require(root.is_dir() and not root.is_symlink(), 'snapshot must be a real directory')
    manifest = json.loads(safe_record(root/MANIFEST)[0])
    require(manifest.get('version') == 1 and manifest.get('qualified') is True and
            isinstance(commit, str) and re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', commit)
            and manifest.get('commit') == commit and manifest.get('root') == str(root),
            'snapshot is unqualified, relocated or selects another commit')
    require(root.stat().st_uid == os.getuid() and not root.stat().st_mode & 0o222,
            'snapshot root is not owned and sealed read-only')
    require(not (root/MANIFEST).is_symlink() and not (root/MANIFEST).stat().st_mode & 0o222,
            'snapshot manifest is not sealed')
    files = manifest['files']
    if full:
        node = manifest.get('tools', {}).get('node')
        if node is not None:
            verify_tools({'node': node})
        actual = inventory(root)
        if allow_interpreter_change:
            for name, info in actual.items():
                if info.get('external_sha256') and re.fullmatch(r'\.venv/bin/python(?:3(?:\.\d+)?)?', name):
                    host_tool('prior interpreter', root/name)
                    info['external_sha256'] = files.get(name, {}).get('external_sha256')
        require(actual == files, 'snapshot content, links or modes changed')
    else:
        expected = manifest.get('source_files')
        if expected is None:
            expected = {name: row['sha256'] for name, row in files.items() if row['kind'] == 'file'
                        and not Path(name).parts[0].startswith('.') and 'node_modules' not in Path(name).parts}
        actual = {}
        for name, digest in expected.items():
            path = root/name
            require(path.is_relative_to(root) and path.resolve() == path and files[name]['kind'] == 'file',
                    'invalid sealed source path')
            mode = stat.S_IMODE(path.lstat().st_mode)
            require(path.is_file() and mode == files[name]['mode'] and sha(path) == digest
                    and digest == files[name]['sha256'], 'sealed source changed')
            actual[name] = files[name]
        try:
            _verify_venv(root, files)
        except OSError:
            raise Refused('sealed interpreter material changed during inspection') from None
    require(all(not row.get('mode', 0) & 0o222 for row in actual.values()), 'snapshot contains writable files')
    expected = ('runner.py', 'coordination.py') if legacy else (
        'ato.py', 'ato_runtime.py', 'scripts/install.py', 'skills/ato/SKILL.md')
    require(legacy or manifest.get('surface') == SURFACE, 'snapshot has a different public interface')
    for name in (*expected, '.venv/bin/python'):
        require((root/name).is_file(), 'incomplete snapshot: '+name)
        if not full and not name.startswith('.venv/'):
            require(name in actual, 'missing sealed source: '+name)
    require(os.access(root/'.venv/bin/python', os.X_OK), 'snapshot interpreter is not executable')
    if not legacy:
        require(not any((root/'bin'/name).exists() for name in LEGACY), 'snapshot has legacy public launchers')
    return manifest


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


def observer_argv(source):
    python, root = source['python'], Path(source['root'])
    return ([python, '-IB', str(root/'ato_runtime.py'), 'observe'] if source.get('surface') == SURFACE
            else [python, str(root/'runner.py'), '_controller'])


def same_observer(a, b):
    return all(a.get(key) == b.get(key) for key in ('pid', 'pid_identity', 'command'))


def is_legacy_observer(command):
    return re.search(r'(?:^|[ /])runner\.py _controller$', command) is not None


def read_private(path):
    require(path.resolve() == path, 'indirect runtime record: ' + str(path))
    data, info = safe_record(path)
    require(not info.st_mode & 0o022, 'writable runtime record: ' + str(path))
    return json.loads(data)


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


class ObserverProbe:
    """Validate observer command, PID identity and heartbeat without sending signals."""

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
            getattr(identity, '__func__', None) is ObserverProbe._native_identity
            and self._start_identity(pid, utc=False) == expected))

    def matches_observer(self, observer):
        if observer.get('surface') == SURFACE:
            from ato_task import ato_process
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
        pid = row.get('pid')
        unified = (source or {}).get('surface') == SURFACE
        if not unified and row.get('state') == 'exited':
            if source is None:
                matching = [p for p in processes if is_legacy_observer(p['command'])]
            require('pid' in row and pid is None and row.get('pid_identity') is None and not matching,
                    'invalid legacy observer exit record')
            return None
        require(type(pid) is int and pid > 0, 'invalid observer process record')
        if (unified or (source is None and isinstance(row.get('release'), str))) and row.get('state') == 'stopped':
            from ato_task import ato_process
            if source is None:
                matching = [p for p in processes if p['command'].endswith('/ato_runtime.py observe')]
            require(not matching
                    and ato_process.disposition(pid, row.get('pid_identity')) in {'absent', 'reused'},
                    'unified observer stop is unverified')
            return None
        occupied = next((p for p in processes if p['pid'] == pid), None)
        if not occupied:
            require(not matching, 'observer command exists with a different PID')
            return None
        require(source and len(matching) == 1 and occupied == matching[0],
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
