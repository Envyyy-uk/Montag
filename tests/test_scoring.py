import json
import shutil

import pytest
from pydantic import SecretStr

from analysis import scoring
from core import budget
from core.db import open_db
from core.projects import create_project
from sources.ingest import ingest_file
from tests.fake_gemini import USAGE, FakeBackend, good_answer
from worker import queue
from worker.runner import Worker
from worker.stages import request_edit


# ---------- бюджет ----------

def test_usage_cost_known_and_batch():
    full = budget.usage_cost("gemini-3.6-flash", USAGE, batch=False)
    r = budget.model_rates("gemini-3.6-flash")
    expect = (15000 * r["input"] + 5000 * r["audio_input"] + 500 * r["output"]) / 1e6
    assert full == pytest.approx(expect)
    assert budget.usage_cost("models/gemini-3.6-flash-001", USAGE, batch=True) == pytest.approx(expect / 2)


def test_unknown_model_uses_conservative_default():
    d = budget.model_rates("gemini-99-ultra")
    assert d == budget.pricing()["models"]["_default"]
    assert d["input"] >= max(m["input"] for k, m in budget.pricing()["models"].items() if k != "_default")


def test_estimate_scales():
    a = budget.estimate_request("gemini-3.6-flash", 60, True, 2000, 0, 10, batch=True)
    b = budget.estimate_request("gemini-3.6-flash", 120, True, 2000, 0, 10, batch=True)
    assert 0 < a < b < 0.1
    assert budget.estimate_request("gemini-3.6-flash", 60, True, 2000, 0, 10, batch=False) == pytest.approx(a * 2)


# ---------- відповідь ----------

def _resp(segs, **kw):
    return json.dumps({"event_type": "концерт", "event_confidence": 0.8, "segments": segs, **kw})


SEG = {"id": 0, "score": 8, "description": "x", "tags": ["натовп"], "vertical_ok": True}


def test_validate_ok_and_filters_extra():
    data = scoring.validate(_resp([SEG, {**SEG, "id": 5}]), {0})
    assert [s["id"] for s in data["segments"]] == [0]


@pytest.mark.parametrize("text", [
    None, "", "не json", _resp([{**SEG, "score": 11}]), _resp([{**SEG, "tags": ["котик"]}]),
    _resp([{k: v for k, v in SEG.items() if k != "vertical_ok"}]), _resp([{**SEG, "id": 1}]),
    json.dumps({"segments": [SEG]}),
])
def test_validate_rejects(text):
    with pytest.raises(scoring.InvalidResponse):
        scoring.validate(text, {0})


def test_windows_grouping():
    C = scoring.Candidate
    cands = [C(1, 0, 3, .5), C(1, 3, 6, .5), C(1, 170, 173, .5), C(1, 181, 184, .5), C(1, 400, 402, .5)]
    w = scoring.windows(cands, 180)
    assert [(a, b, len(i)) for a, b, i in w] == [(0, 173, 3), (181, 402, 2)] or \
        [(a, b, len(i)) for a, b, i in w] == [(0, 173, 3), (181, 184, 1), (400, 402, 1)]


def test_prompt_contains_taste_and_ids():
    taste = scoring.Taste("Люблю натовп", [], [])
    p = scoring.build_prompt(taste, [(0, 0, 3.2), (1, 65.5, 68)])
    assert "Люблю натовп" in p and "0: 00:00.0–00:03.2" in p and "1: 01:05.5–01:08.0" in p
    assert "не недолік".lower() in p.lower()


# ---------- повний цикл ----------

@pytest.fixture(scope="session")
def analysed_library(tmp_path_factory):
    """Один раз: проєкт із проаналізованим відео. Тести отримують копію."""
    from core.config import Settings
    from tests import media

    root = tmp_path_factory.mktemp("tpl")
    settings = Settings(_env_file=None, library_dir=root / "library", inbox_dir=root / "inbox",
                        db_path=root / "library" / "test.sqlite3", whisper_enabled=False)
    settings.ensure_dirs()
    conn = open_db(settings.database_path)
    p = create_project(conn, settings.library_path, "Концерт")
    src = media.analysis_video(root / "IMG.mp4")
    ingest_file(conn, settings.library_path, p, src, src.name, "upload", move=True)
    w = Worker(settings)
    while w.run_one(conn):
        pass
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return settings.library_path


@pytest.fixture
def project(settings, analysed_library, monkeypatch):
    shutil.copytree(analysed_library, settings.library_path, dirs_exist_ok=True)
    settings = settings.model_copy(update={"gemini_api_key": SecretStr("test-key"), "gemini_model": "gemini-3.6-flash"})
    conn = open_db(settings.database_path)
    p = conn.execute("SELECT * FROM projects").fetchone()
    fake = FakeBackend()
    monkeypatch.setattr(scoring, "get_backend", lambda s: fake if s.gemini_api_key.get_secret_value() else None)
    return settings, conn, p, fake


