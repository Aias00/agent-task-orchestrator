"""Synthetic ACP v1 peer. Loaded by a generated absolute-interpreter launcher."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

HERE = Path(__file__).parent
FIXTURE = json.loads((HERE / "fixture.json").read_text())
SCENARIO = FIXTURE["scenario"]
SESSION = "opaque-peer-session-with-private-value"
VERDICT = {
    "verdict": "approve", "summary": "Synthetic read-only review",
    "findings": [], "checked_items": ["app.py"], "residual_risks": [],
}


def seen(event, **fields):
    with (HERE / "seen.jsonl").open("a") as stream:
        stream.write(json.dumps({"event": event, **fields}) + "\n")


def assert_durable_dispatch(rpc, cwd):
    with sqlite3.connect(FIXTURE["db"]) as con:
        row = con.execute("SELECT dispatch_json, launch_json, run_id FROM acpx_reviews ORDER BY rowid DESC").fetchone()
        dispatch, launch = (json.loads(value) for value in row[:2])
        assert dispatch["rpc_id"] == rpc and dispatch["launch_id"] == launch["launch_id"]
        assert dispatch["session_sha256"] == hashlib.sha256(SESSION.encode()).hexdigest()
        assert launch["group_id"] == launch["pid"] == os.getpgrp() == os.getpid()
        task = con.execute("SELECT active_run_id, worktree_path FROM tasks").fetchone()
        assert task == (row[2], cwd)
    seen("prompt", committed=True, pid=os.getpid(), bridge=os.getppid())


def send(**fields):
    print(json.dumps({"jsonrpc": "2.0", **fields}), flush=True)


def reply(request, **result):
    send(id=request, result=result)


def serve():
    current_mode = "auto"
    pending_prompt = None
    cwd = None
    descendant = None
    assert not any(name in os.environ for name in ("NODE_OPTIONS", "GITHUB_TOKEN", "CODEX_THREAD_ID"))
    assert Path(os.environ["CODEX_HOME"]).is_relative_to(FIXTURE["temp"])
    assert os.environ["INITIAL_AGENT_MODE"] == "read-only" and os.environ["NO_BROWSER"] == "1"
    for line in sys.stdin:
        msg = json.loads(line)
        rpc, method, params = msg.get("id"), msg.get("method"), msg.get("params", {})
        if method == "initialize":
            caps = params["clientCapabilities"]
            assert not caps.get("terminal") and not any(caps.get("fs", {}).values())
            seen("initialize")
            reply(rpc, protocolVersion=1, agentCapabilities={}, authMethods=[])
        elif method == "session/new":
            assert params["mcpServers"] == []
            cwd = params["cwd"]
            if SCENARIO == "orphan_descendant":
                descendant = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                               stderr=subprocess.DEVNULL)
                seen("descendant", pid=descendant.pid)
            available = [{"id": "auto", "name": "Auto"}]
            if SCENARIO != "no_mode":
                available.append({"id": "read-only", "name": "Read Only"})
            reply(rpc, sessionId=SESSION, modes={"currentModeId": current_mode, "availableModes": available})
        elif method == "session/set_mode":
            assert params["sessionId"] == SESSION and params["modeId"] == "read-only"
            current_mode = params["modeId"]
            seen("set_mode")
            reply(rpc)
        elif method == "session/prompt":
            assert current_mode == "read-only" and params["sessionId"] == SESSION
            assert_durable_dispatch(rpc, cwd)
            if SCENARIO == "drop":
                os._exit(7)
            if SCENARIO == "rpc_error":
                send(id=rpc, error={"code": -32000, "message": "Synthetic RPC error"})
                continue
            if SCENARIO in ("hold", "orphan", "orphan_descendant", "permission"):
                pending_prompt = rpc
                if SCENARIO == "permission":
                    send(id="permission", method="session/request_permission", params={
                        "sessionId": SESSION, "toolCall": {"toolCallId": "tool", "title": "Write"},
                        "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}],
                    })
                continue
            if SCENARIO == "mutate":
                (Path(cwd) / "unauthorized.txt").write_text("mutation tripwire\n")
            send(method="session/update", params={"sessionId": SESSION, "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": json.dumps(VERDICT)},
            }})
            reply(rpc, stopReason="end_turn")
        elif method == "session/cancel" and pending_prompt is not None:
            assert params["sessionId"] == SESSION
            seen("cancel")
            reply(pending_prompt, stopReason="cancelled")
        elif rpc == "permission":
            assert msg["result"]["outcome"]["outcome"] == "cancelled"
            seen("permission_denied")
            reply(pending_prompt, stopReason="end_turn")
        elif rpc is not None:
            send(id=rpc, error={"code": -32601, "message": "Synthetic peer method unavailable"})
    if SCENARIO in ("orphan", "orphan_descendant"):
        time.sleep(60)  # Keep the real detached group alive after bridge failure.


if __name__ == "__main__":
    if sys.argv[1:] == ["synthetic-child"]:
        time.sleep(60)
    else:
        serve()
