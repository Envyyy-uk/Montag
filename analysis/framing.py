"""Кадрування горизонтального кадру у вертикаль 9:16.

Детектор (MediaPipe: обличчя + люди) шукає головний об'єкт на 480p-проксі з частотою ~5 кадрів/с.
Вікно плавно стежить за ним (згладжування + обмеження швидкості). Якщо вікно обріже
важливе — двоє людей по краях, широкий натовп, обличчя на межі — кліп вписується цілим
з розмитою копією зверху і знизу. Вертикальні оригінали не кадруються.
"""

from __future__ import annotations

import logging
import threading
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

SAMPLE_FPS = 5.0
MAX_SPEED = 0.35       # максимум зсуву вікна, частка ширини кадру за секунду
SMOOTH = 0.35          # експоненційне згладження центру
FIT_SHARE = 0.3        # частка проблемних моментів, після якої — вписування з розмитим фоном
MARGIN = 0.01          # запас для облич біля краю вікна

MODELS = {
    "face": "https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/latest/"
            "blaze_face_short_range.tflite",
    "person": "https://storage.googleapis.com/mediapipe-models/object_detector/efficientdet_lite0/int8/latest/"
              "efficientdet_lite0.tflite",
}


@dataclass
class Box:
    x0: float   # частки ширини/висоти кадру 0..1
    y0: float
    x1: float
    y1: float
    score: float
    kind: str   # face | person

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def area(self) -> float:
        return max(0.0, self.x1 - self.x0) * max(0.0, self.y1 - self.y0)


class Detector(Protocol):
    def detect(self, rgb: np.ndarray) -> list[Box]: ...


@dataclass
class Framing:
    mode: str                          # crop | fit | native
    keys: list[tuple[float, float]] = field(default_factory=list)  # (с від початку кліпу, центр 0..1)
    reason: str = ""


# ---------- детектор ----------

class MediaPipeDetector:
    def __init__(self, models_dir: Path):
        import mediapipe as mp
        from mediapipe.tasks import python as mpp
        from mediapipe.tasks.python import vision

        paths = {k: ensure_model(models_dir, k) for k in MODELS}
        self._mp = mp
        self.face = vision.FaceDetector.create_from_options(vision.FaceDetectorOptions(
            base_options=mpp.BaseOptions(model_asset_path=str(paths["face"])), min_detection_confidence=0.5))
        self.person = vision.ObjectDetector.create_from_options(vision.ObjectDetectorOptions(
            base_options=mpp.BaseOptions(model_asset_path=str(paths["person"])), max_results=10,
            score_threshold=0.35, category_allowlist=["person"]))

    def detect(self, rgb: np.ndarray) -> list[Box]:
        h, w = rgb.shape[:2]
        img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
        out = []
        for kind, det in (("face", self.face), ("person", self.person)):
            for d in det.detect(img).detections:
                b = d.bounding_box
                out.append(Box(b.origin_x / w, b.origin_y / h, (b.origin_x + b.width) / w,
                               (b.origin_y + b.height) / h, d.categories[0].score if d.categories else 0.5, kind))
        return out

    def close(self) -> None:
        self.face.close()
        self.person.close()


_lock = threading.Lock()


def ensure_model(models_dir: Path, kind: str) -> Path:
    path = models_dir / Path(MODELS[kind]).name
    with _lock:
        if not path.exists():
            models_dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            log.info("завантаження моделі %s", path.name)
            with urllib.request.urlopen(MODELS[kind], timeout=60) as r, open(tmp, "wb") as f:
                f.write(r.read())
            tmp.replace(path)
    return path


def make_detector(models_dir: Path) -> Detector | None:
    try:
        return MediaPipeDetector(models_dir)
    except Exception as exc:  # noqa: BLE001 — без детектора працює підказка Gemini або розмитий фон
        log.warning("MediaPipe недоступний (%s) — кадрування за підказкою Gemini або з розмитим фоном", exc)
        return None


# ---------- рішення ----------

def sample_frames(path: Path, start: float, duration: float, fps: float = SAMPLE_FPS):
    """Кадри RGB відрізку з частотою `fps`: [(с від start, кадр)]."""
    import cv2

    cap = cv2.VideoCapture(str(path))
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    out, t, next_t, n = [], 0.0, 0.0, 0
    try:
        while t <= duration:
            ok, frame = cap.read()
            if not ok:
                break
            t = n / src_fps
            n += 1
            if t + 1e-6 >= next_t:
                out.append((t, cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)))
                next_t += 1 / fps
    finally:
        cap.release()
    return out


