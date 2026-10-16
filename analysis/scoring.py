"""AI-оцінка шматків через Gemini Flash (Batch) з кешем, бюджетом і запасними локальними метриками.

Один запит = одне «вікно» відео (≤ GEMINI_WINDOW_SECONDS) з переліком шматків,
що вціліли після локального фільтра. Відповідь — JSON за схемою, перевіряється;
невалідна — одна повторна спроба, далі шматок оцінюється локально.
Кеш: оцінка прив'язана до (медіа, початок, кінець, версія стилю) — повторний
монтаж ті самі шматки вдруге не надсилає.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from analysis import quality, thresholds
from analysis.gemini_client import GeminiBackend, GenaiBackend, Result
from core import budget
from core.config import ROOT_DIR, Settings
from worker import queue

log = logging.getLogger(__name__)

PROMPT_VERSION = 1
MAX_ATTEMPTS = 2  # перша спроба + одна повторна
TAGS = ["натовп", "сцена", "артист", "обличчя", "руки", "світло", "деталь", "друзі", "селфі",
        "пейзаж", "місто", "дорога", "транспорт", "вода", "небо", "ніч", "захід сонця", "їжа",
        "тварини", "текст", "рух", "статика"]
EVENT_TYPES = ["концерт", "фестиваль", "вечірка", "подорож", "місто", "природа", "спорт", "інше"]

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "event_type": {"type": "string", "enum": EVENT_TYPES},
        "event_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "score": {"type": "number", "minimum": 0, "maximum": 10},
                    "description": {"type": "string", "maxLength": 200},
                    "tags": {"type": "array", "items": {"type": "string", "enum": TAGS}, "maxItems": 6},
                    "vertical_ok": {"type": "boolean"},
                    "subject_x": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "required": ["id", "score", "description", "tags", "vertical_ok"],
            },
        },
    },
    "required": ["event_type", "event_confidence", "segments"],
}

DEFAULT_TASTE = "Беру живі, емоційні моменти з атмосферою. Не беру нудне, порожнє і випадкове."


class InvalidResponse(ValueError):
    pass


# ---------- смак і версія стилю ----------

@dataclass
class Taste:
    text: str
    take: list[Path]
    skip: list[Path]

    @property
    def digest(self) -> str:
        h = hashlib.sha256(self.text.encode("utf-8"))
        for p in self.take + self.skip:
            h.update(p.name.encode())
            h.update(hashlib.sha256(p.read_bytes()).digest())
        return h.hexdigest()


IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp"}


def load_taste(settings: Settings) -> Taste:
    path = ROOT_DIR / "taste.md"
    text = path.read_text(encoding="utf-8").strip() if path.exists() else DEFAULT_TASTE
    base = settings.library_path / "taste"

    def images(sub: str) -> list[Path]:
        d = base / sub
        return sorted(p for p in d.iterdir() if p.suffix.lower() in IMAGE_EXT)[:20] if d.is_dir() else []

    return Taste(text, images("take"), images("skip"))


def style_key(style_id: str | None, taste: Taste, model: str) -> str:
    h = hashlib.sha256(f"{PROMPT_VERSION}|{model}|{taste.digest}".encode()).hexdigest()[:12]
    return f"{style_id or 'default'}:{h}"


# ---------- промпт і перевірка ----------

def _mmss(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m):02d}:{s:04.1f}"


def build_prompt(taste: Taste, items: list[tuple[int, float, float]]) -> str:
    lines = [
        "Ти — монтажер концертних і тревел-відео в ло-фай естетиці MiniDV.",
        "Оціни кожен шматок цього відео для монтажу кліпу і вертикальних роликів.",
        "Часи шматків вказані від початку цього відеофайлу (хвилини:секунди).",
        "",
        "Шкала оцінки: 0–2 брак або нудно, 3–4 слабко, 5–6 придатне, 7–8 сильне, 9–10 найкраще в події.",
        "Зерно, шум, легка тряска, поплилі кольори, засвітки — НЕ недолік, це естетика.",
        "description — до 12 слів українською, що відбувається в кадрі.",
        f"tags — лише з цього списку: {', '.join(TAGS)}.",
        "vertical_ok — чи можна вирізати вертикальний кадр 9:16, не втративши головного.",
        "subject_x — горизонтальна позиція головного об'єкта від 0 (лівий край) до 1 (правий).",
        f"event_type — тип події всього відео: {', '.join(EVENT_TYPES)}; event_confidence — впевненість 0–1.",
        "",
        "Смак користувача:",
        taste.text,
    ]
    if taste.take or taste.skip:
        lines += ["", "До запиту додано приклади кадрів: «БЕРУ» — як мені подобається, «НЕ БЕРУ» — як не треба."]
    lines += ["", "Шматки (id: початок–кінець):"]
    lines += [f"{i}: {_mmss(a)}–{_mmss(b)}" for i, a, b in items]
    lines += ["", "Поверни JSON з оцінкою для кожного id зі списку."]
    return "\n".join(lines)


def validate(text: str | None, expected_ids: set[int]) -> dict:
    import jsonschema

    if not text:
        raise InvalidResponse("порожня відповідь")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidResponse(f"не JSON: {exc}") from exc
    try:
        jsonschema.validate(data, RESPONSE_SCHEMA)
    except jsonschema.ValidationError as exc:
        raise InvalidResponse(f"не відповідає схемі: {exc.message}") from exc
    got = {s["id"] for s in data["segments"]}
    missing = expected_ids - got
    if missing:
        raise InvalidResponse(f"немає оцінок для id {sorted(missing)}")
    data["segments"] = [s for s in data["segments"] if s["id"] in expected_ids]
    return data


def gen_config(settings: Settings) -> dict:
    cfg: dict = {
        "response_mime_type": "application/json",
        "response_json_schema": RESPONSE_SCHEMA,
        "media_resolution": "MEDIA_RESOLUTION_LOW",
        "temperature": 0.3,
    }
    if settings.gemini_thinking_level:
        cfg["thinking_config"] = {"thinking_level": settings.gemini_thinking_level}
    return cfg


# ---------- бекенд і файли ----------

def get_backend(settings: Settings) -> GeminiBackend | None:
    key = settings.gemini_api_key.get_secret_value()
    return GenaiBackend(key) if key else None


def _fresh(expires_at: str | None, hours: float = 12) -> bool:
    if not expires_at:
        return False
    exp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    return exp - datetime.now(timezone.utc) > timedelta(hours=hours)


def _cut_window(src: Path, dst: Path, start: float, end: float) -> None:
    """Вікно довгого відео — копіювання потоку без перекодування (ключові кадри щосекунди)."""
    tmp = dst.with_name(f".{dst.stem}.part{dst.suffix}")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-ss", f"{start:.3f}",
           "-i", str(src), "-t", f"{end - start:.3f}", "-c", "copy", "-avoid_negative_ts", "make_zero", str(tmp)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=600)
    tmp.replace(dst)


def ensure_window_file(conn: sqlite3.Connection, settings: Settings, backend: GeminiBackend,
                       media: sqlite3.Row, win_start: float, win_end: float) -> str:
    """URI вікна у Files API (файли живуть 48 год — завантажуємо заново, якщо скоро зникне)."""
    row = conn.execute("SELECT * FROM gemini_files WHERE media_id = ? AND win_start = ?",
                       (media["id"], win_start)).fetchone()
    if row and row["uri"] and abs(row["win_end"] - win_end) < 1e-3 and _fresh(row["expires_at"]):
        return row["uri"]
    library = settings.library_path
    proxy = library / media["gemini_proxy_path"]
    if win_start <= 0 and win_end >= (media["duration"] or 0) - 0.05:
        local = proxy
    else:
        local = library / media["slug"] / "analysis" / f"{media['id']}_w{int(win_start * 1000)}.mp4"
        _cut_window(proxy, local, win_start, win_end)
    up = backend.upload(local, "video/mp4")
    conn.execute(
        "INSERT INTO gemini_files(media_id, win_start, win_end, local_path, uri, name, expires_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(media_id, win_start) DO UPDATE SET"
        " win_end = excluded.win_end, local_path = excluded.local_path, uri = excluded.uri,"
        " name = excluded.name, expires_at = excluded.expires_at",
        (media["id"], win_start, win_end, str(local.relative_to(library)), up.uri, up.name, up.expires_at))
    return up.uri


def ensure_example_files(conn: sqlite3.Connection, backend: GeminiBackend, taste: Taste) -> list[tuple[str, str, str]]:
    """Приклади смаку в Files API (кеш у settings). Повертає (мітка, uri, mime)."""
    row = conn.execute("SELECT value FROM settings WHERE key = 'taste_files'").fetchone()
    cache: dict = json.loads(row["value"]) if row else {}
    out = []
    for label, paths in (("БЕРУ", taste.take), ("НЕ БЕРУ", taste.skip)):
        for p in paths:
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
            mime = "image/png" if p.suffix.lower() == ".png" else "image/webp" if p.suffix.lower() == ".webp" else "image/jpeg"
            hit = cache.get(digest)
            if not hit or not _fresh(hit.get("expires_at")):
                up = backend.upload(p, mime)
                hit = cache[digest] = {"uri": up.uri, "expires_at": up.expires_at}
            out.append((label, hit["uri"], mime))
    conn.execute("INSERT INTO settings(key, value) VALUES ('taste_files', ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(cache),))
    return out


def build_contents(prompt: str, video_uri: str, examples: list[tuple[str, str, str]]) -> list[dict]:
    parts: list[dict] = [{"text": prompt}]
    for i, (label, uri, mime) in enumerate(examples, 1):
        parts.append({"text": f"Приклад {label} #{i}:"})
        parts.append({"file_data": {"file_uri": uri, "mime_type": mime}})
    parts.append({"text": "Відео для оцінки:"})
    parts.append({"file_data": {"file_uri": video_uri, "mime_type": "video/mp4"}, "video_metadata": {"fps": 1}})
    return [{"role": "user", "parts": parts}]


# ---------- вибір шматків ----------

@dataclass
class Candidate:
    media_id: int
    start: float
    end: float
    local_score: float

    @property
    def key(self) -> tuple[int, float, float]:
        return (self.media_id, self.start, self.end)


def candidates(conn: sqlite3.Connection, project_id: int, th: thresholds.FilterThresholds) -> list[Candidate]:
    """Шматки, що проходять локальний фільтр за порогами стилю проєкту."""
    out = []
    rows = conn.execute(
        "SELECT s.* FROM segments s JOIN media m ON m.id = s.media_id"
        " WHERE m.project_id = ? AND m.status = 'analyzed' ORDER BY s.media_id, s.start", (project_id,))
    for r in rows:
        m = json.loads(r["metrics"])
        if quality.reject_reason(m, r["end"] - r["start"], th) is None:
            out.append(Candidate(r["media_id"], r["start"], r["end"], quality.local_score(m, th)))
    return out


def _segment_state(conn: sqlite3.Connection, style: str, media_id: int) -> tuple[set, dict]:
    """(шматки з оцінкою Gemini, {шматок: [статуси запитів]})."""
    scored = {(r["media_id"], r["start"], r["end"]) for r in conn.execute(
        "SELECT media_id, start, end FROM scores WHERE style_key = ? AND source = 'gemini' AND media_id = ?",
        (style, media_id))}
    attempts: dict[tuple, list[str]] = {}
    for r in conn.execute("SELECT segments, status FROM gemini_requests WHERE style_key = ? AND media_id = ?",
                          (style, media_id)):
        for a, b in json.loads(r["segments"]):
            attempts.setdefault((media_id, a, b), []).append(r["status"])
    return scored, attempts


def windows(cands: list[Candidate], size: float) -> list[tuple[float, float, list[Candidate]]]:
    """Жадібне групування шматків одного медіа у вікна ≤ size (по початку шматка)."""
    out: list[tuple[float, float, list[Candidate]]] = []
    for c in sorted(cands, key=lambda c: c.start):
        if out and c.start < out[-1][0] + size:
            ws, we, items = out[-1]
            out[-1] = (ws, max(we, c.end), items + [c])
        else:
            out.append((max(0.0, c.start), c.end, [c]))
    return out


def write_local_scores(conn: sqlite3.Connection, style: str, cands: list[Candidate]) -> int:
    """Запасна оцінка з локальних метрик, де ще немає жодної."""
    n = 0
    for c in cands:
        cur = conn.execute(
            "INSERT OR IGNORE INTO scores(media_id, start, end, style_key, source, score)"
            " VALUES (?, ?, ?, ?, 'local', ?)", (c.media_id, c.start, c.end, style, round(c.local_score * 10, 2)))
        n += cur.rowcount
    return n


# ---------- етапи воркера ----------

Progress = Callable[[float], None]


@dataclass
class Planned:
    media: sqlite3.Row
    win_start: float
    win_end: float
    items: list[Candidate]
    attempt: int
    est: float


def _plan(conn, settings, project, style, taste, cands) -> list[Planned]:
    by_media: dict[int, list[Candidate]] = {}
    for c in cands:
        by_media.setdefault(c.media_id, []).append(c)
    plan: list[Planned] = []
    prompt_chars = len(build_prompt(taste, [])) + 30 * 20
    for media_id, items in by_media.items():
        media = conn.execute("SELECT m.*, p.slug FROM media m JOIN projects p ON p.id = m.project_id WHERE m.id = ?",
                             (media_id,)).fetchone()
        scored, attempts = _segment_state(conn, style, media_id)
        need, retry = [], []
        for c in items:
            st = attempts.get(c.key, [])
            if c.key in scored or any(s in ("pending", "submitted") for s in st) or len(st) >= MAX_ATTEMPTS:
                continue
            (retry if st else need).append(c)
        has_audio = bool(json.loads(media["probe_json"] or "{}").get("has_audio"))
        for group, attempt in ((need, 1), (retry, 2)):
            for ws, we, w_items in windows(group, settings.gemini_window_seconds):
                est = budget.estimate_request(settings.gemini_model, we - ws, has_audio, prompt_chars,
                                              len(taste.take) + len(taste.skip), len(w_items), batch=True)
                plan.append(Planned(media, ws, we, w_items, attempt, est))
    return plan


def _submit(conn, settings, backend, project, style, taste, plan: list[Planned]) -> str | None:
    """Завантажує вікна, створює записи запитів і один batch. Повертає ім'я batch-у."""
    if not plan:
        return None
    examples = ensure_example_files(conn, backend, taste) if (taste.take or taste.skip) else []
    cfg = gen_config(settings)
    requests, ids = [], []
    try:
        for p in plan:
            uri = ensure_window_file(conn, settings, backend, p.media, p.win_start, p.win_end)
            segs = [[c.start, c.end] for c in p.items]
            rid = conn.execute(
                "INSERT INTO gemini_requests(project_id, media_id, win_start, win_end, style_key, segments, attempt,"
                " mode, status, est_cost) VALUES (?, ?, ?, ?, ?, ?, ?, 'batch', 'pending', ?)",
                (project["id"], p.media["id"], p.win_start, p.win_end, style, json.dumps(segs), p.attempt, p.est)
            ).lastrowid
            ids.append(rid)
            items = [(i, c.start - p.win_start, c.end - p.win_start) for i, c in enumerate(p.items)]
            requests.append({"key": str(rid), "contents": build_contents(build_prompt(taste, items), uri, examples),
                             "config": cfg})
        name = backend.submit_batch(settings.gemini_model, requests, f"automontage-{project['id']}")
    except Exception:
        # нічого не надіслано — прибираємо резерв, щоб повторна спроба завдання почала з чистого
        conn.executemany("DELETE FROM gemini_requests WHERE id = ?", [(i,) for i in ids])
        raise
    conn.executemany("UPDATE gemini_requests SET status = 'submitted', batch_name = ?,"
                     " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", [(name, i) for i in ids])
    return name


