"""Експорт таймлайну у FCPXML 1.9 для DaVinci Resolve.

Доріжки:
- V1 (основна сюжетна лінія, spine) — оригінальні кліпи без стилю, вимкнені (`enabled="0"`), лише відео;
- V2 (lane 1) — ті самі кліпи зі стилем, увімкнені; файли мають запас до 1 с з кожного боку;
- A1 (lane −1) — трек, з автоматизацією приглушення під живими вставками;
- A2 (lane −2) — живі вставки з тією ж гучністю, що в готовому mp4.
Маркери (дроп, приспів, вставки) — на кліпах V1.

Усі часи — цілі кадри послідовності, записані раціональними числами FCPXML.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from xml.sax.saxutils import quoteattr

FRAME_DURATIONS = {23.976: Fraction(1001, 24000), 29.97: Fraction(1001, 30000), 59.94: Fraction(1001, 60000)}


def frame_duration(fps: float) -> Fraction:
    for k, v in FRAME_DURATIONS.items():
        if abs(fps - k) < 0.01:
            return v
    return Fraction(1, round(fps))


def rt(seconds: Fraction) -> str:
    """Раціональний час FCPXML: «1001/30000s», «3s», «0s»."""
    seconds = Fraction(seconds)
    if seconds.denominator == 1:
        return f"{seconds.numerator}s"
    return f"{seconds.numerator}/{seconds.denominator}s"


def timecode_seconds(tc: str | None, fps: float) -> Fraction:
    """«21:03:05:12» (або «;» для drop-frame) → секунди. Порожній — 0."""
    if not tc:
        return Fraction(0)
    drop = ";" in tc
    try:
        parts = [int(p) for p in tc.replace(";", ":").split(":")]
    except ValueError:
        return Fraction(0)
    if len(parts) != 4:
        return Fraction(0)
    h, m, s, f = parts
    nominal = round(fps)
    frames = ((h * 60 + m) * 60 + s) * nominal + f
    if drop:  # SMPTE drop-frame: пропуск 2 (4 для 59.94) номерів щохвилини, крім кожної десятої
        d = 2 if nominal == 30 else 4
        total_min = h * 60 + m
        frames -= d * (total_min - total_min // 10)
    return frames * frame_duration(fps)


@dataclass
class Asset:
    key: str
    name: str
    path: Path
    duration: Fraction
    has_video: bool
    has_audio: bool
    width: int = 0
    height: int = 0
    fps: float = 30.0
    start: Fraction = Fraction(0)       # таймкод початку файлу
    audio_channels: int = 2
    audio_rate: int = 48000


@dataclass
class Item:
    asset: str
    offset: int                  # кадр таймлайну
    duration: int                # кадрів
    start: Fraction              # точка входу в джерело, с (від початку файлу, без таймкоду)
    lane: int = 0                # 0 — V1 (spine), 1 — V2, −1 — A1, −2 — A2
    enabled: bool = True
    video_only: bool = False
    audio_only: bool = False
    volume_db: float | None = None
    volume_keys: list[tuple[float, float]] = field(default_factory=list)  # (с від початку кліпу, дБ)
    name: str = ""


@dataclass
class MarkerAt:
    frame: int
    value: str


def media_url(path: Path, base: Path | None) -> str:
    """Відносний URL (від папки .fcpxml) або абсолютний file://."""
    if base is not None:
        rel = Path(path).resolve().relative_to(base.resolve())
        return urllib.parse.quote(rel.as_posix())
    return Path(path).resolve().as_uri()


