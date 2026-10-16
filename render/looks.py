"""«Вигляд» стилю: параметри з JSON → ланцюжок фільтрів FFmpeg.

Стиль задає базові параметри (`look`) і три рівні інтенсивності (`levels`), які
перекривають базу. Ланцюжок застосовується до кадру, вже вписаного у вихідний розмір;
дата й час зйомки малюються останніми, поверх зерна і ліній розгортки — як у камкордера.
"""

from __future__ import annotations

import json
import platform
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

STYLES_DIR = Path(__file__).resolve().parent.parent / "styles"

FONT_CANDIDATES = [
    "C:/Windows/Fonts/consolab.ttf", "C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/lucon.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Bold.ttf",
    "/System/Library/Fonts/Menlo.ttc",
]
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


class StyleError(ValueError):
    pass


@dataclass
class Style:
    id: str
    name: str
    data: dict[str, Any]

    @property
    def default_level(self) -> int:
        return int(self.data.get("default_level", 2))

    def look(self, level: int) -> dict[str, Any]:
        base = dict(self.data.get("look", {}))
        base.update(self.data.get("levels", {}).get(str(level), {}))
        return base

    @property
    def transitions(self) -> dict[str, Any]:
        return self.data.get("transitions", {})


def list_styles() -> list[Style]:
    out = []
    for p in sorted(STYLES_DIR.glob("*.json")):
        if p.name == "filters.json":
            continue
        d = json.loads(p.read_text(encoding="utf-8"))
        out.append(Style(d["id"], d["name"], d))
    return out


def get_style(style_id: str) -> Style:
    for s in list_styles():
        if s.id == style_id:
            return s
    raise StyleError(f"Стиль «{style_id}» не знайдено")


@lru_cache
def stamp_font() -> str | None:
    for f in FONT_CANDIDATES:
        if Path(f).exists():
            return f
    return None


def _ff_path(path: str) -> str:
    """Шлях для опції фільтра: двокрапка диска Windows екранується."""
    p = path.replace("\\", "/")
    return p.replace(":", "\\:") if platform.system() == "Windows" or (len(p) > 1 and p[1] == ":") else p


def _color(c: str, alpha: float = 1.0) -> str:
    return f"{c}@{alpha:.2f}"


def look_filters(look: dict[str, Any], width: int, height: int) -> list[str]:
    """Фільтри кольору й фактури (без дати). Порядок важливий: м'якість → колір → фактура."""
    f: list[str] = []
    lines = look.get("soften_lines")
    if lines and lines < height:
        f.append(f"scale=-2:{int(lines)}:flags=area")
        f.append(f"scale={width}:{height}:flags=bilinear")
    if look.get("sharpen"):
        # «електронна різкість» камкордера: ореоли по краях
        f.append(f"unsharp=5:5:{look['sharpen']:.2f}:5:5:0")
    eq = {k: look[k] for k in ("contrast", "brightness", "saturation", "gamma") if k in look}
    if eq:
        f.append("eq=" + ":".join(f"{k}={v:.3f}" for k, v in eq.items()))
    lift = float(look.get("lift", 0))
    crush = float(look.get("highlight_rolloff", 0))
    if lift or crush:
        f.append(f"curves=all='0/{lift:.3f} 0.5/{0.5 + lift / 3:.3f} 1/{1 - crush:.3f}'")
    cb = []
    for key, pref in (("shadows", "s"), ("midtones", "m"), ("highlights", "h")):
        if key in look:
            r, g, b = look[key]
            cb += [f"r{pref}={r:.3f}", f"g{pref}={g:.3f}", f"b{pref}={b:.3f}"]
    if cb:
        f.append("colorbalance=" + ":".join(cb))
    if look.get("temperature"):
        f.append(f"colortemperature=temperature={int(look['temperature'])}:mix={look.get('temperature_mix', 0.6):.2f}")
    if look.get("chroma_blur"):
        f.append(f"format=yuv420p,boxblur=luma_radius=0:luma_power=0:chroma_radius={int(look['chroma_blur'])}:chroma_power=1")
    if look.get("chroma_shift"):
        s = int(look["chroma_shift"])
        f.append(f"rgbashift=rh={-s}:bh={s}")
    grain, cnoise = int(look.get("grain", 0)), int(look.get("chroma_noise", 0))
    if grain or cnoise:
        f.append(f"format=yuv420p,noise=c0s={grain}:c0f=t+u:c1s={cnoise}:c1f=t+u:c2s={cnoise}:c2f=t+u")
    if look.get("vignette"):
        f.append(f"vignette=angle={float(look['vignette']):.3f}")
    if look.get("scanlines"):
        step = max(2, round(height / 360))
        f.append(f"drawgrid=w={width * 4}:h={step}:t=1:c={_color('black', float(look['scanlines']))}")
    return f


