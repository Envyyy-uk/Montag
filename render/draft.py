"""Чорнове превʼю таймлайну: лише склейки (без стилю і переходів), 720p.

Кожен кліп кодується окремо з точною кількістю кадрів, далі — склейка
concat-демуксером без перекодування і зведений звук. Так склейки лягають
рівно на кадри таймлайну.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable

from edit.timeline import Timeline
from render.ffmpeg import atomic_output, commit_output, h264_args, run_ffmpeg

W, H = 1280, 720


def _fit(w: int, h: int) -> str:
    """Вписати в кадр із полями (вертикальні джерела в горизонтальному кліпі)."""
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=bicubic,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")


def render_draft(tl: Timeline, sources: dict[int, Path], audio: Path, out: Path,
                 on_progress: Callable[[float], None] | None = None, nvenc: bool | None = None,
                 size: tuple[int, int] = (W, H)) -> None:
    tmpdir = Path(tempfile.mkdtemp(prefix="draft_", dir=out.parent))
    try:
        parts = []
        for i, c in enumerate(tl.clips):
            frames = tl.frame(c.end) - tl.frame(c.start)
            if frames <= 0:
                continue
            part = tmpdir / f"{i:04d}.mp4"
            run_ffmpeg(["-ss", f"{c.src_in:.4f}", "-i", str(sources[c.media_id]), "-an",
                        "-vf", f"{_fit(*size)},fps={tl.fps}", "-frames:v", str(frames),
                        *h264_args("master", nvenc), "-pix_fmt", "yuv420p", str(part)])
            parts.append(part)
            if on_progress:
                on_progress(0.9 * (i + 1) / len(tl.clips))
        listing = tmpdir / "list.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        tmp = atomic_output(out)
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(audio),
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-t", f"{tl.frame(tl.duration) / tl.fps:.4f}", "-movflags", "+faststart", str(tmp)])
        commit_output(tmp, out)
        if on_progress:
            on_progress(1.0)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
