"""Бюджет Gemini: вартість за токенами, витрати за місяць, резерв, прогноз, ліміти.

Захист від перевитрат навіть при багатьох проєктах у черзі: вже надіслані, але
ще не оплачені запити резервують свою прогнозну вартість (`est_cost`).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

PRICING_PATH = Path(__file__).with_name("pricing.json")


@lru_cache
def pricing() -> dict:
    return json.loads(PRICING_PATH.read_text(encoding="utf-8"))


def model_rates(model: str) -> dict[str, float]:
    models = pricing()["models"]
    name = (model or "").removeprefix("models/")
    if name in models:
        return models[name]
    # «gemini-3.6-flash-001» → «gemini-3.6-flash»
    for key in sorted((k for k in models if not k.startswith("_")), key=len, reverse=True):
        if name.startswith(key):
            return models[key]
    return models["_default"]


def usage_cost(model: str, usage: dict, batch: bool) -> float:
    """Вартість за `usage_metadata` відповіді. Роздуми оплачуються як вихід."""
    r = model_rates(model)
    prompt = int(usage.get("prompt_token_count") or 0)
    audio = sum(int(d.get("token_count") or 0) for d in usage.get("prompt_tokens_details") or []
                if str(d.get("modality", "")).upper().endswith("AUDIO"))
    output = int(usage.get("candidates_token_count") or 0) + int(usage.get("thoughts_token_count") or 0)
    cost = ((prompt - audio) * r["input"] + audio * r["audio_input"] + output * r["output"]) / 1e6
    if batch:
        cost *= pricing()["batch_discount"]
    return round(cost, 9)


def estimate_request(model: str, video_seconds: float, has_audio: bool, prompt_chars: int,
                     example_images: int, segments: int, batch: bool, fps: float = 1.0) -> float:
    """Прогноз вартості одного запиту з запасом `safety_margin`."""
    t = pricing()["tokens"]
    r = model_rates(model)
    video = video_seconds * fps * t["video_frame_low"]
    audio = video_seconds * t["audio_per_second"] if has_audio else 0
    text = prompt_chars / 3  # кирилиця щільніша за англійську — консервативно
    images = example_images * t["image"]
    output = t["output_base"] + segments * t["output_per_segment"] + t["thinking_per_request"]
    cost = ((video + text + images) * r["input"] + audio * r["audio_input"] + output * r["output"]) / 1e6
    if batch:
        cost *= pricing()["batch_discount"]
    return round(cost * t["safety_margin"], 9)


def month_start_utc(now: datetime | None = None) -> str:
    """Початок поточного календарного місяця (місцевий час) у форматі часу бази (UTC)."""
    local = (now or datetime.now()).astimezone()
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def spent_this_month(conn: sqlite3.Connection, now: datetime | None = None) -> float:
    return float(conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM spend WHERE created_at >= ?",
                              (month_start_utc(now),)).fetchone()[0])


def reserved(conn: sqlite3.Connection) -> float:
    """Прогнозна вартість запитів, які надіслані, але ще не оплачені."""
    return float(conn.execute("SELECT COALESCE(SUM(est_cost), 0) FROM gemini_requests"
                              " WHERE status IN ('pending', 'submitted')").fetchone()[0])


def requests_today(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM gemini_requests WHERE created_at >= strftime('%Y-%m-%dT00:00:00', 'now')"
                            ).fetchone()[0])


@dataclass
class BudgetState:
    limit: float
    spent: float
    reserved: float
    requests_today: int
    daily_limit: int

    @property
    def available(self) -> float:
        return max(0.0, self.limit - self.spent - self.reserved)

    @property
    def requests_left(self) -> int:
        return max(0, self.daily_limit - self.requests_today)

    def as_dict(self) -> dict:
        return {"limit": round(self.limit, 2), "spent": round(self.spent, 4), "reserved": round(self.reserved, 4),
                "available": round(self.available, 4), "requests_today": self.requests_today,
                "daily_limit": self.daily_limit}


def state(conn: sqlite3.Connection, monthly_limit: float, daily_limit: int) -> BudgetState:
    return BudgetState(monthly_limit, spent_this_month(conn), reserved(conn), requests_today(conn), daily_limit)


def record_spend(conn: sqlite3.Connection, project_id: int | None, model: str, purpose: str,
                 usage: dict, batch: bool, request_id: int | None = None) -> float:
    cost = usage_cost(model, usage, batch)
    output = int(usage.get("candidates_token_count") or 0) + int(usage.get("thoughts_token_count") or 0)
    conn.execute(
        "INSERT INTO spend(project_id, model, purpose, input_tokens, output_tokens, cost_usd, batch, request_id)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (project_id, model, purpose, int(usage.get("prompt_token_count") or 0), output, cost, int(batch), request_id))
    return cost


def history(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT s.created_at, s.model, s.purpose, s.input_tokens, s.output_tokens, s.cost_usd, s.batch,"
        " p.name AS project FROM spend s LEFT JOIN projects p ON p.id = s.project_id"
        " ORDER BY s.id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]