def _pending_analysis(conn, project_id: int) -> bool:
    return conn.execute("SELECT 1 FROM jobs WHERE project_id = ? AND kind IN ('prepare_media', 'analyze_media', 'analyze_track')"
                        " AND status IN ('queued', 'running')", (project_id,)).fetchone() is not None


def _active_requests(conn, project_id: int) -> bool:
    return conn.execute("SELECT 1 FROM gemini_requests WHERE project_id = ? AND status IN ('pending', 'submitted')",
                        (project_id,)).fetchone() is not None


def _finish_scoring(conn, project_id: int) -> None:
    if not _active_requests(conn, project_id):
        conn.execute("UPDATE projects SET status = 'editing' WHERE id = ?", (project_id,))
        queue.enqueue(conn, "edit_project", {"project_id": project_id}, project_id=project_id)


def score_project(conn: sqlite3.Connection, settings: Settings, payload: dict, progress: Progress,
                  job_id: int | None = None) -> str | None:
    project_id = payload["project_id"]
    project = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        return None
    if _pending_analysis(conn, project_id):
        queue.enqueue(conn, "score_project", {"project_id": project_id}, project_id=project_id,
                      dedupe=False, delay_seconds=15)
        return None
    conn.execute("UPDATE projects SET status = 'scoring' WHERE id = ?", (project_id,))
    th = thresholds.for_style(project["style_id"], settings.default_filter_preset)
    taste = load_taste(settings)
    style = style_key(project["style_id"], taste, settings.gemini_model)
    cands = candidates(conn, project_id, th)
    backend = get_backend(settings)

    plan = _plan(conn, settings, project, style, taste, cands) if backend else []
    if plan:
        total = round(sum(p.est for p in plan), 4)
        approved = payload.get("approved")
        if approved is None and total > settings.confirm_threshold_usd and job_id is not None:
            queue.await_confirm(conn, job_id, {"forecast_usd": total, "requests": len(plan),
                                               "segments": sum(len(p.items) for p in plan)})
            conn.execute("UPDATE projects SET status = 'awaiting_confirm' WHERE id = ?", (project_id,))
            return queue.KEEP_STATUS
        if approved is False:
            plan = []
        # бюджет і денний ліміт: найперспективніші вікна першими, решта — локально
        st = budget.state(conn, settings.monthly_budget_usd, settings.daily_request_limit)
        plan.sort(key=lambda p: -sum(c.local_score for c in p.items) / len(p.items))
        fitted, spent = [], 0.0
        for p in plan:
            if len(fitted) < st.requests_left and spent + p.est <= st.available:
                fitted.append(p)
                spent += p.est
        if len(fitted) < len(plan):
            log.warning("проєкт %s: бюджет/ліміт дозволяє %s з %s запитів, решта — локальні метрики",
                        project_id, len(fitted), len(plan))
        plan = fitted
    progress(0.2)

    write_local_scores(conn, style, cands)
    batch = _submit(conn, settings, backend, project, style, taste, plan) if plan else None
    progress(1.0)
    if batch:
        queue.enqueue(conn, "poll_batch", {"batch": batch, "project_id": project_id}, project_id=project_id,
                      delay_seconds=settings.gemini_poll_seconds)
    else:
        _finish_scoring(conn, project_id)
    return None


