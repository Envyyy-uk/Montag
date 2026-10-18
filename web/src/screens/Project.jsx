import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, fmtDate, fmtTime, JOB_LABELS, money } from "../api.js";
import { Glass, Toast, usePoll, VhsScreen } from "../components/ui.jsx";

export default function Project() {
  const { id } = useParams();
  const [ov, , setOv] = usePoll(() => api.get(`/api/projects/${id}/overview`), 3000, [id]);
  const [styles] = usePoll(() => api.get("/api/styles"), 60000);
  const [toast, setToast] = useState("");
  if (!ov) return <VhsScreen message="Завантаження проєкту…" />;

  const busy = ov.jobs.filter((j) => j.status !== "awaiting_confirm");
  const confirms = ov.jobs.filter((j) => j.status === "awaiting_confirm");
  const style = (styles || []).find((s) => s.id === ov.style.id);
  const refresh = async () => setOv(await api.get(`/api/projects/${id}/overview`));
  const act = async (fn, msg) => {
    try { await fn(); if (msg) setToast(msg); await refresh(); } catch (e) { setToast(e.message); }
  };

  return (
    <div className="stack">
      <Toast text={toast} onDone={() => setToast("")} />
      <header>
        <Link to="/" className="faint">‹ Черга</Link>
        <h1 className="title-xl">{ov.name}</h1>
        <div className="mono muted" style={{ fontSize: 13, marginTop: 8 }}>
          <span className="tc">{fmtDate(ov.shot_at || ov.created_at)}</span> · {ov.videos} відео · {ov.style.name} · рівень {ov.style.level}
        </div>
      </header>

      {ov.style_auto && !ov.style_auto.confirmed && (
        <Glass className="suggest stack">
          <div>{ov.style_auto.reason}</div>
          <div className="row wrap">
            <button className="pill primary" onClick={() => act(() => api.post(`/api/projects/${id}/style-suggestion`, { accept: true }), "Стиль підтверджено")}>Ок</button>
            {(styles || []).filter((s) => s.id !== ov.style_auto.style_id).map((s) => (
              <button key={s.id} className="pill small" onClick={() => act(() => api.post(`/api/projects/${id}/style-suggestion`, { style_id: s.id }), `Стиль: ${s.name}`)}>{s.name}</button>
            ))}
          </div>
        </Glass>
      )}

      {confirms.map((j) => (
        <Glass key={j.id} className="suggest stack">
          <div>Аналіз у Gemini коштуватиме ≈ <span className="tc">{money(j.confirm?.forecast_usd)}</span>
            {" "}({j.confirm?.segments} шматків). Підтвердити?</div>
          <div className="row">
            <button className="pill primary" onClick={() => act(() => api.post(`/api/jobs/${j.id}/confirm`, { approved: true }), "Підтверджено")}>Так</button>
            <button className="pill" onClick={() => act(() => api.post(`/api/jobs/${j.id}/confirm`, { approved: false }), "Без Gemini — лише локальні метрики")}>Без Gemini</button>
          </div>
        </Glass>
      ))}

      {busy.length > 0 || !ov.clip ? (
        <VhsScreen message={busy.length ? `${JOB_LABELS[busy[0].kind] || busy[0].kind} · ${Math.round(busy[0].progress * 100)}%` : "Чекаю на «Монтувати»"} />
      ) : (
        <video className="player" src={`/api/edits/${ov.clip.id}/preview`} controls playsInline preload="metadata" key={ov.clip.id} />
      )}

      {ov.clip == null && busy.length === 0 && (
        <button className="pill rec" onClick={() => act(() => api.post(`/api/projects/${id}/edit`), "Монтую")}>● Монтувати</button>
      )}

      {ov.tracks > 0 && <EnergyStrip id={id} onSaved={(m) => { setToast(m); refresh(); }} />}

      {style && (
        <Glass className="stack">
          <div className="row spread"><div className="label">Переходи</div>
            <button className={`pill small ${ov.clip_params.transitions ? "on" : ""}`}
              onClick={() => act(() => api.put(`/api/projects/${id}/clip-settings`, { transitions: !ov.clip_params.transitions }))}>
              {ov.clip_params.transitions ? "увімкнені" : "лише різкі"}
            </button>
          </div>
          <div className="row wrap">
            {style.transitions.map((t) => {
              const off = ov.clip_params.disabled_transitions.includes(t.id);
              return (
                <button key={t.id} className={`pill small ${off ? "" : "on"}`} disabled={!ov.clip_params.transitions}
                  onClick={() => act(() => api.put(`/api/projects/${id}/clip-settings`, {
                    disabled_transitions: off ? ov.clip_params.disabled_transitions.filter((x) => x !== t.id)
                      : [...ov.clip_params.disabled_transitions, t.id] }))}>{t.name}</button>
              );
            })}
          </div>
          <button className="pill small" onClick={() => act(() => api.post(`/api/projects/${id}/reedit`), "Перемонтовую з новими налаштуваннями")}>Перемонтувати</button>
        </Glass>
      )}

      <div className="cards">
        <Glass className="stack">
          <div className="label">Кліп</div>
          {ov.clip ? (
            <div className="card-stat">v{ov.clip.version} · {fmtTime(ov.clip.summary.duration || 0)}<br />
              {ov.clip.summary.cuts} склейок · {ov.clip.summary.inserts} вставки</div>
          ) : <div className="card-stat">ще немає</div>}
        </Glass>
        <Link to={`/project/${id}/vertical`}>
          <Glass className="stack" style={{ height: "100%" }}>
            <div className="label">Вертикальні</div>
            <div className="card-stat">{ov.vertical ? `${ov.vertical.summary.reels?.length || 0} ролики · 9:16` : "ще немає"} ›</div>
          </Glass>
        </Link>
      </div>

      <EditBox id={id} onDone={(m) => { setToast(m); refresh(); }} />

      {ov.clip && (
        <div className="row wrap">
          <a className="pill primary" href={`/api/edits/${ov.clip.id}/video`} download>⬇ mp4</a>
          <DaVinci editId={ov.clip.id} />
        </div>
      )}
    </div>
  );
}

