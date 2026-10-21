import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, fmtDate, fmtTime, JOB_LABELS, money } from "../api.js";
import { ask, Glass, Toast, usePoll, VhsScreen } from "../components/ui.jsx";

export default function Project() {
  const { id } = useParams();
  const [ov, , setOv] = usePoll(() => api.get(`/api/projects/${id}/overview`), 3000, [id]);
  const [styles] = usePoll(() => api.get("/api/styles"), 60000);
  const [toast, setToast] = useState("");
  const [trackKey, setTrackKey] = useState(0);
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

      <MusicBox id={id} onDone={(m) => { setToast(m); refresh(); }} onPicked={(m) => { setToast(m); setTrackKey((k) => k + 1); refresh(); }} />

      {ov.tracks > 0 && <EnergyStrip key={`${ov.tracks}-${trackKey}`} id={id} onSaved={(m) => { setToast(m); refresh(); }} />}

      {styles && <StyleBox id={id} ov={ov} styles={styles} act={act} />}

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

      <DeleteProject id={id} onError={setToast} />

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
  // правка, що чекає на Gemini або вже розібрана й чекає «тільки тут / назавжди» (за останню добу)
  useEffect(() => {
    api.get(`/api/projects/${id}/requests`).then((list) => {
      const day = Date.now() - 24 * 3600 * 1000;
      const r = list.find((x) => x.status === "pending" && x.state !== "failed" && Date.parse(x.created_at) > day);
      if (r) setAnswer(r);
    }).catch(() => {});
  }, [id]);
  useEffect(() => {
    if (!answer?.waiting) return;
    const t = setInterval(() => api.get(`/api/requests/${answer.id}`).then((r) => {
      setAnswer(r.state === "failed" ? null : r);
      if (r.state === "failed") onDone(r.message);
    }).catch(() => {}), 15000);
    return () => clearInterval(t);
  }, [answer?.id, answer?.waiting]);
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
      {answer?.waiting && (
        <div className="answer stack">
          <div>⏳ «{answer.text}»</div>
          <div className="muted" style={{ fontSize: 14 }}>{answer.message}</div>
          <div className="row">
            <button className="pill small" onClick={() => api.post(`/api/requests/${answer.id}/reject`).then(() => setAnswer(null))}>Скасувати</button>
          </div>
        </div>
      )}
      {answer && !answer.waiting && (
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
  const pending = dirty || !tr.applied;  // шматок змінено або трек щойно взято — кліп ще не перемонтовано
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
    setSaved(win); setTr({ ...tr, applied: true });
    onSaved(`Шматок треку ${fmtTime(win.start)}–${fmtTime(win.end)} — перемонтовую`);
  };
  const barW = 100 / tr.bars.length;
  return (
    <Glass className="stack">
      <audio ref={audio} src={`/api/projects/${id}/track.audio`} preload="none" onTimeUpdate={tick} onPause={() => setPlaying(false)} />
      <div className="row spread">
        <div className="label" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "60%" }}>
          {tr.title || "Трек"} · {Math.round(tr.tempo)} BPM</div>
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
        {pending && <button className="pill small primary" onClick={apply}>Застосувати</button>}
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
  const remove = async (e) => {
    try {
      const pl = await api.get(`/api/edits/${e.id}/delete-plan`);
      if (!(await ask(`Видалити ${pl.label} (~${pl.mb} МБ)? Оригінальні відео лишаються.`, "Видалити", "Скасувати"))) return;
      await api.del(`/api/edits/${e.id}`); onChange(`${pl.label} видалено`); setMenu(null); load();
    } catch (err) { onChange(err.message); }
  };
  const prune = async () => {
    try {
      const pl = await api.get(`/api/projects/${id}/prune-plan`);
      if (!pl.count) { onChange("Старих версій немає — лишились тільки основні"); return; }
      if (!(await ask(`Видалити ${pl.count} старих версій (~${pl.mb} МБ)? Основні кліп і вертикалі та оригінали лишаються.`, "Видалити", "Скасувати"))) return;
      await api.post(`/api/projects/${id}/prune`); onChange(`Видалено ${pl.count} старих версій`); load();
    } catch (err) { onChange(err.message); }
  };
  const groups = [["clip", "Кліп"], ["vertical", "Вертикалі"]].map(([k, name]) => [k, name, list.filter((e) => e.kind === k)]);
  return (
    <Glass className="stack" style={{ gap: 4 }}>
      <div className="row spread">
        <div className="label">Версії</div>
        {list.length > 2 && <button className="pill small" onClick={prune}>🗑 Старі версії</button>}
      </div>
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
                  {!e.main && <button className="pill small" onClick={() => remove(e)}>🗑 Видалити</button>}
                </div>
              )}
            </div>
          ))}
        </div>
      ))}
    </Glass>
  );
}

