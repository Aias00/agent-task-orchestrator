// One private stdio connection to the ATO controller, one ACP process, one turn.
import { createHash } from "node:crypto";
import { pathToFileURL } from "node:url";

const MAX_INPUT = 2 * 1024 * 1024;
const MAX_VERDICT = 64 * 1024;
const MODE = "read-only";
const controller = new AbortController();
let runtime, turn, stopping = false, started = false, pending;
let spawnAttempted = false, launch, cleanupObserved = false;
let stopTimer;
let fragments = [], inputBytes = 0;

function send(frame) {
    process.stdout.write(JSON.stringify(frame) + "\n");
}

function stop() {
    if (stopping) return;
    stopping = true;
    pending?.finish(false);
    if (turn) {
        // Give the peer a bounded window to acknowledge its cancellation.
        void turn.cancel({ timeoutMs: 500 }).catch(() => {});
        stopTimer = setTimeout(() => controller.abort(), 850);
    } else {
        controller.abort();
        void runtime?.shutdown().catch(() => {});
    }
}

function assertActive() {
    if (stopping || controller.signal.aborted) throw new Error("owner revoked");
}

function approval(type, detail, signal) {
    if (stopping || signal?.aborted) return Promise.resolve(false);
    return new Promise((resolve) => {
        const finish = (ok) => {
            clearTimeout(timer);
            signal?.removeEventListener("abort", abort);
            pending = undefined;
            resolve(ok);
        };
        const abort = () => finish(false);
        const timer = setTimeout(abort, 2000);
        pending = { type, finish };
        signal?.addEventListener("abort", abort, { once: true });
        send({ type, ...detail });
    });
}

function sha(value) {
    return createHash("sha256").update(value, "utf8").digest("hex");
}

