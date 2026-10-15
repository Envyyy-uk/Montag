"""Генерація коротких тестових файлів через ffmpeg (у git не потрапляють)."""

import subprocess
from pathlib import Path


def ff(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def sdr_video(path: Path, w=1280, h=720, dur=2, fps=30, creation="2025-06-14T21:03:05.000000Z") -> Path:
    ff("-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={fps}:d={dur}",
       "-f", "lavfi", "-i", f"sine=f=440:d={dur}",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
       "-metadata", f"creation_time={creation}", str(path))
    return path


def hdr_video(path: Path, w=640, h=360, dur=1) -> Path:
    """HEVC 10 біт, HLG, BT.2020 — як iPhone."""
    ff("-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r=30:d={dur}",
       "-c:v", "libx265", "-preset", "ultrafast", "-pix_fmt", "yuv420p10le",
       "-x265-params", "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
       "-color_primaries", "bt2020", "-color_trc", "arib-std-b67", "-colorspace", "bt2020nc",
       "-tag:v", "hvc1", str(path))
    return path


def track(path: Path, dur=3) -> Path:
    ff("-f", "lavfi", "-i", f"sine=f=220:d={dur}", "-c:a", "libmp3lame", "-b:a", "128k", str(path))
    return path
