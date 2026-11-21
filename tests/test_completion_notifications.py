import hashlib,json,os,subprocess,sys,time,unittest
from pathlib import Path
from unittest.mock import patch
import test_interaction as fixtures
from test_interaction import HUB
import completion_notifications as done
import control,interaction,notifications,recovery,runner

class CompletionTests(unittest.TestCase):
 setUp=fixtures.InteractionTests.setUp
 tearDown=fixtures.InteractionTests.tearDown
 transport=fixtures.InteractionTests.transport
 def enable(self,completion='once'):
  with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
   p=notifications.configure('user_input_only','Actual fixture user: total completion once',completion=completion)
   control.enable('Existing fixture controller authority',60,3)
  return p
 def finish(self,*,at=None,verified=True):
  s=runner.read(self.d/'state.json');e=self.d/'evidence/done.log';e.write_text('independent fixture acceptance\n')
  s.update(status='completed',owner_pid=None,active_process=None,criteria={'file':dict(status='passed' if verified else 'failed',evidence=[str(e)])});runner.save(self.d/'state.json',s)
  with (self.d/'events.jsonl').open('a') as f:f.write(json.dumps(dict(event='workflow_finished',status='completed',time=runner.now() if at is None else at))+'\n')
  control.register(self.d)
 def tick(self):
  with patch('control.subprocess.run',side_effect=self.transport) as send:control.tick()
  return [c.args[0] for c in send.call_args_list]
 def result(self):return next(a for a in control.rows() if a['kind']=='workflow_result')
 def test_verified_total_completion_sends_once_then_never_reminds(self):
  self.enable();self.finish();calls=self.tick();self.assertEqual(len(calls),1)
  text=calls[0][calls[0].index('--message')+1];self.assertTrue(text.startswith('[ctask-workflow-completed id=result-'));self.assertIn('总体验收 1/1',text)
  self.assertEqual(self.tick(),[])
  with patch('control.core.now',return_value=runner.now()+10000):self.assertEqual(self.tick(),[])
  a=self.result();self.assertEqual(a['notice']['state'],'queued');self.assertNotIn('presented_at',a)
 def test_unknown_send_is_claimed_and_not_replayed(self):
  self.enable();self.finish()
  with patch('control.subprocess.run',side_effect=subprocess.TimeoutExpired('fixture',1)) as send:control.tick();self.assertEqual(send.call_count,1)
  self.assertEqual(self.result()['notice']['state'],'delivery_unknown');self.assertEqual(self.tick(),[])
 def test_historical_completion_not_backfilled_even_if_newly_registered(self):
  self.enable();self.finish(at=runner.now()-1000);self.assertEqual(self.tick(),[]);self.assertNotIn('notice',self.result())
 def test_drover_project_mute_wins_over_completion_option(self):
  self.enable()
  with patch.dict(os.environ,CODEX_THREAD_ID=HUB):notifications.configure_project(self.repo,True,'Existing project mute')
  self.finish();self.assertEqual(self.tick(),[])
 def test_false_complete_or_failed_criterion_is_not_announced(self):
  self.enable();self.finish(verified=False);self.assertEqual(self.tick(),[])
 def test_missing_independent_evidence_blocks_notice(self):
  self.enable();self.finish();(self.d/'evidence/done.log').unlink();self.assertEqual(self.tick(),[])
 def test_stage_and_ordinary_progress_remain_silent(self):
  self.enable();control.register(self.d);self.assertEqual(self.tick(),[])
  for kind in ('workflow','host_handoff','watchdog','operational'):
   self.assertFalse(notifications.disposition(dict(kind=kind,workflow=str(self.d),hub=HUB,state='unpresented'))['notify'])
 def test_silent_default_and_option_restore(self):
  self.enable('silent');self.finish();self.assertEqual(self.tick(),[])
 def test_setting_same_policy_does_not_reset_completion_boundary(self):
  one=self.enable()
  with patch.dict(os.environ,CODEX_THREAD_ID=HUB):two=notifications.configure('user_input_only','Idempotent user setting',completion='once')
  self.assertEqual(one['completion_since'],two['completion_since'])
 def test_foreign_actor_cannot_change_completion_policy(self):
  with self.assertRaises(ValueError):notifications.configure('user_input_only','Not hub',completion='once')
 def test_forged_completion_prefix_without_claim_cannot_use_transport(self):
  self.enable();self.finish()
  with patch('codex_session_adapter.subprocess.run') as send:
   with self.assertRaises(ValueError):done.validate_claim('followup:result-'+'0'*20+':0',HUB,'[ctask-workflow-completed fake]')
   send.assert_not_called()
 def test_wrong_claim_hash_target_and_stale_state_refused(self):
  self.enable();self.finish()
  with patch('completion_notifications.validate_claim',side_effect=ValueError('injected mismatch')):
   self.assertEqual(self.tick(),[])
  self.assertEqual(self.result()['notice']['state'],'delivery_unknown');self.assertEqual(self.tick(),[])
 def test_real_manual_ack_is_required(self):
  self.enable();c=runner.read(self.d/'contract.json');c['acceptance'][0]['kind']='manual';runner.save(self.d/'contract.json',c)
  s=runner.read(self.d/'state.json');s['contract_hash']=runner.digest(c);runner.save(self.d/'state.json',s);self.finish()
  self.assertEqual(self.tick(),[])
 def test_completion_does_not_change_task_authority_or_read_state(self):
  self.enable();self.finish();before={p.name:p.read_bytes() for p in self.d.iterdir() if p.is_file()}
  self.tick();after={p.name:p.read_bytes() for p in self.d.iterdir() if p.is_file()};self.assertEqual(before,after);self.assertNotIn('presented_at',self.result())
