"""Reviewed acpx dependency/profile boundary. Never load paths from task JSON.

Provisioning belongs to the operator. Importing this module performs no I/O.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import sys
import tarfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
OPS = Path('OPERATOR_EVIDENCE/ato-agy-acpx-integration-20261119')
DEPS = OPS / 'deps/node_modules'
ARCHIVE = Path('OPERATOR_DEPENDENCIES/acpx/.local-verification/acpx-0.22.0.tgz')
ACPX_SHA256 = 'ea181d761bf141858e8c92de13c77b40783e9b6189d73f1d654afcc96cb15dd0'
ADAPTER_SHA256 = '8ef25bf9e507fb1af3625287b3fb1a9e4aaac539d44834fdef17b7d3cfbe4202'
NODE_SHA256 = '6f32069480ec14d52b6d7b1f3c7124cfbbc46203abf5ddf5c65b127435f3f329'
NATIVE_SHA256 = '80a3933d11a9d13ef806aa24f7bb8afc9169cfe4e9b09d6da6a92922cbde9cff'
NODE = Path('OPERATOR_HOME/.nvm/versions/node/v24.22.1/bin/node')
SANDBOX = Path('/usr/bin/sandbox-exec')
# A clean, dedicated home must be provisioned/authenticated by the operator.
# The implementation never opens auth.json or an existing user's native home.
NATIVE_HOME = OPS / 'codex-home'
SYSTEM_CONFIG_PATHS = (
    Path('/etc/codex/config.toml'), Path('/etc/codex/managed_config.toml'),
    Path('/Library/Managed Preferences/com.openai.codex.plist'),
)


class ProfileUnavailable(ValueError):
    """A fixed, safe-to-display operator prerequisite (never native stderr)."""


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def operator_paths():
    """Private host locator only. Pins/policy stay code-owned, never plan fields."""
    locator = os.environ.get('ATO_ACPX_CONFIG')
    if not locator:
        raise ProfileUnavailable('origin must supply ATO_ACPX_CONFIG pointing to private reviewed dependency paths')
    path = Path(locator)
    try:
        if (not path.is_absolute() or path.is_symlink() or path.resolve() != path
                or not path.is_file() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
            raise ValueError()
        value = json.loads(path.read_text())
        if (set(value) - {'model_proxy'} != {'version', 'node', 'archive', 'deps', 'native_home'}
                or type(value['version']) is not int or value['version'] != 1):
            raise ValueError()
        result = {key: Path(value[key]) for key in ('node', 'archive', 'deps', 'native_home')}
        for item in result.values():
            if not item.is_absolute() or item.resolve() != item:
                raise ValueError()
        if 'model_proxy' in value:
            proxy = urlsplit(value['model_proxy'])
            if (proxy.scheme not in ('http', 'https') or proxy.hostname not in ('127.0.0.1', '::1', 'localhost')
                    or proxy.username or proxy.password or not proxy.port or proxy.path not in ('', '/')
                    or proxy.query or proxy.fragment):
                raise ValueError()
            result['model_proxy'] = value['model_proxy']
        result['config'] = path
        return result
    except (OSError, ValueError, TypeError):
        raise ProfileUnavailable('invalid private host dependency configuration') from None


def _dependencies(paths=None):
    """Verify the qualified package, not a similarly named checkout/package."""
    paths = paths or dict(node=NODE, archive=ARCHIVE, deps=DEPS)
    node, archive_path, deps = [paths[key] for key in ('node', 'archive', 'deps')]
    if (not node.is_file() or sha256(node) != NODE_SHA256
            or not archive_path.is_file() or sha256(archive_path) != ACPX_SHA256):
        raise ProfileUnavailable('approved Node/acpx archive missing or changed; origin must provision exact pins')
    with tarfile.open(archive_path, 'r:gz') as archive:
        count = 0
        for member in archive.getmembers():
            if member.name.startswith('package/dist/') and member.name.endswith('.js'):
                relative = Path(member.name).relative_to('package')
                installed = deps/'acpx'/relative
                if (not member.isfile() or installed.is_symlink() or not installed.is_file()
                        or hashlib.sha256(archive.extractfile(member).read()).hexdigest() != sha256(installed)):
                    raise ProfileUnavailable('installed acpx runtime differs from qualified package')
                count += 1
        if count < 2:
            raise ProfileUnavailable('qualified acpx archive lacks runtime assets')
    for package, version in [('acpx', '0.22.0'), ('@agentclientprotocol/codex-acp', '1.1.5'),
                             ('@openai/codex', '0.144.6')]:
        if json.loads((deps/package/'package.json').read_text()).get('version') != version:
            raise ProfileUnavailable('incorrect pinned dependency: '+package)
    adapter = deps/'@agentclientprotocol/codex-acp/dist/index.js'
    if sha256(adapter) != ADAPTER_SHA256:
        raise ProfileUnavailable('Codex ACP entry differs from reviewed 1.1.5')
    return dict(runtime_url=(deps/'acpx/dist/runtime.js').as_uri(),
                registry_url=(deps/'acpx/dist/agent-registry.js').as_uri())


def dependencies(paths=None):
    try:
        return _dependencies(paths)
    except (OSError, tarfile.TarError, json.JSONDecodeError):
        raise ProfileUnavailable('pinned dependency assets missing or invalid; origin must privately provision exact packages') from None


def validate_request(request):
    if request.planner:
        raise ValueError('acpx_codex does not support planner execution')
    if request.sandbox not in ('read-only', 'workspace-write'):
        raise ValueError('acpx_codex supports only read-only/workspace-write')
    if request.plan.get('permissions'):
        raise ValueError('acpx_codex does not support additional permission grants')


def check_clean_home(home):
    """Inspect names only; never read native configuration or credentials."""
    home = Path(home)
    if home.is_symlink() or not home.is_dir() or home.stat().st_mode & 0o077:
        raise ProfileUnavailable('origin must provision a private clean Codex home (0700)')
    # Refuse config, plugins and instructions, including symlinks. Native login
    # is consumed by native Codex only; account absence is a native prerequisite.
    for name in ('config.toml', 'managed_config.toml', 'requirements.toml', 'AGENTS.md', 'AGENTS.override.md', 'skills', 'plugins', '.agents'):
        if os.path.lexists(home/name):
            raise ProfileUnavailable('clean Codex home must not contain native config/skills/plugins')


def reject_project_config(repo):
    for parent in (Path(repo), *Path(repo).parents):
        if os.path.lexists(parent/'.codex/config.toml'):
            raise ProfileUnavailable('acpx_codex refuses inherited project Codex config')
    for path in SYSTEM_CONFIG_PATHS:
        if os.path.lexists(path):
            raise ProfileUnavailable('acpx_codex requires qualification without inherited system Codex config')


def sandbox_profile(repo, scratch, native_home, authority, mode):
    """Outer native-process write fence; never grants ATO checkpoint access."""
    if mode not in ('read-only', 'workspace-write'):
        raise ValueError('unsupported sandbox')
    q = lambda p: json.dumps(str(Path(p).resolve()))
    rules = ['(version 1)', '(allow default)', '(deny file-write*)',
             '(allow file-write* (literal "/dev/null") (subpath '+q(scratch)+'))',
             '(allow file-write* (require-all (subpath '+q(native_home)+') '
             '(require-not (literal '+q(Path(native_home)/'auth.json')+')) '
             '(require-not (literal '+q(Path(native_home)/'config.toml')+'))))']
    if mode == 'workspace-write':
        rules += ['(allow file-write* (require-all (subpath '+q(repo)+') '+
                  ' '.join('(require-not (subpath '+q(Path(repo)/p)+'))'
                           for p in ('.git', '.codex', '.agents'))+'))']
    # These denies also cover runtime siblings and out-of-project ATO_HOME.
    for path in authority:
        rules += ['(deny file-write* (subpath '+q(path)+'))',
                  '(deny file-read* (subpath '+q(path)+'))']
    # Block credential/config mutation even by a child with a native preset.
    rules += ['(deny file-write* (literal '+q(Path(native_home)/'auth.json')+'))',
              '(deny file-write* (literal '+q(Path(native_home)/'config.toml')+'))']
    return '\n'.join(rules)+'\n'


def trusted_tool_path(node):
    """Only reviewed launch toolchains, never the caller's PATH."""
    return os.pathsep.join(dict.fromkeys([str(Path(node).resolve().parent),
        str(Path(sys.executable).resolve().parent), '/usr/bin', '/bin']))


