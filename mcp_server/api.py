"""Тонкий клієнт до API програми. Помилки — українською, як їх показує сервер."""

from __future__ import annotations

from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError


class Api:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 60.0):
        # x-am-origin: сервер позначає правила смаку, додані через Claude, щоб їх можна було відкотити
        self.client = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout,
                                        headers={"x-am-origin": "mcp"})

    async def request(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            r = await self.client.request(method, url, **kw)
        except httpx.TransportError as exc:
            raise ToolError("Програма на ноутбуці не відповідає — запусти її (python -m server.app)") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ToolError(detail if isinstance(detail, str) else str(detail))
        return r

    async def get(self, url: str, **kw) -> Any:
        return (await self.request("GET", url, **kw)).json()

    async def post(self, url: str, body: dict | None = None) -> Any:
        return (await self.request("POST", url, json=body or {})).json()

    async def put(self, url: str, body: dict) -> Any:
        return (await self.request("PUT", url, json=body)).json()

    async def delete(self, url: str) -> Any:
        return (await self.request("DELETE", url)).json()

    async def image(self, url: str, **kw) -> bytes | None:
        """Картинка або None, якщо її ще немає (404)."""
        try:
            return (await self.request("GET", url, **kw)).content
        except ToolError:
            return None

    async def aclose(self) -> None:
        await self.client.aclose()
