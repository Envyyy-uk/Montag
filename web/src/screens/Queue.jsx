import { useState } from "react";
import { Link } from "react-router-dom";
import { api, fmtDate, JOB_LABELS, money, PROJECT_STATUS } from "../api.js";
import { ask, Bar, Glass, RecDot, usePoll } from "../components/ui.jsx";

const STATUS_CLASS = { running: "run", queued: "", awaiting_confirm: "wait", failed: "err", done: "done" };
const STATUS_TEXT = { running: "іде", queued: "чекає", awaiting_confirm: "підтвердити", failed: "помилка", done: "готово" };

export default function Queue() {
  const [jobs] = usePoll(() => api.get("/api/queue"), 2500);
  const [projects] = usePoll(() => api.get("/api/projects"), 5000);
  const [budget] = usePoll(() => api.get("/api/budget"), 10000);
  const [gone, setGone] = useState([]);
  const active = (jobs || []).filter((j) => j.status !== "done" && !gone.includes(j.id));
  const busy = active.some((j) => j.status === "running");

  return (
    <div className="stack">
      <header className="topbar">
        <div className="brand"><RecDot idle={!busy} /> {busy ? "REC" : "STBY"}</div>
        <Link to="/budget" className="counter glass" style={{ padding: "8px 14px", borderRadius: 99 }}>
          <b>{money(budget?.spent)}</b> / {money(budget?.limit)}
        </Link>
      </header>
      <h1 className="title-xl">Черга</h1>

      <Glass className="stack">
        {active.length === 0 && <div className="muted">Усе зроблено. Додай відео — і поїхали.</div>}
        {active.map((j) => (
          <div key={j.id} className="job">
            <div className="row spread">
              <div className="grow">
                <div style={{ fontWeight: 500 }}>{JOB_LABELS[j.kind] || j.kind}</div>
                <div className="muted" style={{ fontSize: 13 }}>{j.project || "—"}</div>
              </div>
              <span className={`badge ${STATUS_CLASS[j.status] || ""}`}>{STATUS_TEXT[j.status] || j.status}</span>
              <button className="pill small" aria-label="Прибрати з черги" style={{ marginLeft: 8 }}
                onClick={async () => {
                  const q = j.status === "running" ? "Зупинити це завдання?" : j.status === "failed" ? "Сховати помилку?" : "Скасувати це завдання?";
                  if (!(await ask(q))) return;
                  try { await api.del(`/api/jobs/${j.id}`); setGone((g) => [...g, j.id]); } catch (e) { alert(e.message); }
                }}>✕</button>
            </div>
            {j.status === "running" && <Bar value={j.progress} />}
            {j.status === "failed" && <div className="faint mono" style={{ fontSize: 11 }}>{(j.error || "").split("\n")[0]}</div>}
          </div>
        ))}
      </Glass>

      <div className="label" style={{ marginTop: 8 }}>Проєкти</div>
      {(projects || []).map((p) => (
        <Link key={p.id} to={`/project/${p.id}`}>
          <Glass className="row spread">
            <div className="grow">
              <div className="title">{p.name}</div>
              <div className="mono faint" style={{ fontSize: 12 }}>{fmtDate(p.created_at)} · {p.videos} відео{p.tracks ? " · трек" : ""}</div>
            </div>
            <span className={`badge ${p.status === "edited" ? "done" : p.status === "awaiting_confirm" ? "wait" : ""}`}>
              {PROJECT_STATUS[p.status] || p.status}
            </span>
          </Glass>
        </Link>
      ))}
      {projects && projects.length === 0 && <Glass className="muted">Проєктів ще немає.</Glass>}
    </div>
  );
}