def important(boxes: list[Box]) -> list[Box]:
    """Головні об'єкти: великі обличчя (≥ 25% найбільшого), інакше великі постаті."""
    faces = [b for b in boxes if b.kind == "face"]
    if faces:
        top = max(b.area for b in faces)
        return [b for b in faces if b.area >= 0.25 * top]
    people = [b for b in boxes if b.kind == "person" and (b.y1 - b.y0) >= 0.35]
    if people:
        top = max(b.area for b in people)
        return [b for b in people if b.area >= 0.4 * top]
    return []


def decide(samples: list[tuple[float, list[Box]]], window: float, hint_x: float | None = None) -> Framing:
    """samples — (час, рамки) для кожного кадру-зразка; window — ширина вікна 9:16 як частка ширини кадру."""
    if window >= 0.999:
        return Framing("native", reason="кадр уже вертикальний")
    half = window / 2
    raw: list[float | None] = []
    too_wide = 0
    for _t, boxes in samples:
        imp = important(boxes)
        if not imp:
            raw.append(None)
            continue
        left, right = min(b.x0 for b in imp), max(b.x1 for b in imp)
        if right - left > window * 0.95:
            too_wide += 1
            # тримаємо найбільший об'єкт, але запам'ятовуємо, що кадр не влазить
            main = max(imp, key=lambda b: b.area * b.score)
            raw.append(main.cx)
        else:
            raw.append((left + right) / 2)
    if samples and too_wide / len(samples) >= FIT_SHARE:
        return Framing("fit", reason="важливе не влазить у вертикаль (люди по краях або широкий план)")
    if not any(r is not None for r in raw):
        if hint_x is None:
            return Framing("fit", reason="головний об'єкт не знайдено")
        raw = [hint_x] * max(1, len(samples))

    # заповнити пропуски сусідніми значеннями
    first = next(r for r in raw if r is not None)
    filled, last = [], first
    for r in raw:
        last = r if r is not None else last
        filled.append(last)

    # згладження з обмеженням швидкості
    times = [t for t, _ in samples] or [0.0]
    cur = float(np.clip(filled[0], half, 1 - half))
    keys = [(round(times[0], 3), round(cur, 4))]
    for i in range(1, len(filled)):
        dt = times[i] - times[i - 1]
        step = float(np.clip(SMOOTH * (filled[i] - cur), -MAX_SPEED * dt, MAX_SPEED * dt))
        cur = float(np.clip(cur + step, half, 1 - half))
        keys.append((round(times[i], 3), round(cur, 4)))

    # перевірка: головні обличчя не обрізані
    cut = 0
    for (_t, boxes), (_k, cx) in zip(samples, keys):
        for b in important(boxes):
            if b.kind == "face" and (b.x0 < cx - half - MARGIN or b.x1 > cx + half + MARGIN):
                cut += 1
                break
    if samples and cut / len(samples) > 0.1:
        return Framing("fit", reason="вікно обрізало б обличчя")
    return Framing("crop", keys)


def analyze_clip(detector: Detector | None, proxy: Path, start: float, duration: float,
                 src_w: int, src_h: int, hint_x: float | None = None, target_ar: float = 9 / 16) -> Framing:
    window = (src_h * target_ar) / src_w
    if window >= 0.999:
        return Framing("native", reason="кадр уже вертикальний")
    if detector is None:
        return decide([], window, hint_x)
    samples = [(t, detector.detect(rgb)) for t, rgb in sample_frames(proxy, start, duration)]
    return decide(samples, window, hint_x)


def crop_x_expr(keys: list[tuple[float, float]], window: float) -> str:
    """Вираз FFmpeg для лівого краю вікна: кусково-лінійно між ключами (у пікселях `iw`)."""
    if not keys:
        return f"iw*{0.5 - window / 2:.5f}"
    expr = f"{keys[-1][1] - window / 2:.5f}"
    for (t0, c0), (t1, c1) in reversed(list(zip(keys, keys[1:]))):
        x0, x1 = c0 - window / 2, c1 - window / 2
        seg = f"{x0:.5f}+({x1 - x0:.5f})*(t-{t0:.3f})/{max(t1 - t0, 1e-3):.3f}"
        expr = f"if(lt(t\\,{t1:.3f})\\,{seg}\\,{expr})"
    first = keys[0][1] - window / 2
    expr = f"if(lt(t\\,{keys[0][0]:.3f})\\,{first:.5f}\\,{expr})"
    return f"iw*({expr})"
