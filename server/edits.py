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
    return {"job_id": queue.enqueue(conn, "edit_project", {"project_id": project_id}, project_id=project_id)}
