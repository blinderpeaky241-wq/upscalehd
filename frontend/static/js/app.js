(() => {
  "use strict";
  const $ = (s) => document.querySelector(s);
  const MP4_AUDIO = ["aac", "mp3", "ac3", "eac3", "alac", "mp2"];

  const S = { config: null, upload: null, info: null, job: null, es: null, poll: null, uploading: false };

  // ------------------------------------------------------------ helpers
  const fmtBytes = (n) => {
    if (n == null) return "–";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i > 1 ? n.toFixed(2) : n.toFixed(i ? 1 : 0)) + " " + u[i];
  };
  const fmtTime = (s) => {
    if (s == null || !isFinite(s)) return "–";
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
  };
  const fmtEta = (s) => (s == null ? "Calculating…" : s < 60 ? "< 1 min" : "about " + fmtTime(s));
  const hide = (el, yes = true) => { el.hidden = yes; };
  const show = (el) => hide(el, false);

  function fillGrid(el, rows) {
    el.textContent = "";
    for (const [k, v] of rows) {
      const wrap = document.createElement("div"), dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = k; dd.textContent = v ?? "–";
      wrap.append(dt, dd); el.append(wrap);
    }
  }

  function setBar(bar, pct) {
    const i = bar.firstElementChild;
    if (pct == null) { bar.classList.add("indeterminate"); bar.removeAttribute("aria-valuenow"); i.style.width = ""; return; }
    bar.classList.remove("indeterminate");
    bar.setAttribute("aria-valuenow", Math.round(pct));
    i.style.width = Math.max(0, Math.min(100, pct)) + "%";
  }

  function banner(msg, kind = "error") {
    const b = $("#banner");
    if (!msg) return hide(b);
    b.textContent = msg; b.className = "banner" + (kind === "warn" ? " warn" : ""); show(b);
  }

  async function api(path, opts = {}) {
    let res;
    try {
      res = await fetch(path, { credentials: "same-origin", ...opts });
    } catch (e) {
      throw { code: "network", message: "Could not reach the server. Check your connection and try again." };
    }
    let data = null;
    try { data = await res.json(); } catch (_) { /* non-JSON */ }
    if (!res.ok) throw data?.error || { code: "server", message: "Server error (" + res.status + ")." };
    return data;
  }

  // ------------------------------------------------------------ boot
  async function boot() {
    try {
      S.config = await api("/api/config");
    } catch (e) {
      banner(e.message); return;
    }
    const c = S.config;
    document.querySelectorAll("[data-max-size]").forEach((n) => (n.textContent = fmtBytes(c.limits.max_upload_bytes).replace(".00", "")));
    $("#formats").textContent = c.limits.extensions.map((e) => e.slice(1).toUpperCase()).join(", ");
    $("#retention").textContent = c.limits.retention_hours;
    if (!c.ffmpeg.ok) {
      banner(c.ffmpeg.error + " Uploads are disabled until FFmpeg is installed.");
      $("#select-btn").disabled = true; $("#dropzone").setAttribute("aria-disabled", "true");
    }
    // encoders: disable the unavailable ones and say why
    const sel = $("#encoder-select");
    sel.append(new Option("Auto (best available)", "auto"), new Option("CPU (x264) – always works", "cpu"));
    const notes = [];
    for (const e of c.encoders.filter((x) => x.id !== "libx264")) {
      const o = new Option(e.label + (e.available ? "" : " – unavailable"), e.id);
      o.disabled = !e.available; sel.append(o);
      if (!e.available) notes.push(e.label + ": " + e.reason);
    }
    $("#encoder-note").textContent = notes.length ? notes.join(" ") : "GPU encoder detected – Auto will use it and fall back to the CPU if it fails.";
    if (!c.ai.available) {
      const opt = $("#ai-opt"); opt.classList.add("disabled");
      opt.querySelector("input").disabled = true;
      $("#ai-note").textContent = "Unavailable: " + c.ai.reason;
    }
    // resume a conversion after a page reload
    try {
      const id = sessionStorage.getItem("job");
      if (id) { const { job } = await api("/api/jobs/" + id); S.job = job; hide($("#upload-card")); watch(job.id); onJob(job); }
    } catch (_) { try { sessionStorage.removeItem("job"); } catch (__) { /* ignore */ } }
  }

  // ------------------------------------------------------------ file selection
  const dz = $("#dropzone"), input = $("#file-input");
  $("#select-btn").addEventListener("click", (e) => { e.stopPropagation(); input.click(); });
  $("#hero-upload").addEventListener("click", () => setTimeout(() => $("#select-btn").focus({ preventScroll: true }), 400));
  dz.addEventListener("click", () => !$("#select-btn").disabled && input.click());
  dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
  ["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
  dz.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) startUpload(e.dataTransfer.files[0]); });
  input.addEventListener("change", () => { if (input.files[0]) startUpload(input.files[0]); input.value = ""; });
  window.addEventListener("beforeunload", (e) => { if (S.uploading) { e.preventDefault(); e.returnValue = ""; } });

  // ------------------------------------------------------------ chunked upload
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  function putChunk(id, offset, blob, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      S.upload.xhr = xhr;
      xhr.open("PUT", `/api/uploads/${id}/chunk?offset=${offset}`);
      xhr.setRequestHeader("Content-Type", "application/octet-stream");
      xhr.upload.onprogress = (e) => e.lengthComputable && onProgress(e.loaded);
      xhr.onload = () => {
        let body = null; try { body = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
        xhr.status === 200 ? resolve(body) : reject({ ...(body?.error || { code: "server", message: "Server error " + xhr.status }), status: xhr.status });
      };
      xhr.onerror = () => reject({ code: "network", message: "Network error during upload." });
      xhr.onabort = () => reject({ code: "aborted", message: "Upload cancelled." });
      xhr.send(blob);
    });
  }

  async function startUpload(file) {
    banner(null);
    const c = S.config;
    if (!c) return banner("The server is not ready yet. Please reload the page.");
    if (!c.ffmpeg.ok) return banner(c.ffmpeg.error);
    const ext = "." + file.name.split(".").pop().toLowerCase();
    if (!c.limits.extensions.includes(ext)) return banner("Unsupported file type. Please choose " + $("#formats").textContent + ".");
    if (file.size === 0) return banner("That file is empty.");
    if (file.size > c.limits.max_upload_bytes) return banner(`That file is ${fmtBytes(file.size)}, which is over the ${fmtBytes(c.limits.max_upload_bytes)} limit.`);

    S.upload = { cancelled: false, xhr: null, id: null };
    S.uploading = true;
    hide(dz); show($("#upload-progress")); hide($("#setup")); hide($("#failure"));
    $("#up-name").textContent = file.name; $("#up-size").textContent = fmtBytes(file.size);
    $("#up-text").textContent = "Starting upload…"; setBar($("#up-bar"), 0);
    const started = Date.now();
    try {
      const { upload } = await api("/api/uploads", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ filename: file.name, size: file.size }) });
      S.upload.id = upload.id;
      const chunk = c.limits.chunk_bytes;
      let offset = 0, attempts = 0;
      while (offset < file.size) {
        if (S.upload.cancelled) throw { code: "aborted" };
        const end = Math.min(offset + chunk, file.size);
        try {
          const base = offset;
          const res = await putChunk(upload.id, offset, file.slice(offset, end), (loaded) => progress(base + loaded));
          offset = res.received; attempts = 0; progress(offset);
        } catch (e) {
          if (S.upload.cancelled || e.code === "aborted") throw { code: "aborted" };
          if (e.status && e.status !== 409 && e.status < 500) throw e;        // permanent error (4xx): stop
          if (++attempts > 6) throw { code: "network", message: "Upload interrupted: the connection kept failing. Please try again." };
          $("#up-text").textContent = `Connection problem – retrying (${attempts}/6)…`;
          await sleep(Math.min(8000, 700 * 2 ** attempts));
          try { offset = (await api("/api/uploads/" + upload.id)).upload.received; } catch (_) { /* retry loop continues */ }
        }
      }
      $("#up-text").textContent = "Checking the video…"; setBar($("#up-bar"), 100);
      const done = await api(`/api/uploads/${upload.id}/complete`, { method: "POST" });
      S.uploading = false; S.info = done.upload.info; S.upload.id = done.upload.id;
      hide($("#upload-progress")); showSetup(done.upload);
    } catch (e) {
      S.uploading = false;
      if (e.code !== "aborted") banner(e.message || "Upload failed.");
      resetUpload();
    }

    function progress(sent) {
      const pct = (sent / file.size) * 100, secs = (Date.now() - started) / 1000;
      setBar($("#up-bar"), pct);
      $("#up-text").textContent = `Uploading… ${pct.toFixed(1)}%`;
      $("#up-speed").textContent = secs > 1 ? fmtBytes(sent / secs) + "/s" : "";
    }
  }

  $("#up-cancel").addEventListener("click", async () => {
    if (!S.upload) return;
    S.upload.cancelled = true; S.upload.xhr?.abort();
    if (S.upload.id) { try { await api("/api/uploads/" + S.upload.id, { method: "DELETE" }); } catch (_) { /* best effort */ } }
    resetUpload(); banner("Upload cancelled.", "warn");
  });

  function resetUpload() {
    S.uploading = false; hide($("#upload-progress")); show(dz); show($("#upload-card"));
  }

  // ------------------------------------------------------------ info + settings
  function outDims(v, aspect) {
    const tw = v.display_width >= v.display_height ? 1920 : 1080, th = tw === 1920 ? 1080 : 1920;
    const ratio = (v.display_width * (v.sar || 1)) / v.display_height;
    if (aspect !== "preserve" || Math.abs(ratio / (tw / th) - 1) < 0.012) return [tw, th];
    const s = Math.min(tw / (v.display_width * (v.sar || 1)), th / v.display_height);
    return [Math.floor((v.display_width * (v.sar || 1) * s) / 2) * 2, Math.floor((v.display_height * s) / 2) * 2];
  }

  function audioText(info) {
    if (!info.audio.length) return "No audio track";
    return info.audio.map((a) => `${(a.codec || "?").toUpperCase()} ${a.channels}ch ${a.sample_rate ? a.sample_rate / 1000 + " kHz" : ""}${a.bit_rate ? " " + Math.round(a.bit_rate / 1000) + " kbps" : ""}`.trim()).join(" · ");
  }

  function showSetup(upload) {
    const v = upload.info.video;
    fillGrid($("#info-grid"), [
      ["File", upload.filename], ["Original resolution", `${v.display_width}×${v.display_height}`],
      ["Duration", fmtTime(upload.info.duration)], ["File size", fmtBytes(upload.size)],
      ["Frame rate", v.fps ? v.fps.toFixed(2) + " fps" + (v.vfr ? " (variable)" : "") : "–"],
      ["Video codec", (v.codec || "").toUpperCase()], ["Audio", audioText(upload.info)],
      ["Output resolution", outDims(v, "preserve").join("×")],
    ]);
    show($("#setup")); hide($("#upload-card"));
    updateOutputNote();
    if (v.display_width >= 1920 && v.display_height >= 1080) banner("This video is already 1080p or larger. It will be resized, not upscaled.", "warn");
    $("#setup").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function updateOutputNote() {
    if (!S.info) return;
    const f = new FormData($("#settings")), v = S.info.video;
    const [w, h] = outDims(v, f.get("aspect"));
    let msg = `Output: ${w}×${h}. `;
    const bad = S.info.audio.filter((a) => !MP4_AUDIO.includes(a.codec));
    if (f.get("format") === "mp4" && bad.length) {
      msg += f.get("allow_audio_reencode") ? "Audio that MP4 cannot hold will be re-encoded to AAC." : "This video's audio can't be copied into MP4, so the file will be delivered as MKV to keep the audio untouched.";
    } else if (S.info.audio.length) msg += "Original audio will be copied without re-encoding.";
    $("#output-note").textContent = msg;
    const grid = $("#info-grid").lastElementChild; if (grid) grid.querySelector("dd").textContent = `${w}×${h}`;
  }
  $("#settings").addEventListener("change", updateOutputNote);

  $("#discard-btn").addEventListener("click", async () => {
    if (S.upload?.id) { try { await api("/api/uploads/" + S.upload.id, { method: "DELETE" }); } catch (_) { /* ignore */ } }
    S.info = null; hide($("#setup")); banner(null); resetUpload();
  });

  // ------------------------------------------------------------ conversion
  function gatherSettings() {
    const f = new FormData($("#settings"));
    return { mode: f.get("mode"), quality: f.get("quality"), aspect: f.get("aspect"), format: f.get("format"), encoder: f.get("encoder"), allow_audio_reencode: !!f.get("allow_audio_reencode") };
  }

  $("#settings").addEventListener("submit", async (e) => {
    e.preventDefault();
    await createJob(gatherSettings());
  });

  async function createJob(settings) {
    banner(null); hide($("#failure"));
    $("#convert-btn").disabled = true;
    try {
      const { job } = await api("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ upload_id: S.upload?.id || S.job?.upload_id, settings }) });
      S.job = job;
      try { sessionStorage.setItem("job", job.id); } catch (_) { /* private mode */ }
      hide($("#setup")); show($("#processing")); hide($("#result"));
      onJob(job); watch(job.id);
      $("#processing").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      banner(err.message);
    } finally { $("#convert-btn").disabled = false; }
  }

  function watch(id) {
    stopWatching();
    const poll = () => { S.poll = setInterval(async () => { try { onJob((await api("/api/jobs/" + id)).job); } catch (e) { if (e.code === "not_found") { stopWatching(); } } }, 1500); };
    if (!("EventSource" in window)) return poll();
    S.es = new EventSource(`/api/jobs/${id}/events`);
    S.es.onmessage = (m) => onJob(JSON.parse(m.data));
    S.es.onerror = () => { if (S.es) { S.es.close(); S.es = null; } if (!S.poll && S.job && !["finished", "failed", "cancelled"].includes(S.job.status)) poll(); };
  }
  function stopWatching() { S.es?.close(); S.es = null; clearInterval(S.poll); S.poll = null; }

  function onJob(job) {
    S.job = job;
    if (job.status === "finished") { stopWatching(); return showResult(job); }
    if (job.status === "failed") { stopWatching(); return showFailure(job); }
    if (job.status === "cancelled") {
      stopWatching(); try { sessionStorage.removeItem("job"); } catch (_) { /* ignore */ }
      hide($("#processing")); if (S.info) show($("#setup")); else resetUpload();
      return banner("Conversion cancelled. Temporary files were removed.", "warn");
    }
    show($("#processing"));
    const queued = job.status === "queued";
    $("#stage").textContent = queued ? "Waiting in queue" + (job.queue_position ? ` (position ${job.queue_position})` : "") : job.stage_label;
    setBar($("#proc-bar"), queued ? null : job.progress);
    $("#pct").textContent = job.progress == null || queued ? "…" : Math.floor(job.progress) + "%";   // floor: never shows 100% early
    $("#s-conv").textContent = job.stage_progress == null ? "Working…" : job.stage_progress.toFixed(1) + "%";
    $("#s-speed").textContent = job.speed ? job.speed.toFixed(2) + "× realtime" : "–";
    $("#s-eta").textContent = queued ? "–" : fmtEta(job.eta_seconds);
    renderNotes($("#notes"), job.notes);
  }

  function renderNotes(ul, notes) {
    ul.textContent = "";
    for (const n of notes || []) { const li = document.createElement("li"); li.textContent = n; ul.append(li); }
  }

  $("#cancel-job").addEventListener("click", async () => {
    if (!S.job) return;
    $("#cancel-job").disabled = true;
    try { onJob((await api(`/api/jobs/${S.job.id}/cancel`, { method: "POST" })).job); } catch (e) { banner(e.message); }
    $("#cancel-job").disabled = false;
  });

  // ------------------------------------------------------------ result / failure
  function showResult(job) {
    hide($("#processing")); hide($("#failure")); hide($("#setup")); hide($("#upload-card")); show($("#result"));
    const r = job.result, o = r.output, ov = job.source.video, nv = o.video;
    $("#compare").innerHTML = "";
    const box = (title, dims, sub) => { const d = document.createElement("div"), s = document.createElement("span"), b = document.createElement("b"), p = document.createElement("small"); s.className = "muted"; s.textContent = title; b.textContent = dims; p.className = "muted"; p.textContent = sub; d.append(s, b, p); return d; };
    const arrow = document.createElement("div"); arrow.className = "arrow"; arrow.textContent = "→"; arrow.setAttribute("aria-hidden", "true");
    $("#compare").append(box("Original", `${ov.display_width}×${ov.display_height}`, fmtBytes(job.source.size_bytes)), arrow, box("Converted", `${nv.width}×${nv.height}`, fmtBytes(o.size_bytes)));
    fillGrid($("#result-grid"), [
      ["Output file", o.filename], ["Output size", fmtBytes(o.size_bytes)], ["Conversion time", fmtTime(r.conversion_seconds)],
      ["Mode", r.mode === "ai" ? "AI Enhancement" : "Fast Upscaling"], ["Encoder", r.encoder], ["Container", r.container.toUpperCase()],
      ["Frame rate", nv.fps ? nv.fps.toFixed(2) + " fps" : "–"], ["Audio", audioText(o)],
    ]);
    const a = $("#audio-status"); a.textContent = r.audio.message; a.className = "audio-status" + (["copied", "none"].includes(r.audio.status) ? "" : " warn");
    renderNotes($("#result-notes"), job.notes);
    const v = $("#preview"); if (!v.getAttribute("src")) v.src = `/api/jobs/${job.id}/preview`;     // preload="none": nothing downloads until Play
    $("#preview-note").textContent = r.preview_note || "The preview streams only when you press play.";
    $("#download").href = `/api/jobs/${job.id}/download`; $("#download").setAttribute("download", o.filename);
    $("#expiry").textContent = "This file is stored temporarily and will be deleted on " + new Date(r.downloadable_until * 1000).toLocaleString() + ". Download it before then, or delete it yourself at any time.";
    banner(null);
  }

  function showFailure(job) {
    hide($("#processing")); show($("#failure"));
    $("#failure-text").textContent = job.error?.message || "The conversion failed.";
    hide($("#fallback-btn"), !(job.fallback_available && job.settings.mode === "ai"));
    try { sessionStorage.removeItem("job"); } catch (_) { /* ignore */ }
  }

  $("#fallback-btn").addEventListener("click", () => { hide($("#failure")); createJob({ ...S.job.settings, mode: "fast" }); });
  $("#failure-reset").addEventListener("click", () => { hide($("#failure")); S.info ? show($("#setup")) : resetUpload(); });

  function resetAll() {
    stopWatching(); S.job = null; S.info = null; S.upload = null;
    const v = $("#preview"); v.pause(); v.removeAttribute("src"); v.load();
    ["#result", "#processing", "#setup", "#failure"].forEach((s) => hide($(s)));
    try { sessionStorage.removeItem("job"); } catch (_) { /* ignore */ }
    resetUpload(); banner(null);
  }
  $("#again").addEventListener("click", resetAll);
  $("#delete-files").addEventListener("click", async () => {
    if (!S.job) return;
    try { await api("/api/jobs/" + S.job.id, { method: "DELETE" }); resetAll(); banner("Your files were deleted from the server.", "warn"); } catch (e) { banner(e.message); }
  });

  boot();
})();
