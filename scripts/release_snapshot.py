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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ato_release_lib import (
    LEGACY, MANIFEST, PATH, SURFACE, ObserverProbe, Refused, command, file_record, host_tool,
    inventory, is_legacy_observer, launcher_bytes, mailbox, mailbox_copy, mailbox_exists,
    observer_argv, read, read_private, release_lock, require, safe_record, same_observer,
    sha, verify_snapshot, verify_tools,
)


PUBLIC = 'ato'
ENTRIES = (PUBLIC, *LEGACY)
POLICIES = ('hub.json', 'notification-policy.json', 'notification-project-mutes.json',
            'time-policy.json', 'failure-alerts.json', 'control/config.json')


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


def environment(ato_home, codex_home, *, home=None):
    # Never inherit actor/session credentials, Python/Node startup hooks or
    # platform loader settings from the invoking shell.
    home = Path.home().resolve() if home is None else home
    env = dict(PATH=PATH, HOME=str(home), LC_ALL='C', ATO_HOME=str(ato_home),
               CODEX_HOME=str(codex_home), PYTHONDONTWRITEBYTECODE='1')
    return env


def context(ato_home, codex_home, legacy=False):
    if legacy:
        require(ato_home == codex_home, 'legacy context requires identical ATO/Codex homes')
        return ato_home/'ctask-interaction'
    namespace = hashlib.sha256(str(codex_home).encode()).hexdigest()
    return ato_home/'ctask-interaction/contexts'/namespace


def entrypoints(bin_dir):
    if bin_dir.exists() or bin_dir.is_symlink():
        require(bin_dir.is_dir() and not bin_dir.is_symlink() and bin_dir.resolve() == bin_dir,
                'bin-dir must be an existing real directory')
    return {name: file_record(bin_dir/name) for name in ENTRIES}


def snapshot_source(root, *, allow_interpreter_change=False):
    data, _ = safe_record(root/MANIFEST)
    doc = json.loads(data)
    verify_snapshot(root, doc['commit'], legacy=doc.get('surface') != SURFACE,
                    allow_interpreter_change=allow_interpreter_change)
    return dict(root=str(root), python=str(root/'.venv/bin/python'), seal=hashlib.sha256(data).hexdigest(),
                surface=doc.get('surface', 'legacy'), commit=doc['commit'])


def verify_source(source):
    actual = snapshot_source(Path(source['root']), allow_interpreter_change=source.get('interpreter_rebound', False))
    require(source['root'] == actual['root'] and source['seal'] == actual['seal']
            and source['surface'] == actual['surface'] and source['commit'] == actual['commit']
            and source['python_sha256'] == sha(source['python']), 'recorded source or interpreter changed')
    if source['surface'] == SURFACE:
        require(source['python'] == actual['python'], 'unified interpreter differs from the seal')
    return actual


def launcher_sources(entries, *, allow_interpreter_change=False):
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
            try:
                sources[root] = snapshot_source(root)
            except Refused:
                if not allow_interpreter_change:
                    raise
                rebound = snapshot_source(root, allow_interpreter_change=True)
                sources[root] = dict(rebound, interpreter_rebound=True)
        source = sources[root]
        require((source['surface'] == SURFACE) == (name == PUBLIC), 'launcher selects a cross-version source')
        if name == PUBLIC:
            require(str(python) == source['python'] and data == launcher_bytes(root, python, source['seal']),
                    'unified launcher differs from the sealed selection')
        else:
            host_tool('prior interpreter', python)
        selected[name] = dict(source, python=str(python), script=str(script), python_sha256=sha(python))
    return selected


@contextmanager
def mailbox_writer_boundary(state):
    """Reserve the SQLite writer while publishing the launcher selection."""
    if not mailbox_exists(state):
        yield
        return
    path = state/'mailbox/state.sqlite'
    con = sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=2)
    try:
        con.execute('begin immediate')
        yield
    finally:
        con.rollback()
        con.close()


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


