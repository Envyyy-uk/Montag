"""Таймлайн кліпу: структура, яку далі рендерить FFmpeg (Спринт 6) і експортує FCPXML (Спринт 7).

Час таймлайну — секунди від початку кліпу; межі кліпів квантовані до кадрів (`fps`),
тож склейки лягають на найближчий до біта кадр.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class Clip:
    start: float          # на таймлайні
    end: float
    media_id: int
    src_in: float         # в оригіналі/проксі
    kind: str = "beat"    # beat | insert
    score: float = 0.0
    source: str = ""      # gemini | local
    tags: list[str] = field(default_factory=list)
    level: str = ""       # енергія треку в цьому місці: low | mid | high
    accent: bool = False  # дроп/приспів/вставка — тут можливі ефектні переходи (Спринт 6)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def src_out(self) -> float:
        return self.src_in + self.duration


@dataclass
class Insert:
    start: float
    end: float
    media_id: int
    src_in: float
    duck_db: float = -12.0
    live_gain_db: float = 0.0


@dataclass
class Marker:
    t: float
    kind: str   # drop | chorus | insert
    label: str = ""


@dataclass
class Timeline:
    fps: float
    duration: float
    track_media_id: int | None
    track_in: float
    track_out: float
    clips: list[Clip] = field(default_factory=list)
    inserts: list[Insert] = field(default_factory=list)
    markers: list[Marker] = field(default_factory=list)
    params: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Timeline":
        d = dict(d)
        d["clips"] = [Clip(**c) for c in d.get("clips", [])]
        d["inserts"] = [Insert(**i) for i in d.get("inserts", [])]
        d["markers"] = [Marker(**m) for m in d.get("markers", [])]
        return cls(**d)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_text(json.dumps(self.as_dict(), ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Timeline":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def frame(self, t: float) -> int:
        return int(round(t * self.fps))

    def check(self) -> list[str]:
        """Інваріанти: без дірок і накладань, у межах тривалості, вставки — на своїх кліпах."""
        problems = []
        prev = 0.0
        for c in self.clips:
            if abs(c.start - prev) > 1e-6:
                problems.append(f"дірка/накладання на {prev:.3f}–{c.start:.3f}")
            if c.end <= c.start:
                problems.append(f"порожній кліп на {c.start:.3f}")
            prev = c.end
        if abs(prev - self.duration) > 1e-6:
            problems.append(f"кліпи закінчуються на {prev:.3f}, а таймлайн — {self.duration:.3f}")
        return problems
