import { useEffect, useState } from "react";
import { api, money } from "../api.js";
import { Bar, Glass, Toast } from "../components/ui.jsx";

export default function Budget() {
  const [b, setB] = useState(null);
  const [form, setForm] = useState({});
  const [toast, setToast] = useState("");
  const [push, setPush] = useState(typeof Notification !== "undefined" ? Notification.permission : "unsupported");
  const load = async () => { const d = await api.get("/api/budget"); setB(d);
    setForm({ monthly_budget_usd: d.limit, confirm_threshold_usd: d.confirm_threshold, daily_request_limit: d.daily_limit }); };
  useEffect(() => { load(); }, []);
  if (!b) return null;

  const save = async () => {
    try { await api.put("/api/budget/settings", Object.fromEntries(Object.entries(form).map(([k, v]) => [k, Number(v)]))); setToast("Збережено"); load(); }
    catch (e) { setToast(e.message); }
  };

  const enablePush = async () => {
    try {
      const perm = await Notification.requestPermission();
      setPush(perm);
      if (perm !== "granted") return;
      const reg = await navigator.serviceWorker.ready;
      const { key } = await api.get("/api/push/key");
      const pad = "=".repeat((4 - (key.length % 4)) % 4);
      const raw = Uint8Array.from(atob((key + pad).replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: raw });
      await api.post("/api/push/subscribe", sub.toJSON());
      await api.post("/api/push/test");
      setToast("Сповіщення увімкнено");
    } catch (e) { setToast(`Push: ${e.message}`); }
  };

  return (
    <div className="stack">
      <Toast text={toast} onDone={() => setToast("")} />
      <h1 className="title-xl">Ліміт</h1>
      <Glass className="stack">
        <div className="label">Gemini цього місяця</div>
        <div className="mono" style={{ fontSize: 32, whiteSpace: "nowrap" }}><span className="tc">{money(b.spent)}</span> <span className="faint">/ {money(b.limit)}</span></div>
        <Bar value={b.limit ? (b.spent + b.reserved) / b.limit : 0} />
        <div className="mono muted" style={{ fontSize: 12 }}>
          резерв {money(b.reserved)} · доступно {money(b.available)} · запитів сьогодні {b.requests_today}/{b.daily_limit}
          {!b.api_key_set && " · ключ Gemini не задано"}
        </div>
      </Glass>

      <Glass className="stack">
        {[["monthly_budget_usd", "Місячний ліміт, $"], ["confirm_threshold_usd", "Питати підтвердження від, $"], ["daily_request_limit", "Запитів на день"]].map(([k, label]) => (
          <label key={k} className="row spread"><span>{label}</span>
            <input className="field mono" style={{ width: 120, textAlign: "right" }} type="number" step="0.5" min="0" value={form[k] ?? ""}
              onChange={(e) => setForm({ ...form, [k]: e.target.value })} /></label>
        ))}
        <button className="pill primary" onClick={save}>Зберегти</button>
      </Glass>

      <Glass className="stack">
        <div className="row spread"><div className="label">Сповіщення</div>
          <button className={`pill small ${push === "granted" ? "on" : ""}`} disabled={push === "unsupported"} onClick={enablePush}>
            {push === "granted" ? "увімкнені" : push === "unsupported" ? "недоступні" : "увімкнути"}</button></div>
        <div className="faint" style={{ fontSize: 13 }}>На iPhone: «Поділитися» → «На головний екран», відкрити звідти — тоді push працює (iOS 16.4+, HTTPS).</div>
      </Glass>

      <Glass className="stack">
        <div className="label">Історія витрат</div>
        {b.history.length === 0 && <div className="muted">Запитів ще не було.</div>}
        <table className="hist"><tbody>
          {b.history.map((h, i) => (
            <tr key={i}><td className="mono faint">{h.created_at.slice(5, 16).replace("T", " ")}</td>
              <td>{h.project || h.purpose}<div className="faint" style={{ fontSize: 11 }}>{h.purpose}{h.batch ? " · batch" : ""}</div></td>
              <td className="r tc">{money(h.cost_usd)}</td></tr>
          ))}
        </tbody></table>
      </Glass>

      <ClaudeAccess setToast={setToast} />
    </div>
  );
}

function ClaudeAccess({ setToast }) {
  const [m, setM] = useState(null);
  const load = () => api.get("/api/mcp").then(setM).catch(() => {});
  useEffect(() => { load(); }, []);
  if (!m) return null;
  const revoke = async (c) => {
    try { await api.del(`/api/mcp/clients/${encodeURIComponent(c.client_id)}`); setToast(`Доступ «${c.name}» відкликано`); load(); }
    catch (e) { setToast(e.message); }
  };
  return (
    <Glass className="stack">
      <div className="label">Claude з iPhone (MCP)</div>
      {!m.enabled && <div className="muted">Вимкнено. Увімкнення — у README: MCP_REMOTE і Tailscale Funnel.</div>}
      {m.url && <div className="mono" style={{ fontSize: 13, wordBreak: "break-all" }}>{m.url}</div>}
      {m.enabled && m.clients.length === 0 && <div className="muted">Ще ніхто не підключився.</div>}
      {m.clients.map((c) => (
        <div key={c.client_id} className="row">
          <div className="grow">{c.name}
            <div className="faint" style={{ fontSize: 12 }}>
              {c.last_used_at ? `востаннє ${c.last_used_at.slice(5, 16).replace("T", " ")}` : "ще не користувався"}
              {!c.active && " · токен прострочено"}
            </div>
          </div>
          <button className="pill small" onClick={() => revoke(c)}>Відкликати</button>
        </div>
      ))}
    </Glass>
  );
}
