import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Glass, Toast } from "../components/ui.jsx";

const STARTER = "# Смак\n\nЯ монтую концертні й тревел-відео в ло-фай естетиці MiniDV.\n\n## Беру\n- \n\n## Не беру\n- \n";

export default function Taste() {
  const [t, setT] = useState(null);
  const [text, setText] = useState("");
  const [toast, setToast] = useState("");
  const load = async () => { const d = await api.get("/api/taste"); setT(d); setText(d.instruction || STARTER); };
  useEffect(() => { load(); }, []);
  const act = async (fn, msg) => { try { await fn(); setToast(msg); await load(); } catch (e) { setToast(e.message); } };
  if (!t) return null;

  const upload = (kind) => (e) => Promise.all(Array.from(e.target.files).map((f) => {
    const fd = new FormData(); fd.append("file", f);
    return api.post(`/api/taste/examples/${kind}`, fd);
  })).then(() => { setToast("Приклади додано"); load(); }).catch((err) => setToast(err.message));

  return (
    <div className="stack">
      <Toast text={toast} onDone={() => setToast("")} />
      <h1 className="title-xl">Смак</h1>
      {t.first_run && <Glass className="suggest">Перший запуск: опиши, що береш і що ні, і додай 10–20 кадрів-прикладів кожного виду.</Glass>}

      <Glass className="stack">
        <div className="label">Інструкція для AI</div>
        <textarea className="field mono" style={{ minHeight: 220, fontSize: 13 }} value={text} onChange={(e) => setText(e.target.value)} />
        <button className="pill primary" onClick={() => act(() => api.put("/api/taste", { instruction: text }), "Збережено")}>Зберегти</button>
      </Glass>

      <Glass className="stack">
        <div className="label">Приклади-кадри</div>
        <div className="row wrap">
          <label className="pill">Беру · <span className="tc">{t.examples.take}</span>
            <input type="file" accept="image/*" multiple hidden onChange={upload("take")} /></label>
          <label className="pill">Не беру · <span className="tc">{t.examples.skip}</span>
            <input type="file" accept="image/*" multiple hidden onChange={upload("skip")} /></label>
        </div>
      </Glass>

      <Glass className="stack">
        <div className="label">Правила «назавжди»</div>
        {t.rules.length === 0 && <div className="muted">Поки немає. Вони з'являються, коли на правку відповідаєш «Назавжди».</div>}
        {t.rules.map((r) => <Rule key={r.id} r={r} act={act} />)}
        {t.rules.some((r) => r.origin === "mcp") && (
          <button className="pill small" onClick={() => act(async () => {
            for (const r of t.rules.filter((x) => x.origin === "mcp")) await api.del(`/api/taste/rules/${r.id}`);
          }, "Правила від Claude відкочено")}>↶ Відкотити всі правила від Claude</button>
        )}
      </Glass>
    </div>
  );
}

function Rule({ r, act }) {
  const [edit, setEdit] = useState(false);
  const [text, setText] = useState(r.text);
  return (
    <div className="row" style={{ opacity: r.active ? 1 : 0.45 }}>
      {edit ? <input className="field grow" value={text} onChange={(e) => setText(e.target.value)} />
        : <div className="grow">{r.text}{r.origin === "mcp" && <span className="badge run" style={{ marginLeft: 8, whiteSpace: "nowrap" }}>через Claude</span>}</div>}
      {edit ? <button className="pill small primary" onClick={() => act(() => api.put(`/api/taste/rules/${r.id}`, { text }), "Оновлено").then(() => setEdit(false))}>✓</button>
        : <button className="pill small" onClick={() => setEdit(true)}>✎</button>}
      <button className="pill small" onClick={() => act(() => api.put(`/api/taste/rules/${r.id}`, { active: !r.active }), r.active ? "Вимкнено" : "Увімкнено")}>{r.active ? "вимк" : "увімк"}</button>
      <button className="pill small" aria-label="Видалити правило" onClick={() => act(() => api.del(`/api/taste/rules/${r.id}`), "Видалено")}>✕</button>
    </div>
  );
}
