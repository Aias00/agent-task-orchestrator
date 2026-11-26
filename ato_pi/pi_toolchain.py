"""Host-owned Pi tool selection and private launch evidence; standard library only.

Importing this module probes nothing. Configuration comes only from the host,
never a task, model, project file, or alternate registry selected by environment.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT/'integrations/pi-acp/compatibility.json'
SOURCE = ROOT/'integrations/pi-acp/node_modules/pi-acp/dist/index.js'
SAFE_PATH = '/usr/bin:/bin'
PROBE_SECONDS = 10
PROBE_BYTES = 8192


class ToolchainError(ValueError):
    """Safe diagnostics contain no probe output, protocol data, or credentials."""


def registry():
    data = json.loads(REGISTRY.read_text())
    if data.get('schema') != 1 or data.get('node_min_major') != 24:
        raise ToolchainError('unsupported toolchain registry')
    return data


def file_sha256(path, limit=256*1024*1024):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ToolchainError('invalid toolchain file')
        digest = hashlib.sha256()
        total = 0
        while chunk := stream.read(1024*1024):
            total += len(chunk)
            if total > limit:
                raise ToolchainError('toolchain file exceeds bound')
            digest.update(chunk)
        return digest.hexdigest()


def package_sha256(root):
    """Hash the release's own files, not separately installed dependencies.

    npm can hoist/nest external dependencies under node_modules. Their integrity
    remains a separate lock/installation boundary, not a claim of this digest.
    """
    files = {}
    total = 0
    for directory, folders, names in os.walk(root, followlinks=False):
        if Path(directory) == root:
            folders[:] = [name for name in folders if name != 'node_modules']
        for name in folders + names:
            path = Path(directory)/name
            info = path.stat(follow_symlinks=False)
            if (stat.S_ISLNK(info.st_mode) or info.st_mode & 0o022
                    or info.st_uid not in (0, os.getuid())):
                raise ToolchainError('unsafe Pi package contents')
        for name in names:
            path = Path(directory)/name
            total += path.stat().st_size
            if total > 64*1024*1024 or len(files) >= 4096:
                raise ToolchainError('Pi package exceeds bound')
            files[path.relative_to(root).as_posix()] = file_sha256(path, 32*1024*1024)
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _inside(path, roots):
    return any(path.is_relative_to(root) for root in roots)


def trusted_path(path, *, forbidden=(), executable=False):
    path = Path(path)
    if not path.is_absolute():
        raise ToolchainError('tool path must be absolute')
    forbidden = tuple(Path(p).resolve() for p in forbidden)
    resolved = path.resolve(strict=True)
    # Check aliases as well as targets, including an alias through a workspace
    # that subsequently escapes through .. or a second symlink.
    for candidate in (path, resolved):
        for part in (candidate, *candidate.parents):
            if _inside(part.resolve(), forbidden):
                raise ToolchainError('tool path enters workspace or runtime')
            info = part.lstat()
            if info.st_uid not in (0, os.getuid()):
                raise ToolchainError('tool path has untrusted owner')
            sticky_root = (stat.S_ISDIR(info.st_mode) and info.st_uid == 0
                           and info.st_mode & stat.S_ISVTX)
            if not stat.S_ISLNK(info.st_mode) and info.st_mode & 0o022 and not sticky_root:
                raise ToolchainError('tool path is writable by others')
    if not resolved.is_file() or (executable and not os.access(resolved, os.X_OK)):
        raise ToolchainError('tool executable unavailable')
    return resolved


def locate(kind, *, environment=None, forbidden=()):
    environment = os.environ if environment is None else environment
    explicit = environment.get('CTASK_'+kind.upper()+'_BIN')
    if explicit is not None:
        if not explicit:
            raise ToolchainError('empty explicit tool path')
        return trusted_path(explicit, forbidden=forbidden, executable=True)
    for item in environment.get('PATH', '').split(os.pathsep):
        directory = Path(item)
        if not item or not directory.is_absolute():
            continue
        candidate = directory/kind
        # Unsafe search entries are ignored. Once a trusted existing candidate
        # is selected, a version/hash failure does not trigger further search.
        try:
            found = trusted_path(candidate, forbidden=forbidden, executable=True)
        except (OSError, ValueError, RuntimeError):
            continue
        return found
    if kind == 'pi':
        return trusted_path(Path.home()/'.local/bin/pi', forbidden=forbidden, executable=True)
    raise ToolchainError('trusted '+kind+' executable unavailable')


class ExecutableLocator:
    """Lazy path-only facade retaining the historic exports' host locators.

    Native launch resolution separately reads the current trusted host inputs;
    this snapshot only keeps old callers usable under an emptied child/test env.
    """
    def __init__(self, kind):
        self.kind = kind
        self._environment = {key: os.environ[key] for key in ('PATH', 'CTASK_NODE_BIN', 'CTASK_PI_BIN')
                             if key in os.environ}

    def resolve(self):
        return locate(self.kind, environment=self._environment)

    def is_file(self):
        try:
            return self.resolve().is_file()
        except (OSError, ValueError):
            return False

    @property
    def parent(self):
        return self.resolve().parent

    def __str__(self):
        return str(self.resolve())

    def __fspath__(self):
        return str(self)


def clean_environment(*, home):
    # ACP's pinned SDK additionally inherits HOME/LOGNAME/PATH/SHELL/TERM/USER.
    # Override all those keys so neither PATH nor a shell function leaks in.
    # NODE_OPTIONS/NODE_PATH/LD_*/DYLD_* are deliberately absent, not blanked
    # (an empty dynamic-library search component can mean the current directory).
    return dict(HOME=str(home), PATH=SAFE_PATH, LOGNAME='', USER='', SHELL='/bin/sh', TERM='dumb')


def _probe(command, *, home):
    """Bound both streams while running a finite version-only subprocess."""
    output = bytearray()
    proc = subprocess.Popen(command, cwd=home, env=clean_environment(home=home),
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True)
    try:
        with selectors.DefaultSelector() as ready:
            for pipe in (proc.stdout, proc.stderr):
                os.set_blocking(pipe.fileno(), False)
                ready.register(pipe, selectors.EVENT_READ)
            deadline = time.monotonic()+PROBE_SECONDS
            total = 0
            while ready.get_map():
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise ToolchainError('tool version probe timed out')
                for key, _ in ready.select(min(remaining, .1)):
                    chunk = os.read(key.fileobj.fileno(), PROBE_BYTES+1)
                    if not chunk:
                        ready.unregister(key.fileobj)
                        continue
                    total += len(chunk)
                    if total > PROBE_BYTES:
                        raise ToolchainError('tool version probe exceeds output bound')
                    if key.fileobj is proc.stdout:
                        output.extend(chunk)
            proc.wait(timeout=max(.001, deadline-time.monotonic()))
        if proc.returncode:
            raise ToolchainError('tool version probe failed')
        return output.decode('ascii').strip()
    except (OSError, UnicodeError, subprocess.TimeoutExpired) as exc:
        raise ToolchainError('tool version probe failed') from None
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        proc.stdout.close()
        proc.stderr.close()


def _node_version(version):
    match = re.fullmatch(r'v(0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)', version)
    if not match or int(match[1]) < 24:
        raise ToolchainError('stable Node >= 24 required')


def _pi_release(pi, data, forbidden):
    for root in pi.parents:
        package = root/'package.json'
        if package.is_file():
            trusted_path(package, forbidden=forbidden)
            if package.stat().st_size > 1024*1024:
                raise ToolchainError('invalid Pi package metadata')
            metadata = json.loads(package.read_text())
            break
    else:
        raise ToolchainError('reviewed Pi package required')
    for release in data['pi_releases']:
        if metadata.get('name') == release['name'] and metadata.get('version') == release['version']:
            if (pi != root/release['entry'] or file_sha256(pi) != release['entry_sha256']
                    or package_sha256(root) != release['package_sha256']):
                raise ToolchainError('Pi package differs from reviewed release')
            return root, release
    raise ToolchainError('Pi release is not approved')


@dataclass(frozen=True)
class Toolchain:
    node: Path
    pi: Path
    package_root: Path
    node_version: str
    node_sha256: str
    pi_release: str
    pi_version: str
    pi_sha256: str
    package_sha256: str

    def mapping(self):
        return {name: str(value) for name, value in asdict(self).items()}


def _host_sources(project, runtime):
    forbidden = (project, runtime)
    code = ('ato_pi/__init__.py', 'ato_pi/pi_toolchain.py', 'ato_pi/pi_acp_compat.py',
            'ato_pi/pi_acp_exec.py', 'ato_pi/pi_coordinator_exec.py', 'pi-path-guard.mjs',
            'pi-coordinator-guard.mjs')
    for path in (*(ROOT/name for name in code), REGISTRY, SOURCE):
        trusted_path(path, forbidden=forbidden)
    return forbidden


def prepare_toolchain(project, runtime, *, node=None, pi=None, environment=None, probe=None):
    project, runtime = Path(project).resolve(), Path(runtime).resolve()
    forbidden = _host_sources(project, runtime)
    data = registry()
    def select(kind, hint):
        if hint is None or isinstance(hint, ExecutableLocator):
            return locate(kind, environment=environment, forbidden=forbidden)
        return trusted_path(hint, forbidden=forbidden, executable=True)
    node, pi = select('node', node), select('pi', pi)
    root, release = _pi_release(pi, data, forbidden)
    probe = _probe if probe is None else probe
    with tempfile.TemporaryDirectory(prefix='ato-pi-version-') as folder:
        home = Path(folder)
        version = probe([str(node), '--version'], home=home)
        _node_version(version)
        pi_version = probe([str(node), str(pi), '--offline', '--no-extensions', '--no-skills',
            '--no-mcp', '--no-context-files', '--version'], home=home)
        if pi_version != release['version']:
            raise ToolchainError('Pi version differs from reviewed release')
    return Toolchain(node, pi, root, version, file_sha256(node), release['id'], pi_version,
                     release['entry_sha256'], release['package_sha256'])


def _private_directory(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise ToolchainError('private toolchain directory required')


def _write_new(path, content, mode=0o600):
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


def write_launch(runtime, project, role, toolchain, *, production=False):
    """Called by trusted host code only, after resolution and derivation."""
    if role not in ('worker', 'coordinator'):
        raise ToolchainError('unknown Pi role')
    runtime, project = Path(runtime).resolve(), Path(project).resolve()
    _private_directory(runtime)
    adapter = runtime/'adapter/pi-acp.mjs'
    expected = registry()['adapter']['derived_sha256'][role]
    if file_sha256(adapter) != expected:
        raise ToolchainError('derived Pi adapter differs')
    trusted = runtime/'trusted'
    trusted.mkdir(mode=0o700)
    record = dict(schema=1, role=role, runtime=str(runtime), project=str(project),
        registry_sha256=file_sha256(REGISTRY), toolchain=toolchain.mapping(), adapter_sha256=expected)
    if production:
        if role != 'coordinator':
            raise ToolchainError('production is a separate coordinator profile')
        record.update(profile_id='production-pi-v1', production_sources=production_sources(project, runtime))
    temporary = trusted/'toolchain.json.tmp'
    _write_new(temporary, json.dumps(record, sort_keys=True)+'\n')
    os.rename(temporary, trusted/'toolchain.json')  # New private directory, never an old runtime.
    script = ROOT/'ato_pi'/('pi_acp_exec.py' if role == 'worker' else 'pi_coordinator_exec.py')
    if production:
        script = ROOT/'ato_pi/pi_production_exec.py'
    launcher = trusted/'pi-launcher'
    _write_new(launcher, '#!/bin/sh\nexec '+shlex.quote(sys.executable)+' -E -s '
               +shlex.quote(str(script))+' "$@"\n', 0o700)
    return launcher


def load_launch(runtime, project, role):
    runtime, project = Path(runtime).resolve(strict=True), Path(project).resolve(strict=True)
    _host_sources(project, runtime)
    for directory in (runtime, runtime/'trusted', runtime/'adapter'):
        _private_directory(directory)
    path = runtime/'trusted/toolchain.json'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384):
            raise ToolchainError('invalid private toolchain description')
        record = json.load(stream)
    if (record.get('schema') != 1 or record.get('role') != role
            or record.get('runtime') != str(runtime) or record.get('project') != str(project)
            or record.get('registry_sha256') != file_sha256(REGISTRY)):
        raise ToolchainError('toolchain description binding differs')
    if record.get('profile_id') is not None:
        if (role != 'coordinator' or record['profile_id'] != 'production-pi-v1'
                or record.get('production_sources') != production_sources(project, runtime)):
            raise ToolchainError('production launcher sources changed')
    values = record['toolchain']
    tools = Toolchain(**{name: Path(value) if name in ('node', 'pi', 'package_root') else value
                        for name, value in values.items()})
    forbidden = (project, runtime)
    trusted_path(tools.node, forbidden=forbidden, executable=True)
    trusted_path(tools.pi, forbidden=forbidden, executable=True)
    root, release = _pi_release(tools.pi, registry(), forbidden)
    _node_version(tools.node_version)
    if (root != tools.package_root or release['id'] != tools.pi_release
            or release['version'] != tools.pi_version or release['entry_sha256'] != tools.pi_sha256
            or release['package_sha256'] != tools.package_sha256
            or file_sha256(tools.node) != tools.node_sha256
            or record['adapter_sha256'] != registry()['adapter']['derived_sha256'][role]
            or file_sha256(runtime/'adapter/pi-acp.mjs') != record['adapter_sha256']):
        raise ToolchainError('toolchain changed since host verification')
    return tools


def write_exclusions(runtime):
    """Exclude protected materials AND replacement of the runtime directory."""
    quote = lambda path: json.dumps(str(Path(path).resolve()))
    return ('(require-not (literal '+quote(runtime)+')) '
            +' '.join('(require-not (subpath '+quote(Path(runtime)/name)+'))'
                      for name in ('trusted', 'adapter')))


def native_arguments(runtime, args):
    if args[:3] != ['--mode', 'rpc', '--no-themes']:
        raise ToolchainError('unsupported native invocation')
    if len(args) != 3:
        if len(args) != 5 or args[3] != '--session':
            raise ToolchainError('unsupported native arguments')
        path = Path(args[4]).resolve(strict=True)
        if not path.is_relative_to(runtime/'sessions'):
            raise ToolchainError('session file outside owned runtime')
    return args


def native_environment():
    """The fixed launcher forwards locators, never arbitrary host environment."""
    env = clean_environment(home=os.environ['HOME'])
    for key in ('CTASK_AGENT_REPO', 'CTASK_PI_RUNTIME', 'CTASK_AGENT_SANDBOX',
                'CTASK_PI_MODEL', 'PI_CODING_AGENT_DIR', 'TMPDIR', 'JITI_CACHE'):
        if key in os.environ:
            env[key] = os.environ[key]
    env['PI_TELEMETRY'] = '0'
    return env


def production_sources(project, runtime):
    sources = ('ato_pi/pi_production_exec.py', 'pi_production_transport.mjs',
               'ato_pi/pi_production_profile.py', 'ato_pi/pi_production_credentials.py',
               'ato_pi/pi_production_fence.py', 'ato_pi/pi_production_egress.py', 'pi-coordinator-guard.mjs')
    return {Path(name).name: file_sha256(trusted_path(ROOT/name, forbidden=(project, runtime)))
            for name in sources}


def production_sandbox(runtime, project, node, *, sources=ROOT, proxy_port, control_socket=None,
                       sandbox='read-only', workflow=None, pi_root=None, pi_entry=None):
    """Default deny. This policy is separate from the unchanged Local profile."""
    if sandbox not in ('read-only', 'workspace-write') or type(proxy_port) is not int or not 1 <= proxy_port <= 65535:
        raise ToolchainError('invalid production sandbox')
    q = lambda p: json.dumps(str(Path(p).resolve()))
    runtime, project, sources = Path(runtime).resolve(), Path(project).resolve(), Path(sources).resolve()
    if runtime.is_relative_to(project) or sources.is_relative_to(project):
        raise ToolchainError('production trusted roots enter workspace')
    exclusions = ' '.join('(require-not (subpath '+q(project/name)+'))'
                         for name in ('.git','.codex','.ato','.pi','.aws','.ssh','.env'))
    reads = [runtime, sources, Path(node).resolve().parent.parent,
             Path(sys.base_prefix), Path('/System'), Path('/usr/lib'), Path('/usr/share'),
             Path('/private/etc'), Path('/dev')]
    if pi_root is not None:
        pi_root = Path(pi_root).resolve(strict=True)
        if pi_root.is_relative_to(project) or pi_root.is_relative_to(runtime):
            raise ToolchainError('production Pi package enters workspace or runtime')
        reads.append(pi_root)
    # macOS /bin/sh dispatches to the system /bin/bash variant. Both are fixed
    # launcher dependencies, not exposed Shell tools or a PATH search.
    executables = [node, sys.executable, '/bin/sh', '/bin/bash', '/usr/bin/sandbox-exec', runtime/'trusted/pi-launcher']
    if pi_entry is not None:
        pi_entry = Path(pi_entry).resolve(strict=True)
        if pi_root is None or not pi_entry.is_relative_to(pi_root):
            raise ToolchainError('production Pi entry leaves reviewed package')
        # Seatbelt also mediates access(X_OK); load_launch rechecks the exact
        # pinned Pi entry even though Node reads it as a script.
        executables.append(pi_entry)
    rules = ['(version 1)', '(deny default)', '(allow file-read-metadata)',
             '(allow file-read-data (literal "/"))',  # dyld bootstrap reads only the root directory.
             '(allow file-read* (literal "/bin/sh") (literal "/bin/bash"))',
             '(allow sysctl-read)', '(allow process-fork)',
             '(allow process-exec '+ ' '.join('(literal '+q(p)+')' for p in executables) +')',
             '(allow file-read* '+ ' '.join('(subpath '+q(p)+')' for p in reads)+')',
             '(allow file-read* (require-all (subpath '+q(project)+') '+exclusions+'))',
             '(allow file-write* (literal "/dev/null") (require-all (subpath '+q(runtime)+') '+write_exclusions(runtime)+'))',
             '(allow network-outbound (remote ip "localhost:'+str(proxy_port)+'"))']
    rules.extend(['(deny file-read* (regex #".*/[.]env([.][^/]+)?(/.*)?$"))',
                  '(deny file-write* (regex #".*/[.]env([.][^/]+)?(/.*)?$"))'])
    if workflow:
        rules.append('(allow file-read* (subpath '+q(workflow)+'))')
    if control_socket:
        rules.append('(allow network-outbound (remote unix-socket (literal '+q(control_socket)+')))')
    if sandbox == 'workspace-write':
        rules.append('(allow file-write* (require-all (subpath '+q(project)+') '+exclusions+' '+write_exclusions(runtime)+'))')
    return '\n'.join(rules)