score_project.wants_job_id = True


def _apply_result(conn, settings, req: sqlite3.Row, res: Result | None, batch_ok: bool) -> bool:
    """Записує результат одного запиту. Повертає True, якщо потрібна повторна спроба."""
    segs = json.loads(req["segments"])
    if res is not None and res.usage:
        cost = budget.record_spend(conn, req["project_id"], res.model_version or settings.gemini_model,
                                   "оцінка кадрів", res.usage, batch=req["mode"] == "batch", request_id=req["id"])
        conn.execute("UPDATE gemini_requests SET cost = ? WHERE id = ?", (cost, req["id"]))
    error = None
    if res is None:
        error = "batch не повернув результат" if batch_ok else "batch завершився з помилкою"
    elif res.error:
        error = res.error
    else:
        try:
            data = validate(res.text, set(range(len(segs))))
        except InvalidResponse as exc:
            error = str(exc)
        else:
            for s in data["segments"]:
                a, b = segs[s["id"]]
                conn.execute(
                    "INSERT INTO scores(media_id, start, end, style_key, source, score, description, tags,"
                    " vertical_ok, subject_x, request_id) VALUES (?, ?, ?, ?, 'gemini', ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(media_id, start, end, style_key) DO UPDATE SET source = 'gemini',"
                    " score = excluded.score, description = excluded.description, tags = excluded.tags,"
                    " vertical_ok = excluded.vertical_ok, subject_x = excluded.subject_x,"
                    " request_id = excluded.request_id",
                    (req["media_id"], a, b, req["style_key"], s["score"], s["description"],
                     json.dumps(s["tags"], ensure_ascii=False), int(s["vertical_ok"]), s.get("subject_x"),
                     req["id"]))
            conn.execute("UPDATE gemini_requests SET status = 'done', event_type = ?, event_confidence = ?,"
                         " est_cost = 0, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?",
                         (data["event_type"], data["event_confidence"], req["id"]))
            return False
    status = "invalid" if res is not None and not res.error else "failed"
    conn.execute("UPDATE gemini_requests SET status = ?, error = ?, est_cost = 0,"
                 " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = ?", (status, error[:500], req["id"]))
    log.warning("запит Gemini %s: %s", req["id"], error)
    return req["attempt"] < MAX_ATTEMPTS


