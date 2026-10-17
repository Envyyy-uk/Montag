"""Миттєвий (не batch) запит у Gemini — лише для правок словами і стилів словами.

Відповідь перевіряється за JSON-схемою; невалідна — одна повторна спроба. Кожен запит
проходить через бюджет: місячний ліміт, резерв, денний ліміт; вартість записується.
"""

from __future__ import annotations

import json
import logging
import sqlite3

from core import budget
from core.config import Settings

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    pass


def ask_json(conn: sqlite3.Connection, settings: Settings, prompt: str, schema: dict, purpose: str,
             project_id: int | None = None, attempts: int = 2) -> dict:
    """Повертає перевірений JSON або кидає LLMUnavailable (немає ключа, бюджету чи валідної відповіді)."""
    import jsonschema

    from analysis.scoring import get_backend

    backend = get_backend(settings)
    if backend is None:
        raise LLMUnavailable("ключ Gemini не задано")
    est = budget.estimate_request(settings.gemini_model, 0, False, len(prompt), 0, 4, batch=False)
    st = budget.state(conn, settings.monthly_budget_usd, settings.daily_request_limit)
    if st.available < est or st.requests_left <= 0:
        raise LLMUnavailable("місячний або денний ліміт Gemini вичерпано")
    cfg = {"response_mime_type": "application/json", "response_json_schema": schema, "temperature": 0.2}
    if settings.gemini_thinking_level:
        cfg["thinking_config"] = {"thinking_level": settings.gemini_thinking_level}
    last_error = ""
    for _ in range(attempts):
        res = backend.generate(settings.gemini_model, [{"role": "user", "parts": [{"text": prompt}]}], cfg)
        if res.usage:
            budget.record_spend(conn, project_id, res.model_version or settings.gemini_model, purpose, res.usage,
                                batch=False)
        if res.error:
            last_error = res.error
            continue
        try:
            data = json.loads(res.text or "")
            jsonschema.validate(data, schema)
            return data
        except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
            last_error = f"невалідна відповідь: {exc}"
    raise LLMUnavailable(last_error or "Gemini не відповів")
