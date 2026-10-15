"""Звук відео: гучність у часі і сплески натовпу.

Сплеск — ділянка ≥ 0,5 с, де гучність вища за «фон» (ковзна медіана ~8 с)
щонайменше на `burst_db`. Шумоподібний сплеск (висока спектральна пласкість:
крики, оплески, хор натовпу) позначається `crowd`, тональний — `loud`.
"""

from __future__ import annotations

import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

SR = 22050
HOP = 512
ENVELOPE_HZ = 10


@dataclass
class AudioEvent:
    kind: str
    start: float
    end: float
    peak_excess_db: float
    peak_db: float
    flatness: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class AudioAnalysis:
    envelope_db: list[float]   # гучність з частотою ENVELOPE_HZ
    events: list[AudioEvent]
    mean_db: float

    def db_between(self, start: float, end: float) -> float | None:
        a, b = int(start * ENVELOPE_HZ), max(int(start * ENVELOPE_HZ) + 1, int(end * ENVELOPE_HZ))
        part = self.envelope_db[a:b]
        return round(float(np.mean(part)), 2) if part else None


def decode_audio(path: Path, sr: int = SR) -> np.ndarray:
    """Моно float32 через ffmpeg (працює з будь-яким контейнером, без бекендів librosa)."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
           "-vn", "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-500:])
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    if not mask.any():
        return []
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)))


def analyze_signal(y: np.ndarray, sr: int = SR, burst_db: float = 6.0, min_len: float = 0.5,
                   merge_gap: float = 0.3, floor_db: float = -45.0, crowd_flatness: float = 0.05,
                   baseline_s: float = 8.0) -> AudioAnalysis:
    import librosa
    from scipy.ndimage import median_filter, uniform_filter1d

    if y.size < HOP * 2:
        return AudioAnalysis([], [], -120.0)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=HOP)[0]
    flat = librosa.feature.spectral_flatness(y=y, n_fft=2048, hop_length=HOP)[0]
    db = 20 * np.log10(np.maximum(rms, 1e-6))
    fps = sr / HOP
    smooth = uniform_filter1d(db, size=max(1, int(fps * 0.2)), mode="nearest")
    base_len = max(3, int(fps * baseline_s)) | 1
    baseline = median_filter(smooth, size=base_len, mode="nearest")
    excess = smooth - baseline

    mask = (excess >= burst_db) & (smooth > floor_db)
    # злити сусідні ділянки з короткою паузою
    gap = int(merge_gap * fps)
    runs = _runs(mask)
    merged: list[list[int]] = []
    for a, b in runs:
        if merged and a - merged[-1][1] <= gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    events = []
    for a, b in merged:
        if (b - a) / fps < min_len:
            continue
        fl = float(np.mean(flat[a:b]))
        events.append(AudioEvent(
            kind="crowd" if fl >= crowd_flatness else "loud",
            start=round(a / fps, 3), end=round(b / fps, 3),
            peak_excess_db=round(float(excess[a:b].max()), 2),
            peak_db=round(float(smooth[a:b].max()), 2),
            flatness=round(fl, 4)))

    step = fps / ENVELOPE_HZ
    n_env = int(len(db) / step)
    envelope = [round(float(np.mean(db[int(i * step):max(int(i * step) + 1, int((i + 1) * step))])), 2)
                for i in range(n_env)]
    return AudioAnalysis(envelope, events, round(float(np.mean(db)), 2))


def analyze_file(path: Path, **kw) -> AudioAnalysis:
    return analyze_signal(decode_audio(path), SR, **kw)
