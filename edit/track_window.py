"""Найенергійніший шматок треку заданої довжини, межі по початку такту."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from analysis.track import TrackAnalysis


@dataclass(frozen=True)
class Window:
    start: float
    end: float
    first_beat: int   # індекс біта на початку
    last_beat: int    # індекс біта на кінці (не входить)

    @property
    def duration(self) -> float:
        return self.end - self.start


def _beat_at(track: TrackAnalysis, t: float) -> int:
    return int(np.argmin(np.abs(np.asarray(track.beats) - t)))


def best_window(track: TrackAnalysis, length: float = 60.0) -> Window:
    """Перебирає старти на сильних долях; кінець — сильна доля, найближча до start+length.
    Оцінка: 70% середня енергія бітів + 30% щільність onset-ів."""
    beats = np.asarray(track.beats)
    energy = 0.7 * np.asarray(track.beat_energy) + 0.3 * np.asarray(track.beat_onsets)
    downs = track.downbeats
    if not downs:
        return Window(0.0, min(length, track.duration), 0, len(beats))
    down_times = beats[downs]
    best: tuple[float, int, int] | None = None
    for i, d in enumerate(downs):
        target = beats[d] + length
        if target > track.duration + 1e-6 and best is not None:
            break
        j = int(np.argmin(np.abs(down_times - target)))
        end_beat = downs[j] if j > i else len(beats)
        if end_beat <= d:
            continue
        score = float(energy[d:end_beat].mean())
        if best is None or score > best[0] + 1e-9:
            best = (score, d, end_beat)
    _, a, b = best
    end = float(beats[b]) if b < len(beats) else track.duration
    return Window(float(beats[a]), end, a, b)


def window_from_bounds(track: TrackAnalysis, start: float, end: float) -> Window:
    """Межі, задані користувачем, притягуються лише до найближчої долі (≤ півдолі), а не до такту —
    інакше вибраний шматок «стрибав» на секунду."""
    beats = np.asarray(track.beats)
    a = int(np.argmin(np.abs(beats - start)))
    b = int(np.argmin(np.abs(beats - end)))
    if b <= a:
        b = min(len(beats), a + 4)
    end_t = float(beats[b]) if b < len(beats) else track.duration
    if abs(end - track.duration) < abs(end - end_t):  # до кінця пісні — так і лишаємо
        end_t, b = track.duration, len(beats)
    return Window(float(beats[a]), end_t, a, b)
