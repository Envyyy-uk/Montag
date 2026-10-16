"""Рендер вертикального ролика 9:16 (H.264, 30 fps) зі стилем і датою зйомки."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import numpy as np

from analysis.framing import Framing, crop_x_expr
from edit import audio_mix
from render.clip_render import Source
from render.ffmpeg import atomic_output, commit_output, h264_args, run_ffmpeg
from render.looks import fit_graph, look_filters, stamp_filters

FPS = 30.0


def piece_graph(src: Source, framing: Framing, width: int, height: int, look: dict, src_in: float) -> str:
    if framing.mode == "crop":
        window = (src.height * 9 / 16) / src.width
        x = crop_x_expr(framing.keys, window)
        head = (f"[0:v]setpts=PTS-STARTPTS,crop=w=trunc(ih*9/16/2)*2:h=ih:x='{x}':y=0,"
                f"scale={width}:{height}:flags=lanczos,setsar=1[fit]")
    else:  # native (вертикальний оригінал) або fit (розмитий фон)
        head = fit_graph(src.width, src.height, width, height, "0:v", "fit")
    chain = [f"fps={FPS}"] + look_filters(look, width, height) + stamp_filters(look, height, src.shot_at, src_in)
    chain.append("format=yuv420p")
    return f"{head};[fit]" + ",".join(chain) + "[v]"


def render_reel(pieces: list[tuple[Source, float, float, Framing]], look: dict, audio: np.ndarray | None,
                out: Path, size: tuple[int, int] = (1080, 1920), nvenc: bool | None = None) -> float:
    """pieces — (джерело, точка входу, тривалість, кадрування). Повертає тривалість ролика."""
    tmpdir = Path(tempfile.mkdtemp(prefix="reel_", dir=out.parent))
    try:
        parts, frames_total = [], 0
        for i, (src, src_in, dur, framing) in enumerate(pieces):
            frames = int(round(dur * FPS))
            if frames <= 0:
                continue
            part = tmpdir / f"{i:03d}.mp4"
            run_ffmpeg(["-ss", f"{src_in:.4f}", "-t", f"{dur + 1:.4f}", "-i", str(src.path),
                        "-filter_complex", piece_graph(src, framing, size[0], size[1], look, src_in),
                        "-map", "[v]", "-an", "-frames:v", str(frames), *h264_args("final", nvenc),
                        "-pix_fmt", "yuv420p", str(part)])
            parts.append(part)
            frames_total += frames
        duration = frames_total / FPS
        listing = tmpdir / "list.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        wav = tmpdir / "a.wav"
        n = int(round(duration * audio_mix.SR))
        a = np.zeros((n, 2), np.float32) if audio is None else np.pad(audio[:n], ((0, max(0, n - len(audio))), (0, 0)))
        audio_mix.write_wav(wav, a)
        tmp = atomic_output(out)
        run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(listing), "-i", str(wav), "-map", "0:v:0", "-map", "1:a:0",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-t", f"{duration:.4f}",
                    "-movflags", "+faststart", str(tmp)])
        commit_output(tmp, out)
        return duration
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def live_audio(pieces: list[tuple[Path, float, float]], fade: float = 0.03) -> np.ndarray:
    """Живий звук шматків із короткими фейдами на склейках (без клацань)."""
    chunks = []
    for path, src_in, dur in pieces:
        x = audio_mix.decode_stereo(path, src_in, round(dur * FPS) / FPS)
        f = min(int(fade * audio_mix.SR), len(x) // 2)
        if f:
            ramp = np.linspace(0, 1, f, dtype=np.float32)[:, None]
            x[:f] *= ramp
            x[-f:] *= ramp[::-1]
        chunks.append(x)
    return np.concatenate(chunks) if chunks else np.zeros((0, 2), np.float32)
