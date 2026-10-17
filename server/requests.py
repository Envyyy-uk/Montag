"""API правок словами, смаку і стилів словами."""

from __future__ import annotations

import json
import re
import sqlite3

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from analysis.llm import LLMUnavailable
from core.config import Settings
from core.projects import ProjectError, get_project
from edit import changes as ch_mod
from edit.build import project_params
from edit.verticals import vertical_params
from render import style_gen, style_store
from render.looks import StyleError, get_style, list_styles
from server.deps import db, settings_dep
from worker import queue

router = APIRouter(prefix="/api", tags=["requests"])

QUESTION = "Тільки тут чи назавжди?"


class EditText(BaseModel):
    text: str = Field(min_length=2, max_length=500)


class Scope(BaseModel):
    scope: str = Field(pattern="^(here|always)$")


class RuleUpdate(BaseModel):
    text: str | None = Field(None, min_length=2, max_length=300)
    active: bool | None = None


class Instruction(BaseModel):
    instruction: str = Field(max_length=20000)


class StyleText(BaseModel):
    text: str = Field(min_length=3, max_length=500)
    media_id: int | None = None
    start: float = 0.0


class Suggestion(BaseModel):
    accept: bool = True
    style_id: str | None = None


def _project(conn, project_id: int):
    try:
        return get_project(conn, project_id)
    except ProjectError as exc:
        raise HTTPException(404, str(exc)) from exc


def _latest_timeline(conn, settings: Settings, project_id: int) -> dict | None:
    row = conn.execute("SELECT timeline_path FROM edits WHERE project_id = ? AND kind = 'clip'"
                       " ORDER BY version DESC LIMIT 1", (project_id,)).fetchone()
    if row is None:
        return None
    return json.loads((settings.library_path / row["timeline_path"]).read_text(encoding="utf-8"))


def _context(conn, settings: Settings, project) -> dict:
    """Що бачить Gemini для правки: параметри, кліпи з описами, стилі."""
    tl = _latest_timeline(conn, settings, project["id"])
    clips = []
    for c in (tl or {}).get("clips", [])[:80]:
        sc = conn.execute("SELECT description FROM scores WHERE media_id = ? AND start <= ? AND end > ?"
                          " ORDER BY (source = 'gemini') DESC LIMIT 1", (c["media_id"], c["src_in"], c["src_in"])
                          ).fetchone()
        clips.append({"t": round(c["start"], 2), "до": round(c["end"], 2), "тип": c["kind"],
                      "опис": sc["description"] if sc and sc["description"] else "", "теги": c.get("tags", [])})
    return {"кліп": project_params(project, conn), "вертикалі": vertical_params(project, conn),
            "таймлайн": clips, "маркери": (tl or {}).get("markers", []),
            "styles": [s.id for s in list_styles(include_drafts=False)]}


# ---------- правки ----------

@router.post("/projects/{project_id}/requests")
def create_request(project_id: int, body: EditText, conn: sqlite3.Connection = Depends(db),
                   settings: Settings = Depends(settings_dep)) -> dict:
    project = _project(conn, project_id)
    try:
        changes, source = ch_mod.parse(conn, settings, body.text, _context(conn, settings, project), project_id)
    except LLMUnavailable as exc:
        raise HTTPException(422, f"Не вдалося зрозуміти правку без Gemini ({exc}). Спробуй простіше: "
                                 "«більше натовпу», «швидше», «без вставок», «45 секунд».") from exc
    if "style_id" in changes:
        try:
            get_style(changes["style_id"])
        except StyleError:
            changes.pop("style_id")
    rid = conn.execute("INSERT INTO edit_requests(project_id, text, changes, source) VALUES (?, ?, ?, ?)",
                       (project_id, body.text, json.dumps(changes, ensure_ascii=False), source)).lastrowid
    return {"id": rid, "summary": changes.get("summary", ""), "changes": changes, "source": source,
            "question": QUESTION}


@router.get("/projects/{project_id}/requests")
def list_requests(project_id: int, conn: sqlite3.Connection = Depends(db)) -> list[dict]:
    _project(conn, project_id)
    return [{"id": r["id"], "text": r["text"], "status": r["status"], "source": r["source"],
             "summary": json.loads(r["changes"]).get("summary", ""), "created_at": r["created_at"]}
            for r in conn.execute("SELECT * FROM edit_requests WHERE project_id = ? ORDER BY id DESC", (project_id,))]


