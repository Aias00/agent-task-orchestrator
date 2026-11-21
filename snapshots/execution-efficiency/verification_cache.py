"""Opt-in host verification receipts, never worker completion authority.

v1 deliberately qualifies a small offline Python surface. An explicit input
declaration is necessary, not sufficient: unknown tools, imports, dynamic paths,
startup hooks and external state run unchanged and uncached. Receipts live
outside worker grants. Final/full and host/manual acceptance always run fresh.
"""
import ast
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import site
import stat
import sys
import sysconfig
import time

import ato_config
from transport import _private_save

MAX_AGE_SECONDS = 3600
MAX_FILES = 8192
MAX_BYTES = 128 * 1024 * 1024
MAX_RECEIPT_BYTES = 4 * 1024 * 1024
SAFE_IMPORTS = frozenset({'pathlib', 'json', 'hashlib', 're', 'math', 'collections',
    'itertools', 'functools', 'operator', 'statistics', 'decimal', 'fractions',
    'struct', 'string', 'enum', 'bisect', 'heapq',
    'array', 'base64', 'binascii', 'copy', 'difflib', 'textwrap'})
READS = {'read_text', 'read_bytes', 'exists', 'is_file', 'is_dir', 'stat',
         'lstat', 'iterdir', 'glob', 'rglob', 'open'}
WRITES = {'write_text', 'write_bytes', 'touch'}
UNSAFE_CALLS = {'eval', 'exec', 'compile', '__import__', 'getattr', 'setattr', 'hasattr', 'open',
                'globals', 'locals', 'vars', 'input', 'breakpoint', 'help'}
UNSAFE_ATTRIBUTES = {'os', 'sys', 'io', 'socket', 'subprocess', 'time', 'random',
                     'environ', 'system', 'popen', 'connect', 'urlopen', 'import_module',
                     'cwd', 'home', 'owner', 'group', 'is_mount', 'readlink', 'expanduser'}


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mode, info.st_mtime_ns, info.st_ctime_ns)


def validate(policy, ids):
    if (not isinstance(policy, dict) or set(policy) != {'version', 'checks'}
            or type(policy['version']) is not int or policy['version'] != 1
            or not isinstance(policy['checks'], dict) or set(policy['checks']) - set(ids)):
        raise ValueError('verification_policy requires version=1 and checks keyed by actual task/criterion IDs')
    for entry in policy['checks'].values():
        if (not isinstance(entry, dict) or set(entry) != {'inputs', 'env', 'cacheable'}
                or type(entry['cacheable']) is not bool
                or not isinstance(entry['inputs'], list) or not isinstance(entry['env'], list)):
            raise ValueError('verification check requires inputs, env and boolean cacheable')
        for name in entry['inputs']:
            if (not isinstance(name, str) or not name or Path(name).is_absolute()
                    or '..' in Path(name).parts or '\x00' in name):
                raise ValueError('verification inputs must be relative, confined paths')
        if any(not isinstance(n, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', n) for n in entry['env']):
            raise ValueError('verification env must contain variable names only')


def stage_policy(contract, plan):
    """Project only trusted declarations onto exact stage task IDs."""
    if 'verification_policy' not in contract:
        return None
    policy = contract['verification_policy']
    validate(policy, policy.get('checks', {}) if isinstance(policy, dict) else ())
    entries = policy['checks']
    return dict(version=1, checks={t['id']: entries[t['id']] for t in plan['tasks'] if t['id'] in entries})


def _file(path, limit=MAX_BYTES, private=False):
    """No-follow traversal and bounded consistent reads, including ancestors."""
    path = Path(path).absolute()
    parent = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent); parent = child
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit
                or private and (before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600)):
            raise ValueError('verification file type, size, links or authority unknown')
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        if len(data) > limit or _stamp(before) != _stamp(after):
            raise ValueError('verification file changed while fingerprinting')
        return dict(sha256=hashlib.sha256(data).hexdigest(), mode=stat.S_IMODE(before.st_mode), size=len(data)), data


