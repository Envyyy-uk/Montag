import sqlite3

import pytest

from core import db


def test_migrate_fresh(tmp_path):
    conn = db.open_db(tmp_path / "a.sqlite3")
    assert db.schema_version(conn) == len(db.MIGRATIONS)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"projects", "media", "jobs", "spend", "settings"} <= tables


def test_migrate_idempotent(tmp_path):
    path = tmp_path / "a.sqlite3"
    db.open_db(path).close()
    conn = db.open_db(path)
    assert db.schema_version(conn) == len(db.MIGRATIONS)


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    path = tmp_path / "a.sqlite3"
    db.open_db(path).close()
    monkeypatch.setattr(db, "MIGRATIONS", db.MIGRATIONS + ["CREATE TABLE x (a); BROKEN SQL"])
    conn = db.connect(path)
    with pytest.raises(sqlite3.Error):
        db.migrate(conn)
    assert db.schema_version(conn) == len(db.MIGRATIONS) - 1
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='x'").fetchone() is None


def test_foreign_keys_cascade(tmp_path):
    conn = db.open_db(tmp_path / "a.sqlite3")
    conn.execute("INSERT INTO projects(name, slug) VALUES ('Концерт Brixton', 'koncert-brixton')")
    pid = conn.execute("SELECT id FROM projects").fetchone()[0]
    conn.execute(
        "INSERT INTO media(project_id, kind, original_path, sha256, size_bytes, source)"
        " VALUES (?, 'video', 'a.mp4', 'abc', 1, 'upload')", (pid,))
    conn.execute("DELETE FROM projects WHERE id=?", (pid,))
    assert conn.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0


def test_job_status_constraint(tmp_path):
    conn = db.open_db(tmp_path / "a.sqlite3")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs(kind, status) VALUES ('x', 'weird')")
