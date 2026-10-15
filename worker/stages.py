"""Етапи конвеєра. Кожен етап ідемпотентний: повторний запуск після збою безпечний."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from analysis.probe import ProbeError, probe
from render.proxy import EDIT, GEMINI, make_proxy
from worker import queue

log = logging.getLogger(__name__)

Progress = Callable[[float], None]


def prepare_media(conn: sqlite3.Connection, library: Path, payload: dict, progress: Progress) -> None:
    """ffprobe + проксі 1080p і 480p (з HDR→SDR)."""
    media = conn.execute("SELECT m.*, p.slug FROM media m JOIN projects p ON p.id = m.project_id"
                         " WHERE m.id = ?", (payload["media_id"],)).fetchone()
    if media is None:
        return  # медіа видалено разом із проєктом
    src = library / media["original_path"]
    conn.execute("UPDATE media SET status = 'probing' WHERE id = ?", (media["id"],))
    try:
        info, _raw = probe(src)
    except ProbeError as exc:
        conn.execute("UPDATE media SET status = 'error', error = ? WHERE id = ?", (str(exc), media["id"]))
        return  # битий файл не повторюємо
    conn.execute("UPDATE media SET probe_json = ?, duration = ?, shot_at = ? WHERE id = ?",
                 (json.dumps(info.as_dict(), ensure_ascii=False), info.duration, info.shot_at, media["id"]))

    if media["kind"] == "video" and info.has_video:
        proxies = library / media["slug"] / "proxies"
        warnings: list[str] = []
        conn.execute("UPDATE media SET status = 'proxy' WHERE id = ?", (media["id"],))
        edit_dst = proxies / f"{media['id']}_{EDIT.name}.mp4"
        if not edit_dst.exists():
            warnings += make_proxy(src, edit_dst, info, EDIT, lambda p: progress(0.05 + 0.7 * p))
        gem_dst = proxies / f"{media['id']}_{GEMINI.name}.mp4"
        if not gem_dst.exists():
            warnings += make_proxy(src, gem_dst, info, GEMINI, lambda p: progress(0.75 + 0.25 * p))
        conn.execute("UPDATE media SET proxy_path = ?, gemini_proxy_path = ?, error = ? WHERE id = ?",
                     (str(edit_dst.relative_to(library)), str(gem_dst.relative_to(library)),
                      "; ".join(dict.fromkeys(warnings)) or None, media["id"]))
    conn.execute("UPDATE media SET status = 'ready' WHERE id = ?", (media["id"],))


def request_edit(conn: sqlite3.Connection, project_id: int) -> int:
    """«Монтувати»: проєкт іде в чергу на монтаж."""
    conn.execute("UPDATE projects SET status = 'queued', edit_requested_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),"
                 " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (project_id,))
    return queue.enqueue(conn, "edit_project", {"project_id": project_id}, project_id=project_id)


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def auto_queue_idle(conn: sqlite3.Connection, idle_minutes: int, now: datetime | None = None) -> list[int]:
    """Проєкти, куди файли не надходять `idle_minutes`, самі стають у чергу на монтаж."""
    now = now or datetime.now(timezone.utc)
    queued = []
    rows = conn.execute("SELECT id, last_media_at FROM projects WHERE status = 'collecting'"
                        " AND last_media_at IS NOT NULL").fetchall()
    for row in rows:
        if now - _parse_ts(row["last_media_at"]) < timedelta(minutes=idle_minutes):
            continue
        has_video = conn.execute("SELECT 1 FROM media WHERE project_id = ? AND kind = 'video'",
                                 (row["id"],)).fetchone()
        preparing = conn.execute("SELECT 1 FROM jobs WHERE project_id = ? AND kind = 'prepare_media'"
                                 " AND status IN ('queued', 'running')", (row["id"],)).fetchone()
        if has_video and not preparing:
            request_edit(conn, row["id"])
            queued.append(row["id"])
    return queued
