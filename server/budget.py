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
    from core.config import with_overrides
    settings = with_overrides(settings, conn)
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


@router.delete("/jobs/{job_id}")
def cancel_job(job_id: int, conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)) -> dict:
    """Прибрати завдання з черги: те, що чекає, — скасувати; те, що йде, — зупинити; помилку — сховати.
    Відео й готові версії не зачіпаються."""
    job = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise HTTPException(404, "Завдання немає")
    if job["kind"] == "poll_batch" and job["status"] != "done":
        name = json.loads(job["payload"]).get("batch")
        try:  # зупинити batch у Google, щоб не платити за непотрібне
            from analysis.scoring import get_backend
            backend = get_backend(settings)
            if backend is not None and hasattr(backend, "cancel_batch"):
                backend.cancel_batch(name)
        except Exception:  # noqa: BLE001
            pass
        conn.execute("UPDATE gemini_requests SET status = 'failed', error = 'скасовано', est_cost = 0"
                     " WHERE batch_name = ? AND status IN ('pending', 'submitted')", (name,))
    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    stopped = False
    if job["status"] == "running":
        from render.ffmpeg import terminate_all
        stopped = terminate_all() > 0  # рендер обривається одразу; інші етапи просто не запишуть результат
    pid = job["project_id"]
    if pid is not None and not conn.execute("SELECT 1 FROM jobs WHERE project_id = ? AND status IN"
                                            " ('queued', 'running', 'awaiting_confirm') LIMIT 1", (pid,)).fetchone():
        has_edit = conn.execute("SELECT 1 FROM edits WHERE project_id = ? LIMIT 1", (pid,)).fetchone()
        conn.execute("UPDATE projects SET status = ? WHERE id = ? AND status NOT IN ('needs_track')",
                     ("edited" if has_edit else "collecting", pid))
    return {"cancelled": job_id, "was": job["status"], "stopped_render": stopped}
