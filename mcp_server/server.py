"""Інструменти MCP поверх API програми.

Усе, що робить PWA, Claude теж може зробити. Дві дії йдуть у два кроки з одноразовим кодом:
витрати Gemini (не більше ліміту за раз) і видалення рендерів (оригінали не видаляються ніколи).
"""

from __future__ import annotations

from typing import Literal

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_server.api import Api
from mcp_server.codes import CodeError, Codes

INSTRUCTIONS = """\
Автомонтаж: програма на ноутбуці, яка з сирих відео концертів і подорожей робить кліп під трек,
вертикальні ролики 9:16 і проєкт для DaVinci Resolve. Відповідай користувачу українською.

Як працювати:
- Почни з `status`, щоб побачити чергу, проєкти, що чекають рішення, і бюджет Gemini.
- Правки словами: `request_change` → покажи людині, що зрозуміла програма, і спитай
  «тільки тут чи назавжди?» → `apply_change` з її відповіддю. Сам назавжди не обирай.
- Подивитися результат: `show_clip` / `show_reels` повертають аркуш кадрів.
- Витрати Gemini: `prepare_spend` дає прогноз і код. Обов'язково назви людині суму й дочекайся
  явної згоди, лише потім `confirm_spend` з кодом. Те саме з видаленням: `prepare_delete` → згода → `delete_renders`.
- Оригінальні відео програма не змінює й не видаляє — такої дії немає.
"""


def _money(v: float | None) -> str:
    return f"${(v or 0):.2f}"


