"""Збірка кліпу під музику (Режим 1) і етап воркера `edit_project`.

Трек → найенергійніше вікно → сітка склейок → живі вставки → вибір кадрів →
таймлайн (кадри) → зведення звуку з ducking → чорнове превʼю.
Перемонтаж бере вже кешовані оцінки — нових запитів у Gemini немає.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

import numpy as np

from analysis import quality, scoring, thresholds
from analysis.track import TrackAnalysis
from core.config import Settings
from edit import audio_mix, inserts as inserts_mod, rhythm
from edit.select import Shot, Weights, choose
from edit.timeline import Clip, Insert, Marker, Timeline
from edit.track_window import best_window, window_from_bounds

log = logging.getLogger(__name__)

STANDARD_FPS = [23.976, 24.0, 25.0, 29.97, 30.0, 50.0, 59.94, 60.0]

DEFAULT_PARAMS = {
    "length": 60.0,          # с
    "track_start": None,     # межі шматка треку, заданi користувачем
    "track_end": None,
    "inserts": True,
    "max_inserts": None,
    "duck_db": -12.0,
    "live_gain_db": 0.0,
    "beats_per_level": {},   # {"low": 4, "mid": 2, "high": 1}
    "tag_weights": {},       # {"натовп": 0.3}
    "excluded": [],          # [[media_id, start], ...]
    "transitions": True,     # ефектні переходи на акцентах (перемикач у превʼю)
    "disabled_transitions": [],
    "style_level": None,     # рівень інтенсивності стилю саме для цього проєкту
    "aspect": "16:9",        # формат кліпу: 16:9 / 9:16 / 1:1 / 4:5
    "fit_mode": "auto",      # кадри іншої форми: auto / crop (обрізати) / blur (розмитий фон)
}


class EditError(RuntimeError):
    pass


def project_params(project: sqlite3.Row, conn: sqlite3.Connection | None = None) -> dict:
    """Параметри кліпу: стандартні ← правила «назавжди» ← налаштування проєкту («тільки тут»)."""
    from edit.changes import global_clip_defaults

    base = global_clip_defaults(conn, dict(DEFAULT_PARAMS)) if conn is not None else dict(DEFAULT_PARAMS)
    stored = json.loads(project["settings"] or "{}").get("clip", {})
    return {**base, **stored}


def standard_fps(fps: float) -> float:
    return min(STANDARD_FPS, key=lambda f: abs(f - fps)) if fps > 0 else 30.0


def majority_fps(conn: sqlite3.Connection, project_id: int) -> float:
    weight: Counter = Counter()
    for r in conn.execute("SELECT probe_json, duration FROM media WHERE project_id = ? AND kind = 'video'"
                          " AND probe_json IS NOT NULL", (project_id,)):
        info = json.loads(r["probe_json"])
        weight[standard_fps(info.get("fps") or 0)] += r["duration"] or 0
    return weight.most_common(1)[0][0] if weight else 30.0


def _camera(info: dict) -> str:
    return info.get("device") or f"{info.get('width')}x{info.get('height')}@{round(info.get('fps') or 0)}/{info.get('codec')}"


def _chrono_order(media_rows) -> dict[int, tuple]:
    """Ключ хронології: час зйомки, далі порядок додавання."""
    return {m["id"]: (m["shot_at"] or "9999", m["id"]) for m in media_rows}


CROWD_BURST_DB = 6.0
LIVELY_MOTION = 15.0


def derived_tags(tags: list[str], burst_db: float, motion: float) -> list[str]:
    """Теги Gemini + виведені локально: «натовп» зі сплеску звуку, «рух» з оптичного потоку.
    Так правка «більше натовпу» працює і без оцінок Gemini."""
    out = list(tags)
    if burst_db >= CROWD_BURST_DB and "натовп" not in out:
        out.append("натовп")
    if motion >= LIVELY_MOTION and "рух" not in out:
        out.append("рух")
    return out


def load_pool(conn: sqlite3.Connection, settings: Settings, project: sqlite3.Row,
              excluded: list | None = None) -> list[Shot]:
    th = thresholds.for_style(project["style_id"], settings.default_filter_preset)
    style = scoring.style_key(project["style_id"], scoring.load_taste(settings), settings.gemini_model)
    media = {m["id"]: m for m in conn.execute(
        "SELECT * FROM media WHERE project_id = ? AND kind = 'video' AND status = 'analyzed'", (project["id"],))}
    order = _chrono_order(media.values())
    excl = {(int(a), round(float(b), 3)) for a, b in (excluded or [])}
    rows = conn.execute(
        "SELECT s.* FROM segments s JOIN media m ON m.id = s.media_id WHERE m.project_id = ?"
        " AND m.status = 'analyzed'", (project["id"],)).fetchall()
    shots = []
    for r in rows:
        m = json.loads(r["metrics"])
        if quality.reject_reason(m, r["end"] - r["start"], th) is not None:
            continue
        if (r["media_id"], round(r["start"], 3)) in excl:
            continue
        sc = conn.execute(
            "SELECT * FROM scores WHERE media_id = ? AND start = ? AND end = ?"
            " ORDER BY (style_key = ?) DESC, (source = 'gemini') DESC, id DESC LIMIT 1",
            (r["media_id"], r["start"], r["end"], style)).fetchone()
        info = json.loads(media[r["media_id"]]["probe_json"])
        shots.append(Shot(
            media_id=r["media_id"], start=r["start"], end=r["end"],
            score=sc["score"] if sc else quality.local_score(m, th) * 10,
            source=sc["source"] if sc else "local",
            tags=derived_tags(json.loads(sc["tags"]) if sc and sc["tags"] else [], r["burst_db"] or 0.0,
                              m.get("motion", 0.0)),
            scene=(r["media_id"], r["scene_idx"]), camera=_camera(info), motion=m.get("motion", 0.0),
            burst=r["burst_db"] or 0.0,
            vertical_ok=bool(sc["vertical_ok"]) if sc and sc["vertical_ok"] is not None else None,
            subject_x=sc["subject_x"] if sc else None))
    # хронологія: ранг кожного шматка в усій події
    shots.sort(key=lambda s: (order[s.media_id], s.start))
    for i, s in enumerate(shots):
        s.chrono = i / max(1, len(shots) - 1)
    return shots


def load_events(conn: sqlite3.Connection, project_id: int, pool: list[Shot]) -> list[inserts_mod.CrowdEvent]:
    """Сплески натовпу, під якими є придатна картинка."""
    media = {m["id"]: m for m in conn.execute("SELECT * FROM media WHERE project_id = ? AND kind = 'video'",
                                              (project_id,))}
    order = sorted(media, key=lambda i: (media[i]["shot_at"] or "9999", i))
    events = []
    for e in conn.execute("SELECT ae.* FROM audio_events ae JOIN media m ON m.id = ae.media_id"
                          " WHERE m.project_id = ? AND ae.kind = 'crowd'", (project_id,)):
        if not any(s.media_id == e["media_id"] and s.start <= e["start"] < s.end for s in pool):
            continue  # під сплеском — брак
        events.append(inserts_mod.CrowdEvent(
            media_id=e["media_id"], start=e["start"], end=e["end"], peak_db=e["peak_excess_db"],
            chrono=order.index(e["media_id"]) / max(1, len(order) - 1),
            media_duration=media[e["media_id"]]["duration"] or 0))
    return events


def _subtract(slots: list[rhythm.Slot], placed: list[inserts_mod.PlacedInsert],
              track: TrackAnalysis, win_start: float) -> list[rhythm.Slot]:
    """Вирізає з сітки місця вставок (вони теж на бітах)."""
    times = track.beats + [track.duration]
    out = []
    for s in slots:
        pieces = [(s.first_beat, s.last_beat)]
        for p in placed:
            nxt = []
            for a, b in pieces:
                if p.last_beat <= a or p.first_beat >= b:
                    nxt.append((a, b))
                    continue
                if a < p.first_beat:
                    nxt.append((a, p.first_beat))
                if p.last_beat < b:
                    nxt.append((p.last_beat, b))
            pieces = nxt
        for a, b in pieces:
            out.append(rhythm.Slot(a, b, times[a] - win_start, times[b] - win_start, s.level, s.accent))
    return out


@dataclass
class ClipBuild:
    timeline: Timeline
    window_start: float
    window_end: float


def build_timeline(conn: sqlite3.Connection, settings: Settings, project: sqlite3.Row,
                   track_row: sqlite3.Row, track: TrackAnalysis, params: dict) -> ClipBuild:
    if params.get("track_start") is not None and params.get("track_end") is not None:
        win = window_from_bounds(track, float(params["track_start"]), float(params["track_end"]))
    else:
        win = best_window(track, float(params["length"]))
    base = rhythm.slots(track, win, params.get("beats_per_level"))
    pool = load_pool(conn, settings, project, params.get("excluded"))
    if not pool:
        raise EditError("Немає жодного придатного шматка відео (усе відсіяв фільтр браку)")
    placed = []
    if params.get("inserts", True):
        placed = inserts_mod.plan(track, win, load_events(conn, project["id"], pool), params.get("max_inserts"))
    slots = _subtract(base, placed, track, win.start)
    weights = Weights(tag_weights=params.get("tag_weights") or {})
    choices = choose(slots, pool, win.duration, weights)

    fps = majority_fps(conn, project["id"])
    q = lambda t: round(round(t * fps) / fps, 6)  # noqa: E731 — квантування до кадру
    items: list[Clip] = []
    for ch in choices:
        items.append(Clip(start=ch.slot.start, end=ch.slot.end, media_id=ch.shot.media_id, src_in=ch.src_in,
                          kind="beat", score=round(ch.shot.score, 2), source=ch.shot.source, tags=ch.shot.tags,
                          level=ch.slot.level, accent=ch.slot.accent))
    for p in placed:
        items.append(Clip(start=p.start, end=p.end, media_id=p.event.media_id, src_in=p.src_in, kind="insert",
                          level=track.level_at_beat(p.first_beat), accent=True))
    items.sort(key=lambda c: c.start)
    duration = q(win.duration)
    clips: list[Clip] = []
    for c in items:
        c.start, c.end = q(c.start), min(q(c.end), duration)
        if c.end - c.start < 0.5 / fps:  # нуль кадрів після квантування — віддаємо сусіду
            if clips:
                clips[-1].end = c.end
            continue
        clips.append(c)
    if clips:
        clips[0].start = 0.0
        clips[-1].end = duration

    markers = []
    for k in track.drops:
        t = track.bar_start_time(k) - win.start
        if 0 <= t < win.duration:
            markers.append(Marker(q(t), "drop", "Дроп"))
    for a, _b in track.choruses:
        t = track.bar_start_time(a) - win.start
        if 0 <= t < win.duration:
            markers.append(Marker(q(t), "chorus", "Приспів"))
    tl_inserts = []
    for p in placed:
        tl_inserts.append(Insert(q(p.start), q(p.end), p.event.media_id, p.src_in,
                                 duck_db=float(params["duck_db"]), live_gain_db=float(params["live_gain_db"])))
        markers.append(Marker(q(p.start), "insert", "Жива вставка"))
    markers.sort(key=lambda m: m.t)

    beat = float(np.median(np.diff(track.beats))) if len(track.beats) > 1 else 0.5
    tl = Timeline(fps=fps, duration=duration, track_media_id=track_row["id"], track_in=round(win.start, 4),
                  track_out=round(win.start + duration, 4), clips=clips, inserts=tl_inserts, markers=markers,
                  params={k: params[k] for k in DEFAULT_PARAMS}, beat=round(beat, 5))
    problems = tl.check()
    if problems:
        raise EditError("; ".join(problems))
    return ClipBuild(tl, win.start, win.end)


def render_audio(settings: Settings, conn: sqlite3.Connection, tl: Timeline, out: Path) -> None:
    track_row = conn.execute("SELECT * FROM media WHERE id = ?", (tl.track_media_id,)).fetchone()
    lib = settings.library_path
    track = audio_mix.decode_stereo(lib / track_row["original_path"], tl.track_in, tl.duration)
    lives = []
    for ins in tl.inserts:
        m = conn.execute("SELECT * FROM media WHERE id = ?", (ins.media_id,)).fetchone()
        src = lib / (m["proxy_path"] or m["original_path"])
        lives.append((ins.start, audio_mix.decode_stereo(src, ins.src_in, ins.end - ins.start)))
    duck = tl.inserts[0].duck_db if tl.inserts else -12.0
    gain = tl.inserts[0].live_gain_db if tl.inserts else 0.0
    report: dict = {}
    audio_mix.write_wav(out, audio_mix.mix(track, lives, duck, gain, report=report))
    for ins, g in zip(tl.inserts, report["inserts_db"]):
        ins.applied_gain_db = g
    tl.params["track_gain_db"] = report["master_db"]


def clip_size(settings: Settings, params: dict) -> tuple[int, int]:
    from render.frame import CLIP_ASPECTS, size_for
    aspect = params.get("aspect") if params.get("aspect") in CLIP_ASPECTS else "16:9"
    return size_for(aspect, settings.render_height)


def clip_framings(conn, settings: Settings, tl, sources: dict, size: tuple[int, int], fit_mode: str) -> dict:
    """Для кадрів, ширших за формат кліпу (напр. горизонтальні у 9:16), — вікно за обличчями."""
    from analysis import framing as framing_mod
    from render.frame import needs_x_crop

    if fit_mode == "blur":
        return {}
    todo = [(i, c) for i, c in enumerate(tl.clips) if needs_x_crop(sources[c.media_id].width,
                                                                    sources[c.media_id].height, *size)]
    if not todo:
        return {}
    detector = framing_mod.make_detector(settings.library_path / "models")
    out = {}
    try:
        for i, c in todo:
            m = conn.execute("SELECT gemini_proxy_path, proxy_path FROM media WHERE id = ?", (c.media_id,)).fetchone()
            src = sources[c.media_id]
            out[i] = framing_mod.analyze_clip(detector, settings.library_path / (m["gemini_proxy_path"] or m["proxy_path"]),
                                              c.src_in, c.end - c.start, src.width, src.height,
                                              target_ar=size[0] / size[1])
    finally:
        if detector is not None and hasattr(detector, "close"):
            detector.close()
    return out


def edit_dir(settings: Settings, slug: str, version: int) -> Path:
    return settings.library_path / slug / "edits" / f"clip_v{version:03d}"


def render_sources(conn: sqlite3.Connection, settings: Settings, media_ids) -> dict:
    """Джерела рендеру: 1080p-проксі (вже SDR) з розмірами і часом зйомки."""
    from render.clip_render import Source

    out = {}
    for mid in set(media_ids):
        m = conn.execute("SELECT * FROM media WHERE id = ?", (mid,)).fetchone()
        info = json.loads(m["probe_json"] or "{}")
        path = settings.library_path / (m["proxy_path"] or m["original_path"])
        w, h = info.get("display_width") or info.get("width") or 1920, info.get("display_height") or info.get("height") or 1080
        out[mid] = Source(path, int(w), int(h), m["shot_at"])
    return out


def edit_project(conn: sqlite3.Connection, settings: Settings, payload: dict,
                 progress: Callable[[float], None]) -> None:
    from edit import transitions_plan
    from render import style_store
    from render.clip_render import render_timeline

    project = conn.execute("SELECT * FROM projects WHERE id = ?", (payload["project_id"],)).fetchone()
    if project is None:
        return
    tracks = conn.execute("SELECT * FROM media WHERE project_id = ? AND kind = 'track' AND status = 'analyzed'"
                          " ORDER BY id DESC", (project["id"],)).fetchall()
    if not tracks:
        conn.execute("UPDATE projects SET status = 'needs_track' WHERE id = ?", (project["id"],))
        log.warning("проєкт %s: немає треку для кліпу", project["id"])
        return
    track_row = tracks[0]
    analysis = settings.library_path / project["slug"] / "analysis" / f"{track_row['id']}_track.json"
    track = TrackAnalysis.load(analysis)
    params = {**project_params(project, conn), **(payload.get("params") or {})}
    conn.execute("UPDATE projects SET status = 'rendering' WHERE id = ?", (project["id"],))
    build = build_timeline(conn, settings, project, track_row, track, params)
    tl = build.timeline
    style, level = style_store.resolve_for_project(conn, settings, project, params.get("style_level"))
    version = (conn.execute("SELECT MAX(version) FROM edits WHERE project_id = ? AND kind = 'clip'",
                            (project["id"],)).fetchone()[0] or 0) + 1
    st = style.transitions
    tl.transitions = transitions_plan.plan(tl, st.get("allowed", []), float(st.get("max_share", 0.15)),
                                           set(params.get("disabled_transitions") or []),
                                           seed=project["id"] * 1000 + version)
    tl.params["style"] = {"id": style.id, "level": level}
    progress(0.05)

    lib = settings.library_path
    out = edit_dir(settings, project["slug"], version)
    tl_path, wav = out / "timeline.json", out / "mix.wav"
    render_audio(settings, conn, tl, wav)
    tl.save(tl_path)
    progress(0.1)
    exports = lib / project["slug"] / "exports"
    final = exports / f"{project['slug']}_clip_v{version:03d}.mp4"
    preview = exports / f"{project['slug']}_clip_v{version:03d}_720p.mp4"
    sources = render_sources(conn, settings, [c.media_id for c in tl.clips])
    size, fit_mode = clip_size(settings, tl.params), tl.params.get("fit_mode", "auto")
    framings = clip_framings(conn, settings, tl, sources, size, fit_mode)
    render_timeline(tl, style, level, sources, wav, final, preview, transitions=bool(params.get("transitions", True)),
                    size=size, on_progress=lambda p: progress(0.1 + 0.9 * p), framings=framings, fit_mode=fit_mode)

    summary = {
        "duration": tl.duration, "fps": tl.fps, "cuts": len(tl.clips) - 1,
        "inserts": len(tl.inserts), "track_in": tl.track_in, "track_out": tl.track_out,
        "transitions": [t["kind"] for t in tl.transitions] if params.get("transitions", True) else [],
        "style": style.name, "level": level,
        "gemini_share": round(sum(c.source == "gemini" for c in tl.clips) / max(1, len(tl.clips)), 2),
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    conn.execute(
        "INSERT INTO edits(project_id, kind, version, params, timeline_path, audio_path, preview_path, export_path,"
        " style_id, style_level, summary) VALUES (?, 'clip', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (project["id"], version, json.dumps(tl.params, ensure_ascii=False), str(tl_path.relative_to(lib)),
         str(wav.relative_to(lib)), str(preview.relative_to(lib)), str(final.relative_to(lib)), style.id, level,
         json.dumps(summary, ensure_ascii=False)))
    edit_id = conn.execute("SELECT id FROM edits WHERE project_id = ? AND kind = 'clip' AND version = ?",
                           (project["id"], version)).fetchone()[0]
    conn.execute("UPDATE projects SET status = 'edited', updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
                 " WHERE id = ?", (project["id"],))
    from core import notify
    notify.send(conn, settings, f"Готово: {project['name']}",
                f"Кліп {tl.duration:.0f} с, {len(tl.clips) - 1} склейок — можна дивитися", f"/project/{project['id']}")
    # проєкт DaVinci — окремим завданням, щоб mp4 був готовий швидше
    from worker import queue
    queue.enqueue(conn, "export_davinci", {"edit_id": edit_id}, project_id=project["id"])
    queue.enqueue(conn, "drive_upload", {"edit_id": edit_id}, project_id=project["id"])
