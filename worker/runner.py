"""Фоновий воркер: бере завдання з черги по одному, сканує inbox, ставить проєкти на монтаж."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Callable

from core.config import Settings
from core.db import open_db
from sources.inbox import InboxScanner
from worker import queue, stages

log = logging.getLogger(__name__)

Handler = Callable[..., str | None]


def default_handlers() -> dict[str, Handler]:
    from analysis import scoring
    from edit import build, davinci, verticals
    from sources import gdrive, music

    return {"prepare_media": stages.prepare_media, "analyze_media": stages.analyze_media,
            "analyze_track": stages.analyze_track,
            "score_project": scoring.score_project, "poll_batch": scoring.poll_batch,
            "edit_project": build.edit_project, "style_test": stages.style_test,
            "export_davinci": davinci.export_davinci, "vertical_project": verticals.vertical_project,
            "drive_fetch": gdrive.drive_fetch, "drive_create_folder": gdrive.drive_create_folder,
            "drive_upload": gdrive.drive_upload,
            "analyze_music": music.analyze_music, "drive_music_fetch": music.drive_music_fetch}


class Worker:
    def __init__(self, settings: Settings, handlers: dict[str, Handler] | None = None,
                 poll_seconds: float = 2.0):
        from render.looks import register_user_dir

        self.settings = settings
        register_user_dir(settings.user_styles_path)
        self.handlers = handlers if handlers is not None else default_handlers()
        self.poll_seconds = poll_seconds
        self.inbox = InboxScanner(settings.inbox_path, settings.library_path)
        from sources.gdrive import DrivePoller
        self.drive = DrivePoller(settings)
        self._last_drive_poll = 0.0
        self._last_music_scan = 0.0
        from core.power import KeepAwake
        self.awake = KeepAwake()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_housekeeping = 0.0

    def housekeeping(self, conn) -> None:
        self.inbox.scan(conn)
        if time.monotonic() - self._last_drive_poll >= self.settings.drive_poll_seconds:
            self._last_drive_poll = time.monotonic()
            try:
                self.drive.poll(conn)
            except Exception as exc:  # noqa: BLE001 — без інтернету Drive просто чекає
                log.warning("Google Drive недоступний: %s", exc)
        if time.monotonic() - self._last_music_scan >= 60:
            self._last_music_scan = time.monotonic()
            from sources import music
            try:
                music.scan(conn, self.settings)  # нові треки в music/ → аналіз
            except OSError as exc:
                log.warning("папка музики: %s", exc)
        stages.auto_queue_idle(conn, self.settings.idle_minutes_before_edit)
        # поки в черзі є робота — Windows не засинає; порожня черга — звичайна поведінка
        busy = conn.execute("SELECT 1 FROM jobs WHERE status IN ('queued', 'running') LIMIT 1").fetchone()
        self.awake.update(busy is not None)

    def run_one(self, conn) -> bool:
        """Виконує одне завдання. False — черга порожня."""
        job = queue.claim(conn, self.handlers)
        if job is None:
            return False
        handler = self.handlers[job["kind"]]
        log.info("завдання %s (%s) почалось", job["id"], job["kind"])
        from core.config import with_overrides
        try:
            result = handler(conn, with_overrides(self.settings, conn), queue.payload(job),
                             lambda p: queue.set_progress(conn, job["id"], p),
                             **({"job_id": job["id"]} if getattr(handler, "wants_job_id", False) else {}))
        except Exception as exc:  # noqa: BLE001 — будь-яка помилка етапу не валить воркер
            if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job["id"],)).fetchone() is None:
                log.info("завдання %s скасовано", job["id"])  # його прибрали з черги під час роботи
                return True
            if self._stop.is_set():  # програму зупиняють — це не помилка етапу
                queue.requeue_interrupted(conn, job["id"])
                log.info("завдання %s перервано зупинкою — продовжиться після запуску", job["id"])
                return True
            status = queue.fail(conn, job["id"], f"{exc}\n{traceback.format_exc(limit=5)}")
            log.error("завдання %s впало (%s): %s", job["id"], status, exc)
        else:
            if result == queue.KEEP_STATUS:
                log.info("завдання %s чекає підтвердження", job["id"])
            else:
                queue.finish(conn, job["id"])
                log.info("завдання %s готове", job["id"])
        return True

    def loop(self) -> None:
        conn = open_db(self.settings.database_path)
        recovered = queue.recover(conn)
        if recovered:
            log.info("відновлено %s перерваних завдань", recovered)
        try:
            while not self._stop.is_set():
                if time.monotonic() - self._last_housekeeping > 5:
                    self._last_housekeeping = time.monotonic()
                    try:
                        self.housekeeping(conn)
                    except Exception:  # noqa: BLE001
                        log.exception("housekeeping")
                if not self.run_one(conn):
                    self._stop.wait(self.poll_seconds)
        finally:
            self.awake.update(False)  # воркер зупиняється — сон знову дозволено
            conn.close()

    def start(self) -> None:
        self._thread = threading.Thread(target=self.loop, name="worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        from render.ffmpeg import terminate_all
        if terminate_all():
            log.info("зупиняю рендер — завдання продовжиться після запуску")
        if self._thread:
            self._thread.join(timeout)
