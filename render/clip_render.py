"""Фінальний рендер таймлайну: стиль, переходи, дата зйомки → mp4 (NVENC) + 720p-превʼю.

Кожен кліп кодується окремо з точною кількістю кадрів (склейки рівно на кадрах таймлайну),
далі — concat без перекодування і зведений звук.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from edit.timeline import Timeline
from render.ffmpeg import atomic_output, commit_output, h264_args, run_ffmpeg
from render.looks import Style, fit_graph, look_filters, stamp_filters
from render.transitions import TRANSITIONS, edge_filters, structural_graph


@dataclass
class Source:
    path: Path
    width: int
    height: int
    shot_at: str | None = None


def clip_graph(src: Source, src_in: float, frames: int, fps: float, width: int, height: int,
               look: dict, kind_in: str | None = None, t_in: float = 0.0,
               kind_out: str | None = None, t_out: float = 0.0) -> str:
    """filter_complex одного кліпу: вписати → fps → (стоп-кадр/перемотка) → ефекти країв → стиль → дата."""
    dur = frames / fps
    parts = [fit_graph(src.width, src.height, width, height, "0:v", "fit"),
             f"[fit]fps={fps},setpts=PTS-STARTPTS[base]"]
    cur = "base"
    if kind_out and TRANSITIONS[kind_out].structural:
        g = structural_graph(kind_out, cur, "struct", frames, t_out, fps)
        if g:
            parts.append(g)
            cur = "struct"
        kind_out = None  # структурний перехід уже застосовано
    chain = edge_filters(kind_in, t_in, kind_out, t_out, dur, width, height, fps)
    chain += look_filters(look, width, height)
    chain += stamp_filters(look, height, src.shot_at, src_in)
    chain.append("format=yuv420p")
    parts.append(f"[{cur}]" + ",".join(chain) + "[v]")
    return ";".join(parts)


def render_clip_file(src: Source, src_in: float, frames: int, fps: float, out: Path, look: dict,
                     size: tuple[int, int], quality: str = "final", nvenc: bool | None = None, **edges) -> None:
    graph = clip_graph(src, src_in, frames, fps, size[0], size[1], look, **edges)
    run_ffmpeg(["-ss", f"{src_in:.4f}", "-t", f"{frames / fps + 1.0:.4f}", "-i", str(src.path),
                "-filter_complex", graph, "-map", "[v]", "-an", "-frames:v", str(frames),
                *h264_args(quality, nvenc), "-pix_fmt", "yuv420p", str(out)])


def render_timeline(tl: Timeline, style: Style, level: int, sources: dict[int, Source], audio: Path,
                    out: Path, preview: Path | None = None, transitions: bool = True,
                    size: tuple[int, int] = (1920, 1080), nvenc: bool | None = None,
                    on_progress: Callable[[float], None] | None = None) -> None:
    look = style.look(level)
    tmpdir = Path(tempfile.mkdtemp(prefix="render_", dir=out.parent))
    try:
        parts = []
        n = len(tl.clips)
        for i, c in enumerate(tl.clips):
            frames = tl.frame(c.end) - tl.frame(c.start)
            if frames <= 0:
                continue
            edges = {}
            if transitions:
                tin, tout = tl.transition_at(i), tl.transition_at(i + 1)
                if tin and tin["in"] > 0:
                    edges.update(kind_in=tin["kind"], t_in=tin["in"])
                if tout and tout["out"] > 0:
                    edges.update(kind_out=tout["kind"], t_out=tout["out"])
            part = tmpdir / f"{i:04d}.mp4"
            render_clip_file(sources[c.media_id], c.src_in, frames, tl.fps, part, look, size, "final", nvenc, **edges)
            parts.append(part)
            if on_progress:
                on_progress(0.85 * (i + 1) / n)
        listing = tmpdir / "list.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        tmp = atomic_output(out)
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(audio),
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k",
                    "-t", f"{tl.frame(tl.duration) / tl.fps:.4f}", "-movflags", "+faststart", str(tmp)])
        commit_output(tmp, out)
        if preview is not None:
            tmp = atomic_output(preview)
            run_ffmpeg(["-i", str(out), "-vf", f"scale=-2:{min(720, size[1])}:flags=lanczos", *h264_args("preview", nvenc),
                        "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(tmp)])
            commit_output(tmp, preview)
        if on_progress:
            on_progress(1.0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def render_style_test(style: Style, level: int, src: Source, start: float, out: Path, seconds: float = 5.0,
                      fps: float = 30.0, size: tuple[int, int] = (1920, 1080), nvenc: bool | None = None) -> None:
    """Тестовий 5-секундний шматок стилю для затвердження рівня інтенсивності."""
    tmp = atomic_output(out)
    render_clip_file(src, start, int(round(seconds * fps)), fps, tmp, style.look(level), size, "final", nvenc)
    commit_output(tmp, out)
