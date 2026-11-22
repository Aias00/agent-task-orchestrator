"""Explicit production configuration; no registration, credentials or IO on import."""
import copy
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from pi_production_credentials import private_json

PROFILE_ID = 'production-pi-v1'
KIND = 'pi_acp_production'
FIELDS = frozenset(('schema_version', 'enabled', 'profile_id', 'adapter_kind', 'authorization_ref',
    'provider_id', 'model_id', 'endpoint', 'credential_source', 'egress_policy_id', 'sandbox',
    'max_cost_usd', 'hard_timeout_seconds'))


def endpoint(value):
    if type(value) is not str:
        raise ValueError('invalid production endpoint')
    url = urlsplit(value)
    if (url.scheme != 'https' or not url.hostname or url.port not in (None, 443)
            or url.username or url.password or url.query or url.fragment
            or url.path != '/v1' or not re.fullmatch('[a-z0-9.-]+', url.hostname)
            or url.hostname.endswith('.') or url.hostname == 'localhost'):
        raise ValueError('invalid production endpoint')
    try:
        ipaddress.ip_address(url.hostname)
    except ValueError:
        return url.hostname
    raise ValueError('production requires an exact reviewed DNS name')


class ProductionPiProfile:
    """Independent of LocalPiProfile. Host-only reviewed allowlist and permit."""
    native_load_verified = False
    profile_id = PROFILE_ID
    adapter_kind = KIND

    def __init__(self, configuration, *, reviewed, permit, snapshot=None):
        try:
            c = copy.deepcopy(configuration)
            if (type(c) is not dict or set(c) != FIELDS or type(c['schema_version']) is not int
                    or c['schema_version'] != 1 or c['enabled'] is not True
                    or c['profile_id'] != PROFILE_ID or c['adapter_kind'] != KIND
                    or c['sandbox'] not in ('read-only', 'workspace-write')
                    or type(c['hard_timeout_seconds']) is not int
                    or not 1 <= c['hard_timeout_seconds'] <= 60):
                raise ValueError
            host = endpoint(c['endpoint'])
            for name in ('provider_id', 'model_id', 'authorization_ref', 'egress_policy_id'):
                if type(c[name]) is not str or not re.fullmatch('[A-Za-z0-9_.-]{1,80}', c[name]):
                    raise ValueError
            # Host review table is explicit input, never read from a task or shell.
            approval = reviewed[(c['provider_id'], c['model_id'])]
            if (set(approval) != {'endpoint', 'egress_policy_id', 'input_tokens', 'output_tokens',
                                  'cost_microusd', 'addresses'}
                    or approval['endpoint'] != c['endpoint']
                    or approval['egress_policy_id'] != c['egress_policy_id']
                    or not callable(permit)):
                raise ValueError
            amount = Decimal(c['max_cost_usd'])
            if not isinstance(c['max_cost_usd'], str) or not amount.is_finite() or not 0 < amount <= Decimal('0.05'):
                raise ValueError
            micros = amount * 1_000_000
            if micros != int(micros):
                raise ValueError
            for name in ('input_tokens', 'output_tokens', 'cost_microusd'):
                if type(approval[name]) is not int or approval[name] < 1:
                    raise ValueError
            if (approval['input_tokens'] > 64000 or approval['output_tokens'] > 128
                    or approval['cost_microusd'] > int(micros)):
                raise ValueError
            addresses = approval['addresses']
            if not isinstance(addresses, (tuple, list)) or not addresses or len(addresses) > 16:
                raise ValueError
            if len(set(addresses)) != len(addresses):
                raise ValueError
            if any(not ipaddress.ip_address(value).is_global for value in addresses):
                raise ValueError
            source = c['credential_source']
            if type(source) is not dict or source.get('kind') not in ('native_auth', 'environment'):
                raise ValueError
            allowed = {'kind', 'name'} if source['kind'] == 'environment' else {'kind', 'path', 'provider'}
            if set(source) != allowed:
                raise ValueError
            if source['kind'] == 'environment' and not re.fullmatch('[A-Z][A-Z0-9_]{1,63}', source['name']):
                raise ValueError
            if source['kind'] == 'native_auth' and (source['provider'] != c['provider_id']
                    or not Path(source['path']).is_absolute()):
                raise ValueError
            if snapshot is not None and (not isinstance(snapshot,(tuple,list)) or len(snapshot)!=2
                    or not Path(snapshot[0]).is_absolute() or not re.fullmatch('[0-9a-f]{40}',snapshot[1])):
                raise ValueError
            if permit(c['authorization_ref'],copy.deepcopy(c)) is not True:
                raise ValueError
            self._configuration, self._approval = c, copy.deepcopy(approval)
            self.host, self.max_cost_microusd = host, int(micros)
            self._permit = permit
            self._snapshot = tuple(map(str, snapshot)) if snapshot else None
            self._toolchain_pin = None
            self.proxy = None
            self._check = None
            self._control_socket = None
            self._workflow_directory = None
            self._configuration_source = None
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise ValueError('production profile rejected') from None

    @classmethod
    def from_file(cls, path, *, reviewed, permit, forbidden=(), snapshot=None):
        result = cls(private_json(path, forbidden=forbidden, limit=65536), reviewed=reviewed, permit=permit,snapshot=snapshot)
        result._configuration_source = str(Path(path).resolve())
        return result

    @property
    def configuration(self):
        return copy.deepcopy(self._configuration)

    @property
    def approval(self):
        return copy.deepcopy(self._approval)

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps({'configuration': self._configuration,
            'approval': self._approval, 'snapshot': self._snapshot,
            'toolchain': self._toolchain_pin, 'workflow_directory': self._workflow_directory}, sort_keys=True).encode()).hexdigest()

    def recheck_permit(self):
        if self._permit(self._configuration['authorization_ref'], self.configuration) is not True:
            raise ValueError('production permit revoked')

    def launch(self, runtime, project, sandbox):
        from pi_session_profile import Launch
        from pi_toolchain import ROOT, prepare_toolchain, write_launch, clean_environment, production_sandbox
        from pi_acp_compat import derive_directory
        from pi_production_egress import EgressProxy
        from scripts.release_snapshot import verify_snapshot
        from transport import _private_save
        self.recheck_permit()
        if self._configuration_source and any(Path(self._configuration_source).is_relative_to(Path(p).resolve())
                for p in (runtime,project) if p is not None):
            raise ValueError('production configuration enters model workspace or runtime')
        if (not self._snapshot or len(self._snapshot) != 2 or not self._check or not self._control_socket
                or sandbox != self._configuration['sandbox']):
            raise ValueError('production requires an explicit sealed snapshot and owned host')
        root, commit = self._snapshot
        if Path(root).resolve() != Path(__file__).resolve().parent or not re.fullmatch('[0-9a-f]{40}', commit):
            raise ValueError('production code must execute from the selected snapshot')
        verify_snapshot(Path(root), commit)
        runtime, project = Path(runtime).resolve(), Path(project).resolve(strict=True)
        tools = prepare_toolchain(project, runtime)
        self._toolchain_pin = tools.mapping()
        for name in ('home','native','sessions','tmp'):
            (runtime/name).mkdir(mode=0o700)
        adapter = derive_directory(runtime/'adapter',role='coordinator')
        launcher = write_launch(runtime,project,'coordinator',tools,production=True)
        c, a = self.configuration, self.approval
        self.proxy = EgressProxy(self.host,a['addresses'],check=self._check)
        try:
            port = self.proxy.start()
            models = {'providers':{c['provider_id']:{'baseUrl':c['endpoint'],
                'api':'openai-completions','apiKey':'${ATO_PI_PROVIDER_KEY}',
                'models':[{'id':c['model_id'],'name':c['model_id'],'input':['text'],
                    'reasoning':False,'cost':dict(input=0,output=0,cacheRead=0,cacheWrite=0),
                    'contextWindow':a['input_tokens'],'maxTokens':a['output_tokens']}]}}}
            _private_save(runtime/'native/models.json',models)
            _private_save(runtime/'native/settings.json',dict(quietStartup=True,retry={'enabled':False},
                providerRetry={'maxRetries':0},compaction={'enabled':False}))
            _private_save(runtime/'trusted/production.json',dict(profile_hash=self.fingerprint,
                provider_id=c['provider_id'],model_id=c['model_id'],endpoint=c['endpoint'],
                input_tokens=a['input_tokens'],output_tokens=a['output_tokens']))
            policy = production_sandbox(runtime,project,tools.node,sources=ROOT,proxy_port=port,
                control_socket=self._control_socket,sandbox=sandbox,workflow=self._workflow_directory,
                pi_root=tools.package_root,pi_entry=tools.pi)
            (runtime/'sandbox.sb').write_text(policy)
            env = dict(clean_environment(home=runtime/'home'),PI_CODING_AGENT_DIR=str(runtime/'native'),
                PI_TELEMETRY='0',PI_OFFLINE='1',CTASK_PI_ACP_STATE_DIR=str(runtime/'adapter-state'),
                PI_ACP_PI_COMMAND=str(launcher),CTASK_PI_RUNTIME=str(runtime),CTASK_AGENT_REPO=str(project),
                CTASK_AGENT_SANDBOX=sandbox,CTASK_PI_MODEL=c['provider_id']+'/'+c['model_id'],
                TMPDIR=str(runtime/'tmp'),JITI_CACHE=str(runtime/'jiti'),ATO_PI_PROXY_PORT=str(port),
                ATO_PI_CONTROL_SOCKET=str(self._control_socket),ATO_PI_PROFILE_HASH=self.fingerprint)
            return Launch(('/usr/bin/sandbox-exec','-f',str(runtime/'sandbox.sb'),str(tools.node),str(adapter)),
                env,'production-pi-v1:'+self.fingerprint,provenance_data=dict(profile_id=PROFILE_ID,
                    profile_hash=self.fingerprint,snapshot_commit=commit,toolchain=tools.mapping(),
                    actual_cli_qualified=False,cloud_model_qualified=False))
        except BaseException:
            self.close()
            raise

    def configure_host(self, check, control_socket, workflow):
        if self._check or not callable(check):
            raise ValueError('production host binding is immutable')
        self._check, self._control_socket = check, str(control_socket)
        self._workflow_directory = str(Path(workflow).resolve())

    def acquire_secret(self, runtime, project):
        from pi_production_credentials import acquire
        self.recheck_permit()
        return acquire(self._configuration['credential_source'],forbidden=(runtime,project,Path(__file__).parent))

    def close(self):
        if self.proxy:
            self.proxy.close()
