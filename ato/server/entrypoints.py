"""Lazy CLI startup. No serving imports are needed for ordinary ATO commands."""
from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version
import logging
import re

from ato import config
from ato.spec import TaskError, require
from ato.server.config import ServerConfig
from ato.server.service import Service

INSTALL_HINT = "Install optional serving dependencies in this Python environment: python -m pip install '.[server]' (from the checkout), or sh ./install.sh --server. Requires MCP SDK 2.3.x."


def dependencies():
    try:
        require(version("mcp").startswith("2.3."), "UNSUPPORTED_CAPABILITY", detail=INSTALL_HINT)
        import anyio
        import mcp.server.lowlevel
        import starlette
        import uvicorn
    except (ImportError, PackageNotFoundError):
        raise TaskError("UNSUPPORTED_CAPABILITY", detail=INSTALL_HINT) from None


def run(args):
    dependencies()
    cfg = ServerConfig.load(args.config)
    # Protocol/debug logging can contain request parameters. The supported launcher
    # disables these SDK logs and HTTP access logs; errors are returned as safe envelopes.
    for name in ("mcp", "httpx", "httpx2", "httpcore"):
        logger = logging.getLogger(name)
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL)
    if args.operation == "mcp":
        import anyio
        from ato.server.mcp import run_stdio
        require(cfg.stdio_principal is not None, "INVALID_SERVER_CONFIG")
        service = Service(cfg, config.db_path())
        anyio.run(run_stdio, service, cfg.principals[cfg.stdio_principal])
        return None
    import uvicorn
    from ato.server.http import create_app
    port = getattr(args, "port", "8765")
    require(re.fullmatch(r"[0-9]{1,5}", port) is not None and 1 <= int(port) <= 65535, "USAGE")
    service = Service(cfg, config.db_path())
    uvicorn.run(create_app(service), host=getattr(args, "host", "127.0.0.1"), port=int(port),
                proxy_headers=False, access_log=False, log_level="critical", limit_concurrency=64,
                timeout_keep_alive=5)
    return None
