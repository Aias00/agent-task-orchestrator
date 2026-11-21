/** One fresh ATO attempt through the public acpx runtime. No CLI or queue. */
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';

const MAX_TEXT = 1024 * 1024;
const SAFE_CODES = new Set(['ACP_BACKEND_MISSING','ACP_BACKEND_UNAVAILABLE','ACP_BACKEND_UNSUPPORTED_CONTROL','ACP_DISPATCH_DISABLED','ACP_INVALID_RUNTIME_OPTION','ACP_SESSION_INIT_FAILED','ACP_TURN_FAILED']);
const opaque = value => createHash('sha256').update(String(value)).digest('hex').slice(0, 24);

// Pinned Codex ACP 1.1.5 preserves native phases here, whereas acpx rich text
// events intentionally omit nested vendor metadata. No text/prose extraction.
export class CodexFinalText {
  constructor() { this.messages = new Map(); this.bytes = 0; this.final = undefined; this.invalid = false; }
  accept(update) {
    if (update?.sessionUpdate !== 'agent_message_chunk') return;
    const id = update.messageId, phase = update._meta?.codex?.phase;
    if (typeof id !== 'string' || !id || id.length > 256 ||
        !['commentary', 'final_answer'].includes(phase) ||
        update.content?.type !== 'text' || typeof update.content.text !== 'string') {
      this.invalid = true; return;
    }
    this.bytes += Buffer.byteLength(update.content.text);
    const previous = this.messages.get(id);
    if (this.bytes > MAX_TEXT || this.messages.size >= 64 && !previous ||
        previous && previous.phase !== phase ||
        phase === 'final_answer' && this.final && this.final !== id ||
        phase === 'commentary' && this.final) {
      this.invalid = true; return;
    }
    if (phase === 'final_answer') this.final = id;
    this.messages.set(id, { phase, text: (previous?.text ?? '') + update.content.text });
  }
  result() {
    if (this.invalid || !this.final) throw new Error('missing/ambiguous pinned final message');
    return this.messages.get(this.final).text;
  }
}

