import { useEffect, useRef, useState } from "react";

export function Glass({ children, className = "", strong, ...rest }) {
  return <div className={`glass pad ${strong ? "strong" : ""} ${className}`} {...rest}>{children}</div>;
}

export function RecDot({ idle }) {
  return <span className={`rec-dot ${idle ? "idle" : ""}`} aria-hidden="true" />;
}

/** Фон: боке вогнів сцени; на екрані проєкту — сильно розмитий кадр з його відео. */
export function Backdrop({ cover }) {
  const lights = [
    ["#ff2a6d", "18%", "12%", 34], ["#2a4bff", "72%", "20%", 40], ["#ff8a2a", "60%", "70%", 28],
    ["#7a2aff", "12%", "68%", 30], ["#ffd23a", "85%", "52%", 16],
  ];
  return (
    <div className="backdrop" aria-hidden="true">
      {cover ? <div className="cover" style={{ backgroundImage: `url(${cover})` }} /> : lights.map(([c, x, y, s], i) => (
        <div key={i} className="bokeh" style={{ background: c, left: x, top: y, width: `${s}vmax`, height: `${s}vmax`,
          animationDelay: `${-i * 4}s` }} />
      ))}
    </div>
  );
}

/** VHS-екран, поки триває обробка. */
export function VhsScreen({ message = "Обробка…" }) {
  const [now, setNow] = useState(new Date());
  useEffect(() => { const t = setInterval(() => setNow(new Date()), 1000); return () => clearInterval(t); }, []);
  return (
    <div className="vhs" role="status" aria-label={message}>
      <div className="noise" /><div className="bars" /><div className="bars b2" />
      <div className="play">PLAY ▶</div>
      <div className="msg">{message}</div>
      <div className="clock">{now.toLocaleTimeString("uk-UA")}</div>
    </div>
  );
}

export function Bar({ value }) {
  return <div className="bar"><i style={{ width: `${Math.round((value || 0) * 100)}%` }} /></div>;
}

export function Toast({ text, onDone }) {
  useEffect(() => { if (text) { const t = setTimeout(onDone, 3200); return () => clearTimeout(t); } }, [text, onDone]);
  if (!text) return null;
  return <div className="glass strong toast" role="alert">{text}</div>;
}

/** Опитування API кожні `ms`, поки екран відкритий. */
export function usePoll(fn, ms = 3000, deps = []) {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try { const d = await fnRef.current(); if (alive) { setData(d); setError(null); } }
      catch (e) { if (alive) setError(e.message); }
    };
    tick();
    const t = setInterval(tick, ms);
    return () => { alive = false; clearInterval(t); };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return [data, error, setData];
}

/** Паралакс відблиску від нахилу телефона (з дозволом iOS); вимикається при «зменшенні руху». */
export function useTiltGlare() {
  const [needsPermission, setNeeds] = useState(false);
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    const DOE = window.DeviceOrientationEvent;
    if (!DOE) return;
    if (typeof DOE.requestPermission === "function") { setNeeds(true); return; }
    return listen();
  }, []);
  function listen() {
    const onTilt = (e) => {
      const x = Math.max(0, Math.min(100, 50 + (e.gamma || 0) * 1.6));
      const y = Math.max(0, Math.min(100, (e.beta || 0) * 0.9 - 10));
      document.documentElement.style.setProperty("--gx", `${x}%`);
      document.documentElement.style.setProperty("--gy", `${y}%`);
    };
    window.addEventListener("deviceorientation", onTilt);
    return () => window.removeEventListener("deviceorientation", onTilt);
  }
  async function enable() {
    try { if ((await window.DeviceOrientationEvent.requestPermission()) === "granted") { listen(); setNeeds(false); } }
    catch (_) { setNeeds(false); }
  }
  return { needsPermission, enable };
}

/* ---------- Питання «Так / Ні» перед діями, що запускають перемонтаж ---------- */
let askImpl = null;

/** Повертає Promise<boolean>. */
export function ask(text, yes = "Так", no = "Ні") {
  return askImpl ? askImpl(text, yes, no) : Promise.resolve(window.confirm(text));
}

export function AskHost() {
  const [q, setQ] = useState(null);
  useEffect(() => {
    askImpl = (text, yes, no) => new Promise((resolve) => setQ({ text, yes, no, resolve }));
    return () => { askImpl = null; };
  }, []);
  if (!q) return null;
  const done = (v) => { q.resolve(v); setQ(null); };
  return (
    <div className="ask-backdrop" onClick={() => done(false)} role="dialog" aria-modal="true">
      <div className="glass strong pad stack ask" onClick={(e) => e.stopPropagation()}>
        <div style={{ fontSize: 17 }}>{q.text}</div>
        <div className="row">
          <button className="pill primary grow" style={{ justifyContent: "center" }} onClick={() => done(true)}>{q.yes}</button>
          <button className="pill grow" style={{ justifyContent: "center" }} onClick={() => done(false)}>{q.no}</button>
        </div>
      </div>
    </div>
  );
}
