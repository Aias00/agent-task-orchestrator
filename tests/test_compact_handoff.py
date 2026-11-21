import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import runner
import workflow
import control
import coordination
import transport
import recovery
import test_interaction as fixtures
from test_interaction import ORIGIN, HUB


class CompactHandoffTests(unittest.TestCase):
    setUp=fixtures.InteractionTests.setUp
    tearDown=fixtures.InteractionTests.tearDown
    transport=fixtures.InteractionTests.transport

    def prepare(self):
        c=runner.read(self.repo/'input.json')
        c['host_handoff']=dict(source='Actual fixture original authority',instructions='KEEP_FULL_AUTHORITY_AND_SCOPE_'*350)
        p=self.repo/'host-contract.json';runner.save(p,c)
        self.d=workflow.create(self.repo,p,ORIGIN)
        s=runner.read(self.d/'state.json')
        s.update(status='needs_host',host_handoff={'run_dir':None,'tasks':{'overall':{'summary':'Final frozen checks failed','next_step':'Inspect current final acceptance evidence'}}})
        runner.save(self.d/'state.json',s)
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):control.enable('Fixture continuous coordination')
        control.register(self.d)
        self.state=s;self.contract=runner.read(self.d/'contract.json')
        self.identifier=coordination.identifier(self.d,s)
        self.key='host-handoff:'+self.identifier

    def test_only_short_notice_is_queued_full_material_is_retained_for_origin(self):
        self.prepare()
        with patch('control.subprocess.run',side_effect=self.transport) as sent:
            control.tick();control.tick()
        calls=[c.args[0][-1] for c in sent.call_args_list if '--thread' in c.args[0] and c.args[0][c.args[0].index('--thread')+1]==ORIGIN]
        self.assertEqual(len(calls),1)
        notice=calls[0];self.assertLess(len(notice),260)
        self.assertIn('[ctask-host-handoff id='+self.identifier+']',notice)
        self.assertIn('ato-read-handoff '+self.identifier,notice)
        self.assertNotIn('KEEP_FULL_AUTHORITY',notice)
        self.assertNotIn('Final frozen checks failed',notice)
        row=coordination.read(self.identifier)
        self.assertTrue(row['valid']);self.assertIn(self.contract['host_handoff']['instructions'],row['material'])
        self.assertIn('Final frozen checks failed',row['material'])
        self.assertEqual(runner.read(self.d/'state.json')['status'],'needs_host')
        payload=coordination.payload_directory()/(self.identifier+'.json')
        self.assertEqual(payload.stat().st_mode & 0o777,0o600)
        action=next(x for x in control.rows() if x['kind']=='host_handoff')
        message=transport.managed_bus().message(action['origin_delivery']['mailbox_id'])
        self.assertEqual(message['kind'],'host_handoff')

    def envelope(self,legacy=True):
        return transport.prepare_managed(ORIGIN,'legacy body' if legacy else '[ctask-host-handoff id='+self.identifier+']',
            canonical_id=self.key,phase='followup' if legacy else 'host_handoff',
            task_id=str(self.d),contract_hash=self.state['contract_hash'],
            generation=recovery.generation(self.d,self.state),project=str(self.repo),source='Fixture exact host authorization')

    def test_legacy_followup_type_cannot_dispatch_after_completion(self):
        self.prepare();projection=self.envelope()
        s=runner.read(self.d/'state.json');s['status']='completed';runner.save(self.d/'state.json',s)
        with patch('control.subprocess.run') as sent:
            with self.assertRaises(ValueError):transport.dispatch_managed(projection)
            sent.assert_not_called()
        self.assertEqual(transport.managed_bus().message(projection['mailbox_id'])['state'],'cancelled')

    def test_typed_notice_cannot_dispatch_after_completion_or_handoff_change(self):
        for changed in ('completed','different_request','blocked','STOP'):
            with self.subTest(changed=changed):
                # Distinct correlation per case within the same fixture namespace.
                self.prepare();projection=self.envelope(legacy=False)
                s=runner.read(self.d/'state.json')
                if changed=='STOP':(self.d/'STOP').touch()
                elif changed=='different_request':s['host_handoff']['tasks']['overall']['next_step']='new host request'
                else:s['status']=changed
                runner.save(self.d/'state.json',s)
                with patch('control.subprocess.run') as sent:
                    with self.assertRaises(ValueError):transport.dispatch_managed(projection)
                    sent.assert_not_called()
                self.assertEqual(transport.managed_bus().message(projection['mailbox_id'])['state'],'cancelled')

    def test_completion_between_dispatch_barrier_and_send_cancels_without_queue(self):
        from message_bus import MessageBus
        self.prepare();projection=self.envelope();original=MessageBus.begin_dispatch
        def race(bus,*args,**kwargs):
            result=original(bus,*args,**kwargs)
            s=runner.read(self.d/'state.json');s['status']='completed';runner.save(self.d/'state.json',s)
            return result
        with patch.object(MessageBus,'begin_dispatch',race),patch('control.subprocess.run') as sent:
            with self.assertRaises(ValueError):transport.dispatch_managed(projection)
            sent.assert_not_called()
        self.assertEqual(transport.managed_bus().message(projection['mailbox_id'])['state'],'cancelled')

    def test_valid_legacy_notice_still_dispatches_once(self):
        self.prepare();projection=self.envelope()
        with patch('control.subprocess.run',side_effect=self.transport) as sent:
            first=transport.dispatch_managed(projection);transport.dispatch_managed(projection)
        self.assertEqual(sent.call_count,1);self.assertEqual(first['state'],'queued')

    def material(self):
        return coordination.prepare(self.d,self.state,self.contract,ORIGIN,self.key,'FULL PRIVATE MATERIAL')

    def test_read_rechecks_terminal_stop_generation_and_origin_without_mutation(self):
        self.prepare();self.material()
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
            with self.assertRaises(ValueError):coordination.read(self.identifier)
        before=(self.d/'state.json').read_bytes()
        coordination.read(self.identifier);self.assertEqual((self.d/'state.json').read_bytes(),before)
        (self.d/'STOP').touch()
        with self.assertRaises(ValueError):coordination.read(self.identifier)
        (self.d/'STOP').unlink()
        s=runner.read(self.d/'state.json');s['execution_revision']=s.get('execution_revision',0)+1;runner.save(self.d/'state.json',s)
        with self.assertRaises(ValueError):coordination.read(self.identifier)

    def test_material_tamper_permissions_and_immutable_key_are_rejected(self):
        self.prepare();self.material()
        with self.assertRaises(ValueError):coordination.prepare(self.d,self.state,self.contract,ORIGIN,self.key,'DIFFERENT MATERIAL')
        p=coordination.payload_directory()/(self.identifier+'.json');row=runner.read(p);row['material']='tampered';runner.save(p,row)
        p.chmod(0o600)
        with self.assertRaises(ValueError):coordination.read(self.identifier)
        p.chmod(0o644)
        with self.assertRaises(ValueError):coordination.read(self.identifier)

    def test_stale_reader_cli_is_small_and_requests_silent_end(self):
        self.prepare();self.material();s=runner.read(self.d/'state.json');s['status']='completed';runner.save(self.d/'state.json',s)
        p=subprocess.run([sys.executable,str(Path(coordination.__file__)) ,self.identifier],capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,0);v=json.loads(p.stdout)
        self.assertFalse(v['valid']);self.assertEqual(v['action'],'end_silently');self.assertLess(len(p.stdout),180)
        self.assertNotIn('FULL PRIVATE',p.stdout)
