"""MCP по HTTP для Claude з iPhone (Спринт 14).

Окремий застосунок на 127.0.0.1:<MCP_HTTP_PORT>; в інтернет його виводить лише Tailscale Funnel
(`python -m core tailscale-funnel on`). Основний API і PWA через Funnel не доступні:
цей застосунок звертається до них усередині процесу, а назовні дає тільки /mcp, OAuth і сторінку PIN.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings

from core.config import Settings
from mcp_server.api import Api
from mcp_server.oauth import PinOAuthProvider, register_pin_routes
from mcp_server.server import build_server

log = logging.getLogger(__name__)


def public_url(settings: Settings) -> str | None:
    if settings.mcp_public_url:
        return settings.mcp_public_url.rstrip("/")
    from core import tailscale
    name = tailscale.dns_name()
    return f"https://{name}" if name else None


def build_remote_app(settings: Settings, main_app, url: str):
    """Starlette-застосунок: /mcp (лише з токеном), OAuth (/register, /authorize, /token, /revoke), /pin."""
    api = Api("http://127.0.0.1", transport=httpx.ASGITransport(app=main_app))  # всередині процесу
    provider = PinOAuthProvider(settings, url)
    auth = AuthSettings(
        issuer_url=url, resource_server_url=f"{url}/mcp", validate_token_resource=False,
        client_registration_options=ClientRegistrationOptions(enabled=True),
        revocation_options=RevocationOptions(enabled=True))
    server = build_server(api, settings.mcp_spend_limit_usd, auth_server_provider=provider, auth=auth)
    register_pin_routes(server, provider)
    host = urlparse(url).netloc
    port = settings.mcp_http_port
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[host, f"127.0.0.1:{port}", f"localhost:{port}"],
        allowed_origins=[url, "https://claude.ai", "https://claude.com"])
    app = server.streamable_http_app(stateless_http=True, json_response=True, transport_security=security,
                                     host="127.0.0.1")
    app.state.provider = provider
    return app