def native_policy(repo, authority, mode, tool_path='/usr/bin:/bin'):
    """Named native profile, used for CLI defaults AND every app-server turn."""
    if mode not in ('read-only', 'workspace-write'):
        raise ProfileUnavailable('unsupported native mode')
    extends = ':workspace' if mode == 'workspace-write' else ':read-only'
    denies = {str(Path(path).resolve()): 'none' for path in authority}
    return dict(default_permissions='ato-worker', approval_policy='never',
        permissions={'ato-worker': dict(extends=extends, filesystem=denies,
                                      network=dict(enabled=False))},
        mcp_servers={}, web_search='disabled',
        features=dict(multi_agent=False, apps=False, skills=False),
        shell_environment_policy=dict(inherit='none', set={'PATH': tool_path}))


def native_args(config):
    profile = config['permissions']['ato-worker']
    mapping = '{'+', '.join(json.dumps(k)+'='+json.dumps(v)
                          for k, v in profile['filesystem'].items())+'}'
    return ['app-server', '-c', 'default_permissions="ato-worker"',
        '-c', 'permissions.ato-worker.extends='+json.dumps(profile['extends']),
        '-c', 'permissions.ato-worker.filesystem='+mapping,
        '-c', 'permissions.ato-worker.network.enabled=false',
        '-c', 'approval_policy="never"', '-c', 'web_search="disabled"',
        '-c', 'features.multi_agent=false', '-c', 'features.apps=false']


