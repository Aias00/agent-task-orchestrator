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

    def __init__(self, configuration, *, reviewed, permit):
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
                    or not callable(permit) or permit(c['authorization_ref'], copy.deepcopy(c)) is not True):
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
            if (approval['input_tokens'] > 1024 or approval['output_tokens'] > 128
                    or approval['cost_microusd'] > int(micros)):
                raise ValueError
            addresses = approval['addresses']
            if not isinstance(addresses, (tuple, list)) or not addresses or len(addresses) > 16:
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
            self._configuration, self._approval = c, copy.deepcopy(approval)
            self.host, self.max_cost_microusd = host, int(micros)
            self._permit = permit
        except (KeyError, TypeError, ValueError, InvalidOperation):
            raise ValueError('production profile rejected') from None

    @classmethod
    def from_file(cls, path, *, reviewed, permit, forbidden=()):
        return cls(private_json(path, forbidden=forbidden, limit=65536), reviewed=reviewed, permit=permit)

    @property
    def configuration(self):
        return copy.deepcopy(self._configuration)

    @property
    def approval(self):
        return copy.deepcopy(self._approval)

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps({'configuration': self._configuration,
            'approval': self._approval}, sort_keys=True).encode()).hexdigest()

    def recheck_permit(self):
        if self._permit(self._configuration['authorization_ref'], self.configuration) is not True:
            raise ValueError('production permit revoked')

    def launch(self, runtime, project, sandbox):
        # Deliberately unlaunchable until the separately reviewed B/C layers exist.
        raise ValueError('production transport not qualified')
