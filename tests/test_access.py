"""Спринт 12: PIN, вибір шляху завантаження, слухачі, заборона сну."""

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from core import power, tailscale
from core.config import Settings
from core.db import open_db
from server import auth
from server.app import create_app, listeners
from sources import gdrive
from worker import queue
from worker.runner import Worker

REMOTE = "http://100.101.102.103:8443"  # запит прийшов на Tailscale-слухач


@pytest.fixture(autouse=True)
def _reset_fails():
    auth._fails.clear()
    yield
    auth._fails.clear()


def _client(settings, pin="", base_url="http://testserver"):
    s = settings.model_copy(update={"access_pin": SecretStr(pin)})
    return TestClient(create_app(s, start_worker=False), base_url=base_url)


def test_local_needs_no_pin(settings):
    with _client(settings, pin="2468") as c:
        assert c.get("/api/projects").status_code == 200
        assert c.get("/api/auth/status").json() == {"remote": False, "pin_set": True, "authenticated": True}


def test_remote_without_pin_configured_is_closed(settings):
    with _client(settings, pin="", base_url=REMOTE) as c:
        r = c.get("/api/projects")
        assert r.status_code == 403 and "ACCESS_PIN" in r.json()["detail"]
        assert c.post("/api/auth/login", json={"pin": "0000"}).status_code == 403
        assert c.get("/api/health").status_code == 200  # перевірка живості відкрита


def test_remote_login_cookie_and_logout(settings):
    with _client(settings, pin="2468", base_url=REMOTE) as c:
        assert c.get("/api/projects").status_code == 401
        assert c.get("/api/auth/status").json()["authenticated"] is False
        assert c.post("/api/auth/login", json={"pin": "1111"}).status_code == 401
        r = c.post("/api/auth/login", json={"pin": "2468"})
        assert r.status_code == 200 and auth.COOKIE in r.cookies
        assert c.get("/api/projects").status_code == 200
        assert c.get("/api/auth/status").json()["authenticated"] is True
        c.post("/api/auth/logout")
        c.cookies.clear()
        assert c.get("/api/projects").status_code == 401


def test_forged_or_expired_token_rejected(settings):
    import time
    tok = auth.make_token(settings)
    assert auth.valid_token(settings, tok)
    assert not auth.valid_token(settings, tok[:-2] + "xx")
    assert not auth.valid_token(settings, None) and not auth.valid_token(settings, "garbage")
    old = auth.make_token(settings, now=time.time() - (auth.SESSION_DAYS + 1) * 86400)
    assert not auth.valid_token(settings, old)


def test_lockout_after_five_wrong_pins(settings):
    with _client(settings, pin="2468", base_url=REMOTE) as c:
        for _ in range(4):
            assert c.post("/api/auth/login", json={"pin": "0000"}).status_code == 401
        r = c.post("/api/auth/login", json={"pin": "0000"})
        assert r.status_code == 401 and "блокування" in r.json()["detail"]
        # навіть правильний PIN під час блокування не пускає
        assert c.post("/api/auth/login", json={"pin": "2468"}).status_code == 429


def test_pin_validation():
    assert Settings(_env_file=None, access_pin="1234").access_pin.get_secret_value() == "1234"
    for bad in ("12", "abcd", "12a4"):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, access_pin=bad)


def _peer_status(cur: str) -> dict:
    return {"Self": {"TailscaleIPs": ["100.64.0.1"], "DNSName": "msi.tail1234.ts.net."},
            "Peer": {"k": {"TailscaleIPs": ["testclient"], "DNSName": "iphone.tail1234.ts.net.", "CurAddr": cur}}}


@pytest.mark.parametrize("cur, mbps, route", [
    ("192.168.1.20:41641", 80, "direct"),     # вдома, швидко
    ("192.168.1.20:41641", 5, "drive"),       # вдома, але повільно
    ("31.41.59.26:41641", 80, "drive"),       # пряме, але через інтернет
    ("", 80, "drive"),                        # через ретранслятор DERP
])
def test_upload_route_remote(settings, monkeypatch, cur, mbps, route):
    monkeypatch.setattr(tailscale, "status", lambda max_age=10.0: _peer_status(cur))
    monkeypatch.setattr(gdrive, "configured", lambda s: True)
    with _client(settings, pin="2468", base_url=REMOTE) as c:
        c.post("/api/auth/login", json={"pin": "2468"})
        r = c.get(f"/api/upload-route?mbps={mbps}").json()
        assert r["route"] == route and r["reason"]


