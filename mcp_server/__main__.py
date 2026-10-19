"""`python -m mcp_server` — MCP по stdio для Claude Desktop на цьому ж ноутбуці.

Говорить з програмою через http://127.0.0.1:<PORT>; сама програма (python -m server.app) має бути запущена.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from core.config import get_settings
from mcp_server.api import Api
from mcp_server.server import build_server


def main() -> None:
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout — лише протокол MCP
    settings = get_settings()
    api = Api(f"http://127.0.0.1:{settings.port}")
    server = build_server(api, settings.mcp_spend_limit_usd)
    asyncio.run(server.run_stdio_async())


if __name__ == "__main__":
    main()
