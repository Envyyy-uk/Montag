"""Проєкти: одна папка — один монтаж («Концерт Brixton»)."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# Символи, заборонені в іменах файлів Windows, і керівні символи.
_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


class ProjectError(ValueError):
    pass


def make_slug(name: str) -> str:
    """Безпечна назва папки: кирилиця лишається, заборонене прибирається."""
    s = unicodedata.normalize("NFC", name).strip()
    s = _FORBIDDEN.sub("", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if not s or s in {".", ".."}:
        raise ProjectError("Назва проєкту порожня або складається лише із заборонених символів")
    if s.upper().split(".")[0] in _RESERVED:
        s = f"_{s}"
    return s[:80]


@dataclass
class ProjectPaths:
    root: Path

    @property
    def originals(self) -> Path:
        return self.root / "originals"

    @property
    def proxies(self) -> Path:
        return self.root / "proxies"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    def ensure(self) -> None:
        for p in (self.originals, self.proxies, self.exports):
            p.mkdir(parents=True, exist_ok=True)


def project_paths(library: Path, slug: str) -> ProjectPaths:
    return ProjectPaths(library / slug)


def create_project(conn: sqlite3.Connection, library: Path, name: str) -> sqlite3.Row:
    name = unicodedata.normalize("NFC", name).strip()
    slug = make_slug(name)
    existing = find_by_slug(conn, slug)
    if existing:
        raise ProjectError(f"Проєкт «{existing['name']}» вже існує")
    cur = conn.execute("INSERT INTO projects(name, slug, status) VALUES (?, ?, 'collecting')", (name, slug))
    project_paths(library, slug).ensure()
    return get_project(conn, cur.lastrowid)


def get_project(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise ProjectError(f"Проєкт {project_id} не знайдено")
    return row


def find_by_slug(conn: sqlite3.Connection, slug: str) -> sqlite3.Row | None:
    """Без урахування регістру, як у файловій системі Windows (SQLite lower() не знає кирилиці)."""
    key = slug.casefold()
    for row in conn.execute("SELECT * FROM projects"):
        if row["slug"].casefold() == key:
            return row
    return None


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM projects ORDER BY created_at DESC, id DESC").fetchall()
