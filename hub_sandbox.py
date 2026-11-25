"""Read-only launch and owned process cleanup. No adapter or core imports."""
from dataclasses import dataclass, field
import errno
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import time
import tempfile
from urllib.parse import urlsplit

from pi_toolchain import locate

LIMIT = 1024*1024
process_host = ContextVar("hub_process_host", default=None)
trusted_node = ContextVar("hub_trusted_node", default=None)
_ChildProcess = subprocess.Popen


def canonical(path, *, exists=True):
    path=Path(path)
    if not path.is_absolute() or path!=path.resolve(strict=exists):
        raise ValueError('UNSAFE_HUB_PATH')
    return path


def private_dir(path):
    path=canonical(path)
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:
        raise ValueError('PRIVATE_RUNTIME_REQUIRED')
    return path


def validate_loopback_proxy(value):
    if value is None:return None
    if not isinstance(value,str) or not value or any(c.isspace() for c in value):
        raise ValueError('INVALID_LOOPBACK_PROXY')
    parsed=urlsplit(value)
    try:port=parsed.port
    except ValueError:raise ValueError('INVALID_LOOPBACK_PROXY') from None
    if (parsed.scheme not in ('http','https','socks5','socks5h')
            or parsed.hostname not in ('127.0.0.1','::1','localhost')
            or port is None or not (1<=port<=65535)
            or parsed.username is not None or parsed.password is not None or '@' in parsed.netloc
            or parsed.path not in ('','/') or parsed.query or parsed.fragment):
        raise ValueError('INVALID_LOOPBACK_PROXY')
    return value


def clean_environment(home, temporary, *, native_home=None, proxy=None):
    env=dict(HOME=str(canonical(home)),TMPDIR=str(canonical(temporary)),PATH='/usr/bin:/bin',
             LANG='en_US.UTF-8',NO_COLOR='1',NO_BROWSER='1',PYTHONDONTWRITEBYTECODE='1')
    if native_home is not None:env['CODEX_HOME']=str(canonical(native_home,exists=False))
    if proxy is not None:
        url=validate_loopback_proxy(proxy)
        env.update(HTTP_PROXY=url,HTTPS_PROXY=url,http_proxy=url,https_proxy=url,NO_PROXY='127.0.0.1,localhost,::1')
    return env


def native_codex_args(workspace, temporary, *, writable, denied=(), read_only=()):
    """Select one native tool sandbox with exact, host-owned filesystem rules."""
    filesystem = {':root': 'read', str(temporary): 'write'}
    if writable:
        filesystem[str(workspace)] = 'write'
    filesystem.update((str(path), 'read') for path in read_only)
    filesystem.update((str(path), 'deny') for path in denied)
    # Put the complete map in one TOML value. Quoted path segments in dotted
    # CLI overrides are parsed differently across native CLI versions.
    rules = ', '.join(f'{json.dumps(path)}={json.dumps(mode)}'
                      for path, mode in filesystem.items())
    profile = 'permissions.ato-host={filesystem={' + rules + '},network={enabled=false}}'
    return ['-c', 'default_permissions="ato-host"', '-c', profile]


def guarded_codex_args(workspace, temporary, *, writable, denied=(), read_only=()):
    return native_codex_args(workspace, temporary, writable=writable, denied=denied,
        read_only=read_only) + [
        '--ignore-user-config', '--ignore-rules', '--strict-config',
        '-c', 'mcp_servers={}', '-c', 'features.hooks=false', '-c', 'features.apps=false',
        '-c', 'features.multi_agent=false', '-c', 'features.plugins=false',
        '-c', 'features.shell_snapshot=false', '-c', 'web_search="disabled"',
        '-c', 'projects={' + json.dumps(str(workspace)) + '={trust_level="untrusted"}}',
    ]


def secrets(environment):
    return tuple(str(value) for key,value in environment.items() if value and len(str(value))>=8
        and re.search(r'(secret|token|password|credential|api.?key|actor)',key,re.I))


def has_secret(value, private_values):
    encoded=json.dumps(value,ensure_ascii=False,default=str)
    return any(secret in encoded for secret in private_values)


