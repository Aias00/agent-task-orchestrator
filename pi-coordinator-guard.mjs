// Slim coordinator file-tool confinement: only read/edit/write inside bound workspace,
// blocking sensitive directories and enforcing read-only mode when configured.
import fs from 'node:fs';
import path from 'node:path';
import {resolveExisting} from './pi-path-guard.mjs';

const SENSITIVE = new Set(['.git', '.codex', '.ato', '.pi', '.aws', '.ssh', '.env']);

export function allowed(root, target, tool, readonly) {
  if (!['read', 'edit', 'write'].includes(tool) || (readonly && tool !== 'read')) return false;
  if (typeof target !== 'string' || !target) return false;
  const base = fs.realpathSync(root);
  const actual = resolveExisting(path.resolve(base, target));
  const relative = path.relative(base, actual);
  if (!relative || relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative)) return false;
  if (relative.split(path.sep).some(p => SENSITIVE.has(p) || p.startsWith('.env.'))) return false;
  if (fs.existsSync(actual) && !fs.statSync(actual).isFile()) return false;
  return true;
}

export default function(pi) {
  pi.on('tool_call', async event => {
    try {
      const root = process.env.CTASK_AGENT_REPO;
      const readonly = process.env.CTASK_AGENT_SANDBOX === 'read-only';
      if (root && allowed(root, event.input?.path, event.toolName, readonly)) return;
    } catch {}
    return {block: true, reason: 'Outside coordinator file policy'};
  });
}