def observer_environment(source, env):
    env = dict(env)
    path = Path(source['root'])/MANIFEST
    if path.is_file():
        tools = read(path).get('tools', {})
        verify_tools({key: value for key, value in tools.items() if key == 'node'})
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
    npm = Path(tools['npm']['path'])
    npm_command = [tools['node']['path'], str(npm)] if npm.suffix in {'.js', '.cjs'} else [str(npm)]
    npm_env = dict(env, PATH=os.pathsep.join((str(Path(tools['node']['path']).parent), env['PATH'])))
    run_logged([*npm_command, 'ci', '--offline', '--ignore-scripts', '--no-audit', '--no-fund', '--prefix',
                root/'integrations/pi-acp'], root, npm_env, 'node-dependencies')
    return tools


def run_release_tests(root, env):
    python = root/'.venv/bin/python'
    run_logged([python, 'scripts/run_tests.py'], root, env, 'tests')
    # The shipped test runner rejects skips. Also require its explicit receipt.
    report = (root/'.release-logs/tests.log').read_text()
    require(re.search(r'^Ran \d+ tests in [\d.]+s$', report, re.M) and
            re.search(r'^OK$', report, re.M) and not re.search(r'^(FAILED|FAIL:|ERROR:)', report, re.M),
            'missing clean full-test receipt')


def qualify(root, env):
    python = root/'.venv/bin/python'
    run_logged([python, 'scripts/check_architecture.py'], root, env, 'architecture')
    # Compile tracked Python sources, not third-party code or generated caches.
    sources = command(['git', 'ls-files', '-z'], cwd=root).split('\0')
    run_logged([python, '-m', 'py_compile', *[p for p in sources if p.endswith('.py')]], root, env, 'compile')


def stage(repo, commit, root, *, node_bin=None, npm_bin=None, wheelhouse=None, npm_cache=None, run_tests=False):
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
            env = environment(Path(home)/'ato', Path(home)/'native', home=Path(home))
            env['npm_config_cache'] = str(npm_cache or Path(home)/'npm-cache')
            if wheelhouse is not None:
                env['ATO_RELEASE_WHEELHOUSE'] = str(wheelhouse)
            env['ATO_RELEASE_SOURCE'] = str(repo)
            for name, value in (('NODE', node_bin), ('NPM', npm_bin)):
                if value is not None:
                    env['ATO_RELEASE_'+name] = str(value)
            tools = install_dependencies(root, env)
            if run_tests:
                run_release_tests(root, env)
            qualify(root, env)
        require(all(sha(root/p) == digest for p, digest in source.items()), 'qualification modified source files')
        shutil.rmtree(root/'.git')
        # Seal before collecting expected modes; briefly reopen just the root
        # to write its final private manifest, then close it permanently.
        seal(root)
        root.chmod(0o700)
        manifest = dict(version=1, surface=SURFACE, qualified=True, tests="passed" if run_tests else "not_run", root=str(root), commit=commit, tree=tree,
                        source_files=source, files=inventory(root),
                        tools={k:v for k,v in (tools or {}).items() if k == 'node'},
                        build_tools=tools or {}, created_at=time.time())
        save(root/MANIFEST, manifest)
        (root/MANIFEST).chmod(0o400)
        root.chmod(0o500)
        verify_snapshot(root, commit)
        return dict(snapshot=str(root), commit=commit, tree=tree, qualified=True, tests=manifest["tests"], activated=False)
    except BaseException:
        # Keep failed evidence; absence of a sealed qualified manifest prevents
        # activation. Never remove an operator-selected existing directory.
        root.chmod(0o700)
        (root/MANIFEST).unlink(missing_ok=True)
        raise