/** Підбір музики: 3 треки з твоєї бібліотеки під це відео + ідеї нових пісень (SoundCloud). */
function MusicBox({ id, onDone, onPicked }) {
  const [res, setRes] = useState(null);
  const [busy, setBusy] = useState(false);
  const [lib, setLib] = useState(null);
  const [playing, setPlaying] = useState(null);
  const audio = useRef(null);
  useEffect(() => {
    api.get(`/api/projects/${id}/music-suggest`).then(setRes).catch(() => {});
    api.get("/api/music").then(setLib).catch(() => {});
    return () => audio.current?.pause();
  }, [id]);
  const run = async () => {
    setBusy(true);
    try { setRes(await api.post(`/api/projects/${id}/music-suggest`, { use_ai: true })); }
    catch (e) { onDone(e.message); }
    setBusy(false);
  };
  const play = (p) => {
    const a = audio.current;
    if (playing === p.track_id) { a.pause(); setPlaying(null); return; }
    a.src = `/api/music/${p.track_id}/audio`;
    a.currentTime = p.fragment?.start || 0;
    a.play().then(() => setPlaying(p.track_id)).catch(() => {});
    a.ontimeupdate = () => { if (p.fragment && a.currentTime >= p.fragment.end) { a.pause(); setPlaying(null); } };
  };
  // «Взяти» — трек стає в смужку нижче з найкращим шматком; межі правиш там, перемонтаж — кнопкою «Застосувати»
  const take = async (p) => {
    audio.current?.pause(); setPlaying(null);
    try {
      await api.post(`/api/projects/${id}/music/${p.track_id}/use`, p.fragment ? { start: p.fragment.start, end: p.fragment.end } : {});
      onPicked(`«${p.title}» узято — вибери шматок нижче і натисни «Застосувати»`);
    } catch (e) { onDone(e.message); }
  };
  const empty = lib && lib.tracks.filter((t) => t.status === "analyzed").length === 0;
  return (
    <Glass className="stack">
      <audio ref={audio} preload="none" onPause={() => setPlaying(null)} />
      <div className="row spread">
        <div className="label">Музика</div>
        <button className="pill small primary" disabled={busy || empty} onClick={run}>{busy ? "Підбираю…" : res ? "↻ Підібрати ще раз" : "🎵 Підібрати"}</button>
      </div>
      {empty && (
        <div className="faint" style={{ fontSize: 13 }}>
          Бібліотека порожня. Поклади треки (mp3/m4a/wav) у папку <span className="mono">{lib.folder}</span> на ноутбуці
          або в Google Drive → «Автомонтаж» → «{lib.drive_folder}». Програма проаналізує їх сама.
        </div>
      )}
      {res && res.picks.map((p, i) => (
        <div key={p.track_id} className="stack" style={{ gap: 4, paddingTop: 8, borderTop: i ? "1px solid rgba(255,255,255,.07)" : 0 }}>
          <div><b>{p.title}</b>{p.artist && <span className="muted"> — {p.artist}</span>}
            <span className="faint mono" style={{ fontSize: 11 }}> · {p.tempo} BPM · {fmtTime(p.duration)}</span></div>
          <div className="faint" style={{ fontSize: 13 }}>{p.reason}</div>
          <div className="row">
            <button className="pill small" onClick={() => play(p)}>{playing === p.track_id ? "■" : "▶"} {p.fragment ? `${fmtTime(p.fragment.start)}–${fmtTime(p.fragment.end)}` : ""}</button>
            <button className="pill small primary" onClick={() => take(p)}>Взяти</button>
          </div>
        </div>
      ))}
      {lib && lib.tracks.filter((t) => t.status === "analyzed").length > 0 && (
        <select className="field" value="" onChange={(e) => {
          const t = lib.tracks.find((x) => String(x.id) === e.target.value);
          if (t) take({ track_id: t.id, title: t.title });
        }}>
          <option value="">Або будь-який трек з бібліотеки…</option>
          {lib.tracks.filter((t) => t.status === "analyzed").map((t) => (
            <option key={t.id} value={t.id}>{t.artist ? `${t.artist} — ` : ""}{t.title} · {t.tempo} BPM</option>
          ))}
        </select>
      )}
      {res && res.ideas.length > 0 && (
        <div className="stack" style={{ gap: 4, paddingTop: 8 }}>
          <div className="label">Ідеї нових пісень</div>
          {res.ideas.map((x, i) => (
            <div key={i} className="row spread">
              <div className="grow"><b>{x.artist} — {x.title}</b><div className="faint" style={{ fontSize: 12 }}>{x.why}</div></div>
              <a className="pill small" href={x.soundcloud} target="_blank" rel="noreferrer">SoundCloud ↗</a>
            </div>
          ))}
          <div className="faint" style={{ fontSize: 11 }}>Якщо під треком є «Download» — скачай і кинь у папку музики або в Drive.</div>
        </div>
      )}
      {res?.ai_error && <div className="faint" style={{ fontSize: 11 }}>Без AI ({res.ai_error}) — підбір лише за темпом і енергією.</div>}
    </Glass>
  );
}