def test_upload_route_local_and_no_drive(settings, monkeypatch):
    monkeypatch.setattr(tailscale, "status", lambda max_age=10.0: _peer_status(""))
    monkeypatch.setattr(gdrive, "configured", lambda s: False)
    with _client(settings) as c:
        assert c.get("/api/upload-route?mbps=100").json()["route"] == "direct"
    with _client(settings, pin="2468", base_url=REMOTE) as c:
        c.post("/api/auth/login", json={"pin": "2468"})
        r = c.get("/api/upload-route").json()  # ретранслятор, але Drive не підключено — все одно напряму
        assert r["route"] == "direct" and "Drive не підключено" in r["label"]


def test_upload_route_drive_url_and_speedtest(settings, monkeypatch):
    monkeypatch.setattr(gdrive, "configured", lambda s: True)
    with _client(settings) as c:
        pid = c.post("/api/projects", json={"name": "Drive"}).json()["id"]
        conn = open_db(settings.database_path)
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('drive_projects', ?)",
                     (f'{{"{pid}": {{"id": "FOLDER1"}}}}',))
        conn.commit()
        r = c.get(f"/api/upload-route?project_id={pid}&mbps=1").json()
        assert r["route"] == "drive" and r["drive_url"].endswith("/FOLDER1")
        assert c.post("/api/speedtest", content=b"x" * 1000).json() == {"bytes": 1000}
        assert c.post("/api/speedtest", content=b"x" * (4 * 1024 * 1024 + 1)).status_code == 413


def test_peer_for_parsing():
    st = _peer_status("[fd00::5]:41641")
    p = tailscale.peer_for("testclient", st)
    assert p.direct and p.lan and p.name == "iphone.tail1234.ts.net"
    assert tailscale.peer_for("100.1.1.1", st) is None
    assert tailscale.self_ip(st) == "100.64.0.1" and tailscale.dns_name(st) == "msi.tail1234.ts.net"


def test_listeners_never_public(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(tailscale, "status", lambda max_age=10.0: None)
    assert listeners(Settings(_env_file=None)) == [{"host": "127.0.0.1", "port": 8765}]
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    s = Settings(_env_file=None, tailscale_host="100.70.1.2", tls_cert_file=cert, tls_key_file=key)
    assert listeners(s)[1] == {"host": "100.70.1.2", "port": 8443}  # без сертифіката — HTTP
    cert.write_text("c"); key.write_text("k")
    ln = listeners(s)
    assert ln[1]["ssl_certfile"] == str(cert) and all(x["host"] != "0.0.0.0" for x in ln)
    with pytest.raises(ValueError):
        listeners(Settings(_env_file=None).model_copy(update={"tailscale_host": "0.0.0.0"}))


def test_keep_awake_flags():
    calls = []
    ka = power.KeepAwake(setter=lambda f: calls.append(f) or 1)
    ka.update(False)
    assert calls == []  # стан не змінився — нічого не викликаємо
    ka.update(True); ka.update(True)
    assert calls == [power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED | power.ES_AWAYMODE_REQUIRED]
    ka.update(False)
    assert calls[-1] == power.ES_CONTINUOUS and len(calls) == 2


def test_worker_keeps_awake_while_queue_busy(settings):
    calls = []
    w = Worker(settings, handlers={})
    w.awake = power.KeepAwake(setter=lambda f: calls.append(f) or 1)
    conn = open_db(settings.database_path)
    w.housekeeping(conn)
    assert not w.awake.active
    queue.enqueue(conn, "noop")
    w.housekeeping(conn)
    assert w.awake.active
    conn.execute("UPDATE jobs SET status = 'done'"); conn.commit()
    w.housekeeping(conn)
    assert not w.awake.active and len(calls) == 2
