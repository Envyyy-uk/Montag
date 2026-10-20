"""Бібліотека музики для підбору треку: папка music/ на ноутбуці + «Музика» в Google Drive.

Нові файли знаходить воркер (раз на хвилину), кожен трек аналізується один раз: темп, енергія,
дропи, приспіви, назва й виконавець з тегів. Файли в music/ програма ніколи не змінює й не видаляє.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import subprocess
from pathlib import Path

from core.config import Settings
from sources.ingest import AUDIO_EXT
from worker import queue

log = logging.getLogger(__name__)
MUSIC_EXT = AUDIO_EXT | {".ogg", ".opus"}


def analysis_path(settings: Settings, track_id: int) -> Path:
    return settings.library_path / "music_analysis" / f"{track_id}_track.json"


def _rel(settings: Settings, path: Path) -> str:
    try:
        return path.resolve().relative_to(settings.music_path.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def file_of(settings: Settings, row: sqlite3.Row) -> Path:
    p = Path(row["path"])
    return p if p.is_absolute() else settings.music_path / p


def scan(conn: sqlite3.Connection, settings: Settings) -> list[int]:
    """Знаходить нові або змінені треки і ставить їх на аналіз. Повертає id нових завдань."""
    root = settings.music_path
    if not root.exists():
        return []
    seen, new = set(), []
    for f in root.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in MUSIC_EXT or f.name.startswith("."):
            continue
        rel = _rel(settings, f)
        seen.add(rel)
        st = f.stat()
        row = conn.execute("SELECT * FROM music_tracks WHERE path = ?", (rel,)).fetchone()
        if row is not None and row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) < 1 \
                and row["status"] != "missing":
            continue
        if row is None:
            tid = conn.execute("INSERT INTO music_tracks(path, size, mtime, source) VALUES (?, ?, ?, ?)",
                               (rel, st.st_size, st.st_mtime, "drive" if rel.startswith("drive/") else "folder")).lastrowid
        else:
            tid = row["id"]
            conn.execute("UPDATE music_tracks SET size = ?, mtime = ?, status = 'new', error = NULL WHERE id = ?",
                         (st.st_size, st.st_mtime, tid))
            analysis_path(settings, tid).unlink(missing_ok=True)
        new.append(queue.enqueue(conn, "analyze_music", {"track_id": tid}))
    # файл прибрали з папки — у підбір більше не потрапляє
    for row in conn.execute("SELECT id, path FROM music_tracks WHERE status != 'missing'").fetchall():
        if row["path"] not in seen and not Path(row["path"]).is_absolute():
            conn.execute("UPDATE music_tracks SET status = 'missing' WHERE id = ?", (row["id"],))
    return new


def _tags(path: Path) -> dict:
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=title,artist",
                              "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
        tags = json.loads(out.stdout or "{}").get("format", {}).get("tags", {})
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        tags = {}
    tags = {k.lower(): v for k, v in tags.items()}
    title, artist = tags.get("title"), tags.get("artist")
    if not title:  # «Виконавець - Назва.mp3»
        stem = path.stem.replace("_", " ")
        if " - " in stem:
            artist_guess, title = stem.split(" - ", 1)
            artist = artist or artist_guess.strip()
        else:
            title = stem
    return {"title": title.strip()[:200], "artist": (artist or "").strip()[:200] or None}


def absolute_energy(path: Path) -> tuple[float, float]:
    """Енергія, порівнювана між треками (0–1): гучність + щільність ударів. Середнє і пік (90-й перцентиль)."""
    import librosa
    import numpy as np

    y, sr = librosa.load(str(path), sr=22050, mono=True)
    hop = 512
    rms_db = librosa.amplitude_to_db(librosa.feature.rms(y=y, hop_length=hop)[0] + 1e-9, ref=1.0)
    onset = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    per = int(sr / hop)  # ≈ 1 с
    n = max(1, min(len(rms_db), len(onset)) // per)
    loud = np.array([rms_db[i * per:(i + 1) * per].mean() for i in range(n)])
    hits = np.array([onset[i * per:(i + 1) * per].mean() for i in range(n)])
    score = 0.6 * np.clip((loud + 35) / 27, 0, 1) + 0.4 * np.clip(hits / 4.0, 0, 1)  # −35…−8 dB, сила onset 0…4
    return float(score.mean()), float(np.percentile(score, 90))


def analyze_music(conn: sqlite3.Connection, settings: Settings, payload: dict, progress) -> None:
    from analysis import track as track_mod

    row = conn.execute("SELECT * FROM music_tracks WHERE id = ?", (payload["track_id"],)).fetchone()
    if row is None:
        return
    path = file_of(settings, row)
    if not path.exists():
        conn.execute("UPDATE music_tracks SET status = 'missing' WHERE id = ?", (row["id"],))
        return
    out = analysis_path(settings, row["id"])
    try:
        if not out.exists():
            track_mod.analyze_file(path).save(out)
        ta = track_mod.TrackAnalysis.load(out)
    except Exception as exc:  # noqa: BLE001 — битий файл не валить чергу
        conn.execute("UPDATE music_tracks SET status = 'failed', error = ? WHERE id = ?", (str(exc)[:300], row["id"]))
        log.warning("трек %s не проаналізовано: %s", path.name, exc)
        return
    energy, peak = absolute_energy(path)
    tags = _tags(path)
    conn.execute(
        "UPDATE music_tracks SET status = 'analyzed', error = NULL, title = ?, artist = ?, duration = ?, tempo = ?,"
        " energy = ?, energy_peak = ?, drops = ?, choruses = ? WHERE id = ?",
        (tags["title"], tags["artist"], ta.duration, ta.tempo, round(energy, 3), round(peak, 3),
         len(ta.drops), len(ta.choruses), row["id"]))


# ---------- Google Drive: папка «Музика» ----------

def poll_drive(conn: sqlite3.Connection, settings: Settings, drive, folder_id: str, seen: dict) -> list[str]:
    """Нові стабільні аудіофайли з «Музики» → завантажити в music/drive/. З Drive нічого не видаляється."""
    from sources import gdrive

    done = gdrive._state(conn, "drive_music")
    queued = []
    for item in drive.list_children(folder_id):
        if item.is_folder or Path(item.name).suffix.lower() not in MUSIC_EXT or item.id in done:
            continue
        sig = (item.size, item.md5)
        prev, seen[item.id] = seen.get(item.id), sig
        if prev != sig or item.size is None:
            continue  # ще завантажується в Drive
        queue.enqueue(conn, "drive_music_fetch", {"file_id": item.id, "name": item.name, "md5": item.md5})
        queued.append(item.id)
    return queued


def drive_music_fetch(conn: sqlite3.Connection, settings: Settings, payload: dict, progress) -> None:
    from sources import gdrive
    from sources.ingest import safe_filename, unique_path

    drive = gdrive.get_drive(settings)
    if drive is None:
        raise RuntimeError("Google Drive не підключено")
    folder = settings.music_path / "drive"
    folder.mkdir(parents=True, exist_ok=True)
    dest = unique_path(folder, safe_filename(payload["name"]))
    tmp = dest.with_name(f".{dest.name}.part")
    drive.download(payload["file_id"], tmp, progress)
    if payload.get("md5") and gdrive.md5_file(tmp) != payload["md5"]:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"{payload['name']}: контрольна сума не збіглася — завантажу ще раз")
    tmp.replace(dest)
    done = gdrive._state(conn, "drive_music")
    done[payload["file_id"]] = dest.name
    gdrive._save_state(conn, "drive_music", done)
    scan(conn, settings)
