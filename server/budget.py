"""API бюджету Gemini і підтвердження дорогих завдань."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from core import budget
from core.config import Settings
from server.deps import db, settings_dep
from worker import queue

router = APIRouter(prefix="/api", tags=["budget"])


class Decision(BaseModel):
    approved: bool


@router.get("/budget")
def get_budget(conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)) -> dict:
    st = budget.state(conn, settings.monthly_budget_usd, settings.daily_request_limit)
    return {**st.as_dict(), "confirm_threshold": settings.confirm_threshold_usd,
            "model": settings.gemini_model, "api_key_set": bool(settings.gemini_api_key.get_secret_value()),
            "history": budget.history(conn)}


@router.get("/confirmations")
def pending(conn: sqlite3.Connection = Depends(db)) -> list[dict]:
    rows = conn.execute("SELECT j.id, j.kind, j.payload, p.name AS project FROM jobs j"
                        " LEFT JOIN projects p ON p.id = j.project_id WHERE j.status = 'awaiting_confirm'"
                        " ORDER BY j.id").fetchall()
    return [{"job_id": r["id"], "kind": r["kind"], "project": r["project"],
             **json.loads(r["payload"]).get("confirm", {})} for r in rows]


@router.post("/jobs/{job_id}/confirm")
def confirm(job_id: int, body: Decision, conn: sqlite3.Connection = Depends(db)) -> dict:
    if not queue.resolve_confirm(conn, job_id, body.approved):
        raise HTTPException(404, "Немає завдання, що чекає підтвердження")
    return {"job_id": job_id, "approved": body.approved}
