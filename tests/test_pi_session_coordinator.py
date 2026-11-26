"""Bounded local ACP subprocess qualification. No real Pi/cloud claims.

Codex CLI/index/app-server are explicitly unavailable. Every project, workflow
binding, user value, native identity and configuration below is synthetic.
"""
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import agent_registry as registry
from ato_adapters import agents
from ato_adapters import codex_session_adapter
from ato_workflow import session_router
from ato_workflow import transport
from ato_pi.pi_session_coordinator import PiCoordinator, PiSessionAdapter, locate, private_read, provision_path
from ato_pi.pi_session_profile import Launch, LocalPiProfile, SOURCE, ADAPTER_SHA256, derive_coordinator
from pi_toolchain_support import tools_fixture

FIXTURE = Path(__file__).parent/'fixtures/pi_coordinator_fixture.py'


class FixtureProfile:
    native_load_verified = False

    def launch(self, runtime, project, sandbox):
        return Launch((sys.executable, str(FIXTURE), str(runtime)),
            dict(HOME=str(runtime), PATH='/nonexistent', CODEX_HOME=str(runtime/'codex-unavailable')),
            'synthetic ACP subprocess fixture; not actual Pi')


class PiCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ato-pi-coordinator-')
        self.root = Path(self.tmp.name).resolve()
        self.project = self.root/'project'
        self.project.mkdir()
        self.env = patch.dict(os.environ, dict(HOME=str(self.root/'empty-home'),
            ATO_HOME=str(self.root/'ato'), CODEX_HOME=str(self.root/'codex-unavailable'),
            CTASK_CODEX_BIN='/codex-unavailable', CTASK_QUEUE_BIN='/codex-unavailable'), clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.tmp.cleanup)
        self.probes = [patch.object(codex_session_adapter, method,
            side_effect=AssertionError('Codex unavailable: '+method))
            for method in ('session_path', 'queue', 'create_session', 'current')]
        from ato_adapters import appserver
        self.probes.append(patch.object(appserver, 'Client', side_effect=AssertionError('Codex app-server unavailable')))
        for probe in self.probes:
            probe.start()
            self.addCleanup(probe.stop)
        self.adapter = PiSessionAdapter(kind='fixture_pi_'+uuid.uuid4().hex)
        registry.register(self.adapter)
        self.binding = dict(workflow_id='synthetic-workflow', contract_hash='synthetic-frozen-contract', generation=7)
        self.hosts = []
        self.host, self.reference = await self.create()

    async def asyncTearDown(self):
        for host in reversed(self.hosts):
            await host.close()

    async def create(self, **options):
        host, reference = await self.adapter.create_managed_session(FixtureProfile(), self.project,
            sandbox='workspace-write', bindings=self.binding, timeout=.7, **options)
        self.hosts.append(host)
        return host, reference

    def envelope(self, key='message-1', **changes):
        message = dict(id=key, canonical_id=key, correlation_id=key, task_id=self.binding['workflow_id'],
            contract_hash=self.binding['contract_hash'], generation='7', protocol=2, body='fixture hello', expires_at=None)
        message.update(changes)
        return message

    def endpoint(self):
        return dict(kind=self.adapter.kind, session_id=self.reference.identity.native_id, project=str(self.project))

    async def send(self, key='message-1', **changes):
        result = await self.host.dispatch(self.endpoint(), self.envelope(key, **changes))
        await self.host.drain()
        return result

    def events(self, key='message-1', cursor=None, generation=7):
        cursor = cursor or dict(host_id=self.host.host_id, sequence=0)
        return session_router.observe_v2(self.reference.reference, key, generation=generation, cursor=cursor)

    def actor(self):
        proof = registry.ActorProof('pi-host-token', self.host.host_id, self.host.token)
        return session_router.authenticate_actor(self.reference.reference, proof,
            role='session_host', project=str(self.project), **self.binding)

    def checkpoint(self):
        artifact = self.project/'fixture-artifact.txt'
        return dict(full_goal='Complete synthetic full objective, including independent checks',
            current_files=[dict(path=artifact.name, sha256=hashlib.sha256(artifact.read_bytes()).hexdigest())],
            previous_results=[dict(message='message-1', status='turn_completed', workflow_accepted=False)],
            cumulative_history=['original attempt 1 retained; no automatic retry or acceptance'])

    async def test_creation_exact_native_identity_and_opaque_reference(self):
        native = json.loads((self.host.runtime/'native-identity.json').read_text())
        self.assertEqual(native['nativeId'], self.reference.identity.native_id)
        self.assertEqual(native['project'], str(self.project))
        resolved, adapter = session_router.resolve_v2(self.reference.reference, self.project)
        self.assertEqual(resolved, self.reference)
        self.assertIs(adapter, self.adapter)
        self.assertFalse(self.adapter.verify_session('wrong-id', str(self.project)))
        other = self.root/'other'; other.mkdir()
        self.assertFalse(self.adapter.verify_session(native['nativeId'], str(other)))
        self.assertEqual(self.host.runtime.stat().st_mode & 0o077, 0)
        self.assertFalse(self.host.state['workflow_completed_by_end_turn'])

    async def test_two_turns_same_native_and_exact_tool_correlation(self):
        await self.send(body='tool first turn')
        await self.send('message-2', body='second turn')
        admissions = [json.loads(line) for line in (self.host.runtime/'admissions.jsonl').read_text().splitlines()]
        self.assertEqual(len(admissions), 2)
        self.assertEqual({x['session'] for x in admissions}, {self.reference.identity.native_id})
        self.assertEqual(len({x['turn'] for x in admissions}), 2)
        first, cursor = self.events()
        second, _ = self.events('message-2')
        self.assertEqual([e.kind for e in first], ['message_received', 'tool_started', 'tool_completed',
                                                 'response_started', 'response_completed', 'turn_completed'])
        self.assertEqual({e.turn_id for e in first}, {admissions[0]['turn']})
        self.assertEqual({e.turn_id for e in second}, {admissions[1]['turn']})
        self.assertEqual((self.project/'fixture-artifact.txt').read_text(), 'fixture artifact\n')
        self.assertEqual(self.events(cursor=cursor)[0], [])
        self.assertFalse(self.host.state['workflow_completed_by_end_turn'])

    async def test_durable_mailbox_end_to_end_without_codex(self):
        cursor = session_router.cursor(self.reference.reference)
        projection = transport.prepare_managed(self.reference.reference, 'tool mailbox', canonical_id='canonical-pi',
            phase='fixture', task_id=self.binding['workflow_id'], contract_hash=self.binding['contract_hash'],
            generation=7, project=str(self.project), source='synthetic local fixture authority')
        delivered = await asyncio.to_thread(transport.dispatch_managed, projection)
        self.assertEqual(delivered['mailbox_state'], 'delivered')
        await self.host.drain()
        events, _ = session_router.observe_v2(self.reference.reference, 'canonical-pi', generation=7, cursor=cursor)
        settled = transport.observe_managed(projection, events)
        self.assertEqual(settled['mailbox_state'], 'turn_completed')
        again = await asyncio.to_thread(transport.dispatch_managed, projection)
        self.assertEqual(again['mailbox_state'], 'turn_completed')
        self.assertEqual(len((self.host.runtime/'admissions.jsonl').read_text().splitlines()), 1)

    async def test_wrong_session_turn_missing_and_error_never_complete(self):
        for marker in ('wrong-session', 'wrong-turn', 'missing-native', 'native-error', 'sticky-error'):
            with self.subTest(marker=marker):
                if self.host.state['lifecycle'] != 'idle':
                    self.host, self.reference = await self.create()
                await self.send(body=marker)
                events, _ = self.events()
                self.assertNotIn('turn_completed', [e.kind for e in events])
                self.assertNotIn('response_completed', [e.kind for e in events])
                self.assertFalse(self.host.state['workflow_completed_by_end_turn'])

    async def test_disconnect_is_unknown_no_replay_and_no_idle_inference(self):
        await self.send(body='disconnect')
        self.assertEqual(self.host.state['turns']['message-1']['state'], 'delivery_unknown')
        again = await self.send(body='disconnect')
        self.assertEqual(again.state, 'delivery_unknown')
        self.assertEqual(len((self.host.runtime/'admissions.jsonl').read_text().splitlines()), 1)
        self.assertEqual(self.events()[0], [])
        self.assertEqual(session_router.activity_v2(self.reference.reference, generation=7).state.value, 'unknown')

    async def test_timeout_unknown_cannot_reconstruct_or_resend(self):
        actor = self.actor()
        await self.send(body='timeout')
        self.assertEqual((await self.send(body='timeout')).state, 'delivery_unknown')
        await self.host.close()
        checkpoint = dict(full_goal='synthetic goal', current_files=[], previous_results=[], cumulative_history=[])
        with self.assertRaisesRegex(ValueError, 'unknown delivery'):
            await self.adapter.reconstruct_session(self.reference, checkpoint, FixtureProfile(), actor=actor, **self.binding)
        self.assertEqual(len((self.host.runtime/'admissions.jsonl').read_text().splitlines()), 1)

    async def test_native_rpc_error_is_not_end_turn_and_raw_error_not_saved(self):
        await self.send(body='native-rpc-error')
        self.assertEqual(self.host.state['lifecycle'], 'disconnected_unknown')
        for path in self.host.runtime.glob('*.json*'):
            self.assertNotIn('SECRET_RAW_ERROR', path.read_text())
        self.assertNotIn('turn_completed', [e.kind for e in self.events()[0]])

    async def test_cancel_stops_only_owned_group_and_requires_host_actor(self):
        other = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(15)'],
                                 env={'HOME':str(self.root)}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            actor = self.actor()
            with self.assertRaises(ValueError):
                session_router.authorize_actor(actor, 'decision.approve', project=str(self.project), **self.binding)
            await self.host.dispatch(self.endpoint(), self.envelope(body='cancel-me'))
            await asyncio.sleep(.05)
            await asyncio.to_thread(self.adapter.cancel_session, self.reference.identity, actor=actor, **self.binding)
            self.assertIsNotNone(self.host.proc.returncode)
            self.assertIsNone(other.poll())
            self.assertEqual(self.host.state['lifecycle'], 'stopped')
            self.assertTrue(self.host.closed.is_set())
            self.assertNotIn('turn_completed', [e.kind for e in self.events()[0]])
        finally:
            other.terminate(); other.wait(timeout=3)

    async def test_actor_proof_environment_and_stale_generation_rejected(self):
        os.environ.update(CTASK_SESSION_REF=self.reference.reference, CTASK_SESSION_TOKEN=self.host.token)
        self.assertFalse(self.adapter.is_actor(self.reference.identity.native_id, str(self.project)))
        for role, token, generation in [('user', self.host.token, 7), ('origin', self.host.token, 7),
                                         ('session_host', 'forged', 7), ('session_host', self.host.token, 8)]:
            with self.subTest(role=role, generation=generation):
                with self.assertRaises(ValueError):
                    session_router.authenticate_actor(self.reference.reference,
                        registry.ActorProof('pi-host-token', self.host.host_id, token), role=role,
                        project=str(self.project), workflow_id=self.binding['workflow_id'],
                        contract_hash=self.binding['contract_hash'], generation=generation)
        self.assertEqual(self.actor().role.value, 'session_host')

    async def test_origin_proof_is_separate_and_cannot_become_user_or_lifecycle_authority(self):
        proof = self.adapter.origin_actor_proof(self.reference.identity,
            source='synthetic already-authorized coordinator fixture', **self.binding)
        self.assertNotEqual(proof.challenge, self.host.token)
        origin = session_router.authenticate_actor(self.reference.reference, proof, role='origin',
            project=str(self.project), **self.binding)
        self.assertTrue(session_router.authorize_actor(origin, 'question.ask', project=str(self.project), **self.binding))
        for action in ('decision.answer', 'decision.approve', 'criteria.acknowledge', 'session.cancel_owned'):
            with self.assertRaises(ValueError):
                session_router.authorize_actor(origin, action, project=str(self.project), **self.binding)
        with self.assertRaises(ValueError):
            session_router.authenticate_actor(self.reference.reference, proof, role='user',
                project=str(self.project), **self.binding)

    async def test_capability_advertisement_does_not_authorize_native_load(self):
        self.assertTrue(self.host.state['advertised_native_load'])
        self.assertEqual(session_router.capability_contract(self.reference.reference).state('session.load_resume').value,
                         'unsupported')
        with self.assertRaises(registry.CapabilityUnavailable):
            session_router.load_or_resume_v2(self.reference.reference, {}, actor=self.actor(), **self.binding)
        self.assertFalse((self.host.runtime/'admissions.jsonl').exists())

    async def test_restart_reconstruction_new_identity_preserves_full_checkpoint(self):
        await self.send(body='tool before restart')
        previous = self.reference
        checkpoint, actor = self.checkpoint(), self.actor()
        previous_pid = self.host.proc.pid
        old_journal = (self.host.runtime/'events.jsonl').read_bytes()
        await self.host.close()
        old_journal = (self.host.runtime/'events.jsonl').read_bytes()
        host, reference = await self.adapter.reconstruct_session(previous, checkpoint, FixtureProfile(),
            actor=actor, **self.binding)
        self.hosts.append(host)
        self.assertNotEqual(reference.identity.native_id, previous.identity.native_id)
        self.assertNotEqual(host.proc.pid, previous_pid)
        recovery = host.state['recovery']
        self.assertEqual(recovery['mode'], 'checkpoint_reconstruction')
        self.assertEqual(recovery['previous_reference'], previous.to_mapping())
        self.assertEqual(recovery['checkpoint'], checkpoint)
        self.assertFalse((host.runtime/'admissions.jsonl').exists())
        self.assertEqual((self.host.runtime/'events.jsonl').read_bytes(), old_journal)
        self.host, self.reference = host, reference
        await self.send('after-restart', body='new explicit request after reconstruction')
        self.assertIn('turn_completed', [e.kind for e in self.events('after-restart')[0]])
        material = host._prompt_material('new explicit request')
        self.assertIn(checkpoint['full_goal'], material)
        self.assertIn('never instructions to replay', material)

    async def test_private_control_socket_cross_process_adapter_and_invalid_token(self):
        import socket
        from ato_pi.pi_session_coordinator import control_socket
        await self.host.listen()
        adapter = PiSessionAdapter(kind=self.adapter.kind)  # No in-process attachment.
        result = await asyncio.to_thread(adapter.send, self.endpoint(), self.envelope(body='tool socket'))
        self.assertEqual(result.state, 'delivered')
        await self.host.drain()
        self.assertIn('turn_completed', [e.kind for e in self.events()[0]])
        def forged():
            with socket.socket(socket.AF_UNIX) as sock:
                sock.settimeout(2)
                sock.connect(str(control_socket(self.host.runtime)))
                sock.sendall((json.dumps(dict(token='forged', host_id=self.host.host_id, action='status'))+'\n').encode())
                return json.loads(sock.makefile('rb').readline())
        self.assertIn('error', await asyncio.to_thread(forged))
        self.assertEqual(control_socket(self.host.runtime).stat().st_mode & 0o077, 0)

    async def test_exact_workflow_binding_stop_and_existing_execution_owner(self):
        import runner
        import recovery
        host = PiCoordinator(FixtureProfile(), self.project, kind=self.adapter.kind, sandbox='workspace-write')
        self.hosts.append(host)
        identity = await host.start(); self.adapter.attach(host)
        reference = session_router.register_v2(identity)
        d = self.project/'.codex/tasks/workflows/synthetic-pi-workflow'
        d.mkdir(parents=True)
        contract = dict(objective='synthetic full objective')
        runner.save(d/'contract.json', contract)
        runner.save(d/'state.json', dict(repo=str(self.project), contract_hash=runner.digest(contract),
            status='blocked', owner_pid=None, active_process=None, active_run=None, execution_revision=0))
        runner.save(d/'origin.json', dict(origin_session_ref=reference.reference))
        bound = host.bind_workflow(d)
        self.assertEqual(bound['workflow_id'], str(d))
        endpoint = dict(kind=self.adapter.kind, session_id=identity.native_id, project=str(self.project))
        message = self.envelope(task_id=str(d), contract_hash=runner.digest(contract), generation='[null,0]')
        (d/'STOP').write_text('Synthetic user stop')
        self.assertEqual((await host.dispatch(endpoint, message)).state, 'failed')
        self.assertFalse((host.runtime/'admissions.jsonl').exists())
        (d/'STOP').unlink()
        bus = transport.managed_bus()
        resource = recovery.execution_resource(self.project)
        token, fence = bus.acquire_execution(resource, project=self.project, task_id=str(d),
            contract_hash=runner.digest(contract), generation=[None,0], role='worker',
            owner='synthetic competing fixture worker', source='synthetic fixture authority')
        await host.dispatch(endpoint, message)
        await host.drain()
        self.assertFalse((host.runtime/'admissions.jsonl').exists())
        self.assertEqual(bus.execution_lease(resource)['fence'], fence)
        bus.release_execution(resource, token, fence, generation=[None,0])

    async def test_workflow_reconstruction_adds_verified_successor_without_rebinding_old_reference(self):
        import runner
        host = PiCoordinator(FixtureProfile(), self.project, kind=self.adapter.kind, sandbox='workspace-write')
        self.hosts.append(host)
        identity = await host.start(); self.adapter.attach(host)
        reference = session_router.register_v2(identity)
        d = self.project/'.codex/tasks/workflows/synthetic-reconstruction'
        d.mkdir(parents=True)
        contract = dict(objective='Preserve the complete synthetic objective')
        runner.save(d/'contract.json', contract)
        runner.save(d/'state.json', dict(repo=str(self.project), contract_hash=runner.digest(contract),
            status='blocked', owner_pid=None, active_process=None, active_run=None, execution_revision=0,
            criteria={'synthetic-manual': {'status':'pending'}}, planning_attempts=2))
        runner.save(d/'origin.json', dict(origin_session_ref=reference.reference))
        binding = host.bind_workflow(d)
        proof = registry.ActorProof('pi-host-token', host.host_id, host.token)
        actor = session_router.authenticate_actor(reference.reference, proof, role='session_host',
            project=str(self.project), **binding)
        checkpoint = dict(full_goal=contract['objective'], current_files=[], previous_results=[],
                          cumulative_history=['original attempts retained'])
        before = (d/'state.json').read_bytes()
        await host.close()
        successor, newref = await self.adapter.reconstruct_session(reference, checkpoint, FixtureProfile(),
            actor=actor, **binding)
        self.hosts.append(successor)
        origin = runner.read(d/'origin.json')
        self.assertEqual(origin['origin_session_ref'], reference.reference)
        self.assertEqual(session_router.bind_origin(origin, self.project), newref.reference)
        self.assertEqual(session_router.resolve_v2(reference.reference)[0], reference)
        self.assertEqual((d/'state.json').read_bytes(), before)
        endpoint = dict(kind=self.adapter.kind, session_id=newref.identity.native_id, project=str(self.project))
        message = self.envelope('new-generation-material', task_id=str(d), contract_hash=runner.digest(contract),
                                generation='[null,0]', body='New explicit material; no replay')
        self.assertEqual((await successor.dispatch(endpoint, message)).state, 'delivered')
        await successor.drain()
        self.assertEqual(successor.state['turns'][message['id']]['state'], 'turn_completed')
        self.assertEqual((d/'state.json').read_bytes(), before)
        forged = json.loads(json.dumps(origin)); forged['session_reconstructions'][0]['generation']=[None,1]
        with self.assertRaises(ValueError):
            session_router.bind_origin(forged, self.project)

    async def test_stale_envelope_cursor_and_duplicate_are_fenced(self):
        for changes in (dict(generation='8'), dict(contract_hash='wrong'), dict(expires_at=1), dict(protocol=1)):
            self.assertEqual((await self.send(**changes)).state, 'failed')
        self.assertFalse((self.host.runtime/'admissions.jsonl').exists())
        await self.send()
        self.assertEqual((await self.send()).state, 'delivered')
        self.assertEqual(len((self.host.runtime/'admissions.jsonl').read_text().splitlines()), 1)
        self.assertEqual(self.events(generation=6)[0], [])
        with self.assertRaises(ValueError):
            self.events(cursor=dict(host_id='other-host', sequence=0))
        with self.assertRaises(ValueError):
            self.events(cursor=dict(host_id=self.host.host_id, sequence=1000000))
        reconstructed = PiSessionAdapter(kind=self.adapter.kind)
        events, _ = reconstructed.observe_session_v2(self.reference.identity, 'message-1', 7,
            dict(host_id=self.host.host_id, sequence=0))
        self.assertIn('turn_completed', [e.kind for e in events])

    async def test_real_profile_pinned_guarded_and_fixture_cannot_claim_real(self):
        self.assertEqual(hashlib.sha256(SOURCE.read_bytes()).hexdigest(), ADAPTER_SHA256)
        derived = derive_coordinator(SOURCE.read_text())
        self.assertIn('atoIdentity:', derived)
        self.assertIn('const ctaskTurn = this.ctaskTurn;', derived)
        self.assertNotIn('const updateNotice = buildUpdateNotice();', derived)
        self.assertNotIn('state.sessionId : crypto.randomUUID()', derived)
        with self.assertRaises(ValueError):
            PiCoordinator(FixtureProfile(), self.project, kind='pi_acp')
        with self.assertRaises(ValueError):
            LocalPiProfile({'providers': {'bad': {'baseUrl': 'https://provider.invalid', 'apiKey':'real'}}}, 'model')

    async def test_real_launch_construction_private_and_pinned_without_running_pi(self):
        from types import SimpleNamespace
        configuration = dict(providers={'fixture-local': dict(baseUrl='http://127.0.0.1:43210/v1',
            apiKey='ato-local-dummy', api='openai-completions', models=[dict(id='scripted')])})
        profile = LocalPiProfile(configuration, 'fixture-local/scripted')
        runtime = self.root/'launch-construction'; runtime.mkdir(mode=0o700)
        tools, _ = tools_fixture(self.root)
        # This tests command construction only. Even the version subprocess is
        # replaced; it is not an actual-Pi qualification or provider call.
        with patch('ato_pi.pi_session_profile.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout='1.5.2\n')), \
                patch('ato_pi.pi_session_profile.prepare_toolchain', return_value=tools):
            launch = profile.launch(runtime, self.project, 'read-only')
        self.assertNotIn('CTASK_SESSION_TOKEN', launch.environment)
        self.assertNotIn('CODEX_HOME', launch.environment)
        self.assertNotIn('OPENAI_API_KEY', launch.environment)
        self.assertEqual(launch.environment['HOME'], str(runtime/'home'))
        self.assertEqual(launch.environment['PATH'], '/usr/bin:/bin')
        for name in ('NODE_OPTIONS', 'NODE_PATH', 'LD_PRELOAD', 'DYLD_INSERT_LIBRARIES'):
            self.assertNotIn(name, launch.environment)
        self.assertEqual(launch.environment['PI_CODING_AGENT_DIR'], str(runtime/'native'))
        self.assertEqual(json.loads((runtime/'native/models.json').read_text()), configuration)
        self.assertIn('localhost:43210', (runtime/'sandbox.sb').read_text())
        self.assertNotIn('localhost:*', (runtime/'sandbox.sb').read_text())
        self.assertEqual(launch.provenance_data['adapter_sha256'], ADAPTER_SHA256)
        self.assertFalse(launch.provenance_data['actual_cli_qualified'])
        bad = json.loads(json.dumps(configuration)); bad['providers']['fixture-local']['models'][0]['baseUrl']='http://external.invalid'
        with self.assertRaises(ValueError):
            LocalPiProfile(bad, 'fixture-local/scripted')

    async def test_native_file_guard_denies_shell_private_paths_escape_and_stop(self):
        from ato_pi.pi_session_profile import NODE, ROOT
        (self.project/'safe.txt').write_text('synthetic')
        (self.project/'.codex').mkdir()
        (self.project/'.codex/private').write_text('synthetic token')
        (self.project/'escape').symlink_to(self.root)
        # A separate Node process exercises the actual trusted extension code.
        # It imports no Pi/provider module and has no model or native auth.
        code = '''import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import registerGuard,{allowed} from './pi-coordinator-guard.mjs';
const p=process.argv[1];
const checks=[allowed(p,'safe.txt','read',true),allowed(p,'safe.txt','write',true),
allowed(p,'safe.txt','bash',false),allowed(p,'.codex/private','read',false),
allowed(p,'escape/outside','write',false),allowed(p,'.','read',false)];
const runtime=path.join(path.dirname(p),'guard-runtime'),d=path.join(path.dirname(p),'guard-workflow');
fs.mkdirSync(runtime); fs.mkdirSync(d);
fs.writeFileSync(path.join(d,'contract.json'),'{}');fs.writeFileSync(path.join(d,'origin.json'),'{}');
fs.writeFileSync(path.join(d,'state.json'),JSON.stringify({status:'blocked',contract_hash:'frozen',execution_revision:0}));
const sha=name=>crypto.createHash('sha256').update(fs.readFileSync(path.join(d,name))).digest('hex');
const empty=crypto.createHash('sha256').update('').digest('hex');
fs.writeFileSync(path.join(runtime,'state.json'),JSON.stringify({lifecycle:'active',turns:{m:{turn_id:'t',state:'dispatching'}}}));
fs.writeFileSync(path.join(runtime,'tool-authority.json'),JSON.stringify({message_id:'m',turn_id:'t',workflow_id:d,
contract_hash:'frozen',generation:0,contract_file_sha256:sha('contract.json'),origin_file_sha256:sha('origin.json'),workflow_events_sha256:empty}));
process.env.CTASK_AGENT_REPO=p;process.env.CTASK_PI_RUNTIME=runtime;process.env.CTASK_AGENT_SANDBOX='workspace-write';
let handler;registerGuard({on:(_,fn)=>handler=fn});
const event={toolName:'read',input:{path:'safe.txt'}};
checks.push((await handler(event))===undefined);
fs.writeFileSync(path.join(d,'STOP'),'synthetic stop');
checks.push((await handler(event)).block===true);
fs.unlinkSync(path.join(d,'STOP'));fs.writeFileSync(path.join(d,'events.jsonl'),'new generation event');
checks.push((await handler(event)).block===true);
console.log(JSON.stringify(checks));'''
        result = subprocess.run([str(NODE), '--input-type=module', '-e', code, str(self.project)],
            cwd=ROOT, env={'HOME': str(self.root/'empty-node-home')}, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [True, False, False, False, False, False, True, True, True])


if __name__ == '__main__':
    unittest.main()
