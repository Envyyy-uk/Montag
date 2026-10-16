"""Синтетичний «концерт» для тестових рендерів стилів, коли під рукою немає справжнього відео."""

from __future__ import annotations

import subprocess
from pathlib import Path


def stage_clip(path: Path, seconds: float = 5.0, size: str = "1920x1080") -> Path:
    """Темна сцена з кольоровими прожекторами, що рухаються, світлими плямами і силуетами натовпу."""
    w, h = (int(x) for x in size.split("x"))
    crowd = ",".join(
        f"drawbox=x={int(i * w / 14 - w / 40)}:y='{int(h * 0.78)}+{int(h * 0.03)}*sin(t*{3 + i % 4}+{i})'"
        f":w={int(w / 11)}:h={int(h * 0.4)}:color=black:t=fill"
        for i in range(15))
    heads = ",".join(
        f"drawbox=x={int(i * w / 14)}:y='{int(h * 0.70)}+{int(h * 0.03)}*sin(t*{3 + i % 4}+{i})'"
        f":w={int(w / 22)}:h={int(h * 0.09)}:color=black:t=fill"
        for i in range(15))
    graph = (
        f"gradients=s={size}:c0=0x05010d:c1=0x3a0a6a:c2=0xff2a6d:c3=0x05010d:c4=0x1a40ff:nb_colors=5:"
        f"x0=0:y0=0:x1={w}:y1={h}:speed=0.03:rate=30,eq=gamma=1.6:contrast=1.3,"
        f"drawbox=x='{w // 2}+{w // 3}*sin(t*1.3)':y=40:w={w // 9}:h={w // 9}:color=white@0.85:t=fill,"
        f"gblur=sigma=40,{crowd},{heads},format=yuv420p"
    )
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", graph,
                    "-t", f"{seconds}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
                    "-metadata", "creation_time=2025-06-14T18:03:05Z", str(path)], check=True)
    return path
