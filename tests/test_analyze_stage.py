import json
import shutil
from types import SimpleNamespace

from core.db import open_db
from core.projects import create_project
from sources.ingest import ingest_file
from worker import queue
from worker.runner import Worker
from worker.stages import analysis_dir


class FakeWhisper:
    calls = 0

    def transcribe(self, audio, **kw):
        FakeWhisper.calls += 1
        assert audio.dtype.name == "float32" and kw["vad_filter"]
        word = SimpleNamespace(start=1.0, word=" Привіт", end=1.4)
        seg = SimpleNamespace(start=1.0, end=2.5, text=" Привіт, Brixton!", words=[word])
        return iter([seg]), SimpleNamespace(language="uk", language_probability=0.91)


def test_full_pipeline(settings, tmp_path, monkeypatch):
    from analysis import transcribe
    from tests import media

    settings = settings.model_copy(update={"whisper_enabled": True})
    monkeypatch.setattr(transcribe, "get_model", lambda name: FakeWhisper())
    conn = open_db(settings.database_path)
    p = create_project(conn, settings.library_path, "Концерт")
    src = media.analysis_video(tmp_path / "IMG_7.mp4")
    ingest_file(conn, settings.library_path, p, src, src.name, "upload", move=True)

    w = Worker(settings)
    assert w.run_one(conn)  # prepare_media
    assert w.run_one(conn)  # analyze_media
    assert not w.run_one(conn)
    jobs = conn.execute("SELECT kind, status, error FROM jobs ORDER BY id").fetchall()
    assert [(j["kind"], j["status"]) for j in jobs] == [("prepare_media", "done"), ("analyze_media", "done")], \
        [j["error"] for j in jobs]

    m = conn.execute("SELECT * FROM media").fetchone()
    assert m["status"] == "analyzed"
    summary = json.loads(m["analysis_json"])
    assert summary["segments"] >= 6 and summary["rejected"] >= 3
    assert summary["language"] == "uk" and summary["speech_segments"] == 1

    segs = conn.execute("SELECT * FROM segments ORDER BY idx").fetchall()
    assert all(0 <= s["local_score"] <= 1 for s in segs)
    assert all(s["end"] - s["start"] <= 6.0 + 1e-6 for s in segs)  # довгі сцени порізані
    assert segs[0]["reject_reason"] is None
    burst = [s for s in segs if s["burst_db"] > 0]
    assert burst and all(s["start"] < 21 and s["end"] > 19 for s in burst)
    ev = conn.execute("SELECT * FROM audio_events").fetchall()
    assert [e["kind"] for e in ev] == ["crowd"]

    out = analysis_dir(settings, p["slug"])
    srt = (out / f"{m['id']}.srt").read_text(encoding="utf-8")
    assert "00:00:01,000 --> 00:00:02,500" in srt and "Привіт, Brixton!" in srt

    # повторний аналіз (наприклад, після перезапуску) бере кеш і не дублює рядки
    queue.enqueue(conn, "analyze_media", {"media_id": m["id"]}, project_id=p["id"], dedupe=False)
    assert w.run_one(conn)
    assert FakeWhisper.calls == 1
    assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == len(segs)


def test_analysis_api(settings, tmp_path):
    from fastapi.testclient import TestClient

    from server.app import create_app
    from tests import media

    with TestClient(create_app(settings, start_worker=False)) as client:
        conn = open_db(settings.database_path)
        p = create_project(conn, settings.library_path, "X")
        src = media.sdr_video(tmp_path / "a.mp4", dur=3)
        mid = ingest_file(conn, settings.library_path, p, src, "a.mp4", "upload", move=True).media["id"]
        w = Worker(settings)
        while w.run_one(conn):
            pass
        r = client.get(f"/api/media/{mid}/analysis").json()
        assert r["status"] == "analyzed" and r["segments"] and r["transcript"] is None
        assert {"sharpness", "shake", "motion"} <= r["segments"][0]["metrics"].keys()
        assert client.get("/api/media/999/analysis").status_code == 404


def test_whisper_failure_does_not_block(settings, tmp_path, monkeypatch):
    from analysis import transcribe
    from tests import media

    def broken(name):
        raise RuntimeError("немає інтернету для моделі")

    settings = settings.model_copy(update={"whisper_enabled": True})
    monkeypatch.setattr(transcribe, "get_model", broken)
    conn = open_db(settings.database_path)
    p = create_project(conn, settings.library_path, "X")
    src = media.sdr_video(tmp_path / "a.mp4", dur=2)
    ingest_file(conn, settings.library_path, p, src, "a.mp4", "upload", move=True)
    w = Worker(settings)
    while w.run_one(conn):
        pass
    m = conn.execute("SELECT * FROM media").fetchone()
    assert m["status"] == "analyzed"
    assert "інтернету" in json.loads(m["analysis_json"])["whisper_error"]
