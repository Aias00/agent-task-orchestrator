/** Trusted, pinned native app-server policy gateway. Never a task scheduler.
 * The adapter's legacy per-turn sandbox preset must not erase named read denies.
 * Only one fresh thread and one turn are admitted. No auth mutation or retries.
 */
import fs from 'node:fs';
import { spawn } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const MAX_FRAME = 2 * 1024 * 1024;
export class NativePolicy {
  constructor(config) {
    this.config = config; this.thread = undefined; this.starts = 0; this.turns = 0;
    this.pending = new Map(); this.denials = new Set();
  }
  outbound(message) {
    if (message.method === undefined) {
      // No upstream approval response can expand permissions: native requests
      // are answered locally with rejection and are never sent to the adapter.
      throw new Error('unsolicited native response');
    }
    const method = message.method, params = message.params ?? {};
    const read = new Set(['initialize', 'initialized', 'config/read', 'model/list',
      'account/read', 'skills/list', 'mcpServerStatus/list']);
    if (method === 'thread/start') {
      if (++this.starts !== 1 || params.cwd !== this.config.repo ||
          params.dynamicTools?.length || params.environments?.length) throw new Error('invalid fresh native session');
      message = { ...message, params: { cwd: this.config.repo, config: this.config.native_config,
        permissions: 'ato-worker', approvalPolicy: 'never', approvalsReviewer: 'user',
        runtimeWorkspaceRoots: [this.config.repo], environments: [],
        ...(params.modelProvider ? { modelProvider: params.modelProvider } : {}) } };
    } else if (method === 'turn/start') {
      if (!this.thread || params.threadId !== this.thread || ++this.turns !== 1 ||
          !Array.isArray(params.input) || params.input.some(item => item.type !== 'text')) {
        throw new Error('invalid/repeated native turn');
      }
      // Allowlist scalar model controls; no cwd, tools, extra roots, grants or
      // legacy sandbox policy survives. Schema comes from ATO, not model input.
      const next = { threadId: this.thread, input: params.input, permissions: 'ato-worker',
        approvalPolicy: 'never', approvalsReviewer: 'user', environments: [],
        runtimeWorkspaceRoots: [this.config.repo], outputSchema: this.config.schema };
      for (const key of ['model', 'effort', 'summary', 'serviceTier']) {
        if (params[key] !== undefined) next[key] = params[key];
      }
      message = { ...message, params: next };
    } else if (['turn/interrupt', 'thread/unsubscribe'].includes(method)) {
      if (params.threadId !== this.thread) throw new Error('foreign native session');
    } else if (!read.has(method)) {
      throw new Error('unsupported native method');
    }
    if (message.id !== undefined) {
      if (this.pending.size > 64 || this.pending.has(message.id)) throw new Error('native request bound');
      this.pending.set(message.id, method);
    }
    return message;
  }
  inbound(message) {
    if (message.method && message.id !== undefined) {
      // Approval/user-input/elicitation are never delegated or auto-approved.
      if (this.denials.size >= 32) throw new Error('native request bound');
      this.denials.add(message.id);
      return { deny: { id: message.id, error: { code: -32601, message: 'ATO denies native interaction' } } };
    }
    if (message.id !== undefined) {
      const method = this.pending.get(message.id);
      if (!method) throw new Error('unmatched native response');
      this.pending.delete(message.id);
      if (method === 'thread/start' && !message.error) {
        const result = message.result;
        if (result?.activePermissionProfile?.id !== 'ato-worker' ||
            result.activePermissionProfile.extends !== this.config.extends ||
            result.approvalPolicy !== 'never' || result.cwd !== this.config.repo ||
            typeof result.thread?.id !== 'string' || !result.thread.id) {
          throw new Error('native did not acknowledge restricted named policy');
        }
        this.thread = result.thread.id;
      }
    }
    return { forward: message };
  }
}

export async function proxy(config) {
  if (config.version !== 1 || !config.native || !config.repo || !config.schema ||
      ![':workspace', ':read-only'].includes(config.extends)) throw new Error('invalid trusted native config');
  const policy = new NativePolicy(config);
  const native = spawn(config.native, config.args, {
    cwd: config.repo, env: config.env, stdio: ['pipe', 'pipe', 'ignore'], detached: false,
  });
  let failed = false;
  const fail = () => { failed = true; native.kill('SIGTERM'); };
  function pipeLines(stream, receive) {
    let data = Buffer.alloc(0), total = 0;
    stream.on('data', chunk => {
      total += chunk.length;
      if (total > 32 * MAX_FRAME) { fail(); return; }
      data = Buffer.concat([data, chunk]);
      let end;
      while ((end = data.indexOf(10)) >= 0) {
        const line = data.subarray(0, end); data = data.subarray(end + 1);
        try {
          if (!line.length || line.length > MAX_FRAME) throw new Error('native frame bound');
          receive(JSON.parse(line.toString('utf8')));
        } catch { fail(); return; }
      }
      if (data.length > MAX_FRAME) fail();
    });
    stream.on('end', () => { if (data.length) fail(); });
  }
  pipeLines(process.stdin, message => {
    // Pinned ACP publishes an optional goal view during session setup. Return
    // unsupported locally; never create/read a native goal or kill a healthy
    // worker for this best-effort capability query. Mutations remain fatal.
    if (message.method === 'thread/goal/get' && message.id !== undefined) {
      process.stdout.write(JSON.stringify({ id: message.id,
        error: { code: -32601, message: 'ATO worker has no goal capability' } })+'\n');
      return;
    }
    native.stdin.write(JSON.stringify(policy.outbound(message))+'\n');
  });
  pipeLines(native.stdout, message => {
    const action = policy.inbound(message);
    if (action.deny) native.stdin.write(JSON.stringify(action.deny)+'\n');
    if (action.forward) process.stdout.write(JSON.stringify(action.forward)+'\n');
  });
  process.stdin.on('end', () => native.stdin.end());
  native.stdin.on('error', fail); native.on('error', fail);
  process.on('SIGTERM', fail); process.on('SIGINT', fail);
  const code = await new Promise(resolve => native.on('close', (code, signal) => resolve(signal ? 1 : code)));
  process.exitCode = failed ? 1 : code ?? 1;
  process.stdin.destroy();
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    if (process.argv.slice(2).join(' ') !== 'app-server') throw new Error('app-server only');
    const config = JSON.parse(fs.readFileSync(process.env.ATO_NATIVE_POLICY, 'utf8'));
    await proxy(config);
  } catch { process.exitCode = 1; }
}
