"""API монтажу: версії кліпу, превʼю, параметри, перемонтаж без нових запитів у Gemini."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core.config import Settings
from core.projects import ProjectError, get_project
from edit.build import DEFAULT_PARAMS, project_params
from server.deps import db, settings_dep
from worker import queue

router = APIRouter(prefix="/api", tags=["edits"])


class VerticalParams(BaseModel):
    count: int | None = Field(None, ge=1, le=10)
    length: float | None = Field(None, ge=15, le=25)
    audio: str | None = Field(None, pattern="^(live|track|none)$")


class ClipParams(BaseModel):
    length: float | None = Field(None, ge=5, le=600)
    track_start: float | None = Field(None, ge=0)
    track_end: float | None = Field(None, ge=0)
    inserts: bool | None = None
    max_inserts: int | None = Field(None, ge=0, le=40)
    duck_db: float | None = Field(None, ge=-40, le=0)
    live_gain_db: float | None = Field(None, ge=-20, le=20)
    beats_per_level: dict[str, int] | None = None
    tag_weights: dict[str, float] | None = None
    excluded: list[list[float]] | None = None
    transitions: bool | None = None
    disabled_transitions: list[str] | None = None
    style_level: int | None = Field(None, ge=1, le=3)
    reset_bounds: bool = False


def _project(conn, project_id: int):
    try:
        return get_project(conn, project_id)
    except ProjectError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/projects/{project_id}/edits")
def list_edits(project_id: int, conn: sqlite3.Connection = Depends(db)) -> list[dict]:
    _project(conn, project_id)
    rows = conn.execute("SELECT * FROM edits WHERE project_id = ? ORDER BY kind, version DESC", (project_id,))
    return [{"id": r["id"], "kind": r["kind"], "version": r["version"], "created_at": r["created_at"],
             "params": json.loads(r["params"]), "summary": json.loads(r["summary"] or "{}")} for r in rows]


@router.get("/edits/{edit_id}/timeline")
def timeline(edit_id: int, conn: sqlite3.Connection = Depends(db),
             settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM edits WHERE id = ?", (edit_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Монтаж не знайдено")
    return json.loads((settings.library_path / row["timeline_path"]).read_text(encoding="utf-8"))


@router.get("/edits/{edit_id}/video")
def video(edit_id: int, conn: sqlite3.Connection = Depends(db),
          settings: Settings = Depends(settings_dep)) -> FileResponse:
    """Готовий mp4 1080p."""
    row = conn.execute("SELECT * FROM edits WHERE id = ?", (edit_id,)).fetchone()
    if row is None or not row["export_path"] or not (settings.library_path / row["export_path"]).exists():
        raise HTTPException(404, "Готового відео немає")
    path = settings.library_path / row["export_path"]
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.get("/edits/{edit_id}/davinci")
def davinci_status(edit_id: int, conn: sqlite3.Connection = Depends(db),
                   settings: Settings = Depends(settings_dep)) -> dict:
    """Де лежить проєкт DaVinci (папку відкривають на ноутбуці: File → Import → Timeline)."""
    row = conn.execute("SELECT * FROM edits WHERE id = ?", (edit_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Монтаж не знайдено")
    pending = conn.execute("SELECT status FROM jobs WHERE kind = 'export_davinci' AND payload = ?"
                           " ORDER BY id DESC LIMIT 1", (json.dumps({"edit_id": edit_id}),)).fetchone()
    path = settings.library_path / row["davinci_path"] if row["davinci_path"] else None
    return {"ready": bool(path and path.exists()), "fcpxml": str(path) if path else None,
            "folder": str(path.parent) if path else None, "job": pending["status"] if pending else None}


@router.post("/edits/{edit_id}/davinci", status_code=202)
def davinci_export(edit_id: int, conn: sqlite3.Connection = Depends(db)) -> dict:
    row = conn.execute("SELECT * FROM edits WHERE id = ?", (edit_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Монтаж не знайдено")
    return {"job_id": queue.enqueue(conn, "export_davinci", {"edit_id": edit_id}, project_id=row["project_id"])}


@router.get("/edits/{edit_id}/preview")
def preview(edit_id: int, conn: sqlite3.Connection = Depends(db),
            settings: Settings = Depends(settings_dep)) -> FileResponse:
    row = conn.execute("SELECT * FROM edits WHERE id = ?", (edit_id,)).fetchone()
    if row is None or not row["preview_path"]:
        raise HTTPException(404, "Превʼю немає")
    path = settings.library_path / row["preview_path"]
    if not path.exists():
        raise HTTPException(404, "Файл превʼю зник")
    return FileResponse(path, media_type="video/mp4")


@router.get("/projects/{project_id}/clip-settings")
def get_clip_settings(project_id: int, conn: sqlite3.Connection = Depends(db)) -> dict:
    return project_params(_project(conn, project_id))


@router.put("/projects/{project_id}/clip-settings")
def put_clip_settings(project_id: int, body: ClipParams, conn: sqlite3.Connection = Depends(db)) -> dict:
    project = _project(conn, project_id)
    stored = json.loads(project["settings"] or "{}")
    clip = stored.get("clip", {})
    clip.update({k: v for k, v in body.model_dump(exclude_none=True).items() if k in DEFAULT_PARAMS})
    if body.reset_bounds:
        clip.pop("track_start", None)
        clip.pop("track_end", None)
    stored["clip"] = clip
    conn.execute("UPDATE projects SET settings = ? WHERE id = ?", (json.dumps(stored, ensure_ascii=False), project_id))
    return project_params(_project(conn, project_id))


@router.post("/projects/{project_id}/reedit", status_code=202)
def reedit(project_id: int, conn: sqlite3.Connection = Depends(db)) -> dict:
    """Перемонтаж на кешованих оцінках — без запитів у Gemini."""
    _project(conn, project_id)
    return {"job_id": queue.enqueue(conn, "edit_project", {"project_id": project_id}, project_id=project_id, rerun_if_running=True)}


@router.get("/projects/{project_id}/vertical-settings")
def get_vertical_settings(project_id: int, conn: sqlite3.Connection = Depends(db)) -> dict:
    from edit.verticals import vertical_params
    return vertical_params(_project(conn, project_id))


@router.put("/projects/{project_id}/vertical-settings")
def put_vertical_settings(project_id: int, body: VerticalParams, conn: sqlite3.Connection = Depends(db)) -> dict:
    from edit.verticals import vertical_params
    project = _project(conn, project_id)
    stored = json.loads(project["settings"] or "{}")
    stored["vertical"] = {**stored.get("vertical", {}), **body.model_dump(exclude_none=True)}
    conn.execute("UPDATE projects SET settings = ? WHERE id = ?", (json.dumps(stored, ensure_ascii=False), project_id))
    return vertical_params(_project(conn, project_id))


@router.post("/projects/{project_id}/verticals", status_code=202)
def make_verticals(project_id: int, conn: sqlite3.Connection = Depends(db)) -> dict:
    """Перемонтаж вертикалей на кешованих оцінках."""
    _project(conn, project_id)
    return {"job_id": queue.enqueue(conn, "vertical_project", {"project_id": project_id}, project_id=project_id, rerun_if_running=True)}


@router.get("/edits/{edit_id}/reels/{n}")
def reel(edit_id: int, n: int, conn: sqlite3.Connection = Depends(db),
         settings: Settings = Depends(settings_dep)) -> FileResponse:
    row = conn.execute("SELECT * FROM edits WHERE id = ? AND kind = 'vertical'", (edit_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Вертикалей не знайдено")
    reels = json.loads(row["summary"] or "{}").get("reels", [])
    if not 1 <= n <= len(reels):
        raise HTTPException(404, "Немає такого ролика")
    path = settings.library_path / reels[n - 1]["path"]
    return FileResponse(path, media_type="video/mp4", filename=path.name)


def _sheet_for(path, vertical: bool = False):
    from render.contact import contact_sheet
    from render.ffmpeg import FFmpegError
    if not path.exists():
        raise HTTPException(404, "Готового відео немає")
    try:
        grid = {"cols": 6, "rows": 2, "tile_width": 200} if vertical else {}  # 9:16 — ширша сітка, нижча картинка
        return FileResponse(contact_sheet(path, path.with_name(path.stem + "_sheet.jpg"), **grid), media_type="image/jpeg")
    except FFmpegError as exc:
        raise HTTPException(500, str(exc)) from exc


@router.get("/edits/{edit_id}/sheet.jpg")
def sheet(edit_id: int, n: int = 1, conn: sqlite3.Connection = Depends(db),
          settings: Settings = Depends(settings_dep)) -> FileResponse:
    """Аркуш кадрів готового кліпу (або n-го вертикального ролика)."""
    row = conn.execute("SELECT * FROM edits WHERE id = ?", (edit_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Монтаж не знайдено")
    if row["kind"] == "vertical":
        reels = json.loads(row["summary"] or "{}").get("reels", [])
        if not 1 <= n <= len(reels):
            raise HTTPException(404, "Такого ролика немає")
        return _sheet_for(settings.library_path / reels[n - 1]["path"], vertical=True)
    if not row["export_path"]:
        raise HTTPException(404, "Готового відео немає")
    return _sheet_for(settings.library_path / row["export_path"])


@router.get("/edits/{edit_id}/delete-plan")
def delete_plan(edit_id: int, conn: sqlite3.Connection = Depends(db),
                settings: Settings = Depends(settings_dep)) -> dict:
    """Що саме буде видалено (лише рендери цієї версії; оригінали лишаються)."""
    from edit.cleanup import CleanupError, plan
    try:
        pl = plan(conn, settings, edit_id)
    except CleanupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"edit_id": edit_id, "project_id": pl.project_id, "label": pl.label,
            "paths": pl.describe(settings.library_path), "mb": round(pl.bytes / 1e6, 1)}


@router.delete("/edits/{edit_id}")
def delete_edit(edit_id: int, conn: sqlite3.Connection = Depends(db),
                settings: Settings = Depends(settings_dep)) -> dict:
    from edit.cleanup import CleanupError, delete
    if conn.execute("SELECT 1 FROM edits WHERE id = ?", (edit_id,)).fetchone() is None:
        raise HTTPException(404, "Монтаж не знайдено")
    try:
        pl = delete(conn, settings, edit_id)
    except CleanupError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"deleted": edit_id, "label": pl.label, "paths": len(pl.files) + len(pl.dirs)}
