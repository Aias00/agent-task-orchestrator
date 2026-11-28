"""Official MCP Python SDK 2.3 adapter; no protocol implementation lives here."""
from __future__ import annotations

import json

from ato import __version__
from ato.server.service import MUTATIONS, MCP_OPERATIONS


def create_server(service, principal):
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server

    async def list_tools(context, params):
        return types.ListToolsResult(tools=[
            types.Tool(name="ato_" + name, description=description, inputSchema=shape,
                       annotations=types.ToolAnnotations(readOnlyHint=name not in MUTATIONS,
                                                         destructiveHint=name in MUTATIONS,
                                                         idempotentHint=name not in MUTATIONS,
                                                         openWorldHint=name in MUTATIONS))
            for name, (scope, shape, description) in MCP_OPERATIONS.items() if scope in principal.scopes
        ])

    async def call_tool(context, params):
        name = params.name.removeprefix("ato_") if params.name.startswith("ato_") else ""
        # Use a fixed, authenticated principal for this server instance. Session-manager tasks
        # cannot accidentally inherit another request's ContextVar or mutable global identity.
        # Do not prevalidate against tools/list here: exact historical submit
        # replays must reach the journal before new-intent schema requirements.
        result = await anyio.to_thread.run_sync(
            lambda: service.call(principal, name, params.arguments or {}, origin="mcp"))
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                                    structuredContent=result, isError=not result["ok"])

    return Server("ato", version=__version__, on_list_tools=list_tools, on_call_tool=call_tool,
                  instructions="Set explicit worker and configured reviewer execution timeouts before validate/submit. "
                  "Validation previews effective settings and starts no worker. Inspect receipt timeout/source and poll status. "
                  "Worker file contents cannot change the outer execution deadline. Reconnect/rediscover tools after schema changes. "
                  "A submitted task is not a completed task. Human answers require human authority.")


async def run_stdio(service, principal):
    from mcp.server.stdio import stdio_server

    server = create_server(service, principal)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
