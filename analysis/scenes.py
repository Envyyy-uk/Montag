"""Межі сцен (PySceneDetect) і нарізка на шматки для оцінки."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

MAX_PIECE = 6.0  # довші сцени ріжуться
PIECE_MAX = 4.0  # на шматки 2–4 с


@dataclass(frozen=True)
class Piece:
    scene_idx: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def detect_scenes(path: Path, duration: float, fps: float) -> list[tuple[float, float]]:
    """AdaptiveDetector стійкіший до спалахів сценічного світла і ручної зйомки."""
    from scenedetect import AdaptiveDetector, detect

    min_len = max(2, int(round((fps or 15) * 0.6)))
    scenes = detect(str(path), AdaptiveDetector(min_scene_len=min_len))
    secs = lambda tc: tc.seconds if hasattr(type(tc), "seconds") else tc.get_seconds()  # noqa: E731
    result = [(secs(s), secs(e)) for s, e in scenes]
    if not result and duration > 0:
        result = [(0.0, duration)]  # без склейок — одна сцена
    return result


def split_scene(scene_idx: int, start: float, end: float) -> list[Piece]:
    """Сцена ≤ 6 с лишається цілою, довша ділиться порівну на шматки 2–4 с."""
    length = end - start
    if length <= MAX_PIECE:
        return [Piece(scene_idx, start, end)]
    n = math.ceil(length / PIECE_MAX)
    step = length / n
    return [Piece(scene_idx, round(start + i * step, 3), round(start + (i + 1) * step if i < n - 1 else end, 3))
            for i in range(n)]


def pieces(scenes: list[tuple[float, float]]) -> list[Piece]:
    out: list[Piece] = []
    for idx, (s, e) in enumerate(scenes):
        out.extend(split_scene(idx, s, e))
    return out
