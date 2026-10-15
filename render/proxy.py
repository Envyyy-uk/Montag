"""Проксі: 1080p H.264 для монтажу і 480p низького бітрейту для Gemini.

HDR (HLG/PQ з iPhone) переводиться в SDR Rec.709 з тональним мапінгом.
Поворот застосовує сам ffmpeg (autorotate), тож проксі завжди «як видно».
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from analysis.probe import MediaInfo
from render.ffmpeg import atomic_output, available_filters, commit_output, h264_args, run_ffmpeg


@dataclass(frozen=True)
class ProxySpec:
    name: str
    short_side: int
    quality: str
    max_fps: float | None
    audio: list[str]


EDIT = ProxySpec("1080", 1080, "master", None, ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"])
GEMINI = ProxySpec("480", 480, "low", 15.0, ["-c:a", "aac", "-b:a", "64k", "-ar", "32000", "-ac", "1"])

# zscale (zimg): лінеаризація → BT.709 праймері → тональний мапінг → BT.709 гамма.
_TONEMAP_ZSCALE = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                   "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv")
_TONEMAP_PLACEBO = ("libplacebo=tonemapping=bt.2390:colorspace=bt709:color_primaries=bt709"
                    ":color_trc=bt709:range=tv")


def target_size(width: int, height: int, short_side: int) -> tuple[int, int]:
    """Зменшує до короткої сторони `short_side` (без збільшення), парні розміри."""
    short = min(width, height)
    if short <= short_side:
        w, h = width, height
    else:
        k = short_side / short
        w, h = width * k, height * k
    return max(2, 2 * round(w / 2)), max(2, 2 * round(h / 2))


def tonemap_filter(filters: frozenset[str]) -> str | None:
    if {"zscale", "tonemap"} <= filters:
        return _TONEMAP_ZSCALE
    if "libplacebo" in filters:
        return _TONEMAP_PLACEBO
    return None


def build_filter(info: MediaInfo, spec: ProxySpec, filters: frozenset[str]) -> tuple[str, list[str]]:
    """Повертає (ланцюжок -vf, попередження)."""
    chain, warnings = [], []
    if info.hdr:
        tm = tonemap_filter(filters)
        if tm:
            chain.append(tm)
        else:
            warnings.append("FFmpeg без zscale/libplacebo: HDR→SDR без тонального мапінгу, кольори будуть бляклі")
    w, h = target_size(info.display_width, info.display_height, spec.short_side)
    chain.append(f"scale={w}:{h}:flags=lanczos")
    if spec.max_fps:
        # Постійна частота: аналіз рахує час кадру як номер/fps.
        chain.append(f"fps={min(spec.max_fps, info.fps) if info.fps > 0 else spec.max_fps:g}")
    chain.append("format=yuv420p")
    return ",".join(chain), warnings


def build_command(src: Path, dst: Path, info: MediaInfo, spec: ProxySpec,
                  filters: frozenset[str], nvenc: bool | None = None) -> tuple[list[str], list[str]]:
    vf, warnings = build_filter(info, spec, filters)
    args = ["-i", str(src), "-map", "0:v:0", "-map", "0:a:0?", "-vf", vf,
            *h264_args(spec.quality, nvenc),
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
            "-force_key_frames", "expr:gte(t,n_forced*1)",
            *spec.audio, "-movflags", "+faststart", "-map_metadata", "0", str(dst)]
    return args, warnings


def make_proxy(src: Path, dst: Path, info: MediaInfo, spec: ProxySpec,
               on_progress: Callable[[float], None] | None = None,
               nvenc: bool | None = None) -> list[str]:
    """Робить проксі атомарно. Повертає попередження."""
    if not info.has_video:
        raise ValueError("Проксі робляться лише для відео")
    tmp = atomic_output(dst)
    args, warnings = build_command(src, tmp, info, spec, available_filters(), nvenc)
    try:
        run_ffmpeg(args, info.duration, on_progress)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    commit_output(tmp, dst)
    return warnings