def drain(settings, conn, limit=50):
    """Виконує чергу до кінця, «перемотуючи» затримки опитування batch-ів."""
    w = Worker(settings)
    for _ in range(limit):
        conn.execute("UPDATE jobs SET run_after = NULL")
        if not w.run_one(conn):
            return
    raise AssertionError("черга не спорожніла")


def edit_started(conn):
    return any(k == "edit_project" for k, _ in jobs(conn))


def jobs(conn):
    return [(j["kind"], j["status"]) for j in conn.execute("SELECT * FROM jobs WHERE kind NOT IN"
                                                            " ('prepare_media', 'analyze_media') ORDER BY id")]


def test_scoring_end_to_end_and_cache(project):
    settings, conn, p, fake = project
    request_edit(conn, p["id"])
    drain(settings, conn)
    assert edit_started(conn)
    assert len(fake.batches) == 1 and fake.polls == 1
    req = fake.requests[0]
    assert req["config"]["media_resolution"] == "MEDIA_RESOLUTION_LOW"
    assert req["config"]["response_json_schema"] == scoring.RESPONSE_SCHEMA
    prompt = req["contents"][0]["parts"][0]["text"]
    assert "Смак" in prompt and "MiniDV" in prompt

    cands = scoring.candidates(conn, p["id"], scoring.thresholds.for_style(None, "lofi"))
    gem = conn.execute("SELECT * FROM scores WHERE source = 'gemini'").fetchall()
    assert len(gem) == len(cands) > 0
    assert json.loads(gem[0]["tags"]) == ["натовп"] and gem[0]["vertical_ok"] == 1
    r = conn.execute("SELECT * FROM gemini_requests").fetchone()
    assert r["status"] == "done" and r["event_type"] == "концерт" and r["cost"] > 0
    spent = budget.spent_this_month(conn)
    assert spent == pytest.approx(r["cost"]) and budget.reserved(conn) == 0
    # брак (темрява, розмите) у Gemini не йде
    sent = {tuple(s) for s in json.loads(r["segments"])}
    rejected = {(s["start"], s["end"]) for s in conn.execute("SELECT * FROM segments WHERE reject_reason IS NOT NULL")}
    assert not (sent & rejected)

    # повторний «Монтувати» — нових запитів немає
    conn.execute("UPDATE projects SET status = 'collecting'")
    request_edit(conn, p["id"])
    drain(settings, conn)
    assert len(fake.batches) == 1
    assert budget.spent_this_month(conn) == pytest.approx(spent)


def test_invalid_answer_retried_once_then_local(project):
    settings, conn, p, fake = project
    fake.answer = lambda contents: '{"segments": "зламано"}'
    request_edit(conn, p["id"])
    drain(settings, conn)
    assert len(fake.batches) == 2  # перша спроба + одна повторна
    statuses = [r["status"] for r in conn.execute("SELECT status FROM gemini_requests")]
    assert statuses and set(statuses) == {"invalid"}
    assert conn.execute("SELECT COUNT(*) FROM scores WHERE source = 'gemini'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM scores WHERE source = 'local'").fetchone()[0] > 0
    assert edit_started(conn)
    # навіть новий «Монтувати» не шле ті самі шматки втретє
    request_edit(conn, p["id"])
    drain(settings, conn)
    assert len(fake.batches) == 2


def test_failed_batch_retry_succeeds(project):
    settings, conn, p, fake = project
    fake.batch_state = "JOB_STATE_EXPIRED"
    orig_get = fake.get_batch

    def flaky(name):
        st = orig_get(name)
        fake.batch_state = "JOB_STATE_SUCCEEDED"
        return st
    fake.get_batch = flaky
    request_edit(conn, p["id"])
    drain(settings, conn)
    assert len(fake.batches) == 2
    assert {r["status"] for r in conn.execute("SELECT status FROM gemini_requests")} == {"failed", "done"}
    assert conn.execute("SELECT COUNT(*) FROM scores WHERE source = 'gemini'").fetchone()[0] > 0


def test_polls_until_batch_done(project):
    settings, conn, p, fake = project
    fake.polls_until_done = 3
    request_edit(conn, p["id"])
    drain(settings, conn)
    assert fake.polls == 3 and edit_started(conn)


def test_confirmation_flow(project):
    from fastapi.testclient import TestClient

    from server.app import create_app

    settings, conn, p, fake = project
    settings = settings.model_copy(update={"confirm_threshold_usd": 0.0})
    request_edit(conn, p["id"])
    drain(settings, conn)
    assert ("score_project", "awaiting_confirm") in jobs(conn) and not fake.batches
    with TestClient(create_app(settings, start_worker=False)) as client:
        pending = client.get("/api/confirmations").json()
        assert pending[0]["forecast_usd"] > 0 and pending[0]["project"] == "Концерт"
        assert client.post(f"/api/jobs/{pending[0]['job_id']}/confirm", json={"approved": True}).status_code == 200
        assert client.post(f"/api/jobs/{pending[0]['job_id']}/confirm", json={"approved": True}).status_code == 404
    drain(settings, conn)
    assert len(fake.batches) == 1 and edit_started(conn)


