"""Спільне для всіх викликів FFmpeg: енкодер, фільтри, запуск з прогресом."""

from __future__ import annotations

import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Callable


class FFmpegError(RuntimeError):
    pass


@lru_cache
def available_filters(ffmpeg: str = "ffmpeg") -> frozenset[str]:
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 3 and "->" in parts[2]:
            names.add(parts[1])
    return frozenset(names)


@lru_cache
def nvenc_available() -> bool:
    from core.system_check import check_nvenc
    return check_nvenc().ok


def h264_args(quality: str, nvenc: bool | None = None) -> list[str]:
    """Параметри H.264. quality: 'master' (проксі), 'final' (готовий mp4), 'preview' (720p для телефона),
    'low' (480p для Gemini)."""
    if nvenc is None:
        nvenc = nvenc_available()
    if nvenc:
        common = ["-c:v", "h264_nvenc", "-preset", "p5", "-profile:v", "high"]
        if quality == "low":
            return common + ["-rc", "vbr", "-b:v", "500k", "-maxrate", "700k", "-bufsize", "1400k"]
        if quality == "preview":
            return common + ["-rc", "vbr", "-cq", "27", "-b:v", "0", "-maxrate", "3M", "-bufsize", "6M"]
        if quality == "final":
            return common + ["-rc", "vbr", "-cq", "19", "-b:v", "0"]
        return common + ["-rc", "vbr", "-cq", "21", "-b:v", "0"]
    common = ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high"]
    if quality == "low":
        return common + ["-crf", "30", "-maxrate", "700k", "-bufsize", "1400k"]
    if quality == "preview":
        return common + ["-crf", "26", "-maxrate", "3M", "-bufsize", "6M"]
    if quality == "final":
        return common + ["-crf", "18"]
    return common + ["-crf", "20"]


def run_ffmpeg(args: list[str], duration: float = 0.0,
               on_progress: Callable[[float], None] | None = None,
               ffmpeg: str = "ffmpeg") -> None:
    """Запускає ffmpeg, читає `-progress` і повідомляє частку 0..1."""
    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error",
           "-progress", "pipe:1", "-nostats", *args]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    assert proc.stdout is not None
    for line in proc.stdout:
        if on_progress and duration > 0 and line.startswith("out_time_us="):
            try:
                on_progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / duration))
            except ValueError:
                pass
    stderr = proc.stderr.read() if proc.stderr else ""
    if proc.wait() != 0:
        raise FFmpegError(stderr.strip()[-1500:] or "ffmpeg завершився з помилкою")


def atomic_output(final: Path) -> Path:
    """Тимчасовий шлях поруч із фінальним; після успіху — `commit_output`."""
    final.parent.mkdir(parents=True, exist_ok=True)
    return final.with_name(f".{final.stem}.part{final.suffix}")


def commit_output(tmp: Path, final: Path) -> None:
    os.replace(tmp, final)


def which_or_raise(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise FFmpegError(f"{name} не знайдено в PATH")
    return path
