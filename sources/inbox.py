"""Локальна папка `inbox/<проєкт>/` — для файлів з карти пам'яті.

Файл забирається, коли його розмір і час зміни не змінювались між двома
скануваннями (тобто копіювання з карти завершилось). Файл переноситься
в бібліотеку; папка з невідомою назвою створює новий проєкт.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

from core.projects import ProjectError, create_project, find_by_slug, make_slug
from sources.ingest import IGNORED_EXT, IngestError, ingest_file, media_kind

log = logging.getLogger(__name__)

STABLE_SECONDS = 5.0


class InboxScanner:
    def __init__(self, inbox: Path, library: Path):
        self.inbox = inbox
        self.library = library
        self._seen: dict[Path, tuple[int, float]] = {}

    def _project_for(self, conn: sqlite3.Connection, folder: Path) -> sqlite3.Row:
        row = find_by_slug(conn, make_slug(folder.name))
        return row if row else create_project(conn, self.library, folder.name)

    def scan(self, conn: sqlite3.Connection, now: float | None = None) -> list[int]:
        """Одне сканування. Повертає id нових медіа."""
        now = time.time() if now is None else now
        created: list[int] = []
        if not self.inbox.is_dir():
            return created
        alive: set[Path] = set()
        for folder in sorted(p for p in self.inbox.iterdir() if p.is_dir() and not p.name.startswith(".")):
            project = None
            for f in sorted(folder.rglob("*")):
                if not f.is_file() or f.name.startswith((".", "~")):
                    continue
                if f.suffix.lower() in IGNORED_EXT or media_kind(f.name) is None:
                    continue
                alive.add(f)
                try:
                    st = f.stat()
                except OSError:
                    continue
                sig = (st.st_size, st.st_mtime)
                prev = self._seen.get(f)
                self._seen[f] = sig
                if prev != sig or now - st.st_mtime < STABLE_SECONDS or st.st_size == 0:
                    continue  # ще копіюється
                try:
                    project = project or self._project_for(conn, folder)
                    res = ingest_file(conn, self.library, project, f, f.name, "inbox", move=True)
                except (IngestError, ProjectError, OSError) as exc:
                    log.warning("inbox: не вдалося прийняти %s: %s", f, exc)
                    continue
                self._seen.pop(f, None)
                if res.created:
                    created.append(res.media["id"])
        for gone in set(self._seen) - alive:
            self._seen.pop(gone, None)
        return created
