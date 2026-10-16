"""Який стиль і рівень інтенсивності застосувати; затверджені рівні зберігаються в базі."""

from __future__ import annotations

import json
import sqlite3

from core.config import Settings
from render.looks import Style, StyleError, get_style

KEY = "style_levels"


def approved_levels(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (KEY,)).fetchone()
    return {k: int(v) for k, v in json.loads(row["value"]).items()} if row else {}


def approve(conn: sqlite3.Connection, style_id: str, level: int) -> None:
    get_style(style_id)
    if level not in (1, 2, 3):
        raise StyleError("Рівень інтенсивності — 1, 2 або 3")
    levels = approved_levels(conn)
    levels[style_id] = level
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (KEY, json.dumps(levels)))


def resolve(conn: sqlite3.Connection, settings: Settings, style_id: str | None) -> tuple[Style, int]:
    try:
        style = get_style(style_id or settings.default_style)
    except StyleError:
        style = get_style("clean")
    return style, approved_levels(conn).get(style.id, style.default_level)
