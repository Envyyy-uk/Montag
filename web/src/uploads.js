// Черга завантажень на рівні всього застосунку: переходи між вкладками її не зупиняють.
// По 2 файли одночасно; після обриву — повтори ~5 хв з того самого місця; «Продовжити» — вручну.
import { useEffect, useState } from "react";
import * as tus from "tus-js-client";

const PARALLEL = 2;
const queue = [];          // {file, pid, progress, state: wait|up|done|error, error}
const listeners = new Set();
let wake = null;

function emit() { for (const fn of listeners) fn([...queue]); }
function update(it, patch) { Object.assign(it, patch); emit(); }

async function keepAwake(on) {
  try {
    if (on && !wake && navigator.wakeLock) wake = await navigator.wakeLock.request("screen");
    if (!on && wake) { await wake.release(); wake = null; }
  } catch (_) { /* екран усе одно може згаснути — докачаємо після розблокування */ }
}

function startOne(it) {
  update(it, { state: "up", error: null });
  const up = new tus.Upload(it.file, {
    endpoint: "/api/uploads",
    retryDelays: [0, 1000, 3000, 5000, 10000, 15000, 30000, 30000, 60000, 60000, 60000],
    chunkSize: 16 * 1024 * 1024,
    metadata: { filename: it.file.name, project_id: it.pid },
    onProgress: (sent, total) => update(it, { progress: sent / total }),
    onSuccess: () => { update(it, { progress: 1, state: "done" }); pump(); },
    onError: () => {
      update(it, { state: "error", error: "Зв'язок з ноутбуком обірвався. «Продовжити» — докачає з того самого місця." });
      pump();
    },
  });
  up.findPreviousUploads().then((prev) => { if (prev.length) up.resumeFromPreviousUpload(prev[0]); up.start(); });
}

function pump() {
  let active = queue.filter((x) => x.state === "up").length;
  for (const it of queue) {
    if (active >= PARALLEL) break;
    if (it.state !== "wait") continue;
    active += 1;
    startOne(it);
  }
  keepAwake(queue.some((x) => x.state === "up" || x.state === "wait"));
}

export function addUploads(files, pid) {
  queue.push(...Array.from(files).map((file) => ({ file, pid: String(pid), progress: 0, state: "wait" })));
  emit();
  pump();
}

export function resumeUploads(only) {
  for (const it of queue) if (it.state === "error" && (!only || it === only)) it.state = "wait";
  emit();
  pump();
}

export function clearDone() {
  for (let i = queue.length - 1; i >= 0; i--) if (queue[i].state === "done") queue.splice(i, 1);
  emit();
}

/** Стан черги для екранів: список і підсумок. */
export function useUploads() {
  const [list, setList] = useState([...queue]);
  useEffect(() => { listeners.add(setList); return () => listeners.delete(setList); }, []);
  const active = list.filter((x) => x.state === "up" || x.state === "wait").length;
  const done = list.filter((x) => x.state === "done").length;
  const failed = list.filter((x) => x.state === "error").length;
  return { list, active, done, failed, total: list.length };
}

// повернувся в застосунок або з'явилась мережа — обірвані продовжуються самі
if (typeof window !== "undefined") {
  const again = () => { if (document.visibilityState === "visible") resumeUploads(); };
  window.addEventListener("online", again);
  document.addEventListener("visibilitychange", again);
}
