from dataclasses import asdict,replace
import errno
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from hub_contract import HubAgentSpec,HubTurnRequest
from hub_adapter_agy import AgyHubAdapter
from hub_adapter_claude import ClaudeHubAdapter
from hub_adapter_codex import CodexHubAdapter
from hub_registry import HubRegistry
import hub_sandbox
import hub_worktree
from hub_support import repository,verdict

ROOT=Path(__file__).resolve().parents[1]
CLASSES=(AgyHubAdapter,ClaudeHubAdapter,CodexHubAdapter)


def adapter_fixture(root, klass, config):
    home=root/('home-'+klass.kind);home.mkdir(mode=0o700)
    (home/'.gemini/antigravity-cli').mkdir(parents=True,mode=0o700)
    (home/'.claude').mkdir(mode=0o700);(home/'.codex').mkdir(mode=0o700)
    file=home/'fixture.json';file.write_text(json.dumps(config));file.chmod(0o600)
    entry=root/('fake-'+klass.kind)
    entry.write_text('#!'+str(Path(sys.executable).resolve())+'\n'+(ROOT/'tests/fixtures/hub_cli_fixture.py').read_text());entry.chmod(0o700)
    return klass(entry,home=home)


class HubAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='ato-h1-adapter-');self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.repo=repository(self.root);self.number=0
        self.secret='synthetic-h1-secret-must-not-escape-7391'
        scope=patch.dict(os.environ,dict(H1_SECRET=self.secret,CTASK_SESSION_REF=self.secret,
            CTASK_SESSION_TOKEN=self.secret,ATO_ACTOR_FILE=self.secret,PYTHONPATH=self.secret,
            NODE_OPTIONS=self.secret,DYLD_INSERT_LIBRARIES=self.secret,HTTPS_PROXY=self.secret))
        scope.start();self.addCleanup(scope.stop)

    def request(self,adapter,**options):
        self.number+=1;rt=self.root/('turn-'+str(self.number));rt.mkdir(mode=0o700)
        return HubTurnRequest(HubAgentSpec(adapter.kind),self.repo,rt,'Synthetic review',
            snapshot=lambda:hub_worktree.snapshot_integrity(self.repo),stop=lambda:False,timeout_seconds=3,**options)

    def test_O2_three_adapters_same_strict_first_and_resume_contract(self):
        for klass in CLASSES:
            with self.subTest(kind=klass.kind):
                adapter=adapter_fixture(self.root,klass,dict(verdict=verdict(),sentinel=self.secret))
                doctor=adapter.doctor();self.assertTrue(doctor['available'],doctor)
                first=adapter.run_turn(self.request(adapter));self.assertEqual(first.status,'success',first)
                second=adapter.run_turn(self.request(adapter,resume_session_id=first.session_id))
                self.assertEqual(second.status,'success',second)
                if klass.kind=='claude':
                    self.assertNotEqual(first.session_id,second.session_id)
                    self.assertEqual(second.metadata['session_mode_effective'],'fresh_only')
                    self.assertEqual(adapter.capabilities().values['session_resume']['state'],'unknown')
                else:self.assertEqual(first.session_id,second.session_id)
                self.assertIn(first.session_id,adapter.attach_argv(first.session_id,self.repo))
                self.assertNotIn(self.secret,json.dumps([doctor,asdict(first),asdict(second)]))

    def test_doctor_error_codes_survive_without_launch_or_diagnostics_leak(self):
        for klass in CLASSES:
            folder=self.root/klass.kind;folder.mkdir()
            adapter=adapter_fixture(folder,klass,dict(verdict=verdict()))
            for code in ('EXECUTABLE_UNAVAILABLE','TIMEOUT','VERSION_MISMATCH'):
                with self.subTest(kind=klass.kind,code=code):
                    diagnostic=dict(available=False,code=code,stderr=self.secret+' /private/fixture-path')
                    with patch.object(adapter,'doctor',return_value=diagnostic) as doctor,patch(
                            'hub_sandbox.turn_context',side_effect=AssertionError('must not launch')):
                        result=adapter.run_turn(self.request(adapter))
                    doctor.assert_called_once_with()
                    self.assertEqual(result.code,code)
                    encoded=json.dumps(asdict(result))
                    self.assertNotIn(self.secret,encoded)
                    self.assertNotIn('/private/fixture-path',encoded)
                    self.assertNotIn('stderr',encoded)

    def test_fixup_agy_defers_prompt_size_to_exec(self):
        adapter = adapter_fixture(self.root, AgyHubAdapter, dict(verdict=verdict()))
        request = replace(self.request(adapter), prompt='界' * 40000)
        result = adapter.run_turn(request)
        self.assertEqual(result.code, 'OK', result)

        doctor = adapter.doctor()
        oversized = replace(self.request(adapter), prompt=request.prompt)
        original = hub_sandbox.subprocess.Popen
        attempts = []

        def exec_size_error(argv, *args, **kwargs):
            if str(adapter.executable) in argv and '-p' in argv:
                attempts.append(argv[argv.index('-p') + 1] == oversized.prompt)
                raise OSError(errno.E2BIG, self.secret)
            return original(argv, *args, **kwargs)

        with patch.object(adapter, 'doctor', return_value=doctor), \
                patch.object(hub_sandbox.subprocess, 'Popen', side_effect=exec_size_error):
            result = adapter.run_turn(oversized)
        self.assertEqual(attempts, [True])
        self.assertEqual((result.status, result.code), ('blocked', 'REVIEW_PROMPT_TOO_LARGE'))
        self.assertNotIn(self.secret, repr(result))

    def test_O2_each_malformed_auth_failure_timeout_is_closed(self):
        for klass in CLASSES:
            folder=self.root/klass.kind;folder.mkdir()
            adapter=adapter_fixture(folder,klass,dict(verdict=verdict()))
            for mode in ('missing','bad-json','exit','auth','stall'):
                with self.subTest(kind=klass.kind,mode=mode):
                    (adapter.home/'fixture.json').write_text(json.dumps(dict(verdict=verdict(),mode=mode,sentinel=self.secret)))
                    request=self.request(adapter)
                    if mode=='stall':request=replace(request,timeout_seconds=1)
                    result=adapter.run_turn(request)
                    self.assertNotEqual(result.status,'success',result)
                    if mode=='auth':self.assertEqual(result.status,'auth_blocked',result)
                    if mode=='stall':self.assertEqual(result.status,'timeout',result)
                    self.assertNotIn(self.secret,json.dumps(asdict(result)))

    def test_O8_secret_in_structured_result_refuses_and_private_stderr_is_bounded(self):
        for klass in CLASSES:
            folder=self.root/klass.kind;folder.mkdir()
            adapter=adapter_fixture(folder,klass,dict(verdict=verdict(),mode='secret-output',sentinel=self.secret))
            request=self.request(adapter);result=adapter.run_turn(request)
            self.assertEqual(result.code,'SECRET_IN_REVIEW',result)
            error=Path(request.runtime_dir)/'stderr.log'
            self.assertEqual(error.stat().st_mode&0o777,0o600);self.assertLessEqual(error.stat().st_size,1024*1024)
            self.assertNotIn(self.secret,json.dumps(asdict(result)))

    def test_O2_AGY_exact_SUCCESS_and_requested_session_required(self):
        adapter=adapter_fixture(self.root,AgyHubAdapter,dict(verdict=verdict(),mode='bad-status'))
        self.assertNotEqual(adapter.run_turn(self.request(adapter)).status,'success')
        (adapter.home/'fixture.json').write_text(json.dumps(dict(verdict=verdict(),mode='wrong-session')))
        self.assertEqual(adapter.run_turn(self.request(adapter,resume_session_id='original')).code,'SESSION_MISMATCH')

    def test_O3_all_adapters_mandatory_post_snapshot_and_native_Codex_tripwire(self):
        adapter=adapter_fixture(self.root,CodexHubAdapter,dict(verdict=verdict(),mode='mutate'))
        result=adapter.run_turn(self.request(adapter))
        self.assertEqual(result.code,'REVIEWER_MUTATION_VIOLATION')
        for klass in CLASSES:
            folder=self.root/klass.kind;folder.mkdir()
            peer=adapter_fixture(folder,klass,dict(verdict=verdict()))
            calls=[]
            def snapshot():calls.append(1);return dict(sequence=len(calls))
            request=replace(self.request(peer),snapshot=snapshot)
            self.assertEqual(peer.run_turn(request).code,'REVIEWER_MUTATION_VIOLATION')
            self.assertEqual(len(calls),2)

    def test_O2_registry_requires_code_owned_kinds_and_qualified_resume(self):
        registry=HubRegistry()
        self.assertEqual(registry.kinds(),('agy','claude','codex'))
        with self.assertRaises(ValueError):registry.get('module:arbitrary')
        peer=adapter_fixture(self.root,ClaudeHubAdapter,dict(verdict=verdict()))
        self.assertEqual(peer.run_turn(self.request(peer,session_mode='require_resume')).code,'RESUME_NOT_QUALIFIED')
        with self.assertRaises(ValueError):peer.attach_argv('--flag',self.repo)

    def test_O8_native_output_flood_is_bounded_and_owned_group_is_closed(self):
        adapter=adapter_fixture(self.root,CodexHubAdapter,dict(verdict=verdict(),mode='flood'))
        request=self.request(adapter);result=adapter.run_turn(request)
        self.assertEqual(result.code,'OUTPUT_LIMIT',result)
        self.assertLessEqual((Path(request.runtime_dir)/'stderr.log').stat().st_size,1024*1024)

    def test_O3_unknown_recycled_group_cannot_be_signalled(self):
        import hub_sandbox
        process=type('Process',(),dict(pid=12345))()
        owner=hub_sandbox.OwnedGroup(process,'original',{12345:'original'})
        with patch.object(hub_sandbox,'group_members',return_value={12345:'recycled'}),patch.object(hub_sandbox.os,'killpg') as kill:
            self.assertFalse(owner.close());kill.assert_not_called()
