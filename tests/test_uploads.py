import base64
import json

import pytest
from fastapi.testclient import TestClient

from server.app import create_app
from server.uploads import parse_metadata
from worker.runner import Worker

TUS = {"Tus-Resumable": "1.0.0"}


def meta(**kw):
    return ",".join(f"{k} {base64.b64encode(str(v).encode()).decode()}" for k, v in kw.items())


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, start_worker=False)) as c:
        yield c


def test_parse_metadata():
    assert parse_metadata(meta(filename="Концерт.mov", project_id=3) + ",flag") == {
        "filename": "Концерт.mov", "project_id": "3", "flag": ""}


def test_project_api(client):
    r = client.post("/api/projects", json={"name": "Концерт Brixton"})
    assert r.status_code == 201 and r.json()["slug"] == "Концерт Brixton"
    assert client.post("/api/projects", json={"name": "Концерт Brixton"}).status_code == 409
    assert [p["name"] for p in client.get("/api/projects").json()] == ["Концерт Brixton"]
    assert client.post(f"/api/projects/{r.json()['id']}/edit").status_code == 409  # ще немає відео


def test_resumable_upload_end_to_end(client, settings, media_dir):
    pid = client.post("/api/projects", json={"name": "Brixton"}).json()["id"]
    data = (media_dir / "concert.mp4").read_bytes()
    assert client.options("/api/uploads").headers["Tus-Extension"] == "creation,termination"

    r = client.post("/api/uploads", headers={**TUS, "Upload-Length": str(len(data)),
                                             "Upload-Metadata": meta(filename="IMG_0420.MP4", project_id=pid)})
    assert r.status_code == 201
    url = r.headers["Location"]

    patch = {**TUS, "Content-Type": "application/offset+octet-stream"}
    half = len(data) // 2
    r = client.patch(url, headers={**patch, "Upload-Offset": "0"}, content=data[:half])
    assert r.status_code == 204 and r.headers["Upload-Offset"] == str(half)

    # обрив зв'язку: клієнт питає, звідки продовжити
    r = client.head(url, headers=TUS)
    assert r.headers["Upload-Offset"] == str(half)
    assert client.patch(url, headers={**patch, "Upload-Offset": "0"}, content=data).status_code == 409

    r = client.patch(url, headers={**patch, "Upload-Offset": str(half)}, content=data[half:])
    assert r.status_code == 204
    media_id = int(r.headers["X-Media-Id"])

    project = client.get(f"/api/projects/{pid}").json()
    assert project["videos"] == 1
    assert project["media"][0]["filename"] == "IMG_0420.MP4"
    original = settings.library_path / "Brixton" / "originals" / "IMG_0420.MP4"
    assert original.read_bytes() == data
    assert not list((settings.library_path / ".uploads").glob("*.part"))

    # воркер: ffprobe + проксі
    worker = Worker(settings)
    from core.db import open_db
    conn = open_db(settings.database_path)
    assert worker.run_one(conn)
    m = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    assert m["status"] == "ready", m["error"]
    assert json.loads(m["probe_json"])["width"] == 1280
    assert m["shot_at"] == "2025-06-14T21:03:05Z"
    assert (settings.library_path / m["proxy_path"]).exists()
    assert (settings.library_path / m["gemini_proxy_path"]).exists()

    assert client.post(f"/api/projects/{pid}/edit").status_code == 202
    statuses = {(j["kind"], j["status"]) for j in client.get("/api/queue").json()}
    assert ("edit_project", "queued") in statuses and ("prepare_media", "done") in statuses


def test_upload_validation(client):
    pid = client.post("/api/projects", json={"name": "X"}).json()["id"]
    h = {**TUS, "Upload-Length": "10"}
    assert client.post("/api/uploads", headers={**h, "Upload-Metadata": meta(filename="a.exe", project_id=pid)}).status_code == 415
    assert client.post("/api/uploads", headers={**h, "Upload-Metadata": meta(filename="a.mp4", project_id=999)}).status_code == 404
    assert client.post("/api/uploads", headers={"Upload-Length": "10"}).status_code == 412


def test_upload_overflow_and_termination(client, settings):
    pid = client.post("/api/projects", json={"name": "X"}).json()["id"]
    url = client.post("/api/uploads", headers={**TUS, "Upload-Length": "4",
                                               "Upload-Metadata": meta(filename="a.mp4", project_id=pid)}).headers["Location"]
    patch = {**TUS, "Content-Type": "application/offset+octet-stream", "Upload-Offset": "0"}
    assert client.patch(url, headers=patch, content=b"12345678").status_code == 413
    assert client.delete(url, headers=TUS).status_code == 204
    assert client.head(url, headers=TUS).status_code == 404
    assert not list((settings.library_path / ".uploads").glob("*.part"))


def test_corrupt_file_marked_error(client, settings):
    pid = client.post("/api/projects", json={"name": "X"}).json()["id"]
    data = b"not a video at all"
    url = client.post("/api/uploads", headers={**TUS, "Upload-Length": str(len(data)),
                                               "Upload-Metadata": meta(filename="broken.mp4", project_id=pid)}).headers["Location"]
    client.patch(url, headers={**TUS, "Content-Type": "application/offset+octet-stream", "Upload-Offset": "0"}, content=data)
    from core.db import open_db
    conn = open_db(settings.database_path)
    Worker(settings).run_one(conn)
    m = conn.execute("SELECT * FROM media").fetchone()
    assert m["status"] == "error" and "ffprobe" in m["error"]
