"""Аналіз музичного треку: темп, біти, сильні долі, енергія, дропи і приспіви.

- Біти — librosa.beat_track; сітка добудовується до початку і кінця треку з медіанним кроком.
- Сильна доля (початок такту 4/4) — та з 4 фаз, де сумарний удар (низи + onset) найбільший.
- Енергія біта — RMS (дБ) між сусідніми бітами + щільність onset-ів, нормовані 0–1 по треку.
- Рівень такту: low / mid / high за енергією; дроп — різкий стрибок у high; приспів — ≥ 4 high-такти поспіль.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from analysis.audio import SR, decode_audio

HOP = 512
BEATS_PER_BAR = 4
LOW, MID = 0.38, 0.68  # межі рівнів енергії (нормовано 0–1)
DROP_JUMP = 0.22


@dataclass
class TrackAnalysis:
    duration: float
    tempo: float
    beats: list[float]                 # час кожного біта, с
    downbeats: list[int]               # індекси бітів — початки тактів
    beat_energy: list[float]           # 0–1 на біт
    beat_onsets: list[float]           # щільність onset-ів на біт, 0–1
    bar_energy: list[float]            # 0–1 на такт (такт k починається з downbeats[k])
    bar_level: list[str]               # low / mid / high
    drops: list[int] = field(default_factory=list)      # індекси тактів
    choruses: list[tuple[int, int]] = field(default_factory=list)  # [початковий, кінцевий) такт

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TrackAnalysis":
        d = dict(d)
        d["choruses"] = [tuple(c) for c in d.get("choruses", [])]
        return cls(**d)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.as_dict()), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "TrackAnalysis":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    # --- зручні запити ---
    def bar_of_beat(self, i: int) -> int:
        """Індекс такту, якому належить біт i (−1 до першої сильної долі)."""
        k = int(np.searchsorted(self.downbeats, i, side="right")) - 1
        return k

    def bar_start_time(self, k: int) -> float:
        return self.beats[self.downbeats[k]]

    def level_at_beat(self, i: int) -> str:
        k = self.bar_of_beat(i)
        return self.bar_level[k] if 0 <= k < len(self.bar_level) else "low"

    def is_accent_bar(self, k: int) -> bool:
        return k in self.drops or any(a <= k < b for a, b in self.choruses)


def _norm(x: np.ndarray) -> np.ndarray:
    if x.size == 0:
        return x
    lo, hi = np.percentile(x, 5), np.percentile(x, 95)
    return np.clip((x - lo) / (hi - lo), 0, 1) if hi > lo else np.full_like(x, 0.5)


def _extend_grid(beats: np.ndarray, duration: float) -> np.ndarray:
    if len(beats) < 2:
        step = 0.5
        return np.arange(0, duration, step)
    step = float(np.median(np.diff(beats)))
    head = np.arange(beats[0] - step, -0.25 * step, -step)[::-1]
    tail = np.arange(beats[-1] + step, duration - 1e-6, step)
    return np.concatenate([np.maximum(head, 0.0), beats, tail])


def _refine(y: np.ndarray, sr: int, beats: np.ndarray, radius: float = 0.06) -> np.ndarray:
    """Притягує кожен біт до початку удару поруч — склейка точно на удар, а не на сітку.

    Початок удару — максимум зростання гучності (похідна лог-енергії, вікно ~6 мс, крок ~1,5 мс).
    Спектральний onset тут не годиться: він систематично запізнюється на 15–20 мс.
    """
    if len(beats) == 0 or y.size < 512:
        return beats
    win, hop = 256, 64
    n = 1 + (len(y) - win) // hop
    frames = np.lib.stride_tricks.as_strided(y, shape=(n, win), strides=(y.strides[0] * hop, y.strides[0]))
    energy = np.log((frames.astype(np.float64) ** 2).mean(axis=1) + 1e-10)
    rise = np.diff(energy)                        # rise[i] — між кадрами i та i+1
    times = ((np.arange(len(rise)) + 1) * hop + win / 2) / sr  # центр кадру, куди увійшов удар
    floor = np.percentile(rise, 90)
    out = beats.copy()
    for i, b in enumerate(beats):
        a, z = np.searchsorted(times, [b - radius, b + radius])
        if z > a and rise[a:z].max() > floor:
            out[i] = float(times[a + int(np.argmax(rise[a:z]))])
    return np.maximum.accumulate(out)


def analyze_signal(y: np.ndarray, sr: int = SR) -> TrackAnalysis:
    import librosa

    duration = len(y) / sr
    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=HOP)
    tempo, beat_t = librosa.beat.beat_track(onset_envelope=onset_env, sr=sr, hop_length=HOP, units="time")
    tempo = float(np.atleast_1d(tempo)[0])
    beats = _extend_grid(_refine(y, sr, np.asarray(beat_t, dtype=float)), duration)
    if len(beats) > 1:
        tempo = 60.0 / float(np.median(np.diff(beats)))

    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=HOP)[0]
    db = 20 * np.log10(np.maximum(rms, 1e-6))
    # низи для визначення сильної долі
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)
    low = S[freqs < 150].sum(axis=0)
    onsets = librosa.onset.onset_detect(onset_envelope=onset_env, sr=sr, hop_length=HOP, units="time")

    frame_t = librosa.frames_to_time(np.arange(len(db)), sr=sr, hop_length=HOP)
    edges = np.append(beats, duration)
    b_db, b_low, b_on, b_hit = [], [], [], []
    for a, b in zip(edges[:-1], edges[1:]):
        m = (frame_t >= a) & (frame_t < b)
        b_db.append(float(db[m].mean()) if m.any() else -120.0)
        b_low.append(float(low[: len(frame_t)][m].mean()) if m.any() else 0.0)
        b_on.append(float(((onsets >= a) & (onsets < b)).sum()) / max(b - a, 1e-3))
        hit = (frame_t >= a - 0.05) & (frame_t < a + 0.08)
        b_hit.append(float(onset_env[: len(frame_t)][hit].max()) if hit.any() else 0.0)
    beat_energy = _norm(np.array(b_db))
    beat_onsets = _norm(np.array(b_on))

    # фаза сильної долі
    strength = _norm(np.array(b_low)) + _norm(np.array(b_hit))
    phase = int(np.argmax([strength[p::BEATS_PER_BAR].sum() for p in range(BEATS_PER_BAR)])) if len(beats) else 0
    downbeats = list(range(phase, len(beats), BEATS_PER_BAR))

    bar_energy = []
    for k, d in enumerate(downbeats):
        end = downbeats[k + 1] if k + 1 < len(downbeats) else len(beats)
        bar_energy.append(float(np.mean(0.75 * beat_energy[d:end] + 0.25 * beat_onsets[d:end])))
    bar_energy_arr = np.array(bar_energy)
    levels = ["high" if e >= MID else "mid" if e >= LOW else "low" for e in bar_energy_arr]

    drops = [k for k in range(1, len(levels))
             if levels[k] == "high" and levels[k - 1] != "high"
             and bar_energy_arr[k] - bar_energy_arr[max(0, k - 2):k].min() >= DROP_JUMP]
    choruses, k = [], 0
    while k < len(levels):
        if levels[k] == "high":
            j = k
            while j < len(levels) and levels[j] == "high":
                j += 1
            if j - k >= 4:
                choruses.append((k, j))
            k = j
        else:
            k += 1

    return TrackAnalysis(duration=round(duration, 3), tempo=round(tempo, 2),
                         beats=[round(float(b), 4) for b in beats], downbeats=downbeats,
                         beat_energy=[round(float(e), 4) for e in beat_energy],
                         beat_onsets=[round(float(e), 4) for e in beat_onsets],
                         bar_energy=[round(float(e), 4) for e in bar_energy_arr], bar_level=levels,
                         drops=drops, choruses=choruses)


def analyze_file(path: Path) -> TrackAnalysis:
    return analyze_signal(decode_audio(path, SR), SR)