def build(project_name: str, fps: float, width: int, height: int, total_frames: int,
          assets: list[Asset], items: list[Item], markers: list[MarkerAt], base: Path | None) -> str:
    fd = frame_duration(fps)
    t = lambda frames: rt(frames * fd)  # noqa: E731

    spine = sorted((i for i in items if i.lane == 0), key=lambda i: i.offset)
    if not spine:
        raise ValueError("Порожня основна доріжка")
    by_key = {a.key: a for a in assets}

    out = ['<?xml version="1.0" encoding="UTF-8"?>', "<!DOCTYPE fcpxml>", '<fcpxml version="1.9">', "  <resources>",
           f'    <format id="r0" frameDuration="{rt(fd)}" width="{width}" height="{height}"/>']
    fmt_ids: dict[tuple, str] = {}
    for a in assets:
        if a.has_video:
            k = (a.width, a.height, round(a.fps, 3))
            if k not in fmt_ids:
                fmt_ids[k] = f"r{len(fmt_ids) + 1}"
                out.append(f'    <format id="{fmt_ids[k]}" frameDuration="{rt(frame_duration(a.fps))}"'
                           f' width="{a.width}" height="{a.height}"/>')
    for a in assets:
        fmt = f' format="{fmt_ids[(a.width, a.height, round(a.fps, 3))]}"' if a.has_video else ""
        audio = (f' audioSources="1" audioChannels="{a.audio_channels}" audioRate="{a.audio_rate}"'
                 if a.has_audio else "")
        out.append(f'    <asset id="{a.key}" name={quoteattr(a.name)} start="{rt(a.start)}" duration="{rt(a.duration)}"'
                   f' hasVideo="{int(a.has_video)}" hasAudio="{int(a.has_audio)}"{fmt}{audio}>')
        out.append(f'      <media-rep kind="original-media" src={quoteattr(media_url(a.path, base))}/>')
        out.append("    </asset>")
    out += ["  </resources>", "  <library>", '    <event name="Автомонтаж">',
            f"      <project name={quoteattr(project_name)}>",
            f'        <sequence format="r0" duration="{t(total_frames)}" tcStart="0s" tcFormat="NDF"'
            f' audioLayout="stereo" audioRate="48k">', "          <spine>"]

    def clip_xml(it: Item, indent: str, offset_time: Fraction, children: list[str]) -> list[str]:
        a = by_key[it.asset]
        attrs = [f'ref="{a.key}"', f"name={quoteattr(it.name or a.name)}", f'offset="{rt(offset_time)}"',
                 f'start="{rt(a.start + it.start)}"', f'duration="{t(it.duration)}"']
        if it.lane:
            attrs.append(f'lane="{it.lane}"')
        if not it.enabled:
            attrs.append('enabled="0"')
        if it.video_only:
            attrs.append('srcEnable="video"')
        if it.audio_only:
            attrs.append('srcEnable="audio"')
        body = []
        if it.volume_keys:
            body += [f'{indent}  <adjust-volume>', f'{indent}    <param name="amount">',
                     f'{indent}      <keyframeAnimation>']
            for sec, db in it.volume_keys:
                body.append(f'{indent}        <keyframe time="{rt(a.start + it.start + Fraction(sec).limit_denominator(48000))}"'
                            f' value="{db:+.2f}dB"/>')
            body += [f'{indent}      </keyframeAnimation>', f'{indent}    </param>', f'{indent}  </adjust-volume>']
        elif it.volume_db is not None and abs(it.volume_db) > 0.01:
            body.append(f'{indent}  <adjust-volume amount="{it.volume_db:+.2f}dB"/>')
        body += children
        if not body:
            return [f"{indent}<asset-clip {' '.join(attrs)}/>"]
        return [f"{indent}<asset-clip {' '.join(attrs)}>", *body, f"{indent}</asset-clip>"]

    # прив'язка: кожен приєднаний кліп і маркер — до кліпу V1, що звучить у цей момент
    def parent_of(frame: int) -> int:
        idx = 0
        for k, s in enumerate(spine):
            if s.offset <= frame:
                idx = k
        return idx

    attached: dict[int, list[Item]] = {}
    for it in items:
        if it.lane != 0:
            attached.setdefault(parent_of(it.offset), []).append(it)
    marks: dict[int, list[MarkerAt]] = {}
    for m in markers:
        marks.setdefault(parent_of(m.frame), []).append(m)

    for k, s in enumerate(spine):
        a = by_key[s.asset]
        local0 = a.start + s.start  # локальний час батька в момент його початку
        children = []
        for m in marks.get(k, []):
            children.append(f'              <marker start="{rt(local0 + (m.frame - s.offset) * fd)}"'
                            f' duration="{rt(fd)}" value={quoteattr(m.value)}/>')
        for c in sorted(attached.get(k, []), key=lambda c: (-c.lane, c.offset)):
            children += clip_xml(c, "              ", local0 + (c.offset - s.offset) * fd, [])
        out += clip_xml(s, "            ", s.offset * fd, children)
    out += ["          </spine>", "        </sequence>", "      </project>", "    </event>", "  </library>",
            "</fcpxml>", ""]
    return "\n".join(out)
