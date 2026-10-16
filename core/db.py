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
    # 2 — введення відео (Спринт 2)
    """
    ALTER TABLE projects ADD COLUMN last_media_at TEXT;
    ALTER TABLE projects ADD COLUMN edit_requested_at TEXT;

    ALTER TABLE media ADD COLUMN filename TEXT;
    ALTER TABLE media ADD COLUMN duration REAL;
    ALTER TABLE media ADD COLUMN shot_at TEXT;
    ALTER TABLE media ADD COLUMN error TEXT;

    CREATE TABLE uploads (
        id          TEXT PRIMARY KEY,
        project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        filename    TEXT NOT NULL,
        length      INTEGER NOT NULL,
        offset      INTEGER NOT NULL DEFAULT 0,
        part_path   TEXT NOT NULL,
        media_id    INTEGER REFERENCES media(id) ON DELETE SET NULL,
        created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    """,
    # 3 — аналіз: сегменти, звукові події (Спринт 3)
    """
    ALTER TABLE media ADD COLUMN analysis_json TEXT;
    ALTER TABLE media ADD COLUMN analyzed_at TEXT;

    CREATE TABLE segments (
        id            INTEGER PRIMARY KEY,
        media_id      INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
        idx           INTEGER NOT NULL,
        scene_idx     INTEGER NOT NULL,
        start         REAL NOT NULL,
        end           REAL NOT NULL,
        metrics       TEXT NOT NULL,
        local_score   REAL NOT NULL,
        reject_reason TEXT,
        audio_db      REAL,
        burst_db      REAL NOT NULL DEFAULT 0,
        UNIQUE (media_id, idx)
    );
    CREATE INDEX segments_media ON segments(media_id, start);

    CREATE TABLE audio_events (
        id             INTEGER PRIMARY KEY,
        media_id       INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
        kind           TEXT NOT NULL,
        start          REAL NOT NULL,
        end            REAL NOT NULL,
        peak_excess_db REAL NOT NULL,
        peak_db        REAL NOT NULL,
        flatness       REAL NOT NULL
    );
    CREATE INDEX audio_events_media ON audio_events(media_id, start);
    """,
    # 4 — Gemini: файли, запити, оцінки, витрати (Спринт 4)
    """
    ALTER TABLE jobs ADD COLUMN run_after TEXT;
    ALTER TABLE spend ADD COLUMN request_id INTEGER;

    CREATE TABLE gemini_files (
        id          INTEGER PRIMARY KEY,
        media_id    INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
        win_start   REAL NOT NULL,
        win_end     REAL NOT NULL,
        local_path  TEXT NOT NULL,
        uri         TEXT,
        name        TEXT,
        expires_at  TEXT,
        UNIQUE (media_id, win_start)
    );

    CREATE TABLE gemini_requests (
        id            INTEGER PRIMARY KEY,
        project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        media_id      INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
        win_start     REAL NOT NULL,
        win_end       REAL NOT NULL,
        style_key     TEXT NOT NULL,
        segments      TEXT NOT NULL,
        attempt       INTEGER NOT NULL DEFAULT 1,
        mode          TEXT NOT NULL DEFAULT 'batch' CHECK (mode IN ('batch', 'online')),
        status        TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'submitted', 'done', 'invalid', 'failed')),
        batch_name    TEXT,
        est_cost      REAL NOT NULL DEFAULT 0,
        cost          REAL,
        event_type    TEXT,
        event_confidence REAL,
        error         TEXT,
        created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE INDEX gemini_requests_status ON gemini_requests(status, batch_name);

    CREATE TABLE scores (
        id          INTEGER PRIMARY KEY,
        media_id    INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
        start       REAL NOT NULL,
        end         REAL NOT NULL,
        style_key   TEXT NOT NULL,
        source      TEXT NOT NULL CHECK (source IN ('gemini', 'local')),
        score       REAL NOT NULL,
        description TEXT,
        tags        TEXT NOT NULL DEFAULT '[]',
        vertical_ok INTEGER,
        subject_x   REAL,
        request_id  INTEGER REFERENCES gemini_requests(id) ON DELETE SET NULL,
        created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE (media_id, start, end, style_key)
    );
    """,
    # 5 — монтаж кліпу (Спринт 5)
    """
    ALTER TABLE projects ADD COLUMN settings TEXT NOT NULL DEFAULT '{}';

    CREATE TABLE edits (
        id            INTEGER PRIMARY KEY,
        project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        kind          TEXT NOT NULL CHECK (kind IN ('clip', 'vertical')),
        version       INTEGER NOT NULL,
        params        TEXT NOT NULL DEFAULT '{}',
        timeline_path TEXT NOT NULL,
        audio_path    TEXT,
        preview_path  TEXT,
        summary       TEXT,
        created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
        UNIQUE (project_id, kind, version)
    );
    """,
]


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
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
