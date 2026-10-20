import { useEffect, useState } from "react";
import { NavLink, Route, Routes, useMatch } from "react-router-dom";
import { api, driveLinks } from "./api.js";
import { AskHost, Backdrop, useTiltGlare } from "./components/ui.jsx";
import { useUploads } from "./uploads.js";
import Queue from "./screens/Queue.jsx";
import Project from "./screens/Project.jsx";
import AddVideo from "./screens/AddVideo.jsx";
import Verticals from "./screens/Verticals.jsx";
import Styles from "./screens/Styles.jsx";
import Taste from "./screens/Taste.jsx";
import Budget from "./screens/Budget.jsx";

export default function App() {
  const tilt = useTiltGlare();
  const proj = useMatch("/project/:id/*");
  const [needPin, setNeedPin] = useState(false);
  const [offline, setOffline] = useState(false);
  useEffect(() => {
    const onAuth = () => setNeedPin(true);
    const onOff = (e) => setOffline(e.detail);
    window.addEventListener("am-auth", onAuth);
    window.addEventListener("am-offline", onOff);
    api.get("/api/auth/status").then((s) => setNeedPin(!s.authenticated)).catch(() => {});
    return () => { window.removeEventListener("am-auth", onAuth); window.removeEventListener("am-offline", onOff); };
  }, []);
  if (needPin) return <><Backdrop /><div className="scanlines" /><PinScreen onOk={() => { setNeedPin(false); window.location.reload(); }} /></>;
  return (
    <>
      <Backdrop cover={proj ? `/api/projects/${proj.params.id}/cover.jpg` : null} />
      <AskHost />
      <div className="scanlines" aria-hidden="true" />
      <main className="app">
        {offline && <Offline />}
        {tilt.needsPermission && (
          <button className="pill small" style={{ marginBottom: 12 }} onClick={tilt.enable}>Увімкнути відблиск від нахилу</button>
        )}
        <Routes>
          <Route path="/" element={<Queue />} />
          <Route path="/add" element={<AddVideo />} />
          <Route path="/project/:id" element={<Project />} />
          <Route path="/project/:id/vertical" element={<Verticals />} />
          <Route path="/styles" element={<Styles />} />
          <Route path="/taste" element={<Taste />} />
          <Route path="/budget" element={<Budget />} />
        </Routes>
      </main>
      <UploadPill />
      <nav className="glass strong tabbar" aria-label="Розділи">
        <NavLink to="/" end><span className="ico">▤</span>Черга</NavLink>
        <NavLink to="/styles"><span className="ico">◐</span>Стилі</NavLink>
        <NavLink to="/add" className="add" aria-label="Додати відео"><span className="ico">+</span></NavLink>
        <NavLink to="/taste"><span className="ico">♡</span>Смак</NavLink>
        <NavLink to="/budget"><span className="ico">$</span>Ліміт</NavLink>
      </nav>
    </>
  );
}

function PinScreen({ onOk }) {
  const [pin, setPin] = useState("");
  const [err, setErr] = useState("");
  const press = async (d) => {
    const next = d === "⌫" ? pin.slice(0, -1) : (pin + d).slice(0, 8);
    setPin(next); setErr("");
    if (d === "OK") {
      try { await api.post("/api/auth/login", { pin: pin }); onOk(); } catch (e) { setErr(e.message); setPin(""); }
    }
  };
  return (
    <main className="app stack" style={{ maxWidth: 380, paddingTop: "12vh" }}>
      <div className="brand"><span className="rec-dot" /> REC · АВТОМОНТАЖ</div>
      <h1 className="title-xl">PIN</h1>
      <div className="glass pad mono" style={{ fontSize: 34, letterSpacing: "0.4em", textAlign: "center", minHeight: 70 }}>
        {"●".repeat(pin.length) || <span className="faint">····</span>}
      </div>
      {err && <div className="badge err" style={{ justifySelf: "start" }}>{err}</div>}
      <div className="grid3" style={{ gap: 12 }}>
        {["1", "2", "3", "4", "5", "6", "7", "8", "9", "⌫", "0", "OK"].map((d) => (
          <button key={d} className={`pill ${d === "OK" ? "rec" : ""}`} style={{ justifyContent: "center", padding: "18px 0", fontSize: 22 }}
            disabled={d === "OK" && pin.length < 4} onClick={() => press(d === "OK" ? "OK" : d)}>{d}</button>
        ))}
      </div>
    </main>
  );
}

function Offline() {
  const links = Object.values(driveLinks.get()).filter((l) => l.url);
  return (
    <div className="glass suggest pad stack" style={{ marginBottom: 14 }}>
      <div><span className="rec-dot idle" /> Ноутбук зараз недоступний. Відео можна закинути в Google Drive — програма забере їх, коли ноутбук увімкнеться.</div>
      <div className="row wrap">
        {links.map((l) => <a key={l.url} className="pill small" href={l.url} target="_blank" rel="noreferrer">{l.name} · Drive</a>)}
        {links.length === 0 && <a className="pill small" href="https://drive.google.com/drive/my-drive" target="_blank" rel="noreferrer">Відкрити Google Drive</a>}
      </div>
    </div>
  );
}

/** Над нижнім меню: скільки файлів вантажиться — видно з будь-якої вкладки. */
function UploadPill() {
  const u = useUploads();
  if (u.total === 0 || (u.active === 0 && u.failed === 0)) return null;
  return (
    <NavLink to="/add" className="glass strong upload-pill">
      {u.active > 0 ? `⬆ Завантаження ${u.done}/${u.total}` : `⚠ Обрив: ${u.failed} — натисни, щоб продовжити`}
    </NavLink>
  );
}