def _tree(path, *, toolchain=False):
    """Include directory membership and modes, not just existing file bytes."""
    path = Path(path).absolute()
    result = {}; total = 0

    def visit(p):
        nonlocal total
        if len(result) >= MAX_FILES:
            raise ValueError('verification inventory bound exceeded')
        info = p.lstat()
        key = str(p.relative_to(path))
        if stat.S_ISDIR(info.st_mode):
            # Verify all ancestors too, without following directory links.
            fd = os.open(p.anchor, os.O_RDONLY | os.O_DIRECTORY)
            try:
                for part in p.parts[1:]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd); fd = child
                if _stamp(os.fstat(fd)) != _stamp(info):
                    raise ValueError('verification directory replaced during inventory')
                names = []
                with os.scandir(fd) as children:
                    for entry in children:
                        if toolchain and entry.name == 'site-packages':
                            continue
                        if len(result) + len(names) >= MAX_FILES:
                            raise ValueError('verification directory inventory bound exceeded')
                        names.append(entry.name)
                names.sort()
                result[key] = dict(mode=stat.S_IMODE(info.st_mode), children=names)
                for name in names:
                    visit(p/name)
                if _stamp(p.lstat()) != _stamp(info) or _stamp(os.fstat(fd)) != _stamp(info):
                    raise ValueError('verification directory changed during inventory')
            finally:
                os.close(fd)
        else:
            proof, _ = _file(p)
            total += proof['size']
            if total > MAX_BYTES:
                raise ValueError('verification byte bound exceeded')
            result[key] = proof
    # File reads verify ancestors; for empty dirs explicitly reject ancestors.
    for p in (path, *path.parents):
        if p.is_symlink():
            raise ValueError('verification links are unsupported')
    visit(path)
    return result


def _identity(r):
    return dict(directory=str(r.dir), project=str(r.repo.resolve()),
                contract=r.state.get('contract_hash') or r.state['plan_hash'])


def _root(r, create=False):
    root = ato_config.ato_home()/'verification-cache-v1'/_hash(str(r.dir))
    grants = [r.repo, *r.plan.get('permissions', {}).get('writable_roots', [])]
    if r.plan.get('sandbox') == 'danger-full-access' or any(
            root.resolve().is_relative_to(Path(p).resolve()) for p in grants):
        raise ValueError('verification receipt is inside worker authority')
    if create:
        root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.mkdir(mode=0o700)
    for path in (root.parent, root):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('verification private storage authority changed')
        if any(p.is_symlink() for p in path.parents):
            raise ValueError('verification private storage links refused')
    return root


def initialize(directory):
    """Creation-time witness; a missing/invalid witness is never recreated."""
    import runner
    from types import SimpleNamespace
    directory = Path(directory).resolve()
    state = runner.read(directory/'state.json')
    plan = runner.read(directory/('contract.json' if state.get('kind') == 'workflow' else 'plan.json'))
    if 'verification_policy' not in plan:
        return
    r = SimpleNamespace(dir=directory, repo=Path(state['repo']), state=state, plan=plan)
    try:
        root = _root(r, create=True)
        _private_save(root/'receipts.json', dict(version=1, identity=_identity(r),
            policy_sha256=_hash(plan['verification_policy']), entries={}))
    except (OSError, ValueError):
        # No private authority means unchanged fresh checks, not task failure.
        return


