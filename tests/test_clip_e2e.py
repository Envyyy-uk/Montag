"""Повний прогін Режиму 1: 3 «концертні» відео + трек → кліп 60 с."""

import json
import subprocess

import numpy as np
import pytest
from fastapi.testclient import TestClient

from core.db import open_db
from core.projects import create_project
from server.app import create_app
from sources.ingest import ingest_file
from worker.runner import Worker
from worker.stages import request_edit


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-print_format", "json", "-show_streams",
                          str(path)], capture_output=True, text=True, check=True).stdout
    return {s["codec_type"]: s for s in json.loads(out)["streams"]}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    from core.config import Settings
    from tests import media

    root = tmp_path_factory.mktemp("e2e")
    settings = Settings(_env_file=None, library_dir=root / "library", inbox_dir=root / "inbox",
                        db_path=root / "library" / "db.sqlite3", whisper_enabled=False)
    settings.ensure_dirs()
    conn = open_db(settings.database_path)
    p = create_project(conn, settings.library_path, "Концерт Brixton")
    for i in range(3):
        src = media.analysis_video(root / f"cam{i}.mp4")
        ingest_file(conn, settings.library_path, p, src, f"IMG_{i}.mp4", "upload", move=True)
    ingest_file(conn, settings.library_path, p, media.song(root / "track.mp3"), "track.mp3", "upload", move=True)
    w = Worker(settings)
    while w.run_one(conn):
        pass
    request_edit(conn, p["id"])  # без ключа Gemini — локальні оцінки
    while w.run_one(conn):
        pass
    return settings, conn, p


def test_clip_built(built):
    settings, conn, p = built
    jobs = {(j["kind"], j["status"]) for j in conn.execute("SELECT * FROM jobs")}
    assert ("edit_project", "done") in jobs, list(conn.execute("SELECT kind, status, error FROM jobs"))
    edit = conn.execute("SELECT * FROM edits").fetchone()
    tl = json.loads((settings.library_path / edit["timeline_path"]).read_text(encoding="utf-8"))
    assert 58 <= tl["duration"] <= 62
    assert tl["fps"] == 30.0
    assert conn.execute("SELECT status FROM projects").fetchone()[0] == "edited"


def test_all_cuts_on_beats(built):
    settings, conn, p = built
    from analysis.track import TrackAnalysis

    edit = conn.execute("SELECT * FROM edits").fetchone()
    tl = json.loads((settings.library_path / edit["timeline_path"]).read_text(encoding="utf-8"))
    track_id = tl["track_media_id"]
    ta = TrackAnalysis.load(settings.library_path / p["slug"] / "analysis" / f"{track_id}_track.json")
    beats = np.array(ta.beats) - tl["track_in"]
    for c in tl["clips"][1:]:
        assert np.abs(beats - c["start"]).min() <= 0.5 / tl["fps"] + 1e-6, c["start"]


def test_no_rejected_footage(built):
    settings, conn, p = built
    edit = conn.execute("SELECT * FROM edits").fetchone()
    tl = json.loads((settings.library_path / edit["timeline_path"]).read_text(encoding="utf-8"))
    rejected = conn.execute("SELECT media_id, start, end FROM segments WHERE reject_reason IS NOT NULL").fetchall()
    for c in tl["clips"]:
        dur = c["end"] - c["start"]
        mid = c["src_in"] + dur / 2
        for r in rejected:
            if r["media_id"] == c["media_id"]:
                assert not (r["start"] < mid < r["end"]), (c, dict(r))


def test_live_inserts(built):
    settings, conn, p = built
    edit = conn.execute("SELECT * FROM edits").fetchone()
    tl = json.loads((settings.library_path / edit["timeline_path"]).read_text(encoding="utf-8"))
    ins = tl["inserts"]
    assert len(ins) >= 2
    for x, y in zip(ins, ins[1:]):
        assert y["start"] - x["end"] >= 8 - 1e-6
    assert {m["kind"] for m in tl["markers"]} >= {"insert"}
    kinds = [c["kind"] for c in tl["clips"]]
    assert kinds.count("insert") == len(ins)


def test_draft_and_mix(built):
    settings, conn, p = built
    edit = conn.execute("SELECT * FROM edits").fetchone()
    tl = json.loads((settings.library_path / edit["timeline_path"]).read_text(encoding="utf-8"))
    st = _probe(settings.library_path / edit["preview_path"])
    assert (st["video"]["width"], st["video"]["height"]) == (1280, 720)
    assert int(st["video"]["nb_read_frames"]) == round(tl["duration"] * tl["fps"])
    assert abs(float(st["audio"]["duration"]) - tl["duration"]) < 0.1


def test_api_and_reedit_without_gemini(built):
    settings, conn, p = built
    before = conn.execute("SELECT COUNT(*) FROM gemini_requests").fetchone()[0]
    with TestClient(create_app(settings, start_worker=False)) as client:
        edits = client.get(f"/api/projects/{p['id']}/edits").json()
        assert edits[0]["summary"]["inserts"] >= 2
        assert client.get(f"/api/edits/{edits[0]['id']}/preview").status_code == 200
        r = client.put(f"/api/projects/{p['id']}/clip-settings", json={"length": 30, "inserts": False})
        assert r.json()["length"] == 30
        assert client.post(f"/api/projects/{p['id']}/reedit").status_code == 202
    w = Worker(settings)
    while w.run_one(conn):
        pass
    v2 = conn.execute("SELECT * FROM edits WHERE version = 2").fetchone()
    s2 = json.loads(v2["summary"])
    assert 28 <= s2["duration"] <= 32 and s2["inserts"] == 0
    assert conn.execute("SELECT COUNT(*) FROM gemini_requests").fetchone()[0] == before
