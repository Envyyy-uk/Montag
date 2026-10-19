"""Аркуш кадрів: сітка рівномірно взятих кадрів готового відео — щоб «побачити» монтаж однією картинкою."""

from __future__ import annotations

import subprocess
from pathlib import Path

from render.ffmpeg import FFmpegError, atomic_output, commit_output


def _duration(video: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                         capture_output=True, text=True, timeout=30)
    try:
        return float(out.stdout.strip())
    except ValueError as exc:
        raise FFmpegError(f"не вдалося прочитати тривалість {video.name}") from exc


def contact_sheet(video: Path, out: Path, cols: int = 4, rows: int = 3, tile_width: int = 320) -> Path:
    """Кеш поруч із відео; перебудовується, якщо відео новіше."""
    if out.exists() and out.stat().st_mtime >= video.stat().st_mtime:
        return out
    n = cols * rows
    dur = _duration(video)
    # кадри по центрах n рівних відрізків, без першого і останнього кадру
    fps = n / max(dur, 0.1)
    tmp = atomic_output(out)
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(video), "-vf",
           f"fps={fps:.6f}:start_time={dur / n / 2:.3f},scale={tile_width}:-2,tile={cols}x{rows}:padding=4:color=black",
           "-frames:v", "1", "-q:v", "4", str(tmp)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if res.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise FFmpegError(res.stderr.strip()[-500:] or "аркуш кадрів не вдався")
    commit_output(tmp, out)
    return out
