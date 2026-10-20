"""Формати кадру (16:9, 9:16, 1:1, 4:5) і як вписати в них джерело іншої форми.

- джерело ширше за кадр → вікно по ширині, що йде за обличчями (Framing.crop), або по центру;
- джерело вужче (вертикальне відео в горизонтальному кадрі) → обрізання по висоті, трохи вище центру,
  бо обличчя зазвичай у верхній третині;
- коли обрізати не можна (люди по краях) або так вибрано → розмитий фон.
"""

from __future__ import annotations

ASPECTS: dict[str, tuple[int, int]] = {"16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "4:5": (4, 5)}
CLIP_ASPECTS = ("16:9", "9:16", "1:1", "4:5")
REEL_ASPECTS = ("9:16", "4:5", "1:1")
FIT_MODES = ("auto", "crop", "blur")  # auto — обрізати, якщо не втрачається забагато, інакше розмитий фон
Y_ANCHOR = 0.4  # де брати смугу при обрізанні по висоті: 0 — верх, 0.5 — центр


def ratio(aspect: str) -> float:
    a, b = ASPECTS[aspect]
    return a / b


def size_for(aspect: str, short_side: int = 1080) -> tuple[int, int]:
    """Розмір кадру: коротка сторона = short_side (висота рендеру з налаштувань), обидві парні."""
    a, b = ASPECTS[aspect]
    short = short_side - short_side % 2
    long_ = round(short * max(a, b) / min(a, b))
    long_ -= long_ % 2
    return (long_, short) if a >= b else (short, long_)


def needs_x_crop(src_w: int, src_h: int, width: int, height: int) -> bool:
    """Джерело помітно ширше за кадр — треба вибрати, яку частину по ширині показати."""
    return src_w / max(src_h, 1) > width / height * 1.02


def _blur(width: int, height: int, inp: str, out: str) -> str:
    return (f"[{inp}]split=2[fg_{out}][bg_{out}];"
            f"[bg_{out}]scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
            f"gblur=sigma=30,eq=brightness=-0.08[bgb_{out}];"
            f"[fg_{out}]scale={width}:{height}:force_original_aspect_ratio=decrease[fgs_{out}];"
            f"[bgb_{out}][fgs_{out}]overlay=(W-w)/2:(H-h)/2,setsar=1[{out}]")


def frame_graph(src_w: int, src_h: int, width: int, height: int, inp: str, out: str,
                framing=None, fit_mode: str = "auto") -> str:
    """Фрагмент filter_complex: [inp] → кадр width×height → [out]."""
    from analysis.framing import crop_x_expr

    src_ar, dst_ar = src_w / max(src_h, 1), width / height
    mismatch = max(src_ar / dst_ar, dst_ar / src_ar)
    if mismatch < 1.02:
        return f"[{inp}]scale={width}:{height}:flags=lanczos,setsar=1[{out}]"
    if fit_mode == "blur":
        return _blur(width, height, inp, out)
    mode = getattr(framing, "mode", None)
    if src_ar > dst_ar:  # ширше: вікно по ширині
        if mode == "crop":
            window = (src_h * dst_ar) / src_w
            x = crop_x_expr(framing.keys, window)
            return (f"[{inp}]crop=w=trunc(ih*{dst_ar:.6f}/2)*2:h=ih:x='{x}':y=0,"
                    f"scale={width}:{height}:flags=lanczos,setsar=1[{out}]")
        if fit_mode == "auto" and (mode == "fit" or mismatch > 1.34):
            return _blur(width, height, inp, out)
        return (f"[{inp}]scale={width}:{height}:force_original_aspect_ratio=increase:flags=bicubic,"
                f"crop={width}:{height},setsar=1[{out}]")
    # вужче (вертикальне в горизонтальному): смуга по висоті
    if fit_mode == "auto" and mismatch > 1.34:
        return _blur(width, height, inp, out)
    return (f"[{inp}]crop=w=iw:h=trunc(iw/{dst_ar:.6f}/2)*2:x=0:y='(ih-oh)*{Y_ANCHOR}',"
            f"scale={width}:{height}:flags=lanczos,setsar=1[{out}]")
