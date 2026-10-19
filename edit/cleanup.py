"""Видалення готових рендерів однієї версії монтажу. Оригінали й проксі не чіпаються ніколи.

У папці DaVinci оригінали лежать жорсткими посиланнями: видалення посилання не зачіпає сам файл
в `originals/` — його вміст і далі на місці.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
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
        shutil.rmtree(d, ignore_errors=True)
    conn.execute("DELETE FROM edits WHERE id = ?", (edit_id,))
    return pl
