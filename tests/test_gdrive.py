import pytest
from fastapi.testclient import TestClient

from core.db import open_db
from core.projects import create_project
from sources import gdrive
from tests.fake_drive import FakeDrive
from worker import queue
from worker.runner import Worker


@pytest.fixture
def drive(monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(gdrive, "_override", fake)
    return fake


def run_all(settings, conn, limit=30):
    w = Worker(settings)
    for _ in range(limit):
        conn.execute("UPDATE jobs SET run_after = NULL")
        if not w.run_one(conn):
            return
    raise AssertionError("черга не спорожніла")


def test_new_project_creates_drive_folders(settings, drive):
    from server.app import create_app

    with TestClient(create_app(settings, start_worker=False)) as client:
        pid = client.post("/api/projects", json={"name": "Концерт Brixton"}).json()["id"]
        conn = open_db(settings.database_path)
        run_all(settings, conn)
        root = drive.find_folder("Автомонтаж", None)
        pf = drive.find_folder("Концерт Brixton", root)
        assert pf and drive.find_folder("Готово", pf)
        st = client.get("/api/drive").json()
        assert st["configured"] and st["projects"][str(pid)]["id"] == pf


def test_fetch_from_phone_verified_and_deleted(settings, drive, media_dir):
    conn = open_db(settings.database_path)
    root = drive.mkdir("Автомонтаж")
    pf = drive.mkdir("Поїздка у Львів", root)  # папку створили з iPhone
    data = (media_dir / "concert.mp4").read_bytes()
    fid = drive.put(pf, "IMG_0420.MOV.mp4", data)
    drive.put(pf, "notes.txt", b"hello")  # не відео — ігнорується

    poller = gdrive.DrivePoller(settings)
    assert poller.poll(conn) == []          # перший погляд — ще може довантажуватись
    assert poller.poll(conn) == [fid]       # той самий розмір і MD5 — забираємо
    project = conn.execute("SELECT * FROM projects").fetchone()
    assert project["name"] == "Поїздка у Львів"
    run_all(settings, conn)

    m = conn.execute("SELECT * FROM media").fetchone()
    assert m["source"] == "drive" and m["filename"] == "IMG_0420.MOV.mp4"
    assert (settings.library_path / m["original_path"]).read_bytes() == data
    assert drive.deleted == [fid]
    assert drive.names_in(pf) == ["notes.txt"]
    assert not list((settings.library_path / ".uploads").glob("drive_*"))
    assert m["status"] == "analyzed"  # далі той самий конвеєр, що й для upload


def test_corrupted_download_not_deleted(settings, drive, media_dir):
    conn = open_db(settings.database_path)
    root = drive.mkdir("Автомонтаж")
    pf = drive.mkdir("X", root)
    fid = drive.put(pf, "a.mp4", (media_dir / "concert.mp4").read_bytes())
    poller = gdrive.DrivePoller(settings)
    poller.poll(conn)
    poller.poll(conn)
    drive.corrupt_next = True
    w = Worker(settings)
    w.run_one(conn)
    job = conn.execute("SELECT * FROM jobs WHERE kind = 'drive_fetch'").fetchone()
    assert job["status"] == "queued" and "MD5" in job["error"]
    assert fid in drive.items and not drive.deleted               # у Drive лишається
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    run_all(settings, conn)                                        # повтор — уже цілий
    assert drive.deleted == [fid]


def test_duplicate_from_drive_is_deleted_without_new_media(settings, drive, media_dir):
    conn = open_db(settings.database_path)
    root = drive.mkdir("Автомонтаж")
    pf = drive.mkdir("X", root)
    data = (media_dir / "concert.mp4").read_bytes()
    poller = gdrive.DrivePoller(settings)
    for name in ("a.mp4", "b.mp4"):
        drive.put(pf, name, data)
    poller.poll(conn)
    poller.poll(conn)
    run_all(settings, conn)
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 1
    assert len(drive.deleted) == 2


def test_growing_file_waits(settings, drive):
    conn = open_db(settings.database_path)
    root = drive.mkdir("Автомонтаж")
    pf = drive.mkdir("X", root)
    fid = drive.put(pf, "a.mp4", b"x" * 10)
    poller = gdrive.DrivePoller(settings)
    poller.poll(conn)
    drive.items[fid]["data"] = b"x" * 20  # телефон ще вантажить
    assert poller.poll(conn) == []


def test_upload_ready_mp4s_once(settings, drive, tmp_path):
    import json

    conn = open_db(settings.database_path)
    from core.projects import create_project
    p = create_project(conn, settings.library_path, "Концерт")
    exp = settings.library_path / p["slug"] / "exports"
    for n in ("clip_720p.mp4", "v1.mp4", "v2.mp4"):
        (exp / n).write_bytes(b"mp4")
    rel = lambda n: str((exp / n).relative_to(settings.library_path))  # noqa: E731
    clip = conn.execute("INSERT INTO edits(project_id, kind, version, timeline_path, preview_path, export_path)"
                        " VALUES (?, 'clip', 1, 't', ?, 'big.mp4')", (p["id"], rel("clip_720p.mp4"))).lastrowid
    vert = conn.execute("INSERT INTO edits(project_id, kind, version, timeline_path, summary) VALUES (?, 'vertical', 1, 't', ?)",
                        (p["id"], json.dumps({"reels": [{"path": rel("v1.mp4")}, {"path": rel("v2.mp4")}]}))).lastrowid
    for eid in (clip, vert, clip):
        queue.enqueue(conn, "drive_upload", {"edit_id": eid}, dedupe=False)
    run_all(settings, conn)
    done = drive.find_folder("Готово", drive.find_folder("Концерт", drive.find_folder("Автомонтаж", None)))
    assert drive.names_in(done) == ["clip_720p.mp4", "v1.mp4", "v2.mp4"]  # без дублікатів


def test_no_drive_is_noop(settings):
    conn = open_db(settings.database_path)
    assert gdrive.DrivePoller(settings).poll(conn) == []
    assert not gdrive.configured(settings)


def test_old_projects_get_folders_after_connecting(settings, drive):
    conn = open_db(settings.database_path)
    p = create_project(conn, settings.library_path, "Старий концерт")  # створений до підключення Drive
    gdrive.DrivePoller(settings).poll(conn)
    run_all(settings, conn)
    root = gdrive.root_folder(conn, settings, drive)
    names = drive.names_in(root)
    assert "Старий концерт" in names and "Музика" in names
    assert str(p["id"]) in gdrive._state(conn, "drive_projects")
