import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import runner
import workflow
import recovery
import interaction

ORIGIN = '11111111-1111-4111-8111-000000000001'
HUB = '11111111-1111-4111-8111-000000000002'


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.repo = Path(self.tmp.name)
        subprocess.run(['git','init','-q',str(self.repo)],check=True)
        self.home = self.repo/'codex-home';self.home.mkdir()
        with sqlite3.connect(self.home/'state_5.sqlite') as con:
            con.execute('create table threads (id text, rollout_path text)')
            for thread in [ORIGIN,HUB]:
                path = self.home/(thread+'.jsonl')
                path.write_text(json.dumps({'type':'session_meta','payload':{'id':thread}})+'\n')
                con.execute('insert into threads values (?,?)',(thread,str(path)))
        self.env = dict(os.environ,CODEX_HOME=str(self.home),CODEX_THREAD_ID=ORIGIN)
        self.patch = patch.dict(os.environ,self.env);self.patch.start()
        p=self.repo/'input.json';runner.save(p,dict(objective='A complete task',scope='Fixture repo only',
            authorization='Local fixture checks',memory=False,
            acceptance=[dict(id='file',description='File exists',checks=[['test','-f','result']])]))
        self.d=workflow.create(self.repo,p,ORIGIN)
        s=runner.read(self.d/'state.json');s.update(status='blocked');runner.save(self.d/'state.json',s)
        interaction.configure(HUB,'User requested this conversation as the unified interaction hub')

    def tearDown(self):
        self.patch.stop();self.tmp.cleanup()

    def transport(self,argv,**kwargs):
        target=argv[argv.index('--thread')+1]
        return subprocess.CompletedProcess(argv,0,f'Queued message {ORIGIN} for thread {target}.\n','')

    def ask(self):
        return interaction.ask(self.d,'Which option?', 'A, because it preserves compatibility', 'Existing agreed scope')

    def test_round_trip_routes_to_hub_then_original_and_accepts_once(self):
        with patch('interaction.subprocess.run',side_effect=self.transport) as send:
            q=self.ask()
            self.assertEqual(send.call_args.args[0][3],HUB)
            with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
                self.assertEqual(interaction.inbox()[0]['id'],q['id'])
                answered=interaction.answer(q['id'],'Choose A','Hub user explicitly chose A at 12:00')
                self.assertEqual(send.call_args.args[0][3],ORIGIN)
                interaction.answer(q['id'],'Choose A','Hub user explicitly chose A at 12:00')
            self.assertEqual(send.call_count,2)
            self.assertEqual(answered['state'],'answered')
            accepted=interaction.accept(q['id']);self.assertEqual(accepted['state'],'accepted')
            with self.assertRaises(ValueError):interaction.accept(q['id'])
        self.assertEqual(runner.read(self.d/'state.json')['status'],'blocked')

    def test_wrong_conversation_cannot_ask_answer_or_accept(self):
        with patch('interaction.subprocess.run',side_effect=self.transport):q=self.ask()
        with self.assertRaises(ValueError):interaction.answer(q['id'],'A','not in hub')
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
            with self.assertRaises(ValueError):self.ask()
            with self.assertRaises(ValueError):interaction.accept(q['id'])

    def test_no_hub_preserves_original_question_flow(self):
        (interaction.directory()/'hub.json').unlink()
        with patch('interaction.subprocess.run') as send:
            self.assertFalse(self.ask()['routed']);send.assert_not_called()

    def test_repeated_questions_dont_duplicate_notifications(self):
        with patch('interaction.subprocess.run',side_effect=self.transport) as send:
            first=self.ask();second=self.ask()
            self.assertEqual(first['id'],second['id']);self.assertEqual(send.call_count,1)

    def test_new_execution_revision_allows_fresh_acceptance_question(self):
        with patch('interaction.subprocess.run',side_effect=self.transport) as send:
            first=self.ask()
            s=runner.read(self.d/'state.json');s['execution_revision']=1;runner.save(self.d/'state.json',s)
            second=self.ask()
            self.assertNotEqual(first['id'],second['id']);self.assertEqual(send.call_count,2)
            with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
                with self.assertRaises(ValueError):interaction.answer(first['id'],'A','reply to an old version')

    def test_unknown_question_delivery_is_never_blind_retried(self):
        with patch('interaction.subprocess.run',side_effect=subprocess.TimeoutExpired('queue',25)) as send:
            first=self.ask();self.ask()
            self.assertEqual(send.call_count,1)
            self.assertEqual(first['question_delivery']['state'],'delivery_unknown')

    def test_unknown_answer_delivery_is_never_blind_retried(self):
        with patch('interaction.subprocess.run',side_effect=self.transport):q=self.ask()
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB),patch('interaction.subprocess.run',side_effect=subprocess.TimeoutExpired('queue',25)) as send:
            first=interaction.answer(q['id'],'A','user said A')
            interaction.answer(q['id'],'A','user said A')
            self.assertEqual(send.call_count,1)
            self.assertEqual(first['answer_delivery']['state'],'delivery_unknown')
            with self.assertRaises(ValueError):interaction.answer(q['id'],'B','changed without new question')

    def test_stop_and_contract_changes_reject_late_answers(self):
        with patch('interaction.subprocess.run',side_effect=self.transport):q=self.ask()
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB),patch('interaction.subprocess.run') as send:
            (self.d/'STOP').touch()
            with self.assertRaises(ValueError):interaction.answer(q['id'],'A','actual reply')
            self.assertIn('stale_reason',interaction.inbox()[0])
            (self.d/'STOP').unlink()
            c=runner.read(self.d/'contract.json');c['objective']='Changed scope';runner.save(self.d/'contract.json',c)
            with self.assertRaises(ValueError):interaction.answer(q['id'],'A','actual reply')
            send.assert_not_called()

    def test_same_origin_and_hub_avoids_self_queue(self):
        interaction.configure(ORIGIN,'User interacts in original conversation')
        with patch('interaction.subprocess.run') as send:
            q=self.ask();self.assertEqual(q['question_delivery']['state'],'local')
            a=interaction.answer(q['id'],'A','user replied here')
            self.assertEqual(a['answer_delivery']['state'],'local');send.assert_not_called()

    def test_hub_change_does_not_rebind_existing_question(self):
        with patch('interaction.subprocess.run',side_effect=self.transport):q=self.ask()
        interaction.configure(ORIGIN,'Changed default for future questions')
        self.assertEqual(runner.read(interaction.question_path(q['id']))['hub_thread'],HUB)
        with self.assertRaises(ValueError):interaction.answer(q['id'],'A','wrong hub')

    def test_takeover_needs_input_routes_question_and_recommendation(self):
        recovery.enable(self.d,'User enabled recovery')
        with patch('recovery.subprocess.run',side_effect=self.transport) as send:
            recovery.tick(self.d)
            r=runner.read(self.d/'recovery.json');request=r['requests'][-1]['id']
            result=recovery.takeover(self.d,request,'needs_input','May I change the compatibility requirement?',
                                     recommendation='Keep compatibility; narrow this fix')
            self.assertEqual(result['interaction']['hub_thread'],HUB)
            self.assertIn('Keep compatibility',result['interaction']['recommendation'])
            self.assertEqual(send.call_count,2)
            again=interaction.ask(self.d,'Repeated question',request_id=request)
            self.assertEqual(again['id'],result['interaction']['id']);self.assertEqual(send.call_count,2)

    def test_recovery_resumed_before_answer_marks_question_stale(self):
        recovery.enable(self.d,'User enabled recovery')
        with patch('recovery.subprocess.run',side_effect=self.transport):
            recovery.tick(self.d);r=runner.read(self.d/'recovery.json');request=r['requests'][-1]['id']
            q=recovery.takeover(self.d,request,'needs_input','Which option?')['interaction']
        r=runner.read(self.d/'recovery.json');r['requests'][-1]['state']='resumed';runner.save(self.d/'recovery.json',r)
        with patch.dict(os.environ,CODEX_THREAD_ID=HUB):
            with self.assertRaises(ValueError):interaction.answer(q['id'],'A','late reply')

    def test_public_cli_round_trip_with_real_transport_process(self):
        queue=self.repo/'queue-fixture'
        queue.write_text(f'''#!{sys.executable}
import sys,json
target=sys.argv[sys.argv.index('--thread')+1]
with open({str(self.repo/'transport.jsonl')!r},'a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
print('Queued message '+target+' for thread '+target+'.')
''');queue.chmod(0o755)
        env=dict(self.env,CTASK_QUEUE_BIN=str(queue))
        def cli(*args,thread=ORIGIN):
            p=subprocess.run([sys.executable,str(ROOT/'runner.py'),*args],env=dict(env,CODEX_THREAD_ID=thread),capture_output=True,text=True)
            self.assertEqual(p.returncode,0,p.stderr);return json.loads(p.stdout)
        q=cli('ask',str(self.d),'--question','Choose A or B?','--recommendation','A')
        self.assertEqual(cli('inbox',thread=HUB)[0]['id'],q['id'])
        cli('answer',q['id'],'--answer','A; literal $(touch do-not-create)','--source','Fixture user reply',thread=HUB)
        result=cli('accept-answer',q['id'])
        self.assertIn('$(touch do-not-create)',result['answer'])
        self.assertFalse((self.repo/'do-not-create').exists())
        records=[json.loads(x) for x in (self.repo/'transport.jsonl').read_text().splitlines()]
        self.assertEqual([r[r.index('--thread')+1] for r in records],[HUB,ORIGIN])


if __name__ == '__main__':unittest.main()