def apply_here(conn, settings: Settings, project, changes: dict) -> list[str]:
    """Зміни в налаштуваннях проєкту + перемонтаж на кешованих оцінках. Повертає види запущених завдань."""
    stored = json.loads(project["settings"] or "{}")
    tl = _latest_timeline(conn, settings, project["id"])
    clip = ch_mod.merge_clip(project_params(project, conn), changes, (tl or {}).get("clips"))
    if "style_level_delta" in changes:
        _, level = style_store.resolve_for_project(conn, settings, project, clip.get("style_level"))
        clip["style_level"] = max(1, min(3, level + int(changes["style_level_delta"])))
    stored["clip"] = clip
    stored["vertical"] = ch_mod.merge_vertical(vertical_params(project, conn), changes)
    conn.execute("UPDATE projects SET settings = ? WHERE id = ?", (json.dumps(stored, ensure_ascii=False), project["id"]))
    if changes.get("style_id"):
        conn.execute("UPDATE projects SET style_id = ? WHERE id = ?", (changes["style_id"], project["id"]))
    if changes.get("needs_reanalysis"):
        queue.enqueue(conn, "score_project", {"project_id": project["id"]}, project_id=project["id"])
        return ["score_project"]
    kinds = ["edit_project"]
    queue.enqueue(conn, "edit_project", {"project_id": project["id"]}, project_id=project["id"])
    if ch_mod.touches_vertical(changes):
        queue.enqueue(conn, "vertical_project", {"project_id": project["id"]}, project_id=project["id"])
        kinds.append("vertical_project")
    return kinds


@router.post("/requests/{request_id}/apply")
def apply_request(request_id: int, body: Scope, conn: sqlite3.Connection = Depends(db),
                  settings: Settings = Depends(settings_dep)) -> dict:
    req = conn.execute("SELECT * FROM edit_requests WHERE id = ?", (request_id,)).fetchone()
    if req is None:
        raise HTTPException(404, "Правку не знайдено")
    if req["status"] != "pending":
        raise HTTPException(409, "Правку вже застосовано")
    project = _project(conn, req["project_id"])
    changes = json.loads(req["changes"])
    rule_id = None
    if body.scope == "always":
        rule = {k: v for k, v in changes.items() if k not in ("summary", "rule_text", "exclude_at", "needs_reanalysis")}
        if "style_level_delta" in rule:
            style, level = style_store.resolve_for_project(conn, settings, project)
            style_store.approve(conn, style.id, max(1, min(3, level + int(rule.pop("style_level_delta")))))
        text = changes.get("rule_text") or req["text"]
        rule_id = conn.execute("INSERT INTO taste_rules(text, changes, request_id) VALUES (?, ?, ?)",
                               (text, json.dumps(rule, ensure_ascii=False), request_id)).lastrowid
        ch_mod.sync_taste_md(conn, settings.taste_path)
        # правило вже діє глобально; у проєкт пишемо лише те, що правило не покриває
        project_only = {k: v for k, v in changes.items() if k in ("exclude_at", "length", "track_start", "track_end")}
        project = _project(conn, req["project_id"])
        jobs = apply_here(conn, settings, project, project_only | {"summary": changes.get("summary", "")})
    else:
        jobs = apply_here(conn, settings, project, changes)
    conn.execute("UPDATE edit_requests SET status = ? WHERE id = ?", (body.scope, request_id))
    return {"id": request_id, "scope": body.scope, "rule_id": rule_id, "jobs": jobs}


@router.post("/requests/{request_id}/reject")
def reject_request(request_id: int, conn: sqlite3.Connection = Depends(db)) -> dict:
    if not conn.execute("UPDATE edit_requests SET status = 'rejected' WHERE id = ? AND status = 'pending'",
                        (request_id,)).rowcount:
        raise HTTPException(404, "Немає такої правки, що чекає рішення")
    return {"id": request_id, "status": "rejected"}


# ---------- смак ----------

def _split_taste(text: str) -> str:
    return re.sub(r"\s*<!-- правила:початок.*?<!-- правила:кінець -->\s*", "\n", text, flags=re.S).strip()


@router.get("/taste")
def get_taste(conn: sqlite3.Connection = Depends(db), settings: Settings = Depends(settings_dep)) -> dict:
    path = settings.taste_path
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    ex = settings.library_path / "taste"
    count = {k: len(list((ex / k).glob("*"))) if (ex / k).is_dir() else 0 for k in ("take", "skip")}
    rules = [{"id": r["id"], "text": r["text"], "active": bool(r["active"]), "changes": json.loads(r["changes"]),
              "created_at": r["created_at"]} for r in conn.execute("SELECT * FROM taste_rules ORDER BY id")]
    return {"instruction": _split_taste(text), "rules": rules, "examples": count, "first_run": not path.exists()}


@router.put("/taste")
def put_taste(body: Instruction, conn: sqlite3.Connection = Depends(db),
              settings: Settings = Depends(settings_dep)) -> dict:
    settings.taste_path.write_text(body.instruction.strip() + "\n", encoding="utf-8")
    ch_mod.sync_taste_md(conn, settings.taste_path)
    return {"ok": True}


@router.put("/taste/rules/{rule_id}")
def update_rule(rule_id: int, body: RuleUpdate, conn: sqlite3.Connection = Depends(db),
                settings: Settings = Depends(settings_dep)) -> dict:
    if body.text is not None:
        conn.execute("UPDATE taste_rules SET text = ? WHERE id = ?", (body.text, rule_id))
    if body.active is not None:
        conn.execute("UPDATE taste_rules SET active = ? WHERE id = ?", (int(body.active), rule_id))
    row = conn.execute("SELECT * FROM taste_rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Правило не знайдено")
    ch_mod.sync_taste_md(conn, settings.taste_path)
    return {"id": rule_id, "text": row["text"], "active": bool(row["active"])}


