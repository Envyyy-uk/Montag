"""Нові стилі словами: опис → JSON пресета (Gemini або локальні ключові слова) → тестовий рендер → збереження.

Параметри «вигляду» обмежені безпечними діапазонами; рівні інтенсивності 1 і 3
виводяться з рівня 2 (м'якше / сильніше).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from render.transitions import TRANSITIONS

RANGES: dict[str, tuple[float, float]] = {
    "soften_lines": (240, 1080), "sharpen": (0, 1.5), "contrast": (0.7, 1.4), "saturation": (0, 1.6),
    "gamma": (0.7, 1.4), "brightness": (-0.2, 0.2), "lift": (0, 0.15), "highlight_rolloff": (0, 0.15),
    "temperature": (2000, 10000), "temperature_mix": (0, 1), "grain": (0, 30), "chroma_noise": (0, 40),
    "chroma_blur": (0, 8), "chroma_shift": (0, 8), "vignette": (0, 1.2), "scanlines": (0, 0.3),
}
NEUTRAL: dict[str, float] = {"contrast": 1, "saturation": 1, "gamma": 1, "brightness": 0, "lift": 0,
                             "highlight_rolloff": 0, "sharpen": 0, "grain": 0, "chroma_noise": 0, "chroma_blur": 0,
                             "chroma_shift": 0, "vignette": 0, "scanlines": 0, "temperature": 6500,
                             "temperature_mix": 0}
INT_KEYS = {"soften_lines", "grain", "chroma_noise", "chroma_blur", "chroma_shift", "temperature"}
COLOR_KEYS = ("shadows", "midtones", "highlights")

LOOK_SCHEMA = {
    "type": "object",
    "properties": {
        **{k: {"type": "number", "minimum": lo, "maximum": hi} for k, (lo, hi) in RANGES.items()},
        **{k: {"type": "array", "items": {"type": "number", "minimum": -0.3, "maximum": 0.3}, "minItems": 3,
               "maxItems": 3} for k in COLOR_KEYS},
        "stamp": {"type": "string", "enum": ["minidv", "vhs", "film", "none"]},
    },
    "additionalProperties": False,
}
STYLE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "maxLength": 40},
        "description": {"type": "string", "maxLength": 300},
        "event_types": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "filter": {"type": "string", "enum": ["lofi", "standard"]},
        "look": LOOK_SCHEMA,
        "transitions": {"type": "array", "items": {"type": "string", "enum": list(TRANSITIONS)}, "maxItems": 7},
    },
    "required": ["name", "description", "look"],
}
STAMPS = {"minidv": {"format": "minidv", "color": "#ffd23a", "size": 0.045},
          "vhs": {"format": "vhs", "color": "#f2f2f2", "size": 0.05},
          "film": {"format": "film", "color": "#ff8a2a", "size": 0.04}}


def clamp_look(look: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in look.items():
        if k in RANGES and isinstance(v, (int, float)):
            lo, hi = RANGES[k]
            v = min(hi, max(lo, float(v)))
            out[k] = int(round(v)) if k in INT_KEYS else round(v, 3)
        elif k in COLOR_KEYS and isinstance(v, list) and len(v) == 3:
            out[k] = [round(min(0.3, max(-0.3, float(x))), 3) for x in v]
        elif k == "stamp":
            if isinstance(v, str) and v in STAMPS:
                out[k] = STAMPS[v]
            elif isinstance(v, dict) and v.get("format") in STAMPS:
                out[k] = {**STAMPS[v["format"]], **{kk: vv for kk, vv in v.items() if kk in ("color", "size")}}
    return out


def scale_look(look: dict[str, Any], k: float) -> dict[str, Any]:
    """k < 1 — м'якше, k > 1 — сильніше: відхилення від нейтрального множиться на k."""
    out: dict[str, Any] = {}
    for key, v in look.items():
        if key in NEUTRAL and isinstance(v, (int, float)):
            out[key] = NEUTRAL[key] + (v - NEUTRAL[key]) * k
        elif key == "soften_lines":
            out[key] = v / k if k else v
        elif key in COLOR_KEYS:
            out[key] = [x * k for x in v]
        else:
            out[key] = v
    return clamp_look(out)


