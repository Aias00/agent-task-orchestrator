"""Trusted managed-coordinator launch policy, separate from the ACP worker.

No plan, model or installed native auth chooses a command. Trusted host tool
locators are verified against the code-owned release registry before launch.
The local-only qualification profile accepts explicit *non-secret* model config
from trusted host code. It never copies a user's native configuration.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import subprocess  # Retain the historic subprocess.run injection seam.
from pi_toolchain import (ExecutableLocator, clean_environment, prepare_toolchain,
                          write_exclusions, write_launch)

from pi_acp_compat import SOURCE, backend, derive_coordinator, derive_directory

ROOT = Path(__file__).resolve().parent
ADAPTER_SHA256 = backend('coordinator')['source_sha256']
PI = ExecutableLocator('pi')
NODE = ExecutableLocator('node')
SANDBOX = Path('/usr/bin/sandbox-exec')


@dataclass(frozen=True)
class Launch:
    command: tuple
    environment: dict
    provenance: str
    native_load_verified: bool = False
    provenance_data: dict | None = None


class LocalPiProfile:
    """Real Pi 1.5.2 + pinned ACP, loopback model only; no implicit credentials.

    Creating this profile does no IO. ``launch`` checks the exact dependency
    and version, writes an empty private home, and builds a restricted command.
    It does not launch a model. No filesystem/network permission widening is
    accepted through contract fields.
    """
    native_load_verified = False

    def __init__(self, model_configuration, model):
        self.models = json.loads(json.dumps(model_configuration, allow_nan=False))
        self.model = model
        from urllib.parse import urlsplit
        providers = self.models.get('providers', {})
        if (set(self.models) != {'providers'} or not isinstance(providers, dict) or not providers
                or not isinstance(model, str) or not model or model.startswith('-')):
            raise ValueError('explicit local model configuration required')
        for provider in providers.values():
            url = urlsplit(provider.get('baseUrl', ''))
            if (url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost', '::1')
                    or url.username or url.password or not url.port
                    or provider.get('apiKey') != 'ato-local-dummy'):
                raise ValueError('only scripted loopback model with dummy credential is allowed')
            if set(provider) - {'baseUrl', 'apiKey', 'api', 'models'}:
                raise ValueError('unsupported provider configuration')
            if provider.get('api') not in ('openai-completions', 'openai-responses', 'anthropic-messages'):
                raise ValueError('explicit local protocol required')
            models = provider.get('models')
            if not isinstance(models, list) or not models:
                raise ValueError('explicit scripted model list required')
            allowed = {'id', 'name', 'reasoning', 'input', 'cost', 'contextWindow', 'maxTokens'}
            for entry in models:
                if (not isinstance(entry, dict) or set(entry)-allowed
                        or not isinstance(entry.get('id'), str) or not entry['id']):
                    raise ValueError('model entry may not override endpoint, headers, key or protocol')
        # Config is data, not executable key commands, headers or extensions.
        if any(word in json.dumps(self.models) for word in ('!command', 'apiKeyCommand')):
            raise ValueError('executable model configuration is forbidden')

    def launch(self, runtime, project, sandbox):
        from transport import _private_save
        if sandbox not in ('read-only', 'workspace-write'):
            raise ValueError('managed Pi supports bounded file tools only')
        runtime, project = Path(runtime).resolve(), Path(project).resolve()
        if runtime.is_relative_to(project):
            raise ValueError('coordinator runtime must be outside model workspace')
        if any(path.resolve().is_relative_to(project) for path in (ROOT, SOURCE)):
            raise ValueError('model workspace may not contain trusted coordinator code or executables')
        if not SANDBOX.is_file():
            raise ValueError('OS sandbox unavailable')
        tools = prepare_toolchain(project, runtime, node=NODE, pi=PI)
        for name in ('home', 'native', 'sessions', 'tmp'):
            (runtime/name).mkdir(mode=0o700)
        _private_save(runtime/'native/models.json', self.models)
        _private_save(runtime/'native/settings.json', dict(quietStartup=True))
        adapter = derive_directory(runtime/'adapter', role='coordinator')
        launcher = write_launch(runtime, project, 'coordinator', tools)
        q = lambda p: json.dumps(str(Path(p).resolve()))
        # The model has no shell/network tool. The OS further limits the native
        # model client to loopback and writes to the owned runtime/project.
        from urllib.parse import urlsplit
        ports = sorted({urlsplit(p['baseUrl']).port for p in self.models['providers'].values()})
        rules = ['(version 1)', '(allow default)', '(deny network*)',
                 *['(allow network-outbound (remote ip "localhost:'+str(port)+'"))' for port in ports],
                 '(deny file-write*)',
                 '(allow file-write* (literal "/dev/null") (require-all (subpath '+q(runtime)+') '+write_exclusions(runtime)+'))']
        if sandbox == 'workspace-write':
            exclusions = ' '.join('(require-not (subpath '+q(project/name)+'))'
                                  for name in ('.git', '.codex', '.ato', '.pi', '.aws', '.ssh', '.env'))
            rules.append('(allow file-write* (require-all (subpath '+q(project)+') '+exclusions+' '+write_exclusions(runtime)+'))')
        (runtime/'sandbox.sb').write_text('\n'.join(rules))
        environment = dict(clean_environment(home=runtime/'home'),
            PI_CODING_AGENT_DIR=str(runtime/'native'), PI_TELEMETRY='0',
            CTASK_PI_ACP_STATE_DIR=str(runtime/'adapter-state'),
            PI_ACP_PI_COMMAND=str(launcher),
            CTASK_PI_RUNTIME=str(runtime), CTASK_AGENT_REPO=str(project),
            CTASK_AGENT_SANDBOX=sandbox, CTASK_PI_MODEL=self.model,
            TMPDIR=str(runtime/'tmp'), JITI_CACHE=str(runtime/'jiti'))
        return Launch((str(SANDBOX), '-f', str(runtime/'sandbox.sb'), str(tools.node), str(adapter)),
            environment, 'actual_pi_cli_with_scripted_loopback_model:Pi'+tools.pi_version
            +':pi-acp'+backend('coordinator')['version']+':'+ADAPTER_SHA256,
            provenance_data=dict(pi_version=tools.pi_version, pi_binary_sha256=tools.pi_sha256,
                node_version=tools.node_version, node_binary_sha256=tools.node_sha256,
                adapter_sha256=ADAPTER_SHA256, derived_adapter_sha256=hashlib.sha256(adapter.read_bytes()).hexdigest(),
                actual_cli_qualified=False, cloud_model_qualified=False))
