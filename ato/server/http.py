"""One listener: authenticated REST and official MCP Streamable HTTP."""
from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager

from ato.spec import TaskError, require, result_envelope, strict_json
from ato.server.mcp import create_server
from ato.server.service import OPERATIONS

# One routing manifest also generates OpenAPI, avoiding a drifting checked-in schema.
ROUTES = [
    ("GET", "/api/v1/repositories", "repositories"),
    ("POST", "/api/v1/validate", "validate"),
    ("POST", "/api/v1/tasks", "submit"),
    ("GET", "/api/v1/tasks", "list"),
    ("GET", "/api/v1/tasks/{task_id}", "status"),
    ("GET", "/api/v1/tasks/{task_id}/diff", "diff"),
    ("GET", "/api/v1/tasks/{task_id}/trace", "trace"),
    ("POST", "/api/v1/tasks/{task_id}/stop", "stop"),
    ("POST", "/api/v1/tasks/{task_id}/resume", "resume"),
    ("GET", "/api/v1/tasks/{task_id}/inbox", "inbox"),
    ("POST", "/api/v1/tasks/{task_id}/questions/{question_id}/answer", "answer"),
]


def http_status(result):
    if result["ok"]:
        return 200
    code = result["error"]["code"]
    if code == "AUTH_REQUIRED":
        return 401
    if code == "FORBIDDEN":
        return 403
    if code == "NOT_FOUND":
        return 404
    if code == "METHOD_NOT_ALLOWED":
        return 405
    if code == "BODY_TOO_LARGE":
        return 413
    error = TaskError(code)
    return {2: 400, 3: 403, 4: 409, 5: 500}[error.exit_code]


