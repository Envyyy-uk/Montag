"""Прийом файлу в бібліотеку: хеш, дедуплікація, оригінал без змін, завдання на підготовку.

Оригінал кладеться в `library/<проєкт>/originals/` і позначається «лише читання».
Програма ніколи не змінює і не видаляє оригінали.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from core.projects import project_paths
from worker import queue

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mts", ".m2ts", ".avi", ".mkv", ".3gp"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
# Службові файли камер (GoPro .LRV/.THM, Sony .XML тощо) мовчки пропускаються.
IGNORED_EXT = {".lrv", ".thm", ".xml", ".bdm", ".cpi", ".mpl", ".ini", ".db", ".ds_store"}


class IngestError(ValueError):
    pass


@dataclass
class IngestResult:
    media: sqlite3.Row
    created: bool


def media_kind(filename: str) -> str | None:
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in AUDIO_EXT:
        return "track"
    return None


def sha256_file(path: Path, chunk: int = 4 * 1024 * 1024) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def safe_filename(name: str) -> str:
    name = unicodedata.normalize("NFC", Path(name.replace("\\", "/")).name).strip()
    name = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name).strip(" .")
    return name or "video"


def unique_path(folder: Path, filename: str) -> Path:
    candidate = folder / filename
    stem, suffix = candidate.stem, candidate.suffix
    n = 2
    while candidate.exists():
        candidate = folder / f"{stem} ({n}){suffix}"
        n += 1
    return candidate


def _place(src: Path, dst: Path, move: bool) -> None:
    if move:
        try:
            os.replace(src, dst)
            return
        except OSError:
            pass  # інший диск — копіюємо
    shutil.copy2(src, dst)
    if move:
        src.unlink()


def ingest_file(conn: sqlite3.Connection, library: Path, project: sqlite3.Row, src: Path,
                filename: str, source: str, move: bool, sha256: str | None = None) -> IngestResult:
    """Приймає файл у проєкт. При `move` джерело переноситься (upload-частина, inbox)."""
    filename = safe_filename(filename)
    kind = media_kind(filename)
    if kind is None:
        raise IngestError(f"Непідтримуваний формат: {filename}")
    digest = sha256 or sha256_file(src)
    size = src.stat().st_size

    existing = conn.execute("SELECT * FROM media WHERE project_id = ? AND sha256 = ?",
                            (project["id"], digest)).fetchone()
    if existing:
        if move:
            src.unlink(missing_ok=True)  # точна копія вже в бібліотеці
        return IngestResult(existing, False)

    paths = project_paths(library, project["slug"])
    paths.ensure()
    dst = unique_path(paths.originals, filename)
    _place(src, dst, move)
    os.chmod(dst, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)

    cur = conn.execute(
        "INSERT INTO media(project_id, kind, original_path, filename, sha256, size_bytes, source)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (project["id"], kind, str(dst.relative_to(library)), filename, digest, size, source))
    media_id = cur.lastrowid
    conn.execute("UPDATE projects SET last_media_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),"
                 " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (project["id"],))
    queue.enqueue(conn, "prepare_media", {"media_id": media_id}, project_id=project["id"])
    row = conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
    return IngestResult(row, True)
