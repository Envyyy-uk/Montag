"""Мова: faster-whisper на CUDA (або CPU), транскрипт і субтитри .srt."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_model_lock = threading.Lock()
_models: dict[tuple[str, str], object] = {}


def _device() -> tuple[str, str]:
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda", "float16"
    except Exception:  # noqa: BLE001
        pass
    return "cpu", "int8"


def get_model(name: str):
    from faster_whisper import WhisperModel

    device, compute = _device()
    key = (name, device)
    with _model_lock:
        if key not in _models:
            log.info("whisper: завантаження моделі %s на %s", name, device)
            _models[key] = WhisperModel(name, device=device, compute_type=compute)
        return _models[key]


def transcribe(audio_16k: np.ndarray, model_name: str = "small", language: str = "", model=None) -> dict:
    model = model or get_model(model_name)
    segments, info = model.transcribe(audio_16k, language=language or None, vad_filter=True,
                                      word_timestamps=True, condition_on_previous_text=False)
    out = []
    for s in segments:
        out.append({
            "start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(),
            "words": [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word}
                      for w in (s.words or [])],
        })
    return {"language": info.language, "language_probability": round(info.language_probability, 3),
            "segments": out}


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def to_srt(transcript: dict) -> str:
    blocks = []
    for i, s in enumerate((s for s in transcript["segments"] if s["text"]), 1):
        blocks.append(f"{i}\n{_ts(s['start'])} --> {_ts(s['end'])}\n{s['text']}\n")
    return "\n".join(blocks)


def save(transcript: dict, json_path: Path, srt_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=1), encoding="utf-8")
    srt_path.write_text(to_srt(transcript), encoding="utf-8")
