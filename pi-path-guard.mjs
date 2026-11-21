// Model file tools stay inside the bound repo. CLI-owned auth/cache metadata is
// separate and cannot be targeted through these tools. No Bash/MCP is enabled.
import fs from 'node:fs';
import path from 'node:path';
export const SENSITIVE = new Set(['.git','.codex','.ato','.pi','.aws','.ssh','.env']);
const TOOLS = new Set(['read','grep','find','ls','edit','write']);
export function resolveExisting(target, inspect=()=>{}) {
  let candidate=path.resolve(target);
  // Resolve one link at a time, including dangling links and sensitive detours.
  for(let links=0; links<=40; links++) {
    inspect(candidate);
    const parsed=path.parse(candidate), parts=candidate.slice(parsed.root.length).split(path.sep);
    let current=parsed.root, rewritten=false;
    for(let i=0; i<parts.length; i++) {
      current=path.join(current,parts[i]);
      let info;
      try { info=fs.lstatSync(current); }
      catch(error) { if(error.code==='ENOENT') return candidate; throw error; }
      if(info.isSymbolicLink()) {
        candidate=path.resolve(path.dirname(current),fs.readlinkSync(current),...parts.slice(i+1));
        rewritten=true; break;
      }
    }
    if(!rewritten) return candidate;
  }
  throw Error('too many symbolic links');
}
export function allowed(root, target, tool, readonly) {
  if(!TOOLS.has(tool)||typeof target!=='string'||!target) return false;
  if(target.split(path.sep).some(part=>SENSITIVE.has(part))) return false;
  if(readonly&&['edit','write'].includes(tool)) return false;
  try {
    const base=fs.realpathSync(root);
    const inspect=candidate=>{
      const relative=path.relative(base,candidate);
      if(relative==='..'||relative.startsWith('..'+path.sep)||path.isAbsolute(relative)
          ||relative.split(path.sep).some(part=>SENSITIVE.has(part))) throw Error('outside file policy');
    };
    const actual=resolveExisting(path.resolve(base,target),inspect);
    inspect(actual);
    return true;
  } catch { return false; }
}
export default function(pi) {
  const root=process.env.CTASK_AGENT_REPO, readonly=process.env.CTASK_AGENT_SANDBOX==='read-only';
  pi.on('tool_call', async event => {
    const name=event.toolName;
    if(!TOOLS.has(name)) return {block:true,reason:'Tool outside scheduler allowlist'};
    try {
      const target=event.input?.path ?? '.';
      if(typeof target!=='string'||!allowed(root,target,name,readonly)) return {block:true,reason:'Path outside scheduler task scope'};
    } catch { return {block:true,reason:'Unresolvable tool path'}; }
  });
}