def build_style(spec: dict[str, Any], style_id: str) -> dict[str, Any]:
    look = clamp_look(spec.get("look", {}))
    allowed = [t for t in spec.get("transitions", []) if t in TRANSITIONS] or ["flash", "zoom", "glitch"]
    return {
        "id": style_id, "name": spec.get("name", "Мій стиль")[:40],
        "description": spec.get("description", "")[:300],
        "event_types": spec.get("event_types", [])[:5], "filter": spec.get("filter", "lofi"),
        "default_level": 2, "look": look,
        "levels": {"1": scale_look(look, 0.6), "2": {}, "3": scale_look(look, 1.4)},
        "transitions": {"allowed": allowed, "max_share": 0.15},
    }


# ---------- без Gemini: ключові слова ----------

KEYWORDS: list[tuple[str, dict[str, Any]]] = [
    (r"тепл|золот|сонц|літ", {"temperature": 4600, "temperature_mix": 0.5, "highlights": [0.05, 0.02, -0.05]}),
    (r"холод|син|ціан|зимов", {"temperature": 8500, "temperature_mix": 0.5, "shadows": [-0.06, 0.02, 0.08]}),
    (r"зерн|плівк|шум", {"grain": 14, "chroma_noise": 6}),
    (r"vhs|касет|90", {"soften_lines": 288, "chroma_blur": 4, "chroma_shift": 3, "scanlines": 0.12,
                       "chroma_noise": 18, "stamp": "vhs"}),
    (r"minidv|камкордер|2000", {"soften_lines": 480, "sharpen": 0.6, "scanlines": 0.08, "stamp": "minidv"}),
    (r"чорно-?біл|монохром|ч/б", {"saturation": 0.0, "contrast": 1.15, "grain": 12}),
    (r"вицвіл|бляк|пастел|м'як", {"lift": 0.07, "saturation": 0.8, "highlight_rolloff": 0.05}),
    (r"соковит|яскрав|насичен", {"saturation": 1.3, "contrast": 1.1}),
    (r"контраст|жорстк|різк", {"contrast": 1.2, "sharpen": 0.5}),
    (r"темн|нуар|похмур", {"gamma": 0.85, "vignette": 0.7, "saturation": 0.85}),
    (r"дат|час", {"stamp": "minidv"}),
    (r"віньєт", {"vignette": 0.6}),
]


def local_spec(text: str) -> dict[str, Any] | None:
    t = text.lower()
    look: dict[str, Any] = {}
    for pat, params in KEYWORDS:
        if re.search(pat, t):
            look.update(params)
    if not look:
        return None
    name = text.strip().split(".")[0][:40] or "Мій стиль"
    return {"name": name.capitalize(), "description": text.strip()[:300], "look": look,
            "filter": "lofi" if any(k in look for k in ("grain", "chroma_noise", "soften_lines")) else "standard"}


def gemini_prompt(text: str, examples: list[dict]) -> str:
    return "\n".join([
        "Ти — колорист. Перетвори опис стилю відео на параметри пресета за JSON-схемою.",
        "Параметри — фільтри FFmpeg: soften_lines (рядків по вертикалі, менше — м'якше), sharpen, contrast, saturation,",
        "gamma, brightness, lift (підняті чорні), highlight_rolloff, shadows/midtones/highlights (зсув RGB −0.3..0.3),",
        "temperature (K, менше — тепліше), grain, chroma_noise, chroma_blur, chroma_shift, vignette (рад), scanlines,",
        "stamp (дата в куті: minidv / vhs / film / none). Рівень, який описуєш, — середній (2).",
        "Приклади готових стилів:", json.dumps(examples, ensure_ascii=False)[:3000],
        f"Опис: {text}",
    ])


def new_id() -> str:
    return f"user-{int(time.time() * 1000)}"


def save(dir_: Path, style: dict[str, Any]) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"{style['id']}.json"
    path.write_text(json.dumps(style, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
