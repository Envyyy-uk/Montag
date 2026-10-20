"""Бібліотека музики й підбір треку під відео."""

import json

import pytest
from fastapi.testclient import TestClient

from analysis import llm
from core.db import open_db
from core.projects import create_project
from edit import music_pick
from edit.build import project_tracks
from server.app import create_app
from sources import gdrive, music
from tests import media
from tests.fake_drive import FakeDrive
from worker.runner import Worker


@pytest.fixture(scope="module")
def songs(tmp_path_factory):
    d = tmp_path_factory.mktemp("songs")
    return {
        "fast": media.song(d / "Band - Fast One.wav", bpm=128),
        "slow": media.song(d / "Calm.wav", bpm=80, sections=[(16, "low"), (8, "mid"), (16, "low")]),
    }


@pytest.fixture
def lib(settings, songs):
    import shutil
    settings.ensure_dirs()
    for p in songs.values():
        shutil.copy(p, settings.music_path / p.name)
    conn = open_db(settings.database_path)
    music.scan(conn, settings)
    w = Worker(settings)
    while w.run_one(conn):
        pass
    return conn


def _concert(conn, settings):
    p = create_project(conn, settings.library_path, "Концерт")
    mid = conn.execute("INSERT INTO media(project_id, kind, original_path, filename, sha256, size_bytes, source)"
                       " VALUES (?, 'video', 'x.mp4', 'x.mp4', ?, 1, 'upload')", (p["id"], f"sha{p['id']}")).lastrowid
    conn.execute("INSERT INTO gemini_requests(project_id, media_id, win_start, win_end, style_key, segments, status,"
                 " event_type, event_confidence) VALUES (?, ?, 0, 1, 'k', '[]', 'done', 'концерт', 0.9)", (p["id"], mid))
    return p


def test_scan_and_analyze(lib, settings):
    rows = {r["title"]: r for r in lib.execute("SELECT * FROM music_tracks")}
    assert set(rows) == {"Fast One", "Calm"} and rows["Fast One"]["artist"] == "Band"
    assert all(r["status"] == "analyzed" for r in rows.values())
    assert abs(rows["Fast One"]["tempo"] - 128) < 4 and rows["Fast One"]["energy_peak"] > rows["Calm"]["energy_peak"]
    assert music.scan(lib, settings) == []  # без змін — нічого нового
    (settings.music_path / "Calm.wav").unlink()
    music.scan(lib, settings)
    assert lib.execute("SELECT status FROM music_tracks WHERE title = 'Calm'").fetchone()[0] == "missing"


def test_suggest_local_prefers_fitting_tempo(lib, settings):
    p = _concert(lib, settings)
    res = music_pick.suggest(lib, settings, p["id"], use_ai=False)
    assert res["source"] == "local" and res["picks"][0]["title"] == "Fast One"
    assert "пасує до події «концерт»" in res["picks"][0]["reason"] and res["picks"][0]["fragment"]["end"] > res["picks"][0]["fragment"]["start"]
    assert res["ideas"] == []


def test_suggest_with_ai_picks_and_ideas(lib, settings, monkeypatch):
    p = _concert(lib, settings)
    calm = lib.execute("SELECT id FROM music_tracks WHERE title = 'Calm'").fetchone()[0]
    seen = {}

    def fake_ask(conn, settings_, prompt, schema, purpose, project_id=None, attempts=2):
        seen["prompt"] = prompt
        return {"picks": [{"id": calm, "reason": "м'який контраст до натовпу"}, {"id": 999, "reason": "вигадка"}],
                "ideas": [{"artist": "Daft Punk", "title": "One More Time", "why": "фестивальний вайб"}]}
    monkeypatch.setattr(llm, "ask_json", fake_ask)
    res = music_pick.suggest(lib, settings, p["id"])
    assert res["source"] == "gemini" and [x["track_id"] for x in res["picks"]] == [calm]  # чужі id відкинуто
    assert res["ideas"][0]["soundcloud"].startswith("https://soundcloud.com/search/sounds?q=Daft+Punk")
    assert "концерт" in seen["prompt"] and "Fast One" in seen["prompt"]


def test_use_track_pins_it_and_reuses_analysis(lib, settings):
    p = _concert(lib, settings)
    with TestClient(create_app(settings, start_worker=False)) as c:
        tid = lib.execute("SELECT id FROM music_tracks WHERE title = 'Fast One'").fetchone()[0]
        r = c.post(f"/api/projects/{p['id']}/music/{tid}/use", json={"rerender": False}).json()
        lib_file = settings.music_path / "Band - Fast One.wav"
        assert lib_file.exists()  # файл у бібліотеці не переміщено
        w = Worker(settings)
        while w.run_one(lib):
            pass
        assert project_tracks(lib, p["id"])[0]["id"] == r["media_id"]
        st = json.loads(lib.execute("SELECT settings FROM projects WHERE id = ?", (p["id"],)).fetchone()[0])
        assert st["track_media_id"] == r["media_id"]
        lib_list = c.get("/api/music").json()
        assert len(lib_list["tracks"]) == 2 and c.get(f"/api/music/{tid}/audio").status_code == 200
        assert c.post(f"/api/projects/{p['id']}/music/999/use", json={}).status_code == 404


def test_drive_music_folder_is_not_a_project(settings, monkeypatch, songs):
    fake = FakeDrive()
    monkeypatch.setattr(gdrive, "_override", fake)
    conn = open_db(settings.database_path)
    poller = gdrive.DrivePoller(settings)
    poller.poll(conn)
    root = gdrive.root_folder(conn, settings, fake)
    mfolder = gdrive.music_folder(conn, settings, fake, root)
    fid = fake.put(mfolder, "Drive Song.wav", songs["fast"].read_bytes())
    poller.poll(conn)
    poller.poll(conn)  # файл стабільний — забираємо
    w = Worker(settings)
    while w.run_one(conn):
        pass
    assert (settings.music_path / "drive" / "Drive Song.wav").exists()
    assert fid not in fake.deleted
    assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM music_tracks WHERE path = 'drive/Drive Song.wav'").fetchone()[0] == "analyzed"
