"""Правки словами: текст → структуровані зміни → «тільки тут» або «назавжди».

1. Локальний розбір частих фраз («більше натовпу», «швидше», «без вставок», «60 секунд»,
   «стиль VHS») — без запиту в Gemini.
2. Решта — миттєвий запит у Gemini з контекстом монтажу; відповідь за JSON-схемою.
3. «Тут» — зміни в налаштуваннях проєкту, перемонтаж на кешованих оцінках.
   «Назавжди» — правило в `taste_rules` (діє на всі проєкти) і рядок у розділі правил `taste.md`.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from analysis.scoring import TAGS

TRANSITION_IDS = ["flash", "zoom", "whip", "glitch", "rewind", "freeze", "burn"]
STYLE_IDS_HINT = ["minidv-concert", "summer-trip", "vhs-90s", "clean"]

CHANGES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "maxLength": 300},
        "rule_text": {"type": "string", "maxLength": 200},
        "rhythm": {"type": "object", "properties": {k: {"type": "integer", "enum": [1, 2, 4, 8]}
                                                    for k in ("low", "mid", "high")}},
        "tag_weights": {"type": "object", "additionalProperties": {"type": "number", "minimum": -1, "maximum": 1}},
        "length": {"type": "number", "minimum": 5, "maximum": 600},
        "track_start": {"type": "number", "minimum": 0},
        "track_end": {"type": "number", "minimum": 0},
        "inserts": {"type": "boolean"},
        "max_inserts": {"type": "integer", "minimum": 0, "maximum": 40},
        "live_gain_db": {"type": "number", "minimum": -20, "maximum": 20},
        "exclude_at": {"type": "array", "items": {"type": "number", "minimum": 0}, "maxItems": 20},
        "transitions": {"type": "boolean"},
        "disable_transitions": {"type": "array", "items": {"type": "string", "enum": TRANSITION_IDS}},
        "enable_transitions": {"type": "array", "items": {"type": "string", "enum": TRANSITION_IDS}},
        "style_id": {"type": "string"},
        "style_level_delta": {"type": "integer", "minimum": -2, "maximum": 2},
        "vertical_count": {"type": "integer", "minimum": 1, "maximum": 10},
        "vertical_length": {"type": "number", "minimum": 15, "maximum": 25},
        "vertical_audio": {"type": "string", "enum": ["live", "track", "none"]},
        "aspect": {"type": "string", "enum": ["16:9", "9:16", "1:1", "4:5"]},
        "fit_mode": {"type": "string", "enum": ["auto", "crop", "blur"]},
        "vertical_aspect": {"type": "string", "enum": ["9:16", "4:5", "1:1"]},
        "needs_reanalysis": {"type": "boolean"},
    },
    "required": ["summary"],
    "additionalProperties": False,
}

# ---------- локальний розбір ----------

TAG_STEMS = {
    "натовп": "натовп", "людей": "натовп", "публік": "натовп", "фанат": "натовп",
    "сцен": "сцена", "артист": "артист", "співак": "артист", "гурт": "артист", "музикант": "артист",
    "облич": "обличчя", "крупн": "обличчя", "рук": "руки", "світл": "світло", "прожектор": "світло",
    "деталь": "деталь", "друз": "друзі", "селфі": "селфі", "пейзаж": "пейзаж", "природ": "пейзаж",
    "міст": "місто", "вулиц": "місто", "дорог": "дорога", "машин": "транспорт", "поїзд": "транспорт",
    "вод": "вода", "мор": "вода", "неб": "небо", "ніч": "ніч", "захід": "захід сонця", "їж": "їжа",
    "тварин": "тварини", "собак": "тварини", "рух": "рух", "статик": "статика",
}
STYLE_WORDS = {"vhs": "vhs-90s", "віхаес": "vhs-90s", "касет": "vhs-90s", "90-х": "vhs-90s",
               "minidv": "minidv-concert", "мінідв": "minidv-concert", "камкордер": "minidv-concert",
               "літн": "summer-trip", "поїздк": "summer-trip", "плівк": "summer-trip",
               "чист": "clean", "без стилю": "clean", "без фільтр": "clean"}
WEIGHT = 0.4


def _tags_after(text: str, word: str) -> list[str]:
    out = []
    for m in re.finditer(rf"{word}\s+([\w'’-]+)(?:\s+(?:і|та|й)\s+([\w'’-]+))?", text):
        for w in filter(None, m.groups()):
            for stem, tag in TAG_STEMS.items():
                if w.startswith(stem) and tag not in out:
                    out.append(tag)
    return out


def _seconds(s: str) -> float:
    if ":" in s:
        m, sec = s.split(":", 1)
        return int(m) * 60 + float(sec)
    return float(s)


def local_parse(text: str) -> dict | None:
    """Розпізнає типові правки. None — не зрозуміло, треба Gemini."""
    t = " " + text.lower().replace("’", "'").strip() + " "
    ch: dict[str, Any] = {}
    notes: list[str] = []

    weights = {tag: WEIGHT for tag in _tags_after(t, "більше")}
    weights.update({tag: -WEIGHT for tag in _tags_after(t, "менше")})
    weights.update({tag: -1.0 for tag in _tags_after(t, r"(?:без|прибери|прибрати)")})
    if weights:
        ch["tag_weights"] = weights
        notes += [("більше" if w > 0 else "менше") + f" «{tag}»" for tag, w in weights.items()]
        if weights.get("натовп", 0) > 0:
            ch["max_inserts"] = 4  # більше натовпу — і більше живих вставок
            ch["inserts"] = True

    if re.search(r"швидш|динамічн|частіш\w* склейк|рваніш|агресивн", t):
        ch["rhythm"] = {"low": 2, "mid": 1, "high": 1}
        notes.append("склейки частіше")
    elif re.search(r"повільн|спокійн|рідш\w* склейк|плавніш", t):
        ch["rhythm"] = {"low": 8, "mid": 4, "high": 2}
        notes.append("склейки рідше")

    if re.search(r"без (живих )?вставок|прибер\w* вставк|вимкн\w* вставк", t):
        ch["inserts"] = False
        notes.append("без живих вставок")
    elif re.search(r"більше (живих )?вставок", t):
        ch["inserts"], ch["max_inserts"] = True, 4
        notes.append("більше живих вставок")

    if re.search(r"без переход|прибер\w* переход|тільки різк", t):
        ch["transitions"] = False
        notes.append("без ефектних переходів")

    m = re.search(r"з (\d+:\d{2}|\d+(?:[.,]\d+)?) ?(?:с|сек\w*)? до (\d+:\d{2}|\d+(?:[.,]\d+)?)", t)
    if m:
        ch["track_start"], ch["track_end"] = (_seconds(x.replace(",", ".")) for x in m.groups())
        notes.append(f"шматок треку {m.group(1)}–{m.group(2)}")
    elif (m := re.search(r"(\d+(?:[.,]\d+)?)\s*(?:с\b|сек|секунд)", t)) and "вертикал" not in t:
        ch["length"] = float(m.group(1).replace(",", "."))
        notes.append(f"довжина {ch['length']:g} с")
    elif (m := re.search(r"(\d+(?:[.,]\d+)?)\s*хв", t)):
        ch["length"] = float(m.group(1).replace(",", ".")) * 60
        notes.append(f"довжина {ch['length']:g} с")

    for word, sid in STYLE_WORDS.items():
        if re.search(rf"(?:стиль|стилі|як|зроби)\s+\S*{re.escape(word)}|{re.escape(word)}\S* стиль", t):
            ch["style_id"] = sid
            notes.append(f"стиль {sid}")
            break
    if re.search(r"більше зерна|сильніш\w* стиль|ще більше ло-?фай|агресивніш\w* ефект", t):
        ch["style_level_delta"] = 1
        notes.append("стиль інтенсивніше")
    elif re.search(r"менше зерна|м'якш\w* стиль|слабш\w* стиль|чистіш", t):
        ch["style_level_delta"] = -1
        notes.append("стиль м'якше")

    # формат кадру: «квадрат», «1:1», «4:5 для інсти», «вертикальний кліп 9:16»
    fmt = None
    if re.search(r"\b1\s*[:x×]\s*1\b|квадрат", t):
        fmt = "1:1"
    elif re.search(r"\b4\s*[:x×]\s*5\b", t):
        fmt = "4:5"
    elif re.search(r"\b9\s*[:x×]\s*16\b", t):
        fmt = "9:16"
    elif re.search(r"\b16\s*[:x×]\s*9\b|горизонтальн", t):
        fmt = "16:9"
    reel_words = re.search(r"вертикал|рілс|тікток|ролик", t)
    if fmt and reel_words and fmt != "16:9" and not re.search(r"кліп", t):
        ch["vertical_aspect"] = fmt
        notes.append(f"вертикалі у форматі {fmt}")
    elif fmt:
        ch["aspect"] = fmt
        notes.append(f"кліп у форматі {fmt}")
    if re.search(r"розмит\w* фон", t):
        ch["fit_mode"] = "blur"
        notes.append("кадри іншої форми — на розмитому фоні")
    elif re.search(r"обріж|обріз|кадруй|без (розмит|рамок|смуг)", t):
        ch["fit_mode"] = "crop"
        notes.append("кадри іншої форми — обрізати")

    if "вертикал" in t or "рілс" in t or "тікток" in t:
        if re.search(r"без (музики|звуку|треку)", t):
            ch["vertical_audio"] = "none"
            notes.append("вертикалі без музики")
        elif re.search(r"(з|під) трек", t):
            ch["vertical_audio"] = "track"
            notes.append("вертикалі під трек")
        if (m := re.search(r"(\d+)\s*(?:вертикал|рілс|ролик)", t)):
            ch["vertical_count"] = int(m.group(1))
            notes.append(f"{m.group(1)} вертикалей")
        if (m := re.search(r"(\d+)\s*(?:с\b|сек)", t)):
            ch["vertical_length"] = min(25.0, max(15.0, float(m.group(1))))
            notes.append(f"вертикалі по {ch['vertical_length']:g} с")

    if not ch:
        return None
    ch["summary"] = "; ".join(notes).capitalize()
    ch["rule_text"] = text.strip()[:200]
    return ch


# ---------- Gemini ----------

def build_prompt(text: str, context: dict) -> str:
    return "\n".join([
        "Ти — помічник монтажера. Перетвори правку користувача на структуровані зміни монтажу.",
        "Заповнюй лише ті поля, яких стосується правка. summary — коротко українською, що зміниться.",
        "rule_text — правка як загальне правило смаку (на випадок «назавжди»).",
        "rhythm — склейка кожні N бітів для тихих (low), середніх (mid) і енергійних (high) частин (1, 2, 4 або 8).",
        f"tag_weights — вага тегів від −1 до 1, теги: {', '.join(TAGS)}.",
        "exclude_at — секунди таймлайну, де стоять кадри, які треба прибрати (див. список кліпів).",
        f"Переходи: {', '.join(TRANSITION_IDS)}. Стилі: {', '.join(context.get('styles', STYLE_IDS_HINT))}.",
        "needs_reanalysis — true лише якщо правка вимагає наново оцінити відео (наприклад, змінився сам смак).",
        "",
        "Поточний монтаж:",
        json.dumps(context, ensure_ascii=False)[:6000],
        "",
        f"Правка: {text}",
    ])


def parse(conn: sqlite3.Connection, settings, text: str, context: dict, project_id: int | None) -> tuple[dict, str]:
    """(зміни, джерело: 'local' | 'gemini'). Кидає LLMUnavailable, якщо локально не зрозуміло, а Gemini недоступний."""
    local = local_parse(text)
    if local:
        return local, "local"
    from analysis.llm import ask_json
    data = ask_json(conn, settings, build_prompt(text, context), CHANGES_SCHEMA, "правка словами", project_id)
    return data, "gemini"


# ---------- застосування ----------

CLIP_KEYS = {"length", "track_start", "track_end", "inserts", "max_inserts", "live_gain_db", "aspect", "fit_mode"}
VERTICAL_KEYS = {"vertical_count": "count", "vertical_length": "length", "vertical_audio": "audio",
                 "vertical_aspect": "aspect"}


def merge_clip(params: dict, ch: dict, timeline_clips: list[dict] | None = None) -> dict:
    p = json.loads(json.dumps(params))  # глибока копія
    for k in CLIP_KEYS & ch.keys():
        p[k] = ch[k]
    if "rhythm" in ch:
        p["beats_per_level"] = {**p.get("beats_per_level", {}), **ch["rhythm"]}
    if "tag_weights" in ch:
        tw = dict(p.get("tag_weights", {}))
        for tag, w in ch["tag_weights"].items():
            tw[tag] = round(max(-1.0, min(1.0, tw.get(tag, 0.0) + w)), 3)
        p["tag_weights"] = tw
    if "transitions" in ch:
        p["transitions"] = ch["transitions"]
    dis = set(p.get("disabled_transitions", [])) | set(ch.get("disable_transitions", []))
    dis -= set(ch.get("enable_transitions", []))
    p["disabled_transitions"] = sorted(dis)
    if ch.get("exclude_at") and timeline_clips:
        ex = [list(x) for x in p.get("excluded", [])]
        for t in ch["exclude_at"]:
            c = next((c for c in timeline_clips if c["start"] <= t < c["end"]), None)
            if c and c["kind"] == "beat":
                ex.append([c["media_id"], round(c["src_in"], 3)])
        p["excluded"] = ex
    return p


def merge_vertical(params: dict, ch: dict) -> dict:
    p = dict(params)
    for src, dst in VERTICAL_KEYS.items():
        if src in ch:
            p[dst] = ch[src]
    return p


def touches_vertical(ch: dict) -> bool:
    return bool(VERTICAL_KEYS.keys() & ch.keys()) or "tag_weights" in ch or "style_id" in ch or "style_level_delta" in ch


# ---------- правила «назавжди» ----------

RULES_START = "<!-- правила:початок (керується програмою, редагуй у розділі «Смак») -->"
RULES_END = "<!-- правила:кінець -->"


def active_rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM taste_rules WHERE active = 1 ORDER BY id").fetchall()


def global_clip_defaults(conn: sqlite3.Connection, base: dict) -> dict:
    """Базові параметри кліпу з урахуванням правил «назавжди» (по черзі, як їх додавали)."""
    p = base
    for r in active_rules(conn):
        ch = json.loads(r["changes"])
        ch.pop("exclude_at", None)  # виключення конкретних кадрів не буває «назавжди»
        for k in ("length", "track_start", "track_end"):
            ch.pop(k, None)  # довжина й межі треку — властивість конкретного проєкту
        p = merge_clip(p, ch)
    return p


def global_vertical_defaults(conn: sqlite3.Connection, base: dict) -> dict:
    p = base
    for r in active_rules(conn):
        p = merge_vertical(p, json.loads(r["changes"]))
    return p


def global_style(conn: sqlite3.Connection) -> str | None:
    sid = None
    for r in active_rules(conn):
        sid = json.loads(r["changes"]).get("style_id", sid)
    return sid


def sync_taste_md(conn: sqlite3.Connection, path) -> None:
    """Розділ правил у taste.md — завжди копія активних правил з бази."""
    text = path.read_text(encoding="utf-8") if path.exists() else "# Смак\n"
    lines = [f"- {r['text']}" for r in active_rules(conn)]
    block = "\n".join([RULES_START, "## Правила «назавжди»", *(lines or ["- (поки немає)"]), RULES_END])
    if RULES_START in text and RULES_END in text:
        a, rest = text.split(RULES_START, 1)
        _, b = rest.split(RULES_END, 1)
        text = a + block + b
    else:
        text = text.rstrip() + "\n\n" + block + "\n"
    path.write_text(text, encoding="utf-8")
