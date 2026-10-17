"""Режим 2: вертикальні ролики 9:16 і етап воркера `vertical_project`."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from typing import Callable

import numpy as np

from analysis import framing as framing_mod
from core.config import Settings
from edit import audio_mix, moments
from edit.build import load_pool, render_sources

log = logging.getLogger(__name__)

DEFAULTS = {"count": 3, "length": 20.0, "audio": "live"}  # audio: live | track | none
AUDIO_MODES = ("live", "track", "none")


def vertical_params(project: sqlite3.Row, conn: sqlite3.Connection | None = None) -> dict:
    """Стандартні ← правила «назавжди» ← налаштування проєкту."""
    from edit.changes import global_vertical_defaults

    base = global_vertical_defaults(conn, dict(DEFAULTS)) if conn is not None else dict(DEFAULTS)
    return {**base, **json.loads(project["settings"] or "{}").get("vertical", {})}


def _track_audio(conn, settings: Settings, project, duration: float) -> np.ndarray | None:
    from analysis.track import TrackAnalysis
    from edit.track_window import best_window

    row = conn.execute("SELECT * FROM media WHERE project_id = ? AND kind = 'track' AND status = 'analyzed'"
                       " ORDER BY id DESC", (project["id"],)).fetchone()
    if row is None:
        return None
    ta = TrackAnalysis.load(settings.library_path / project["slug"] / "analysis" / f"{row['id']}_track.json")
    win = best_window(ta, duration)
    x = audio_mix.decode_stereo(settings.library_path / row["original_path"], win.start, duration)
    fade = min(len(x), audio_mix.SR)  # м'яке завершення за 1 с
    x[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)[:, None]
    return x


def vertical_project(conn: sqlite3.Connection, settings: Settings, payload: dict,
                     progress: Callable[[float], None]) -> None:
    from render import style_store
    from render.vertical_render import live_audio, render_reel

    project = conn.execute("SELECT * FROM projects WHERE id = ?", (payload["project_id"],)).fetchone()
    if project is None:
        return
    params = {**vertical_params(project, conn), **(payload.get("params") or {})}
    from edit.build import project_params
    clip = project_params(project, conn)
    pool = load_pool(conn, settings, project, clip.get("excluded"))
    if not pool:
        log.warning("проєкт %s: немає матеріалу для вертикалей", project["id"])
        return
    chosen = moments.choose(pool, int(params["count"]), float(params["length"]), clip.get("tag_weights"))
    style, level = style_store.resolve_for_project(conn, settings, project, clip.get("style_level"))
    look = style.look(level)
    lib = settings.library_path
    media = {m["id"]: m for m in conn.execute("SELECT * FROM media WHERE project_id = ?", (project["id"],))}
    sources = render_sources(conn, settings, [p.media_id for m in chosen for p in m.pieces])
    version = (conn.execute("SELECT MAX(version) FROM edits WHERE project_id = ? AND kind = 'vertical'",
                            (project["id"],)).fetchone()[0] or 0) + 1
    width, height = settings.render_size[1], settings.render_size[0]  # 1080×1920
    detector = framing_mod.make_detector(lib / "models")
    reels = []
    try:
        for k, mom in enumerate(chosen, 1):
            pieces = []
            for p in mom.pieces:
                m = media[p.media_id]
                src = sources[p.media_id]
                fr = framing_mod.analyze_clip(detector, lib / (m["gemini_proxy_path"] or m["proxy_path"]),
                                              p.src_in, p.duration, src.width, src.height, p.subject_x)
                pieces.append((src, p.src_in, p.duration, fr))
            if params["audio"] == "live":
                audio = live_audio([(s.path, a, d) for s, a, d, _ in pieces])
            elif params["audio"] == "track":
                audio = _track_audio(conn, settings, project, sum(d for _, _, d, _ in pieces))
            else:
                audio = None
            out = lib / project["slug"] / "exports" / f"{project['slug']}_vertical_v{version:03d}_{k}.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            dur = render_reel(pieces, look, audio, out, (width, height))
            reels.append({
                "path": str(out.relative_to(lib)), "duration": round(dur, 2), "audio": params["audio"],
                "peak": {"media_id": mom.peak.media_id, "start": mom.peak.start, "score": mom.peak.score},
                "pieces": [{"media_id": s_p.media_id, "src_in": round(s_p.src_in, 3), "duration": round(s_p.duration, 3),
                            "role": s_p.role, "framing": fr.mode, "reason": fr.reason}
                           for s_p, (_, _, _, fr) in zip(mom.pieces, pieces)],
            })
            progress(k / max(1, len(chosen)))
    finally:
        if detector is not None and hasattr(detector, "close"):
            detector.close()

    desc = lib / project["slug"] / "edits" / f"vertical_v{version:03d}" / "reels.json"
    desc.parent.mkdir(parents=True, exist_ok=True)
    desc.write_text(json.dumps({"params": params, "reels": reels}, ensure_ascii=False, indent=1), encoding="utf-8")
    summary = {"reels": reels, "style": style.name, "level": level, "created": datetime.now().isoformat(timespec="seconds")}
    conn.execute(
        "INSERT INTO edits(project_id, kind, version, params, timeline_path, preview_path, export_path, style_id,"
        " style_level, summary) VALUES (?, 'vertical', ?, ?, ?, ?, ?, ?, ?, ?)",
        (project["id"], version, json.dumps(params, ensure_ascii=False), str(desc.relative_to(lib)),
         reels[0]["path"] if reels else None, reels[0]["path"] if reels else None, style.id, level,
         json.dumps(summary, ensure_ascii=False)))
