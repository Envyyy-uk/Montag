"""Миттєвий (не batch) запит у Gemini — лише для правок словами і стилів словами.

Відповідь перевіряється за JSON-схемою; невалідна — одна повторна спроба. Кожен запит
проходить через бюджет: місячний ліміт, резерв, денний ліміт; вартість записується.

Якщо Gemini перевантажений (503, 429, 500) — ще 3 спроби через 2, 5 і 15 с; останні дві —
на запасній моделі (GEMINI_FALLBACK_MODEL), бо перевантаження зазвичай стосується однієї моделі.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time

from core import budget
from core.config import Settings

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    def __init__(self, message: str, overloaded: bool = False):
        super().__init__(message)
        self.overloaded = overloaded


RETRY_DELAYS = (2, 5, 15)
# (пауза, модель): спершу основна, потім ще раз основна, далі — запасна
PATIENT = ((0, "main"), (2, "main"), (5, "fallback"), (15, "fallback"))
QUICK = ((0, "main"), (0, "fallback"))  # для фонових повторів — без пауз, наступна спроба за хвилину
_TRANSIENT = re.compile(r"\b(429|500|503|504)\b|UNAVAILABLE|RESOURCE_EXHAUSTED|DEADLINE_EXCEEDED|overloaded"
                        r"|timed out", re.IGNORECASE)
_sleep = time.sleep  # у тестах підміняється


def is_overloaded(error: str) -> bool:
    return bool(_TRANSIENT.search(error))


def _call(backend, models: list[str], contents: list, cfg: dict, plan=PATIENT):
    """Один запит із повторами при перевантаженні. Повертає (результат, модель)."""
    res, model = None, models[0]
    for delay, which in plan:
        model = models[-1] if which == "fallback" else models[0]
        if delay:
            log.info("Gemini перевантажений — повтор через %s с (%s)", delay, model)
            _sleep(delay)
        res = backend.generate(model, contents, cfg)
        if not (res.error and is_overloaded(res.error)):
            return res, model
    return res, model


def ask_json(conn: sqlite3.Connection, settings: Settings, prompt: str, schema: dict, purpose: str,
             project_id: int | None = None, attempts: int = 2, plan=PATIENT) -> dict:
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
    models = [settings.gemini_model]
    if settings.gemini_fallback_model and settings.gemini_fallback_model != settings.gemini_model:
        models.append(settings.gemini_fallback_model)
    last_error = ""
    for _ in range(attempts):
        res, model = _call(backend, models, [{"role": "user", "parts": [{"text": prompt}]}], cfg, plan)
        if res.usage:
            budget.record_spend(conn, project_id, res.model_version or model, purpose, res.usage, batch=False)
        if res.error:
            if is_overloaded(res.error):
                log.warning("Gemini перевантажений після всіх повторів: %s", res.error[:300])
                raise LLMUnavailable("Gemini зараз перевантажений", overloaded=True)
            last_error = res.error[:200]
            continue
        try:
            data = json.loads(res.text or "")
            jsonschema.validate(data, schema)
            return data
        except (json.JSONDecodeError, jsonschema.ValidationError) as exc:
            last_error = f"невалідна відповідь: {exc}"
    raise LLMUnavailable(last_error or "Gemini не відповів")