def read_denials(runtime, protected):
    q = lambda path: json.dumps(str(path))
    rules = []
    for path in protected:
        root = Path(path).resolve()
        rules.append('(deny file-read* (require-all (subpath ' + q(root)
                     + ') (require-not (subpath ' + q(runtime) + '))))')
        if runtime.is_relative_to(root):
            rules.extend('(allow file-read-metadata (literal ' + q(parent) + '))'
                         for parent in runtime.parents if parent.is_relative_to(root))
    return rules


def readonly_profile(runtime, *, protected, writable_native=(), protected_reads=()):
    runtime=private_dir(runtime)
    roots=tuple(canonical(p) for p in protected)
    if any(runtime==p or runtime.is_relative_to(p) or p.is_relative_to(runtime) for p in roots):
        raise ValueError('RUNTIME_OVERLAPS_REVIEW_SCOPE')
    q=lambda p:json.dumps(str(p))
    rules=['(version 1)','(allow default)','(deny file-write*)',
        '(allow file-write* (literal "/dev/null"))',
        '(allow file-write* (subpath '+q(runtime/'tmp')+') (literal '+q(runtime/'output.json')+'))']
    for raw in writable_native:
        path=canonical(raw,exists=False)
        if any(path==p or path.is_relative_to(p) or p.is_relative_to(path) for p in (*roots,runtime)):
            raise ValueError('NATIVE_STATE_OVERLAPS_AUTHORITY')
        rules.append('(allow file-write* (subpath '+q(path)+'))')
    # Explicit denies prevail even if a future allow-rule is broadened.
    for path in (*roots,runtime/'trusted',runtime/'adapter'):
        rules.append('(deny file-write* (subpath '+q(path)+'))')
    rules.extend(read_denials(runtime, protected_reads))
    return '\n'.join(rules)+'\n'


def identity(pid):
    try:
        data=subprocess.check_output(['/bin/ps','-p',str(pid),'-o','lstart='],timeout=2,
            env={'PATH':'/usr/bin:/bin','LC_ALL':'C','TZ':'UTC'},stderr=subprocess.DEVNULL)
        return hashlib.sha256(data.strip()).hexdigest() if data.strip() else None
    except (OSError,subprocess.SubprocessError):return None


def group_members(group):
    try:
        data=subprocess.check_output(['/bin/ps','-axo','pid=,pgid=,stat='],text=True,timeout=2,
            env={'PATH':'/usr/bin:/bin','LC_ALL':'C','TZ':'UTC'},stderr=subprocess.DEVNULL)
        return {int(pid):birth for pid,pgid,state in (row.split() for row in data.splitlines())
                if int(pgid)==group and not state.startswith('Z') and (birth:=identity(int(pid)))}
    except (OSError,ValueError,subprocess.SubprocessError):return None


@dataclass
class OwnedGroup:
    process: object
    leader_identity: str | None
    known: dict = field(default_factory=dict)
    members: object = None

    def observe(self):
        rows=(self.members or group_members)(self.process.pid)
        if rows is None:return None,False
        if self.process.pid in rows and rows[self.process.pid]!=self.leader_identity:return rows,False
        trusted=any(value and self.known.get(pid)==value for pid,value in rows.items())
        # An unreaped session leader's PID still anchors its process group.
        if (not trusted and isinstance(self.process, _ChildProcess)
                and self.process.returncode is None and self.process.pid not in rows):
            trusted = True
        if trusted:self.known.update({pid:value for pid,value in rows.items() if value})
        return rows,trusted

    def close(self):
        for sig in (signal.SIGTERM,signal.SIGKILL):
            rows,trusted=self.observe()
            if rows is None:return False
            if not rows:return True
            if not trusted:return False
            try:os.killpg(self.process.pid,sig)
            except ProcessLookupError:return True
            except PermissionError:return False
            until=time.monotonic()+2
            while time.monotonic()<until:
                rows,trusted=self.observe()
                if rows is None:return False
                if not rows:return True
                if not trusted:return False
                time.sleep(.04)
        return False


