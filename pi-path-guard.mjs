// Model file tools stay inside the bound repo. CLI-owned auth/cache metadata is
// separate and cannot be targeted through these tools. No Bash/MCP is enabled.
import fs from 'node:fs';
import path from 'node:path';
export const SENSITIVE = new Set(['.git','.codex','.cursor','.ato','.pi','.aws','.ssh','.env']);
const TOOLS = new Set(['read','grep','find','ls','edit','write']);
export function resolveExisting(target, inspect=()=>{}, base=path.parse(target).root) {
  if(typeof target!=='string'||target.includes('\0')) throw Error('invalid path');
  // Never normalize unresolved dot-dot segments: symlinks must be followed first.
  const relativeParts=value=>{
    if(value.includes('\0')) throw Error('invalid path');
    if(!path.isAbsolute(value)) return value.split(path.sep);
    if(value===base) return [];
    const prefix=base.endsWith(path.sep)?base:base+path.sep;
    if(!value.startsWith(prefix)) throw Error('outside file policy');
    return value.slice(prefix.length).split(path.sep);
  };
  let pending=relativeParts(target), current=base, links=0;
  inspect(current);
  while(pending.length) {
    const part=pending.shift();
    if(!part||part==='.') continue;
    if(SENSITIVE.has(part.toLowerCase())) throw Error('sensitive path');
    if(part==='..') {
      current=path.dirname(current);
      inspect(current);
      continue;
    }
    const candidate=path.join(current,part);
    inspect(candidate);
    let info;
    try { info=fs.lstatSync(candidate); }
    catch(error) { if(error.code!=='ENOENT') throw error; }
    if(info?.isSymbolicLink()) {
      if(++links>40) throw Error('too many symbolic links');
      const destination=fs.readlinkSync(candidate);
      const parts=relativeParts(destination);
      if(path.isAbsolute(destination)) current=base;
      pending=parts.concat(pending);
    } else {
      current=candidate;
    }
  }
  return current;
}
export function allowed(root, target, tool, readonly) {
  if(!TOOLS.has(tool)||typeof target!=='string'||!target||target.includes('\0')) return false;
  if(target.split(path.sep).some(part=>SENSITIVE.has(part.toLowerCase()))) return false;
  if(readonly&&['edit','write'].includes(tool)) return false;
  try {
    const base=fs.realpathSync(root);
    const inspect=candidate=>{
      const relative=path.relative(base,candidate);
      if(relative==='..'||relative.startsWith('..'+path.sep)||path.isAbsolute(relative)
          ||relative.split(path.sep).some(part=>SENSITIVE.has(part.toLowerCase()))) throw Error('outside file policy');
    };
    resolveExisting(target,inspect,base);
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
