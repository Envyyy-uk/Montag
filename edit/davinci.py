"""Проєкт DaVinci Resolve: папка з медіа (відносні шляхи) + FCPXML.

exports/<проєкт>_clip_vNNN_davinci/
  <проєкт>_clip_vNNN.fcpxml             — відносні шляхи (папку можна переносити)
  <проєкт>_clip_vNNN_абсолютні.fcpxml   — запасний варіант з повними шляхами на цьому ноутбуці
  media/originals/   — оригінали (жорсткі посилання — без зайвого місця на диску)
  media/styled/      — стилізовані кліпи для V2 з запасом до 1 с з кожного боку
  media/audio/       — трек
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from fractions import Fraction
from pathlib import Path
from typing import Callable

from analysis.probe import ffprobe, parse
from core.config import Settings
from edit.timeline import Timeline
from render import fcpxml
from render.audio_keys import duck_keyframes
from render.clip_render import Source, render_clip_file
from render.looks import get_style

log = logging.getLogger(__name__)

HANDLE = 1.0


def link_or_copy(src: Path, dst: Path) -> None:
    """Жорстке посилання на оригінал (той самий диск), інакше — копія. Оригінал не змінюється."""
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _asset_from_file(key: str, path: Path) -> fcpxml.Asset:
    info = parse(ffprobe(path))
    fps = info.fps or 30.0
    fd = fcpxml.frame_duration(fps)
    dur = Fraction(info.duration).limit_denominator(10**6)
    dur_frames = -(-dur // fd) if info.has_video else None  # вгору до кадру
    return fcpxml.Asset(
        key=key, name=path.stem, path=path,
        duration=(dur_frames * fd) if dur_frames is not None else Fraction(round(info.duration * 48000), 48000),
        has_video=info.has_video, has_audio=info.has_audio,
        width=info.display_width, height=info.display_height, fps=fps,
        start=fcpxml.timecode_seconds(info.timecode, fps),
        audio_channels=2, audio_rate=info.sample_rate or 48000)


def export(conn: sqlite3.Connection, settings: Settings, edit_id: int,
           progress: Callable[[float], None] = lambda p: None) -> Path:
    lib = settings.library_path
    edit = conn.execute("SELECT * FROM edits WHERE id = ?", (edit_id,)).fetchone()
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (edit["project_id"],)).fetchone()
    tl = Timeline.load(lib / edit["timeline_path"])
    fps = tl.fps
    fd = fcpxml.frame_duration(fps)
    name = Path(edit["export_path"]).stem if edit["export_path"] else f"{project['slug']}_clip_v{edit['version']:03d}"
    root = lib / project["slug"] / "exports" / f"{name}_davinci"
    media_dir = root / "media"
    from edit.build import clip_size
    width, height = clip_size(settings, tl.params)  # формат кліпу: 16:9 / 9:16 / 1:1 / 4:5
    fit_mode = tl.params.get("fit_mode", "auto")
    style = get_style(edit["style_id"] or settings.default_style)
    look = style.look(edit["style_level"] or style.default_level)

    assets: dict[str, fcpxml.Asset] = {}
    rows = {m["id"]: m for m in conn.execute("SELECT * FROM media WHERE project_id = ?", (project["id"],))}

    def original(media_id: int) -> str:
        key = f"o{media_id}"
        if key not in assets:
            m = rows[media_id]
            dst = media_dir / "originals" / f"{media_id}_{m['filename']}"
            link_or_copy(lib / m["original_path"], dst)
            assets[key] = _asset_from_file(key, dst)
        return key

    items: list[fcpxml.Item] = []
    n = len(tl.clips)
    for i, c in enumerate(tl.clips):
        off, end = tl.frame(c.start), tl.frame(c.end)
        dur = end - off
        if dur <= 0:
            continue
        m = rows[c.media_id]
        src_in_f = round(c.src_in * fps)
        # V1 — оригінал, вимкнений
        items.append(fcpxml.Item(original(c.media_id), off, dur, src_in_f * fd, lane=0, enabled=False,
                                 video_only=True, name=m["filename"]))
        # V2 — стилізований файл з запасом
        info = json.loads(m["probe_json"] or "{}")
        head = min(round(HANDLE * fps), src_in_f)
        tail = max(0, min(round(HANDLE * fps), int((m["duration"] or 0) * fps) - (src_in_f + dur)))
        styled = media_dir / "styled" / f"{i + 1:03d}_{c.media_id}.mp4"
        if not styled.exists():
            src = Source(lib / (m["proxy_path"] or m["original_path"]), info.get("display_width") or 1920,
                         info.get("display_height") or 1080, m["shot_at"])
            styled.parent.mkdir(parents=True, exist_ok=True)
            tmp = styled.with_name(f".{styled.stem}.part.mp4")
            render_clip_file(src, (src_in_f - head) / fps, head + dur + tail, fps, tmp, look, (width, height),
                             fit_mode=fit_mode)
            tmp.replace(styled)
        key = f"s{i + 1}"
        assets[key] = fcpxml.Asset(key, styled.stem, styled, (head + dur + tail) * fd, True, False,
                                   width, height, fps)
        items.append(fcpxml.Item(key, off, dur, head * fd, lane=1, name=f"{m['filename']} · стиль"))
        progress(0.9 * (i + 1) / n)

    # A1 — трек з приглушенням під вставками
    track = rows[tl.track_media_id]
    track_dst = media_dir / "audio" / f"{track['id']}_{track['filename']}"
    link_or_copy(lib / track["original_path"], track_dst)
    assets["t"] = _asset_from_file("t", track_dst)
    total = tl.frame(tl.duration)
    base_db = float(tl.params.get("track_gain_db", 0.0))
    keys = duck_keyframes([(ins.start, ins.end, ins.duck_db) for ins in tl.inserts], base_db) if tl.inserts else []
    items.append(fcpxml.Item("t", 0, total, round(tl.track_in * fps) * fd, lane=-1, audio_only=True,
                             volume_db=base_db, volume_keys=keys, name=track["filename"]))
    # A2 — живі вставки
    for ins in tl.inserts:
        off, dur = tl.frame(ins.start), tl.frame(ins.end) - tl.frame(ins.start)
        items.append(fcpxml.Item(original(ins.media_id), off, dur, round(ins.src_in * fps) * fd, lane=-2,
                                 audio_only=True, volume_db=ins.applied_gain_db,
                                 name=f"{rows[ins.media_id]['filename']} · натовп"))

    markers = [fcpxml.MarkerAt(tl.frame(mk.t), mk.label or mk.kind) for mk in tl.markers]
    title = f"{project['name']} — кліп v{edit['version']:03d}"
    asset_list = list(assets.values())
    rel = fcpxml.build(title, fps, width, height, total, asset_list, items, markers, base=root)
    absolute = fcpxml.build(title, fps, width, height, total, asset_list, items, markers, base=None)
    xml_path = root / f"{name}.fcpxml"
    xml_path.write_text(rel, encoding="utf-8")
    (root / f"{name}_абсолютні.fcpxml").write_text(absolute, encoding="utf-8")
    conn.execute("UPDATE edits SET davinci_path = ? WHERE id = ?", (str(xml_path.relative_to(lib)), edit_id))
    progress(1.0)
    return xml_path


def export_davinci(conn: sqlite3.Connection, settings: Settings, payload: dict,
                   progress: Callable[[float], None]) -> None:
    export(conn, settings, payload["edit_id"], progress)
