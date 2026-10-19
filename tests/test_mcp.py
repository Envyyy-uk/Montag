"""Спринт 13: MCP — інструменти, аркуш кадрів, витрати й видалення у два кроки."""

import asyncio
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client

from core.db import open_db
from edit import cleanup
from mcp_server.api import Api
from mcp_server.codes import CodeError, Codes
from mcp_server.server import build_server
from server.app import create_app
from tests import media
from worker import queue


@pytest.fixture
def env(settings):
    settings.ensure_dirs()
    open_db(settings.database_path).close()
    app = create_app(settings, start_worker=False)
    api = Api("http://127.0.0.1", transport=httpx.ASGITransport(app=app))
    return {"settings": settings, "app": app, "server": build_server(api, 2.0), "conn": open_db(settings.database_path)}


def call(env, name, args=None):
    async def go():
        async with Client(env["server"]) as c:
            return await c.call_tool(name, args or {})
    return asyncio.run(go())


def text(res) -> str:
    return "\n".join(getattr(c, "text", "") for c in res.content)


def data(res):
    return res.structured_content.get("result", res.structured_content) if res.structured_content else json.loads(text(res))


@pytest.fixture
def clip(env, tmp_path):
    """Проєкт з готовим кліпом v1: mp4, превʼю, таймлайн і папка DaVinci з жорстким посиланням на оригінал."""
    s, conn = env["settings"], env["conn"]
    with TestClient(env["app"]) as c:
        pid = c.post("/api/projects", json={"name": "Концерт MCP"}).json()["id"]
    slug = conn.execute("SELECT slug FROM projects WHERE id = ?", (pid,)).fetchone()[0]
    lib, base = s.library_path, s.library_path / slug
    orig = media.sdr_video(base / "originals" / "a.mp4", w=320, h=180, dur=1)
    final = media.sdr_video(base / "exports" / f"{slug}_clip_v001.mp4", w=320, h=180, dur=2)
    preview = base / "exports" / f"{slug}_clip_v001_720p.mp4"
    preview.write_bytes(final.read_bytes())
    ed = base / "edits" / "clip_v001"
    ed.mkdir(parents=True)
    (ed / "timeline.json").write_text("{}")
    (ed / "mix.wav").write_bytes(b"RIFF")
    dv = base / "exports" / f"{slug}_clip_v001_davinci"
    (dv / "media" / "originals").mkdir(parents=True)
    os.link(orig, dv / "media" / "originals" / "1_a.mp4")
    (dv / f"{slug}_clip_v001.fcpxml").write_text("<fcpxml/>")
    summary = {"duration": 2.0, "cuts": 5, "inserts": 1, "style": "MiniDV", "level": 2}
    eid = conn.execute(
        "INSERT INTO edits(project_id, kind, version, timeline_path, audio_path, preview_path, export_path,"
        " davinci_path, summary) VALUES (?, 'clip', 1, ?, ?, ?, ?, ?, ?)",
        (pid, str((ed / "timeline.json").relative_to(lib)), str((ed / "mix.wav").relative_to(lib)),
         str(preview.relative_to(lib)), str(final.relative_to(lib)),
         str((dv / f"{slug}_clip_v001.fcpxml").relative_to(lib)), json.dumps(summary))).lastrowid
    conn.commit()
    return {"pid": pid, "eid": eid, "orig": orig, "final": final, "ed": ed, "dv": dv, "base": base}


def test_tools_and_status(env):
    async def names():
        async with Client(env["server"]) as c:
            return {t.name for t in (await c.list_tools()).tools}
    got = asyncio.run(names())
    assert {"status", "show_clip", "request_change", "apply_change", "prepare_spend", "confirm_spend",
            "prepare_delete", "delete_renders", "set_style", "make_verticals", "export_davinci"} <= got
    st = data(call(env, "status"))
    assert st["бюджет"]["ліміт"] == "$10.00" and st["черга"] == []
    pid = data(call(env, "create_project", {"name": "Подорож"}))["id"]
    assert [p["name"] for p in data(call(env, "list_projects"))] == ["Подорож"]
    assert data(call(env, "project", {"project_id": pid}))["name"] == "Подорож"


def test_errors_are_readable(env):
    res = call(env, "project", {"project_id": 999})
    assert res.is_error and "не знайдено" in text(res)
    dead = build_server(Api("http://127.0.0.1:9"), 2.0)
    res = call({"server": dead}, "status")
    assert res.is_error and "не відповідає" in text(res)


def test_show_clip_returns_contact_sheet(env, clip):
    res = call(env, "show_clip", {"project_id": clip["pid"]})
    assert not res.is_error
    imgs = [c for c in res.content if c.type == "image"]
    assert len(imgs) == 1 and imgs[0].mime_type == "image/jpeg"
    assert "5 склейок" in text(res)
    sheet = clip["final"].with_name(clip["final"].stem + "_sheet.jpg")
    assert sheet.exists()  # закешовано поруч із відео
    res = call(env, "show_reels", {"project_id": clip["pid"]})
    assert res.is_error and "Вертикалей ще немає" in text(res)


