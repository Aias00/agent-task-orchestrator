"""Dependency-free stand-in for the Node bridge's private stdio protocol."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace
import uuid


def receive():
    return json.loads(sys.stdin.readline())


def send(**frame):
    print(json.dumps(frame), flush=True)


def main():
    start = receive()
    assert start["type"] == "start"
    launcher = start["launcher"]
    peer = SimpleNamespace(**runpy.run_path(launcher))
    child = subprocess.Popen([launcher, "synthetic-child"], stdin=subprocess.DEVNULL, start_new_session=True)
    launch = {"launch_id": str(uuid.uuid4()), "pid": child.pid, "group_id": child.pid}
    send(type="launch", **launch)
    assert receive() == {"type": "ack", "for": "launch", "ok": True}
    identity = {
        "request_id": start["request_id"], "record_id": start["request_id"],
        "turn_id": str(uuid.uuid4()), "rpc_id": 4,
        "session_sha256": peer.hashlib.sha256(peer.SESSION.encode()).hexdigest(),
    }
    send(type="dispatch", launch_id=launch["launch_id"], **identity)
    command = receive()
    if command["type"] == "stop":
        command = receive()
    assert command["type"] == "ack" and command["for"] == "dispatch"
    cancelled = False
    if command["ok"]:
        with peer.sqlite3.connect(peer.FIXTURE["db"]) as con:
            row = con.execute("SELECT dispatch_json, launch_json FROM acpx_reviews ORDER BY rowid DESC").fetchone()
            assert json.loads(row[0]) == {**identity, "launch_id": launch["launch_id"]}
            assert json.loads(row[1])["pid"] == child.pid
        peer.seen("prompt", committed=True, pid=child.pid, bridge=os.getpid())
        if peer.SCENARIO in ("drop", "orphan"):
            if peer.SCENARIO == "drop":
                child.terminate()
                child.wait()
                send(type="cleanup", launch_id=launch["launch_id"],
                     root_exit_observed=True, group_disposition="verified_gone")
                return
            receive()
            return
        if peer.SCENARIO == "hold":
            assert receive() == {"type": "stop"}
            peer.seen("cancel")
            cancelled = True
        if peer.SCENARIO == "mutate":
            (Path(start["cwd"]) / "unauthorized.txt").write_text("mutation tripwire\n")
    send(type="receipt", **identity, delivery="finished" if command["ok"] else "unsent",
         transport="response" if command["ok"] else "not_dispatched",
         turn_status="cancelled" if cancelled else ("completed" if command["ok"] else "failed"),
         cancel_requested=cancelled, cancel_written=cancelled, cancel_acknowledged=cancelled)
    child.terminate()
    child.wait()
    send(type="cleanup", launch_id=launch["launch_id"],
         root_exit_observed=True, group_disposition="verified_gone")
    send(type="done", code="STOPPED" if cancelled else ("OK" if command["ok"] else "ACPX_DISPATCH_REJECTED"),
         verdict=json.dumps(peer.VERDICT))


if __name__ == "__main__":
    main()
