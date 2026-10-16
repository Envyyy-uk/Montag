"""Зведення звуку кліпу: трек + живі вставки з приглушенням треку (ducking).

Під час вставки трек тихішає на `duck_db` (за замовчуванням −12 дБ) з атакою і відпуском
150 мс — не обривається. Живий звук вирівнюється до гучності треку і має короткі фейди без клацань.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

SR = 48000
RAMP = 0.150
LIVE_FADE = 0.03


def decode_stereo(path: Path, start: float, duration: float, sr: int = SR) -> np.ndarray:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{start:.4f}", "-t",
           f"{duration:.4f}", "-i", str(path), "-vn", "-ac", "2", "-ar", str(sr), "-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "replace")[-500:])
    data = np.frombuffer(proc.stdout, dtype=np.float32).reshape(-1, 2)
    n = int(round(duration * sr))
    if len(data) < n:  # трек або відео коротші — тиша
        data = np.vstack([data, np.zeros((n - len(data), 2), np.float32)])
    return data[:n].copy()


def duck_envelope(n: int, inserts: list[tuple[float, float]], duck_db: float, sr: int = SR,
                  ramp: float = RAMP) -> np.ndarray:
    """Коефіцієнт гучності треку: 1 поза вставками, 10^(duck/20) всередині, лінійні рампи `ramp`."""
    g = np.ones(n, dtype=np.float32)
    low = 10 ** (duck_db / 20)
    t = np.arange(n) / sr
    for s, e in inserts:
        # повністю приглушено на [s, e], рампи [s−ramp, s] і [e, e+ramp]
        down = np.clip((t - (s - ramp)) / ramp, 0, 1)
        up = np.clip(((e + ramp) - t) / ramp, 0, 1)
        depth = np.minimum(down, up)          # 0 далеко, 1 всередині
        g = np.minimum(g, 1 - depth * (1 - low))
    return g


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2))) if x.size else 0.0


def mix(track: np.ndarray, lives: list[tuple[float, np.ndarray]], duck_db: float = -12.0,
        live_gain_db: float = 0.0, sr: int = SR) -> np.ndarray:
    """track — стерео вікна; lives — (час початку, стерео шматок живого звуку)."""
    n = len(track)
    env = duck_envelope(n, [(s, s + len(x) / sr) for s, x in lives], duck_db, sr)
    out = track * env[:, None]
    target = rms(track) * 10 ** (live_gain_db / 20)
    fade = int(LIVE_FADE * sr)
    for s, x in lives:
        a = int(round(s * sr))
        x = x[: max(0, n - a)].copy()
        if not len(x):
            continue
        level = rms(x)
        if level > 1e-5 and target > 0:
            x *= min(target / level, 10.0)  # не підсилювати тишу понад +20 дБ
        f = min(fade, len(x) // 2)
        if f:
            ramp = np.linspace(0, 1, f, dtype=np.float32)[:, None]
            x[:f] *= ramp
            x[-f:] *= ramp[::-1]
        out[a:a + len(x)] += x
    peak = float(np.abs(out).max()) if out.size else 0.0
    if peak > 0.98:
        out *= 0.98 / peak
    return out.astype(np.float32)


def write_wav(path: Path, audio: np.ndarray, sr: int = SR) -> None:
    import soundfile as sf

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.stem}.part.wav")
    sf.write(tmp, audio, sr, subtype="PCM_24")
    tmp.replace(path)
