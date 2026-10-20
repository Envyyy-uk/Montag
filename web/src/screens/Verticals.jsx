import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api.js";
import { ask, Glass, Toast, usePoll, VhsScreen } from "../components/ui.jsx";

const AUDIO = { live: "Живий звук", track: "Під трек", none: "Без музики" };
const REEL_ASPECTS = { "9:16": "9:16 · Reels, TikTok, Shorts", "4:5": "4:5 · стрічка Instagram", "1:1": "1:1 · квадрат" };

export default function Verticals() {
  const { id } = useParams();
  const [ov, , setOv] = usePoll(() => api.get(`/api/projects/${id}/overview`), 4000, [id]);
  const [toast, setToast] = useState("");
  if (!ov) return <VhsScreen message="Завантаження…" />;
  const v = ov.vertical;
  const busy = ov.jobs.some((j) => j.kind === "vertical_project");

  const setAudio = async (audio) => {
    await api.put(`/api/projects/${id}/vertical-settings`, { audio });
    if (await ask(`${AUDIO[audio]}. Переробити ролики зараз?`)) {
      await api.post(`/api/projects/${id}/verticals`);
      setToast(`${AUDIO[audio]} — перероблюю ролики`);
    } else setToast("Збережено — застосується, коли переробиш ролики");
    setOv(await api.get(`/api/projects/${id}/overview`));
  };

  const setAspect = async (aspect) => {
    await api.put(`/api/projects/${id}/vertical-settings`, { aspect });
    if (await ask(`Формат ${aspect}. Переробити ролики зараз?`)) {
      await api.post(`/api/projects/${id}/verticals`);
      setToast(`Формат ${aspect} — перероблюю ролики`);
    } else setToast("Збережено — застосується, коли переробиш ролики");
    setOv(await api.get(`/api/projects/${id}/overview`));
  };

  const share = async (n, name) => {
    const url = `/api/edits/${v.id}/reels/${n}`;
    try {
      const blob = await (await fetch(url)).blob();
      const file = new File([blob], name, { type: "video/mp4" });
      if (navigator.canShare?.({ files: [file] })) { await navigator.share({ files: [file], title: ov.name }); return; }
    } catch (e) { if (e.name === "AbortError") return; }
    window.location.href = url;  // без меню «Поділитися» — просто завантажити
  };

  return (
    <div className="stack">
      <Toast text={toast} onDone={() => setToast("")} />
      <Link to={`/project/${id}`} className="faint">‹ {ov.name}</Link>
      <h1 className="title-xl">Вертикальні</h1>
      <Glass className="row wrap">
        {Object.entries(AUDIO).map(([k, label]) => (
          <button key={k} className={`pill small ${ov.vertical_params.audio === k ? "on" : ""}`} onClick={() => setAudio(k)}>{label}</button>
        ))}
      </Glass>
      <Glass className="stack">
        <div className="label">Формат</div>
        <div className="row wrap">
          {Object.entries(REEL_ASPECTS).map(([k, label]) => (
            <button key={k} className={`pill small ${(ov.vertical_params.aspect || "9:16") === k ? "on" : ""}`} onClick={() => setAspect(k)}>{label}</button>
          ))}
        </div>
      </Glass>
      {!busy && (
        <button className="pill small" onClick={async () => {
          if (!(await ask("Переробити ролики з поточними налаштуваннями?"))) return;
          await api.post(`/api/projects/${id}/verticals`); setToast("Перероблюю ролики");
          setOv(await api.get(`/api/projects/${id}/overview`));
        }}>↻ Переробити ролики</button>
      )}
      {busy && <VhsScreen message="Роблю вертикалі…" />}
      {!busy && !v && <Glass className="muted">Ролики з'являться після аналізу проєкту.</Glass>}
      {!busy && v && v.summary.reels.map((r, i) => (
        <Glass key={r.path} className="stack">
          <video className="reel" style={{ aspectRatio: (v.aspect || "9:16").replace(":", "/") }} src={`/api/edits/${v.id}/reels/${i + 1}`} controls playsInline preload="metadata" />
          <div className="row spread">
            <div className="mono muted" style={{ fontSize: 13 }}>#{i + 1} · {r.duration.toFixed(1)} с · {AUDIO[r.audio]}</div>
            <button className="pill primary" onClick={() => share(i + 1, r.path.split("/").pop())}>Поділитися</button>
          </div>
        </Glass>
      ))}
    </div>
  );
}
