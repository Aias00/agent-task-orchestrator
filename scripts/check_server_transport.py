#!/usr/bin/env python3
"""Bounded real loopback + official SDK stdio check; no native worker/provider.

Run with the Python environment containing .[server]. All state, credentials,
Git repositories and HOME are synthetic. Only the native dispatch boundary is
replaced: admission creates real worktrees/runs, then records a synthetic event
and leaves the run queued. This does not qualify model execution or deployment.
"""
from contextlib import asynccontextmanager
import asyncio
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import anyio
import httpx2
import mcp.client.stdio as stdio_transport
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
import uvicorn

from ato import config
from ato.server.config import ServerConfig
from ato.server.entrypoints import dependencies
from ato.server.http import create_app
from ato.server.service import Service
from ato.store import Store

TOKENS = {name: "synthetic-transport-" + name + "-" + name[0] * 32
          for name in ("bot", "reader", "human")}


def envelope(value, code=None):
    assert set(value) == {"schema_version", "ok", "data", "error"}, value
    assert value["schema_version"] == 1
    assert value["ok"] is (code is None), value
    if code is None:
        assert value["error"] is None, value
    else:
        assert value["data"] is None and value["error"]["code"] == code, value
    return value


def tool_result(reply, code=None):
    assert reply.is_error is (code is not None), reply
    value = envelope(reply.structured_content, code)
    assert json.loads(reply.content[0].text) == value
    return value


@asynccontextmanager
async def sdk(url, token, rejected=None):
    async def observe_response(response):
        if rejected is not None:
            await response.aread()
            rejected.append((response.status_code, response.json()))

    async with httpx2.AsyncClient(headers={"Authorization": "Bearer " + token} if token else {},
                                  trust_env=False, timeout=10,
                                  event_hooks={"response": [observe_response]}) as http:
        async with streamable_http_client(url + "/mcp", http_client=http) as streams:
            async with ClientSession(*streams, read_timeout_seconds=10) as session:
                assert (await session.initialize()).server_info.name == "ato"
                yield session


def leaves(exc):
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for child in exc.exceptions for leaf in leaves(child)]
    return [exc]


