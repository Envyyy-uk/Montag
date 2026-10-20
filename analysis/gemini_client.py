"""Тонка обгортка над google-genai: Files API, Batch API, миттєвий запит.

Решта коду працює з `GeminiBackend` і словниками, тож у тестах його
замінює фейк, а реальні запити (і гроші) йдуть лише з ноутбука з ключем.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

TERMINAL_OK = {"JOB_STATE_SUCCEEDED", "JOB_STATE_PARTIALLY_SUCCEEDED"}
TERMINAL_BAD = {"JOB_STATE_FAILED", "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"}


@dataclass
class UploadedFile:
    uri: str
    name: str
    expires_at: str | None  # ISO UTC


@dataclass
class Result:
    text: str | None = None
    usage: dict = field(default_factory=dict)
    model_version: str = ""
    error: str | None = None


@dataclass
class BatchStatus:
    state: str
    results: dict[str, Result] = field(default_factory=dict)  # ключ запиту → результат
    error: str | None = None

    @property
    def done(self) -> bool:
        return self.state in TERMINAL_OK | TERMINAL_BAD


class GeminiBackend(Protocol):
    def upload(self, path: Path, mime_type: str) -> UploadedFile: ...
    def submit_batch(self, model: str, requests: list[dict], display_name: str) -> str: ...
    def get_batch(self, name: str) -> BatchStatus: ...
    def generate(self, model: str, contents: list, config: dict) -> Result: ...


def _usage(obj) -> dict:
    if obj is None:
        return {}
    data = obj.model_dump(mode="json", exclude_none=True) if hasattr(obj, "model_dump") else dict(obj)
    return data


def _result_from_response(resp) -> Result:
    try:
        text = resp.text
    except Exception as exc:  # noqa: BLE001 — заблокована/порожня відповідь
        return Result(error=f"порожня відповідь: {exc}", usage=_usage(getattr(resp, "usage_metadata", None)))
    return Result(text=text, usage=_usage(resp.usage_metadata), model_version=resp.model_version or "")


class GenaiBackend:
    def __init__(self, api_key: str, upload_timeout: float = 600):
        from google import genai

        self.client = genai.Client(api_key=api_key)
        self.upload_timeout = upload_timeout

    def upload(self, path: Path, mime_type: str) -> UploadedFile:
        f = self.client.files.upload(file=str(path), config={"mime_type": mime_type, "display_name": path.name})
        deadline = time.monotonic() + self.upload_timeout
        while f.state and f.state.name == "PROCESSING":
            if time.monotonic() > deadline:
                raise TimeoutError(f"Gemini надто довго обробляє {path.name}")
            time.sleep(3)
            f = self.client.files.get(name=f.name)
        if f.state and f.state.name == "FAILED":
            raise RuntimeError(f"Gemini не прийняв файл {path.name}: {f.error}")
        expires = f.expiration_time.isoformat() if f.expiration_time else None
        return UploadedFile(uri=f.uri, name=f.name, expires_at=expires)

    def submit_batch(self, model: str, requests: list[dict], display_name: str) -> str:
        src = [{"contents": r["contents"], "config": r["config"], "metadata": {"key": r["key"]}} for r in requests]
        job = self.client.batches.create(model=model, src=src, config={"display_name": display_name})
        return job.name

    def cancel_batch(self, name: str) -> None:
        self.client.batches.cancel(name=name)

    def get_batch(self, name: str) -> BatchStatus:
        job = self.client.batches.get(name=name)
        state = job.state.name if job.state else "JOB_STATE_UNSPECIFIED"
        status = BatchStatus(state=state, error=str(job.error) if job.error else None)
        if state in TERMINAL_OK and job.dest and job.dest.inlined_responses:
            for i, item in enumerate(job.dest.inlined_responses):
                key = (item.metadata or {}).get("key", str(i))
                if item.error:
                    status.results[key] = Result(error=str(item.error))
                else:
                    status.results[key] = _result_from_response(item.response)
        return status

    def generate(self, model: str, contents: list, config: dict) -> Result:
        try:
            resp = self.client.models.generate_content(model=model, contents=contents, config=config)
        except Exception as exc:  # noqa: BLE001
            return Result(error=str(exc))
        return _result_from_response(resp)