@contextlib.contextmanager
def _ledger(r):
    root = _root(r)
    fd = os.open(root/'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as lock:
        info = os.fstat(lock.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('verification lock is not private')
        fcntl.flock(lock, fcntl.LOCK_EX)
        _, data = _file(root/'receipts.json', limit=MAX_RECEIPT_BYTES, private=True)
        ledger = json.loads(data)
        if (not isinstance(ledger, dict) or ledger.get('version') != 1 or ledger.get('identity') != _identity(r)
                or ledger.get('policy_sha256') != _hash(r.plan['verification_policy'])
                or not isinstance(ledger.get('entries'), dict)):
            raise ValueError('verification witness project/contract/policy drift')
        yield root, ledger


def _source_dependencies(repo, script, source, declared):
    """Conservative offline dependency qualification, not a Python sandbox.

    Only static local paths and audited offline stdlib imports qualify. A
    cacheable declaration vouches for diagnostic outputs (e.g. a counter),
    which must not be acceptance inputs. Unknown syntax runs freshly.
    """
    tools = {}; visited = set(); diagnostics = set()
    search_roots = [Path(script).parent, repo, *(Path(p) for p in os.environ.get('PYTHONPATH', '').split(os.pathsep) if p)]

    def covered(name):
        p = Path(name)
        return any(p == d or d.is_dir() and p.is_relative_to(d) for d in declared)

    def analyze(path, text):
        if path in visited:
            return
        visited.add(path)
        if len(visited) > 128:
            raise ValueError('verification import bound exceeded')
        tree = ast.parse(text)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        factories = {'Path'}; aliases = {}; outputs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                raise ValueError('callable/annotation dependency surface is unqualified')
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                modules = [x.name for x in node.names] if isinstance(node, ast.Import) else [node.module]
                if isinstance(node, ast.ImportFrom) and (node.level or not node.module):
                    raise ValueError('relative import dependency unknown')
                if isinstance(node, ast.ImportFrom) and any(
                        x.name == '*' or x.name.startswith('_') or x.name in UNSAFE_ATTRIBUTES for x in node.names):
                    raise ValueError('imported dependency capability unknown')
                for module in modules:
                    parts = module.split('.')
                    local = repo.joinpath(*parts).with_suffix('.py')
                    for folder in search_roots:
                        if folder == repo:
                            continue
                        import importlib.machinery
                        candidates = [folder.joinpath(*parts).with_suffix(suffix)
                                      for suffix in ('.py', '.pyc', *importlib.machinery.EXTENSION_SUFFIXES)]
                        if any(p.exists() for p in candidates) or (folder/parts[0]).is_dir():
                            raise ValueError('additional search path shadows dependency')
                    import importlib.machinery
                    if any(repo.joinpath(*parts).with_suffix(s).exists() for s in ('.pyc', *importlib.machinery.EXTENSION_SUFFIXES)):
                        raise ValueError('local native/bytecode dependency unknown')
                    if local.is_file():
                        proof, data = _file(local, limit=1024*1024); tools[str(local)] = proof
                        analyze(str(local), data.decode())
                    elif parts[0] not in SAFE_IMPORTS:
                        raise ValueError('external/dynamic dependency is not reusable')
                    elif (repo/parts[0]).exists():
                        raise ValueError('shadowed toolchain dependency unknown')
                if isinstance(node, ast.ImportFrom) and node.module == 'pathlib':
                    factories.update(x.asname or x.name for x in node.names if x.name == 'Path')
            if isinstance(node, ast.Attribute) and (node.attr.startswith('_') or node.attr in UNSAFE_ATTRIBUTES):
                raise ValueError('dynamic Python introspection is not reusable')
            if isinstance(node, ast.Name) and (node.id.startswith('__') or node.id in UNSAFE_CALLS):
                raise ValueError('dynamic Python namespace is not reusable')
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in UNSAFE_CALLS:
                raise ValueError('dynamic execution is not reusable')

        def path_value(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id)
            if isinstance(node, ast.Call) and (isinstance(node.func, ast.Name) and node.func.id in factories
                    or isinstance(node.func, ast.Attribute) and node.func.attr == 'Path'):
                if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                    raise ValueError('dynamic filesystem dependency unknown')
                rel = Path(node.args[0].value)
                if rel.is_absolute() or '..' in rel.parts:
                    raise ValueError('filesystem dependency escapes project')
                return repo/rel
            return None

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                value = path_value(node.value)
                if value is not None:
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            aliases[target.id] = value
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in WRITES:
                value = path_value(node.func.value)
                if value is None:
                    raise ValueError('unknown filesystem output')
                if not covered(value) and not isinstance(parents.get(node), ast.Expr):
                    raise ValueError('diagnostic write result affects acceptance')
                outputs.add(value)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            value = path_value(node)
            if value is not None and not covered(value) and value not in outputs:
                raise ValueError('undeclared filesystem input')
            if isinstance(node.func, ast.Attribute) and node.func.attr in READS:
                value = path_value(node.func.value)
                if value is None or not covered(value) and value not in outputs:
                    raise ValueError('unknown/undeclared filesystem read')
                if not covered(value):
                    # Undeclared diagnostics may be read only to update that
                    # same output, with the write result discarded. They may
                    # not feed an assertion, branch or another acceptance input.
                    parent = parents.get(node)
                    while parent is not None:
                        if (isinstance(parent, ast.Call) and isinstance(parent.func, ast.Attribute)
                                and parent.func.attr in WRITES and path_value(parent.func.value) == value):
                            break
                        parent = parents.get(parent)
                    if parent is None:
                        raise ValueError('undeclared output is used as an acceptance dependency')
                if node.func.attr in ('stat', 'lstat', 'open'):
                    raise ValueError('filesystem metadata/stream semantics are unqualified')
                if node.func.attr in ('glob', 'rglob') and (len(node.args) != 1
                        or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str)
                        or Path(node.args[0].value).is_absolute() or '..' in Path(node.args[0].value).parts):
                    raise ValueError('dynamic/escaping filesystem pattern')
            if isinstance(node.func, ast.Attribute) and path_value(node.func.value) is not None:
                if node.func.attr not in READS | WRITES:
                    raise ValueError('unqualified filesystem operation')
            if isinstance(node.func, ast.Name) and node.func.id == 'open':
                raise ValueError('builtin stream dependency is unqualified')
        for output in outputs:
            if output.is_symlink() or any(p.is_symlink() for p in output.parents):
                raise ValueError('filesystem output links refused')
        diagnostics.update(outputs)
    analyze(str(script), source)
    # Cached local bytecode can override source; its membership/bytes matter.
    for path in (Path(script), *(Path(n) for n in tools)):
        folder = path.parent/'__pycache__'
        if folder.exists():
            tools[str(folder)] = _tree(folder)
    return tools, diagnostics


def fingerprint(r, task, argv):
    import runner
    if runner.digest(r.plan) != (r.state.get('contract_hash') or r.state['plan_hash']):
        raise ValueError('verification requires unchanged frozen plan/authority')
    entry = r.plan['verification_policy']['checks'].get(task['id'])
    if not entry or not entry['cacheable'] or not entry['inputs']:
        raise ValueError('check is undeclared, external/manual or noncacheable')
    if task.get('kind', 'command') != 'command':
        raise ValueError('manual/external checks are always fresh')
    frozen = r.plan.get('tasks', r.plan.get('acceptance', []))
    declared_task = next((t for t in frozen if t['id'] == task['id']), None)
    if declared_task is None or task['checks'] != declared_task.get('checks'):
        raise ValueError('verification requires exact original checks')
    row = r.state.get('tasks', {}).get(task['id'], {})
    if row.get('delivery_unknown') or r.process is not None or r.state.get('active_process'):
        raise ValueError('unknown delivery or incomplete cleanup cannot reuse verification')
    owner = r.state.get('owner_pid')
    if owner is not None and (owner != os.getpid() or not r.state.get('owner_identity')
            or runner.process_identity(owner) != r.state['owner_identity']):
        raise ValueError('current execution owner is unverified')
    # Only the actual selected Python runtime is currently qualified. Arbitrary
    # executables and shell wrappers keep their original argv and run uncached.
    executable = Path(argv[0]).absolute()
    if str(executable) != str(Path(sys.executable).absolute()) or len(argv) != 2:
        raise ValueError('toolchain/argv dependency surface is unqualified')
    script = Path(argv[1])
    script = script if script.is_absolute() else r.repo/script
    if not script.is_relative_to(r.repo) or '..' in script.parts:
        raise ValueError('check script escapes project')
    proof, source = _file(script, limit=1024*1024)
    declared = [r.repo/name for name in entry['inputs']]
    inputs = {name: _tree(r.repo/name) for name in entry['inputs']}
    dependencies, diagnostics = _source_dependencies(r.repo, script, source.decode(), declared)
    output_proofs = {}
    for path in sorted(diagnostics):
        try:
            output_proofs[str(path)] = _file(path)[0]
        except FileNotFoundError:
            output_proofs[str(path)] = None
    # Site hooks, native package imports and custom search paths are unknown
    # dependencies in v1. They do not prevent execution, only caching.
    for name in ('PYTHONHOME', 'PYTHONSTARTUP'):
        if os.environ.get(name):
            raise ValueError('Python startup dependency unknown')
    safe_python_env = {'PYTHONPATH', 'PYTHONDONTWRITEBYTECODE', 'PYTHONHASHSEED', 'PYTHONWARNINGS',
                       'PYTHONIOENCODING', 'PYTHONUTF8', 'PYTHONUNBUFFERED'}
    if any(n.startswith(('LD_', 'DYLD_')) or n.startswith('PYTHON') and n not in safe_python_env
           for n in os.environ):
        raise ValueError('loader/startup dependency unknown')
    for folder in site.getsitepackages() + ([site.getusersitepackages()] if site.ENABLE_USER_SITE else []):
        if any(Path(folder).glob('*.pth')):
            raise ValueError('Python site startup hooks are unqualified')
    for folder in [script.parent, r.repo, *(Path(p) for p in os.environ.get('PYTHONPATH', '').split(os.pathsep) if p)]:
        if any((folder/name).exists() for name in ('sitecustomize.py', 'usercustomize.py')):
            raise ValueError('custom Python startup is unqualified')
        if folder not in (r.repo, script.parent):
            import runtime_selection
            if folder.resolve() != runtime_selection.ROOT:
                raise ValueError('additional module search path is unqualified')
    actual = executable.resolve(strict=True)
    executable_proof, _ = _file(actual)
    venv = executable.parent.parent/'pyvenv.cfg'
    toolchain = dict(executable=str(executable), resolved=str(actual), proof=executable_proof,
        version=sys.version, prefix=sys.prefix, base_prefix=sys.base_prefix,
        stdlib=_hash(_tree(sysconfig.get_path('stdlib'), toolchain=True)),
        venv=_file(venv)[0] if venv.exists() else None, platform=list(os.uname()))
    if sysconfig.get_config_var('PYTHONFRAMEWORK'):
        raise ValueError('framework toolchain dependency is unqualified')
    library = sysconfig.get_config_var('LDLIBRARY')
    if sysconfig.get_config_var('Py_ENABLE_SHARED'):
        if not library or not sysconfig.get_config_var('LIBDIR'):
            raise ValueError('shared Python toolchain dependency unknown')
        lib = Path(sysconfig.get_config_var('LIBDIR'))/library
        toolchain['shared_library'] = dict(path=str(lib), resolved=str(lib.resolve(strict=True)),
                                          proof=_file(lib.resolve(strict=True))[0])
    # Lock/config files affect tools even when omitted from declared inputs.
    for name in ('pyproject.toml', 'requirements.txt', 'requirements.lock', 'uv.lock', 'poetry.lock',
                 'Pipfile', 'Pipfile.lock', 'package.json', 'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml'):
        p = r.repo/name
        dependencies[name] = _tree(p) if p.exists() else None
    parent = None
    if (r.dir/'parent.json').exists():
        _, data = _file(r.dir/'parent.json', limit=MAX_RECEIPT_BYTES)
        ref = json.loads(data)
        directory = Path(ref['workflow_dir'])
        _, raw = _file(directory/'state.json', limit=MAX_RECEIPT_BYTES)
        state = json.loads(raw)
        _, raw_contract = _file(directory/'contract.json', limit=MAX_RECEIPT_BYTES)
        contract = json.loads(raw_contract)
        if (directory != r.repo/'.codex/tasks/workflows'/ref['workflow_id']
                or state.get('id') != ref['workflow_id'] or state.get('repo') != str(r.repo)
                or state.get('contract_hash') != runner.digest(contract)
                or r.plan.get('verification_policy') != stage_policy(contract, r.plan)
                or state.get('active_run') != str(r.dir)
                or (directory/'STOP').exists() or not getattr(r, 'parent_guard', None)):
            raise ValueError('parent authority is unverified')
        parent = dict(reference=_hash(ref), contract=state['contract_hash'],
                      generation=state.get('execution_revision', 0))
    import runtime_selection
    selected = runtime_selection.require_current(r.dir)
    runtime = selected['runtime']['manifest_sha256'] if selected else _hash(runtime_selection._sources(runtime_selection.ROOT))
    payload = dict(version=1, identity=_identity(r), task=task['id'], argv=_hash(argv),
        source=proof, inputs=inputs, dependencies=dependencies, toolchain=toolchain,
        environment=_hash(dict(os.environ)), declared_env={n: _hash(os.environ.get(n)) for n in entry['env']},
        authority=_hash(dict(sandbox=r.plan.get('sandbox', 'workspace-write'), permissions=r.plan.get('permissions'),
            host_handoff=r.plan.get('host_handoff'), generation=r.state.get('execution_revision', 0), parent=parent)),
        runtime=runtime)
    return dict(sha256=_hash(payload), source_sha256=_hash(dict(script=proof, inputs=inputs, dependencies=dependencies)),
                toolchain_sha256=_hash(toolchain), argv_sha256=_hash(argv), diagnostics_sha256=_hash(output_proofs))


def fresh_label(label):
    return bool(re.search(r'(^|[-_])(final|full|integration|external|manual|health|permission|host)([-_]|$)', label))


def lookup(r, task, index, argv, label, *, fresh=False):
    """Return a verified original receipt or a fresh-execution ticket."""
    ticket = dict(key=_hash([task['id'], index, argv]), before=None, reason='fresh_required')
    try:
        ticket['before'] = fingerprint(r, task, argv)
        with _ledger(r) as (_, ledger):
            receipt = ledger['entries'].get(ticket['key'])
            if fresh or fresh_label(label):
                return None, ticket
            if (isinstance(receipt, dict) and type(receipt.get('returncode')) is int
                    and receipt['returncode'] == 0 and receipt.get('inputs') == ticket['before']):
                age = time.time() - receipt['finished_at']
                log = Path(receipt['evidence'])
                if log.parent != r.dir/'evidence':
                    raise ValueError('verification evidence belongs to another run')
                proof, _ = _file(log, limit=16*1024*1024)
                if (0 <= age <= MAX_AGE_SECONDS and proof == receipt['evidence_proof']
                        and fingerprint(r, task, argv) == ticket['before']):
                    ticket['reason'] = 'unchanged_complete_host_inputs'
                    return receipt, ticket
            ticket['reason'] = 'missing_expired_or_changed_receipt'
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, SyntaxError, RuntimeError):
        ticket['before'] = None
        ticket['reason'] = 'unknown_dependency_authority_or_evidence'
    return None, ticket