def build_server(api: Api, spend_limit_usd: float = 2.0, codes: Codes | None = None, **server_kw) -> MCPServer:
    mcp = MCPServer("Автомонтаж", instructions=INSTRUCTIONS, **server_kw)
    codes = codes or Codes()

    async def _latest(project_id: int, kind: str) -> dict:
        ov = await api.get(f"/api/projects/{project_id}/overview")
        e = ov.get(kind)
        if not e:
            raise ToolError("Кліпу ще немає — спершу `start_edit`" if kind == "clip"
                            else "Вертикалей ще немає — спершу `make_verticals`")
        return e

    # ---------- огляд ----------

    @mcp.tool()
    async def status() -> dict:
        """Черга завдань, проєкти, що чекають рішення, і стан бюджету Gemini — з цього варто починати."""
        queue = await api.get("/api/queue")
        budget = await api.get("/api/budget")
        confirms = await api.get("/api/confirmations")
        active = [j for j in queue if j["status"] in ("queued", "running", "awaiting_confirm", "failed")][:30]
        return {
            "черга": [{"job_id": j["id"], "що": j["kind"], "статус": j["status"], "проєкт": j["project"],
                       "прогрес": round(j["progress"] or 0, 2), **({"помилка": j["error"]} if j.get("error") else {})}
                      for j in active],
            "чекають_підтвердження_витрат": confirms,
            "бюджет": {"витрачено": _money(budget.get("spent")), "ліміт": _money(budget.get("limit")),
                       "лишилось": _money(budget.get("available")), "ключ_gemini": budget.get("api_key_set")},
        }

    @mcp.tool()
    async def list_projects() -> list[dict]:
        """Усі проєкти: id, назва, статус, кількість відео й треків."""
        return [{k: p[k] for k in ("id", "name", "status", "style_id", "videos", "tracks", "created_at")}
                for p in await api.get("/api/projects")]

    @mcp.tool()
    async def create_project(name: str) -> dict:
        """Створює новий проєкт (відео потім додають з телефона, через Google Drive або папку inbox)."""
        p = await api.post("/api/projects", {"name": name})
        return {"id": p["id"], "name": p["name"]}

    @mcp.tool()
    async def project(project_id: int) -> dict:
        """Подробиці проєкту: стиль, параметри кліпу й вертикалей, останні версії, завдання, правки."""
        ov = await api.get(f"/api/projects/{project_id}/overview")
        ov["правки"] = (await api.get(f"/api/projects/{project_id}/requests"))[:10]
        ov["версії"] = [{k: e[k] for k in ("id", "kind", "version", "created_at")}
                        for e in await api.get(f"/api/projects/{project_id}/edits")]
        return ov

    # ---------- результат ----------

    @mcp.tool(structured_output=False)
    async def show_clip(project_id: int, edit_id: int | None = None) -> list:
        """Аркуш кадрів готового кліпу (12 кадрів рівномірно) і коротка інформація про версію."""
        e = {"id": edit_id} if edit_id else await _latest(project_id, "clip")
        sheet = await api.image(f"/api/edits/{e['id']}/sheet.jpg")
        s = e.get("summary") or {}
        text = (f"Кліп v{e.get('version', '?')} (edit_id {e['id']}): {s.get('duration', 0):.0f} с, "
                f"{s.get('cuts', '?')} склейок, вставок {s.get('inserts', 0)}, стиль {s.get('style', '?')} рівень {s.get('level', '?')}. "
                f"Файл: GET /api/edits/{e['id']}/video") if s else f"edit_id {e['id']}"
        if sheet is None:
            cover = await api.image(f"/api/projects/{project_id}/cover.jpg")
            return [text + " — готового відео ще немає", *([Image(data=cover, format="jpeg")] if cover else [])]
        return [text, Image(data=sheet, format="jpeg")]

    @mcp.tool(structured_output=False)
    async def show_reels(project_id: int) -> list:
        """Аркуші кадрів кожного вертикального ролика останньої версії."""
        e = await _latest(project_id, "vertical")
        reels = (e.get("summary") or {}).get("reels", [])
        out: list = [f"Вертикалі v{e['version']} (edit_id {e['id']}): {len(reels)} ролик(и)"]
        for n, r in enumerate(reels, 1):
            out.append(f"Ролик {n}: {r.get('duration', 0):.0f} с, звук {r.get('audio', '?')}, {r.get('path', '')}")
            img = await api.image(f"/api/edits/{e['id']}/sheet.jpg", params={"n": n})
            if img:
                out.append(Image(data=img, format="jpeg"))
        return out

    # ---------- дії ----------

    @mcp.tool()
    async def start_edit(project_id: int) -> dict:
        """Запускає повний монтаж: аналіз, AI-оцінка (може потребувати підтвердження витрат), кліп, DaVinci."""
        return await api.post(f"/api/projects/{project_id}/edit")

    @mcp.tool()
    async def reedit(project_id: int) -> dict:
        """Перемонтаж на вже готових оцінках — без нових запитів у Gemini."""
        return await api.post(f"/api/projects/{project_id}/reedit")

    @mcp.tool()
    async def make_verticals(project_id: int, count: int | None = None, length: float | None = None,
                             audio: Literal["live", "track", "none"] | None = None) -> dict:
        """Вертикальні ролики 9:16. count 1–10, length 15–25 с, audio: live (живий звук), track або none."""
        body = {k: v for k, v in {"count": count, "length": length, "audio": audio}.items() if v is not None}
        if body:
            await api.put(f"/api/projects/{project_id}/vertical-settings", body)
        return await api.post(f"/api/projects/{project_id}/verticals")

    @mcp.tool()
    async def export_davinci(project_id: int, edit_id: int | None = None) -> dict:
        """Проєкт DaVinci Resolve (FCPXML) для кліпу; повертає, де лежить папка, або запускає експорт."""
        eid = edit_id or (await _latest(project_id, "clip"))["id"]
        st = await api.get(f"/api/edits/{eid}/davinci")
        if st["ready"]:
            return {"готово": True, "папка": st["folder"], "fcpxml": st["fcpxml"]}
        return {"готово": False, **await api.post(f"/api/edits/{eid}/davinci")}

    # ---------- правки словами ----------

    @mcp.tool()
    async def request_change(project_id: int, text: str) -> dict:
        """Правка словами («більше натовпу», «швидше на приспіві», «без вставок»). Повертає, що зрозуміла
        програма. Потім спитай людину «тільки тут чи назавжди?» і виклич apply_change."""
        r = await api.post(f"/api/projects/{project_id}/requests", {"text": text})
        return {"request_id": r["id"], "зрозуміла_так": r["summary"], "зміни": r["changes"], "питання": r["question"]}

    @mcp.tool()
    async def apply_change(request_id: int, scope: Literal["here", "always"]) -> dict:
        """Застосовує правку: here — лише цей проєкт; always — ще й правило смаку назавжди (лише якщо людина так сказала)."""
        return await api.post(f"/api/requests/{request_id}/apply", {"scope": scope})

    @mcp.tool()
    async def reject_change(request_id: int) -> dict:
        """Відкидає правку, нічого не змінюючи."""
        return await api.post(f"/api/requests/{request_id}/reject")

    # ---------- стилі й смак ----------

    @mcp.tool()
    async def list_styles() -> list[dict]:
        """Стилі (вигляд кліпу) з описом і схваленим рівнем інтенсивності 1–3."""
        return [{k: s[k] for k in ("id", "name", "description", "default_level", "approved_level")}
                for s in await api.get("/api/styles")]

    @mcp.tool()
    async def set_style(project_id: int, style_id: str, level: int | None = None, rerender: bool = True) -> dict:
        """Міняє стиль проєкту (і рівень 1–3); з rerender — одразу перемонтовує без нових запитів у Gemini."""
        await api.put(f"/api/projects/{project_id}/style", {"style_id": style_id})
        if level is not None:
            await api.put(f"/api/projects/{project_id}/clip-settings", {"style_level": level})
        out = {"style_id": style_id, "level": level}
        if rerender:
            out["job"] = await api.post(f"/api/projects/{project_id}/reedit")
        return out

    @mcp.tool()
    async def taste() -> dict:
        """Смак: загальна інструкція і правила «назавжди» (origin=mcp — додані через Claude)."""
        t = await api.get("/api/taste")
        return {"інструкція": t["instruction"], "правила": t["rules"], "приклади": t["examples"]}

    @mcp.tool()
    async def remove_taste_rule(rule_id: int) -> dict:
        """Видаляє правило смаку «назавжди»."""
        return await api.delete(f"/api/taste/rules/{rule_id}")

    # ---------- витрати Gemini: два кроки ----------

    @mcp.tool()
    async def prepare_spend(job_id: int) -> str:
        """Крок 1 з 2: прогноз вартості завдання, що чекає підтвердження, і одноразовий код."""
        item = next((c for c in await api.get("/api/confirmations") if c["job_id"] == job_id), None)
        if item is None:
            raise ToolError("Це завдання не чекає підтвердження — подивись `status`")
        cost = float(item.get("forecast_usd") or 0)
        if cost > spend_limit_usd:
            raise ToolError(f"Прогноз {_money(cost)} більший за ліміт {_money(spend_limit_usd)} для підтвердження через "
                            "Claude — підтвердь у застосунку на телефоні (екран «Ліміт»)")
        code = codes.issue("spend", job_id, cost)
        return (f"Проєкт «{item.get('project')}»: прогноз {_money(cost)}, запитів {item.get('requests', '?')}. "
                f"Скажи людині цю суму і дочекайся явної згоди. Після згоди: confirm_spend(job_id={job_id}, code=\"{code}\"). "
                "Код діє 10 хв.")

    @mcp.tool()
    async def confirm_spend(job_id: int, code: str = "", approved: bool = True) -> dict:
        """Крок 2 з 2: підтверджує витрату кодом з prepare_spend (лише після згоди людини). approved=false — відмова, без коду."""
        if approved:
            try:
                p = codes.take(code, "spend", job_id)
            except CodeError as exc:
                raise ToolError(str(exc)) from exc
            item = next((c for c in await api.get("/api/confirmations") if c["job_id"] == job_id), None)
            if item and float(item.get("forecast_usd") or 0) > p.amount + 1e-6:
                raise ToolError("Прогноз змінився — почни з prepare_spend ще раз")
        return await api.post(f"/api/jobs/{job_id}/confirm", {"approved": approved})

    # ---------- видалення рендерів: два кроки ----------

    @mcp.tool()
    async def prepare_delete(edit_id: int) -> str:
        """Крок 1 з 2: що буде видалено (лише рендери однієї версії; оригінали лишаються) і одноразовий код."""
        pl = await api.get(f"/api/edits/{edit_id}/delete-plan")
        code = codes.issue("delete", edit_id)
        files = "\n".join(f"- {p}" for p in pl["paths"]) or "- (файлів уже немає, лише запис)"
        return (f"Буде видалено {pl['label']} (~{pl['mb']} МБ):\n{files}\nОригінальні відео лишаються. "
                f"Покажи це людині й дочекайся згоди. Після згоди: delete_renders(edit_id={edit_id}, code=\"{code}\").")

    @mcp.tool()
    async def delete_renders(edit_id: int, code: str) -> dict:
        """Крок 2 з 2: видаляє рендери версії кодом з prepare_delete (лише після згоди людини)."""
        try:
            codes.take(code, "delete", edit_id)
        except CodeError as exc:
            raise ToolError(str(exc)) from exc
        return await api.delete(f"/api/edits/{edit_id}")

    return mcp
