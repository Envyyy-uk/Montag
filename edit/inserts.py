"""Живі вставки: шматки відео з власним звуком натовпу поверх приглушеного треку.

Правила: кандидати — сплески натовпу ≥ 0,5 с; тривалість вставки 1–3 с (ціла кількість бітів);
пауза між вставками ≥ 8 с; не більше 4 на хвилину; початок — на сильній долі.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from analysis.track import TrackAnalysis
from edit.track_window import Window

MIN_EVENT = 0.5
MIN_LEN, MAX_LEN = 1.0, 3.0
MIN_GAP = 8.0
PER_MINUTE = 4
EDGE = 2.0      # не ставити впритул до початку/кінця кліпу
LEAD = 0.3      # трохи до сплеску, щоб чути наростання


@dataclass
class CrowdEvent:
    media_id: int
    start: float
    end: float
    peak_db: float
    chrono: float = 0.0
    media_duration: float = 0.0


@dataclass
class PlacedInsert:
    first_beat: int
    last_beat: int
    start: float      # від початку вікна
    end: float
    event: CrowdEvent
    src_in: float


def plan(track: TrackAnalysis, win: Window, events: list[CrowdEvent], max_count: int | None = None,
         min_gap: float = MIN_GAP) -> list[PlacedInsert]:
    events = [e for e in events if e.end - e.start >= MIN_EVENT]
    if not events:
        return []
    beats = np.asarray(track.beats + [track.duration])
    beat_len = float(np.median(np.diff(track.beats))) if len(track.beats) > 1 else 0.5
    min_beats = max(1, math.ceil(MIN_LEN / beat_len - 1e-6))
    max_beats = max(min_beats, math.floor(MAX_LEN / beat_len + 1e-6))
    limit = math.floor(PER_MINUTE * win.duration / 60 + 1e-6)
    if max_count is not None:
        limit = min(limit, max_count)
    if limit <= 0:
        return []

    downs = [d for d in track.downbeats if win.first_beat <= d < win.last_beat] or list(range(win.first_beat, win.last_beat))
    bar_e = {d: track.bar_energy[k] for k, d in enumerate(track.downbeats)}

    drop_beats = {track.downbeats[k] for k in track.drops if k < len(track.downbeats)}

    def pos_score(d: int, e_beat: int) -> float:
        # межі секцій (стрибок енергії) — природне місце для «вдиху» натовпу
        k = track.bar_of_beat(d)
        prev = track.bar_energy[k - 1] if k > 0 else bar_e.get(d, 0)
        v = abs(bar_e.get(d, 0) - prev)
        if any(d <= b < e_beat for b in drop_beats):
            v -= 1.0   # не глушити трек на самому дропі
        if e_beat in drop_beats:
            v += 0.5   # натовп «розгойдує» — і тут вдаряє дроп
        return v

    placed: list[PlacedInsert] = []
    for ev in sorted(events, key=lambda e: -e.peak_db):
        if len(placed) >= limit:
            break
        n_beats = int(np.clip(round((ev.end - ev.start + LEAD) / beat_len), min_beats, max_beats))
        best, best_v = None, -1e9
        for d in downs:
            e_beat = d + n_beats
            if e_beat > win.last_beat:
                continue
            s, e = beats[d] - win.start, beats[e_beat] - win.start
            if s < EDGE or e > win.duration - EDGE / 2:
                continue
            if any(s < p.end + min_gap and e > p.start - min_gap for p in placed):
                continue
            pos = (s + e) / 2 / win.duration
            v = pos_score(d, e_beat) - 0.5 * abs(ev.chrono - pos)
            if v > best_v:
                best, best_v = (d, e_beat, s, e), v
        if best is None:
            continue
        d, e_beat, s, e = best
        dur = e - s
        src_in = max(0.0, ev.start - LEAD)
        if ev.media_duration:
            src_in = min(src_in, max(0.0, ev.media_duration - dur))
        placed.append(PlacedInsert(d, e_beat, float(s), float(e), ev, round(src_in, 4)))
    return sorted(placed, key=lambda p: p.start)