@dataclass(frozen=True)
class ProcessResult:
    code: str
    returncode: int | None
    stdout: bytes = field(repr=False,default=b'')
    stderr: bytes = field(repr=False,default=b'')
    cleaned: bool = True
    truncated: bool = False


def run(command, *, workspace, runtime, environment, prompt='', timeout=300, stop=lambda:False,
        truncate=False):
    """Bounded pipes, private bounded stderr, and no adopted process identities."""
    runtime=private_dir(runtime);workspace=canonical(workspace)
    if type(timeout) not in (int,float) or timeout<=0:raise ValueError('INVALID_TURN_TIMEOUT')
    stdin=runtime/'stdin.txt';error=runtime/'stderr.log'
    for path in (stdin,error):
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:
            if path==stdin:stream.write(prompt.encode())
    output,errors=bytearray(),bytearray();code='PROCESS_ERROR';process=None;owner=None;clean=True
    truncated=False;exited=False;selector=None
    host=process_host.get()
    try:
        if stop():return ProcessResult('STOPPED',None)
        with stdin.open('rb') as source,error.open('ab') as log:
            deadline=time.monotonic()+timeout
            if host is not None:
                host.before_spawn(runtime)
            try:
                process=subprocess.Popen(list(command),cwd=workspace,env=environment,stdin=source,
                    stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,umask=0o077)
            except OSError as exc:
                if exc.errno != errno.E2BIG:
                    raise
                if host is not None:
                    host.finished(runtime, None, True)
                return ProcessResult('ARGUMENT_LIST_TOO_LONG', None)
            if host is None:
                birth=identity(process.pid)
                owner=OwnedGroup(process,birth,{process.pid:birth} if birth else {})
            else:
                try:
                    owner=host.own(process, runtime)
                except Exception:
                    birth=identity(process.pid)
                    owner=OwnedGroup(process,birth,{process.pid:birth} if birth else {})
                    raise
            selector=selectors.DefaultSelector()
            for stream in (process.stdout,process.stderr):
                os.set_blocking(stream.fileno(),False);selector.register(stream,selectors.EVENT_READ)
            next_observation = 0
            while True:
                if stop():code='STOPPED';break
                if time.monotonic()>=deadline:code='TIMEOUT';break
                for key,_ in selector.select(timeout=min(.1,max(.001,deadline-time.monotonic()))):
                    chunk=os.read(key.fd,65536)
                    if not chunk:selector.unregister(key.fileobj);continue
                    target=output if key.fileobj is process.stdout else errors
                    remaining=LIMIT-len(target)
                    target.extend(chunk[:max(0,remaining)])
                    if key.fileobj is process.stderr:log.write(chunk[:max(0,remaining)])
                    if len(chunk)>remaining:
                        truncated=True
                        if not truncate:code='OUTPUT_LIMIT';break
                if code=='OUTPUT_LIMIT':break
                if time.monotonic() >= next_observation:
                    rows,_ = owner.observe()
                    next_observation = time.monotonic() + .1
                    if rows is not None and process.pid not in rows:
                        if not selector.get_map():
                            exited=True
                            break
                        if rows:
                            code='PIPE_EOF_UNKNOWN'
                            break
    except Exception:
        code='PROCESS_ERROR'
    finally:
        if selector is not None:
            selector.close()
        if process is not None:
            clean=owner is not None and owner.close()
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                clean=False
            finally:
                process.stdout.close();process.stderr.close()
            if exited and clean and process.returncode == 0:
                code='OK'
    if host is not None:
        host.finished(runtime, process, clean)
    return ProcessResult(code if clean else 'CLEANUP_BLOCKED',process.returncode if process else None,
                         bytes(output),bytes(errors),clean,truncated)


def checked_turn(request, invoke):
    """Trusted request supplies hub_worktree.snapshot_integrity through a callback.

    This keeps the sandbox leaf independent of worktree/adapter modules. A
    missing/failed snapshot is a refusal, including on exceptional turn exits.
    """
    before=request.snapshot()
    try:
        return invoke()
    finally:
        try:after=request.snapshot()
        except Exception:raise ValueError('REVIEWER_MUTATION_VIOLATION') from None
        if after!=before:raise ValueError('REVIEWER_MUTATION_VIOLATION')