def _escape_text(s: str) -> str:
    # одинарна лапка всередині '...' у фільтрах FFmpeg неможлива — замінюємо на ’
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\u2019").replace("%", "\\%")


def stamp_filters(look: dict[str, Any], height: int, shot_at: str | None, offset: float) -> list[str]:
    """Жовта дата і час зйомки в правому нижньому куті; секунди «тікають» разом із кліпом."""
    st = look.get("stamp")
    font = stamp_font()
    if not st or not shot_at or not font:
        return []
    dt = datetime.fromisoformat(shot_at.replace("Z", "+00:00")).astimezone(timezone.utc)
    epoch = int(dt.timestamp() + offset)
    local = datetime.fromtimestamp(epoch).astimezone()  # час ноутбука = час зйомки, як у камкордера
    size = max(14, round(height * float(st.get("size", 0.045))))
    color = st.get("color", "#ffd23a")
    margin_x, margin_y = round(height * 0.07), round(height * 0.06)
    common = (f"fontfile='{_ff_path(font)}':fontsize={size}:fontcolor={color}:"
              f"shadowcolor=black@0.6:shadowx={max(1, size // 14)}:shadowy={max(1, size // 14)}")
    fmt = st.get("format", "minidv")
    if fmt == "film":
        date_txt = f"\u2019{local:%y} {local.month} {local.day}"  # ’25 6 14 — типографський апостроф
        return [f"drawtext={common}:text='{_escape_text(date_txt)}':x=w-tw-{margin_x}:y=h-th-{margin_y}"]
    if fmt == "vhs":
        date_txt = local.strftime("%d.%m.%Y")
        line1 = f"drawtext={common}:text='{_escape_text(date_txt)}':x=w-tw-{margin_x}:y=h-th-{margin_y}"
        return [line1]
    date_txt = f"{MONTHS[local.month - 1]} {local.day:2d} {local.year}"
    # %{pts:localtime:EPOCH:FORMAT} — час іде разом із кадрами
    time_expr = f"%{{pts\\:localtime\\:{epoch}\\:%I\\\\\\:%M\\\\\\:%S %p}}"
    line_date = f"drawtext={common}:text='{_escape_text(date_txt)}':x=w-tw-{margin_x}:y=h-{margin_y}-2.3*{size}"
    line_time = f"drawtext={common}:text='{time_expr}':x=w-tw-{margin_x}:y=h-{margin_y}-{size}"
    return [line_date, line_time]


def fit_graph(src_w: int, src_h: int, width: int, height: int, inp: str, out: str) -> str:
    """Вписати джерело у кадр. Сильно інша орієнтація — розмита копія на фоні, інакше — кадрування."""
    src_ar, dst_ar = src_w / max(src_h, 1), width / height
    if src_ar / dst_ar < 0.75 or src_ar / dst_ar > 1.33:
        return (f"[{inp}]split=2[fg_{out}][bg_{out}];"
                f"[bg_{out}]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
                f"gblur=sigma=30,eq=brightness=-0.08[bgb_{out}];"
                f"[fg_{out}]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs_{out}];"
                f"[bgb_{out}][fgs_{out}]overlay=(W-w)/2:(H-h)/2,setsar=1[{out}]")
    return (f"[{inp}]scale={width}:{height}:force_original_aspect_ratio=increase:flags=bicubic,"
            f"crop={width}:{height},setsar=1[{out}]")

