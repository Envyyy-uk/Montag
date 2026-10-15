"""ffprobe: технічні дані файлу."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from fractions import Fraction

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10/Dolby Vision) і HLG (iPhone)


class ProbeError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    duration: float
    has_video: bool
    has_audio: bool
    width: int = 0
    height: int = 0
    fps: float = 0.0
    codec: str = ""
    pix_fmt: str = ""
    bit_depth: int = 8
    color_transfer: str = ""
    color_primaries: str = ""
    hdr: bool = False
    rotation: int = 0
    shot_at: str | None = None  # ISO 8601 UTC
    audio_codec: str = ""
    sample_rate: int = 0

    @property
    def display_width(self) -> int:
        return self.height if self.rotation % 180 else self.width

    @property
    def display_height(self) -> int:
        return self.width if self.rotation % 180 else self.height

    @property
    def orientation(self) -> str:
        if not self.has_video:
            return "none"
        if self.display_height > self.display_width:
            return "vertical"
        return "horizontal" if self.display_width > self.display_height else "square"

    def as_dict(self) -> dict:
        d = asdict(self)
        d.update(display_width=self.display_width, display_height=self.display_height,
                 orientation=self.orientation)
        return d


def _fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        val = stream.get(key) or "0/0"
        try:
            f = Fraction(val)
        except (ValueError, ZeroDivisionError):
            continue
        if f > 0:
            return round(float(f), 3)
    return 0.0


def _rotation(stream: dict) -> int:
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            return int(round(float(sd["rotation"]))) % 360
    rot = (stream.get("tags") or {}).get("rotate")
    return int(rot) % 360 if rot else 0


def _bit_depth(stream: dict) -> int:
    if stream.get("bits_per_raw_sample"):
        try:
            return int(stream["bits_per_raw_sample"])
        except ValueError:
            pass
    m = re.search(r"p(10|12|16)", stream.get("pix_fmt") or "")
    return int(m.group(1)) if m else 8


def _parse_time(value: str) -> str | None:
    value = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S"):
        try:
            dt = datetime.strptime(value.replace("Z", "+0000") if "%z" in fmt else value, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt.year < 1990:  # камери без годинника пишуть 1970/1904
            return None
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def _shot_at(data: dict) -> str | None:
    tags = {k.lower(): v for k, v in ((data.get("format") or {}).get("tags") or {}).items()}
    for s in data.get("streams") or []:
        for k, v in (s.get("tags") or {}).items():
            tags.setdefault(k.lower(), v)
    # iPhone пише локальний час з поясом сюди — він точніший за creation_time.
    for key in ("com.apple.quicktime.creationdate", "creation_time", "date"):
        if key in tags:
            parsed = _parse_time(tags[key])
            if parsed:
                return parsed
    return None


def parse(data: dict) -> MediaInfo:
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}
    duration = float(fmt.get("duration") or (video or audio or {}).get("duration") or 0)
    if video is None and audio is None:
        raise ProbeError("У файлі немає ні відео, ні звуку")

    info = MediaInfo(duration=duration, has_video=video is not None, has_audio=audio is not None,
                     shot_at=_shot_at(data))
    if video:
        info.width = int(video.get("width") or 0)
        info.height = int(video.get("height") or 0)
        info.fps = _fps(video)
        info.codec = video.get("codec_name") or ""
        info.pix_fmt = video.get("pix_fmt") or ""
        info.bit_depth = _bit_depth(video)
        info.color_transfer = video.get("color_transfer") or ""
        info.color_primaries = video.get("color_primaries") or ""
        info.hdr = info.color_transfer in HDR_TRANSFERS
        info.rotation = _rotation(video)
    if audio:
        info.audio_codec = audio.get("codec_name") or ""
        info.sample_rate = int(audio.get("sample_rate") or 0)
    return info


def ffprobe(path, ffprobe_bin: str = "ffprobe") -> dict:
    cmd = [ffprobe_bin, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProbeError(f"ffprobe не запускається: {exc}") from exc
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe не зміг прочитати файл: {proc.stderr.strip()[-500:]}")
    return json.loads(proc.stdout or "{}")


def probe(path, ffprobe_bin: str = "ffprobe") -> tuple[MediaInfo, dict]:
    raw = ffprobe(path, ffprobe_bin)
    return parse(raw), raw
