import json

import pytest
from fastapi.testclient import TestClient

from core import notify
from core.config import with_overrides
from core.db import open_db
from server import ui
from server.app import create_app


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, start_worker=False)) as c:
        yield c


def test_overview_and_missing_track(client):
    pid = client.post("/api/projects", json={"name": "Огляд"}).json()["id"]
    ov = client.get(f"/api/projects/{pid}/overview").json()
    assert ov["name"] == "Огляд" and ov["clip"] is None and ov["vertical"] is None
    assert ov["style"]["id"] == "minidv-concert" and ov["clip_params"]["transitions"] is True
    assert client.get(f"/api/projects/{pid}/track").status_code == 404
    assert client.get(f"/api/projects/{pid}/cover.jpg").status_code == 404
    assert client.get("/api/projects/999/overview").status_code == 404


def test_budget_overrides_reach_worker_settings(client, settings):
    r = client.put("/api/budget/settings", json={"monthly_budget_usd": 4, "confirm_threshold_usd": 0.5}).json()
    assert r["limit"] == 4 and r["confirm_threshold"] == 0.5
    assert client.get("/api/budget").json()["limit"] == 4
    conn = open_db(settings.database_path)
    eff = with_overrides(settings, conn)
    assert eff.monthly_budget_usd == 4 and eff.confirm_threshold_usd == 0.5 and eff.daily_request_limit == 2000
    assert client.put("/api/budget/settings", json={"monthly_budget_usd": -1}).status_code == 422


def test_push_key_subscribe_and_send(client, settings, monkeypatch):
    key = client.get("/api/push/key").json()["key"]
    assert len(key) == 87 and "=" not in key  # 65 байт несжатої точки P-256 у base64url
    assert client.get("/api/push/key").json()["key"] == key  # ключ стабільний
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": "x", "auth": "y"}}
    assert client.post("/api/push/subscribe", json=sub).status_code == 200
    client.post("/api/push/subscribe", json=sub)  # повторна підписка — без дубля
    got = []
    monkeypatch.setattr(notify, "_sender", lambda s, payload: got.append((s["endpoint"], json.loads(payload))))
    assert client.post("/api/push/test").json() == {"sent": 1}
    assert got[0][0] == sub["endpoint"] and got[0][1]["title"] == "Автомонтаж"


def test_push_failure_does_not_raise(settings, monkeypatch):
    conn = open_db(settings.database_path)
    notify.subscribe(conn, {"endpoint": "e", "keys": {}})

    def boom(s, p):
        raise RuntimeError("мережа")
    monkeypatch.setattr(notify, "_sender", boom)
    assert notify.send(conn, settings, "т", "т") == 0


def test_spa_served_and_api_not_shadowed(client):
    if not ui.WEB_DIST.exists():
        pytest.skip("web/dist не зібрано")
    r = client.get("/project/42/vertical")
    assert r.status_code == 200 and "<div id=\"root\">" in r.text
    assert client.get("/sw.js").headers["cache-control"] == "no-cache"
    assert client.get("/manifest.webmanifest").json()["display"] == "standalone"
    assert client.get("/api/nope").status_code == 404
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/../../etc/passwd").status_code in (200, 404) and "root:" not in client.get("/../../etc/passwd").text


def test_track_energy_and_cover_after_edit(settings):
    """Енергія треку для смужки і обкладинка — на проєкті з треком і монтажем (з e2e-шаблону)."""
    pytest.importorskip("librosa")
    from tests.media import analysis_video, song
    from core.projects import create_project
    from sources.ingest import ingest_file
    from worker.runner import Worker

    conn = open_db(settings.database_path)
    p = create_project(conn, settings.library_path, "Т")
    ingest_file(conn, settings.library_path, p, analysis_video(settings.library_path / "v.mp4"), "v.mp4", "upload", move=True)
    ingest_file(conn, settings.library_path, p, song(settings.library_path / "s.mp3"), "s.mp3", "upload", move=True)
    w = Worker(settings)
    while w.run_one(conn):
        pass
    with TestClient(create_app(settings, start_worker=False)) as c:
        tr = c.get(f"/api/projects/{p['id']}/track").json()
        assert abs(tr["tempo"] - 120) < 2 and len(tr["bars"]) > 40 and tr["drops"] and tr["window"] is None
        r = c.get(f"/api/projects/{p['id']}/cover.jpg")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
