/** Synthetic ACP peer. No native model, login, network or external packages. */
import fs from 'node:fs';
import readline from 'node:readline';
import { randomUUID } from 'node:crypto';

const send = value => process.stdout.write(JSON.stringify({ jsonrpc: '2.0', ...value })+'\n');
const reply = (id, result) => send({ id, result });
const update = (sessionId, value) => send({ method: 'session/update', params: { sessionId, update: value } });
const fixture = fs.existsSync('fixture-config.json') ? JSON.parse(fs.readFileSync('fixture-config.json')) : {};
const mode = { id: 'mode', name: 'Mode', type: 'select', currentValue: 'read-only',
  options: [{ value: 'read-only', name: 'Read' }, { value: 'agent', name: 'Write' }] };
const pending = new Map();
const sid = randomUUID();
let hanging;

async function handle(message) {
  const { id, method, params = {} } = message;
  if (!method) {
    pending.get(id)?.(message.result);
    pending.delete(id);
    return;
  }
  if (method === 'initialize') {
    fs.writeFileSync('fixture-client-capabilities.json', JSON.stringify(params.clientCapabilities));
    reply(id, { protocolVersion: 1, agentCapabilities: { loadSession: !!fixture.resumable },
                agentInfo: { name: 'synthetic-acpx-peer', version: '1' } });
  } else if (method === 'session/new') {
    reply(id, { sessionId: sid, ...(fixture.no_mode ? {} : { configOptions: [mode] }) });
  } else if (method === 'session/set_config_option') {
    mode.currentValue = fixture.wrong_mode ? 'agent-full-access' : params.value;
    reply(id, { configOptions: [mode] });
  } else if (method === 'session/prompt') {
    const text = params.prompt.map(item => item.text ?? '').join('');
    const scenario = /scenario:([a-z_-]+)/.exec(text)?.[1] ?? 'success';
    fs.appendFileSync('fixture-admissions', sid+'\n');
    fs.writeFileSync('fixture-process.json', JSON.stringify({ pid: process.pid, ppid: process.ppid }));
    if (scenario === 'hang' || scenario === 'cancel') {
      hanging = { id, session: params.sessionId };
      setTimeout(() => {}, 60000);
      return;
    }
    if (scenario === 'eof') { process.exit(0); }
    if (scenario === 'permission') {
      const response = new Promise(resolve => pending.set('permission-1', resolve));
      send({ id: 'permission-1', method: 'session/request_permission', params: {
        sessionId: sid, toolCall: { toolCallId: 'dangerous', title: 'PRIVATE_SECRET', kind: 'execute' },
        options: [{ optionId: 'allow', name: 'Allow', kind: 'allow_once' }],
      } });
      fs.writeFileSync('fixture-permission.json', JSON.stringify(await response));
    }
    if (scenario === 'flood') {
      for (let i = 0; i < 500; i++) update(sid, { sessionUpdate: 'tool_call',
        toolCallId: 'PRIVATE_SECRET_'+i, title: 'PRIVATE_SECRET', kind: 'edit', status: 'completed',
        rawInput: { secret: 'PRIVATE_SECRET' }, locations: [{ path: '/PRIVATE_SECRET' }] });
    } else {
      update(sid, { sessionUpdate: 'tool_call', toolCallId: 'write', title: 'PRIVATE_SECRET',
        kind: 'edit', status: 'completed', rawInput: { private: 'PRIVATE_SECRET' } });
    }
    const target = /fixture-value:([A-Za-z0-9_-]+)/.exec(text)?.[1] ?? 'dynamic-default';
    fs.writeFileSync('result.txt', target);
    const result = { status: 'complete', summary: 'fixture wrote '+target, blocker: '', next_step: '', retry_after_seconds: 1 };
    let output = JSON.stringify(result);
    if (scenario === 'malformed') output = output.slice(0, -1);
    if (scenario === 'duplicate') output = output.replace('"status":"complete"', '"status":"blocked","status":"complete"');
    if (scenario === 'prose') output = 'Done! '+output;
    if (scenario === 'schema') output = JSON.stringify({ ...result, retry_after_seconds: true });
    if (scenario === 'ambiguous') output += JSON.stringify(result);
    if (scenario === 'oversize') output = 'x'.repeat(1024 * 1024 + 1);
    if (scenario === 'thought_only') {
      update(sid, { sessionUpdate: 'agent_thought_chunk', content: { type: 'text', text: output } });
    } else if (scenario !== 'empty') {
      if (scenario === 'commentary') update(sid, { sessionUpdate: 'agent_message_chunk',
        messageId: 'interim', _meta: { codex: { phase: 'commentary' } },
        content: { type: 'text', text: 'I will edit the file, then check it.' } });
      update(scenario === 'wrong_session' ? 'unrelated-session' : sid,
        { sessionUpdate: 'agent_message_chunk', messageId: 'final',
          _meta: { codex: { phase: 'final_answer' } }, content: { type: 'text', text: output } });
    }
    if (scenario === 'rpc_error') send({ id, error: { code: -32603, message: 'PRIVATE_SECRET' } });
    else reply(id, { stopReason: scenario === 'truncated' ? 'max_tokens' : scenario === 'cancelled' ? 'cancelled' : 'end_turn' });
  } else if (method === 'session/cancel') {
    if (hanging && !fixture.ignore_cancel) {
      reply(hanging.id, { stopReason: 'cancelled' });
      hanging = undefined;
    }
  } else if (id !== undefined) {
    send({ id, error: { code: -32601, message: 'unsupported fixture operation' } });
  }
}
const lines = readline.createInterface({ input: process.stdin });
lines.on('line', line => { void handle(JSON.parse(line)).catch(() => process.exit(2)); });
lines.on('close', () => process.exit(0));
