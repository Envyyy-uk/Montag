"""Заборона сну Windows, поки в черзі є завдання.

SetThreadExecutionState діє, доки живий потік, що його викликав, тому ставиться і знімається
з потоку воркера. На інших системах — нічого не робить.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


class KeepAwake:
    def __init__(self, setter=None):
        self.active = False
        if setter is not None:
            self._set = setter
        elif sys.platform == "win32":
            import ctypes
            self._set = ctypes.windll.kernel32.SetThreadExecutionState  # type: ignore[attr-defined]
        else:
            self._set = None

    def update(self, busy: bool) -> None:
        """busy=True — не засинати (екран може гаснути); False — звичайна поведінка."""
        if busy == self.active:
            return
        self.active = busy
        if self._set is None:
            return
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED if busy else ES_CONTINUOUS
        if not self._set(flags):
            log.warning("SetThreadExecutionState не спрацював")
        log.info("сон Windows %s", "заборонено — у черзі є завдання" if busy else "дозволено — черга порожня")
