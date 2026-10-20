"""API стилів: бібліотека, тестові 5-секундні рендери трьох рівнів, затвердження рівня, стиль проєкту."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from core.config import Settings
from core.projects import ProjectError, get_project
from render import style_store
from render.looks import StyleError, get_style, list_styles
from render.transitions import TRANSITIONS
from server.deps import db, settings_dep
from worker import queue

router = APIRouter(prefix="/api", tags=["styles"])


class TestRequest(BaseModel):
    media_id: int | None = None
    start: float = Field(0.0, ge=0)


class Approve(BaseModel):
    level: int = Field(ge=1, le=3)


class ProjectStyle(BaseModel):
    style_id: str


class EventStyle(BaseModel):
    event: str
    style_id: str | None = None  # None — повернути вбудований вибір


def test_path(settings: Settings, style_id: str, level: int):
    return settings.library_path / "style_tests" / style_id / f"level_{level}.mp4"


@router.get("/styles")
def styles(conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)) -> list[dict]:
    approved = style_store.approved_levels(conn)
    return [{"id": s.id, "name": s.name, "description": s.data.get("description", ""),
             "default_level": s.default_level, "approved_level": approved.get(s.id),
             "transitions": [{"id": k, "name": TRANSITIONS[k].name} for k in s.transitions.get("allowed", [])],
             "tests": {lvl: test_path(settings, s.id, lvl).exists() for lvl in (1, 2, 3)}}
            for s in list_styles(include_drafts=False)]


@router.post("/styles/{style_id}/test", status_code=202)
def make_test(style_id: str, body: TestRequest, conn: sqlite3.Connection = Depends(db)) -> dict:
    try:
        get_style(style_id)
    except StyleError as exc:
        raise HTTPException(404, str(exc)) from exc
    job = queue.enqueue(conn, "style_test", {"style_id": style_id, "media_id": body.media_id, "start": body.start},
                        dedupe=False)
    return {"job_id": job}


@router.get("/styles/{style_id}/test/{level}")
def get_test(style_id: str, level: int, settings: Settings = Depends(settings_dep)) -> FileResponse:
    path = test_path(settings, style_id, level)
    if not path.exists():
        raise HTTPException(404, "Тестового рендеру ще немає")
    return FileResponse(path, media_type="video/mp4")


@router.post("/styles/{style_id}/approve")
def approve(style_id: str, body: Approve, conn: sqlite3.Connection = Depends(db)) -> dict:
    try:
        style_store.approve(conn, style_id, body.level)
    except StyleError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"style_id": style_id, "approved_level": body.level}


@router.put("/projects/{project_id}/style")
def set_project_style(project_id: int, body: ProjectStyle, conn: sqlite3.Connection = Depends(db)) -> dict:
    try:
        get_project(conn, project_id)
        get_style(body.style_id)
    except (ProjectError, StyleError) as exc:
        raise HTTPException(404, str(exc)) from exc
    conn.execute("UPDATE projects SET style_id = ? WHERE id = ?", (body.style_id, project_id))
    return {"project_id": project_id, "style_id": body.style_id}


@router.get("/styles/events")
def event_styles(conn: sqlite3.Connection = Depends(db)) -> list[dict]:
    """Для кожного типу події — який стиль поставиться автоматично і чи це твій вибір."""
    from analysis.scoring import EVENT_TYPES
    from edit import style_auto
    mine = style_auto.event_map(conn)
    return [{"event": e, "style_id": style_auto.style_for_event(e, conn), "builtin": style_auto.style_for_event(e),
             "mine": e in mine} for e in EVENT_TYPES]


@router.put("/styles/events")
def set_event_style(body: EventStyle, conn: sqlite3.Connection = Depends(db)) -> list[dict]:
    import json

    from analysis.scoring import EVENT_TYPES
    from edit import style_auto
    if body.event not in EVENT_TYPES:
        raise HTTPException(404, f"Невідомий тип події. Є: {', '.join(EVENT_TYPES)}")
    if body.style_id is not None:
        try:
            get_style(body.style_id)
        except StyleError as exc:
            raise HTTPException(404, str(exc)) from exc
    mine = style_auto.event_map(conn)
    if body.style_id is None:
        mine.pop(body.event, None)
    else:
        mine[body.event] = body.style_id
    conn.execute("INSERT INTO settings(key, value) VALUES ('event_styles', ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(mine, ensure_ascii=False),))
    return event_styles(conn)
