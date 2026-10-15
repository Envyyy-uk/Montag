"""SQLite: підключення і міграції схеми.

Кожна міграція — окремий SQL-блок; номер поточної версії зберігається в
`PRAGMA user_version`. Наступні спринти лише додають нові міграції в кінець.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1 — базова схема (Спринт 1)
    """
    CREATE TABLE projects (
        id          INTEGER PRIMARY KEY,
        name        TEXT NOT NULL UNIQUE,
        slug        TEXT NOT NULL UNIQUE,
        status      TEXT NOT NULL DEFAULT 'new',
        style_id    TEXT,
        created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );

    CREATE TABLE media (
        id            INTEGER PRIMARY KEY,
        project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        kind          TEXT NOT NULL CHECK (kind IN ('video', 'track')),
        original_path TEXT NOT NULL,
        sha256        TEXT NOT NULL,
        size_bytes    INTEGER NOT NULL,
        source        TEXT NOT NULL CHECK (source IN ('upload', 'drive', 'inbox')),
        probe_json    TEXT,
        proxy_path    TEXT,
        gemini_proxy_path TEXT,
        status        TEXT NOT NULL DEFAULT 'received',
        created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE (project_id, sha256)
    );

    CREATE TABLE jobs (
        id          INTEGER PRIMARY KEY,
        project_id  INTEGER REFERENCES projects(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL,
        payload     TEXT NOT NULL DEFAULT '{}',
        status      TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued', 'running', 'awaiting_confirm', 'done', 'failed')),
        progress    REAL NOT NULL DEFAULT 0,
        attempts    INTEGER NOT NULL DEFAULT 0,
        error       TEXT,
        created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        started_at  TEXT,
        finished_at TEXT
    );
    CREATE INDEX jobs_status ON jobs(status, id);

    CREATE TABLE spend (
        id            INTEGER PRIMARY KEY,
        project_id    INTEGER REFERENCES projects(id) ON DELETE SET NULL,
        model         TEXT NOT NULL,
        purpose       TEXT NOT NULL,
        input_tokens  INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd      REAL NOT NULL,
        batch         INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE INDEX spend_created ON spend(created_at);

    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """Застосовує всі нові міграції атомарно. Повертає фінальну версію."""
    current = schema_version(conn)
    for version, sql in enumerate(MIGRATIONS[current:], start=current + 1):
        conn.execute("BEGIN")
        try:
            for statement in sql.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return schema_version(conn)


def open_db(path: Path | str) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