def invalidate(r, ticket):
    """Before every real execution, retire previous success, including final."""
    try:
        with _ledger(r) as (root, ledger):
            if ticket['key'] in ledger['entries']:
                ledger['entries'].pop(ticket['key'])
                # A valid prior success must be retired durably before another
                # execution can fail. Storage failure here aborts before spawn.
                try:
                    _private_save(root/'receipts.json', ledger)
                except OSError as error:
                    raise RuntimeError('cannot durably retire prior host verification') from error
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return


def record(r, task, argv, ticket, code, log, started, finished):
    if code or ticket['before'] is None:
        return
    try:
        after = fingerprint(r, task, argv)
        # Diagnostic outputs may change during their own host execution. Bind
        # the successful post-state so later deletion/corruption also reruns.
        main_before = {k: v for k, v in ticket['before'].items() if k != 'diagnostics_sha256'}
        main_after = {k: v for k, v in after.items() if k != 'diagnostics_sha256'}
        if main_after != main_before or Path(log).parent != r.dir/'evidence':
            return
        proof, _ = _file(log, limit=16*1024*1024)
        receipt = dict(version=1, run=str(r.dir), task=task['id'], inputs=after,
            started_at=started, finished_at=finished, duration_seconds=max(0, finished-started),
            returncode=code, evidence=log, evidence_proof=proof, authority='host-command-returncode-v1')
        with _ledger(r) as (root, ledger):
            if len(ledger['entries']) >= 512 and ticket['key'] not in ledger['entries']:
                return
            ledger['entries'][ticket['key']] = receipt
            if len(json.dumps(ledger)) <= MAX_RECEIPT_BYTES:
                _private_save(root/'receipts.json', ledger)
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, SyntaxError, RuntimeError):
        return
