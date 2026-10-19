"""OAuth для MCP з iPhone: вхід за тим самим ACCESS_PIN.

Claude (застосунок) реєструється як клієнт, відкриває сторінку /pin на ноутбуці, людина вводить PIN —
і клієнт отримує токени. Токени в базі лише як SHA-256; кожен підключений клієнт можна відкликати в PWA.
Після 5 хибних PIN сторінка блокується для всіх на 5 хв, далі вдвічі довше щоразу.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import secrets
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from core.config import Settings
from core.db import open_db

ACCESS_TTL = 24 * 3600
REFRESH_TTL = 90 * 86400
CODE_TTL = 300
PENDING_TTL = 600
MAX_CLIENTS = 50
MAX_FAILS = 5
# куди можна повертати код: лише Claude і локальні клієнти — фішингове посилання з чужою адресою не спрацює
ALLOWED_REDIRECT_HOSTS = ("claude.ai", "claude.com")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Pending:
    client_id: str
    client_name: str
    params: AuthorizationParams
    expires: float


class PinOAuthProvider:
    def __init__(self, settings: Settings, issuer: str, clock=time.time):
        self.settings = settings
        self.issuer = issuer.rstrip("/")
        self.clock = clock
        self._pending: dict[str, Pending] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._fails = 0
        self._locked_until = 0.0

    def _db(self):
        return open_db(self.settings.database_path)

    # ---------- клієнти ----------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        conn = self._db()
        try:
            row = conn.execute("SELECT data FROM mcp_clients WHERE client_id = ?", (client_id,)).fetchone()
        finally:
            conn.close()
        return OAuthClientInformationFull.model_validate_json(row["data"]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        for uri in client_info.redirect_uris or []:
            u = urlparse(str(uri))
            host = u.hostname or ""
            local = host in ("localhost", "127.0.0.1", "::1")
            claude = u.scheme == "https" and any(host == h or host.endswith("." + h) for h in ALLOWED_REDIRECT_HOSTS)
            if not (claude or (local and u.scheme in ("http", "https"))):
                raise RegistrationError("invalid_redirect_uri", "Дозволено лише Claude (claude.ai) або localhost")
        conn = self._db()
        try:
            # прибираємо реєстрації, які так і не пройшли PIN за добу; обмежуємо кількість
            conn.execute("DELETE FROM mcp_clients WHERE authorized_at IS NULL"
                         " AND created_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-1 day')")
            if conn.execute("SELECT COUNT(*) FROM mcp_clients").fetchone()[0] >= MAX_CLIENTS:
                raise RegistrationError("invalid_client_metadata", "Забагато клієнтів — відклич зайві в застосунку")
            conn.execute("INSERT INTO mcp_clients(client_id, data) VALUES (?, ?)",
                         (client_info.client_id, client_info.model_dump_json()))
        finally:
            conn.close()

    # ---------- авторизація через сторінку PIN ----------

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        now = self.clock()
        self._pending = {k: v for k, v in self._pending.items() if v.expires > now}
        req = secrets.token_urlsafe(24)
        self._pending[req] = Pending(client.client_id, client.client_name or "MCP-клієнт", params, now + PENDING_TTL)
        return f"{self.issuer}/pin?req={req}"

    def locked_for(self) -> int:
        return max(0, int(self._locked_until - self.clock()))

    def check_pin(self, pin: str) -> bool:
        """Перевірка PIN з глобальним блокуванням (за Funnel адреса клієнта — завжди проксі)."""
        if self.locked_for():
            return False
        good = self.settings.access_pin.get_secret_value()
        if good and hmac.compare_digest(pin.encode(), good.encode()):
            self._fails = 0
            return True
        self._fails += 1
        if self._fails >= MAX_FAILS:
            self._locked_until = self.clock() + 300 * 2 ** (self._fails - MAX_FAILS)
        return False

    def approve(self, req: str) -> str:
        """PIN правильний — видаємо код і повертаємо адресу, куди перенаправити браузер."""
        p = self._pending.pop(req, None)
        if p is None or p.expires <= self.clock():
            raise AuthorizeError("access_denied", "Запит прострочено — почни підключення ще раз")
        code = secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code, scopes=p.params.scopes or [], expires_at=self.clock() + CODE_TTL, client_id=p.client_id,
            code_challenge=p.params.code_challenge, redirect_uri=p.params.redirect_uri,
            redirect_uri_provided_explicitly=p.params.redirect_uri_provided_explicitly,
            resource=p.params.resource, subject="owner")
        conn = self._db()
        try:
            conn.execute("UPDATE mcp_clients SET authorized_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                         " WHERE client_id = ?", (p.client_id,))
        finally:
            conn.close()
        return construct_redirect_uri(str(p.params.redirect_uri), code=code, state=p.params.state, iss=self.issuer)

    def pending(self, req: str) -> Pending | None:
        p = self._pending.get(req)
        return p if p and p.expires > self.clock() else None

    # ---------- коди й токени ----------

    async def load_authorization_code(self, client: OAuthClientInformationFull, authorization_code: str):
        c = self._codes.get(authorization_code)
        return c if c and c.client_id == client.client_id else None

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(self.clock())
        conn = self._db()
        try:
            for tok, kind, ttl in ((access, "access", ACCESS_TTL), (refresh, "refresh", REFRESH_TTL)):
                conn.execute("INSERT INTO mcp_tokens(token_hash, kind, client_id, scopes, resource, expires_at)"
                             " VALUES (?, ?, ?, ?, ?, ?)", (_hash(tok), kind, client_id, json.dumps(scopes), resource, now + ttl))
        finally:
            conn.close()
        return OAuthToken(access_token=access, expires_in=ACCESS_TTL, refresh_token=refresh,
                          scope=" ".join(scopes) or None)

    async def exchange_authorization_code(self, client: OAuthClientInformationFull,
                                          authorization_code: AuthorizationCode) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "Код уже використано")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    def _load(self, token: str, kind: str):
        conn = self._db()
        try:
            row = conn.execute("SELECT * FROM mcp_tokens WHERE token_hash = ? AND kind = ?",
                               (_hash(token), kind)).fetchone()
            if row is None or row["expires_at"] < self.clock():
                return None
            if kind == "access":
                conn.execute("UPDATE mcp_clients SET last_used_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                             " WHERE client_id = ?", (row["client_id"],))
            return row
        finally:
            conn.close()

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        row = self._load(refresh_token, "refresh")
        if row is None or row["client_id"] != client.client_id:
            return None
        return RefreshToken(token=refresh_token, client_id=row["client_id"], scopes=json.loads(row["scopes"]),
                            expires_at=row["expires_at"], resource=row["resource"], subject="owner")

    async def exchange_refresh_token(self, client: OAuthClientInformationFull, refresh_token: RefreshToken,
                                     scopes: list[str]) -> OAuthToken:
        conn = self._db()
        try:  # ротація: старий refresh більше не діє
            conn.execute("DELETE FROM mcp_tokens WHERE token_hash = ?", (_hash(refresh_token.token),))
        finally:
            conn.close()
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        row = self._load(token, "access")
        if row is None:
            return None
        return AccessToken(token=token, client_id=row["client_id"], scopes=json.loads(row["scopes"]),
                           expires_at=row["expires_at"], resource=row["resource"], subject="owner")

    async def revoke_token(self, token) -> None:
        conn = self._db()
        try:
            conn.execute("DELETE FROM mcp_tokens WHERE client_id = ?", (token.client_id,))
        finally:
            conn.close()


# ---------- сторінка PIN ----------

PAGE = """<!doctype html><html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Автомонтаж · доступ</title>
<style>
:root{{color-scheme:dark}}body{{margin:0;min-height:100vh;display:grid;place-items:center;font:16px/1.45 system-ui,sans-serif;
background:radial-gradient(120% 80% at 20% 0%,#5a1530,#0b0b10 60%);color:#f3efe6}}
main{{box-sizing:border-box;width:min(360px,calc(100% - 32px));padding:28px;border-radius:28px;background:rgba(255,255,255,.07);
border:1px solid rgba(255,255,255,.14);backdrop-filter:blur(24px)}}
h1{{font-size:24px;margin:0 0 8px}}p{{color:#bdb6aa;margin:0 0 18px}}.who{{color:#ffd166}}
input{{width:100%;box-sizing:border-box;font-size:28px;letter-spacing:.35em;text-align:center;padding:14px;border-radius:16px;
border:1px solid rgba(255,255,255,.2);background:rgba(0,0,0,.35);color:#fff}}
button{{margin-top:14px;width:100%;padding:14px;border:0;border-radius:999px;font-size:17px;background:#ff3b30;color:#fff}}
.err{{color:#ff8a80;margin-top:12px}}
</style></head><body><main>
<h1>● Автомонтаж</h1>
<p><span class="who">{client}</span> хоче керувати програмою на ноутбуці: монтаж, правки, витрати Gemini до ліміту.
Повернення на <b>{host}</b>.</p>
<form method="post" action="/pin"><input type="hidden" name="req" value="{req}">
<input name="pin" type="password" inputmode="numeric" autocomplete="one-time-code" placeholder="PIN" autofocus required>
<button>Дозволити</button></form>{error}
</main></body></html>"""


def _page(p: Pending | None, req: str, error: str = "", status: int = 200) -> HTMLResponse:
    if p is None:
        return HTMLResponse(PAGE.format(client="?", host="?", req="", error='<p class="err">Запит прострочено — почни '
                                        "підключення в Claude ще раз</p>"), status_code=400)
    host = urlparse(str(p.params.redirect_uri)).hostname or "?"
    err = f'<p class="err">{html.escape(error)}</p>' if error else ""
    return HTMLResponse(PAGE.format(client=html.escape(p.client_name), host=html.escape(host), req=html.escape(req),
                                    error=err), status_code=status,
                        headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https:"})


def register_pin_routes(server, provider: PinOAuthProvider) -> None:
    @server.custom_route("/pin", methods=["GET"], include_in_schema=False)
    async def pin_form(request: Request) -> Response:
        req = request.query_params.get("req", "")
        return _page(provider.pending(req), req)

    @server.custom_route("/pin", methods=["POST"], include_in_schema=False)
    async def pin_submit(request: Request) -> Response:
        form = await request.form()
        req, pin = str(form.get("req", "")), str(form.get("pin", ""))
        p = provider.pending(req)
        if p is None:
            return _page(None, req)
        if provider.locked_for():
            return _page(p, req, f"Забагато спроб. Зачекай {provider.locked_for() // 60 + 1} хв", 429)
        if not provider.check_pin(pin):
            return _page(p, req, "Невірний PIN", 401)
        try:
            return RedirectResponse(provider.approve(req), status_code=302)
        except AuthorizeError as exc:
            return _page(None, req, exc.error_description or "")
