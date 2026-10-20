import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, fmtDate, fmtTime, JOB_LABELS, money } from "../api.js";
import { ask, Glass, Toast, usePoll, VhsScreen } from "../components/ui.jsx";

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
        <button className="pill rec" onClick={async () => { if (await ask("Почати монтаж? Якщо є ключ Gemini — спершу AI-оцінка кадрів (покаже ціну, якщо більше $1).")) act(() => api.post(`/api/projects/${id}/edit`), "Монтую"); }}>● Монтувати</button>
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
          <button className="pill small" onClick={async () => { if (await ask("Перемонтувати кліп з цими налаштуваннями?")) act(() => api.post(`/api/projects/${id}/reedit`), "Перемонтовую з новими налаштуваннями"); }}>Перемонтувати</button>
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
            <div className="card-stat">{ov.vertical ? `${ov.vertical.summary.reels?.length || 0} ролики · ${ov.vertical.aspect}` : "ще немає"} ›</div>
          </Glass>
        </Link>
      </div>

      <FormatBox id={id} ov={ov} act={act} />

      <EditBox id={id} onDone={(m) => { setToast(m); refresh(); }} />

      <Versions id={id} onChange={(m) => { setToast(m); refresh(); }} />

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

/** Смужка енергії треку: межі тягнуться вільно, ▶ — прослухати шматок, перемонтаж — лише кнопкою «Застосувати». */
function EnergyStrip({ id, onSaved }) {
  const [tr, setTr] = useState(null);
  const [win, setWin] = useState(null);
  const [saved, setSaved] = useState(null);
  const [playing, setPlaying] = useState(false);
  const box = useRef(null);
  const drag = useRef(null);
  const audio = useRef(null);
  useEffect(() => {
    api.get(`/api/projects/${id}/track`).then((t) => { setTr(t); setWin(t.window); setSaved(t.window); }).catch(() => {});
    return () => audio.current?.pause();
  }, [id]);
  if (!tr || !win) return null;
  const pct = (t) => `${(t / tr.duration) * 100}%`;
  const toTime = (clientX) => {
    const r = box.current.getBoundingClientRect();
    return Math.max(0, Math.min(tr.duration, ((clientX - r.left) / r.width) * tr.duration));
  };
  const move = (e) => {
    if (!drag.current) return;
    e.preventDefault();
    const t = toTime(e.clientX);
    setWin((w) => drag.current === "start" ? { ...w, start: Math.min(t, w.end - 5) } : { ...w, end: Math.max(t, w.start + 5) });
  };
  const up = () => { drag.current = null; };
  const dirty = saved && (Math.abs(win.start - saved.start) > 0.05 || Math.abs(win.end - saved.end) > 0.05);
  const play = () => {
    const a = audio.current;
    if (!a) return;
    if (playing) { a.pause(); setPlaying(false); return; }
    a.currentTime = win.start;
    a.play().then(() => setPlaying(true)).catch(() => {});
  };
  const tick = () => {
    const a = audio.current;
    if (a && a.currentTime >= win.end) { a.pause(); setPlaying(false); }
  };
  const apply = async () => {
    audio.current?.pause(); setPlaying(false);
    if (!(await ask(`Перемонтувати кліп під шматок ${fmtTime(win.start)}–${fmtTime(win.end)}?`))) return;
    await api.put(`/api/projects/${id}/clip-settings`, { track_start: win.start, track_end: win.end });
    await api.post(`/api/projects/${id}/reedit`);
    setSaved(win);
    onSaved(`Шматок треку ${fmtTime(win.start)}–${fmtTime(win.end)} — перемонтовую`);
  };
  const barW = 100 / tr.bars.length;
  return (
    <Glass className="stack">
      <audio ref={audio} src={`/api/projects/${id}/track.audio`} preload="none" onTimeUpdate={tick} onPause={() => setPlaying(false)} />
      <div className="row spread">
        <div className="label">Трек · {Math.round(tr.tempo)} BPM</div>
        <div className="tc" style={{ fontSize: 13 }}>{fmtTime(win.start)}–{fmtTime(win.end)} · {Math.round(win.end - win.start)} с</div>
      </div>
      <div className="energy" ref={box} style={{ touchAction: "none" }} onPointerMove={move} onPointerUp={up} onPointerCancel={up}>
        {tr.bars.map((b, i) => (
          <div key={i} className={`b ${b.level}`} style={{ left: `${i * barW}%`, width: `${barW * 0.8}%`, height: `${15 + b.energy * 85}%` }} />
        ))}
        {tr.drops.map((t, i) => <div key={`d${i}`} className="drop" style={{ left: pct(t) }} title="дроп" />)}
        <div className="win" style={{ left: pct(win.start), width: `calc(${pct(win.end)} - ${pct(win.start)})` }} />
        {["start", "end"].map((k) => (
          <div key={k} className="h" style={{ left: pct(win[k]), touchAction: "none" }} role="slider" aria-label={k === "start" ? "Початок" : "Кінець"}
            aria-valuenow={Math.round(win[k])} onPointerDown={(e) => { drag.current = k; e.currentTarget.setPointerCapture?.(e.pointerId); }}
            onPointerMove={move} onPointerUp={up} onPointerCancel={up} />
        ))}
      </div>
      <div className="row wrap">
        <button className="pill small" onClick={play}>{playing ? "■ Стоп" : "▶ Прослухати"}</button>
        {dirty && <button className="pill small primary" onClick={apply}>Застосувати</button>}
        {dirty && <button className="pill small" onClick={() => setWin(saved)}>Скасувати</button>}
      </div>
    </Glass>
  );
}