def codex_profile(repo, scratch, authority, mode):
    """Single native sandbox; trusted gateway prevents ACP preset override."""
    import runner
    paths = operator_paths()
    workspace = Path(repo).resolve()
    protected_paths = [ROOT, *(v for v in paths.values() if isinstance(v, Path))]
    for protected in protected_paths:
        protected = protected.resolve()
        if workspace == protected or workspace in protected.parents or protected in workspace.parents:
            raise ProfileUnavailable('worker repository must not overlap runtime, dependencies or authority')
    pins = dependencies(paths)
    if platform.system() != 'Darwin' or platform.machine() != 'arm64' or not SANDBOX.is_file():
        raise ProfileUnavailable('acpx_codex requires qualified native macOS arm64 sandbox support')
    home = paths['native_home']
    check_clean_home(home)
    if not os.path.lexists(home/'auth.json'):
        raise ProfileUnavailable('origin must authenticate clean native home in place; no automatic login/fallback')
    reject_project_config(repo)
    # Native startup generates config and built-in skills. Keep the operator's
    # clean authentication seed untouched; every attempt gets a fresh disposable
    # native home. Reference login by path only, never read/copy its contents.
    execution_home = Path(scratch)/'native-home'
    execution_home.mkdir(mode=0o700)
    (execution_home/'auth.json').symlink_to(home/'auth.json')
    native = paths['deps']/'@openai/codex-darwin-arm64/vendor/aarch64-apple-darwin/bin/codex'
    if not native.is_file() or sha256(native) != NATIVE_SHA256:
        raise ProfileUnavailable('pinned native Codex dependency unavailable or changed')
    # Private unique control files are denied to native tools, including alias
    # access. The trusted unsandboxed server consumes them before executing tools.
    control = Path(scratch)/'control'
    control.mkdir(mode=0o700)
    work = Path(scratch)/'work'
    work.mkdir(mode=0o700)
    tool_path = trusted_tool_path(paths['node'])
    native_config = native_policy(repo, [*authority, *protected_paths, home, execution_home, control], mode, tool_path)
    environment = dict(HOME=str(work), CODEX_HOME=str(execution_home), PATH=tool_path,
                       TMPDIR=str(work), NO_BROWSER='1')
    # Only the native model transport receives an explicitly reviewed loopback
    # proxy. Worker tool shells keep inherit=none and network disabled. Never
    # inherit ambient proxy/config/token variables from the caller.
    if paths.get('model_proxy'):
        environment['HTTPS_PROXY'] = paths['model_proxy']
    policy_file = control/'policy.json'
    policy_file.write_text(json.dumps(dict(version=1, native=str(native),
        repo=str(workspace), schema=runner.SCHEMA, native_config=native_config,
        extends=native_config['permissions']['ato-worker']['extends'],
        args=native_args(native_config), env=environment)))
    policy_file.chmod(0o600)
    launcher = control/'codex-proxy'
    launcher.write_text('#!/bin/sh\nexec '+shlex.quote(str(paths['node']))+' '+
        shlex.quote(str(ROOT/'acpx_native_proxy.mjs'))+' "$@"\n')
    launcher.chmod(0o700)
    return dict(**pins, agent='ato-codex', final_parser='codex-1.1.5', node=str(paths['node']),
        argv=[str(paths['node']), str(paths['deps']/'@agentclientprotocol/codex-acp/dist/index.js')],
        agent_env=dict(**environment, CODEX_PATH=str(launcher), ATO_NATIVE_POLICY=str(policy_file),
            CODEX_CONFIG=json.dumps(native_config),
            INITIAL_AGENT_MODE='read-only' if mode == 'read-only' else 'agent'),
        required_mode='read-only' if mode == 'read-only' else 'agent')