def test_confirmation_rejected_goes_local(project):
    settings, conn, p, fake = project
    settings = settings.model_copy(update={"confirm_threshold_usd": 0.0})
    request_edit(conn, p["id"])
    drain(settings, conn)
    job = conn.execute("SELECT id FROM jobs WHERE status = 'awaiting_confirm'").fetchone()["id"]
    queue.resolve_confirm(conn, job, False)
    drain(settings, conn)
    assert not fake.batches and edit_started(conn)
    assert conn.execute("SELECT COUNT(*) FROM scores WHERE source = 'local'").fetchone()[0] > 0


def test_budget_exhausted_continues_locally(project):
    settings, conn, p, fake = project
    conn.execute("INSERT INTO spend(model, purpose, cost_usd) VALUES ('x', 'раніше', 10.0)")
    request_edit(conn, p["id"])
    drain(settings, conn)
    assert not fake.batches
    assert edit_started(conn)
    assert conn.execute("SELECT COUNT(*) FROM scores WHERE source = 'local'").fetchone()[0] > 0


def test_daily_limit(project):
    settings, conn, p, fake = project
    settings = settings.model_copy(update={"daily_request_limit": 0})
    request_edit(conn, p["id"])
    drain(settings, conn)
    assert not fake.batches and edit_started(conn)


def test_no_api_key_local_only(project):
    settings, conn, p, fake = project
    settings = settings.model_copy(update={"gemini_api_key": SecretStr("")})
    request_edit(conn, p["id"])
    drain(settings, conn)
    assert not fake.batches and edit_started(conn)


def test_reservation_blocks_parallel_overspend(project, tmp_path):
    """Два проєкти в черзі, бюджету вистачає лише на один: другий не надсилається, поки перший не оплачено."""
    from tests import media

    settings, conn, p, fake = project
    fake.polls_until_done = 10**6  # перший batch «висить»
    request_edit(conn, p["id"])
    w = Worker(settings)
    while w.run_one(conn):
        pass
    est = budget.reserved(conn)
    assert est > 0 and len(fake.batches) == 1

    p2 = create_project(conn, settings.library_path, "Другий")
    src = media.analysis_video(tmp_path / "B.mp4")
    ingest_file(conn, settings.library_path, p2, src, src.name, "upload", move=True)
    settings2 = settings.model_copy(update={"monthly_budget_usd": est * 1.5})
    w2 = Worker(settings2)
    while w2.run_one(conn):
        pass
    request_edit(conn, p2["id"])
    w2.run_one(conn)
    assert len(fake.batches) == 1  # другий проєкт пішов на локальні метрики
    assert conn.execute("SELECT status FROM projects WHERE id = ?", (p2["id"],)).fetchone()[0] == "editing"


def test_long_video_split_into_windows(project):
    settings, conn, p, fake = project
    settings = settings.model_copy(update={"gemini_window_seconds": 20})
    request_edit(conn, p["id"])
    drain(settings, conn)
    assert len(fake.requests) >= 2
    files = conn.execute("SELECT * FROM gemini_files ORDER BY win_start").fetchall()
    assert len(files) >= 2 and all((settings.library_path / f["local_path"]).exists() for f in files)
    # час у промпті — від початку вікна, а оцінка лягає на абсолютний час шматка
    prompt = fake.requests[-1]["contents"][0]["parts"][0]["text"]
    assert "0: 00:0" in prompt
    assert conn.execute("SELECT MAX(start) FROM scores WHERE source = 'gemini'").fetchone()[0] >= 15


def test_taste_examples_uploaded_once(project):
    import cv2
    import numpy as np

    settings, conn, p, fake = project
    for sub in ("take", "skip"):
        d = settings.library_path / "taste" / sub
        d.mkdir(parents=True)
        cv2.imwrite(str(d / "a.jpg"), np.full((32, 32, 3), 128 if sub == "take" else 20, np.uint8))
    request_edit(conn, p["id"])
    drain(settings, conn)
    imgs = [u for u in fake.uploads if u[1] == "image/jpeg"]
    assert len(imgs) == 2
    parts = fake.requests[0]["contents"][0]["parts"]
    assert any(pt.get("text") == "Приклад БЕРУ #1:" for pt in parts)
    assert any(pt.get("text") == "Приклад НЕ БЕРУ #2:" for pt in parts)
    # приклади змінюють версію смаку → новий ключ стилю; але файли не вантажаться повторно
    conn.execute("UPDATE projects SET status = 'collecting'")
    request_edit(conn, p["id"])
    drain(settings, conn)
    assert len([u for u in fake.uploads if u[1] == "image/jpeg"]) == 2
