(() => {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const defaultApi = window.location.hostname === "localhost" || window.location.hostname === "127.0.0.1"
    ? "http://localhost:8000"
    : "https://save-hack-api.onrender.com";
  const API_BASE = String(window.SAVE_HACK_API_BASE || params.get("api") || defaultApi).replace(/\/+$/, "");
  const $ = (selector) => document.querySelector(selector);
  const state = { format: "mp4", currentUrl: "", currentJobId: null, analyzing: false, downloading: false, availableHeights: [] };

  function status(message, kind = "") {
    const el = $("#status");
    el.hidden = false;
    el.className = "status " + kind;
    el.textContent = message;
  }

  function hideStatus() {
    $("#status").hidden = true;
    $("#status").textContent = "";
  }

  function setBusy(isBusy) {
    $("#analyze").disabled = isBusy || state.downloading;
    $("#download").disabled = isBusy || state.downloading;
    $("#paste").disabled = isBusy || state.downloading;
    $("#url").disabled = isBusy || state.downloading;
    document.querySelectorAll(".format").forEach((button) => { button.disabled = isBusy || state.downloading; });
    $("#quality").disabled = isBusy || state.downloading || state.format === "mp3";
  }

  function humanBytes(value) {
    if (!Number.isFinite(value) || value <= 0) return "";
    const units = ["B", "KB", "MB", "GB"];
    let size = value, index = 0;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index++; }
    return size.toFixed(index === 0 ? 0 : 1) + " " + units[index];
  }

  async function readResponse(response) {
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = data.detail;
      throw new Error(typeof detail === "string" ? detail : "Permintaan gagal (" + response.status + ").");
    }
    return data;
  }

  async function apiFetch(path, options = {}) {
    const response = await fetch(API_BASE + path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      cache: "no-store"
    });
    return response;
  }

  function validateForm() {
    const url = $("#url").value.trim();
    if (!url) { status("Masukkan tautan media terlebih dahulu.", "error"); $("#url").focus(); return false; }
    let parsed;
    try { parsed = new URL(url); } catch (_) { status("Format tautan tidak valid.", "error"); $("#url").focus(); return false; }
    if (parsed.protocol !== "https:") { status("Gunakan tautan HTTPS yang aman.", "error"); $("#url").focus(); return false; }
    if (!$("#rights").checked) { status("Konfirmasi hak unduh wajib dicentang.", "error"); $("#rights").focus(); return false; }
    return true;
  }

  function setProgress(title, progress, detail, indeterminate = false) {
    $("#progressPanel").hidden = false;
    $("#progressTitle").textContent = title;
    $("#progressDetails").textContent = detail;
    $("#progressTrack").classList.toggle("indeterminate", indeterminate);
    const safeProgress = Number.isFinite(progress) ? Math.max(0, Math.min(100, progress)) : 0;
    $("#progressBar").style.width = safeProgress + "%";
    $("#progressPercent").textContent = indeterminate ? "..." : Math.round(safeProgress) + "%";
    $("#progressTrack").setAttribute("aria-valuenow", String(Math.round(safeProgress)));
  }

  function resetProgress() {
    $("#progressPanel").hidden = true;
    $("#progressTrack").classList.remove("indeterminate");
    $("#progressBar").style.width = "0%";
    $("#progressPercent").textContent = "0%";
    $("#progressTrack").setAttribute("aria-valuenow", "0");
  }

  function setFormat(format) {
    state.format = format;
    document.querySelectorAll(".format").forEach((button) => {
      const active = button.dataset.format === format;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    $("#qualityField").hidden = format === "mp3";
    $("#quality").disabled = format === "mp3" || state.analyzing || state.downloading;
    $("#result").hidden = true;
  }

  function formatDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds <= 0) return "Durasi tidak tersedia";
    const total = Math.floor(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const secs = total % 60;
    return hours > 0
      ? hours + ":" + String(minutes).padStart(2, "0") + ":" + String(secs).padStart(2, "0")
      : minutes + ":" + String(secs).padStart(2, "0");
  }

  $("#paste").addEventListener("click", async () => {
    try {
      $("#url").value = await navigator.clipboard.readText();
      $("#url").focus();
      hideStatus();
    } catch (_) {
      status("Clipboard tidak tersedia. Tempel tautan secara manual.", "error");
    }
  });

  document.querySelectorAll(".format").forEach((button) => {
    button.addEventListener("click", () => setFormat(button.dataset.format));
  });

  $("#analyze").addEventListener("click", async () => {
    if (state.analyzing || state.downloading || !validateForm()) return;
    state.currentUrl = $("#url").value.trim();
    state.analyzing = true;
    setBusy(true);
    hideStatus();
    resetProgress();
    $("#result").hidden = true;
    status("Menghubungkan ke server dan menganalisis media...");
    try {
      const response = await apiFetch("/analyze", {
        method: "POST",
        body: JSON.stringify({ url: state.currentUrl, rights_confirmed: true })
      });
      const data = await readResponse(response);
      state.availableHeights = Array.isArray(data.available_heights) ? data.available_heights : [];
      $("#title").textContent = data.title || "Media ditemukan";
      $("#details").textContent = (data.uploader || "Sumber media") + " · " +
        formatDuration(data.duration) + " · " + (data.extractor || "Media");
      const maxHeight = Number(data.max_height);
      $("#sourceQuality").textContent = Number.isFinite(maxHeight) && maxHeight > 0
        ? "Resolusi MP4 tertinggi yang terdeteksi: " + maxHeight + "p. Pilihan di atas batas ini akan turun ke kualitas terdekat yang tersedia."
        : data.mp4_available === false
          ? "Stream video MP4 kompatibel tidak terdeteksi. Unduhan MP4 mungkin tidak tersedia untuk tautan ini."
          : "Resolusi sumber tidak dapat dipastikan sebelum proses unduhan.";
      $("#result").hidden = false;
      status("Analisis selesai. Pilih kualitas video, lalu mulai unduhan.", "success");
    } catch (error) {
      const isNetworkError = error instanceof TypeError;
      status(isNetworkError
        ? "Server belum dapat dijangkau. Periksa apakah backend sudah aktif dan URL API benar."
        : error.message, "error");
    } finally {
      state.analyzing = false;
      setBusy(false);
    }
  });

  async function pollJob(jobId) {
    while (true) {
      const response = await apiFetch("/jobs/" + encodeURIComponent(jobId), { method: "GET", headers: {} });
      const job = await readResponse(response);
      const progress = typeof job.progress === "number" ? job.progress : null;
      const bytes = job.total_bytes > 0
        ? humanBytes(job.downloaded_bytes) + " dari " + humanBytes(job.total_bytes)
        : (job.downloaded_bytes > 0 ? humanBytes(job.downloaded_bytes) + " diterima" : "Menunggu informasi progres dari sumber...");
      const labels = {
        queued: "Menunggu antrean",
        downloading: "Mengunduh media dari sumber",
        finalizing: "Menyatukan dan menyiapkan file",
        completed: "File siap diunduh",
        failed: "Unduhan gagal"
      };
      setProgress(labels[job.status] || "Memproses media", progress === null ? 0 : progress, bytes, progress === null);
      if (job.status === "failed" || job.status === "cancelled") throw new Error(job.error || "Unduhan dibatalkan.");
      if (job.status === "completed" && job.file_ready) return job;
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }
  }

  $("#download").addEventListener("click", async () => {
    if (state.analyzing || state.downloading || !validateForm()) return;
    state.currentUrl = $("#url").value.trim();
    state.downloading = true;
    setBusy(true);
    hideStatus();
    $("#result").hidden = true;
    setProgress("Memulai unduhan", 0, "Mengirim pekerjaan ke server...", true);
    try {
      const response = await apiFetch("/download", {
        method: "POST",
        body: JSON.stringify({
          url: state.currentUrl,
          format: state.format,
          quality: state.format === "mp3" ? "best" : $("#quality").value,
          rights_confirmed: true
        })
      });
      const started = await readResponse(response);
      if (!started.job_id) throw new Error("Server tidak mengembalikan ID unduhan yang valid.");
      state.currentJobId = started.job_id;
      $("#cancelDownload").hidden = false;
      $("#cancelDownload").disabled = false;
      await pollJob(started.job_id);
      const link = document.createElement("a");
      link.href = API_BASE + "/jobs/" + encodeURIComponent(started.job_id) + "/file";
      link.download = state.format === "mp3" ? "save-hack.mp3" : "save-hack.mp4";
      link.rel = "noopener";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setProgress("File siap diunduh", 100, "Unduhan browser dimulai. Periksa panel unduhan atau folder Download.", false);
      status("Pemrosesan selesai. Browser sedang menerima file.", "success");
    } catch (error) {
      status(error instanceof TypeError
        ? "Koneksi ke server terputus. Periksa jaringan dan status backend."
        : error.message, "error");
      $("#progressTitle").textContent = "Unduhan tidak selesai";
      $("#progressTrack").classList.remove("indeterminate");
    } finally {
      state.downloading = false;
      state.currentJobId = null;
      $("#cancelDownload").hidden = true;
      $("#cancelDownload").disabled = false;
      setBusy(false);
    }
  });

  $("#cancelDownload").addEventListener("click", async () => {
    if (!state.currentJobId || !state.downloading) return;
    const button = $("#cancelDownload");
    button.disabled = true;
    status("Mengirim permintaan pembatalan...", "");
    try {
      const response = await apiFetch("/jobs/" + encodeURIComponent(state.currentJobId) + "/cancel", {
        method: "POST"
      });
      const data = await readResponse(response);
      status(data.status === "cancelling" ? "Pembatalan diminta. Menunggu proses berhenti dengan aman..." : "Status unduhan: " + data.status, "");
    } catch (error) {
      status(error.message || "Permintaan pembatalan gagal.", "error");
      button.disabled = false;
    }
  });

  // A lightweight health check makes a disconnected backend visible before the first action.
  apiFetch("/health", { method: "GET", headers: {} })
    .then(readResponse)
    .then((data) => {
      if (data.ok) {
        $("#status").hidden = true;
      } else {
        status("Backend merespons, tetapi belum siap digunakan.", "error");
      }
    })
    .catch(() => {
      status("Backend belum dapat dijangkau. Fitur analisis dan unduhan aktif setelah API tersedia.", "error");
    });
})();
