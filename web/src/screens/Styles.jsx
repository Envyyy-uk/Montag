import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Glass, Toast, usePoll } from "../components/ui.jsx";

export default function Styles() {
  const [styles, , setStyles] = usePoll(() => api.get("/api/styles"), 5000);
  const [drafts, , setDrafts] = usePoll(() => api.get("/api/styles/drafts"), 5000);
  const [events, setEvents] = useState(null);
  useEffect(() => { api.get("/api/styles/events").then(setEvents).catch(() => {}); }, []);
  const [open, setOpen] = useState(null);
  const [text, setText] = useState("");
  const [toast, setToast] = useState("");
  const reload = async () => { setStyles(await api.get("/api/styles")); setDrafts(await api.get("/api/styles/drafts")); };
  const act = async (fn, msg) => { try { await fn(); setToast(msg); await reload(); } catch (e) { setToast(e.message); } };

  return (
    <div className="stack">
      <Toast text={toast} onDone={() => setToast("")} />
      <h1 className="title-xl">Стилі</h1>

      <Glass className="stack">
        <div className="label">Новий стиль словами</div>
        <div className="row">
          <input className="field grow" placeholder="холодний, контрастний, з зерном і датою" value={text} onChange={(e) => setText(e.target.value)} />
          <button className="round" aria-label="Створити стиль" disabled={text.trim().length < 3}
            onClick={() => act(async () => { await api.post("/api/styles/generate", { text }); setText(""); }, "Чернетка готова — рендерю тест")}>↑</button>
        </div>
      </Glass>

      {events && styles && (
        <Glass className="stack">
          <div className="label">Стиль для типу події</div>
          <div className="faint" style={{ fontSize: 13 }}>AI визначає, що на відео, і ставить цей стиль сам.</div>
          {events.map((e) => (
            <div key={e.event} className="row">
              <div className="grow">{e.event}</div>
              <select className="field" style={{ maxWidth: 200 }} value={e.mine ? e.style_id : ""}
                onChange={(ev) => act(async () => setEvents(await api.put("/api/styles/events",
                  { event: e.event, style_id: ev.target.value || null })), "Збережено")}>
                <option value="">авто ({(styles.find((x) => x.id === e.builtin) || {}).name || e.builtin})</option>
                {styles.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
              </select>
            </div>
          ))}
        </Glass>
      )}

      {(drafts || []).map((d) => (
        <StyleCard key={d.id} s={{ ...d, tests: { 1: true, 2: true, 3: true } }} draft open={open === d.id} onToggle={() => setOpen(open === d.id ? null : d.id)}
          onSave={() => act(() => api.post(`/api/styles/drafts/${d.id}/save`), "Стиль збережено")}
          onDelete={() => act(() => api.del(`/api/styles/drafts/${d.id}`), "Чернетку видалено")} />
      ))}

      {(styles || []).map((s) => (
        <StyleCard key={s.id} s={s} open={open === s.id} onToggle={() => setOpen(open === s.id ? null : s.id)}
          onTest={() => act(() => api.post(`/api/styles/${s.id}/test`, {}), "Рендерю 3 тестові шматки по 5 с")}
          onApprove={(level) => act(() => api.post(`/api/styles/${s.id}/approve`, { level }), `Рівень ${level} затверджено`)} />
      ))}
    </div>
  );
}

function StyleCard({ s, open, onToggle, onTest, onApprove, draft, onSave, onDelete }) {
  return (
    <Glass className="stack">
      <div className="row spread" onClick={onToggle} style={{ cursor: "pointer" }}>
        <div className="grow">
          <div className="title">{s.name}{draft && <span className="badge wait" style={{ marginLeft: 8 }}>чернетка</span>}</div>
          <div className="muted" style={{ fontSize: 14 }}>{s.description}</div>
        </div>
        {s.approved_level && <span className="badge done">рівень {s.approved_level}</span>}
      </div>
      {open && (
        <>
          <div className="grid3">
            {[1, 2, 3].map((l) => (
              <div key={l} className="stack" style={{ gap: 6 }}>
                {s.tests?.[l] ? <video className="thumb" src={`/api/styles/${s.id}/test/${l}`} muted loop autoPlay playsInline />
                  : <div className="thumb" />}
                {onApprove && <button className={`pill small ${s.approved_level === l ? "on" : ""}`} onClick={() => onApprove(l)}>Рівень {l}</button>}
              </div>
            ))}
          </div>
          <div className="row wrap">
            {onTest && <button className="pill small" onClick={onTest}>Тестові рендери</button>}
            {draft && <><button className="pill primary small" onClick={onSave}>Зберегти</button>
              <button className="pill small" onClick={onDelete}>Видалити</button></>}
          </div>
        </>
      )}
    </Glass>
  );
}
