"""Пороги локального фільтра браку. Живуть у стилі; базові пресети — `styles/filters.json`."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from functools import lru_cache
from pathlib import Path
from typing import Any

STYLES_DIR = Path(__file__).resolve().parent.parent / "styles"


@dataclass(frozen=True)
class FilterThresholds:
    min_duration: float = 0.4    # с; коротші шматки — брак
    min_sharpness: float = 30    # дисперсія Лапласіана (медіана по шматку)
    max_shake: float = 1.5       # тремтіння камери, % висоти кадру (RMS)
    dark_p95: float = 60         # якщо навіть 95-й перцентиль яскравості нижчий — темрява
    max_brightness: float = 235  # середня яскравість вища — пересвіт
    min_contrast: float = 12     # СКВ яскравості нижче — однотонне (підлога, стеля, кишеня)

    def as_dict(self) -> dict:
        return asdict(self)


@lru_cache
def presets() -> dict[str, dict[str, float]]:
    raw = json.loads((STYLES_DIR / "filters.json").read_text(encoding="utf-8"))
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def from_spec(spec: str | dict[str, Any] | None, default: str = "lofi") -> FilterThresholds:
    """`"lofi"` або `{"preset": "lofi", "max_shake": 4}` або None (пресет за замовчуванням)."""
    if spec is None:
        spec = default
    if isinstance(spec, str):
        spec = {"preset": spec}
    base_name = spec.get("preset", default)
    if base_name not in presets():
        raise ValueError(f"Невідомий пресет фільтра: {base_name}")
    values = dict(presets()[base_name])
    known = {f.name for f in fields(FilterThresholds)}
    values.update({k: v for k, v in spec.items() if k in known})
    return FilterThresholds(**{k: float(v) for k, v in values.items() if k in known})


def for_style(style_id: str | None, default: str = "lofi") -> FilterThresholds:
    """Пороги стилю проєкту (Спринт 6 додає самі стилі); без стилю — пресет за замовчуванням."""
    if style_id:
        path = STYLES_DIR / f"{style_id}.json"
        if path.exists():
            style = json.loads(path.read_text(encoding="utf-8"))
            return from_spec(style.get("filter"), default)
    return from_spec(None, default)
