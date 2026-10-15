"""Локальні метрики якості кадрів і фільтр явного браку.

Кадри читаються з 480p-проксі з частотою ~5 кадрів/с. На кожен кадр:
- різкість — дисперсія Лапласіана;
- яскравість — середнє і 95-й перцентиль (концерт темний, але з вогнями сцени — не брак);
- контраст — СКВ яскравості (однотонна підлога, кишеня);
- рух — середня величина оптичного потоку Фарнебека;
- зсув камери — фазова кореляція між сусідніми кадрами; тряска = відхилення
  траєкторії камери від її згладженої версії (плавна панорама — не тряска).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

from analysis.thresholds import FilterThresholds

SAMPLE_FPS = 5.0
SMALL_W = 160
MIN_PHASE_RESPONSE = 0.05

REASONS = {
    "short": "закороткий",
    "pocket": "кишеня",
    "dark": "темрява",
    "overexposed": "пересвіт",
    "flat": "однотонне",
    "blur": "розмито",
    "shake": "тряска",
}


@dataclass
class FrameMetrics:
    t: np.ndarray
    sharpness: np.ndarray
    brightness: np.ndarray
    p95: np.ndarray
    contrast: np.ndarray
    motion: np.ndarray   # % висоти кадру за секунду
    dx: np.ndarray       # зсув камери від попереднього кадру, % висоти
    dy: np.ndarray

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.stem + ".part.npz")
        np.savez_compressed(tmp, **self.__dict__)
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "FrameMetrics":
        with np.load(path) as data:
            return cls(**{k: data[k] for k in data.files})


def measure_frames(path: Path, duration: float = 0.0, sample_fps: float = SAMPLE_FPS,
                   on_progress: Callable[[float], None] | None = None) -> FrameMetrics:
    import cv2

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"OpenCV не відкриває {path}")
    rows: list[tuple] = []
    prev_small = None
    prev_t = None
    next_t = 0.0
    frame_no = 0
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    try:
        while True:
            ok = cap.grab()
            if not ok:
                break
            t = frame_no / fps
            frame_no += 1
            if t + 1e-6 < next_t:
                continue
            ok, frame = cap.retrieve()
            if not ok:
                break
            next_t = t + 1.0 / sample_fps
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            h, w = gray.shape
            sharp = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            mean = float(gray.mean())
            p95 = float(np.percentile(gray, 95))
            contrast = float(gray.std())
            small_h = max(2, int(round(h * SMALL_W / w)))
            small = cv2.resize(gray, (SMALL_W, small_h), interpolation=cv2.INTER_AREA)
            motion = dx = dy = 0.0
            if prev_small is not None:
                dt = max(1e-3, t - prev_t)
                flow = cv2.calcOpticalFlowFarneback(prev_small, small, None, 0.5, 3, 15, 3, 5, 1.2, 0)
                mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
                motion = float(mag.mean()) / small_h * 100 / dt
                (sx, sy), resp = cv2.phaseCorrelate(prev_small.astype(np.float32), small.astype(np.float32))
                if resp >= MIN_PHASE_RESPONSE:  # на однотонному кадрі зсув не визначений — не тряска
                    dx, dy = sx / small_h * 100, sy / small_h * 100
            rows.append((t, sharp, mean, p95, contrast, motion, dx, dy))
            prev_small, prev_t = small, t
            if on_progress and duration > 0 and len(rows) % 25 == 0:
                on_progress(min(1.0, t / duration))
    finally:
        cap.release()
    if not rows:
        raise RuntimeError(f"У {path} не прочитано жодного кадру")
    arr = np.array(rows, dtype=np.float64)
    return FrameMetrics(*(arr[:, i] for i in range(arr.shape[1])))


def shake_rms(dx: np.ndarray, dy: np.ndarray, window: int = 5) -> float:
    """Тремтіння: відхилення траєкторії камери від згладженої (≈1 с) версії."""
    if len(dx) < 3:
        return 0.0
    path = np.stack([np.cumsum(dx), np.cumsum(dy)], axis=1)
    k = min(window, len(dx))
    kernel = np.ones(k) / k
    pad = k // 2
    smooth = np.stack([np.convolve(np.pad(path[:, i], pad, mode="edge"), kernel, mode="valid")[: len(dx)]
                       for i in range(2)], axis=1)
    return float(np.sqrt(((path - smooth) ** 2).sum(axis=1).mean()))


def segment_metrics(fm: FrameMetrics, start: float, end: float) -> dict[str, float]:
    mask = (fm.t >= start - 1e-6) & (fm.t < end - 1e-6)
    if not mask.any():  # дуже короткий шматок — найближчий кадр
        mask = np.zeros_like(fm.t, dtype=bool)
        mask[int(np.argmin(np.abs(fm.t - (start + end) / 2)))] = True
    idx = np.flatnonzero(mask)
    # перший кадр шматка — після склейки, його рух/зсув відносно попередньої сцени не рахуємо
    inner = idx[1:] if len(idx) > 1 else idx[:0]
    return {
        "sharpness": round(float(np.median(fm.sharpness[idx])), 2),
        "brightness": round(float(np.mean(fm.brightness[idx])), 2),
        "p95": round(float(np.median(fm.p95[idx])), 2),
        "contrast": round(float(np.median(fm.contrast[idx])), 2),
        "motion": round(float(np.mean(fm.motion[inner])) if len(inner) else 0.0, 3),
        "shake": round(shake_rms(fm.dx[inner], fm.dy[inner]), 3),
        "samples": int(len(idx)),
    }


def reject_reason(m: dict[str, float], duration: float, th: FilterThresholds) -> str | None:
    """Причина явного браку або None. Ло-фай (зерно, легка тряска) проходить з м'якшим пресетом."""
    if duration < th.min_duration:
        return REASONS["short"]
    if m["p95"] < th.dark_p95 and m["contrast"] < th.min_contrast:
        return REASONS["pocket"]
    if m["p95"] < th.dark_p95:
        return REASONS["dark"]
    if m["brightness"] > th.max_brightness:
        return REASONS["overexposed"]
    if m["contrast"] < th.min_contrast:
        return REASONS["flat"]
    if m["sharpness"] < th.min_sharpness:
        return REASONS["blur"]
    if m["shake"] > th.max_shake:
        return REASONS["shake"]
    return None


def local_score(m: dict[str, float], th: FilterThresholds) -> float:
    """Запасна оцінка 0–1 без AI: різкість, стабільність, експозиція, «живість» кадру."""
    sharp = np.clip(np.log2(max(m["sharpness"], 1e-3) / th.min_sharpness) / 3, 0, 1)
    stable = np.clip(1 - m["shake"] / th.max_shake, 0, 1)
    b = m["brightness"]
    exposure = 1.0 if 35 <= b <= 200 else float(np.clip(1 - min(abs(b - 35), abs(b - 200)) / 40, 0, 1))
    lively = np.clip(m["motion"] / 15, 0, 1)
    return round(float(0.4 * sharp + 0.3 * stable + 0.2 * exposure + 0.1 * lively), 3)