class System(ObserverProbe):

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
            env = observer_environment(source, env)
            try:
                subprocess.run([source['python'], '-c', code], cwd=source['root'],
                               env=env, check=True, capture_output=True, timeout=25)
            except (OSError, subprocess.SubprocessError):
                self._record_legacy_start(source, record)
                raise
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
        if not unified and previous is None:
            self._record_legacy_start(source, record)
        raise Refused('observer did not produce two independently verified healthy heartbeats')

    def _record_legacy_start(self, source, record):
        expected = ' '.join(observer_argv(source))
        matching = [row for row in self.processes() if row['command'] == expected]
        if len(matching) == 1 and (birth := self.identity(matching[0]['pid'])):
            record(dict(matching[0], pid_identity=birth, surface='legacy'))

    def _start_unified(self, source, env, record, intent):
        import ato_process
        rd, wr = os.pipe()
        try:
            parent = os.getpid()
            birth = ato_process.identity(parent)
            require(birth is not None, 'release owner identity is unverifiable')
            argv = observer_argv(source)
            env = observer_environment(source, env)
            if override := os.environ.get('CTASK_QUEUE_BIN'):
                env['CTASK_QUEUE_BIN'] = host_tool('queue', override)['path']
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


def _directory_entries(path):
    if not path.exists() and not path.is_symlink():
        return []
    require(path.is_dir() and path.resolve() == path, 'indirect runtime directory: ' + str(path))
    return sorted(path.iterdir())


def _no_live_lock(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return True
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), 'unsafe legacy lock')
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    finally:
        os.close(fd)


def active_tasks(paths, system, sources):
    """Fence unresolved execution while retaining idle tasks and their sealed source."""
    prefixes = [str(Path(row['root'])/name) for row in sources.values()
                for name in ('runner.py', 'ato.py', 'ato_runtime.py', 'hub_cli.py', 'coordination.py',
                             'pi_coordinator_exec.py', 'pi_production_exec.py', 'pi_acp_exec.py', 'pi_acp_worker.py')]
    active = [row for row in system.processes() if any(path in row['command'] for path in prefixes)
              and not row['command'].endswith(('runner.py _controller', 'ato_runtime.py observe'))]
    blockers = ['selected runtime has active commands'] if active else []
    if os.path.lexists(paths['ato_home']/'unified-v1'):
        blockers.extend(_unified_blockers(paths))
    workflows = set()
    legacy = paths['legacy_state']/'control/state.sqlite'
    if legacy.exists() or legacy.is_symlink():
        with mailbox_copy(legacy) as copied, closing(sqlite3.connect(copied)) as con:
            workflows.update(Path(row[0]) for row in con.execute('select path from workflows'))
    for hub in _directory_entries(paths['legacy_hub_home']/'hubs'):
        if not _no_live_lock(hub/'.hub.lock'):
            blockers.append('legacy H1 lock is busy: ' + str(hub/'.hub.lock'))
        for directory in _directory_entries(hub/'tasks'):
            row = read_private(directory/'state.json')
            if row.get('status') == 'cleaned':
                continue
            if (row.get('owner_pid') or row.get('active_process') or
                    row.get('notice') == 'delivery_unknown' or row.get('status') in (
                        'running', 'launching', 'verifying', 'reviewing', 'cleanup_blocked', 'running_worker', 'fixing')):
                blockers.append('legacy H1 task has unresolved activity')
            workflows.update(Path(path) for path in row.get('workflow_history', ()))
            if row.get('active_workflow_dir'):
                workflows.add(Path(row['active_workflow_dir']))
    for directory in workflows:
        require(directory.is_absolute(), 'unverifiable legacy workflow path')
        lock = directory.parent.parent/'.worker.lock'
        if not _no_live_lock(lock):
            blockers.append('legacy workflow lock is busy: ' + str(lock))
        try:
            row = read_private(directory/'state.json')
        except FileNotFoundError:
            blockers.append('missing legacy workflow state: ' + str(directory))
            continue
        if (row.get('owner_pid') or row.get('active_process') or
                row.get('status') in ('running', 'launching', 'cleanup_blocked', 'delivery_unknown')):
            blockers.append('legacy workflow has unresolved activity')
        if row.get('active_run'):
            try:
                child = read_private(Path(row['active_run'])/'state.json')
            except FileNotFoundError:
                blockers.append('missing legacy workflow stage: ' + str(directory))
                continue
            if child.get('owner_pid') or child.get('active_process'):
                blockers.append('legacy workflow stage has unresolved activity')
    return sorted(set(blockers))


