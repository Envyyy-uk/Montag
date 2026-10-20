"""Автовибір стилю за типом події, який Gemini визначив під час оцінки.

Впевнено (одна подія, висока впевненість) — стиль ставиться сам.
Інакше — пропозиція з поясненням; кліп рендериться з нею, а користувач тисне «ок» або «змінити».
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from render.looks import list_styles

CONFIDENT_SHARE = 0.7
CONFIDENT_LEVEL = 0.7


def event_map(conn: sqlite3.Connection | None) -> dict[str, str]:
    """Твій вибір «тип події → стиль» (екран «Стилі»). Має пріоритет над вбудованими."""
    if conn is None:
        return {}
    row = conn.execute("SELECT value FROM settings WHERE key = 'event_styles'").fetchone()
    return json.loads(row["value"]) if row else {}


def style_for_event(event: str, conn: sqlite3.Connection | None = None) -> str:
    mine = event_map(conn).get(event)
    if mine and any(s.id == mine and not s.data.get("draft") for s in list_styles()):
        return mine
    for s in list_styles():
        if not s.data.get("draft") and event in s.data.get("event_types", []):
            return s.id
    return "clean"


def detect(conn: sqlite3.Connection, project_id: int) -> dict | None:
    weight: dict[str, float] = defaultdict(float)
    conf: dict[str, list[float]] = defaultdict(list)
    for r in conn.execute("SELECT event_type, event_confidence FROM gemini_requests"
                          " WHERE project_id = ? AND status = 'done' AND event_type IS NOT NULL", (project_id,)):
        weight[r["event_type"]] += r["event_confidence"] or 0.5
        conf[r["event_type"]].append(r["event_confidence"] or 0.5)
    if not weight:
        return None
    total = sum(weight.values())
    event = max(weight, key=weight.get)
    share = weight[event] / total
    level = sum(conf[event]) / len(conf[event])
    sure = share >= CONFIDENT_SHARE and level >= CONFIDENT_LEVEL
    style_id = style_for_event(event, conn)
    others = sorted((e for e in weight if e != event), key=lambda e: -weight[e])
    if sure:
        reason = f"Схоже на «{event}» (впевненість {level:.0%}) — стиль поставлено автоматично."
    elif share < CONFIDENT_SHARE:
        reason = (f"Матеріал змішаний: «{event}» {share:.0%}" +
                  (f", також «{others[0]}»" if others else "") + ". Пропоную цей стиль — ок чи змінити?")
    else:
        reason = f"Схоже на «{event}», але впевненість лише {level:.0%}. Пропоную цей стиль — ок чи змінити?"
    return {"style_id": style_id, "event_type": event, "share": round(share, 2), "confidence": round(level, 2),
            "confirmed": sure, "reason": reason}


def apply(conn: sqlite3.Connection, project_id: int) -> dict | None:
    """Записує автовибір у налаштування проєкту (стиль, обраний користувачем, не чіпає)."""
    found = detect(conn, project_id)
    if found is None:
        return None
    row = conn.execute("SELECT settings FROM projects WHERE id = ?", (project_id,)).fetchone()
    stored = json.loads(row["settings"] or "{}")
    stored["style_auto"] = found
    conn.execute("UPDATE projects SET settings = ? WHERE id = ?", (json.dumps(stored, ensure_ascii=False), project_id))
    return found
