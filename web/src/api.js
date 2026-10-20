// Тонкий клієнт API сервера на ноутбуці.
async function req(method, url, body) {
  const opt = { method, headers: {} };
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) {
    opt.headers["content-type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(url, opt);
  } catch (e) {
    window.dispatchEvent(new CustomEvent("am-offline", { detail: true }));  // ноутбук недоступний
    throw new Error("Ноутбук недоступний");
  }
  window.dispatchEvent(new CustomEvent("am-offline", { detail: false }));
  if (res.status === 401) window.dispatchEvent(new Event("am-auth"));
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("json") ? res.json() : res;
}

export const api = {
  get: (u) => req("GET", u),
  post: (u, b = {}) => req("POST", u, b),
  put: (u, b) => req("PUT", u, b),
  del: (u) => req("DELETE", u),
};

export const money = (v) => `$${(v ?? 0).toFixed(2)}`;
export const fmtDate = (iso) => {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString("uk-UA", { day: "2-digit", month: "short", year: "numeric" }).toUpperCase();
};
export const fmtTime = (s) => {
  const m = Math.floor(s / 60);
  return `${m}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
};

export const JOB_LABELS = {
  prepare_media: "Завантаження", analyze_media: "Аналіз", analyze_track: "Аналіз треку",
  score_project: "AI-оцінка", poll_batch: "Чекаю Gemini", edit_project: "Монтаж", vertical_project: "Вертикалі",
  export_davinci: "DaVinci", style_test: "Тест стилю", drive_fetch: "З Google Drive", drive_upload: "У Drive",
  drive_create_folder: "Папка в Drive", analyze_music: "Аналіз треку бібліотеки", drive_music_fetch: "Музика з Drive",
};
export const PROJECT_STATUS = {
  collecting: "чекає", queued: "у черзі", scoring: "аналіз", awaiting_confirm: "чекає підтвердження",
  editing: "монтаж", rendering: "монтаж", edited: "готово", needs_track: "потрібен трек",
};

// Посилання на папки Drive запам'ятовуються — працюють, навіть коли ноутбук вимкнений.
export const driveLinks = {
  get: () => { try { return JSON.parse(localStorage.getItem("am-drive") || "{}"); } catch (_) { return {}; } },
  set: (projectId, name, url) => {
    try { const d = driveLinks.get(); d[projectId] = { name, url }; localStorage.setItem("am-drive", JSON.stringify(d)); } catch (_) {}
  },
};

/** Швидкість завантаження на ноут: 2 МБ шумом, Мбіт/с. */
export async function measureUpload(bytes = 2 * 1024 * 1024) {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf.subarray(0, 65536));
  const t0 = performance.now();
  await fetch("/api/speedtest", { method: "POST", body: buf });
  const s = (performance.now() - t0) / 1000;
  return (bytes * 8) / 1e6 / Math.max(s, 0.001);
}
