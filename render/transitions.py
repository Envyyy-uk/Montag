"""Модулі переходів. Кожен — тривалість у бітах і фільтри FFmpeg на краях кліпів.

Переходи не перекривають кліпи: кінець кліпу A («out») і початок кліпу B («in»)
отримують свою половину ефекту, тож склейка лишається точно на біті, а тривалість
таймлайну не змінюється.

Два переходи «структурні» (змінюють кадри, а не лише вигляд): стоп-кадр і перемотка
плівки — вони працюють лише на кінці кліпу A.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Transition:
    id: str
    name: str
    out_beats: float   # частка на кінці кліпу A
    in_beats: float    # частка на початку кліпу B
    structural: bool = False


TRANSITIONS: dict[str, Transition] = {t.id: t for t in [
    Transition("flash", "Біла спалах-склейка", 0.5, 0.5),
    Transition("zoom", "Пробій зумом", 0.5, 1.0),
    Transition("whip", "Розмита панорама", 0.5, 0.5),
    Transition("glitch", "VHS-збій", 0.5, 0.5),
    Transition("rewind", "Перемотка плівки", 2.0, 0.0, structural=True),
    Transition("freeze", "Стоп-кадр", 1.0, 0.0, structural=True),
    Transition("burn", "Засвітка плівки", 1.0, 1.0),
]}

MAX_EDGE_SHARE = 0.45  # ефект займає не більше ~половини кліпу


def edge_seconds(kind: str, beat: float, clip_a: float, clip_b: float) -> tuple[float, float]:
    t = TRANSITIONS[kind]
    return (min(t.out_beats * beat, clip_a * MAX_EDGE_SHARE), min(t.in_beats * beat, clip_b * MAX_EDGE_SHARE))


def _win(a: float, b: float) -> str:
    return f"between(t\\,{a:.4f}\\,{b:.4f})"


def edge_filters(kind_in: str | None, t_in: float, kind_out: str | None, t_out: float,
                 dur: float, width: int, height: int, fps: float) -> list[str]:
    """Неструктурні ефекти для одного кліпу: вхідний (на початку) і вихідний (наприкінці)."""
    f: list[str] = []
    a_out = dur - t_out
    edges = [(k, s) for k, s in ((kind_in, "in"), (kind_out, "out")) if k]

    def window(side: str) -> str:
        return _win(0, t_in) if side == "in" else _win(a_out, dur + 1)

    # зум і панорама — одним zoompan
    zoom_terms, pan_terms = [], []
    for k, side in edges:
        if k == "zoom":
            if side == "in" and t_in > 0:
                zoom_terms.append(f"0.35*max(0\\,1-it/{t_in:.4f})")
            if side == "out" and t_out > 0:
                zoom_terms.append(f"0.25*max(0\\,(it-{a_out:.4f})/{t_out:.4f})")
        if k == "whip":
            if side == "in" and t_in > 0:
                zoom_terms.append(f"0.2*lt(it\\,{t_in:.4f})")
                pan_terms.append(f"-(iw-iw/zoom)/2*max(0\\,1-it/{t_in:.4f})")
            if side == "out" and t_out > 0:
                zoom_terms.append(f"0.2*gte(it\\,{a_out:.4f})")
                pan_terms.append(f"(iw-iw/zoom)/2*min(1\\,max(0\\,(it-{a_out:.4f})/{t_out:.4f}))")
    if zoom_terms:
        z = "1+" + "+".join(zoom_terms)
        x = "iw/2-(iw/zoom/2)" + "".join(f"+{p}" if not p.startswith("-") else p for p in pan_terms)
        f.append(f"zoompan=z='{z}':x='{x}':y='ih/2-(ih/zoom/2)':d=1:s={width}x{height}:fps={fps}")

    for k, side in edges:
        w = window(side)
        if k == "flash":
            if side == "in" and t_in > 0:
                f.append(f"fade=t=in:st=0:d={t_in:.4f}:color=white")
            if side == "out" and t_out > 0:
                f.append(f"fade=t=out:st={a_out:.4f}:d={t_out:.4f}:color=white")
        elif k == "whip":
            f.append(f"avgblur=sizeX={max(8, width // 30)}:sizeY=1:enable='{w}'")
        elif k == "glitch":
            s = max(4, width // 120)
            f.append(f"rgbashift=rh={-s}:bh={s}:gv={s // 2}:enable='{w}'")
            f.append(f"format=yuv420p,noise=alls=45:allf=t:enable='{w}'")
            f.append(f"hue=s=1.8:enable='{w}'")
        elif k == "burn":
            ramp = (f"0.45*max(0\\,1-t/{t_in:.4f})" if side == "in" and t_in > 0
                    else f"0.45*max(0\\,(t-{a_out:.4f})/{t_out:.4f})")
            f.append(f"eq=brightness='{ramp}':eval=frame")
            f.append(f"colortemperature=temperature=2200:mix=0.8:enable='{w}'")
            f.append(f"gblur=sigma=6:enable='{w}'")
    return f


def structural_graph(kind: str | None, inp: str, out: str, frames: int, t_out: float, fps: float) -> str | None:
    """Стоп-кадр або перемотка на кінці кліпу. Повертає фрагмент filter_complex або None."""
    k = int(round(t_out * fps))
    if kind not in ("freeze", "rewind") or k <= 0 or k >= frames:
        return None
    body = frames - k
    if kind == "freeze":
        return (f"[{inp}]trim=end_frame={body},setpts=PTS-STARTPTS,"
                f"tpad=stop_mode=clone:stop={k},"
                f"eq=brightness='0.25*gte(t\\,{body / fps:.4f})*max(0\\,1-(t-{body / fps:.4f})*8)':eval=frame[{out}]")
    # перемотка: останні k кадрів — попередній матеріал кліпу задом наперед у 3–4 рази швидше
    speed = max(1, min(4, body // k))
    start = max(0, body - k * speed)
    return (f"[{inp}]split=2[ra_{out}][rb_{out}];"
            f"[ra_{out}]trim=end_frame={body},setpts=PTS-STARTPTS[rbody_{out}];"
            f"[rb_{out}]trim=start_frame={start}:end_frame={body},setpts=PTS-STARTPTS,reverse,"
            f"select='not(mod(n\\,{speed}))',setpts=N/({fps}*TB),trim=end_frame={k},"
            f"tpad=stop_mode=clone:stop={k},trim=end_frame={k},"
            f"format=yuv420p,noise=alls=35:allf=t,"
            f"drawbox=x=0:y='mod(t*{int(fps * 40)}\\,ih)':w=iw:h=ih/14:color=white@0.35:t=fill[rtail_{out}];"
            f"[rbody_{out}][rtail_{out}]concat=n=2:v=1:a=0[{out}]")