function EditBox({ id, onDone }) {
  const [text, setText] = useState("");
  const [answer, setAnswer] = useState(null);
  const [busy, setBusy] = useState(false);
  const send = async () => {
    setBusy(true);
    try { setAnswer(await api.post(`/api/projects/${id}/requests`, { text })); setText(""); }
    catch (e) { onDone(e.message); }
    setBusy(false);
  };
  const apply = async (scope) => {
    try {
      await api.post(`/api/requests/${answer.id}/apply`, { scope });
      onDone(scope === "always" ? "Запам'ятав назавжди — перемонтовую" : "Перемонтовую");
      setAnswer(null);
    } catch (e) { onDone(e.message); }
  };
  return (
    <Glass className="stack">
      <div className="row">
        <input className="field grow" placeholder="Правка словами: «більше натовпу»" value={text}
          onChange={(e) => setText(e.target.value)} onKeyDown={(e) => e.key === "Enter" && text.trim() && send()} />
        <button className="round" aria-label="Надіслати правку" disabled={!text.trim() || busy} onClick={send}>↑</button>
      </div>
      {answer && (
        <div className="answer stack">
          <div>{answer.summary}</div>
          <div className="muted" style={{ fontSize: 14 }}>{answer.question}</div>
          <div className="row">
            <button className="pill primary" onClick={() => apply("here")}>Тільки тут</button>
            <button className="pill" onClick={() => apply("always")}>Назавжди</button>
            <button className="pill small" onClick={() => api.post(`/api/requests/${answer.id}/reject`).then(() => setAnswer(null))}>✕</button>
          </div>
        </div>
      )}
    </Glass>
  );
}

function DaVinci({ editId }) {
  const [st, setSt] = useState(null);
  useEffect(() => { api.get(`/api/edits/${editId}/davinci`).then(setSt).catch(() => {}); }, [editId]);
  if (!st) return null;
  return st.ready
    ? <button className="pill" title={st.folder} onClick={() => navigator.clipboard?.writeText(st.folder)}>DaVinci · скопіювати шлях</button>
    : <span className="pill" aria-disabled>DaVinci готується…</span>;
}

/** Смужка енергії треку: межі шматка перетягуються, по відпусканню — перемонтаж. */
function EnergyStrip({ id, onSaved }) {
  const [tr, setTr] = useState(null);
  const [win, setWin] = useState(null);
  const box = useRef(null);
  const drag = useRef(null);
  useEffect(() => {
    api.get(`/api/projects/${id}/track`).then((t) => { setTr(t); setWin(t.window); }).catch(() => {});
  }, [id]);
  if (!tr || !win) return null;
  const pct = (t) => `${(t / tr.duration) * 100}%`;
  const toTime = (clientX) => {
    const r = box.current.getBoundingClientRect();
    return Math.max(0, Math.min(tr.duration, ((clientX - r.left) / r.width) * tr.duration));
  };
  const move = (e) => {
    if (!drag.current) return;
    const t = toTime(e.clientX);
    setWin((w) => drag.current === "start" ? { ...w, start: Math.min(t, w.end - 5) } : { ...w, end: Math.max(t, w.start + 5) });
  };
  const up = async () => {
    if (!drag.current) return;
    drag.current = null;
    await api.put(`/api/projects/${id}/clip-settings`, { track_start: win.start, track_end: win.end });
    await api.post(`/api/projects/${id}/reedit`);
    onSaved(`Шматок треку ${fmtTime(win.start)}–${fmtTime(win.end)} — перемонтовую`);
  };
  const barW = 100 / tr.bars.length;
  return (
    <Glass className="stack">
      <div className="row spread">
        <div className="label">Трек · {Math.round(tr.tempo)} BPM</div>
        <div className="tc" style={{ fontSize: 13 }}>{fmtTime(win.start)}–{fmtTime(win.end)}</div>
      </div>
      <div className="energy" ref={box} onPointerMove={move} onPointerUp={up} onPointerLeave={up}>
        {tr.bars.map((b, i) => (
          <div key={i} className={`b ${b.level}`} style={{ left: `${i * barW}%`, width: `${barW * 0.8}%`, height: `${15 + b.energy * 85}%` }} />
        ))}
        {tr.drops.map((t, i) => <div key={`d${i}`} className="drop" style={{ left: pct(t) }} title="дроп" />)}
        <div className="win" style={{ left: pct(win.start), width: `calc(${pct(win.end)} - ${pct(win.start)})` }} />
        {["start", "end"].map((k) => (
          <div key={k} className="h" style={{ left: pct(win[k]) }} role="slider" aria-label={k === "start" ? "Початок" : "Кінець"}
            aria-valuenow={Math.round(win[k])} onPointerDown={(e) => { drag.current = k; e.currentTarget.setPointerCapture?.(e.pointerId); }}
            onPointerMove={move} onPointerUp={up} />
        ))}
      </div>
    </Glass>
  );
}