async def check_http(url, store, dispatches):
    args = {"repo": "demo", "spec": {
        "schema_version": 1, "repo": "demo", "base_ref": "HEAD",
        "worker_contract": {"objective": "Synthetic transport qualification", "scope": "README.md",
            "authorization": "Temporary fixture only; no provider invocation",
            "acceptance": [{"id": "whitespace", "kind": "command", "description": "Synthetic check",
                            "checks": [["git", "diff", "--check"]]}]}}}
    async with httpx2.AsyncClient(base_url=url, trust_env=False, timeout=10) as http:
        async def request(method, path, *, token=TOKENS["bot"], status=200, code=None, **kwargs):
            headers = kwargs.pop("headers", {})
            if token is not None:
                headers["Authorization"] = "Bearer " + token
            response = await http.request(method, path, headers=headers, **kwargs)
            assert response.status_code == status, (path, response.status_code, response.text)
            return envelope(response.json(), code)

        for path in ("/health", "/api/v1/tasks", "/mcp"):
            for token in (None, "wrong-synthetic-token"):
                await request("GET", path, token=token, status=401, code="AUTH_REQUIRED")
        for token in (None, "wrong-synthetic-token"):
            rejected = []
            try:
                async with sdk(url, token, rejected):
                    raise AssertionError("Unauthenticated MCP initialize succeeded")
            except Exception as exc:
                errors = leaves(exc)
                # SDK 2.3 maps HTTP failures to MCPError; observe the actual HTTP
                # status/envelope as well, so an unrelated protocol failure cannot pass.
                assert errors and all(isinstance(error, MCPError) and error.code == -32603
                                      for error in errors), errors
                assert rejected and all(status == 401 for status, _ in rejected), rejected
                for _, value in rejected:
                    envelope(value, "AUTH_REQUIRED")
        assert (await request("GET", "/health"))["data"] == {"status": "ok"}
        for headers in ({"Host": "untrusted.invalid"}, {"Origin": "https://untrusted.invalid"}):
            await request("GET", "/health", headers=headers, status=403, code="FORBIDDEN")
        for path in ("/api/v1/validate", "/mcp"):
            await request("POST", path, content=b'x' * 8193, headers={"Content-Type": "application/json"},
                          status=413, code="BODY_TOO_LARGE")

            async def chunks():
                yield b'x' * 8192
                yield b'x'

            await request("POST", path, content=chunks(), headers={"Content-Type": "application/json"},
                          status=413, code="BODY_TOO_LARGE")
        await request("POST", "/api/v1/validate", json=args)
        for name in ("reader", "human"):
            await request("POST", "/api/v1/validate", json=args, token=TOKENS[name],
                          status=403, code="FORBIDDEN")
        result = await request("POST", "/api/v1/tasks", json=args, status=202,
                               headers={"Idempotency-Key": "shared-submit"})
        task_id = result["data"]["task_id"]
        status = await request("GET", "/api/v1/tasks/" + task_id)
        assert status["data"]["status"] == "queued"
        task = store.get_task(task_id)
        assert (task["origin_kind"], task["origin_session_id"]) == ("api", None)
        assert Path(task["worktree_path"]).is_dir()
        assert len(dispatches) == 1
        async with sdk(url, TOKENS["bot"]) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert "ato_submit" in names and "ato_answer" not in names
            tool_result(await session.call_tool("ato_validate", args))
            replay = tool_result(await session.call_tool("ato_submit", {**args, "request_id": "shared-submit"}))
            assert replay == result and len(dispatches) == 1
            tool_result(await session.call_tool("ato_stop", {"task_id": task_id, "request_id": "shared-submit"}),
                        "IDEMPOTENCY_CONFLICT")
            second = tool_result(await session.call_tool("ato_submit", {**args, "request_id": "mcp-submit"}))
            second_id = second["data"]["task_id"]
            assert store.get_task(second_id)["origin_kind"] == "mcp"
            replay = await request("POST", "/api/v1/tasks", json=args, status=202,
                                   headers={"Idempotency-Key": "mcp-submit"})
            assert replay == second and len(dispatches) == 2
            tool_result(await session.call_tool("ato_status", {"task_id": task_id}))
        question = store.create_question(task_id, "permission", "Synthetic question?", "Fixture")
        answer = {"task_id": task_id, "question_id": question["id"], "answer": "Synthetic explicit answer",
                  "request_id": "human-answer"}
        for name in ("reader", "human", "bot"):
            async with sdk(url, TOKENS[name]) as session:
                names = {tool.name for tool in (await session.list_tools()).tools}
                assert ("ato_answer" in names) is (name == "human")
                assert ("ato_submit" in names) is (name == "bot")
                tool_result(await session.call_tool("ato_list", {}))
                if name != "bot":
                    tool_result(await session.call_tool("ato_submit", {**args, "request_id": "denied"}), "FORBIDDEN")
                if name != "human":
                    tool_result(await session.call_tool("ato_answer", answer), "FORBIDDEN")
        answer_path = f"/api/v1/tasks/{task_id}/questions/{question['id']}/answer"
        await request("POST", answer_path, json={"answer": answer["answer"]},
                      headers={"Idempotency-Key": "human-answer"}, status=403, code="FORBIDDEN")
        async with sdk(url, TOKENS["human"]) as session:
            accepted = tool_result(await session.call_tool("ato_answer", answer))
        replay = await request("POST", answer_path, json={"answer": answer["answer"]}, token=TOKENS["human"],
                               headers={"Idempotency-Key": "human-answer"})
        assert replay == accepted
        assert store.get_task(task_id)["status"] == "queued"
        assert store.get_task(task_id)["acceptance"] is None
        assert len(dispatches) == 2
    return task_id


async def check_stdio(config_file, task_id):
    # The actual CLI entrypoint is a separate process. The configured principal
    # has read scope only, so no worker invocation can be admitted over this pipe.
    params = StdioServerParameters(command=sys.executable,
                                  args=["-m", "ato.cli", "mcp", "--stdio", "--config", str(config_file)],
                                  env=dict(os.environ), cwd=str(ROOT))
    processes = []
    real_spawn = stdio_transport._create_platform_compatible_process

    async def observe_spawn(**kwargs):
        process = await real_spawn(**kwargs)  # Real SDK process, only retain its handle for cleanup assertions.
        processes.append(process)
        return process

    try:
        with patch.object(stdio_transport, "_create_platform_compatible_process", side_effect=observe_spawn):
            async with stdio_client(params) as streams:
                async with ClientSession(*streams, read_timeout_seconds=10) as session:
                    assert (await session.initialize()).server_info.name == "ato"
                    names = {tool.name for tool in (await session.list_tools()).tools}
                    assert names == {"ato_repositories", "ato_list", "ato_status", "ato_diff", "ato_trace", "ato_inbox"}
                    tool_result(await session.call_tool("ato_repositories", {}))
                    tool_result(await session.call_tool("ato_status", {"task_id": task_id}))
                    tool_result(await session.call_tool("ato_trace", {"task_id": task_id}))
                    tool_result(await session.call_tool("ato_inbox", {"task_id": task_id}))
        assert len(processes) == 1 and processes[0].returncode == 0, "Stdio CLI did not exit cleanly"
    finally:
        for process in processes:
            if process.returncode is None:
                process.kill()
                with anyio.fail_after(5, shield=True):
                    await process.wait()
            assert process.returncode is not None, "Stdio child leaked"


