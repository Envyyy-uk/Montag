import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import * as tus from "tus-js-client";
import { api, driveLinks, measureUpload } from "../api.js";
import { Bar, Glass, Toast } from "../components/ui.jsx";

export default function AddVideo() {
  const nav = useNavigate();
  const [projects, setProjects] = useState([]);
  const [pid, setPid] = useState("");
  const [name, setName] = useState("");
  const [route, setRoute] = useState(null);
  const [files, setFiles] = useState([]);   // {file, progress, state}
  const [toast, setToast] = useState("");
  const input = useRef(null);

  useEffect(() => {
    api.get("/api/projects").then((p) => { setProjects(p); if (p[0]) setPid(String(p[0].id)); });
  }, []);
  // шлях вибирається сам: швидкість до ноута + тип з'єднання Tailscale
  useEffect(() => {
    if (!pid) return;
    let alive = true;
    setRoute(null);
    (async () => {
      let mbps = null;
      try { mbps = await measureUpload(); } catch (_) {}
      const q = new URLSearchParams({ project_id: pid, ...(mbps ? { mbps: mbps.toFixed(1) } : {}) });
      const r = await api.get(`/api/upload-route?${q}`).catch(() => null);
      if (!alive || !r) return;
      setRoute({ ...r, mbps });
      const p = projects.find((x) => String(x.id) === pid);
      if (r.drive_url && p) driveLinks.set(pid, p.name, r.drive_url);
    })();
    return () => { alive = false; };
  }, [pid, projects]);

  const create = async () => {
    try {
      const p = await api.post("/api/projects", { name: name.trim() });
      setProjects((x) => [p, ...x]); setPid(String(p.id)); setName("");
      setToast(`Проєкт «${p.name}» створено`);
    } catch (e) { setToast(e.message); }
  };

  const upload = (list) => {
    const items = Array.from(list).map((file) => ({ file, progress: 0, state: "wait" }));
    setFiles((f) => [...f, ...items]);
    items.forEach((it) => {
      const up = new tus.Upload(it.file, {
        endpoint: "/api/uploads",
        retryDelays: [0, 2000, 5000, 10000, 30000],   // обрив зв'язку — докачуємо
        chunkSize: 16 * 1024 * 1024,
        metadata: { filename: it.file.name, project_id: pid },
        onProgress: (sent, total) => update(it, { progress: sent / total, state: "up" }),
        onSuccess: () => update(it, { progress: 1, state: "done" }),
        onError: (e) => update(it, { state: "error", error: String(e.message || e).slice(0, 120) }),
      });
      up.findPreviousUploads().then((prev) => { if (prev.length) up.resumeFromPreviousUpload(prev[0]); up.start(); });
    });
  };
  const update = (it, patch) => setFiles((f) => f.map((x) => (x.file === it.file ? { ...x, ...patch } : x)));
  const allDone = files.length > 0 && files.every((f) => f.state === "done");

  return (
    <div className="stack">
      <Toast text={toast} onDone={() => setToast("")} />
      <h1 className="title-xl">Додати відео</h1>

      <Glass className="stack">
        <div className="label">Новий проєкт</div>
        <div className="row">
          <input className="field grow" placeholder="Концерт Brixton" value={name} onChange={(e) => setName(e.target.value)} />
          <button className="pill primary" disabled={!name.trim()} onClick={create}>Створити</button>
        </div>
        <div className="label">Або в наявний</div>
        <select className="field" value={pid} onChange={(e) => setPid(e.target.value)}>
          {projects.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
      </Glass>

      <Glass className="stack">
        <div className="row">
          <span className="rec-dot idle" style={{ background: !route ? "var(--ink-3)" : route.route === "direct" ? "var(--ok)" : "var(--tc)", opacity: 1 }} />
          <span className="grow">{route ? route.label : "Перевіряю з'єднання з ноутбуком…"}</span>
          {route?.mbps && <span className="mono faint" style={{ fontSize: 12 }}>{route.mbps.toFixed(0)} Мбіт/с</span>}
        </div>
        {route && <div className="faint" style={{ fontSize: 13 }}>{route.reason}</div>}
        {route?.route === "drive" && (
          <div className="stack">
            <div className="muted" style={{ fontSize: 14 }}>Завантаж відео в папку проєкту в Google Drive — ноутбук забере їх сам, перевірить і видалить з Drive.</div>
            {route.drive_url
              ? <a className="pill primary" href={route.drive_url} target="_blank" rel="noreferrer">Відкрити папку в Google Drive</a>
              : <span className="faint">Папка в Drive ще створюється…</span>}
          </div>
        )}
      </Glass>

      {route?.route !== "drive" && <Glass className="stack">
        <div className="drop-zone" onClick={() => input.current?.click()} role="button" tabIndex={0}>
          <div style={{ fontSize: 28 }}>＋</div>
          <div>Вибрати відео або трек</div>
          <div className="faint" style={{ fontSize: 13 }}>Відео з iPhone, екшн-камери, окулярів; трек — mp3/wav/m4a</div>
        </div>
        <input ref={input} type="file" multiple accept="video/*,audio/*,.mp3,.wav,.m4a" hidden disabled={!pid}
          onChange={(e) => { upload(e.target.files); e.target.value = ""; }} />
        {files.map((f, i) => (
          <div key={i} className="file-row">
            <div className="grow" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{f.file.name}</div>
            <span className={`badge ${f.state === "done" ? "done" : f.state === "error" ? "err" : "run"}`}>
              {f.state === "done" ? "готово" : f.state === "error" ? "помилка" : `${Math.round(f.progress * 100)}%`}
            </span>
            <div style={{ gridColumn: "1 / -1" }}>{f.state === "error" ? <span className="faint">{f.error}</span> : <Bar value={f.progress} />}</div>
          </div>
        ))}
      </Glass>}

      {allDone && (
        <button className="pill rec" onClick={async () => { await api.post(`/api/projects/${pid}/edit`); nav(`/project/${pid}`); }}>
          ● Монтувати
        </button>
      )}
    </div>
  );
}
