"""API для веб-інтерфейсу: огляд проєкту, обкладинка, енергія треку, ліміт, push, роздача PWA."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core import budget, notify
from core.config import OVERRIDABLE, Settings, with_overrides
from server.deps import db, settings_dep

router = APIRouter(prefix="/api", tags=["ui"])
WEB_DIST = Path(__file__).resolve().parent.parent / "web" / "dist"


class BudgetSettings(BaseModel):
    monthly_budget_usd: float | None = Field(None, ge=0, le=1000)
    confirm_threshold_usd: float | None = Field(None, ge=0, le=1000)
    daily_request_limit: int | None = Field(None, ge=0, le=100000)


class PushSub(BaseModel):
    endpoint: str
    keys: dict


@router.get("/projects/{project_id}/overview")
def overview(project_id: int, conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)) -> dict:
    """Усе, що показує екран проєкту, одним запитом."""
    from edit.build import project_params
    from edit.verticals import vertical_params
    from render import style_store

    p = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if p is None:
        raise HTTPException(404, "Проєкт не знайдено")
    clip_params = project_params(p, conn)
    style, level = style_store.resolve_for_project(conn, settings, p, clip_params.get("style_level"))
    counts = {r["kind"]: r["n"] for r in conn.execute(
        "SELECT kind, COUNT(*) n FROM media WHERE project_id = ? GROUP BY kind", (project_id,))}
    shot = conn.execute("SELECT MIN(shot_at) FROM media WHERE project_id = ?", (project_id,)).fetchone()[0]

    from server.edits import main_edit

    def latest(kind):
        r = main_edit(conn, project_id, kind)  # основна версія (закріплена або найновіша)
        return None if r is None else {"id": r["id"], "version": r["version"], "created_at": r["created_at"],
                                       "summary": json.loads(r["summary"] or "{}"), "davinci": bool(r["davinci_path"]),
                                       "aspect": json.loads(r["params"] or "{}").get("aspect")
                                       or ("9:16" if kind == "vertical" else "16:9")}

    jobs = [dict(r) for r in conn.execute(
        "SELECT id, kind, status, progress, payload FROM jobs WHERE project_id = ? AND status IN"
        " ('queued', 'running', 'awaiting_confirm') ORDER BY id", (project_id,))]
    for j in jobs:
        j["confirm"] = json.loads(j.pop("payload")).get("confirm")
    return {
        "id": p["id"], "name": p["name"], "status": p["status"], "created_at": p["created_at"], "shot_at": shot,
        "videos": counts.get("video", 0), "tracks": counts.get("track", 0),
        "style": {"id": style.id, "name": style.name, "level": level},
        "style_auto": json.loads(p["settings"] or "{}").get("style_auto"),
        "clip": latest("clip"), "vertical": latest("vertical"),
        "clip_params": clip_params, "vertical_params": vertical_params(p, conn), "jobs": jobs,
    }


@router.get("/projects/{project_id}/cover.jpg")
def cover(project_id: int, conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)):
    """Кадр з відео проєкту для розмитого фону екрана."""
    p = conn.execute("SELECT slug FROM projects WHERE id = ?", (project_id,)).fetchone()
    m = conn.execute("SELECT * FROM media WHERE project_id = ? AND kind = 'video' AND gemini_proxy_path IS NOT NULL"
                     " ORDER BY id LIMIT 1", (project_id,)).fetchone()
    if p is None or m is None:
        raise HTTPException(404, "Обкладинки ще немає")
    out = settings.library_path / p["slug"] / "analysis" / "cover.jpg"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        t = (m["duration"] or 2) * 0.4  # не перший кадр: на початку часто темрява чи кишеня
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t}", "-i", str(settings.library_path / m["gemini_proxy_path"]),
                        "-frames:v", "1", "-vf", "scale=480:-2", str(out)], timeout=60)
    if not out.exists():
        raise HTTPException(404, "Обкладинки ще немає")
    return FileResponse(out, media_type="image/jpeg")


@router.get("/projects/{project_id}/track")
def track_energy(project_id: int, conn: sqlite3.Connection = Depends(db),
                 settings: Settings = Depends(settings_dep)) -> dict:
    """Енергія треку по тактах + поточне вікно — для смужки з перетягуваними межами."""
    from analysis.track import TrackAnalysis

    p = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    from edit.build import project_tracks
    t = next(iter(project_tracks(conn, project_id)), None)
    if p is None or t is None:
        raise HTTPException(404, "Треку немає")
    ta = TrackAnalysis.load(settings.library_path / p["slug"] / "analysis" / f"{t['id']}_track.json")
    edit = conn.execute("SELECT timeline_path FROM edits WHERE project_id = ? AND kind = 'clip'"
                        " ORDER BY version DESC LIMIT 1", (project_id,)).fetchone()
    window = None
    clip = json.loads(p["settings"] or "{}").get("clip", {})
    if clip.get("track_start") is not None and clip.get("track_end") is not None:
        window = {"start": clip["track_start"], "end": clip["track_end"]}  # те, що вибрав ти
    elif edit:
        tl = json.loads((settings.library_path / edit["timeline_path"]).read_text(encoding="utf-8"))
        window = {"start": tl["track_in"], "end": tl["track_out"]}
    bars = [{"t": ta.bar_start_time(k), "energy": e, "level": ta.bar_level[k]} for k, e in enumerate(ta.bar_energy)]
    return {"duration": ta.duration, "tempo": ta.tempo, "bars": bars, "window": window,
            "drops": [ta.bar_start_time(k) for k in ta.drops],
            "choruses": [[ta.bar_start_time(a), ta.bar_start_time(min(b, len(ta.downbeats) - 1))] for a, b in ta.choruses]}


@router.get("/projects/{project_id}/track.audio")
def track_audio(project_id: int, conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)):
    """Сам трек — щоб прослухати вибраний шматок перед перемонтажем."""
    import mimetypes

    from edit.build import project_tracks
    t = next(iter(project_tracks(conn, project_id, analyzed=False)), None)
    if t is None:
        raise HTTPException(404, "Треку немає")
    path = settings.library_path / t["original_path"]
    if not path.exists():
        raise HTTPException(404, "Файл треку зник")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "audio/mpeg")


@router.put("/budget/settings")
def put_budget_settings(body: BudgetSettings, conn: sqlite3.Connection = Depends(db),
                        settings: Settings = Depends(settings_dep)) -> dict:
    row = conn.execute("SELECT value FROM settings WHERE key = 'overrides'").fetchone()
    cur = json.loads(row["value"]) if row else {}
    cur.update({k: v for k, v in body.model_dump(exclude_none=True).items() if k in OVERRIDABLE})
    conn.execute("INSERT INTO settings(key, value) VALUES ('overrides', ?) ON CONFLICT(key) DO UPDATE"
                 " SET value = excluded.value", (json.dumps(cur),))
    eff = with_overrides(settings, conn)
    return budget.state(conn, eff.monthly_budget_usd, eff.daily_request_limit).as_dict() | {
        "confirm_threshold": eff.confirm_threshold_usd}


@router.get("/push/key")
def push_key(settings: Settings = Depends(settings_dep)) -> dict:
    return {"key": notify.public_key(settings)}


@router.post("/push/subscribe")
def push_subscribe(body: PushSub, conn: sqlite3.Connection = Depends(db)) -> dict:
    notify.subscribe(conn, body.model_dump())
    return {"ok": True}


@router.post("/push/test")
def push_test(conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)) -> dict:
    return {"sent": notify.send(conn, settings, "Автомонтаж", "Сповіщення працюють ✓", "/")}


@router.post("/speedtest")
async def speedtest(request: Request) -> dict:
    """Телефон шле до 4 МБ і сам міряє час — так видно реальну швидкість завантаження на ноут."""
    n = 0
    async for chunk in request.stream():
        n += len(chunk)
        if n > 4 * 1024 * 1024:
            raise HTTPException(413, "Не більше 4 МБ")
    return {"bytes": n}


@router.get("/upload-route")
def upload_route(request: Request, mbps: float | None = None, project_id: int | None = None,
                 conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)) -> dict:
    """Вдома (пряме з'єднання через домашню мережу і достатня швидкість) — напряму на ноут, інакше — Google Drive."""
    from core import tailscale
    from server.auth import client_ip, is_remote
    from sources import gdrive

    drive_url = None
    if project_id is not None:
        folders = json.loads((conn.execute("SELECT value FROM settings WHERE key = 'drive_projects'").fetchone()
                              or {"value": "{}"})["value"])
        fid = folders.get(str(project_id), {}).get("id")
        drive_url = f"https://drive.google.com/drive/folders/{fid}" if fid else None
    if not is_remote(request):
        home, reason = True, "телефон у тій самій мережі, що й ноутбук"
    else:
        peer = tailscale.peer_for(client_ip(request))
        if peer is None:
            home, reason = False, "не вдалося визначити з'єднання"
        elif not peer.direct:
            home, reason = False, "з'єднання через ретранслятор Tailscale (поза домом)"
        elif not peer.lan:
            home, reason = False, "пряме з'єднання, але не через домашню мережу"
        else:
            home, reason = True, "пряме з'єднання через домашню мережу"
    if home and mbps is not None and mbps < settings.upload_direct_min_mbps:
        home, reason = False, f"швидкість {mbps:.0f} Мбіт/с — нижче порогу {settings.upload_direct_min_mbps:.0f}"
    if home or not gdrive.configured(settings):
        label = "Вдома: напряму на ноут" if home else "Напряму на ноут (Google Drive не підключено)"
        return {"route": "direct", "label": label, "reason": reason, "drive_url": drive_url}
    return {"route": "drive", "label": "Через Google Drive", "reason": reason, "drive_url": drive_url}


def mount_web(app) -> None:
    """Роздає зібраний PWA (web/dist); невідомі шляхи — index.html (маршрутизація в браузері)."""
    if not WEB_DIST.exists():
        return

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        if path.startswith("api/"):
            raise HTTPException(404)
        f = (WEB_DIST / path).resolve()
        if path and f.is_file() and WEB_DIST.resolve() in f.parents:
            headers = {"Cache-Control": "no-cache"} if f.name in ("sw.js", "manifest.webmanifest") else None
            return FileResponse(f, headers=headers)
        return FileResponse(WEB_DIST / "index.html", headers={"Cache-Control": "no-cache"})
