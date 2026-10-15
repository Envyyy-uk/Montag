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


def analysis_video(path: Path) -> Path:
    """Склейка з відомими ділянками (по 3 с, 640×360, 30 fps):
    0–3 різке, 3–6 темрява, 6–9 розмите, 9–12 тряска, 12–15 однотонне, 15–24 різке без склейок.
    Звук: тихий тон + шумовий сплеск «натовпу» на 19–21 с."""
    S = "640x360"
    graph = (
        f"testsrc2=s={S}:r=30:d=3[a];"
        f"color=c=black:s={S}:r=30:d=3,noise=alls=3:allf=t[b];"
        f"testsrc2=s={S}:r=30:d=3,boxblur=12:2[c];"
        f"testsrc2=s=800x500:r=30:d=3,crop=640:360:'80+60*sin(n*2.3)':'70+50*cos(n*3.1)'[d];"
        f"color=c=0x707070:s={S}:r=30:d=3[e];"
        f"mandelbrot=s={S}:r=30,trim=duration=9,setpts=PTS-STARTPTS[f];"
        "[a][b][c][d][e][f]concat=n=6:v=1:a=0,format=yuv420p[v];"
        "sine=f=330:d=24,volume=0.05[tone];"
        "anoisesrc=d=2:c=pink:a=0.6,adelay=19000|19000,apad=whole_dur=24[crowd];"
        "[tone][crowd]amix=inputs=2:duration=first:normalize=0[aout]"
    )
    ff("-filter_complex", graph, "-map", "[v]", "-map", "[aout]", "-c:v", "libx264", "-preset", "ultrafast",
       "-crf", "18", "-c:a", "aac", "-t", "24", str(path))
    return path