def poll_batch(conn: sqlite3.Connection, settings: Settings, payload: dict, progress: Progress) -> None:
    name, project_id = payload["batch"], payload["project_id"]
    backend = get_backend(settings)
    reqs = conn.execute("SELECT * FROM gemini_requests WHERE batch_name = ? AND status = 'submitted' ORDER BY id",
                        (name,)).fetchall()
    if not reqs:
        _finish_scoring(conn, project_id)
        return
    if backend is None:  # ключ прибрали — далі лише локально
        status_obj = None
    else:
        status_obj = backend.get_batch(name)
        if not status_obj.done:
            queue.enqueue(conn, "poll_batch", payload, project_id=project_id, dedupe=False,
                          delay_seconds=settings.gemini_poll_seconds)
            return
    batch_ok = status_obj is not None and status_obj.state != "JOB_STATE_FAILED" and not status_obj.state.endswith(
        ("CANCELLED", "EXPIRED"))
    need_retry = False
    for i, req in enumerate(reqs):
        res = None
        if status_obj is not None:
            res = status_obj.results.get(str(req["id"])) or status_obj.results.get(str(i))
        need_retry |= _apply_result(conn, settings, req, res, batch_ok)
    if need_retry and backend is not None:
        # одна повторна спроба для невалідних — новим batch-ем через той самий етап
        queue.enqueue(conn, "score_project", {"project_id": project_id, "approved": True}, project_id=project_id)
        return
    _finish_scoring(conn, project_id)
