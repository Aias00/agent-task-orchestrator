"""Version 1 task runtime bindings, issued by trusted creation code only.

The task carries an audit record, not a command or import override. Its exact
bytes are witnessed in the private ATO control home, outside the worker's
repository write grant. Neither a plan nor an environment variable selects a
runtime. Environment locators never substitute for a missing creation receipt.
Unbound historical tasks remain legacy; loading never writes or migrates them.
"""
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import stat
import sys
import uuid

import ato_config

ROOT = Path(__file__).resolve().parent
NAME = 'runtime-selection.json'
COMPATIBILITY = 'ato-runtime-selection-v1'
_hashes = {}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _read(path, private=False):
    path = Path(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW
    fd = os.open(path, flags)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('runtime binding requires a regular unlinked file: '+str(path))
        if private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
            raise ValueError('runtime receipt ownership or mode changed')
        return stream.read()


def _json(path, private=False):
    return json.loads(_read(path, private))


def _exclusive(path, value):
    data = (json.dumps(value, sort_keys=True, indent=2)+'\n').encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _receipt(directory):
    # A locator is not authorization. A missing receipt for a bound task fails
    # closed, even when the caller changes ATO_HOME or removes the audit record.
    return ato_config.ato_home()/'runtime-selections-v1'/(_digest(str(directory))+'.json')


def _file(path, *, dependency=False):
    path = Path(path)
    if not dependency and path.is_symlink():
        raise ValueError('runtime source links are not supported: '+str(path))
    actual = path.resolve(strict=True)
    info = actual.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('runtime input is not a regular file: '+str(path))
    key = (str(actual), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
           info.st_ctime_ns, info.st_mode)
    if key not in _hashes:
        if len(_hashes) >= 8192:
            _hashes.clear()
        _hashes[key] = hashlib.sha256(actual.read_bytes()).hexdigest()
    return dict(sha256=_hashes[key],
                mode=stat.S_IMODE(info.st_mode), resolved=str(actual), path=str(path.absolute()))


def _tree(folder, *, dependency=False):
    folder = Path(folder)
    if not folder.exists():
        return None
    result = {}
    for base, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        if any((Path(base)/d).is_symlink() for d in dirs):
            raise ValueError('nested runtime directory links are not supported')
        for name in sorted(files):
            if name.endswith(('.pyc', '.pyo')):
                continue
            p = Path(base)/name
            result[str(p.relative_to(folder))] = _file(p, dependency=dependency)
    return result


def _sources(root):
    # Enumerate as well as hash: a new shadowing Python module is also drift.
    result = {p.name: _file(p) for p in sorted(root.iterdir())
              if p.suffix in ('.py', '.mjs', '.lock')}
    for name in ('bin', 'scripts', 'integrations', 'tests/fixtures'):
        folder = root/name
        if not folder.exists():
            continue
        for base, dirs, files in os.walk(folder, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in ('node_modules', '__pycache__'))
            if any((Path(base)/d).is_symlink() for d in dirs):
                raise ValueError('runtime source directory links are not supported')
            for leaf in sorted(files):
                if not leaf.endswith(('.pyc', '.pyo', '.log')):
                    p = Path(base)/leaf
                    result[str(p.relative_to(root))] = _file(p)
    return result


def _python_dependencies():
    result = {}
    for name in ('agent-client-protocol', 'pydantic', 'pydantic_core',
                 'annotated-types', 'typing-inspection', 'typing_extensions'):
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
            continue
        files = {}
        if dist.files is None:
            raise ValueError('runtime dependency inventory unavailable: '+name)
        for leaf in dist.files:
            if str(leaf).endswith(('.pyc', '.pyo')):
                continue
            files[str(leaf)] = _file(dist.locate_file(leaf), dependency=True)
        result[name] = dict(version=dist.version, files=files)
    return result


def _selected():
    import agent_registry
    runner = agent_registry.runtime_entrypoint()
    if not Path(runner).is_relative_to(ROOT):
        raise ValueError('trusted runtime bootstrap must belong to its source tree')
    source = _sources(ROOT)
    if str(Path(runner).relative_to(ROOT)) not in source:
        raise ValueError('trusted runtime bootstrap is not in source manifest')
    python = str(Path(sys.executable).absolute())  # Retain the venv invocation path.
    manifest = dict(source=source, interpreter=_file(python, dependency=True),
                    python_version=sys.version, prefix=sys.prefix, base_prefix=sys.base_prefix,
                    python_dependencies=_python_dependencies(),
                    node_dependencies=_tree(ROOT/'integrations/pi-acp/node_modules', dependency=True))
    return dict(runner=runner, python=python, root=str(ROOT), manifest=manifest,
                manifest_sha256=_digest(manifest), compatibility=COMPATIBILITY)


def _binding(directory, state):
    workflow = state.get('kind') == 'workflow'
    contract = _json(directory/('contract.json' if workflow else 'plan.json'))
    # Preserve the established contract digest, including its legacy encoding.
    checksum = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
    if checksum != state.get('contract_hash' if workflow else 'plan_hash'):
        raise ValueError('runtime contract does not match authoritative state')
    project = str(Path(state['repo']).resolve(strict=True))
    if state['repo'] != project or not directory.is_relative_to(Path(project)/'.codex/tasks'):
        raise ValueError('runtime binding has wrong project or task directory')
    origin = None
    if (directory/'origin.json').exists():
        value = _json(directory/'origin.json')
        if value.get('repo') != project or value.get('run_id') != state['id']:
            raise ValueError('runtime origin task/project mismatch')
        origin = {k: value.get(k) for k in ('origin_session_ref', 'origin_thread_id',
                                          'parent_workflow', 'codex_home', 'host')}
    return dict(directory=str(directory), project=project, id=state['id'],
                kind='workflow' if workflow else 'stage', contract_sha256=checksum,
                generation=state.get('runtime_selection'), actor='trusted-creation-code-v1', origin=origin)


def pin(directory, *, parent=None):
    """Trusted create-time API; never called with a model-selected runtime.

    Existing tasks cannot be rebound. A stage inherits its parent's selection;
    legacy parents explicitly keep legacy children. This is not migration.
    """
    directory = Path(directory).resolve(strict=True)
    state = _json(directory/'state.json')
    if state.get('status') != 'created' or state.get('runtime_selection') or (directory/NAME).exists():
        raise ValueError('runtime selection can only be issued once at creation')
    receipt = _receipt(directory)
    receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if receipt.parent.is_symlink() or stat.S_IMODE(receipt.parent.stat().st_mode) != 0o700:
        raise ValueError('runtime receipt directory must be private')
    inherited = None
    if parent is not None:
        inherited = require_current(parent)
        if inherited is None:
            return None
    selection = _selected() if inherited is None else inherited['runtime']
    marker = dict(version=1, generation=uuid.uuid4().hex,
                  authority_home=str(ato_config.ato_home()))
    state['runtime_selection'] = marker
    record = dict(version=1, binding=_binding(directory, state), runtime=selection,
                  source='trusted CLI creation' if parent is None else 'verified workflow stage inheritance',
                  parent=None if inherited is None else dict(directory=str(Path(parent).resolve()),
                      selection_sha256=_digest(inherited)))
    # Receipt first: interruption can leave an unusable new task, never a legacy
    # fallback or a partially trusted command. No overwrite/rebind operation.
    _exclusive(receipt, record)
    _exclusive(directory/NAME, record)
    import runner
    runner.save(directory/'state.json', state)
    return record


def load(directory):
    """Read-only authenticated audit record, or None for an unbound legacy task.

    Validates task identity and exact source/interpreter/dependency bytes before
    any selected entry point can execute. Does not import selected code.
    """
    directory = Path(directory).resolve(strict=True)
    path = directory/NAME
    receipt = _receipt(directory)
    # Public adapter command builders historically also accept a plain scratch
    # directory. This grants no execution ownership or task identity.
    if not (directory/'state.json').exists() and not path.exists() and not receipt.exists():
        return None
    state = _json(directory/'state.json')
    marker = state.get('runtime_selection')
    if marker and marker.get('authority_home') != str(ato_config.ato_home()):
        raise ValueError('runtime receipt authority home differs from creation; locator is not authority')
    if receipt.parent.is_symlink():
        raise ValueError('runtime receipt directory link refused')
    if not path.exists() and not path.is_symlink():
        if state.get('runtime_selection') or receipt.exists():
            raise ValueError('runtime binding removed; legacy downgrade refused')
        return None
    record = _json(path, private=True)
    if _read(path, True) != _read(receipt, True):
        raise ValueError('runtime selection differs from trusted creation receipt')
    if record.get('version') != 1 or record.get('binding') != _binding(directory, state):
        raise ValueError('runtime selection task/project/contract/generation/actor mismatch')
    selected = record['runtime']
    if selected.get('compatibility') != COMPATIBILITY:
        raise ValueError('unsupported runtime selection compatibility')
    root = Path(selected['root'])
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError('runtime source root changed')
    runner = Path(selected['runner'])
    if not runner.is_relative_to(root) or str(runner.relative_to(root)) not in selected['manifest']['source']:
        raise ValueError('runtime entrypoint outside its witnessed source manifest')
    manifest = selected['manifest']
    if _digest(manifest) != selected['manifest_sha256']:
        raise ValueError('runtime manifest fingerprint mismatch')
    if _sources(root) != manifest['source']:
        raise ValueError('selected runtime source drift')
    if _file(selected['python'], dependency=True) != manifest['interpreter']:
        raise ValueError('selected runtime interpreter drift')
    # Validate witnessed dependency files before exec, without importing code
    # through the selected interpreter or trusting this launcher's environment.
    for dependency in manifest['python_dependencies'].values():
        if dependency is not None:
            for expected in dependency['files'].values():
                if _file(expected['path'], dependency=True) != expected:
                    raise ValueError('selected Python dependency drift')
    if _tree(root/'integrations/pi-acp/node_modules', dependency=True) != manifest['node_dependencies']:
        raise ValueError('selected runtime dependency drift')
    parent = record.get('parent')
    if parent:
        p = Path(parent['directory'])
        # Parent references come only from the witnessed creation receipt. Do
        # not follow a model-supplied parent.json as a runtime/import selector.
        if p == directory or not (p/'contract.json').is_file():
            raise ValueError('invalid runtime parent')
        original = load(p)
        relation = _json(directory/'parent.json')
        if (original is None or _digest(original) != parent['selection_sha256']
                or original['runtime'] != selected or relation.get('workflow_dir') != str(p)
                or relation.get('workflow_id') != original['binding']['id']):
            raise ValueError('stage runtime differs from its exact workflow binding')
    return record


def require_current(directory):
    record = load(directory)
    if record is not None:
        selected = record['runtime']
        import agent_registry
        if (selected['root'] != str(ROOT) or selected['runner'] != agent_registry.runtime_entrypoint()
                or selected['python'] != str(Path(sys.executable).absolute())):
            raise ValueError('wrong runtime binding; use the task CLI to dispatch its pinned entrypoint')
        manifest = selected['manifest']
        if (manifest['python_version'] != sys.version or manifest['prefix'] != sys.prefix
                or manifest['base_prefix'] != sys.base_prefix
                or manifest['python_dependencies'] != _python_dependencies()):
            raise ValueError('selected Python environment drift')
    return record


def command(directory, *arguments):
    record = load(task_directory(directory))
    if record is None:
        return None
    selected = record['runtime']
    return [selected['python'], selected['runner'], *map(str, arguments)]


def route(directory, arguments):
    """Replace this foreground CLI with the exact witnessed task runtime.

    No shell, dynamic imports, launcher pin, detached job or environment runtime
    override. The destination repeats validation before performing any action.
    """
    directory = task_directory(directory)
    record = load(directory)
    if record is None:
        return
    selected = record['runtime']
    import agent_registry
    if (selected['root'] == str(ROOT) and selected['runner'] == agent_registry.runtime_entrypoint()
            and selected['python'] == str(Path(sys.executable).absolute())):
        require_current(directory)
        return
    argv = command(directory, *arguments)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    for key in ('PYTHONPATH', 'PYTHONHOME'):
        env.pop(key, None)
    os.execve(argv[0], argv, env)


def task_directory(directory):
    """Resolve an existing delegation to its exact task, never to an import.

    Dispatchers carry a delegation directory rather than a task directory. Its
    target is still authenticated by load before any executable is selected.
    """
    directory = Path(directory).resolve(strict=True)
    path = directory/'delegation.json'
    if not (directory/'state.json').exists() and path.exists():
        row = _json(path)
        if row.get('workflow_dir'):
            target = Path(row['workflow_dir']).resolve(strict=True)
            state = _json(target/'state.json')
            if (state.get('kind') != 'workflow' or state.get('repo') != row.get('repo')
                    or not directory.is_relative_to(Path(state['repo'])/'.codex/tasks/delegations')):
                raise ValueError('delegation runtime task/project mismatch')
            return target
    return directory
