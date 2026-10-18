"""Вхід за PIN для доступу через Tailscale.

- Запити на 127.0.0.1 (сам ноутбук) — без PIN.
- Запити на Tailscale-адресу — лише з підписаною cookie, яку дає POST /api/auth/login.
- Після 5 хибних PIN з однієї адреси — блокування на 5 хв (далі довше).
- Без ACCESS_PIN у .env віддалений доступ закритий повністю.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

COOKIE = "am_session"
SESSION_DAYS = 90
MAX_FAILS = 5
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
OPEN_PATHS = ("/api/auth/", "/api/health")

router = APIRouter(prefix="/api/auth", tags=["auth"])
_fails: dict[str, tuple[int, float]] = {}  # ip → (кількість, заблоковано до)


class Login(BaseModel):
    pin: str = Field(min_length=1, max_length=32)


def _secret(settings) -> bytes:
    path = settings.library_path / "session_secret"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secrets.token_bytes(32))
    return path.read_bytes()


def make_token(settings, now: float | None = None) -> str:
    ts = str(int(now or time.time()))
    sig = hmac.new(_secret(settings), ts.encode(), hashlib.sha256).digest()
    return f"{ts}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"


def valid_token(settings, token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    ts, sig = token.split(".", 1)
    good = base64.urlsafe_b64encode(hmac.new(_secret(settings), ts.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    if not hmac.compare_digest(sig, good):
        return False
    try:
        return (now or time.time()) - int(ts) < SESSION_DAYS * 86400
    except ValueError:
        return False


def is_remote(request: Request) -> bool:
    """Запит прийшов на Tailscale-слухач, а не на 127.0.0.1."""
    server = request.scope.get("server") or ("", 0)
    return server[0] not in LOCAL_HOSTS


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


class PinMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        settings = request.app.state.settings
        path = request.url.path
        if is_remote(request) and path.startswith("/api/") and not path.startswith(OPEN_PATHS):
            if not settings.access_pin.get_secret_value():
                return JSONResponse({"detail": "Віддалений доступ вимкнено: задай ACCESS_PIN у .env"}, status_code=403)
            if not valid_token(settings, request.cookies.get(COOKIE)):
                return JSONResponse({"detail": "Потрібен PIN"}, status_code=401)
        return await call_next(request)


@router.get("/status")
def auth_status(request: Request) -> dict:
    settings = request.app.state.settings
    remote = is_remote(request)
    return {"remote": remote, "pin_set": bool(settings.access_pin.get_secret_value()),
            "authenticated": (not remote) or valid_token(settings, request.cookies.get(COOKIE))}


@router.post("/login")
def login(body: Login, request: Request, response: Response) -> dict:
    settings = request.app.state.settings
    pin = settings.access_pin.get_secret_value()
    ip = client_ip(request)
    count, until = _fails.get(ip, (0, 0.0))
    if until > time.time():
        raise HTTPException(429, f"Забагато спроб. Спробуй через {int(until - time.time()) // 60 + 1} хв")
    if not pin:
        raise HTTPException(403, "ACCESS_PIN не задано в .env")
    if not hmac.compare_digest(body.pin.encode(), pin.encode()):
        count += 1
        lock = 300 * 2 ** max(0, count - MAX_FAILS) if count >= MAX_FAILS else 0
        _fails[ip] = (count, time.time() + lock if lock else 0.0)
        raise HTTPException(401, "Невірний PIN" + (f" — блокування на {lock // 60} хв" if lock else ""))
    _fails.pop(ip, None)
    response.set_cookie(COOKIE, make_token(settings), max_age=SESSION_DAYS * 86400, httponly=True,
                        secure=request.url.scheme == "https", samesite="strict", path="/")
    return {"ok": True}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}
