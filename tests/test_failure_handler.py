"""Real local lifecycle and host-accounting regressions from deployment follow-ups."""
import json
import os
import unittest
from unittest.mock import patch

import test_interaction as fixtures
from test_interaction import ORIGIN,HUB
from ato_workflow import control
from ato_workflow import failure_attention
import runner


class FailureHandlerTests(unittest.TestCase):
    setUp=fixtures.InteractionTests.setUp
    tearDown=fixtures.InteractionTests.tearDown

    def setup_policy(self):
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
            control.enable('fixture controller',60,0)
            failure_attention.configure('actual user requested one orphan-failure notice')
        control.register(self.d)
        with control.database() as con:
            return json.loads(con.execute('select body from workflows where path=?',(str(self.d),)).fetchone()[0])

    def lifecycle(self,kind,padding=0):
        with (self.home/(ORIGIN+'.jsonl')).open('a') as f:
            f.write(json.dumps(dict(type='event_msg',payload=dict(type=kind)))+'\n')
            if padding:f.write(json.dumps(dict(type='response_item',payload=dict(text='x'*padding)))+'\n')

    def observe(self,row):
        failure_attention.observe(row,self.d,runner.read(self.d/'state.json'),runner.read(self.d/'contract.json'))
        return [a for a in control.rows(True) if a['kind']=='failure_attention']

    def test_active_host_origin_prevents_false_missing_handler_notice(self):
        row=self.setup_policy();self.lifecycle('task_started')
        self.assertEqual(self.observe(row),[])

    def test_long_host_turn_start_is_not_lost_outside_recent_tail(self):
        row=self.setup_policy();self.lifecycle('task_started',700000)
        self.assertTrue(failure_attention.origin_busy(ORIGIN))
        self.assertEqual(self.observe(row),[])

    def test_idle_failed_origin_still_gets_one_notice(self):
        row=self.setup_policy();self.lifecycle('task_started');self.lifecycle('task_complete')
        self.assertEqual(len(self.observe(row)),1)
        self.assertEqual(len(self.observe(row)),1)

    def test_host_accounting_revision_does_not_create_another_failure_episode(self):
        row=self.setup_policy();self.lifecycle('task_complete')
        first=self.observe(row)[0]
        s=runner.read(self.d/'state.json');s['execution_revision']=s.get('execution_revision',0)+1;s['elapsed_seconds']+=2
        runner.save(self.d/'state.json',s)
        self.assertEqual([a['id'] for a in self.observe(row)],[first['id']])

    def test_new_failed_planning_attempt_is_a_new_episode(self):
        row=self.setup_policy();self.lifecycle('task_complete');self.observe(row)
        s=runner.read(self.d/'state.json');s['planning_attempts']=s.get('planning_attempts',0)+1
        runner.save(self.d/'state.json',s)
        self.assertEqual(len(self.observe(row)),2)

    def test_successful_host_recheck_is_not_a_new_failure(self):
        row=self.setup_policy();self.lifecycle('task_complete')
        s=runner.read(self.d/'state.json');s.update(status='blocked',error='Selected host recheck passed; inspect evidence and resume remaining work.')
        stage=self.repo/'fixture-stage';stage.mkdir();runner.save(stage/'state.json',dict(status='completed',owner_pid=None,active_process=None,tasks={}))
        s['active_run']=str(stage);runner.save(self.d/'state.json',s)
        self.assertEqual(self.observe(row),[])
