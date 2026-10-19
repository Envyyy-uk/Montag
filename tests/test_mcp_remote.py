"""Спринт 14: MCP з iPhone — OAuth за PIN, токени, відкликання, Funnel лише для MCP."""

import base64
import hashlib
import secrets

import pytest
from pydantic import SecretStr, ValidationError
from starlette.testclient import TestClient

from core import tailscale
from core.config import Settings
from core.db import open_db
from mcp_server import oauth
from mcp_server.remote import build_remote_app
from server.app import create_app

URL = "https://msi.tail1.ts.net"
CB = "https://claude.ai/api/mcp/auth_callback"
PIN = "246813"
H = {"accept": "application/json, text/event-stream", "content-type": "application/json",
     "mcp-protocol-version": "2025-06-18"}


@pytest.fixture
def remote(settings):
    s = settings.model_copy(update={"access_pin": SecretStr(PIN), "mcp_remote": True})
    s.ensure_dirs()
    open_db(s.database_path).close()
    main = create_app(s, start_worker=False)
    with TestClient(build_remote_app(s, main, URL), base_url=URL) as c, \
            TestClient(main, base_url="http://127.0.0.1") as local:
        yield {"c": c, "local": local, "settings": s, "provider": c.app.state.provider}


def _register(c, uri=CB):
    return c.post("/register", json={"redirect_uris": [uri], "client_name": "Claude"})


def _start(c):
    cl = _register(c).json()
    ver = secrets.token_urlsafe(40)
    ch = base64.urlsafe_b64encode(hashlib.sha256(ver.encode()).digest()).decode().rstrip("=")
    r = c.get("/authorize", params={"response_type": "code", "client_id": cl["client_id"], "redirect_uri": CB,
                                    "code_challenge": ch, "code_challenge_method": "S256", "state": "st1"},
              follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(f"{URL}/pin?req=")
    return cl, ver, r.headers["location"].split("req=")[1]


def _login(c):
    cl, ver, req = _start(c)
    r = c.post("/pin", data={"req": req, "pin": PIN}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(CB) and "state=st1" in r.headers["location"]
    code = r.headers["location"].split("code=")[1].split("&")[0]
    tok = c.post("/token", data={"grant_type": "authorization_code", "code": code, "redirect_uri": CB,
                                 "code_verifier": ver, "client_id": cl["client_id"],
                                 "client_secret": cl["client_secret"]}).json()
    return cl, tok


def _rpc(c, token, method, params=None):
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
                  headers={**H, "authorization": f"Bearer {token}"})


def test_full_flow_and_tools(remote):
    c = remote["c"]
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["registration_endpoint"] == f"{URL}/register"
    assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=H).status_code == 401
    cl, tok = _login(c)
    assert set(tok) >= {"access_token", "refresh_token"}
    r = _rpc(c, tok["access_token"], "tools/call", {"name": "status", "arguments": {}})
    assert r.status_code == 200 and "бюджет" in r.json()["result"]["content"][0]["text"]
    assert _rpc(c, "nonsense", "tools/list").status_code == 401
    # токени в базі лише хешами
    conn = open_db(remote["settings"].database_path)
    stored = [r[0] for r in conn.execute("SELECT token_hash FROM mcp_tokens")]
    assert tok["access_token"] not in stored and oauth._hash(tok["access_token"]) in stored


def test_pin_page_wrong_pin_and_lockout(remote):
    c, prov = remote["c"], remote["provider"]
    _, _, req = _start(c)
    page = c.get("/pin", params={"req": req})
    assert page.status_code == 200 and "Claude" in page.text and "claude.ai" in page.text
    assert c.get("/pin", params={"req": "bad"}).status_code == 400
    for _ in range(oauth.MAX_FAILS):
        assert c.post("/pin", data={"req": req, "pin": "000000"}, follow_redirects=False).status_code in (401, 429)
    r = c.post("/pin", data={"req": req, "pin": PIN}, follow_redirects=False)
    assert r.status_code == 429 and prov.locked_for() > 0  # блокування діє навіть на правильний PIN


def test_code_single_use_and_refresh_rotation(remote):
    c = remote["c"]
    cl, tok = _login(c)
    form = {"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": cl["client_id"],
            "client_secret": cl["client_secret"]}
    new = c.post("/token", data=form).json()
    assert new["access_token"] != tok["access_token"]
    assert c.post("/token", data=form).status_code == 400  # старий refresh більше не діє
    assert _rpc(c, new["access_token"], "tools/list").status_code == 200


def test_redirect_must_be_https(remote):
    c = remote["c"]
    assert _register(c, "http://evil.example/cb").status_code == 400
    assert _register(c, "https://evil.example/cb").status_code == 400
    assert _register(c, "https://claude.ai.evil.example/cb").status_code == 400
    assert _register(c, "http://localhost:3000/cb").status_code == 201


def test_revoke_from_app(remote):
    c, local = remote["c"], remote["local"]
    cl, tok = _login(c)
    st = local.get("/api/mcp").json()
    assert st["enabled"]
    assert [x["name"] for x in st["clients"]] == ["Claude"]
    assert local.delete(f"/api/mcp/clients/{cl['client_id']}").status_code == 200
    assert _rpc(c, tok["access_token"], "tools/list").status_code == 401
    assert local.get("/api/mcp").json()["clients"] == []


def test_expired_access_token(remote):
    c, prov = remote["c"], remote["provider"]
    _, tok = _login(c)
    prov.clock = lambda: 9e12
    assert _rpc(c, tok["access_token"], "tools/list").status_code == 401


def test_foreign_host_rejected(remote):
    _, tok = _login(remote["c"])
    r = remote["c"].post("http://evil.example/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={**H, "authorization": f"Bearer {tok['access_token']}"})
    assert r.status_code in (403, 421)


def test_remote_requires_strong_pin():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, mcp_remote=True, access_pin="1234")
    Settings(_env_file=None, mcp_remote=True, access_pin="123456")


def test_funnel_command(monkeypatch):
    calls = []
    monkeypatch.setattr(tailscale, "_cli", lambda: "tailscale")
    monkeypatch.setattr(tailscale, "dns_name", lambda st=None: "msi.tail1.ts.net")

    class R:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(tailscale.subprocess, "run", lambda args, **kw: calls.append(args) or R())
    assert tailscale.funnel(8766, True) == URL
    tailscale.funnel(8766, False)
    assert calls[0][1:] == ["funnel", "--bg", "--https=443", "http://127.0.0.1:8766"]
    assert calls[1][1:] == ["funnel", "--https=443", "off"]
