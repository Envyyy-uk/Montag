"""API медіа: результати аналізу."""

from __future__ import annotations

import json
import sqlite3

from fastapi import APIRouter, Depends, HTTPException

from core.config import Settings
from server.deps import db, settings_dep
from worker.stages import analysis_dir

router = APIRouter(prefix="/api/media", tags=["media"])


@router.get("/{media_id}/analysis")
def analysis(media_id: int, conn: sqlite3.Connection = Depends(db),
             settings: Settings = Depends(settings_dep)) -> dict:
    media = conn.execute("SELECT m.*, p.slug FROM media m JOIN projects p ON p.id = m.project_id"
                         " WHERE m.id = ?", (media_id,)).fetchone()
    if media is None:
        raise HTTPException(404, "Медіа не знайдено")
    segments = [
        {"idx": r["idx"], "scene": r["scene_idx"], "start": r["start"], "end": r["end"],
         "local_score": r["local_score"], "reject_reason": r["reject_reason"],
         "audio_db": r["audio_db"], "burst_db": r["burst_db"], "metrics": json.loads(r["metrics"])}
        for r in conn.execute("SELECT * FROM segments WHERE media_id = ? ORDER BY idx", (media_id,))]
    events = [dict(r) for r in conn.execute(
        "SELECT kind, start, end, peak_excess_db, peak_db, flatness FROM audio_events"
        " WHERE media_id = ? ORDER BY start", (media_id,))]
    tr_path = analysis_dir(settings, media["slug"]) / f"{media_id}_transcript.json"
    transcript = json.loads(tr_path.read_text(encoding="utf-8")) if tr_path.exists() else None
    return {
        "media_id": media_id, "status": media["status"],
        "summary": json.loads(media["analysis_json"]) if media["analysis_json"] else None,
        "segments": segments, "audio_events": events, "transcript": transcript,
    }
