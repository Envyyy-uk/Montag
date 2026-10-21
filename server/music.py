"""API бібліотеки музики й підбору треку під відео."""

from __future__ import annotations

import json
import mimetypes
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from core.config import Settings
from core.projects import ProjectError, get_project
from server.deps import db, settings_dep
from worker import queue

router = APIRouter(prefix="/api", tags=["music"])


class Suggest(BaseModel):
    use_ai: bool = True


class Use(BaseModel):
    rerender: bool = False
    start: float | None = None   # шматок треку, с (за замовчуванням — найкращий під довжину кліпу)
    end: float | None = None


@router.get("/music")
def library(conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)) -> dict:
    rows = conn.execute("SELECT * FROM music_tracks WHERE status != 'missing' ORDER BY artist, title").fetchall()
    return {"folder": str(settings.music_path), "drive_folder": settings.drive_music_folder,
            "tracks": [{"id": r["id"], "title": r["title"] or r["path"], "artist": r["artist"], "status": r["status"],
                        "tempo": round(r["tempo"] or 0), "duration": round(r["duration"] or 0), "source": r["source"]}
                       for r in rows]}


@router.post("/music/rescan")
def rescan(conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)) -> dict:
    from sources import music
    settings.music_path.mkdir(parents=True, exist_ok=True)
    return {"new": len(music.scan(conn, settings))}


@router.get("/music/{track_id}/audio")
def audio(track_id: int, conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)):
    from sources.music import file_of
    r = conn.execute("SELECT * FROM music_tracks WHERE id = ?", (track_id,)).fetchone()
    path = file_of(settings, r) if r else None
    if path is None or not path.exists():
        raise HTTPException(404, "Треку немає")
    return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "audio/mpeg")


def _project(conn, project_id):
    try:
        return get_project(conn, project_id)
    except ProjectError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/projects/{project_id}/music-suggest")
def last_suggest(project_id: int, conn: sqlite3.Connection = Depends(db)) -> dict | None:
    return json.loads(_project(conn, project_id)["settings"] or "{}").get("music_suggest")


@router.post("/projects/{project_id}/music-suggest")
def suggest(project_id: int, body: Suggest, conn: sqlite3.Connection = Depends(db),
            settings: Settings = Depends(settings_dep)) -> dict:
    from core.config import with_overrides
    from edit import music_pick
    _project(conn, project_id)
    return music_pick.suggest(conn, with_overrides(settings, conn), project_id, use_ai=body.use_ai)


@router.post("/projects/{project_id}/music/{track_id}/use")
def use(project_id: int, track_id: int, body: Use, conn: sqlite3.Connection = Depends(db),
        settings: Settings = Depends(settings_dep)) -> dict:
    from edit import music_pick
    _project(conn, project_id)
    try:
        if body.start is None or body.end is None:
            from edit.build import project_params
            frag = music_pick._best_fragment(settings, track_id, float(project_params(_project(conn, project_id), conn)["length"] or 60))
            body.start, body.end = (frag["start"], frag["end"]) if frag else (None, None)
        out = music_pick.use_track(conn, settings, project_id, track_id, body.start, body.end)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    if body.rerender:
        out["job_id"] = queue.enqueue(conn, "edit_project", {"project_id": project_id}, project_id=project_id,
                                      rerun_if_running=True)
    return out
