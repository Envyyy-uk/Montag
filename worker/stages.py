"""Етапи конвеєра. Кожен етап ідемпотентний: повторний запуск після збою безпечний."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from analysis.probe import ProbeError, probe
from core.config import Settings
from render.proxy import EDIT, GEMINI, make_proxy
from worker import queue

log = logging.getLogger(__name__)

Progress = Callable[[float], None]

ANALYSIS_VERSION = 1
MIN_BURST_OVERLAP = 0.2  # с; дотик сплеску до краю шматка не рахується
NOW_SQL = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"


def _media(conn: sqlite3.Connection, media_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT m.*, p.slug, p.style_id FROM media m JOIN projects p ON p.id = m.project_id"
                        " WHERE m.id = ?", (media_id,)).fetchone()


def prepare_media(conn: sqlite3.Connection, settings: Settings, payload: dict, progress: Progress) -> None:
    """ffprobe + проксі 1080p і 480p (з HDR→SDR), далі — аналіз."""
    library = settings.library_path
    media = _media(conn, payload["media_id"])
    if media is None:
        return  # медіа видалено разом із проєктом
    src = library / media["original_path"]
    conn.execute("UPDATE media SET status = 'probing' WHERE id = ?", (media["id"],))
    try:
        info, _raw = probe(src)
    except ProbeError as exc:
        conn.execute("UPDATE media SET status = 'error', error = ? WHERE id = ?", (str(exc), media["id"]))
        return  # битий файл не повторюємо
    conn.execute("UPDATE media SET probe_json = ?, duration = ?, shot_at = ? WHERE id = ?",
                 (json.dumps(info.as_dict(), ensure_ascii=False), info.duration, info.shot_at, media["id"]))

    if media["kind"] == "video" and info.has_video:
        proxies = library / media["slug"] / "proxies"
        warnings: list[str] = []
        conn.execute("UPDATE media SET status = 'proxy' WHERE id = ?", (media["id"],))
        edit_dst = proxies / f"{media['id']}_{EDIT.name}.mp4"
        if not edit_dst.exists():
            warnings += make_proxy(src, edit_dst, info, EDIT, lambda p: progress(0.05 + 0.7 * p))
        gem_dst = proxies / f"{media['id']}_{GEMINI.name}.mp4"
        if not gem_dst.exists():
            warnings += make_proxy(src, gem_dst, info, GEMINI, lambda p: progress(0.75 + 0.25 * p))
        conn.execute("UPDATE media SET proxy_path = ?, gemini_proxy_path = ?, error = ? WHERE id = ?",
                     (str(edit_dst.relative_to(library)), str(gem_dst.relative_to(library)),
                      "; ".join(dict.fromkeys(warnings)) or None, media["id"]))
        queue.enqueue(conn, "analyze_media", {"media_id": media["id"]}, project_id=media["project_id"])
    elif media["kind"] == "track" and info.has_audio:
        queue.enqueue(conn, "analyze_track", {"media_id": media["id"]}, project_id=media["project_id"])
    conn.execute("UPDATE media SET status = 'ready' WHERE id = ?", (media["id"],))


def analyze_track(conn: sqlite3.Connection, settings: Settings, payload: dict, progress: Progress) -> None:
    """Музичний трек: темп, біти, сильні долі, енергія, дропи і приспіви."""
    from analysis import track as track_mod

    media = _media(conn, payload["media_id"])
    if media is None or media["kind"] != "track":
        return
    conn.execute("UPDATE media SET status = 'analyzing' WHERE id = ?", (media["id"],))
    path = analysis_dir(settings, media["slug"]) / f"{media['id']}_track.json"
    if not path.exists():
        ta = track_mod.analyze_file(settings.library_path / media["original_path"])
        ta.save(path)
    ta = track_mod.TrackAnalysis.load(path)
    summary = {"tempo": ta.tempo, "beats": len(ta.beats), "bars": len(ta.bar_level),
               "drops": len(ta.drops), "choruses": len(ta.choruses)}
    conn.execute(f"UPDATE media SET status = 'analyzed', analysis_json = ?, analyzed_at = {NOW_SQL} WHERE id = ?",
                 (json.dumps(summary), media["id"]))


def analysis_dir(settings: Settings, slug: str) -> Path:
    return settings.library_path / slug / "analysis"


def analyze_media(conn: sqlite3.Connection, settings: Settings, payload: dict, progress: Progress) -> None:
    """Сцени → метрики кадрів → шматки з причиною браку → звук і сплески → whisper."""
    from analysis import audio as audio_mod
    from analysis import quality, scenes, thresholds

    media = _media(conn, payload["media_id"])
    if media is None or media["kind"] != "video" or not media["gemini_proxy_path"]:
        return
    library = settings.library_path
    proxy = library / media["gemini_proxy_path"]
    info = json.loads(media["probe_json"])
    out = analysis_dir(settings, media["slug"])
    out.mkdir(parents=True, exist_ok=True)
    conn.execute("UPDATE media SET status = 'analyzing' WHERE id = ?", (media["id"],))
    summary: dict = {"version": ANALYSIS_VERSION}

    # 1. Сцени і метрики кадрів (кеш у npz — повторний запуск їх не перераховує)
    frames_path = out / f"{media['id']}_frames.npz"
    scenes_path = out / f"{media['id']}_scenes.json"
    if scenes_path.exists():
        scene_list = [tuple(s) for s in json.loads(scenes_path.read_text(encoding="utf-8"))]
    else:
        scene_list = scenes.detect_scenes(proxy, info["duration"], min(info.get("fps") or 15, 15))
        scenes_path.write_text(json.dumps(scene_list), encoding="utf-8")
    progress(0.25)
    if frames_path.exists():
        fm = quality.FrameMetrics.load(frames_path)
    else:
        fm = quality.measure_frames(proxy, info["duration"], on_progress=lambda p: progress(0.25 + 0.35 * p))
        fm.save(frames_path)
    progress(0.6)

    # 2. Звук
    audio = None
    if info.get("has_audio"):
        audio = audio_mod.analyze_file(proxy)
        (out / f"{media['id']}_audio.json").write_text(
            json.dumps({"hz": audio_mod.ENVELOPE_HZ, "mean_db": audio.mean_db, "envelope_db": audio.envelope_db},
                       ensure_ascii=False), encoding="utf-8")
        summary["audio_mean_db"] = audio.mean_db
    progress(0.7)

    # 3. Шматки з метриками і причиною браку (пороги стилю проєкту)
    th = thresholds.for_style(media["style_id"], settings.default_filter_preset)
    rows = []
    for i, piece in enumerate(scenes.pieces(scene_list)):
        m = quality.segment_metrics(fm, piece.start, piece.end)
        burst = 0.0
        if audio:
            overlapping = [e.peak_excess_db for e in audio.events
                           if min(e.end, piece.end) - max(e.start, piece.start) >= MIN_BURST_OVERLAP]
            burst = max(overlapping, default=0.0)
        rows.append((media["id"], i, piece.scene_idx, piece.start, piece.end, json.dumps(m),
                     quality.local_score(m, th), quality.reject_reason(m, piece.duration, th),
                     audio.db_between(piece.start, piece.end) if audio else None, burst))
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM segments WHERE media_id = ?", (media["id"],))
        conn.executemany(
            "INSERT INTO segments(media_id, idx, scene_idx, start, end, metrics, local_score, reject_reason,"
            " audio_db, burst_db) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.execute("DELETE FROM audio_events WHERE media_id = ?", (media["id"],))
        if audio:
            conn.executemany(
                "INSERT INTO audio_events(media_id, kind, start, end, peak_excess_db, peak_db, flatness)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(media["id"], e.kind, e.start, e.end, e.peak_excess_db, e.peak_db, e.flatness)
                 for e in audio.events])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    summary.update(scenes=len(scene_list), segments=len(rows),
                   rejected=sum(1 for r in rows if r[7]), filter=th.as_dict())
    progress(0.75)

    # 4. Мова
    transcript_path = out / f"{media['id']}_transcript.json"
    if settings.whisper_enabled and info.get("has_audio"):
        try:
            if not transcript_path.exists():
                from analysis import transcribe
                pcm = audio_mod.decode_audio(proxy, sr=16000)
                tr = transcribe.transcribe(pcm, settings.whisper_model, settings.whisper_language)
                transcribe.save(tr, transcript_path, out / f"{media['id']}.srt")
            tr = json.loads(transcript_path.read_text(encoding="utf-8"))
            summary.update(language=tr.get("language"), speech_segments=len(tr.get("segments", [])))
        except Exception as exc:  # noqa: BLE001 — без субтитрів монтаж усе одно можливий
            log.warning("whisper для медіа %s не вдався: %s", media["id"], exc)
            summary["whisper_error"] = str(exc)[:300]
    progress(1.0)

    conn.execute(f"UPDATE media SET status = 'analyzed', analysis_json = ?, analyzed_at = {NOW_SQL}"
                 " WHERE id = ?", (json.dumps(summary, ensure_ascii=False), media["id"]))


def request_edit(conn: sqlite3.Connection, project_id: int) -> int:
    """«Монтувати»: проєкт іде в чергу — спершу AI-оцінка, потім монтаж."""
    conn.execute(f"UPDATE projects SET status = 'queued', edit_requested_at = {NOW_SQL},"
                 f" updated_at = {NOW_SQL} WHERE id = ?", (project_id,))
    return queue.enqueue(conn, "score_project", {"project_id": project_id}, project_id=project_id)


def _parse_ts(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def auto_queue_idle(conn: sqlite3.Connection, idle_minutes: int, now: datetime | None = None) -> list[int]:
    """Проєкти, куди файли не надходять `idle_minutes`, самі стають у чергу на монтаж
    (коли підготовка й аналіз усіх файлів закінчені)."""
    now = now or datetime.now(timezone.utc)
    queued = []
    rows = conn.execute("SELECT id, last_media_at FROM projects WHERE status = 'collecting'"
                        " AND last_media_at IS NOT NULL").fetchall()
    for row in rows:
        if now - _parse_ts(row["last_media_at"]) < timedelta(minutes=idle_minutes):
            continue
        has_video = conn.execute("SELECT 1 FROM media WHERE project_id = ? AND kind = 'video'",
                                 (row["id"],)).fetchone()
        busy = conn.execute("SELECT 1 FROM jobs WHERE project_id = ? AND kind IN ('prepare_media', 'analyze_media', 'analyze_track')"
                            " AND status IN ('queued', 'running')", (row["id"],)).fetchone()
        if has_video and not busy:
            request_edit(conn, row["id"])
            queued.append(row["id"])
    return queued
