// A coordinator receives exact file tools only. No shell, task administration,
// glob/tree search, native auth or ATO private metadata enters the model tools.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {resolveExisting} from './pi-path-guard.mjs';
export function allowed(root, target, tool, readonly) {
  if (!['read','edit','write'].includes(tool) || (readonly && tool !== 'read')) return false;
  if (typeof target !== 'string' || !target) return false;
  const base=fs.realpathSync(root), actual=resolveExisting(path.resolve(base,target));
  const relative=path.relative(base,actual);
  if (!relative || relative==='..' || relative.startsWith('..'+path.sep) || path.isAbsolute(relative)) return false;
  if (relative.split(path.sep).some(p=>['.git','.codex','.ato','.pi','.aws','.ssh','.env'].includes(p))) return false;
  if (fs.existsSync(actual) && !fs.statSync(actual).isFile()) return false;
  return true;
}
export default function(pi) {
  pi.on('tool_call', async event => {
    try {
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