def _unified_blockers(paths):
    from ato_host import Host
    from ato_task_store import TaskError, TaskStore

    store = TaskStore(paths['ato_home'], native_home=paths['codex_home'])
    try:
        with store._private(store.root):
            Host(store=store, read_only=True).compose()
        tasks = _directory_entries(store.tasks)
    except (TaskError, OSError, ValueError):
        return ['unified state could not be verified']
    blockers = []
    for directory in tasks:
        if not re.fullmatch(r'task-[0-9a-f]{32}', directory.name):
            continue
        try:
            task, status, _ = store._read_projection(directory.name)
        except (TaskError, OSError, ValueError):
            blockers.append('unified Task state could not be verified: ' + directory.name)
            continue
        if (task['creation']['phase'] != 'ready' or status['execution']['state'] != 'idle'
                or not status['execution']['verified'] or status['status'] == 'delivery_unknown'):
            blockers.append('unified Task execution or provisioning is unresolved')
        if any(row['phase'] in ('launching', 'active', 'unknown') for row in status['run_history'][-1:]):
            blockers.append('unified Run evidence is unsettled')
    return blockers


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


def inspect(paths, system, target=None, *, allow_interpreter_change=False):
    entries = entrypoints(paths['bin_dir'])
    sources = launcher_sources(entries, allow_interpreter_change=allow_interpreter_change)
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
    observed_count = sum(p['command'] in commands or (
                         'ctask-notify' not in sources and is_legacy_observer(p['command']))
                         for p in system.processes())
    require(len(observers) <= 1 and observed_count <= len(observers),
            'more than one or unrecorded observer at the selected boundary')
    source, observer = observers[0] if observers else (None, None)
    policy = policy_identity(paths)
    if observer and source.get('surface') != SURFACE:
        cfg = read_private(paths['legacy_state']/'control/config.json')
        hub = read_private(paths['legacy_state']/'hub.json')
        require(cfg.get('enabled') is True and cfg.get('hub_thread') == hub.get('thread_id'),
                'legacy observer policy or binding is inconsistent')
    mailboxes = {key: mailbox(paths[key]) for key in ('legacy_state', 'unified_state')}
    activity = active_tasks(paths, system, sources | ({'target': target} if target else {}))
    blockers = []
    selected = sources.get(PUBLIC)
    if (target and selected and not mixed
            and all(selected[key] == target[key] for key in ('root', 'python', 'seal'))):
        blockers.append('snapshot is already selected')
    if observer:
        config = paths['ato_home']/'unified-v1/host/observation.json'
        if 'observation' not in policy or read_private(config).get('enabled') is not True:
            blockers.append('target observer has no authorized configuration')
        elif not mailbox_exists(paths['unified_state']):
            blockers.append('target observer has no selected mailbox')
    return dict(entries=entries, sources=sources, mixed_sources=mixed, observer=observer,
                observer_source=source, mailboxes=mailboxes, policies=policy, activity=activity, blockers=blockers)


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


def _verify_cutover(paths, journal):
    _matches_entries(paths, journal)
    require(policy_identity(paths) == journal['before']['policies'], 'release policies changed')


