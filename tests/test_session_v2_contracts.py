"""Versioned neutral session contracts, explicit actor authority and ATO_HOME isolation."""
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import agent_registry
from ato_adapters import agents
from ato_common import ato_config
import runner
from ato_workflow import session_router


class ContractAdapter:
    capabilities = frozenset({'session_verify'})
    capability_calls = []
    activity_value = None
    event_generation = 4
    resume_calls = 0

    def __init__(self, kind):
        self.kind = kind
        self.session_capabilities_v2 = {
            'session.verify': {'state': 'supported', 'evidence': 'fixture exact identity verifier'},
            'session.observe': {'state': 'supported', 'evidence': 'fixture correlated event reader'},
            'actor.authenticate': {'state': 'supported', 'evidence': 'fixture private endpoint challenge'},
            'session.load_resume': {'state': 'unsupported', 'evidence': 'fixture has no native load/resume'},
        }

    def verify_session(self, identity, project):
        return identity == 'opaque-session' and Path(project).is_dir()

    def authenticate_actor(self, identity, proof, *, role, workflow_id, contract_hash, generation):
        self.capability_calls.append((identity, proof, role, workflow_id, contract_hash, generation))
        if proof.credential_ref != 'local-endpoint:test-user' or proof.challenge != 'challenge-1':
            return None
        return 'principal:fixture-user'

    def session_activity(self, identity, *, generation):
        return self.activity_value

    def observe_session_v2(self, identity, correlation_id, generation, cursor):
        event = agent_registry.SessionEvent(
            event_id='event-1', session=identity, correlation_id=correlation_id,
            generation=self.event_generation, kind='turn_completed', source='fixture local event log',
            observed_at=123.0, turn_id='turn-1', message_id='message-1')
        return [event], {'offset': 1}

    def load_or_resume_session(self, identity, checkpoint, *, operation, workflow_id, contract_hash, generation):
        self.resume_calls += 1
        return identity

