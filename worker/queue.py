"""Черга завдань у SQLite.

Завдання переживають перезапуск: при старті воркера всі `running`
повертаються в `queued` (етапи ідемпотентні).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

MAX_ATTEMPTS = 3


def enqueue(conn: sqlite3.Connection, kind: str, payload: dict[str, Any] | None = None,
            project_id: int | None = None, dedupe: bool = True) -> int:
    """Додає завдання. З `dedupe` не дублює вже заплановане таке саме."""
    body = json.dumps(payload or {}, sort_keys=True, ensure_ascii=False)
    if dedupe:
        row = conn.execute(
            "SELECT id FROM jobs WHERE kind = ? AND payload = ? AND project_id IS ?"
            " AND status IN ('queued', 'running', 'awaiting_confirm')",
            (kind, body, project_id)).fetchone()
        if row:
            return row["id"]
    cur = conn.execute("INSERT INTO jobs(kind, payload, project_id) VALUES (?, ?, ?)",
                       (kind, body, project_id))
    return cur.lastrowid


def claim(conn: sqlite3.Connection, kinds: Iterable[str]) -> sqlite3.Row | None:
    """Атомарно бере найстаріше завдання з відомих видів."""
    kinds = list(kinds)
    if not kinds:
        return None
    marks = ",".join("?" * len(kinds))
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            f"SELECT * FROM jobs WHERE status = 'queued' AND kind IN ({marks}) ORDER BY id LIMIT 1",
            kinds).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, progress = 0,"
            " started_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (row["id"],))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()


def set_progress(conn: sqlite3.Connection, job_id: int, progress: float) -> None:
    conn.execute("UPDATE jobs SET progress = ? WHERE id = ?", (max(0.0, min(1.0, progress)), job_id))


def finish(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'done', progress = 1, error = NULL,"
        " finished_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (job_id,))


def fail(conn: sqlite3.Connection, job_id: int, error: str) -> str:
    """Повертає в чергу, доки не вичерпано спроби. Повертає новий статус."""
    row = conn.execute("SELECT attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
    status = "queued" if row and row["attempts"] < MAX_ATTEMPTS else "failed"
    conn.execute(
        "UPDATE jobs SET status = ?, error = ?, finished_at = CASE WHEN ? = 'failed'"
        " THEN strftime('%Y-%m-%dT%H:%M:%fZ', 'now') END WHERE id = ?",
        (status, error[-2000:], status, job_id))
    return status


def recover(conn: sqlite3.Connection) -> int:
    """Після перезапуску: перервані завдання знову в черзі."""
    return conn.execute("UPDATE jobs SET status = 'queued' WHERE status = 'running'").rowcount


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE status IN ('queued', 'running')").fetchone()[0]


def payload(job: sqlite3.Row) -> dict[str, Any]:
    return json.loads(job["payload"])
