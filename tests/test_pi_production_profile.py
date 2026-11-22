"""O1/O2/O3/O9: synthetic credentials and private workflows only."""
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import pickle
import tempfile
import tomllib
import unittest

from ato_core import digest
from pi_production_credentials import acquire, private_json, SecretLease
from pi_production_fence import ProductionFence
from pi_production_profile import ProductionPiProfile
from scripts import prepare_acp_process_candidate

ROOT = Path(__file__).resolve().parents[1]
SENTINEL = 'synthetic-key-never-persisted'


def configuration():
    return dict(schema_version=1, enabled=True, profile_id='production-pi-v1',
        adapter_kind='pi_acp_production', authorization_ref='fixture-permit',
        provider_id='synthetic', model_id='scripted', endpoint='https://provider.example/v1',
        credential_source=dict(kind='environment', name='FIXTURE_KEY'),
        egress_policy_id='fixture-egress', sandbox='read-only', max_cost_usd='0.05', hard_timeout_seconds=60)


def reviewed():
    return {('synthetic', 'scripted'): dict(endpoint='https://provider.example/v1',
        egress_policy_id='fixture-egress', input_tokens=1024, output_tokens=128,
        cost_microusd=50000, addresses=['93.184.216.34'])}


class ProductionProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def profile(self, c=None, **kwargs):
        return ProductionPiProfile(c or configuration(), reviewed=kwargs.get('reviewed', reviewed()),
                                   permit=kwargs.get('permit', lambda *_: True))

    def private(self, name, data):
        p = self.root/name
        p.write_text(json.dumps(data)); p.chmod(0o600)
        return p

    def test_explicit_profile_is_distinct_and_no_launch_until_transport_exists(self):
        from pi_session_profile import LocalPiProfile
        p = self.profile()
        self.assertNotIsInstance(p, LocalPiProfile)
        self.assertFalse(p.native_load_verified)
        self.assertEqual(p.adapter_kind, 'pi_acp_production')
        # Commit A is intentionally not capable of accidentally starting Pi.
        if not (ROOT/'pi_production_exec.py').exists():
            with self.assertRaises(ValueError): p.launch(None, None, None)

    def test_closed_configuration_missing_unknown_wrong_types_and_identity(self):
        for name, value in [('enabled',False),('schema_version',True),('adapter_kind','pi_acp'),
                ('profile_id','local'),('hard_timeout_seconds',True),('max_cost_usd','NaN'),
                ('max_cost_usd','0.051'),('sandbox','danger-full-access')]:
            c=configuration();c[name]=value
            with self.subTest(name=name), self.assertRaises(ValueError):self.profile(c)
        c=configuration();c['api_key']=SENTINEL
        with self.assertRaises(ValueError):self.profile(c)
        c=configuration();del c['enabled']
        with self.assertRaises(ValueError):self.profile(c)
        with self.assertRaises(ValueError):self.profile(permit=lambda *_: False)

    def test_endpoint_and_review_policy_cannot_be_redirected(self):
        for endpoint in ('http://provider.example/v1','https://x:y@provider.example/v1',
                         'https://127.0.0.1/v1','https://provider.example/v1?api_key=synthetic',
                         'https://other.example/v1','https://provider.example:444/v1'):
            c=configuration();c['endpoint']=endpoint
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):self.profile(c)
        for address in ('127.0.0.1','::1','169.254.169.254','10.0.0.1','0.0.0.0'):
            r=reviewed();r[('synthetic','scripted')]['addresses']=[address]
            with self.assertRaises(ValueError):self.profile(reviewed=r)

    def test_source_schema_cannot_contain_secrets_commands_or_oauth(self):
        for source in (dict(kind='oauth', token=SENTINEL),dict(kind='environment',name='bad'),
                dict(kind='environment',name='FIXTURE_KEY',key=SENTINEL),
                dict(kind='native_auth',path='relative',provider='synthetic')):
            c=configuration();c['credential_source']=source
            with self.assertRaises(ValueError):self.profile(c)

    def test_profile_identity_is_immutable_to_callers_and_bound_to_policy(self):
        c=configuration();p=self.profile(c);before=p.fingerprint
        c['endpoint']='changed';p.configuration['endpoint']='changed'
        self.assertEqual(p.fingerprint,before)
        c=configuration();c['authorization_ref']='another'
        self.assertNotEqual(self.profile(c).fingerprint,before)
        permit={'on':True};p=self.profile(permit=lambda *_:permit['on']);permit['on']=False
        with self.assertRaises(ValueError):p.recheck_permit()

    def test_private_config_duplicate_keys_unsafe_mode_alias_and_bounds(self):
        p=self.private('config.json',configuration())
        self.assertEqual(ProductionPiProfile.from_file(p,reviewed=reviewed(),permit=lambda *_:True).profile_id,'production-pi-v1')
        p.write_text('{"enabled":true,"enabled":false}')
        with self.assertRaises(ValueError):private_json(p)
        p.write_text('{}');p.chmod(0o644)
        with self.assertRaises(ValueError):private_json(p)
        p.chmod(0o600);alias=self.root/'alias';alias.symlink_to(p)
        with self.assertRaises(ValueError):private_json(alias)
        with self.assertRaises(ValueError):private_json(p,forbidden=[self.root])
        p.write_bytes(b' ' * 65537)
        with self.assertRaises(ValueError):private_json(p)

    def test_env_is_one_explicit_variable_no_search_or_fallback(self):
        source=dict(kind='environment',name='FIXTURE_KEY')
        for env in ({},{'FIXTURE_KEY':''},{'OTHER_KEY':SENTINEL}):
            with self.assertRaises(ValueError):acquire(source,environment=env)
        lease=acquire(source,environment={'FIXTURE_KEY':SENTINEL,'OTHER_KEY':'never copied'})
        self.assertEqual(lease.environment(),{'ATO_PI_PROVIDER_KEY':SENTINEL})
        self.assertNotIn(SENTINEL,repr(lease))
        with self.assertRaises(TypeError):pickle.dumps(lease)
        with self.assertRaises(TypeError):json.dumps(lease)
        lease.close()
        with self.assertRaises(ValueError):lease.environment()

    def test_native_auth_static_selected_provider_read_only(self):
        p=self.private('auth.json',{'synthetic':{'type':'api_key','key':SENTINEL},'other':{'type':'oauth','refresh':'unused'}})
        before=p.read_bytes()
        lease=acquire(dict(kind='native_auth',path=str(p),provider='synthetic'))
        self.assertEqual(lease.environment()['ATO_PI_PROVIDER_KEY'],SENTINEL)
        lease.close();self.assertEqual(p.read_bytes(),before)
        for selected in ({'type':'oauth','key':SENTINEL},{'type':'api_key','key':'!echo synthetic'},
                         {'type':'api_key','key':'${UNSELECTED}'},{'type':'api_key','key':SENTINEL,'extra':1}):
            p.write_text(json.dumps({'synthetic':selected}))
            with self.assertRaises(ValueError) as caught:acquire(dict(kind='native_auth',path=str(p),provider='synthetic'))
            self.assertNotIn(SENTINEL,str(caught.exception))

    def test_hardlink_and_untrusted_credentials_rejected_without_value_in_error(self):
        p=self.private('auth.json',{'synthetic':{'type':'api_key','key':SENTINEL}})
        os.link(p,self.root/'linked')
        with self.assertRaises(ValueError):acquire(dict(kind='native_auth',path=str(p),provider='synthetic'))
        for secret in ('',' bad','bad\nvalue','!command','${NAME}'):
            with self.assertRaises(ValueError):SecretLease(secret)

    def test_module_distribution_and_generic_vendor_free_core_ast(self):
        modules=tomllib.loads((ROOT/'pyproject.toml').read_text())['tool']['setuptools']['py-modules']
        for name in ('pi_production_credentials','pi_production_fence','pi_production_profile'):
            self.assertIn(name,modules);self.assertIn(name+'.py',prepare_acp_process_candidate.FILES)
        expected=json.loads((ROOT/'tests/fixtures/w2-core-ast.json').read_text())
        for name,sha in expected.items():
            tree=ast.parse((ROOT/(name+'.py')).read_text())
            self.assertEqual(hashlib.sha256(ast.dump(tree).encode()).hexdigest(),sha,name)
            for node in ast.walk(tree):
                if isinstance(node,ast.Import):names=[x.name for x in node.names]
                elif isinstance(node,ast.ImportFrom):names=[node.module or '']
                else:continue
                self.assertFalse(any(x.startswith(('pi_','codex_','appserver','acp.','anthropic','openai')) for x in names),name)


class ProductionFenceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.clock=[10.0];self.calls=[]
        self.contract={'fixture':'immutable'}
        self.state=dict(repo=str(self.root),contract_hash=digest(self.contract),status='running',execution_revision=7)
        self.save()
        self.binding=dict(workflow_id=str(self.root),project=str(self.root),contract_hash=digest(self.contract),generation=7)

    def save(self):
        (self.root/'state.json').write_text(json.dumps(self.state))
        (self.root/'contract.json').write_text(json.dumps(self.contract))

    def fence(self):
        return ProductionFence(self.binding,owner_guard=lambda:self.calls.append('guard'),profile_hash='fixture',
            max_cost_microusd=50000,hard_timeout_seconds=60,clock=lambda:self.clock[0])

    def test_every_action_calls_owner_and_cost_is_not_refunded(self):
        f=self.fence();f.authorize('fixture');f.authorize('fixture',request=True,cost_microusd=50000)
        self.assertEqual(self.calls,['guard','guard'])
        self.assertEqual(f.snapshot()['reserved_microusd'],50000)
        with self.assertRaises(ValueError):f.authorize('fixture',request=True,cost_microusd=1)
        self.assertEqual(f.snapshot()['requests'],1)

    def test_stop_contract_generation_profile_and_deadline_revoke(self):
        for mode in ('stop','contract','generation','profile','deadline'):
            self.state['execution_revision']=7;self.contract={'fixture':'immutable'};self.save()
            (self.root/'STOP').unlink(missing_ok=True);self.clock[0]=10
            f=self.fence();profile='fixture'
            if mode=='stop':(self.root/'STOP').touch()
            if mode=='contract':(self.root/'contract.json').write_text('{}')
            if mode=='generation':self.state['execution_revision']=8;self.save()
            if mode=='profile':profile='wrong'
            if mode=='deadline':self.clock[0]=70
            with self.subTest(mode=mode),self.assertRaises(ValueError):f.authorize(profile)
            self.assertTrue(f.snapshot()['revoked'])

    def test_budget_guard_failure_is_sticky_and_bad_money_never_admitted(self):
        f=self.fence()
        def expired():raise ValueError('existing budget guard rejected')
        f.owner_guard=expired
        with self.assertRaises(ValueError):f.authorize('fixture')
        self.assertTrue(f.revoked)
        for cost in (True,0,-1,1.1,50001):
            with self.assertRaises(ValueError):self.fence().authorize('fixture',request=True,cost_microusd=cost)
