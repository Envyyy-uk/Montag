"""Сітка склейок за енергією треку.

Низька енергія — склейка раз на такт (4 біти), середня — раз на 2 біти,
дроп і приспів (висока) — кожен біт. Склейки рівно на біти вікна.
"""

from __future__ import annotations

from dataclasses import dataclass

from analysis.track import TrackAnalysis
from edit.track_window import Window

DEFAULT_BEATS = {"low": 4, "mid": 2, "high": 1}


@dataclass
class Slot:
    first_beat: int
    last_beat: int    # не входить
    start: float      # від початку вікна
    end: float
    level: str
    accent: bool

    @property
    def beats(self) -> int:
        return self.last_beat - self.first_beat


def slots(track: TrackAnalysis, win: Window, beats_per_level: dict[str, int] | None = None) -> list[Slot]:
    per = {**DEFAULT_BEATS, **(beats_per_level or {})}
    times = track.beats + [track.duration]
    out: list[Slot] = []
    i = win.first_beat
    while i < win.last_beat:
        level = track.level_at_beat(i)
        n = max(1, int(per[level]))
        # не переступати початок такту з іншим рівнем — нова енергія починає новий ритм
        bar = track.bar_of_beat(i)
        next_bar_beat = track.downbeats[bar + 1] if 0 <= bar + 1 < len(track.downbeats) else win.last_beat
        j = min(i + n, win.last_beat)
        if bar >= 0 and next_bar_beat < j and track.level_at_beat(next_bar_beat) != level:
            j = next_bar_beat
        accent = bar >= 0 and track.is_accent_bar(bar) and level == "high"
        out.append(Slot(i, j, times[i] - win.start, times[j] - win.start, level, accent))
        i = j
    return out
