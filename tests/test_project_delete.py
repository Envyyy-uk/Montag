"""Видалення проєкту (оригінали й основна версія — в архів) і старих версій."""

import json
import os
import stat

from fastapi.testclient import TestClient

from core.db import open_db
from core.projects import create_project
from server.app import create_app
from sources import gdrive
from tests import media
from tests.fake_drive import FakeDrive
from worker.runner import Worker


def _project_with_versions(settings):
    conn = open_db(settings.database_path)
    p = create_project(conn, settings.library_path, "Фест")
    lib, base = settings.library_path, settings.library_path / p["slug"]
    orig = media.sdr_video(base / "originals" / "a.mp4", w=160, h=90, dur=1)
    os.chmod(orig, stat.S_IREAD)
    conn.execute("INSERT INTO media(project_id, kind, original_path, filename, sha256, size_bytes, source)"
                 " VALUES (?, 'video', ?, 'a.mp4', 'x', 1, 'upload')", (p["id"], str(orig.relative_to(lib))))
    ids = []
    for v in (1, 2, 3):
        ed = base / "edits" / f"clip_v{v:03d}"
        ed.mkdir(parents=True)
        (ed / "timeline.json").write_text("{}")
        exp = base / "exports" / f"clip_v{v:03d}.mp4"
        exp.parent.mkdir(exist_ok=True)
        exp.write_bytes(b"x" * 1000 * v)
        dv = base / "exports" / f"clip_v{v:03d}_davinci"
        (dv / "media").mkdir(parents=True)
        os.link(orig, dv / "media" / "a.mp4")  # жорстке посилання на оригінал, «лише читання»
        (dv / "x.fcpxml").write_text("<x/>")
        ids.append(conn.execute(
            "INSERT INTO edits(project_id, kind, version, timeline_path, export_path, davinci_path, summary)"
            " VALUES (?, 'clip', ?, ?, ?, ?, '{}')",
            (p["id"], v, str((ed / "timeline.json").relative_to(lib)), str(exp.relative_to(lib)),
             str((dv / "x.fcpxml").relative_to(lib)))).lastrowid)
    reel = base / "exports" / "vert_1.mp4"
    reel.write_bytes(b"r")
    vd = base / "edits" / "vertical_v001"
    vd.mkdir(parents=True)
    (vd / "reels.json").write_text("{}")
    conn.execute("INSERT INTO edits(project_id, kind, version, timeline_path, summary) VALUES (?, 'vertical', 1, ?, ?)",
                 (p["id"], str((vd / "reels.json").relative_to(lib)),
                  json.dumps({"reels": [{"path": str(reel.relative_to(lib))}]})))
    conn.commit()
    return conn, p, orig, ids


def test_prune_keeps_main_and_originals(settings):
    conn, p, orig, ids = _project_with_versions(settings)
    with TestClient(create_app(settings, start_worker=False)) as c:
        c.post(f"/api/edits/{ids[0]}/main")  # основна — v1
        plan = c.get(f"/api/projects/{p['id']}/prune-plan").json()
        assert plan["count"] == 2 and set(plan["edit_ids"]) == {ids[1], ids[2]}
        c.post(f"/api/projects/{p['id']}/prune")
        left = {e["version"] for e in c.get(f"/api/projects/{p['id']}/edits").json() if e["kind"] == "clip"}
        assert left == {1}
    assert orig.exists() and orig.read_bytes()[:4] and not (orig.stat().st_mode & stat.S_IWUSR)


def test_delete_project_archives_originals_and_main(settings, monkeypatch):
    fake = FakeDrive()
    monkeypatch.setattr(gdrive, "_override", fake)
    conn, p, orig, ids = _project_with_versions(settings)
    root = gdrive.root_folder(conn, settings, fake)
    folder = gdrive.project_folder(conn, settings, fake, p)
    data = orig.read_bytes()
    with TestClient(create_app(settings, start_worker=False)) as c:
        plan = c.get(f"/api/projects/{p['id']}/delete-plan").json()
        assert plan["originals"] == 1 and "clip_v003.mp4" in plan["keep"] and "vert_1.mp4" in plan["keep"]
        r = c.delete(f"/api/projects/{p['id']}").json()
        assert r["originals"] == 1 and r["kept"] == 2 and r["drive"]
        assert c.get(f"/api/projects/{p['id']}").status_code == 404
    arch = settings.archive_path / "Фест"
    assert (arch / "originals" / "a.mp4").read_bytes() == data
    assert not ((arch / "originals" / "a.mp4").stat().st_mode & stat.S_IWUSR)
    assert sorted(x.name for x in (arch / "готове").iterdir()) == ["clip_v003.mp4", "vert_1.mp4"]
    assert not (settings.library_path / p["slug"]).exists()
    assert conn.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 0
    w = Worker(settings)
    while w.run_one(conn):
        pass
    assert folder in fake.trashed and "Фест" not in fake.names_in(root)
    # папку не створено заново
    gdrive.DrivePoller(settings).poll(conn)
    assert "Фест" not in fake.names_in(root)


def test_delete_refused_while_running(settings):
    from worker import queue
    conn, p, _, _ = _project_with_versions(settings)
    jid = queue.enqueue(conn, "edit_project", {"project_id": p["id"]}, project_id=p["id"])
    conn.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (jid,))
    conn.commit()
    with TestClient(create_app(settings, start_worker=False)) as c:
        r = c.delete(f"/api/projects/{p['id']}")
        assert r.status_code == 409 and "зупини" in r.json()["detail"]


def test_mcp_delete_project_two_steps(settings):
    import asyncio

    import httpx
    from mcp import Client

    from mcp_server.api import Api
    from mcp_server.server import build_server
    conn, p, _, _ = _project_with_versions(settings)
    app = create_app(settings, start_worker=False)
    server = build_server(Api("http://127.0.0.1", transport=httpx.ASGITransport(app=app)))

    async def go():
        async with Client(server) as c:
            msg = (await c.call_tool("prepare_delete_project", {"project_id": p["id"]})).content[0].text
            code = msg.split('code="')[1].split('"')[0]
            bad = await c.call_tool("delete_project", {"project_id": p["id"], "code": "nope"})
            ok = await c.call_tool("delete_project", {"project_id": p["id"], "code": code})
            return msg, bad, ok
    msg, bad, ok = asyncio.run(go())
    assert "в архів" in msg and bad.is_error and not ok.is_error
    assert (settings.archive_path / "Фест" / "originals" / "a.mp4").exists()
