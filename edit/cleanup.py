"""Видалення: рендери однієї версії, усі старі версії, цілий проєкт (оригінали — в архів, не видаляються).

У папці DaVinci оригінали лежать жорсткими посиланнями: видалення посилання не зачіпає сам файл
в `originals/` — його вміст і далі на місці.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
from dataclasses import dataclass, field
from pathlib import Path

from core.config import Settings

BUSY_KINDS = ("edit_project", "vertical_project", "export_davinci", "drive_upload")


class CleanupError(Exception):
    pass


@dataclass
class Plan:
    edit_id: int
    project_id: int
    label: str
    files: list[Path] = field(default_factory=list)
    dirs: list[Path] = field(default_factory=list)

    @property
    def bytes(self) -> int:
        n = sum(p.stat().st_size for p in self.files if p.is_file())
        for d in self.dirs:
            n += sum(p.stat().st_size for p in d.rglob("*") if p.is_file() and p.stat().st_nlink == 1)
        return n

    def describe(self, lib: Path) -> list[str]:
        return [str(p.relative_to(lib)) for p in [*self.files, *self.dirs]]


def rmtree(path: Path) -> None:
    """Як shutil.rmtree, але й на Windows з файлами «лише читання» (жорсткі посилання на оригінали в DaVinci)."""
    def force(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if path.exists():
        shutil.rmtree(path, onexc=force)


def _readonly(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass


def protect_originals(settings: Settings, project_id: int) -> None:
    """Зняття «лише читання» з жорсткого посилання знімає його й з оригіналу — повертаємо."""
    from core.db import open_db
    conn = open_db(settings.database_path)
    try:
        for r in conn.execute("SELECT original_path FROM media WHERE project_id = ?", (project_id,)):
            p = settings.library_path / r["original_path"]
            if p.exists():
                _readonly(p)
    finally:
        conn.close()


def _inside(path: Path, roots: list[Path]) -> bool:
    rp = path.resolve()
    return any(rp == r or r in rp.parents for r in roots)


def plan(conn: sqlite3.Connection, settings: Settings, edit_id: int) -> Plan:
    lib = settings.library_path
    e = conn.execute("SELECT * FROM edits WHERE id = ?", (edit_id,)).fetchone()
    if e is None:
        raise CleanupError("Монтаж не знайдено")
    p = conn.execute("SELECT slug FROM projects WHERE id = ?", (e["project_id"],)).fetchone()
    base = lib / p["slug"]
    allowed = [(base / "edits").resolve(), (base / "exports").resolve()]
    forbidden = [(base / "originals").resolve(), (base / "proxies").resolve()]

    files: set[Path] = set()
    dirs: set[Path] = set()
    for col in ("export_path", "preview_path", "audio_path", "timeline_path"):
        if e[col]:
            files.add(lib / e[col])
    for reel in json.loads(e["summary"] or "{}").get("reels", []):
        files.add(lib / reel["path"])
    for f in list(files):
        if f.suffix == ".mp4":
            files.add(f.with_name(f.stem + "_sheet.jpg"))
    if e["davinci_path"]:
        dirs.add((lib / e["davinci_path"]).parent)
    tl_dir = (lib / e["timeline_path"]).parent
    if tl_dir.name.startswith(("clip_v", "vertical_v")):
        dirs.add(tl_dir)
        files = {f for f in files if tl_dir not in f.parents}

    for path in [*files, *dirs]:
        if not _inside(path, allowed) or _inside(path, forbidden):
            raise CleanupError(f"Відмовлено: {path} поза папками рендерів")
    label = f"{'кліп' if e['kind'] == 'clip' else 'вертикалі'} v{e['version']}"
    return Plan(edit_id, e["project_id"], label, sorted(f for f in files if f.exists()),
                sorted(d for d in dirs if d.exists()))


def delete(conn: sqlite3.Connection, settings: Settings, edit_id: int) -> Plan:
    pl = plan(conn, settings, edit_id)
    busy = conn.execute(f"SELECT 1 FROM jobs WHERE project_id = ? AND status IN ('queued', 'running')"
                        f" AND kind IN ({','.join('?' * len(BUSY_KINDS))}) LIMIT 1",
                        (pl.project_id, *BUSY_KINDS)).fetchone()
    if busy:
        raise CleanupError("Зараз іде монтаж або вивантаження цього проєкту — спробуй пізніше")
    for f in pl.files:
        f.unlink(missing_ok=True)
    for d in pl.dirs:
        rmtree(d)
    protect_originals(settings, pl.project_id)
    conn.execute("DELETE FROM edits WHERE id = ?", (edit_id,))
    return pl


# ---------- старі версії ----------

def _main_ids(conn, project_id: int) -> set[int]:
    from server.edits import main_edit
    return {r["id"] for k in ("clip", "vertical") if (r := main_edit(conn, project_id, k)) is not None}


def prune_plan(conn: sqlite3.Connection, settings: Settings, project_id: int) -> dict:
    keep = _main_ids(conn, project_id)
    ids = [r["id"] for r in conn.execute("SELECT id FROM edits WHERE project_id = ?", (project_id,)) if r["id"] not in keep]
    size = sum(plan(conn, settings, i).bytes for i in ids)
    return {"edit_ids": ids, "count": len(ids), "mb": round(size / 1e6, 1), "keep": sorted(keep)}


def prune(conn: sqlite3.Connection, settings: Settings, project_id: int) -> dict:
    pl = prune_plan(conn, settings, project_id)
    for i in pl["edit_ids"]:
        delete(conn, settings, i)
    return pl


# ---------- цілий проєкт ----------

def _dir_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and f.stat().st_nlink == 1) if path.exists() else 0


def project_plan(conn: sqlite3.Connection, settings: Settings, project_id: int) -> dict:
    p = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if p is None:
        raise CleanupError("Проєкт не знайдено")
    lib = settings.library_path
    originals = [lib / r["original_path"] for r in conn.execute("SELECT original_path FROM media WHERE project_id = ?",
                                                                (project_id,)) if (lib / r["original_path"]).exists()]
    keep = []
    for eid in _main_ids(conn, project_id):
        e = conn.execute("SELECT * FROM edits WHERE id = ?", (eid,)).fetchone()
        if e["kind"] == "clip" and e["export_path"] and (lib / e["export_path"]).exists():
            keep.append(lib / e["export_path"])
        for reel in json.loads(e["summary"] or "{}").get("reels", []):
            if (lib / reel["path"]).exists():
                keep.append(lib / reel["path"])
    base = lib / p["slug"]
    kept = sum(f.stat().st_size for f in originals + keep)
    from sources.ingest import safe_filename
    return {"project_id": project_id, "name": p["name"], "base": base, "originals": originals, "keep": keep,
            "archive": settings.archive_path / safe_filename(p["name"]),
            "archive_mb": round(kept / 1e6, 1), "freed_mb": round(max(0, _dir_size(base) - kept) / 1e6, 1)}


def delete_project(conn: sqlite3.Connection, settings: Settings, project_id: int) -> dict:
    """Оригінали й основна версія → архів; решта видаляється; папка в Drive → у кошик Drive."""
    from sources import gdrive
    from sources.ingest import unique_path

    pl = project_plan(conn, settings, project_id)
    if conn.execute("SELECT 1 FROM jobs WHERE project_id = ? AND status = 'running'", (project_id,)).fetchone():
        raise CleanupError("Зараз іде завдання цього проєкту — зупини його в черзі (✕) і спробуй ще раз")
    archive = pl["archive"]
    n = 2
    while archive.exists():  # не змішувати з архівом іншого проєкту з тією ж назвою
        archive = pl["archive"].with_name(f"{pl['archive'].name} ({n})")
        n += 1
    (archive / "originals").mkdir(parents=True, exist_ok=True)
    moved = []
    for f in pl["originals"]:
        dst = unique_path(archive / "originals", f.name)
        shutil.move(str(f), str(dst))
        moved.append(dst)
    if pl["keep"]:
        (archive / "готове").mkdir(exist_ok=True)
        for f in pl["keep"]:
            shutil.move(str(f), str(unique_path(archive / "готове", f.name)))
    folders = gdrive._state(conn, "drive_projects")
    folder = folders.pop(str(project_id), None)
    if folder:
        gdrive._save_state(conn, "drive_projects", folders)
        from worker import queue
        queue.enqueue(conn, "drive_trash", {"file_id": folder["id"], "name": folder.get("name")})
    conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
    rmtree(pl["base"])
    for f in moved:
        _readonly(f)
    return {"name": pl["name"], "archive": str(archive), "originals": len(moved), "kept": len(pl["keep"]),
            "freed_mb": pl["freed_mb"], "drive": bool(folder)}