const CLIP_ASPECTS = { "16:9": "16:9 · YouTube", "9:16": "9:16 · Reels/TikTok", "1:1": "1:1 · квадрат", "4:5": "4:5 · Instagram" };
const FIT_MODES = { auto: "авто", crop: "обрізати", blur: "розмитий фон" };

/** Формат кліпу і що робити з кадрами іншої форми (напр. вертикальне відео в горизонтальному кліпі). */
function FormatBox({ id, ov, act }) {
  const p = ov.clip_params;
  const set = (patch, msg) => act(() => api.put(`/api/projects/${id}/clip-settings`, patch), msg);
  const changed = ov.clip && ((p.aspect || "16:9") !== (ov.clip.aspect || "16:9"));
  return (
    <Glass className="stack">
      <div className="label">Формат кліпу</div>
      <div className="row wrap">
        {Object.entries(CLIP_ASPECTS).map(([k, label]) => (
          <button key={k} className={`pill small ${(p.aspect || "16:9") === k ? "on" : ""}`} onClick={() => set({ aspect: k }, `Формат ${k}`)}>{label}</button>
        ))}
      </div>
      <div className="label">Кадри іншої форми</div>
      <div className="row wrap">
        {Object.entries(FIT_MODES).map(([k, label]) => (
          <button key={k} className={`pill small ${(p.fit_mode || "auto") === k ? "on" : ""}`} onClick={() => set({ fit_mode: k }, label)}>{label}</button>
        ))}
      </div>
      <div className="faint" style={{ fontSize: 12 }}>Авто: обрізає за обличчями, а якщо обрізати не вийде — кладе на розмитий фон.</div>
      {changed && <button className="pill small primary" onClick={async () => { if (await ask(`Перемонтувати кліп у форматі ${p.aspect}?`)) act(() => api.post(`/api/projects/${id}/reedit`), "Перемонтовую в новому форматі"); }}>Перемонтувати в {p.aspect}</button>}
    </Glass>
  );
}

/** Усі версії: дві розкривашки (кліп / вертикалі), кожна версія — один рядок, дії — у меню «⋯». */
function Versions({ id, onChange }) {
  const [list, setList] = useState(null);
  const [open, setOpen] = useState({});      // яка група розгорнута
  const [menu, setMenu] = useState(null);    // у якої версії відкрите «⋯»
  const load = () => api.get(`/api/projects/${id}/edits`).then(setList).catch(() => {});
  useEffect(() => { load(); }, [id]);
  if (!list || list.length === 0) return null;
  const main = async (e) => {
    if (!(await ask(`Зробити v${e.version} основною? Наступні правки підуть від її налаштувань.`))) return;
    try { await api.post(`/api/edits/${e.id}/main`); onChange(`v${e.version} тепер основна`); setMenu(null); load(); }
    catch (err) { onChange(err.message); }
  };
  const groups = [["clip", "Кліп"], ["vertical", "Вертикалі"]].map(([k, name]) => [k, name, list.filter((e) => e.kind === k)]);
  return (
    <Glass className="stack" style={{ gap: 4 }}>
      <div className="label">Версії</div>
      {groups.filter(([, , items]) => items.length).map(([k, name, items]) => (
        <div key={k}>
          <button className="row spread versions-head" onClick={() => setOpen({ ...open, [k]: !open[k] })}>
            <span>{name} <span className="faint">· {items.length}</span></span>
            <span className="faint">{open[k] ? "▴" : "▾"}</span>
          </button>
          {open[k] && items.map((e) => (
            <div key={e.id} className="version-row">
              <button className="row spread versions-head" onClick={() => setMenu(menu === e.id ? null : e.id)}>
                <span>v{e.version} {e.main && <span className="badge done">основна</span>}
                  <span className="faint mono" style={{ fontSize: 11, marginLeft: 6 }}>{e.created_at.slice(5, 16).replace("T", " ")} · {e.aspect}</span>
                </span>
                <span className="faint">⋯</span>
              </button>
              {menu === e.id && (
                <div className="row wrap" style={{ padding: "4px 0 8px" }}>
                  {k === "clip" && e.files.preview && <a className="pill small" href={`/api/edits/${e.id}/preview`} target="_blank" rel="noreferrer">▶</a>}
                  {k === "clip" && e.files.video && <a className="pill small" href={`/api/edits/${e.id}/video`} download>⬇ 1080p</a>}
                  {k === "clip" && e.files.preview && <a className="pill small" href={`/api/edits/${e.id}/preview?download=1`} download>⬇ 720p</a>}
                  {k === "vertical" && (e.summary.reels || []).map((r, i) => (
                    <a key={i} className="pill small" href={`/api/edits/${e.id}/reels/${i + 1}`} download>⬇ #{i + 1}</a>
                  ))}
                  {!e.main && <button className="pill small" onClick={() => main(e)}>★ Основна</button>}
                </div>
              )}
            </div>
          ))}
        </div>
      ))}
    </Glass>
  );
}
