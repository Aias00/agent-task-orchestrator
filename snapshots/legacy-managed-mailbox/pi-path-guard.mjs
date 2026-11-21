// Model file tools stay inside the bound repo. CLI-owned auth/cache metadata is
// separate and cannot be targeted through these tools. No Bash/MCP is enabled.
import fs from 'node:fs';
import path from 'node:path';
export function resolveExisting(target) {
  let p=path.resolve(target), parts=[];
  while(!fs.existsSync(p)) { parts.unshift(path.basename(p)); const next=path.dirname(p); if(next===p) throw Error('unresolvable path'); p=next; }
  return path.join(fs.realpathSync(p), ...parts);
}
export function allowed(root, target, tool, readonly) {
  const base=fs.realpathSync(root), actual=resolveExisting(path.resolve(base,target));
  const relative=path.relative(base,actual);
  if(relative==='..'||relative.startsWith('..'+path.sep)||path.isAbsolute(relative)) return false;
  if(['edit','write'].includes(tool)) {
    if(readonly) return false;
    if(relative==='.git'||relative.startsWith('.git'+path.sep)||relative==='.codex'||relative.startsWith('.codex'+path.sep)) return false;
  }
  return true;
}
export default function(pi) {
  const root=process.env.CTASK_AGENT_REPO, readonly=process.env.CTASK_AGENT_SANDBOX==='read-only';
  pi.on('tool_call', async event => {
    const name=event.toolName;
    if(!['read','grep','find','ls','edit','write'].includes(name)) return {block:true,reason:'Tool outside scheduler allowlist'};
    try {
      const target=event.input?.path ?? '.';
      if(typeof target!=='string'||!allowed(root,target,name,readonly)) return {block:true,reason:'Path outside scheduler task scope'};
    } catch { return {block:true,reason:'Unresolvable tool path'}; }
  });
}