/** Видалити проєкт: оригінали й основна версія — в архів на ноутбуці, решта видаляється, папка в Drive — у кошик. */
function DeleteProject({ id, onError }) {
  const nav = useNavigate();
  const run = async () => {
    try {
      const pl = await api.get(`/api/projects/${id}/delete-plan`);
      const msg = `Видалити проєкт «${pl.name}»?\n\nВ архів (${pl.archive_mb} МБ): ${pl.originals} оригінальних відео` +
        (pl.keep.length ? ` і основна версія (${pl.keep.length} файл.)` : "") +
        ` → ${pl.archive}\n\nВидаляться проксі, аналіз і всі інші версії — звільниться ~${pl.freed_mb} МБ. Папка в Google Drive піде в кошик Drive.`;
      if (!(await ask(msg, "Видалити", "Скасувати"))) return;
      await api.del(`/api/projects/${id}`);
      nav("/");
    } catch (e) { onError(e.message); }
  };
  return <button className="pill small" style={{ alignSelf: "center", opacity: 0.8, marginTop: 24 }} onClick={run}>🗑 Видалити проєкт</button>;
}

/** Стиль вручну: усі стилі + рівень 1–3. Перемонтаж — лише кнопкою з питанням. */
function StyleBox({ id, ov, styles, act }) {
  const cur = ov.style;
  const changed = ov.clip && ((ov.clip.summary.style && ov.clip.summary.style !== cur.name) ||
    (ov.clip.summary.level && ov.clip.summary.level !== cur.level));
  return (
    <Glass className="stack">
      <div className="label">Стиль</div>
      <div className="row wrap">
        {styles.map((s) => (
          <button key={s.id} className={`pill small ${cur.id === s.id ? "on" : ""}`}
            onClick={() => act(() => api.put(`/api/projects/${id}/style`, { style_id: s.id }), `Стиль: ${s.name}`)}>{s.name}</button>
        ))}
      </div>
      <div className="row">
        <span className="faint" style={{ fontSize: 13 }}>Сила ефекту</span>
        {[1, 2, 3].map((l) => (
          <button key={l} className={`pill small ${cur.level === l ? "on" : ""}`}
            onClick={() => act(() => api.put(`/api/projects/${id}/clip-settings`, { style_level: l }), `Рівень ${l}`)}>{l}</button>
        ))}
      </div>
      {changed && (
        <button className="pill small primary" onClick={async () => {
          if (await ask(`Перемонтувати кліп у стилі «${cur.name}», рівень ${cur.level}? Gemini не потрібен.`)) act(() => api.post(`/api/projects/${id}/reedit`), "Перемонтовую в новому стилі");
        }}>Перемонтувати в «{cur.name}»</button>
      )}
    </Glass>
  );
}
