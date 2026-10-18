"""API проєктів: створення, список, деталі, «Монтувати»."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from core.config import Settings
from core.projects import ProjectError, create_project, get_project, list_projects
from server.deps import db, settings_dep
from worker.stages import request_edit

router = APIRouter(prefix="/api/projects", tags=["projects"])


class NewProject(BaseModel):
    name: str = Field(min_length=1, max_length=120)


def _project_json(row: sqlite3.Row, conn: sqlite3.Connection) -> dict:
    counts = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM media WHERE project_id = ? GROUP BY kind", (row["id"],)).fetchall()
    by_kind = {r["kind"]: r["n"] for r in counts}
    return {
        "id": row["id"], "name": row["name"], "slug": row["slug"], "status": row["status"],
        "style_id": row["style_id"], "created_at": row["created_at"],
        "last_media_at": row["last_media_at"],
        "videos": by_kind.get("video", 0), "tracks": by_kind.get("track", 0),
    }


def _media_json(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"], "kind": row["kind"], "filename": row["filename"], "status": row["status"],
        "size_bytes": row["size_bytes"], "source": row["source"], "duration": row["duration"],
        "shot_at": row["shot_at"], "error": row["error"],
        "probe": json.loads(row["probe_json"]) if row["probe_json"] else None,
    }


@router.post("", status_code=201)
def create(body: NewProject, conn: sqlite3.Connection = Depends(db),
           settings: Settings = Depends(settings_dep)) -> dict:
    try:
        row = create_project(conn, settings.library_path, body.name)
    except ProjectError as exc:
        raise HTTPException(409, str(exc)) from exc
    # однойменна папка в Google Drive (якщо Drive підключено; без мережі — пізніше)
    from sources import gdrive
    from worker import queue
    if gdrive.configured(settings):
        queue.enqueue(conn, "drive_create_folder", {"project_id": row["id"]}, project_id=row["id"])
    return _project_json(row, conn)


@router.get("")
def index(conn: sqlite3.Connection = Depends(db)) -> list[dict]:
    return [_project_json(r, conn) for r in list_projects(conn)]


@router.get("/{project_id}")
def show(project_id: int, conn: sqlite3.Connection = Depends(db)) -> dict:
    try:
        row = get_project(conn, project_id)
    except ProjectError as exc:
        raise HTTPException(404, str(exc)) from exc
    media = conn.execute("SELECT * FROM media WHERE project_id = ? ORDER BY shot_at, id", (project_id,)).fetchall()
    return {**_project_json(row, conn), "media": [_media_json(m) for m in media]}


@router.post("/{project_id}/edit", status_code=202)
def edit(project_id: int, conn: sqlite3.Connection = Depends(db)) -> dict:
    try:
        get_project(conn, project_id)
    except ProjectError as exc:
        raise HTTPException(404, str(exc)) from exc
    if not conn.execute("SELECT 1 FROM media WHERE project_id = ? AND kind = 'video'", (project_id,)).fetchone():
        raise HTTPException(409, "У проєкті ще немає відео")
    return {"job_id": request_edit(conn, project_id)}
