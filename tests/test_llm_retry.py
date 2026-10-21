"""Gemini перевантажений: повтори 2/5/15 с, запасна модель, зрозуміла помилка."""

from types import SimpleNamespace

import pytest

from analysis import llm
from analysis.gemini_client import Result
from pydantic import SecretStr

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
BUSY = '503 UNAVAILABLE. {"error": {"code": 503, "message": "The model is overloaded."}}'


class Backend:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def generate(self, model, contents, config):
        self.calls.append(model)
        a = self.answers.pop(0)
        return Result(error=a) if a.startswith("503") or a.startswith("400") else Result(text=a)


@pytest.fixture
def env(settings, conn, monkeypatch):
    settings = settings.model_copy(update={"gemini_api_key": SecretStr("k")})
    sleeps = []
    monkeypatch.setattr(llm, "_sleep", sleeps.append)
    return SimpleNamespace(settings=settings, conn=conn, sleeps=sleeps, monkeypatch=monkeypatch)


def _use(env, backend):
    env.monkeypatch.setattr("analysis.scoring.get_backend", lambda s: backend)


def test_retries_then_falls_back_to_lite(env):
    b = Backend([BUSY, BUSY, '{"ok": true}'])
    _use(env, b)
    assert llm.ask_json(env.conn, env.settings, "x", SCHEMA, "тест") == {"ok": True}
    assert b.calls == ["gemini-flash-latest", "gemini-flash-latest", "gemini-flash-lite-latest"]
    assert env.sleeps == [2, 5]


def test_all_busy_gives_clear_error(env):
    b = Backend([BUSY] * 4)
    _use(env, b)
    with pytest.raises(llm.LLMUnavailable) as e:
        llm.ask_json(env.conn, env.settings, "x", SCHEMA, "тест")
    assert e.value.overloaded and "перевантажений" in str(e.value) and "{" not in str(e.value)
    assert env.sleeps == [2, 5, 15] and len(b.calls) == 4


def test_other_errors_not_retried_with_delay(env):
    b = Backend(["400 INVALID_ARGUMENT", "400 INVALID_ARGUMENT"])
    _use(env, b)
    with pytest.raises(llm.LLMUnavailable) as e:
        llm.ask_json(env.conn, env.settings, "x", SCHEMA, "тест")
    assert not e.value.overloaded and env.sleeps == []


def test_is_overloaded():
    assert llm.is_overloaded(BUSY) and llm.is_overloaded("429 RESOURCE_EXHAUSTED")
    assert not llm.is_overloaded("невалідна відповідь: 15000 токенів")


# ---------- правка в черзі, поки Gemini перевантажений ----------

def test_overloaded_request_waits_then_parsed_with_push(settings, monkeypatch):
    import json

    from fastapi.testclient import TestClient

    from analysis import scoring
    from core import notify
    from core.db import open_db
    from server.app import create_app
    from server.requests import retry_waiting

    settings = settings.model_copy(update={"gemini_api_key": SecretStr("k")})
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    pushes = []
    monkeypatch.setattr(notify, "_sender", lambda s, payload: pushes.append(json.loads(payload)))
    good = json.dumps({"summary": "Більше світла й руху", "tag_weights": {"світло": 0.5}}, ensure_ascii=False)
    b = Backend([BUSY] * 4)
    monkeypatch.setattr(scoring, "get_backend", lambda s: b)
    with TestClient(create_app(settings, start_worker=False)) as c:
        conn = open_db(settings.database_path)
        notify.subscribe(conn, {"endpoint": "https://push/x", "keys": {"p256dh": "a", "auth": "b"}})
        pid = c.post("/api/projects", json={"name": "Концерт"}).json()["id"]
        r = c.post(f"/api/projects/{pid}/requests", json={"text": "Більше вайбу літнього вечора"})
        assert r.status_code == 200 and r.json()["waiting"] and "перевантажений" in r.json()["message"]
        rid = r.json()["id"]
        assert c.post(f"/api/requests/{rid}/apply", json={"scope": "here"}).status_code == 409
        # фон: ще перевантажений — лишається в черзі, без push
        b.answers = [BUSY, BUSY]
        assert retry_waiting(conn, settings) == [] and pushes == []
        # Gemini ожив — розібрано, push, можна підтвердити
        b.answers = [good]
        assert retry_waiting(conn, settings) == [rid]
        assert pushes[0]["title"] == "Правку зрозумів: Концерт" and pushes[0]["url"] == f"/project/{pid}"
        got = c.get(f"/api/requests/{rid}").json()
        assert got["state"] == "ready" and got["summary"] == "Більше світла й руху" and got["question"]
        assert c.post(f"/api/requests/{rid}/apply", json={"scope": "here"}).status_code == 200


def test_waiting_request_expires_after_hour(settings, monkeypatch):
    import json

    from fastapi.testclient import TestClient

    from analysis import scoring
    from core import notify
    from core.db import open_db
    from server.app import create_app
    from server.requests import retry_waiting

    settings = settings.model_copy(update={"gemini_api_key": SecretStr("k")})
    monkeypatch.setattr(llm, "_sleep", lambda s: None)
    pushes = []
    monkeypatch.setattr(notify, "_sender", lambda s, payload: pushes.append(json.loads(payload)))
    b = Backend([BUSY] * 10)
    monkeypatch.setattr(scoring, "get_backend", lambda s: b)
    with TestClient(create_app(settings, start_worker=False)) as c:
        conn = open_db(settings.database_path)
        notify.subscribe(conn, {"endpoint": "https://push/x", "keys": {"p256dh": "a", "auth": "b"}})
        pid = c.post("/api/projects", json={"name": "П"}).json()["id"]
        rid = c.post(f"/api/projects/{pid}/requests", json={"text": "щось дуже вільне"}).json()["id"]
        conn.execute("UPDATE edit_requests SET retry_until = '2000-01-01T00:00:00Z' WHERE id = ?", (rid,))
        assert retry_waiting(conn, settings) == []
        assert c.get(f"/api/requests/{rid}").json()["state"] == "failed"
        assert pushes and pushes[0]["title"].startswith("Правка не вдалась")
        # скасована правка в фоні не повторюється
        rid2 = c.post(f"/api/projects/{pid}/requests", json={"text": "ще одна вільна"}).json()["id"]
        c.post(f"/api/requests/{rid2}/reject")
        b.answers = []
        assert retry_waiting(conn, settings) == []
