// A coordinator receives exact file tools only. No shell, task administration,
// glob/tree search, native auth or ATO private metadata enters the model tools.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import net from 'node:net';
import {resolveExisting} from './pi-path-guard.mjs';
export function allowed(root, target, tool, readonly) {
  if (!['read','edit','write'].includes(tool) || (readonly && tool !== 'read')) return false;
  if (typeof target !== 'string' || !target) return false;
  const base=fs.realpathSync(root), actual=resolveExisting(path.resolve(base,target));
  const relative=path.relative(base,actual);
  if (!relative || relative==='..' || relative.startsWith('..'+path.sep) || path.isAbsolute(relative)) return false;
  if (relative.split(path.sep).some(p=>['.git','.codex','.ato','.pi','.aws','.ssh','.env'].includes(p))) return false;
  if (process.env.ATO_PI_PROFILE_HASH && relative.split(path.sep).some(p=>p.startsWith('.env.'))) return false;
  if (fs.existsSync(actual) && !fs.statSync(actual).isFile()) return false;
  return true;
}

export async function authorizeAction(operation) {
  if(!['tool','provider_request'].includes(operation)||!process.env.ATO_PI_PROFILE_HASH
      ||!process.env.ATO_PI_CONTROL_SOCKET) throw Error('Production action unavailable');
  const runtime=process.env.CTASK_PI_RUNTIME;
  const state=JSON.parse(fs.readFileSync(path.join(runtime,'state.json'),'utf8'));
  const authority=JSON.parse(fs.readFileSync(path.join(runtime,'tool-authority.json'),'utf8'));
  const control=JSON.parse(fs.readFileSync(path.join(runtime,'control.json'),'utf8'));
  if(state.production_profile_hash!==process.env.ATO_PI_PROFILE_HASH||state.lifecycle!=='active'
      ||state.turns[authority.message_id]?.turn_id!==authority.turn_id
      ||state.turns[authority.message_id]?.state!=='dispatching') throw Error('Production turn stale');
  return await new Promise((resolve,reject)=>{
    const socket=net.connect(process.env.ATO_PI_CONTROL_SOCKET);let body='';
    const fail=()=>{socket.destroy();reject(Error('Production authorization rejected'));};
    const timer=setTimeout(fail,2000);
    socket.on('connect',()=>socket.write(JSON.stringify({token:control.token,host_id:state.host_id,
      action:'authorize_action',operation,turn_id:authority.turn_id,
      profile_hash:process.env.ATO_PI_PROFILE_HASH})+'\n'));
    socket.on('data',chunk=>{
      body+=chunk.toString();if(body.length>4096){clearTimeout(timer);fail();return;}
      if(!body.includes('\n'))return;
      clearTimeout(timer);
      try {const value=JSON.parse(body.trim());
        if(value.result?.allowed!==true)throw Error();socket.destroy();resolve(value.result);
      }catch{fail();}
    });
    socket.on('error',()=>{clearTimeout(timer);fail();});
    socket.on('end',()=>{clearTimeout(timer);fail();});
  });
}
export default function(pi) {
  pi.on('tool_call', async event => {
    try {
      if(process.env.ATO_PI_PROFILE_HASH) await authorizeAction('tool');
      const runtime=process.env.CTASK_PI_RUNTIME;
      const state=JSON.parse(fs.readFileSync(path.join(runtime,'state.json'),'utf8'));
      const authority=JSON.parse(fs.readFileSync(path.join(runtime,'tool-authority.json'),'utf8'));
      if (state.lifecycle !== 'active' || state.turns[authority.message_id]?.turn_id !== authority.turn_id
          || state.turns[authority.message_id]?.state !== 'dispatching') throw Error('Inactive turn');
      if (path.isAbsolute(authority.workflow_id)) {
        const d=authority.workflow_id, current=JSON.parse(fs.readFileSync(path.join(d,'state.json'),'utf8'));
        const sha=name=>crypto.createHash('sha256').update(fs.readFileSync(path.join(d,name))).digest('hex');
        const events=path.join(d,'events.jsonl');
        const eventsSha=crypto.createHash('sha256').update(fs.existsSync(events)?fs.readFileSync(events):Buffer.alloc(0)).digest('hex');
        if (fs.existsSync(path.join(d,'STOP')) || fs.existsSync(path.join(d,'user-closure.json'))
            || ['stopped','completed'].includes(current.status)
            || current.contract_hash !== authority.contract_hash
            || (current.execution_revision ?? 0) !== authority.generation
            || sha('contract.json') !== authority.contract_file_sha256
            || sha('origin.json') !== authority.origin_file_sha256
            || eventsSha !== authority.workflow_events_sha256) throw Error('Stale workflow');
      }
      if (allowed(process.env.CTASK_AGENT_REPO,event.input?.path,event.toolName,
                  process.env.CTASK_AGENT_SANDBOX==='read-only')) return;
    } catch {}
    return {block:true,reason:'Outside managed coordinator file policy'};
  });
}