async def exercise(root):
    repo = root / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.name", "Synthetic"],
                 ["config", "user.email", "synthetic@example.invalid"]):
        subprocess.run(["git", *args], cwd=repo, check=True, timeout=10)
    (repo / "README.md").write_text("Synthetic transport repository.\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, timeout=10)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=repo, check=True, timeout=10)
    sock = socket.socket()
    server = runner = None
    dispatches = []

    def synthetic_dispatch(store, task_id, run_id, *, child_env):
        assert not any(key.startswith("ATO_SERVER_") for key in child_env)
        assert not any(token in value for token in TOKENS.values() for value in child_env.values())
        assert "CODEX_THREAD_ID" not in child_env
        dispatches.append((task_id, run_id))
        store.append_event(task_id, "synthetic_dispatch", {"provider_invoked": False}, run_id=run_id)

    try:
        sock.bind(("127.0.0.1", 0))
        address = "127.0.0.1:" + str(sock.getsockname()[1])
        raw = {"repositories": {"demo": str(repo)}, "principals": {
            name: {"token_env": "ATO_SERVER_TOKEN_" + name.upper(), "repositories": ["demo"], "scopes": scopes}
            for name, scopes in (("bot", ["read", "control"]), ("reader", ["read"]), ("human", ["read", "human"]))},
            "allowed_hosts": [address], "max_body_bytes": 8192, "stdio_principal": "reader"}
        config_file = root / "server.json"
        config_file.write_text(json.dumps(raw))
        service = Service(ServerConfig.load(config_file), config.db_path())
        store = Store(service.db_file)
        server = uvicorn.Server(uvicorn.Config(create_app(service), host="127.0.0.1", port=0,
                                proxy_headers=False, access_log=False, log_level="critical",
                                timeout_graceful_shutdown=5, timeout_keep_alive=1))
        with patch("ato.executor._spawn_background_run", side_effect=synthetic_dispatch), \
             patch("ato.executor.execute_run", side_effect=AssertionError("Native execution forbidden")):
            runner = asyncio.create_task(server.serve(sockets=[sock]))
            async with asyncio.timeout(10):
                while not server.started:
                    if runner.done():
                        await runner
                        raise AssertionError("Server stopped before readiness")
                    await asyncio.sleep(0.02)
            task_id = await check_http("http://" + address, store, dispatches)
            await check_stdio(config_file, task_id)
            assert len(dispatches) == 2
    finally:
        if server is not None:
            server.should_exit = True
        try:
            if runner is not None:
                try:
                    with anyio.fail_after(10, shield=True):
                        await asyncio.shield(runner)
                finally:
                    if not runner.done():
                        runner.cancel()
                        await asyncio.gather(runner, return_exceptions=True)
        finally:
            sock.close()
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(("127.0.0.1", int(address.split(":")[1]))) != 0, "Listener leaked"
    print("PASS: live loopback REST + official MCP HTTP; shared replay, auth and scope separation")
    print("PASS: actual stdio CLI initialize/list/read calls; child exit 0; HTTP listener closed")
    print("Synthetic dispatches: 2; native provider/worker invocations: 0")


def main():
    if not __debug__:
        raise RuntimeError("Assertions must be enabled; do not use python -O")
    dependencies()
    logging.disable(logging.CRITICAL)
    with TemporaryDirectory(prefix="ato-transport-smoke-") as folder:
        root = Path(folder).resolve()
        env = {"HOME": str(root), "PATH": os.defpath, "ATO_HOME": str(root / "ato"),
               "CODEX_HOME": str(root / "codex"), "PYTHONNOUSERSITE": "1",
               "GIT_CONFIG_NOSYSTEM": "1", "CODEX_THREAD_ID": "synthetic-parent-binding",
               **{"ATO_SERVER_TOKEN_" + name.upper(): token for name, token in TOKENS.items()}}
        with patch.dict(os.environ, env, clear=True):
            async def bounded():
                with anyio.fail_after(90):
                    await exercise(root)
            asyncio.run(bounded())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