def executable(kind, selected=None):
    """Host-only selection seam; task specs never contain executable fields."""
    value=selected if selected is not None else shutil.which(kind)
    if value is None:return None
    path=Path(value)
    if not path.is_absolute():raise ValueError('EXECUTABLE_UNAVAILABLE')
    path=path.resolve(strict=True)
    if not path.is_file() or not os.access(path,os.X_OK):raise ValueError('EXECUTABLE_UNAVAILABLE')
    return path


def tool_environment(home, temporary, tool, *, codex=False, proxy=None):
    env=clean_environment(home,temporary,native_home=Path(home)/'.codex' if codex else None,proxy=proxy)
    node = trusted_node.get()
    if node is None and process_host.get() is None:
        try:
            node = locate('node')
        except (OSError, ValueError):
            pass
    env['PATH'] = tool_path(tool, node)
    return env


def tool_path(tool, node):
    paths=[str(Path(tool).parent),'/usr/bin','/bin']
    if node:paths.insert(1,str(Path(node).resolve().parent))
    return os.pathsep.join(dict.fromkeys(paths))


def write_private(path, data):
    fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:stream.write(data)


def read_output(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>LIMIT:
            raise ValueError('INVALID_PROTOCOL')
        return stream.read(LIMIT+1)


def turn_context(request, tool, home, *, outer=True, writable_native=(), codex=False, proxy=None):
    if tool is None:raise ValueError('EXECUTABLE_UNAVAILABLE')
    workspace=canonical(request.workspace);rt=private_dir(request.runtime_dir);home=canonical(home)
    if rt.is_relative_to(workspace) or tool.is_relative_to(workspace):raise ValueError('INVALID_PROTOCOL')
    temporary=rt/'tmp';temporary.mkdir(mode=0o700)
    env=tool_environment(home,temporary,tool,codex=codex,proxy=proxy)
    prefix=[]
    if outer:
        from_root=Path(__file__).resolve().parent
        protected={workspace,from_root}
        # A worktree's Git metadata and common directory belong to its source
        # repository. Protect those exact roots without granting a write path.
        result=subprocess.run(['git','-C',str(workspace),'rev-parse','--git-common-dir'],
            capture_output=True,text=True,timeout=5,env=dict(PATH='/usr/bin:/bin',LANG='C',GIT_OPTIONAL_LOCKS='0'))
        if result.returncode:raise ValueError('INVALID_PROTOCOL')
        common=Path(result.stdout.strip());common=(workspace/common).resolve() if not common.is_absolute() else common.resolve()
        protected.add(common)
        policy=readonly_profile(rt,protected=protected,writable_native=writable_native,
                                protected_reads=request.protected_reads)
        write_private(rt/'readonly.sb',policy);prefix=['/usr/bin/sandbox-exec','-f',str(rt/'readonly.sb')]
    return rt,env,prefix


def doctor(kind, tool, expected, *, protected_reads=()):
    if tool is None:return dict(kind=kind,available=False,version=None,code='EXECUTABLE_UNAVAILABLE')
    with tempfile.TemporaryDirectory(prefix='h1-doctor-') as folder:
        rt=Path(folder).resolve();rt.chmod(0o700);home=rt/'home';home.mkdir(mode=0o700);temporary=rt/'tmp';temporary.mkdir(mode=0o700)
        profile=readonly_profile(rt,protected=(),protected_reads=protected_reads) + '(deny network*)\n'
        result=run(['/usr/bin/sandbox-exec','-p',profile,str(tool),'--version'],workspace=rt,runtime=rt,
            environment=tool_environment(home,temporary,tool),timeout=3)
        found = re.search(rb'(?<![0-9.])[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?(?![0-9.])',
                          result.stdout[:4096])
        version = found.group().decode('ascii') if result.code == 'OK' and found else None
        good = version is not None and (expected is None or expected == version)
        return dict(kind=kind,available=good,version=version,
                    code='OK' if good else result.code if result.code!='OK' else 'VERSION_MISMATCH')
