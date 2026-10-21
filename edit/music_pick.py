"""Підбір музики під відео: 3 найкращі треки з твоєї бібліотеки + ідеї нових пісень.

1. Профіль відео: тип події (з AI-оцінки), що в кадрі (теги), наскільки воно енергійне, потрібна довжина.
2. Локальний рейтинг бібліотеки: темп під подію, енергія, чи вистачає довжини, є дроп/приспів.
3. Якщо є ключ Gemini — один дешевий текстовий запит (~$0.005): AI вибирає 3 з топ-15 з поясненням
   і радить 5 нових пісень (посилання на пошук у SoundCloud).
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from urllib.parse import quote_plus

from core.config import Settings

# темп, який пасує до типу події (BPM); половинний/подвійний темп теж рахується
TEMPO = {"концерт": (110, 150), "фестиваль": (115, 150), "вечірка": (115, 135), "спорт": (120, 160),
         "подорож": (85, 125), "місто": (90, 125), "природа": (65, 105), "інше": (90, 130)}
ENERGETIC_TAGS = {"натовп", "сцена", "артист", "світло", "рух", "друзі", "селфі", "руки", "ніч"}
CALM_TAGS = {"пейзаж", "вода", "небо", "захід сонця", "статика", "дорога", "їжа", "тварини"}

PICK_SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {"type": "array", "maxItems": 3, "items": {
            "type": "object", "properties": {"id": {"type": "integer"}, "reason": {"type": "string", "maxLength": 200}},
            "required": ["id", "reason"]}},
        "ideas": {"type": "array", "maxItems": 6, "items": {
            "type": "object", "properties": {"artist": {"type": "string", "maxLength": 80},
                                             "title": {"type": "string", "maxLength": 120},
                                             "why": {"type": "string", "maxLength": 160}},
            "required": ["artist", "title", "why"]}},
    },
    "required": ["picks", "ideas"],
}


def profile(conn: sqlite3.Connection, project_id: int, length: float) -> dict:
    events = Counter()
    for r in conn.execute("SELECT event_type, event_confidence FROM gemini_requests WHERE project_id = ?"
                          " AND status = 'done' AND event_type IS NOT NULL", (project_id,)):
        events[r["event_type"]] += r["event_confidence"] or 0.5
    rows = conn.execute("SELECT s.tags, s.score, s.description FROM scores s JOIN media m ON m.id = s.media_id"
                        " WHERE m.project_id = ? ORDER BY s.score DESC", (project_id,)).fetchall()
    tags = Counter(t for r in rows for t in json.loads(r["tags"] or "[]"))
    total = sum(tags.values()) or 1
    energy = 0.5 + 0.5 * (sum(tags[t] for t in ENERGETIC_TAGS) - sum(tags[t] for t in CALM_TAGS)) / total
    event = events.most_common(1)[0][0] if events else None
    if event in ("концерт", "фестиваль", "вечірка", "спорт"):
        energy = max(energy, 0.65)
    return {"event": event, "events": dict(events.most_common(3)), "tags": dict(tags.most_common(8)),
            "energy": round(min(1.0, max(0.0, energy)), 2), "length": length,
            "moments": [r["description"] for r in rows[:8] if r["description"]]}


def _tempo_fit(bpm: float, lo: float, hi: float) -> float:
    best = 0.0
    for b in (bpm, bpm * 2, bpm / 2):
        if lo <= b <= hi:
            return 1.0
        gap = min(abs(b - lo), abs(b - hi))
        best = max(best, max(0.0, 1 - gap / 25))
    return best


def local_rank(tracks: list[sqlite3.Row], prof: dict) -> list[tuple[float, sqlite3.Row, str]]:
    lo, hi = TEMPO.get(prof["event"] or "інше", TEMPO["інше"])
    out = []
    for t in tracks:
        tf = _tempo_fit(t["tempo"] or 0, lo, hi)
        ef = 1 - abs((t["energy_peak"] or 0.5) - prof["energy"])
        lf = 1.0 if (t["duration"] or 0) >= prof["length"] else max(0.0, (t["duration"] or 0) / prof["length"])
        bonus = 0.1 if prof["energy"] >= 0.6 and (t["drops"] or t["choruses"]) else 0.0
        score = 0.45 * tf + 0.35 * ef + 0.2 * lf + bonus
        why = []
        if tf >= 0.99:
            why.append(f"темп пасує до події «{prof['event']}»" if prof["event"] else "темп пасує до відео")
        why.append("енергійний" if (t["energy_peak"] or 0) >= 0.7 else "спокійний" if (t["energy_peak"] or 0) < 0.45 else "рівний")
        if t["drops"]:
            why.append("є дроп")
        elif t["choruses"]:
            why.append("є приспів")
        if lf < 1:
            why.append("коротший за кліп")
        out.append((round(score, 3), t, ", ".join(why).capitalize()))
    return sorted(out, key=lambda x: -x[0])


def _best_fragment(settings: Settings, track_id: int, length: float) -> dict | None:
    from analysis.track import TrackAnalysis
    from edit.track_window import best_window
    from sources.music import analysis_path
    path = analysis_path(settings, track_id)
    if not path.exists():
        return None
    w = best_window(TrackAnalysis.load(path), length)
    return {"start": round(w.start, 2), "end": round(w.end, 2)}


def _prompt(prof: dict, cands: list[tuple[float, sqlite3.Row, str]], taste: str) -> str:
    lines = [f"{t['id']}: {t['artist'] or '?'} — {t['title']} · {round(t['tempo'] or 0)} BPM · енергія "
             f"{(t['energy_peak'] or 0):.2f} · {round(t['duration'] or 0)} с" for _, t, _ in cands]
    return (
        "Ти музичний редактор коротких кліпів. Підбери музику під відео.\n"
        f"Відео: подія — {prof['event'] or 'невідомо'} ({prof['events']}); що в кадрі — {prof['tags']}; "
        f"енергія відео {prof['energy']} (0 — спокійне, 1 — дуже енергійне); кліп ≈ {round(prof['length'])} с.\n"
        f"Найкращі моменти: {'; '.join(prof['moments'][:6]) or 'немає описів'}.\n"
        f"Смак автора (уривок): {taste[:600]}\n\n"
        "Треки з бібліотеки автора (id: виконавець — назва · темп · енергія 0–1 · довжина):\n" + "\n".join(lines) +
        "\n\npicks — до 3 найкращих id саме з цього списку, reason — коротко українською, чому пасує до цього відео.\n"
        "ideas — 5 реальних відомих пісень, яких немає в списку, що ідеально лягли б на це відео "
        "(artist, title — як у SoundCloud/Spotify; why — коротко українською).")


def suggest(conn: sqlite3.Connection, settings: Settings, project_id: int, use_ai: bool = True) -> dict:
    from edit.build import project_params
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    length = float(project_params(project, conn).get("length") or 60)
    prof = profile(conn, project_id, length)
    tracks = conn.execute("SELECT * FROM music_tracks WHERE status = 'analyzed'").fetchall()
    ranked = local_rank(tracks, prof)
    picks = [{"id": t["id"], "reason": why, "score": s} for s, t, why in ranked[:3]]
    ideas, source, ai_error = [], "local", None
    if use_ai:
        from analysis.llm import LLMUnavailable, ask_json
        taste = settings.taste_path.read_text(encoding="utf-8") if settings.taste_path.exists() else ""
        try:
            data = ask_json(conn, settings, _prompt(prof, ranked[:15], taste), PICK_SCHEMA, "підбір музики", project_id)
            known = {t["id"] for _, t, _ in ranked[:15]}
            ai_picks = [p for p in data["picks"] if p["id"] in known]
            if ai_picks:
                picks, source = ai_picks, "gemini"
            ideas = data["ideas"]
        except LLMUnavailable as exc:
            ai_error = str(exc)
    by_id = {t["id"]: t for t in tracks}
    out_picks = []
    for p in picks:
        t = by_id[p["id"]]
        out_picks.append({"track_id": t["id"], "title": t["title"], "artist": t["artist"], "tempo": round(t["tempo"] or 0),
                          "duration": round(t["duration"] or 0), "reason": p["reason"],
                          "fragment": _best_fragment(settings, t["id"], length)})
    out_ideas = [{**i, "soundcloud": "https://soundcloud.com/search/sounds?q=" + quote_plus(f"{i['artist']} {i['title']}")}
                 for i in ideas]
    result = {"profile": {k: prof[k] for k in ("event", "energy", "tags", "length")}, "picks": out_picks,
              "ideas": out_ideas, "source": source, "library": len(tracks), "ai_error": ai_error}
    stored = json.loads(project["settings"] or "{}")
    stored["music_suggest"] = result
    conn.execute("UPDATE projects SET settings = ? WHERE id = ?", (json.dumps(stored, ensure_ascii=False), project_id))
    return result


def use_track(conn: sqlite3.Connection, settings: Settings, project_id: int, track_id: int,
              start: float | None = None, end: float | None = None) -> dict:
    """Додає трек бібліотеки в проєкт (копією — файл у music/ лишається як є) і бере найкращий шматок."""
    import shutil

    from core.projects import get_project
    from sources.ingest import ingest_file
    from sources.music import analysis_path, file_of
    from worker.stages import analysis_dir

    t = conn.execute("SELECT * FROM music_tracks WHERE id = ? AND status = 'analyzed'", (track_id,)).fetchone()
    if t is None:
        raise ValueError("Трек не знайдено або ще аналізується")
    project = get_project(conn, project_id)
    src = file_of(settings, t)
    res = ingest_file(conn, settings.library_path, project, src, src.name, "upload", move=False)  # копія з music/
    media = res.media
    # аналіз уже є — копіюємо, щоб не рахувати вдруге
    cached = analysis_path(settings, track_id)
    dst = analysis_dir(settings, project["slug"]) / f"{media['id']}_track.json"
    if cached.exists() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(cached, dst)
    # цей трек — головний: свіжий id, межі шматка скинуто (візьметься найкращий)
    stored = json.loads(project["settings"] or "{}")
    clip = stored.get("clip", {})
    clip.pop("track_start", None)
    clip.pop("track_end", None)
    if start is not None and end is not None and end - start >= 5:
        clip["track_start"], clip["track_end"] = round(start, 2), round(end, 2)  # шматок, вибраний у підборі
    stored["clip"] = clip
    stored["track_media_id"] = media["id"]
    conn.execute("UPDATE projects SET settings = ? WHERE id = ?", (json.dumps(stored, ensure_ascii=False), project_id))
    return {"media_id": media["id"], "title": t["title"], "artist": t["artist"]}