export async function execute(config) {
  if (config.version !== 1 || !Array.isArray(config.argv) || !config.argv.length ||
      !Number.isInteger(config.timeout_ms) || config.timeout_ms < 1 || config.timeout_ms > 300000 ||
      typeof config.text !== 'string' || Buffer.byteLength(config.text) > MAX_TEXT + 65536) {
    throw new Error('invalid trusted bridge configuration');
  }
  const { createAcpRuntime } = await import(config.runtime_url);
  const { createAgentRegistry } = await import(config.registry_url);
  const records = new Map();
  const abort = new AbortController();
  let runtime, turn, text = '', bytes = 0, launches = 0, permissionDenied = 0;
  let phase = 'setup', dispatched = false;
  let observations = 0, toolCalls = 0, dropped = 0, revoked = false;
  let activeSession, foreignUpdate = false, wireBytes = 0;
  const pinnedText = config.final_parser === 'codex-1.1.5' ? new CodexFinalText() : undefined;
  const metadata = [];
  const assertActive = () => {
    if (foreignUpdate) throw new Error('foreign-session ACP update');
    if (revoked || abort.signal.aborted || fs.existsSync(config.stop_file)) {
      revoked = true;
      throw new Error('ATO authority revoked');
    }
  };
  const cancel = () => {
    revoked = true;
    abort.abort();
    if (turn) void turn.cancel({ reason: 'ATO cancelled', timeoutMs: 250 }).catch(() => {});
  };
  const signalHandler = () => cancel();
  process.on('SIGTERM', signalHandler);
  process.on('SIGINT', signalHandler);
  const timer = setTimeout(cancel, config.timeout_ms);
  const poll = setInterval(() => { if (fs.existsSync(config.stop_file)) cancel(); }, 50);
  // The runner remains the hard process-group fence even if library teardown
  // stalls. Never turn a bounded wait into a detached agent or retry.
  const hard = setTimeout(() => process.exit(1), config.timeout_ms + 4000);
  try {
    assertActive();
    runtime = createAcpRuntime({
      cwd: config.cwd,
      sessionStore: {
        async load(id) { return records.has(id) ? structuredClone(records.get(id)) : undefined; },
        async save(record) {
          if (records.size > 1 || Buffer.byteLength(JSON.stringify(record)) > 4 * MAX_TEXT) {
            throw new Error('in-memory transport state bound exceeded');
          }
          records.set(record.acpxRecordId, structuredClone(record));
        },
      },
      agentRegistry: createAgentRegistry({ overrides: { [config.agent]: config.argv } }),
      agentProcessEnvAllow: [],
      agentProcessEnv: config.agent_env,
      mcpServers: [], fs: false, terminal: false, elicitationModes: [],
      permissionMode: 'deny-all', nonInteractivePermissions: 'deny',
      onPermissionRequest: async () => { permissionDenied++; return { outcome: 'cancel' }; },
      timeoutMs: config.timeout_ms,
      onAcpMessage(direction, message) {
        // Runtime v1 rich events do not retain sessionId. Check the actual
        // update envelope before consuming them; never persist wire payloads.
        if (direction === 'inbound' && message.method === 'session/update' && activeSession &&
            message.params?.sessionId !== activeSession) foreignUpdate = true;
        if (direction === 'inbound' && message.method === 'session/update' &&
            activeSession && message.params?.sessionId === activeSession) {
          pinnedText?.accept(message.params.update);
        }
        // Bound the accepted wire stream independently of sanitized telemetry.
        // The public callback is observation-only, so revoke dispatch/consume
        // authority rather than relying on a thrown callback to stop the client.
        wireBytes += Buffer.byteLength(JSON.stringify(message));
        if (wireBytes > 8 * MAX_TEXT) cancel();
      },
      processLifecycle: {
        onBeforeSpawn(launch) {
          assertActive();
          // A single exact launch. Reject version probes, reconnect/fallback,
          // package-exec and any alternate command before subprocess creation.
          if (++launches !== 1 || launch.command !== config.argv[0] ||
              JSON.stringify(launch.args) !== JSON.stringify(config.argv.slice(1)) ||
              launch.scope.kind !== 'runtime-session' || launch.cwd !== config.cwd) {
            throw new Error('unexpected acpx process launch');
          }
        },
      },
      observation: {
        maxEventsPerTurn: 64, maxPendingEvents: 16, maxPendingBytes: 16384,
        maxToolsPerTurn: 32, sinkTimeoutMs: 100,
        sink(event) {
          // Do not serialize peer text, titles, paths, tool inputs, wire frames
          // or producer-supplied identifiers. Fixed counters only.
          observations++;
          toolCalls = Math.max(toolCalls, event.toolCalls);
          dropped = Math.max(dropped, event.dropped);
          if (metadata.length < 64) metadata.push({
            sequence: event.sequence, type: event.type,
            ...(event.toolId ? { tool: opaque(event.toolId), state: event.toolStatus ?? 'unknown' } : {}),
          });
        },
      },
    });
    phase = 'ensure_session';
    const handle = await runtime.ensureSession({
      sessionKey: `ato:${config.correlation.turn}`, agent: config.agent,
      mode: 'persistent', cwd: config.cwd,
    });
    activeSession = handle.backendSessionId;
    if (!activeSession) throw new Error('missing native ACP session identity');
    const capabilityEvidence = await runtime.getCapabilityEvidence({ handle });
    assertActive();
    phase = 'mode';
    if (config.required_mode) {
      const status = await runtime.getStatus({ handle });
      const mode = status.details?.configOptions?.find(entry => entry.id === 'mode');
      if (!mode || mode.type !== 'select' || !mode.options?.some(entry => entry.value === config.required_mode)) {
        throw new Error('backend does not advertise required sandbox mode');
      }
      const accepted = await runtime.setConfigOption({ handle, key: 'mode', value: config.required_mode,
                                                       signal: abort.signal, assertActive });
      const selected = accepted?.configOptions?.find(entry => entry.id === 'mode');
      if (!selected || selected.currentValue !== config.required_mode) {
        throw new Error('backend did not acknowledge required sandbox mode');
      }
    }
    if (config.model) {
      await runtime.setModel({ handle, model: config.model, signal: abort.signal, assertActive });
    }
    assertActive();
    phase = 'turn';
    dispatched = true;
    turn = runtime.startTurn({ handle, text: config.text, mode: 'prompt',
      requestId: config.correlation.turn, timeoutMs: config.timeout_ms,
      signal: abort.signal, assertActive, assurance: true,
      onPermissionRequest: async () => { permissionDenied++; return { outcome: 'cancel' }; },
    });
    // Observe rejection immediately, including when event consumption fails.
    const resultPromise = turn.result;
    void resultPromise.catch(() => {});
    for await (const event of turn.events) {
      assertActive();
      if (event.type === 'text_delta' && event.stream !== 'thought') {
        if (typeof event.text !== 'string') throw new Error('malformed text event');
        bytes += Buffer.byteLength(event.text);
        if (bytes > MAX_TEXT) throw new Error('output bound exceeded');
        if (!pinnedText) text += event.text;
      }
    }
    const result = await resultPromise;
    const receipt = await turn.assurance;
    assertActive();
    phase = 'final';
    if (pinnedText) text = pinnedText.result();
    if (result.status !== 'completed' || result.stopReason !== 'end_turn' ||
        receipt.delivery !== 'finished' || receipt.transport !== 'response' ||
        receipt.identity.requestId !== config.correlation.turn || permissionDenied) {
      throw new Error('turn failed, denied, truncated, cancelled or delivery unknown');
    }
    const envelope = { version: 1, correlation: config.correlation,
      protocol: result.status, stop_reason: result.stopReason,
      delivery: receipt.delivery, transport: receipt.transport,
      backend_outcome: 'unknown', business_validation: 'unknown',
      session: opaque(handle.backendSessionId), text,
      capabilities: { load_session: capabilityEvidence.peer.loadSession,
                      qualification: capabilityEvidence.qualification.state },
      observations: { events: observations, tool_calls: toolCalls, dropped,
                      permission_denied: permissionDenied, coverage: 'partial' }, metadata,
    };
    // Completion is published only after runtime cleanup settles.
    phase = 'cleanup';
    await runtime.shutdown();
    runtime = undefined;
    assertActive();
    return envelope;
  } catch (error) {
    // Fixed enums only. Never serialize upstream messages/stack/stderr/body.
    const wrapped = new Error('trusted bridge failure');
    wrapped.atoSafe = {phase, code: SAFE_CODES.has(error?.code) ? error.code : 'unknown',
      dispatch_state: dispatched ? 'unknown' : 'not_started'};
    throw wrapped;
  } finally {
    clearInterval(poll);
    clearTimeout(timer);
    try {
      if (runtime) {
        await Promise.race([runtime.shutdown(), new Promise((_, reject) => {
          const cleanup = setTimeout(() => reject(new Error('cleanup incomplete')), 2500);
          cleanup.unref();
        })]);
      }
    } finally {
      clearTimeout(hard);
      process.off('SIGTERM', signalHandler);
      process.off('SIGINT', signalHandler);
      records.clear();
    }
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    let input = '', size = 0;
    for await (const chunk of process.stdin) {
      size += chunk.length;
      if (size > MAX_TEXT + 131072) throw new Error('input too large');
      input += chunk;
    }
    const result = await execute(JSON.parse(input));
    process.stdout.write(JSON.stringify(result));
  } catch (error) {
    // Raw library/native errors may contain prompts, stderr or credentials.
    process.stdout.write(JSON.stringify({ version: 1, protocol: 'failed', delivery: 'unknown',
      diagnostic: error?.atoSafe ?? {phase:'configuration',code:'unknown',dispatch_state:'not_started'} }));
    process.exitCode = 1;
  }
}
