import json
import uuid

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from analysis.gemini_client import Result
from core.db import open_db
from core.projects import create_project
from edit import changes as ch
from edit import style_auto
from render import style_gen, style_store
from server.app import create_app


# ---------- локальний розбір ----------

@pytest.mark.parametrize("text,expect", [
    ("більше натовпу", {"tag_weights": {"натовп": 0.4}, "inserts": True, "max_inserts": 4}),
    ("Менше сцени і світла", {"tag_weights": {"сцена": -0.4, "світло": -0.4}}),
    ("зроби динамічніше", {"rhythm": {"low": 2, "mid": 1, "high": 1}}),
    ("спокійніше, без вставок", {"rhythm": {"low": 8, "mid": 4, "high": 2}, "inserts": False}),
    ("зроби 45 секунд", {"length": 45.0}),
    ("на 1,5 хв", {"length": 90.0}),
    ("візьми трек з 1:20 до 2:10", {"track_start": 80.0, "track_end": 130.0}),
    ("без переходів", {"transitions": False}),
    ("зроби в стилі vhs", {"style_id": "vhs-90s"}),
    ("більше зерна", {"style_level_delta": 1}),
    ("вертикалі без музики", {"vertical_audio": "none"}),
    ("5 вертикалей по 18 с", {"vertical_count": 5, "vertical_length": 18.0}),
])
def test_local_parse(text, expect):
    got = ch.local_parse(text)
    assert got is not None and got["summary"]
    for k, v in expect.items():
        assert got[k] == v, (k, got)


def test_local_parse_unknown():
    assert ch.local_parse("зроби як у тому кліпі з минулого літа, але сумніше") is None


def test_merge_clip_additive_and_exclude():
    base = {"tag_weights": {"натовп": 0.4}, "beats_per_level": {}, "disabled_transitions": ["zoom"], "excluded": []}
    clips = [{"start": 0, "end": 2, "kind": "beat", "media_id": 3, "src_in": 5.0},
             {"start": 2, "end": 4, "kind": "insert", "media_id": 4, "src_in": 1.0}]
    p = ch.merge_clip(base, {"tag_weights": {"натовп": 0.4, "сцена": -0.4}, "rhythm": {"high": 2},
                             "enable_transitions": ["zoom"], "disable_transitions": ["glitch"],
                             "exclude_at": [1.0, 3.0]}, clips)
    assert p["tag_weights"] == {"натовп": 0.8, "сцена": -0.4}
    assert p["beats_per_level"] == {"high": 2}
    assert p["disabled_transitions"] == ["glitch"]
    assert p["excluded"] == [[3, 5.0]]  # вставку не виключаємо через таймлайн
    assert base["tag_weights"] == {"натовп": 0.4}  # оригінал не змінено


# ---------- Gemini для незрозумілих правок ----------

class FakeLLM:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def generate(self, model, contents, config):
        self.calls += 1
        assert config["response_json_schema"]["required"] == ["summary"]
        return Result(text=self.text, usage={"prompt_token_count": 1200, "candidates_token_count": 80},
                      model_version="gemini-3.6-flash")


@pytest.fixture
def client_and_conn(settings, monkeypatch):
    settings = settings.model_copy(update={"gemini_api_key": SecretStr("k")})
    with TestClient(create_app(settings, start_worker=False)) as client:
        yield client, open_db(settings.database_path), settings