@contextmanager
def _writer_boundaries(paths):
    with ExitStack() as stack:
        for key in ('legacy_state', 'unified_state'):
            stack.enter_context(mailbox_writer_boundary(paths[key]))
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
            if (not launched or not matching
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


def _restore_preflight(journal, paths, system):
    for source in journal['before']['sources'].values():
        verify_source(source)
    verify_source(journal['target'])
    if journal['state'] != 'rolled_back':
        _verify_cutover(paths, journal)
    observer = _observers(paths, journal, system)
    old = journal['before']['observer']
    restored = journal.get('rollback_observer')
    keep = observer is not None and ((old and same_observer(old, observer))
                                     or (restored and same_observer(restored, observer)
                                         and journal.get('observer_starting') != 'rollback_observer'))
    if journal['state'] == 'rolled_back':
        require(entrypoints(paths['bin_dir']) == journal['before']['entries']
                and (keep if old else observer is None), 'rolled back state changed')
    return observer, old, keep


def restore(path, journal, paths, system, *, preflight=None):
    observer, old, keep = preflight or _restore_preflight(journal, paths, system)
    if journal['state'] == 'rolled_back':
        return observer
    if observer and not keep:
        system.stop(observer)
    with _writer_boundaries(paths):
        current = _observers(paths, journal, system)
        require((current is None and not keep) or (keep and current and same_observer(current, observer)),
                'observer changed at rollback boundary')
        require(policy_identity(paths) == journal['before']['policies'], 'release policies changed')
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


def activate(root, commit, paths, transaction, system=None, *, converge_mixed=False,
             allow_interpreter_change=False):
    system = system or System()
    target = snapshot_source(root)
    require(target['surface'] == SURFACE and target['commit'] == commit, 'wrong cutover snapshot')
    target['python_sha256'] = sha(target['python'])
    with release_lock(paths['bin_dir']):
        before = inspect(paths, system, target, allow_interpreter_change=allow_interpreter_change)
        require(not before['blockers'], '; '.join(before['blockers']))
        require(not before['mixed_sources'] or converge_mixed, 'mixed entries require explicit convergence')
        transaction.mkdir(mode=0o700, parents=False)
        sync_directory(transaction.parent)
        path = transaction/'transaction.json'
        journal = dict(version=2, state='prepared', paths=contexts(paths), before=before,
                       after_entries=entry_set(target), target=target)
        save(path, journal)
        try:
            _verify_cutover(paths, journal)
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
        preflight = _restore_preflight(journal, paths, system)
        try:
            observer = restore(path, journal, paths, system, preflight=preflight)
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
    parser.add_argument('--run-tests', action='store_true', help='Run the full offline suite while staging')
    parser.add_argument('--allow-runtime-change', action='store_true')
    parser.add_argument('--converge-mixed-entrypoints', action='store_true')
    parser.add_argument('--allow-prior-interpreter-change', action='store_true')
    args = parser.parse_args(argv)

    if args.run_tests and not args.stage:
        parser.error('--run-tests requires --stage')

    def need(*names):
        for name in names:
            if getattr(args, name) is None:
                parser.error('--'+name.replace('_', '-')+' is required')

    try:
        if not args.activate and args.converge_mixed_entrypoints:
            parser.error('--converge-mixed-entrypoints requires --activate')
        if args.allow_prior_interpreter_change and not (args.activate or args.dry_run):
            parser.error('--allow-prior-interpreter-change requires --activate or --dry-run')
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
                           wheelhouse=args.wheelhouse, npm_cache=args.npm_cache, run_tests=args.run_tests)
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
                                      converge_mixed=args.converge_mixed_entrypoints,
                                      allow_interpreter_change=args.allow_prior_interpreter_change)
                else:
                    result = rollback(args.transaction, paths)
            else:
                commit, tree = git_commit(args.repo, args.commit)
                target = snapshot_source(args.snapshot) if args.snapshot.exists() else None
                if target:
                    require(target['commit'] == commit and target['surface'] == SURFACE
                            and read(args.snapshot/MANIFEST)['tree'] == tree, 'snapshot differs from selected Git tree')
                result = inspect(paths, System(), target,
                                 allow_interpreter_change=args.allow_prior_interpreter_change)
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