@router.delete("/taste/rules/{rule_id}")
def delete_rule(rule_id: int, conn: sqlite3.Connection = Depends(db),
                settings: Settings = Depends(settings_dep)) -> dict:
    if not conn.execute("DELETE FROM taste_rules WHERE id = ?", (rule_id,)).rowcount:
        raise HTTPException(404, "Правило не знайдено")
    ch_mod.sync_taste_md(conn, settings.taste_path)
    return {"deleted": rule_id}


@router.post("/taste/examples/{kind}")
async def add_example(kind: str, file: UploadFile = File(...), settings: Settings = Depends(settings_dep)) -> dict:
    if kind not in ("take", "skip"):
        raise HTTPException(404, "Лише take або skip")
    name = re.sub(r"[^\w.\-]", "_", file.filename or "example.jpg")
    if not name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
        raise HTTPException(415, "Приклад — картинка jpg/png/webp (кадр з відео)")
    d = settings.library_path / "taste" / kind
    d.mkdir(parents=True, exist_ok=True)
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "Завеликий файл")
    (d / name).write_bytes(data)
    return {"kind": kind, "name": name}


@router.delete("/taste/examples/{kind}/{name}")
def delete_example(kind: str, name: str, settings: Settings = Depends(settings_dep)) -> dict:
    p = settings.library_path / "taste" / kind / name
    if kind not in ("take", "skip") or "/" in name or "\\" in name or not p.exists():
        raise HTTPException(404, "Приклад не знайдено")
    p.unlink()
    return {"deleted": name}


# ---------- стиль проєкту: автовибір ----------

@router.post("/projects/{project_id}/style-suggestion")
def answer_suggestion(project_id: int, body: Suggestion, conn: sqlite3.Connection = Depends(db)) -> dict:
    project = _project(conn, project_id)
    stored = json.loads(project["settings"] or "{}")
    auto = stored.get("style_auto")
    if not auto:
        raise HTTPException(404, "Пропозиції стилю немає")
    if body.accept and not body.style_id:
        auto["confirmed"] = True
        stored["style_auto"] = auto
        conn.execute("UPDATE projects SET settings = ? WHERE id = ?", (json.dumps(stored, ensure_ascii=False), project_id))
        return {"style_id": auto["style_id"], "rerender": False}
    try:
        get_style(body.style_id or "")
    except StyleError as exc:
        raise HTTPException(404, str(exc)) from exc
    conn.execute("UPDATE projects SET style_id = ? WHERE id = ?", (body.style_id, project_id))
    queue.enqueue(conn, "edit_project", {"project_id": project_id}, project_id=project_id)
    queue.enqueue(conn, "vertical_project", {"project_id": project_id}, project_id=project_id)
    return {"style_id": body.style_id, "rerender": True}


# ---------- стилі словами ----------

@router.post("/styles/generate", status_code=202)
def generate_style(body: StyleText, conn: sqlite3.Connection = Depends(db),
                   settings: Settings = Depends(settings_dep)) -> dict:
    spec, source = None, "local"
    try:
        from analysis.llm import ask_json
        examples = [{"name": s.name, "look": s.look(2)} for s in list_styles(include_drafts=False)
                    if not s.data.get("user")][:4]
        spec = ask_json(conn, settings, style_gen.gemini_prompt(body.text, examples), style_gen.STYLE_SCHEMA,
                        "стиль словами")
        source = "gemini"
    except LLMUnavailable:
        spec = style_gen.local_spec(body.text)
    if spec is None:
        raise HTTPException(422, "Не зрозумів опис без Gemini. Спробуй слова: теплий, холодний, зерно, VHS, "
                                 "MiniDV, чорно-білий, вицвілий, контрастний, віньєтка.")
    style = style_gen.build_style(spec, style_gen.new_id())
    style_gen.save(settings.user_styles_path / "drafts", style)
    job = queue.enqueue(conn, "style_test", {"style_id": style["id"], "media_id": body.media_id,
                                             "start": body.start}, dedupe=False)
    return {"style": style, "source": source, "test_job": job}


@router.get("/styles/drafts")
def drafts() -> list[dict]:
    return [s.data for s in list_styles() if s.data.get("draft")]


@router.post("/styles/drafts/{style_id}/save")
def save_draft(style_id: str, settings: Settings = Depends(settings_dep)) -> dict:
    src = settings.user_styles_path / "drafts" / f"{style_id}.json"
    if not src.exists():
        raise HTTPException(404, "Чернетку не знайдено")
    dst = settings.user_styles_path / f"{style_id}.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dst)
    return {"saved": style_id}


@router.delete("/styles/drafts/{style_id}")
def delete_draft(style_id: str, settings: Settings = Depends(settings_dep)) -> dict:
    src = settings.user_styles_path / "drafts" / f"{style_id}.json"
    if not src.exists():
        raise HTTPException(404, "Чернетку не знайдено")
    src.unlink()
    return {"deleted": style_id}