def test_unknown_phrase_goes_to_gemini_and_is_billed(client_and_conn, monkeypatch):
    from analysis import scoring

    client, conn, settings = client_and_conn
    fake = FakeLLM(json.dumps({"summary": "Сумніше: менше яскравих кадрів", "tag_weights": {"світло": -0.5},
                               "rule_text": "менше яскравих кадрів"}, ensure_ascii=False))
    monkeypatch.setattr(scoring, "get_backend", lambda s: fake)
    pid = client.post("/api/projects", json={"name": "П"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/requests", json={"text": "зроби сумніше, як восени"}).json()
    assert r["source"] == "gemini" and r["question"] == "Тільки тут чи назавжди?"
    assert r["changes"]["tag_weights"] == {"світло": -0.5}
    assert conn.execute("SELECT purpose FROM spend").fetchone()[0] == "правка словами"
    # «більше натовпу» — локально, Gemini не викликається
    client.post(f"/api/projects/{pid}/requests", json={"text": "більше натовпу"})
    assert fake.calls == 1


def test_invalid_gemini_answer_retried_then_422(client_and_conn, monkeypatch):
    from analysis import scoring

    client, conn, settings = client_and_conn
    fake = FakeLLM('{"nope": 1}')
    monkeypatch.setattr(scoring, "get_backend", lambda s: fake)
    pid = client.post("/api/projects", json={"name": "П"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/requests", json={"text": "щось дивне незрозуміле"})
    assert r.status_code == 422 and fake.calls == 2


def test_no_key_unknown_phrase_422(settings):
    with TestClient(create_app(settings, start_worker=False)) as client:
        pid = client.post("/api/projects", json={"name": "П"}).json()["id"]
        assert client.post(f"/api/projects/{pid}/requests", json={"text": "щось дивне"}).status_code == 422


# ---------- правила «назавжди» ----------

def test_always_rule_global_and_taste_md(settings):
    from edit.build import project_params

    settings.taste_path.write_text("# Смак\n\nЛюблю живі моменти.\n", encoding="utf-8")
    with TestClient(create_app(settings, start_worker=False)) as client:
        conn = open_db(settings.database_path)
        a = client.post("/api/projects", json={"name": "А"}).json()["id"]
        b = client.post("/api/projects", json={"name": "Б"}).json()["id"]
        rid = client.post(f"/api/projects/{a}/requests", json={"text": "більше натовпу"}).json()["id"]
        r = client.post(f"/api/requests/{rid}/apply", json={"scope": "always"}).json()
        assert r["rule_id"] and "edit_project" in r["jobs"]
        assert client.post(f"/api/requests/{rid}/apply", json={"scope": "here"}).status_code == 409
        md = settings.taste_path.read_text(encoding="utf-8")
        assert "Люблю живі моменти." in md and "- більше натовпу" in md
        # правило діє і на інший проєкт
        pb = conn.execute("SELECT * FROM projects WHERE id = ?", (b,)).fetchone()
        assert project_params(pb, conn)["tag_weights"]["натовп"] == pytest.approx(0.4)
        taste = client.get("/api/taste").json()
        assert taste["instruction"].startswith("# Смак") and "правила" not in taste["instruction"]
        assert taste["rules"][0]["text"] == "більше натовпу"
        # редагування і вимкнення
        client.put(f"/api/taste/rules/{r['rule_id']}", json={"text": "більше натовпу на концертах"})
        assert "- більше натовпу на концертах" in settings.taste_path.read_text(encoding="utf-8")
        client.put(f"/api/taste/rules/{r['rule_id']}", json={"active": False})
        assert project_params(pb, conn)["tag_weights"] == {}
        assert client.delete(f"/api/taste/rules/{r['rule_id']}").status_code == 200
        assert "- (поки немає)" in settings.taste_path.read_text(encoding="utf-8")
        # редагування інструкції не губить правила
        client.put("/api/taste", json={"instruction": "# Смак\n\nНове."})
        assert "правила:початок" in settings.taste_path.read_text(encoding="utf-8")


def test_rules_block_does_not_reset_score_cache(settings):
    from analysis import scoring

    settings.taste_path.write_text("# Смак\nX\n", encoding="utf-8")
    k1 = scoring.style_key(None, scoring.load_taste(settings), "m")
    conn = open_db(settings.database_path)
    conn.execute("INSERT INTO taste_rules(text, changes) VALUES ('більше натовпу', '{}')")
    ch.sync_taste_md(conn, settings.taste_path)
    assert scoring.style_key(None, scoring.load_taste(settings), "m") == k1


def test_taste_examples_upload(settings):
    with TestClient(create_app(settings, start_worker=False)) as client:
        r = client.post("/api/taste/examples/take", files={"file": ("кадр 1.jpg", b"\xff\xd8\xff", "image/jpeg")})
        assert r.status_code == 200
        assert client.get("/api/taste").json()["examples"] == {"take": 1, "skip": 0}
        assert client.post("/api/taste/examples/take", files={"file": ("x.exe", b"1", "x")}).status_code == 415
        assert client.delete(f"/api/taste/examples/take/{r.json()['name']}").status_code == 200


# ---------- автовибір стилю ----------

def _req(conn, pid, event, conf):
    mid = conn.execute("INSERT INTO media(project_id, kind, original_path, sha256, size_bytes, source)"
                       " VALUES (?, 'video', 'x', ?, 1, 'upload')", (pid, uuid.uuid4().hex)).lastrowid
    conn.execute("INSERT INTO gemini_requests(project_id, media_id, win_start, win_end, style_key, segments, status,"
                 " event_type, event_confidence) VALUES (?, ?, 0, 1, 'k', '[]', 'done', ?, ?)", (pid, mid, event, conf))


def test_style_auto_confident_and_suggestion(settings):
    conn = open_db(settings.database_path)
    a = create_project(conn, settings.library_path, "Концерт")
    for _ in range(3):
        _req(conn, a["id"], "концерт", 0.9)
    found = style_auto.apply(conn, a["id"])
    assert found["confirmed"] and found["style_id"] == "minidv-concert"
    pa = conn.execute("SELECT * FROM projects WHERE id = ?", (a["id"],)).fetchone()
    assert style_store.resolve_for_project(conn, settings, pa)[0].id == "minidv-concert"

    b = create_project(conn, settings.library_path, "Мікс")
    _req(conn, b["id"], "подорож", 0.8)
    _req(conn, b["id"], "концерт", 0.7)
    found = style_auto.apply(conn, b["id"])
    assert not found["confirmed"] and "змішаний" in found["reason"]
    with TestClient(create_app(settings, start_worker=False)) as client:
        r = client.post(f"/api/projects/{b['id']}/style-suggestion", json={"style_id": "vhs-90s"}).json()
        assert r == {"style_id": "vhs-90s", "rerender": True}
    pb = conn.execute("SELECT * FROM projects WHERE id = ?", (b["id"],)).fetchone()
    assert style_store.resolve_for_project(conn, settings, pb)[0].id == "vhs-90s"  # вибір користувача сильніший


# ---------- стилі словами ----------

def test_style_gen_levels_and_clamp():
    s = style_gen.build_style({"name": "Т", "description": "", "look": {"grain": 99, "saturation": 0.5,
                                                                         "stamp": "film", "shadows": [0.5, 0, 0]}},
                              "user-1")
    assert s["look"]["grain"] == 30 and s["look"]["shadows"] == [0.3, 0, 0]
    assert s["look"]["stamp"]["format"] == "film"
    assert s["levels"]["1"]["saturation"] > 0.5 > s["levels"]["3"]["saturation"]
    assert s["levels"]["1"]["grain"] < 30


def test_style_by_words_local_flow(settings):
    from worker.runner import Worker

    with TestClient(create_app(settings, start_worker=False)) as client:
        r = client.post("/api/styles/generate", json={"text": "теплий вицвілий з зерном і датою"})
        assert r.status_code == 202, r.text
        st = r.json()["style"]
        assert r.json()["source"] == "local" and st["look"]["temperature"] < 6500 and st["look"]["grain"] > 0
        conn = open_db(settings.database_path)
        Worker(settings).run_one(conn)  # тестовий рендер 3 рівнів
        assert client.get(f"/api/styles/{st['id']}/test/2").status_code == 200
        assert st["id"] not in {s["id"] for s in client.get("/api/styles").json()}  # ще чернетка
        assert client.post(f"/api/styles/drafts/{st['id']}/save").status_code == 200
        assert st["id"] in {s["id"] for s in client.get("/api/styles").json()}
        assert client.post("/api/styles/generate", json={"text": "щось абстрактне"}).status_code == 422


def test_event_style_mapping_overrides_builtin(settings):
    from fastapi.testclient import TestClient

    from core.db import open_db
    from edit import style_auto
    from server.app import create_app
    with TestClient(create_app(settings, start_worker=False)) as c:
        ev = {e["event"]: e for e in c.get("/api/styles/events").json()}
        assert ev["концерт"]["style_id"] == "minidv-concert" and not ev["концерт"]["mine"]
        r = c.put("/api/styles/events", json={"event": "концерт", "style_id": "vhs-90s"}).json()
        got = {e["event"]: e for e in r}["концерт"]
        assert got["style_id"] == "vhs-90s" and got["mine"] and got["builtin"] == "minidv-concert"
        conn = open_db(settings.database_path)
        assert style_auto.style_for_event("концерт", conn) == "vhs-90s"
        assert style_auto.style_for_event("концерт") == "minidv-concert"
        assert c.put("/api/styles/events", json={"event": "весілля", "style_id": "clean"}).status_code == 404
        assert c.put("/api/styles/events", json={"event": "концерт", "style_id": "nope"}).status_code == 404
        r = c.put("/api/styles/events", json={"event": "концерт", "style_id": None}).json()
        assert {e["event"]: e for e in r}["концерт"]["style_id"] == "minidv-concert"