class SessionV2Contracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kind = 'fixture_contracts_'+uuid.uuid4().hex
        cls.adapter = ContractAdapter(cls.kind)
        agent_registry.register(cls.adapter)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root/'project'
        self.project.mkdir()
        self.ato = self.root/'ato-state'
        self.codex = self.root/'codex-config'
        env = patch.dict(os.environ, {'ATO_HOME': str(self.ato), 'CODEX_HOME': str(self.codex)}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.adapter = self.__class__.adapter
        self.adapter.capability_calls.clear()
        self.adapter.resume_calls = 0
        self.adapter.event_generation = 4
        self.identity = agent_registry.SessionIdentity(
            self.kind, 'opaque-session', str(self.project.resolve()), 'fixture verified identity')
        self.session = session_router.register_v2(self.identity)

    def host_actor(self, reference=None):
        proof = agent_registry.ActorProof('fixture-challenge', 'local-endpoint:test-user', 'challenge-1')
        return session_router.authenticate_actor(reference or self.session.reference, proof, role='session_host',
            project=self.project, workflow_id='wf-1', contract_hash='contract-hash', generation=4)

    def test_session_identity_v2_round_trips_as_json_mapping(self):
        encoded = json.dumps(self.identity.to_mapping(), allow_nan=False)
        decoded = agent_registry.SessionIdentity.from_mapping(json.loads(encoded))
        self.assertEqual(decoded, self.identity)
        self.assertEqual(decoded.version, 2)

    def test_activity_and_event_contracts_round_trip_as_json(self):
        activity = agent_registry.SessionActivity(self.identity, 'active', 'fixture endpoint',
                                                  123.0, 4, 'turn-1')
        restored_activity = agent_registry.SessionActivity.from_mapping(
            json.loads(json.dumps(activity.to_mapping(), allow_nan=False)))
        self.assertEqual(restored_activity, activity)
        event = agent_registry.SessionEvent('event-json', self.identity, 'message-json', 4,
            'turn_completed', 'fixture endpoint', 124.0, 'turn-1', 'message-json')
        restored_event = agent_registry.SessionEvent.from_mapping(
            json.loads(json.dumps(event.to_mapping(), allow_nan=False)))
        self.assertEqual(restored_event, event)

    def test_reference_is_opaque_and_persists_v2_identity(self):
        row = runner.read(session_router._path(self.session.reference))
        self.assertEqual(row['version'], 2)
        self.assertNotEqual(self.session.reference, self.identity.native_id)
        restored = agent_registry.SessionReference.from_mapping(self.session.to_mapping())
        self.assertEqual(restored, self.session)
        resolved, adapter = session_router.resolve_v2(self.session.reference, project=self.project)
        self.assertEqual(resolved, self.session)
        self.assertIs(adapter, self.adapter)

    def test_reference_rejects_wrong_project_before_use(self):
        other = self.root/'other'
        other.mkdir()
        with self.assertRaisesRegex(ValueError, 'project mismatch'):
            session_router.resolve_v2(self.session.reference, project=other)

    def test_capability_states_are_explicit_and_unknown_is_not_unsupported(self):
        capabilities = session_router.capability_contract(self.session.reference)
        self.assertEqual(capabilities.state('session.verify'), agent_registry.CapabilityState.SUPPORTED)
        self.assertEqual(capabilities.state('session.load_resume'), agent_registry.CapabilityState.UNSUPPORTED)
        self.assertEqual(capabilities.state('session.activity'), agent_registry.CapabilityState.UNKNOWN)
        restored = agent_registry.SessionCapabilities.from_mapping(capabilities.to_mapping())
        self.assertEqual(restored, capabilities)

    def test_absent_v1_capability_is_unknown_not_a_claim_of_support(self):
        capabilities = agent_registry.SessionCapabilities('fixture_absent')
        self.assertEqual(capabilities.state('session.observe'), agent_registry.CapabilityState.UNKNOWN)
        with self.assertRaises(agent_registry.CapabilityUnavailable) as raised:
            capabilities.require('session.observe')
        self.assertEqual(raised.exception.state, agent_registry.CapabilityState.UNKNOWN)

    def test_builtin_session_capabilities_are_truthful_and_separate_from_workers(self):
        codex = agent_registry.session_capabilities('codex')
        self.assertEqual(codex.state('session.create'), agent_registry.CapabilityState.SUPPORTED)
        self.assertEqual(codex.state('session.load_resume'), agent_registry.CapabilityState.UNSUPPORTED)
        self.assertEqual(codex.state('actor.authenticate'), agent_registry.CapabilityState.UNKNOWN)
        for kind in ('claude', 'pi'):
            capabilities = agent_registry.session_capabilities(kind)
            self.assertEqual(capabilities.state('session.create'), agent_registry.CapabilityState.UNSUPPORTED)
            self.assertEqual(capabilities.state('session.transport'), agent_registry.CapabilityState.UNSUPPORTED)
            self.assertEqual(capabilities.state('actor.authenticate'), agent_registry.CapabilityState.UNSUPPORTED)

    def test_environment_reference_is_only_a_locator_not_an_actor(self):
        with patch.dict(os.environ, {'CTASK_SESSION_REF': self.session.reference}, clear=False):
            located = session_router.current_reference()
        self.assertEqual(located, self.session)
        self.assertNotIsInstance(located, agent_registry.ActorContext)
        with self.assertRaisesRegex(ValueError, 'explicit v2 credential proof'):
            session_router.authenticate_actor(self.session.reference, None, role='user',
                project=self.project, workflow_id='wf-1', contract_hash='contract-hash', generation=4)
        self.assertEqual(self.adapter.capability_calls, [])

    def test_actor_authentication_requires_adapter_verified_local_proof(self):
        proof = agent_registry.ActorProof('fixture-challenge', 'local-endpoint:test-user', 'challenge-1')
        actor = session_router.authenticate_actor(self.session.reference, proof, role='user',
            project=self.project, workflow_id='wf-1', contract_hash='contract-hash', generation=4)
        self.assertEqual(actor.role, agent_registry.ActorRole.USER)
        self.assertEqual(actor.actor_id, 'principal:fixture-user')
        self.assertEqual(self.adapter.capability_calls[0][1], proof)
        self.assertNotIn('challenge-1', repr(runner.read(session_router._path(self.session.reference))))
        self.assertTrue(session_router.authorize_actor(actor, 'decision.answer', project=self.project,
            workflow_id='wf-1', contract_hash='contract-hash', generation=4))

    def test_origin_actor_cannot_claim_user_decision_authority(self):
        proof = agent_registry.ActorProof('fixture-challenge', 'local-endpoint:test-user', 'challenge-1')
        actor = session_router.authenticate_actor(self.session.reference, proof, role='origin',
            project=self.project, workflow_id='wf-1', contract_hash='contract-hash', generation=4)
        with self.assertRaisesRegex(ValueError, 'not authorized'):
            session_router.authorize_actor(actor, 'decision.approve', project=self.project,
                workflow_id='wf-1', contract_hash='contract-hash', generation=4)

    def test_hub_actor_can_present_but_cannot_authorize_or_impersonate_user(self):
        proof = agent_registry.ActorProof('fixture-challenge', 'local-endpoint:test-user', 'challenge-1')
        actor = session_router.authenticate_actor(self.session.reference, proof, role='hub',
            project=self.project, workflow_id='wf-1', contract_hash='contract-hash', generation=4)
        self.assertTrue(session_router.authorize_actor(actor, 'interaction.present', project=self.project,
            workflow_id='wf-1', contract_hash='contract-hash', generation=4))
        for action in ('decision.answer', 'interaction.forward_user_answer'):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'not authorized'):
                session_router.authorize_actor(actor, action, project=self.project,
                    workflow_id='wf-1', contract_hash='contract-hash', generation=4)

    def test_actor_claim_cannot_be_constructed_without_adapter_authentication(self):
        with self.assertRaisesRegex(ValueError, 'invalid authenticated actor context'):
            agent_registry.ActorContext('forged', 'user', self.session, str(self.project.resolve()),
                'wf-1', 'contract-hash', 4, 'environment variable')

    def test_actor_binding_rejects_stale_contract_or_generation(self):
        proof = agent_registry.ActorProof('fixture-challenge', 'local-endpoint:test-user', 'challenge-1')
        actor = session_router.authenticate_actor(self.session.reference, proof, role='user',
            project=self.project, workflow_id='wf-1', contract_hash='contract-hash', generation=4)
        with self.assertRaisesRegex(ValueError, 'binding mismatch'):
            session_router.authorize_actor(actor, 'decision.accept', project=self.project,
                workflow_id='wf-1', contract_hash='changed-contract', generation=4)
        with self.assertRaisesRegex(ValueError, 'binding mismatch'):
            session_router.authorize_actor(actor, 'decision.accept', project=self.project,
                workflow_id='wf-1', contract_hash='contract-hash', generation=5)

    def test_unverifiable_activity_is_explicit_unknown_not_idle(self):
        activity = session_router.activity_v2(self.session.reference, generation=4)
        self.assertEqual(activity.state, agent_registry.ActivityState.UNKNOWN)
        self.assertEqual(activity.source, 'capability_unknown')
        self.assertEqual(activity.generation, 4)

    def test_reported_activity_must_match_exact_session_and_generation(self):
        self.adapter.session_capabilities_v2['session.activity'] = {
            'state': 'supported', 'evidence': 'fixture exact activity endpoint'}
        self.adapter.activity_value = agent_registry.SessionActivity(
            self.identity, 'active', 'fixture local endpoint', 123.0, 4, 'turn-1')
        try:
            activity = session_router.activity_v2(self.session.reference, generation=4)
            self.assertEqual(activity.state, agent_registry.ActivityState.ACTIVE)
            self.adapter.activity_value = agent_registry.SessionActivity(
                self.identity, 'idle', 'fixture stale endpoint', 124.0, 3)
            with self.assertRaisesRegex(ValueError, 'identity or generation mismatch'):
                session_router.activity_v2(self.session.reference, generation=4)
        finally:
            self.adapter.session_capabilities_v2.pop('session.activity', None)
            self.adapter.activity_value = None

    def test_unsupported_native_resume_fails_before_adapter_effect(self):
        actor = self.host_actor()
        with self.assertRaises(agent_registry.CapabilityUnavailable) as raised:
            session_router.load_or_resume_v2(self.session.reference, {'checkpoint': 'fixture'}, actor=actor,
                workflow_id='wf-1', contract_hash='contract-hash', generation=4)
        self.assertEqual(raised.exception.state, agent_registry.CapabilityState.UNSUPPORTED)
        self.assertEqual(self.adapter.resume_calls, 0)

    def test_unknown_native_resume_is_not_inferred_from_session_registration(self):
        no_resume_kind = 'fixture_unknown_resume_'+uuid.uuid4().hex

        class NoResume:
            kind = no_resume_kind
            capabilities = frozenset({'session_verify'})
            session_capabilities_v2 = {
                'session.verify': {'state': 'supported', 'evidence': 'fixture exact identity verifier'},
                'actor.authenticate': {'state': 'supported', 'evidence': 'fixture private endpoint challenge'},
            }

            def verify_session(self, identity, project):
                return identity == 'opaque-session' and Path(project).is_dir()

            def authenticate_actor(self, identity, proof, *, role, workflow_id, contract_hash, generation):
                return 'principal:fixture-user' if proof.challenge == 'challenge-1' else None

        adapter = NoResume()
        agent_registry.register(adapter)
        identity = agent_registry.SessionIdentity(adapter.kind, 'opaque-session',
            str(self.project.resolve()), 'fixture verified identity')
        reference = session_router.register_v2(identity)
        proof = agent_registry.ActorProof('fixture-challenge', 'local-endpoint:test-user', 'challenge-1')
        actor = self.host_actor(reference.reference)
        with self.assertRaises(agent_registry.CapabilityUnavailable) as raised:
            session_router.load_or_resume_v2(reference.reference, {'checkpoint': 'fixture'}, actor=actor,
                workflow_id='wf-1', contract_hash='contract-hash', generation=4)
        self.assertEqual(raised.exception.state, agent_registry.CapabilityState.UNKNOWN)

    def test_non_host_actor_cannot_resume_even_if_native_support_is_available(self):
        self.adapter.session_capabilities_v2['session.load_resume'] = {
            'state': 'supported', 'evidence': 'fixture native load/resume implementation'}
        try:
            proof = agent_registry.ActorProof('fixture-challenge', 'local-endpoint:test-user', 'challenge-1')
            actor = session_router.authenticate_actor(self.session.reference, proof, role='origin',
                project=self.project, workflow_id='wf-1', contract_hash='contract-hash', generation=4)
            with self.assertRaisesRegex(ValueError, 'not authorized'):
                session_router.load_or_resume_v2(self.session.reference, {'checkpoint': 'fixture'}, actor=actor,
                    workflow_id='wf-1', contract_hash='contract-hash', generation=4)
            self.assertEqual(self.adapter.resume_calls, 0)
        finally:
            self.adapter.session_capabilities_v2['session.load_resume'] = {
                'state': 'unsupported', 'evidence': 'fixture has no native load/resume'}

    def test_host_resume_is_bound_to_the_current_contract_generation(self):
        self.adapter.session_capabilities_v2['session.load_resume'] = {
            'state': 'supported', 'evidence': 'fixture native load/resume implementation'}
        try:
            actor = self.host_actor()
            result = session_router.load_or_resume_v2(self.session.reference, {'checkpoint': 'fixture'},
                actor=actor, workflow_id='wf-1', contract_hash='contract-hash', generation=4)
            self.assertEqual(result, self.identity)
            self.assertEqual(self.adapter.resume_calls, 1)
        finally:
            self.adapter.session_capabilities_v2['session.load_resume'] = {
                'state': 'unsupported', 'evidence': 'fixture has no native load/resume'}

    def test_events_bind_exact_session_correlation_and_generation(self):
        events, cursor = session_router.observe_v2(self.session.reference, 'message-1', generation=4, cursor={'offset': 0})
        self.assertEqual(events[0].session, self.identity)
        self.assertEqual(events[0].correlation_id, 'message-1')
        self.assertEqual(events[0].generation, 4)
        json.dumps(cursor, allow_nan=False)
        self.adapter.event_generation = 3
        with self.assertRaisesRegex(ValueError, 'correlation or generation mismatch'):
            session_router.observe_v2(self.session.reference, 'message-1', generation=4, cursor={'offset': 0})

    def test_turn_completion_cannot_be_encoded_as_workflow_acceptance(self):
        with self.assertRaisesRegex(ValueError, 'invalid versioned session event'):
            agent_registry.SessionEvent(event_id='event-2', session=self.identity,
                correlation_id='message-1', generation=4, kind='workflow_accepted',
                source='fixture agent output', observed_at=time.time(), turn_id='turn-1')

    def test_ato_home_is_separate_and_legacy_fallback_is_preserved(self):
        roots = ato_config.roots()
        self.assertEqual(Path(roots['ato_home']), self.ato.resolve())
        self.assertEqual(Path(roots['codex_home']), self.codex.resolve())
        self.assertEqual(ato_config.ato_home(), self.ato.resolve())
        with patch.dict(os.environ, {'CODEX_HOME': str(self.codex)}, clear=False):
            os.environ.pop('ATO_HOME', None)
            self.assertEqual(ato_config.ato_home(), self.codex.resolve())
            self.assertEqual(ato_config.codex_home(), self.codex.resolve())

    def test_v1_register_and_resolve_entrypoints_remain_compatible(self):
        reference = session_router.register(self.kind, 'opaque-session', self.project, 'legacy fixture source')
        row, adapter = session_router.resolve(reference, project=self.project)
        self.assertEqual(row['version'], 1)
        self.assertEqual(row['kind'], self.kind)
        self.assertEqual(row['identity'], 'opaque-session')
        self.assertIs(adapter, self.adapter)

    def test_v1_resolve_entrypoint_reads_v2_rows_through_neutral_capability(self):
        kind = 'fixture_v2_only_'+uuid.uuid4().hex

        class V2Only:
            capabilities = frozenset()
            session_capabilities_v2 = {
                'session.verify': {'state': 'supported', 'evidence': 'fixture exact identity verifier'},
            }

            def __init__(self):
                self.kind = kind

            def verify_session(self, identity, project):
                return identity == 'opaque-session' and Path(project).is_dir()

        adapter = V2Only()
        agent_registry.register(adapter)
        identity = agent_registry.SessionIdentity(kind, 'opaque-session',
            str(self.project.resolve()), 'fixture verified identity')
        reference = session_router.register_v2(identity)
        row, resolved_adapter = session_router.resolve(reference.reference, project=self.project)
        self.assertEqual(row['identity'], 'opaque-session')
        self.assertIs(resolved_adapter, adapter)


if __name__ == '__main__':
    unittest.main()
