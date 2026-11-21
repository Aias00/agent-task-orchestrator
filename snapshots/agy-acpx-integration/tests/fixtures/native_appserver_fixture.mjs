/** Offline native-protocol peer for the actual pinned Codex ACP adapter. */
import fs from 'node:fs';
import readline from 'node:readline';
const send = value => process.stdout.write(JSON.stringify(value)+'\n');
const sid='fixture-native-session';
readline.createInterface({input:process.stdin}).on('line',line=>{
  const m=JSON.parse(line);if(!m.method)return;
  fs.appendFileSync('native-methods.jsonl',JSON.stringify({method:m.method})+'\n');
  let result={};
  if(m.method==='initialize')result={userAgent:'offline-native-fixture',codexHome:process.env.CODEX_HOME};
  if(m.method==='account/read')result={account:null,requiresOpenaiAuth:false};
  if(m.method==='config/read')result={config:{model_provider:'openai'},layers:[]};
  if(m.method==='skills/list')result={data:[]};
  if(m.method==='mcpServerStatus/list')result={data:[],nextCursor:null};
  if(m.method==='thread/start')result={activePermissionProfile:{id:'ato-worker',extends:':workspace'},
    approvalPolicy:'never',cwd:process.cwd(),thread:{id:sid},model:'fixture-model',reasoningEffort:'medium',modelProvider:'openai'};
  if(m.method==='model/list')result={data:[{id:'fixture-model',model:'fixture-model',displayName:'Offline Fixture',
    description:'synthetic model; no provider',isDefault:true,defaultReasoningEffort:'medium',
    supportedReasoningEfforts:[{reasoningEffort:'medium',description:'fixture'}]}],nextCursor:null};
  if(m.method==='turn/start') {
    fs.writeFileSync('native-turn.json',JSON.stringify(m.params));
    const turn={id:'fixture-turn',status:'inProgress',items:[],error:null};
    send({id:m.id,result:{turn}});
    setTimeout(()=>{
      const value={status:'ready_for_checks',summary:'offline pinned adapter fixture',blocker:'',next_step:'',retry_after_seconds:1};
      for(const [id,phase,text] of [['interim','commentary','Editing the fixture.'],['final','final_answer',JSON.stringify(value)]]) {
        const item={type:'agentMessage',id,phase,text};
        send({method:'item/started',params:{threadId:sid,turnId:turn.id,item}});
        send({method:'item/agentMessage/delta',params:{threadId:sid,turnId:turn.id,itemId:id,delta:text}});
        send({method:'item/completed',params:{threadId:sid,turnId:turn.id,item}});
      }
      send({method:'turn/completed',params:{threadId:sid,turn:{...turn,status:'completed'}}});
    },20);
    return;
  }
  if(m.id!==undefined)send({id:m.id,result});
});
