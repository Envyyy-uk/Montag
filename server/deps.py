"""Залежності FastAPI: налаштування і з'єднання з базою на запит."""

from __future__ import annotations

import sqlite3
from typing import Iterator

from fastapi import Request

from core.config import Settings
from core.db import connect


def settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def db(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.settings.database_path)
    try:
        yield conn
    finally:
        conn.close()