def test_delete_two_steps_keeps_originals(env, clip):
    before = clip["orig"].read_bytes()
    res = call(env, "prepare_delete", {"edit_id": clip["eid"]})
    msg = text(res)
    assert "Оригінальні відео лишаються" in msg and "clip_v001_davinci" in msg and "originals/a.mp4" not in msg
    code = msg.split('code="')[1].split('"')[0]
    bad = call(env, "delete_renders", {"edit_id": clip["eid"], "code": "000000" if code != "000000" else "111111"})
    assert bad.is_error and clip["final"].exists()
    ok = call(env, "delete_renders", {"edit_id": clip["eid"], "code": code})
    assert not ok.is_error
    assert not clip["final"].exists() and not clip["ed"].exists() and not clip["dv"].exists()
    assert clip["orig"].read_bytes() == before and clip["orig"].stat().st_nlink == 1
    assert env["conn"].execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 0
    again = call(env, "delete_renders", {"edit_id": clip["eid"], "code": code})
    assert again.is_error  # код одноразовий


def test_delete_refused_while_busy_or_outside(env, clip):
    conn, s = env["conn"], env["settings"]
    queue.enqueue(conn, "export_davinci", {"edit_id": clip["eid"]}, project_id=clip["pid"])
    with pytest.raises(cleanup.CleanupError, match="спробуй пізніше"):
        cleanup.delete(conn, s, clip["eid"])
    conn.execute("UPDATE jobs SET status = 'done'")
    rel = str(clip["orig"].relative_to(s.library_path))
    conn.execute("UPDATE edits SET export_path = ? WHERE id = ?", (rel, clip["eid"]))
    with pytest.raises(cleanup.CleanupError, match="поза папками"):
        cleanup.plan(conn, s, clip["eid"])
    assert clip["orig"].exists()


def _awaiting(conn, forecast):
    jid = queue.enqueue(conn, "score_project", {"project_id": None}, dedupe=False)
    queue.await_confirm(conn, jid, {"forecast_usd": forecast, "requests": 3})
    return jid


def test_spend_two_steps(env):
    conn = env["conn"]
    jid = _awaiting(conn, 0.42)
    msg = text(call(env, "prepare_spend", {"job_id": jid}))
    assert "$0.42" in msg and "дочекайся явної згоди" in msg
    code = msg.split('code="')[1].split('"')[0]
    assert call(env, "confirm_spend", {"job_id": jid, "code": "x"}).is_error
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == "awaiting_confirm"
    assert not call(env, "confirm_spend", {"job_id": jid, "code": code}).is_error
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()[0] == "queued"


def test_spend_over_limit_or_changed(env):
    conn = env["conn"]
    big = _awaiting(conn, 3.5)
    res = call(env, "prepare_spend", {"job_id": big})
    assert res.is_error and "застосунку" in text(res)
    jid = _awaiting(conn, 0.5)
    code = text(call(env, "prepare_spend", {"job_id": jid})).split('code="')[1].split('"')[0]
    queue.await_confirm(conn, jid, {"forecast_usd": 1.5, "requests": 9})  # прогноз виріс після кроку 1
    res = call(env, "confirm_spend", {"job_id": jid, "code": code})
    assert res.is_error and "Прогноз змінився" in text(res)
    # відмова — без коду
    assert not call(env, "confirm_spend", {"job_id": jid, "approved": False}).is_error
    assert conn.execute("SELECT status FROM jobs WHERE id = ?", (jid,)).fetchone()[0] != "awaiting_confirm"


def test_rules_from_claude_are_marked(env, clip):
    r = data(call(env, "request_change", {"project_id": clip["pid"], "text": "без вставок"}))
    assert r["питання"] == "Тільки тут чи назавжди?"
    assert not call(env, "apply_change", {"request_id": r["request_id"], "scope": "always"}).is_error
    rules = data(call(env, "taste"))["правила"]
    assert len(rules) == 1 and rules[0]["origin"] == "mcp"
    with TestClient(env["app"]) as c:  # з PWA — без позначки
        rid = c.post(f"/api/projects/{clip['pid']}/requests", json={"text": "швидше"}).json()["id"]
        c.post(f"/api/requests/{rid}/apply", json={"scope": "always"})
        assert [x["origin"] for x in c.get("/api/taste").json()["rules"]] == ["mcp", "pwa"]
    assert not call(env, "remove_taste_rule", {"rule_id": rules[0]["id"]}).is_error


def test_set_style_with_level(env, clip):
    res = data(call(env, "set_style", {"project_id": clip["pid"], "style_id": "vhs-90s", "level": 3}))
    assert res["job"]["job_id"]
    ov = data(call(env, "project", {"project_id": clip["pid"]}))
    assert ov["style"]["id"] == "vhs-90s" and ov["clip_params"]["style_level"] == 3


def test_codes_expire_and_lock():
    now = [0.0]
    c = Codes(clock=lambda: now[0])
    code = c.issue("spend", 1, 0.3)
    with pytest.raises(CodeError):
        c.take(code, "spend", 2)  # інша ціль
    assert c.take(code, "spend", 1).amount == 0.3
    code = c.issue("delete", 5)
    now[0] = 601
    with pytest.raises(CodeError):
        c.take(code, "delete", 5)
    code = c.issue("delete", 6)
    for _ in range(5):
        with pytest.raises(CodeError):
            c.take("nope", "delete", 6)
    with pytest.raises(CodeError):
        c.take(code, "delete", 6)  # після 5 хибних спроб усі коди анульовано
