// Save Hack frontend: analyze a URL, pick a format, download with progress / cancel / retry.
// config.js may set window.SAVE_HACK_API_BASE; the Railway URL is the production fallback.
const API_BASE = (window.SAVE_HACK_API_BASE || "https://save-hack-api-production-f527.up.railway.app").replace(/\/$/, "");
const POLL_MS = 1000;
const $ = (s) => document.querySelector(s);

const state = {
  category: "mp4",      // "mp4" (video/image/file) or "mp3" (audio) -- filters the option list
  analysis: null,       // response of /api/analyze
  formatId: null,
  jobId: null,
  polling: null,
  busy: false,
};

// ---------- helpers ----------
function status(message, kind = "") {
  const el = $("#status");
  el.hidden = !message;
  el.className = "status " + kind;
  el.textContent = message || "";
}

function fmtDuration(sec) {
  if (!sec || sec <= 0) return "";
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const pad = (n) => String(n).padStart(2, "0");
  return h ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

function fmtBytes(n) {
  if (!n || n <= 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n >= 100 || i === 0 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c) node.append(c);
  return node;
}

// Every string from the server is inserted with textContent / setAttribute only (never innerHTML).
function httpsUrl(u) {
  try { const p = new URL(u); return p.protocol === "https:" ? p.href : null; } catch { return null; }
}

async function api(path, options = {}) {
  let res;
  try {
    res = await fetch(API_BASE + path, options);
  } catch {
    throw Object.assign(new Error("Tidak dapat terhubung ke server. Periksa koneksi internetmu lalu coba lagi."), { code: "NETWORK_ERROR", retryable: true });
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    const info = data.error || {};
    throw Object.assign(new Error(info.message || data.detail || "Permintaan gagal."), { code: info.code || "SERVER_ERROR", retryable: !!info.retryable, status: res.status });
  }
  return data;
}

const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

// ---------- UI state ----------
function updateDownloadEnabled() {
  $("#download").disabled = !state.formatId || state.busy;
}

function setBusy(busy) {
  state.busy = busy;
  $("#analyze").disabled = busy;
  updateDownloadEnabled();
}

function showProgress(visible, percent = null, text = "") {
  $("#progress").hidden = !visible;
  const bar = $(".bar");
  bar.classList.toggle("indeterminate", visible && percent === null);
  $("#barfill").style.width = percent === null ? "" : percent + "%";
  $("#progresstext").textContent = text;
}

function setButtons({ download = true, cancel = false, retry = false } = {}) {
  $("#download").hidden = !download;
  $("#cancel").hidden = !cancel;
  $("#retry").hidden = !retry;
}

function visibleOptions() {
  const formats = state.analysis ? state.analysis.formats : [];
  const wanted = state.category === "mp3" ? ["audio"] : ["video", "image", "file"];
  const main = formats.filter((f) => wanted.includes(f.kind));
  const subs = formats.filter((f) => f.kind === "subtitle");
  return { main, subs };
}

function describe(f) {
  return [f.resolution, f.vcodec, f.acodec && f.vcodec ? "+ " + f.acodec : f.acodec, f.ext && f.ext.toUpperCase(), f.note].filter(Boolean).join(" · ");
}

function renderOptions() {
  const box = $("#formatlist");
  box.replaceChildren();
  const { main, subs } = visibleOptions();
  const all = main.concat(subs);
  if (!main.length) {
    box.append(el("p", { class: "empty", text: state.category === "mp3" ? "Tidak ada opsi audio untuk media ini. Pilih MP4 di atas." : "Tidak ada opsi video untuk media ini. Pilih MP3 di atas." }));
  }
  if (!all.some((f) => f.id === state.formatId)) state.formatId = main.length ? main[0].id : null;
  for (const f of all) {
    const btn = el("button", { type: "button", class: "opt", role: "radio", "aria-checked": String(f.id === state.formatId) });
    btn.append(el("span", {}, el("b", { text: f.label }), el("small", { text: describe(f) })), el("span", { class: "size", text: fmtBytes(f.filesize) }));
    btn.addEventListener("click", () => { state.formatId = f.id; renderOptions(); });
    box.append(btn);
  }
  updateDownloadEnabled();
}

function renderPreview(media) {
  const box = $("#preview");
  box.replaceChildren();
  const thumb = httpsUrl(media.thumbnail);
  const preview = httpsUrl(media.preview_url);
  let node = null;
  if (preview && media.kind === "video") node = el("video", { src: preview, controls: "", preload: "metadata", playsinline: "", referrerpolicy: "no-referrer" });
  else if (preview && media.kind === "audio") node = el("audio", { src: preview, controls: "", preload: "none" });
  else if (preview && media.kind === "image") node = el("img", { src: preview, alt: "", referrerpolicy: "no-referrer", loading: "lazy" });
  else if (thumb) node = el("img", { src: thumb, alt: "", referrerpolicy: "no-referrer", loading: "lazy" });
  if (node) {
    if (node.tagName === "VIDEO" && thumb) node.setAttribute("poster", thumb);
    node.addEventListener("error", () => { box.hidden = true; }, { once: true });
    box.append(node);
  }
  box.hidden = !node;
}

function renderResult(data) {
  const m = data.media;
  $("#title").textContent = m.title || "Media ditemukan";
  const parts = [m.uploader, fmtDuration(m.duration), fmtBytes(m.filesize)].filter(Boolean);
  const meta = m.meta || {};
  if (meta.audio_tracks && meta.audio_tracks.length) parts.push("Audio: " + meta.audio_tracks.join(", "));
  if (meta.codecs && meta.codecs.length) parts.push(meta.codecs.join(", "));
  $("#details").textContent = parts.join(" · ") || "Detail tidak tersedia";
  $("#source").textContent = `Sumber: ${m.site || "Tidak diketahui"} · mesin: ${m.engine}`;
  renderPreview(m);
  state.formatId = data.default_format;
  // If the default is audio-only content, switch the category so the list is not empty.
  if (!data.formats.some((f) => ["video", "image", "file"].includes(f.kind)) && data.formats.some((f) => f.kind === "audio")) setCategory("mp3", false);
  renderOptions();
  showProgress(false);
  setButtons({ download: true });
  $("#result").hidden = false;
}

function setCategory(cat, rerender = true) {
  state.category = cat;
  document.querySelectorAll(".format").forEach((b) => b.classList.toggle("active", b.dataset.format === cat));
  if (rerender && state.analysis) renderOptions();
}

// ---------- analyze ----------
function valid() {
  if (!$("#url").value.trim()) { status("Masukkan URL terlebih dahulu.", "error"); return false; }
  if (!$("#rights").checked) { status("Konfirmasi hak unduh wajib dicentang.", "error"); return false; }
  return true;
}

async function analyze() {
  if (state.busy || !valid()) return;
  await stopJob(true);
  setBusy(true);
  $("#result").hidden = true;
  status("Menganalisis URL… beberapa sumber bisa memerlukan hingga 45 detik.");
  try {
    const data = await post("/api/analyze", { url: $("#url").value.trim(), rights_confirmed: true });
    state.analysis = data;
    state.jobId = data.job_id;
    renderResult(data);
    status("Analisis selesai. Pastikan kamu berhak menyimpan media ini.", "success");
  } catch (e) {
    state.analysis = null;
    status(e.message, "error");
  } finally {
    setBusy(false);
  }
}

// ---------- download ----------
function progressText(p) {
  const stage = { queued: "Mengantre…", downloading: "Mengunduh", converting: "Mengonversi / menggabungkan…", done: "Selesai" }[p.stage] || p.stage;
  if (p.stage !== "downloading") return stage;
  const bits = [stage];
  if (p.percent !== null) bits.push(p.percent.toFixed(0) + "%");
  const size = p.total ? `${fmtBytes(p.downloaded)} / ${fmtBytes(p.total)}` : fmtBytes(p.downloaded);
  if (size) bits.push(size);
  if (p.speed) bits.push(fmtBytes(p.speed) + "/s");
  if (p.eta) bits.push("sisa " + fmtDuration(p.eta));
  return bits.join(" · ");
}

function stopPolling() {
  if (state.polling) { clearTimeout(state.polling); state.polling = null; }
}

async function startDownload() {
  if (!state.jobId || !state.formatId || !valid()) return;
  stopPolling();
  setButtons({ download: false, cancel: true });
  showProgress(true, null, "Memulai…");
  status("Memproses file di server…");
  try {
    await post("/api/download", { job_id: state.jobId, format_id: state.formatId });
  } catch (e) {
    if (e.status === 404 || (e.code === "MEDIA_NOT_FOUND" && /kedaluwarsa|tidak ditemukan/i.test(e.message))) {
      showProgress(false);
      setButtons({ download: true });
      status("Sesi analisis sudah kedaluwarsa. Menganalisis ulang…");
      await analyze();
      return;
    }
    return failed(e);
  }
  poll();
}

function poll() {
  const jobId = state.jobId;
  state.polling = setTimeout(async () => {
    try {
      const job = await api("/api/jobs/" + encodeURIComponent(jobId));
      if (jobId !== state.jobId) return;
      const p = job.progress;
      showProgress(true, job.status === "running" || job.status === "queued" ? p.percent : 100, progressText(p));
      if (job.status === "done") return finished(job);
      if (job.status === "failed") return failed(Object.assign(new Error(job.error ? job.error.message : "Unduhan gagal."), { retryable: job.error ? job.error.retryable : true, code: job.error && job.error.code }));
      if (job.status === "cancelled") { showProgress(false); setButtons({ download: true }); status("Unduhan dibatalkan.", ""); return; }
      poll();
    } catch (e) {
      if (e.status === 404) { showProgress(false); setButtons({ download: true }); status("Sesi kedaluwarsa. Analisis ulang URL-nya.", "error"); return; }
      // transient network hiccup while polling: keep trying
      showProgress(true, null, "Menghubungkan ulang…");
      poll();
    }
  }, POLL_MS);
}

function finished(job) {
  showProgress(true, 100, `Selesai · ${job.file.name} · ${fmtBytes(job.file.size)}`);
  setButtons({ download: true });
  status("File siap. Unduhan dimulai oleh browser (periksa folder Download).", "success");
  // Let the browser stream the file straight to disk: no in-memory blob, resumable by the browser.
  const a = document.createElement("a");
  a.href = `${API_BASE}/api/download/${encodeURIComponent(state.jobId)}`;
  a.rel = "noopener";
  a.download = job.file.name;
  document.body.append(a);
  a.click();
  a.remove();
}

function failed(e) {
  stopPolling();
  showProgress(false);
  const canRetry = e.retryable !== false;
  setButtons({ download: !canRetry, retry: canRetry });
  status(e.message, "error");
}

async function stopJob(silent = false) {
  stopPolling();
  const id = state.jobId;
  if (!id) return;
  state.jobId = null;
  try { await api("/api/jobs/" + encodeURIComponent(id), { method: "DELETE" }); } catch { /* already gone */ }
  if (!silent) { showProgress(false); setButtons({ download: true }); }
}

async function cancel() {
  const keep = state.analysis;
  await stopJob();
  status("Unduhan dibatalkan.");
  // The server deleted the job, so analysis data is stale: ask for a fresh analysis on next download.
  if (keep) { state.jobId = null; state.analysis = keep; $("#download").textContent = "Analisis ulang & unduh"; }
}

// ---------- wiring ----------
document.querySelectorAll(".format").forEach((b) => b.addEventListener("click", () => setCategory(b.dataset.format)));
$("#paste").addEventListener("click", async () => {
  try { $("#url").value = (await navigator.clipboard.readText()).trim(); } catch { status("Clipboard tidak tersedia. Tempel URL secara manual.", "error"); }
});
$("#analyze").addEventListener("click", analyze);
$("#url").addEventListener("keydown", (e) => { if (e.key === "Enter") analyze(); });
$("#download").addEventListener("click", async () => {
  if (!state.jobId) { await analyze(); if (!state.jobId) return; }
  $("#download").textContent = "Unduh file";
  startDownload();
});
$("#cancel").addEventListener("click", cancel);
$("#retry").addEventListener("click", startDownload);
