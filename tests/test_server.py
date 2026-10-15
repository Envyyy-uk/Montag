from fastapi.testclient import TestClient

from core.db import MIGRATIONS
from server.app import create_app


def test_health(settings):
    with TestClient(create_app(settings)) as client:
        r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "schema_version": len(MIGRATIONS)}
    assert settings.database_path.exists()


def test_system(settings):
    with TestClient(create_app(settings)) as client:
        r = client.get("/api/system")
    assert r.status_code == 200
    assert {"ok", "nvenc", "checks"} <= r.json().keys()
