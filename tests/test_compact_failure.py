"""Real controller/render/dispatch boundaries with isolated synthetic sessions."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import control
import coordination
import failure_attention
import failure_notice
import notifications
import recovery
import runner
import transport
import test_interaction as fixtures
from test_interaction import ORIGIN, HUB


class CompactFailureTests(unittest.TestCase):
    setUp = fixtures.InteractionTests.setUp
    tearDown = fixtures.InteractionTests.tearDown
    transport = fixtures.InteractionTests.transport

    def prepare(self):
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            control.enable('User authorized coordination', 60, 0)
            notifications.configure('user_input_only', 'Normal updates silent')
            failure_attention.configure('Once-only unattended failures')
        control.register(self.d)
        with control.database() as con:
            row = json.loads(con.execute('select body from workflows where path=?', (str(self.d),)).fetchone()[0])
        control.workflow_observation(row)
        self.action = next(a for a in control.rows() if a['kind'] == 'failure_attention')
        self.key = 'followup:'+self.action['id']+':0'

    def send(self, body='FULL_PRIVATE_FAILURE_MATERIAL_'*500):
        with patch('control.subprocess.run', side_effect=self.transport) as send:
            result = control.send(self.key, HUB, body, workflow=str(self.d))
        return result, send

    def read(self):
        with patch.dict(os.environ, CODEX_THREAD_ID=HUB):
            return coordination.read(self.action['id'])

    def test_full_body_private_only_and_duplicate_send_not_replayed(self):
        self.prepare()
        before = {p.name:p.read_bytes() for p in (self.d/'state.json', self.d/'contract.json')}
        result, sent = self.send()
        self.assertEqual(result['state'], 'queued')
        notice = sent.call_args.args[0][-1]
        self.assertLess(len(notice.encode()), 300)
        self.assertIn('[ctask-followup id='+self.action['id']+' reminder=0]', notice)
        self.assertIn('ato-read-handoff '+self.action['id'], notice)
        self.assertNotIn('FULL_PRIVATE', notice)
        self.assertNotIn(str(self.d), notice)
        self.assertIn('FULL_PRIVATE', self.read()['material'])
        path = failure_notice.payload_directory()/(self.action['id']+'.json')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        _, again = self.send()
        again.assert_not_called()
        self.assertEqual(before, {p.name:p.read_bytes() for p in (self.d/'state.json', self.d/'contract.json')})

    def test_current_reader_is_hub_bound(self):
        self.prepare(); self.send()
        with self.assertRaises(ValueError): coordination.read(self.action['id'])
        self.assertTrue(self.read()['valid'])

    def test_handler_acquired_before_send_is_recorded_without_a_wakeup(self):
        self.prepare()
        with patch('recovery.handler_status', return_value='origin_active'):
            result, sent = self.send()
        self.assertEqual(result['state'], 'recorded_only'); sent.assert_not_called()
        self.assertEqual(runner.read(self.d/'state.json')['status'], 'blocked')

    def test_handler_acquired_after_queue_makes_reader_silent_and_read_only(self):
        self.prepare(); self.send()
        before = (self.d/'state.json').read_bytes()
        with patch('recovery.handler_status', return_value='origin_active'):
            with self.assertRaises(ValueError): self.read()
        self.assertEqual((self.d/'state.json').read_bytes(), before)

    def test_stale_reader_cli_after_completion_returns_small_silent_result(self):
        self.prepare(); self.send()
        state = runner.read(self.d/'state.json'); state['status']='completed'; runner.save(self.d/'state.json',state)
        result = subprocess.run([sys.executable,str(Path(coordination.__file__)),self.action['id']],
                                capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0)
        self.assertEqual(json.loads(result.stdout)['action'],'end_silently')
        self.assertLess(len(result.stdout),180); self.assertNotIn('FULL_PRIVATE',result.stdout)

    def test_stop_changed_generation_and_presented_notice_cannot_be_read(self):
        for change in ['STOP','generation','presented','stale']:
            with self.subTest(change=change):
                self.prepare(); self.send()
                if change=='STOP': (self.d/'STOP').touch()
                elif change=='generation':
                    state=runner.read(self.d/'state.json');state['execution_revision']=10;runner.save(self.d/'state.json',state)
                else:
                    with control.database() as con,con:
                        a=failure_notice.action_for(self.action['id'])
                        if change=='presented':a['presented_at']=runner.now()
                        else:a['state']='stale'
                        control.put(con,a)
                with self.assertRaises(ValueError):self.read()
                # Each boundary uses a separate fixture rather than rewriting history.
                self.tearDown();self.setUp()

    def test_private_material_tamper_and_permissions_rejected(self):
        self.prepare();self.send()
        p=failure_notice.payload_directory()/(self.action['id']+'.json')
        original=p.read_bytes();data=runner.read(p);data['material']='altered';runner.save(p,data)
        with self.assertRaises(ValueError):self.read()
        p.write_bytes(original);p.chmod(0o644)
        with self.assertRaises(ValueError):self.read()

    def test_route_reminder_and_immutable_content_cannot_be_rebound(self):
        self.prepare()
        with self.assertRaises(ValueError):failure_notice.prepare(self.key,ORIGIN,str(self.d),'private')
        with self.assertRaises(ValueError):failure_notice.prepare(self.key[:-1]+'1',HUB,str(self.d),'private')
        failure_notice.prepare(self.key,HUB,str(self.d),'private')
        with self.assertRaises(ValueError):failure_notice.prepare(self.key,HUB,str(self.d),'different')

    def test_acquiring_handler_at_dispatch_barrier_cancels_without_send(self):
        from message_bus import MessageBus
        self.prepare()
        state=runner.read(self.d/'state.json')
        projection=transport.prepare_managed(HUB,'short',canonical_id=self.key,phase='followup',
            task_id=str(self.d),contract_hash=state['contract_hash'],generation=recovery.generation(self.d,state),
            project=str(self.repo),source='Fixture failure routing')
        original=MessageBus.begin_dispatch
        handler=['unhandled']
        def race(bus,*args,**kwargs):
            result=original(bus,*args,**kwargs);handler[0]='origin_active';return result
        with patch('recovery.handler_status',side_effect=lambda *a:handler[0]),patch.object(MessageBus,'begin_dispatch',race),patch('control.subprocess.run') as sent:
            with self.assertRaises(ValueError):transport.dispatch_managed(projection)
        sent.assert_not_called()
        self.assertEqual(transport.managed_bus().message(projection['mailbox_id'])['state'],'cancelled')

    def test_normal_progress_stays_silent_and_unhandled_failure_still_notifies(self):
        self.prepare()
        self.assertTrue(notifications.disposition(self.action)['notify'])
        for kind in ['host_handoff','workflow','operational','watchdog']:
            self.assertFalse(notifications.disposition(dict(kind=kind,workflow=str(self.d)))['notify'])
