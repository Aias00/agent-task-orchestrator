"""Optional real-SDK transport contract tests, entirely in-process with synthetic stores."""
from contextlib import asynccontextmanager
import asyncio
import json
import threading
import unittest
from unittest.mock import patch

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from ato.server.http import Boundary, create_app
from server_fixtures import BOT_TOKEN, HUMAN_TOKEN, READER_TOKEN, ServerFixture


@asynccontextmanager
async def lifespan(app):
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    # ASGI lifespan (and all SDK cancel scopes) has one owning task even when
    # unittest enters/exits this context from different lifecycle coroutines.
    task = asyncio.create_task(app({"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}},
                                   incoming.get, outgoing.put))
    await incoming.put({"type": "lifespan.startup"})
    assert (await asyncio.wait_for(outgoing.get(), 10))["type"] == "lifespan.startup.complete"
    try:
        yield
    finally:
        await incoming.put({"type": "lifespan.shutdown"})
        assert (await asyncio.wait_for(outgoing.get(), 10))["type"] == "lifespan.shutdown.complete"
        await asyncio.wait_for(task, 10)


class TransportTests(ServerFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app(self.service)
        await self.enterAsyncContext(lifespan(self.app))
        self.client = await self.enterAsyncContext(httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=self.app), base_url="http://testserver",
            headers={"Authorization": "Bearer " + BOT_TOKEN}))

    @asynccontextmanager
    async def sdk(self, token=BOT_TOKEN):
        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app),
                                      headers={"Authorization": "Bearer " + token}) as client:
            async with streamable_http_client("http://testserver/mcp", http_client=client) as streams:
                async with ClientSession(*streams, read_timeout_seconds=10) as session:
                    info = await session.initialize()
                    self.assertEqual(info.server_info.name, "ato")
                    yield session

    async def test_health_schema_authentication_and_host_origin_policy(self):
        for path in ("/health", "/api/v1/openapi.json", "/api/v1/tasks", "/mcp"):
            for headers in ({"Authorization": ""}, {"Authorization": "Bearer invalid"}):
                response = await self.client.get(path, headers=headers)
                self.assertEqual(response.status_code, 401, path)
                self.assertEqual(response.json()["error"]["code"], "AUTH_REQUIRED")
                self.assertNotIn("invalid", response.text)
        response = await self.client.get("/health")
        self.assertEqual(response.json()["data"], {"status": "ok"})
        self.assertEqual(response.headers["cache-control"], "no-store")
        schema = (await self.client.get("/api/v1/openapi.json")).json()
        self.assertIn("/api/v1/tasks/{task_id}/questions/{question_id}/answer", schema["paths"])
        self.assertEqual(schema["security"], [{"bearerAuth": []}])
        for headers in ({"Host": "attacker.invalid"}, {"Origin": "https://attacker.invalid"}, {"Origin": "null"}):
            self.assertEqual((await self.client.get("/health", headers=headers)).status_code, 403)
        self.assertEqual((await self.client.get("/health", headers={"Origin": "https://testserver"})).status_code, 200)
        self.assertEqual((await self.client.get("/missing")).json()["error"]["code"], "NOT_FOUND")
        self.assertEqual((await self.client.put("/api/v1/tasks")).status_code, 405)

    async def test_request_limits_json_and_duplicate_headers(self):
        for path in ("/api/v1/tasks", "/mcp"):
            response = await self.client.post(path, content=b"x" * (self.cfg.max_body_bytes + 1),
                                              headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 413)

            async def chunks():
                yield b"x" * self.cfg.max_body_bytes
                yield b"x"
            response = await self.client.post(path, content=chunks(), headers={"Content-Type": "application/json"})
            self.assertEqual(response.status_code, 413)
            for body in (b'{"repo":"demo","repo":"other"}', b'{"x":NaN}', b'[]', b'{'):
                response = await self.client.post(path, content=body, headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 400)
        response = await self.client.get("/health", headers=[("Authorization", "Bearer " + BOT_TOKEN),
                                                              ("Authorization", "Bearer " + HUMAN_TOKEN)])
        self.assertEqual(response.status_code, 400)
        self.spawn.assert_not_called()

    async def test_rest_validate_submit_poll_and_real_sdk_cross_transport_replay(self):
        args = {"repo": "demo", "spec": self.spec()}
        response = await self.client.post("/api/v1/validate", json=args)
        self.assertEqual(response.status_code, 200, response.text)
        missing_key = await self.client.post("/api/v1/tasks", json=args)
        self.assertEqual(missing_key.status_code, 400)
        response = await self.client.post("/api/v1/tasks", json=args, headers={"Idempotency-Key": "shared-key"})
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()
        task_id = result["data"]["task_id"]
        status = await self.client.get("/api/v1/tasks/" + task_id, params={"check_history": "true"})
        self.assertEqual(status.status_code, 200)
        self.assertIn("check_history", status.json()["data"])
        self.assertEqual(status.json()["data"]["status"], "queued")
        async with self.sdk() as session:
            listing = await session.list_tools()
            names = {tool.name for tool in listing.tools}
            self.assertIn("ato_submit", names)
            self.assertNotIn("ato_answer", names)
            reply = await session.call_tool("ato_submit", {**args, "request_id": "shared-key"})
            self.assertFalse(reply.is_error)
            self.assertEqual(reply.structured_content, result)
            self.assertEqual(json.loads(reply.content[0].text), result)
            collision = await session.call_tool("ato_stop", {"task_id": task_id, "request_id": "shared-key"})
            self.assertTrue(collision.is_error)
            self.assertEqual(collision.structured_content["error"]["code"], "IDEMPOTENCY_CONFLICT")
        self.spawn.assert_called_once()

    async def test_sdk_principal_isolation_and_separate_human_authority(self):
        own, other = self.task(), self.task("other")
        question = self.store.create_question(own["id"], "permission", "Approve?", "Synthetic")
        args = {"task_id": own["id"], "question_id": question["id"], "answer": "yes", "request_id": "decision"}

        async def visible(token, expected):
            async with self.sdk(token) as session:
                await session.list_tools()
                result = await session.call_tool("ato_list", {})
                self.assertEqual([t["task_id"] for t in result.structured_content["data"]["tasks"]], [expected])
        await asyncio.gather(visible(BOT_TOKEN, own["id"]), visible(READER_TOKEN, other["id"]))
        async with self.sdk() as bot:
            blocked = await bot.call_tool("ato_answer", args)
            self.assertTrue(blocked.is_error)
            self.assertEqual(blocked.structured_content["error"]["code"], "FORBIDDEN")
            hidden = await bot.call_tool("ato_status", {"task_id": other["id"]})
            self.assertEqual(hidden.structured_content["error"]["code"], "NOT_FOUND")
        async with self.sdk(HUMAN_TOKEN) as human:
            names = {t.name for t in (await human.list_tools()).tools}
            self.assertIn("ato_answer", names)
            self.assertNotIn("ato_submit", names)
            accepted = await human.call_tool("ato_answer", args)
            self.assertFalse(accepted.is_error)
        response = await self.client.post(f"/api/v1/tasks/{own['id']}/questions/{question['id']}/answer",
                                          json={"answer": "yes"}, headers={"Idempotency-Key": "bot-decision"})
        self.assertEqual(response.status_code, 403)
        self.spawn.assert_not_called()

    async def test_task_routes_and_mutation_keys_delegate_without_implicit_actions(self):
        task = self.task()
        task_id = task["id"]
        with patch("ato.product.collect_diff", return_value={"files": [], "patch": ""}) as diff:
            response = await self.client.get(f"/api/v1/tasks/{task_id}/diff?stat=true")
            self.assertEqual(response.status_code, 200)
            self.assertFalse(diff.call_args.kwargs["include_patch"])
        self.assertEqual((await self.client.get(f"/api/v1/tasks/{task_id}/trace")).status_code, 200)
        self.assertEqual((await self.client.get(f"/api/v1/tasks/{task_id}/inbox")).json()["data"]["count"], 0)
        with patch("ato.executor.request_stop", return_value={"task_id": task_id, "status": "stopped"}) as stop:
            for _ in range(2):
                response = await self.client.post(f"/api/v1/tasks/{task_id}/stop", headers={"Idempotency-Key": "stop"})
                self.assertEqual(response.status_code, 200)
            stop.assert_called_once()
        with patch("ato.executor.resume", return_value={"task_id": task_id, "status": "queued"}) as resume:
            response = await self.client.post(f"/api/v1/tasks/{task_id}/resume", json={"timeout_seconds": 60},
                                              headers={"Idempotency-Key": "resume"})
            self.assertEqual(response.status_code, 202)
            self.assertEqual(resume.call_args.kwargs["timeout_seconds"], 60)
        for path in ("recheck", "accept", "cleanup", "merge", "deploy"):
            self.assertEqual((await self.client.post(f"/api/v1/tasks/{task_id}/{path}")).status_code, 404)
        for query in ("check_history=1", "check_history=true&check_history=false", "task_id=override"):
            self.assertEqual((await self.client.get(f"/api/v1/tasks/{task_id}?{query}")).status_code, 400)
        response = await self.client.post(f"/api/v1/tasks/{task_id}/stop", json={"task_id": task_id},
                                          headers={"Idempotency-Key": "bad-path"})
        self.assertEqual(response.status_code, 400)

    async def test_cross_repository_read_routes_and_tools_do_not_leak(self):
        own, other = self.task(), self.task("other")
        self.store.create_question(other["id"], "permission", "Private question", "Synthetic")
        self.store.append_event(other["id"], "private_event", {"text": "Private event"})
        for suffix in ("", "/diff", "/trace", "/inbox"):
            response = await self.client.get(f"/api/v1/tasks/{other['id']}{suffix}")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["error"]["code"], "NOT_FOUND")
            self.assertNotIn("Private", response.text)
        response = await self.client.get("/api/v1/tasks")
        self.assertEqual([t["task_id"] for t in response.json()["data"]["tasks"]], [own["id"]])
        async with self.sdk() as session:
            for op in ("status", "diff", "trace", "inbox"):
                reply = await session.call_tool("ato_" + op, {"task_id": other["id"]})
                self.assertTrue(reply.is_error)
                self.assertEqual(reply.structured_content["error"]["code"], "NOT_FOUND")
                self.assertNotIn("Private", reply.content[0].text)
        self.spawn.assert_not_called()

    async def test_concurrent_rest_mcp_admission_is_unresolved_then_replays(self):
        entered, finish = threading.Event(), threading.Event()

        def dispatch(*args, **kwargs):
            entered.set()
            self.assertTrue(finish.wait(10))

        self.spawn.side_effect = dispatch
        args = {"repo": "demo", "spec": self.spec()}
        async with self.sdk() as session:
            first = asyncio.create_task(self.client.post("/api/v1/tasks", json=args,
                                        headers={"Idempotency-Key": "concurrent"}))
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                # Admission is waiting on dispatch, but the event loop and another
                # authenticated MCP request remain responsive.
                self.assertEqual((await self.client.get("/health")).status_code, 200)
                duplicate = await session.call_tool("ato_submit", {**args, "request_id": "concurrent"})
                self.assertTrue(duplicate.is_error)
                self.assertEqual(duplicate.structured_content["error"]["code"], "REQUEST_UNRESOLVED")
                reserved = duplicate.structured_content["error"]["task_id"]
                self.assertEqual(self.store.get_task(reserved)["status"], "queued")
            finally:
                finish.set()
                response = await asyncio.wait_for(first, 10)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["data"]["task_id"], reserved)
            replay = await session.call_tool("ato_submit", {**args, "request_id": "concurrent"})
            self.assertEqual(replay.structured_content, response.json())
        self.spawn.assert_called_once()

    async def test_mutation_failure_stays_unresolved_across_transports(self):
        self.spawn.side_effect = RuntimeError("Synthetic failure after task creation")
        args = {"repo": "demo", "spec": self.spec()}
        response = await self.client.post("/api/v1/tasks", json=args, headers={"Idempotency-Key": "uncertain"})
        self.assertEqual(response.status_code, 409)
        result = response.json()
        self.assertEqual(result["error"]["code"], "REQUEST_UNRESOLVED")
        self.assertNotIn("Synthetic failure", response.text)
        self.assertEqual(self.store.get_task(result["error"]["task_id"])["status"], "queued")
        async with self.sdk() as session:
            replay = await session.call_tool("ato_submit", {**args, "request_id": "uncertain"})
            self.assertTrue(replay.is_error)
            self.assertEqual(replay.structured_content, result)
        self.spawn.assert_called_once()

    async def test_header_and_body_rejections_precede_service_or_sdk(self):
        for path in ("/api/v1/tasks", "/mcp"):
            cases = (
                ({"Content-Type": "text/plain"}, b'{}'),
                ({"Content-Type": "application/json", "Content-Length": "3"}, b'{}'),
                ({"Content-Type": "application/json", "Content-Length": "-1"}, b'{}'),
                ({"Content-Type": "application/json", "Content-Length": "abc"}, b'{}'),
                ({"Content-Type": "application/json"}, b'\xff'),
                ({"Content-Type": "application/json"}, b'{"x":"\\ud800"}'),
                ({"Content-Type": "application/json"}, b'{"x":1e999}'),
            )
            for headers, body in cases:
                response = await self.client.post(path, content=body, headers=headers)
                self.assertEqual(response.status_code, 400, (path, headers, body))
            for name, value in (("Host", "testserver"), ("Origin", "https://testserver"),
                                ("Content-Type", "application/json"), ("Idempotency-Key", "key"),
                                ("Content-Length", "2")):
                response = await self.client.post(path, content=b'{}', headers=[
                    (name, value), (name, value), ("Authorization", "Bearer " + BOT_TOKEN)])
                self.assertEqual(response.status_code, 400, name)
        self.spawn.assert_not_called()

    async def test_auth_precedes_receive_and_receive_timeout_is_canonical(self):
        async def downstream(*args):
            self.fail("Rejected request reached the application")

        reads = []

        async def stalled_receive():
            reads.append(1)
            # Simulate expiry of the boundary's bounded receive without sleeping.
            raise TimeoutError

        for credential, expected_status, expected_code in (
                (b"Bearer invalid", 401, "AUTH_REQUIRED"),
                (("Bearer " + BOT_TOKEN).encode(), 400, "USAGE")):
            messages = []

            async def send(message):
                messages.append(message)

            await Boundary(downstream, self.cfg)({
                "type": "http", "headers": [(b"host", b"testserver"), (b"authorization", credential)]},
                stalled_receive, send)
            self.assertEqual(messages[0]["status"], expected_status)
            self.assertEqual(json.loads(messages[1]["body"])["error"]["code"], expected_code)
            self.assertEqual(len(reads), 0 if expected_status == 401 else 1)


if __name__ == "__main__":
    unittest.main()