def openapi():
    paths = {}
    envelope = {"type": "object", "required": ["schema_version", "ok", "data", "error"],
                "properties": {"schema_version": {"const": 1}, "ok": {"type": "boolean"},
                               "data": {}, "error": {"type": ["object", "null"]}}}
    responses = {str(code): {"description": description,
                            "content": {"application/json": {"schema": envelope}}}
                 for code, description in ((200, "Current result envelope"), (202, "Asynchronous dispatch result; poll status"),
                                           (400, "Invalid arguments"), (401, "Missing/invalid bearer credential"),
                                           (403, "Forbidden"), (404, "Unavailable record or route"),
                                           (405, "Method not allowed"), (409, "Conflict or unresolved intent"),
                                           (413, "Body too large"), (500, "Internal failure"))}
    for method, path, operation in ROUTES:
        scope, shape, description = OPERATIONS[operation]
        params, body = [], {}
        for key, value in shape["properties"].items():
            if "{" + key + "}" in path:
                params.append({"in": "path", "name": key, "required": True, "schema": value})
            elif key == "request_id":
                params.append({"in": "header", "name": "Idempotency-Key", "required": True, "schema": value})
            elif method == "GET":
                params.append({"in": "query", "name": key, "required": key in shape["required"], "schema": value})
            else:
                body[key] = value
        entry = {"operationId": operation, "description": description + " Required scope: " + scope,
                 "parameters": params, "responses": responses}
        if method == "POST":
            entry["requestBody"] = {"required": any(key in shape["required"] for key in body),
                                    "content": {"application/json": {"schema": {
                "type": "object", "additionalProperties": False, "properties": body,
                "required": [key for key in shape["required"] if key in body]}}}}
        paths.setdefault(path, {})[method.lower()] = entry
    for path, description in (("/health", "Authenticated liveness only; does not check workers/providers"),
                              ("/api/v1/openapi.json", "This authenticated OpenAPI schema")):
        paths[path] = {"get": {"description": description, "responses": {"200": {"description": description},
                                                                               "401": responses["401"]}}}
    return {"openapi": "3.1.0", "info": {"title": "ATO REST API", "version": "1"},
            "security": [{"bearerAuth": []}], "paths": paths,
            "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}}}


class Boundary:
    """Authenticate before parsing; exact Host/Origin policy and bounded streaming receive."""
    def __init__(self, app, config):
        self.app, self.config = app, config

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return
        from starlette.responses import JSONResponse
        import anyio

        try:
            headers = {}
            for key, value in scope.get("headers", []):
                name = key.decode("latin1").lower()
                if name in {"host", "origin", "authorization", "content-length", "idempotency-key", "content-type"}:
                    require(name not in headers, "USAGE")
                    headers[name] = value.decode("latin1")
            require(headers.get("host", "").lower() in self.config.allowed_hosts, "FORBIDDEN")
            origin = headers.get("origin")
            require(origin is None or origin in self.config.allowed_origins, "FORBIDDEN")
            principal = self.config.authenticate(headers.get("authorization"))
            length = headers.get("content-length")
            if length is not None:
                require(length.isascii() and length.isdecimal() and len(length) <= 12, "USAGE")
                require(int(length) <= self.config.max_body_bytes, "BODY_TOO_LARGE")
            buffered = bytearray()
            size = 0
            with anyio.fail_after(15):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    require(message["type"] == "http.request", "USAGE")
                    chunk = message.get("body", b"")
                    size += len(chunk)
                    require(size <= self.config.max_body_bytes, "BODY_TOO_LARGE")
                    buffered.extend(chunk)
                    if not message.get("more_body", False):
                        break
            require(length is None or int(length) == size, "USAGE")
            body = bytes(buffered)
            if size:
                require(headers.get("content-type", "").split(";", 1)[0].lower() == "application/json", "USAGE")
                # Also reject duplicate keys/nonfinite values in MCP before handing JSON to the SDK.
                strict_json(body)
            scope = {**scope, "ato.principal": principal}
        except TimeoutError:
            result = result_envelope(error=TaskError("USAGE"))
        except TaskError as exc:
            result = result_envelope(error=TaskError(exc.code))
        else:
            sent_body = False

            async def replay():
                nonlocal sent_body
                if not sent_body:
                    sent_body = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            async def private_send(message):
                if message["type"] == "http.response.start":
                    message = {**message, "headers": [*message.get("headers", []),
                                                       (b"cache-control", b"no-store"),
                                                       (b"x-content-type-options", b"nosniff")]}
                await send(message)

            await self.app(scope, replay, private_send)
            return
        await JSONResponse(result, status_code=http_status(result),
                           headers={"Cache-Control": "no-store", **({"WWW-Authenticate": "Bearer"}
                                    if http_status(result) == 401 else {})})(scope, receive, send)


def create_app(service):
    import anyio
    from starlette.applications import Starlette
    from starlette.exceptions import HTTPException
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from mcp.server.transport_security import TransportSecuritySettings

    cfg = service.config
    managers = {name: StreamableHTTPSessionManager(create_server(service, principal),
                stateless=True, json_response=True,
                security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                    allowed_hosts=list(cfg.allowed_hosts), allowed_origins=list(cfg.allowed_origins)),
                max_request_body_size=cfg.max_body_bytes)
                for name, principal in cfg.principals.items()}

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            for manager in managers.values():
                await stack.enter_async_context(manager.run())
            yield

    def endpoint(operation, method):
        async def handle(request):
            try:
                params = dict(request.path_params)
                if method == "GET":
                    pairs = request.query_params.multi_items()
                    require(len(pairs) == len(dict(pairs)), "USAGE")
                    for key, value in pairs:
                        require(key not in params, "USAGE")
                        rule = OPERATIONS[operation][1]["properties"].get(key, {})
                        if rule.get("type") == "boolean":
                            require(value in {"true", "false"}, "USAGE")
                            value = value == "true"
                        params[key] = value
                else:
                    require(not request.query_params, "USAGE")
                    body = await request.body()
                    payload = strict_json(body) if body else {}
                    # Path IDs and the header key have exactly one authoritative location in REST.
                    require(not set(payload) & (set(params) | {"request_id"}), "USAGE")
                    params.update(payload)
                    if "request_id" in OPERATIONS[operation][1]["properties"]:
                        params["request_id"] = request.headers.get("idempotency-key")
                result = await anyio.to_thread.run_sync(
                    lambda: service.call(request.scope["ato.principal"], operation, params, origin="api"))
                status = 202 if result["ok"] and operation in {"submit", "resume"} else http_status(result)
            except TaskError as exc:
                result = result_envelope(error=TaskError(exc.code))
                status = http_status(result)
            return JSONResponse(result, status_code=status)
        return handle

    async def health(request):
        return JSONResponse(result_envelope(data={"status": "ok"}))

    async def schema_endpoint(request):
        return JSONResponse(openapi())

    async def error_handler(request, exc):
        code = "NOT_FOUND" if isinstance(exc, HTTPException) and exc.status_code == 404 else (
            "METHOD_NOT_ALLOWED" if isinstance(exc, HTTPException) and exc.status_code == 405 else "INTERNAL_ERROR")
        result = result_envelope(error=TaskError(code))
        return JSONResponse(result, status_code=http_status(result))

    routes = [Route(path, endpoint(operation, method), methods=[method]) for method, path, operation in ROUTES]
    routes += [Route("/health", health), Route("/api/v1/openapi.json", schema_endpoint)]
    rest = Starlette(routes=routes, lifespan=lifespan, exception_handlers={HTTPException: error_handler, Exception: error_handler})
    rest.router.redirect_slashes = False

    async def combined(scope, receive, send):
        if scope["type"] == "http" and scope["path"] == "/mcp":
            principal = scope["ato.principal"]
            await managers[principal.name].handle_request(scope, receive, send)
        else:
            await rest(scope, receive, send)

    return Boundary(combined, cfg)