async function review(input) {
    let code = "ACPX_SETUP_FAILED";
    let verdict;
    let shutdownSettled = false;
    let sessionRequest, sessionId, advertised = false, currentMode, modeEpoch = 0;
    let modeVerified = false, modeBreached = false;
    let prohibitedRequest = false;
    let resolveCleanup;
    const cleanup = new Promise((resolve) => { resolveCleanup = resolve; });
    try {
        const fork = await import(pathToFileURL(input.runtime).href);
        assertActive();
        runtime = fork.createAcpRuntime({
            cwd: input.cwd,
            sessionStore: fork.createMemorySessionStore(),
            agentRegistry: fork.createAgentRegistry({
                overrides: { "ato-codex": [input.launcher] },
                allowPackageExecFallback: false,
            }),
            agentProcessEnvAllow: [
                "CODEX_HOME", "OPENAI_API_KEY", "INITIAL_AGENT_MODE", "NO_BROWSER", "DEFAULT_AUTH_REQUEST",
                "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME",
            ],
            permissionMode: "deny-all",
            nonInteractivePermissions: "deny",
            fs: false,
            terminal: false,
            mcpServers: [],
            elicitationModes: [],
            onPermissionRequest: async () => {
                prohibitedRequest = true;
                return { outcome: "cancel" };
            },
            maxMessageBytes: 256 * 1024,
            processGroupCleanup: true,
            timeoutMs: input.timeout_ms,
            processLifecycle: {
                onBeforeSpawn: () => {
                    assertActive();
                    if (spawnAttempted) throw new Error("a replacement process is forbidden");
                    spawnAttempted = true;
                },
                async onOwnedProcessStarted(evidence) {
                    const { launchId, pid, processGroupId, birthIdentity } = evidence;
                    launch = { launch_id: launchId, pid, group_id: processGroupId };
                    if (!birthIdentity || !await approval("launch", launch)) {
                        throw new Error("launch denied");
                    }
                },
                onOwnedProcessCleanup(evidence) {
                    cleanupObserved = true;
                    send({
                        type: "cleanup",
                        launch_id: evidence.launchId,
                        group_disposition: evidence.groupDisposition,
                        root_exit_observed: evidence.rootExit !== null,
                    });
                    resolveCleanup();
                },
            },
            // Inspect only session mode metadata. Keep wire messages in memory.
            onAcpMessage(direction, message) {
                if (direction === "outbound" && message.method === "session/new") {
                    sessionRequest = message.id;
                } else if (direction === "inbound" && sessionRequest !== undefined &&
                           message.id === sessionRequest && !message.method) {
                    sessionRequest = undefined;
                    sessionId = message.result?.sessionId;
                    const modes = message.result?.modes;
                    advertised = Array.isArray(modes?.availableModes) &&
                        modes.availableModes.some((mode) => mode.id === MODE);
                    currentMode = modes?.currentModeId;
                } else if (direction === "inbound" && message.method === "session/update" &&
                           sessionId && message.params?.sessionId === sessionId) {
                    const update = message.params.update;
                    const mode = update?.sessionUpdate === "current_mode_update"
                        ? update.currentModeId
                        : update?.sessionUpdate === "config_option_update" && Array.isArray(update.configOptions)
                            ? update.configOptions.find(({ id }) => id === "mode")?.currentValue
                            : undefined;
                    if (mode !== undefined) {
                        currentMode = mode;
                        modeEpoch += 1;
                        modeBreached ||= modeVerified && mode !== MODE;
                    }
                }
            },
        });
        const handle = await runtime.ensureSession({
            agent: "ato-codex", sessionKey: input.request_id, mode: "persistent", cwd: input.cwd,
        });
        code = "ACPX_READ_ONLY_UNVERIFIED";
        if (!launch || !advertised || typeof sessionId !== "string" ||
            sessionId !== handle.backendSessionId) return;
        if (currentMode !== MODE) {
            const before = modeEpoch;
            await runtime.setMode({ handle, mode: MODE, signal: controller.signal, assertActive });
            // Successful set_mode is a correlated mode acknowledgement. Respect newer mode updates.
            if (modeEpoch === before) currentMode = MODE;
        }
        const assertReadOnly = () => {
            assertActive();
            if (currentMode !== MODE || modeBreached || prohibitedRequest) throw new Error("read-only unverified");
        };
        assertReadOnly();
        modeVerified = true;
        code = "ACPX_SETUP_FAILED";
        turn = runtime.startTurn({
            handle, text: input.prompt, mode: "prompt", requestId: input.request_id,
            timeoutMs: input.timeout_ms, assurance: true, signal: controller.signal,
            assertActive: assertReadOnly,
            async beforePromptDispatch(ctx) {
                assertReadOnly();
                if (ctx.launchId !== launch.launch_id || ctx.backendSessionIdSha256 !== sha(sessionId)) {
                    return false;
                }
                const ok = await approval("dispatch", {
                    request_id: ctx.requestId, turn_id: ctx.turnId,
                    record_id: ctx.acpxRecordId, session_sha256: ctx.backendSessionIdSha256,
                    rpc_id: ctx.rpcRequestId, launch_id: ctx.launchId,
                }, ctx.signal);
                return ok && !stopping && currentMode === MODE && !modeBreached && !prohibitedRequest;
            },
        });
        void turn.promptStarted.catch(() => {});
        let text = "";
        let oversized = false;
        const readOutput = async () => {
            for await (const event of turn.events) {
                if (event.type !== "text_delta" || event.tag !== "agent_message_chunk" || oversized) continue;
                text += event.text;
                if (Buffer.byteLength(text) > MAX_VERDICT) {
                    text = "";
                    oversized = true;
                }
            }
        };
        const [result, receipt] = await Promise.all([turn.result, turn.assurance, readOutput()]);
        if (!receipt || receipt.schemaVersion !== 1) return;
        const identity = receipt.identity;
        send({
            type: "receipt",
            request_id: identity.requestId, turn_id: identity.turnId,
            record_id: identity.acpxRecordId,
            session_sha256: typeof identity.backendSessionId === "string" ? sha(identity.backendSessionId) : undefined,
            rpc_id: identity.rpcRequestId,
            delivery: receipt.delivery, transport: receipt.transport,
            turn_status: receipt.turnStatus,
            cancel_requested: receipt.cancellation.requested,
            cancel_written: receipt.cancellation.notificationWritten,
            cancel_acknowledged: receipt.cancellation.acknowledged,
        });
        if (stopping) code = "STOPPED";
        else if (prohibitedRequest) code = "ACPX_PERMISSION_DENIED";
        else if (modeBreached) code = "ACPX_READ_ONLY_UNVERIFIED";
        else if (receipt.delivery === "unsent") code = "ACPX_DISPATCH_REJECTED";
        else if (result.status === "completed" && result.stopReason === "end_turn" && !oversized &&
                 receipt.delivery === "finished" && currentMode === MODE) {
            code = "OK";
            verdict = text;
        } else code = "ACPX_REVIEW_FAILED";
    } catch {
        if (stopping) code = "STOPPED";
    } finally {
        clearTimeout(stopTimer);
        try {
            await runtime?.shutdown();
            shutdownSettled = true;
        } catch {
            // ATO independently verifies or cleans the persisted process group.
        }
        if (launch && !cleanupObserved) {
            let timer;
            await Promise.race([cleanup, new Promise((resolve) => { timer = setTimeout(resolve, 500); })]);
            clearTimeout(timer);
        }
        send({ type: "done", code, verdict, no_process: shutdownSettled && !spawnAttempted });
        process.stdin.pause();
        process.stdin.unref?.();
    }
}

function receive(frame) {
    if (frame.type === "start" && !started && !stopping) {
        started = true;
        void review(frame);
    } else if (frame.type === "ack" && pending?.type === frame.for) {
        pending.finish(frame.ok === true);
    } else if (frame.type === "stop") {
        stop();
    } else {
        stop();
    }
}

process.stdout.on("error", stop);
process.stdin.on("end", stop);
process.stdin.on("error", stop);
process.stdin.on("data", (chunk) => {
    try {
        while (chunk.length) {
            const newline = chunk.indexOf(10);
            const size = newline === -1 ? chunk.length : newline;
            inputBytes += size;
            if (inputBytes > MAX_INPUT) throw new Error("oversized IPC frame");
            fragments.push(chunk.subarray(0, size));
            if (newline === -1) break;
            receive(JSON.parse(Buffer.concat(fragments, inputBytes).toString("utf8")));
            fragments = [];
            inputBytes = 0;
            chunk = chunk.subarray(size + 1);
        }
    } catch {
        stop();
        process.stdin.pause();
    }
});
