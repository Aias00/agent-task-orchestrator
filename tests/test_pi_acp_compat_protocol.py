"""Actual reviewed JS transformations over local synthetic native RPC only."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import acp
from acp.schema import DeniedOutcome, RequestPermissionResponse, TextContentBlock

from ato_pi import pi_acp_compat as compat
from ato_pi.pi_toolchain import clean_environment, locate

ROOT = Path(__file__).resolve().parents[1]


class Client:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append((session_id, update))

    async def request_permission(self, **kwargs):
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome='cancelled'))


class CompatibilityProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ato-compat-rpc-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.node = locate('node')  # Runs only reviewed adapter JS, never real Pi.

    async def exercise(self, role, prompt='success', *, missing_identity=False, turns=1):
        base = self.root/role; base.mkdir()
        home = base/'home'; home.mkdir()
        project = base/'project'; project.mkdir()
        adapter = compat.derive_directory(base/'adapter', role=role)
        env = dict(clean_environment(home=home), CTASK_PI_ACP_STATE_DIR=str(base/'state'),
            PI_ACP_PI_COMMAND=str(ROOT/'tests/fixtures/pi_compat_rpc.py'))
        if missing_identity:
            env['CTASK_FIXTURE_MISSING_ID'] = '1'
        client = Client()
        async with acp.spawn_agent_process(client, str(self.node), str(adapter), cwd=project, env=env) as (conn, proc):
            await asyncio.wait_for(conn.initialize(protocol_version=1), timeout=5)
            session = await asyncio.wait_for(conn.new_session(cwd=str(project)), timeout=5)
            if role == 'coordinator':
                self.assertEqual(session.session_id, 'synthetic-native-compat')
                self.assertEqual(session.field_meta['atoIdentity'],
                    dict(nativeId='synthetic-native-compat', project=str(project)))
            for index in range(turns):
                nonce = 'fresh-synthetic-turn-'+str(index)
                client.updates.clear()  # Startup/history cannot stand in for a turn.
                response = await asyncio.wait_for(conn.prompt(session_id=session.session_id,
                    prompt=[TextContentBlock(type='text', text=prompt)], ctaskTurn=nonce), timeout=5)
                await asyncio.sleep(.05)
                self.assertEqual(response.stop_reason, 'end_turn')
                updates = [update for identity, update in client.updates if identity == session.session_id]
                receipts = [u.field_meta['ctaskNativeReceipt'] for u in updates
                    if u.session_update == 'session_info_update' and (u.field_meta or {}).get('ctaskNativeReceipt')]
                self.assertTrue(receipts)
                self.assertEqual({r['turn'] for r in receipts}, {nonce})
                expected = 'error' if prompt == 'error-then-stop' else 'stop'
                self.assertEqual({r['stopReason'] for r in receipts}, {expected})
                if expected == 'stop':
                    self.assertEqual({r['result'] for r in receipts}, {'synthetic answer'})
                if role == 'coordinator':
                    self.assertTrue(updates)
                    startup = session.field_meta['piAcp']['startupInfo']
                    # The adapter declares the exact startup text in new_session;
                    # that separately emitted chunk may arrive after the reply.
                    messages = [u for u in updates if u.session_update == 'agent_message_chunk']
                    self.assertTrue(any(u.content.text == 'synthetic answer' for u in messages))
                    turn_updates = [u for u in messages if u.content.text != startup]
                    turn_updates += [u for u in updates if (u.field_meta or {}).get('ctaskNativeReceipt')]
                    self.assertEqual({(u.field_meta or {}).get('ctaskTurn') for u in turn_updates}, {nonce})
            admissions = (base/'state/admissions.jsonl').read_text().splitlines()
            self.assertEqual(len(admissions), turns)
            self.assertEqual(json.loads(admissions[0])['message'], prompt)

    async def test_both_roles_emit_fresh_native_receipts_from_actual_derived_js(self):
        for role in compat.CAPABILITIES:
            with self.subTest(role=role):
                await self.exercise(role)

    async def test_both_roles_keep_native_error_sticky_after_later_stop_and_end_turn(self):
        for role in compat.CAPABILITIES:
            with self.subTest(role=role):
                await self.exercise(role, 'error-then-stop')

    async def test_coordinator_rejects_missing_native_identity_instead_of_random_uuid(self):
        with self.assertRaises(acp.RequestError) as rejection:
            await self.exercise('coordinator', missing_identity=True)
        self.assertEqual(rejection.exception.code, -32603)
        self.assertFalse((self.root/'coordinator/state/admissions.jsonl').exists())

    async def test_coordinator_two_turns_keep_identity_and_distinct_nonce_binding(self):
        await self.exercise('coordinator', turns=2)
