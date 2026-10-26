(() => {
  "use strict";

  const basePath = (document.querySelector('meta[name="clawvoice-base-path"]')?.content || "").replace(/\/$/, "");
  const serviceUrl = (path) => `${basePath}${path}`;

  const state = {
    sessionId: null,
    token: null,
    session: null,
    sampleBlob: null,
    sampleUrl: null,
    upload: null,
    recording: null,
    mediaRecorder: null,
    chunks: [],
    stream: null,
    audioContext: null,
    analyser: null,
    levelTimer: null,
    startedAt: 0,
    pollTimer: null,
    completed: false,
    disposed: false,
  };

  const $ = (id) => document.getElementById(id);
  const ui = {
    fatal: $("fatal"),
    expired: $("expired"),
    studio: $("studio"),
    samplePanel: $("sample-panel"),
    sessionBadge: $("session-badge"),
    recordButton: $("record-button"),
    stopButton: $("stop-button"),
    level: $("level"),
    timer: $("timer"),
    recordStatus: $("record-status"),
    recordedReview: $("recorded-review"),
    recordedAudio: $("recorded-audio"),
    rerecordButton: $("rerecord-button"),
    useRecordedButton: $("use-recorded-button"),
    tabRecord: $("tab-record"),
    tabUpload: $("tab-upload"),
    recordPanel: $("record-panel"),
    uploadPanel: $("upload-panel"),
    fileInput: $("file-input"),
    uploadReview: $("upload-review"),
    uploadedAudio: $("uploaded-audio"),
    useUploadButton: $("use-upload-button"),
    uploadStatus: $("upload-status"),
    clipPanel: $("clip-panel"),
    clipBadge: $("clip-badge"),
    checks: $("checks"),
    startRange: $("start-range"),
    endRange: $("end-range"),
    startOutput: $("start-output"),
    endOutput: $("end-output"),
    selection: $("selection"),
    chooseAgain: $("choose-again"),
    continueButton: $("continue-button"),
    savePanel: $("save-panel"),
    saveForm: $("save-form"),
    voiceName: $("voice-name"),
    authorized: $("authorized"),
    createButton: $("create-button"),
    saveStatus: $("save-status"),
    donePanel: $("done-panel"),
    doneCopy: $("done-copy"),
    auditionPending: $("audition-pending"),
    auditionProgress: $("audition-progress"),
    auditionAudio: $("audition-audio"),
    auditionStatus: $("audition-status"),
    downloadAudition: $("download-audition"),
    copyVoiceId: $("copy-voice-id"),
  };

  function show(element, visible = true) {
    element.classList.toggle("hidden", !visible);
  }

  function setStatus(element, message, kind = "") {
    element.textContent = message;
    element.classList.remove("error", "ok");
    if (kind) element.classList.add(kind);
  }

  function fatal(message) {
    show(ui.fatal);
    ui.fatal.textContent = message;
    show(ui.studio, false);
    show(ui.expired, false);
  }

  function parseError(body, fallback) {
    if (body && body.error && body.error.message) {
      return body.error.message;
    }
    return fallback;
  }

  async function api(path, options = {}, body = null) {
    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${state.token}`);
    if (body !== null) headers.set("Content-Type", "application/json");
    const response = await fetch(serviceUrl(path), {
      ...options,
      headers,
      body: body === null ? options.body : JSON.stringify(body),
    });
    let payload = null;
    try { payload = await response.json(); } catch (_) { payload = null; }
    if (!response.ok) {
      const error = new Error(parseError(payload, `Request failed (${response.status})`));
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function consumeFragment() {
    const hash = window.location.hash || "";
    if (!hash.startsWith("#token=")) return null;
    let token;
    try {
      token = decodeURIComponent(hash.slice("#token=".length));
    } catch (_) {
      return null;
    }
    try {
      history.replaceState(null, "", window.location.pathname + window.location.search);
    } catch (_) {
      window.location.hash = "";
    }
    return token || null;
  }

  function detectSession() {
    const path = window.location.pathname;
    const relativePath = basePath && path.startsWith(`${basePath}/`) ? path.slice(basePath.length) : path;
    const match = relativePath.match(/^\/enroll\/(enr_[0-9a-f]{24})$/);
    return match ? match[1] : null;
  }

  function stopTracks() {
    if (state.stream) {
      for (const track of state.stream.getTracks()) {
        try { track.stop(); } catch (_) { /* already stopped */ }
      }
      state.stream = null;
    }
    if (state.audioContext) {
      state.audioContext.close().catch(() => {});
      state.audioContext = null;
    }
    state.analyser = null;
    if (state.levelTimer) {
      clearInterval(state.levelTimer);
      state.levelTimer = null;
    }
    ui.level.value = 0;
  }

  function stopRecordingIfAny() {
    if (state.mediaRecorder && state.mediaRecorder.state !== "inactive") {
      try { state.mediaRecorder.stop(); } catch (_) { /* noop */ }
    }
    stopTracks();
  }

  function mimeCandidates() {
    return [
      "audio/webm;codecs=opus",
      "audio/webm",
      "audio/mp4;codecs=mp4a.40.2",
      "audio/mp4",
      "audio/ogg;codecs=opus",
      "audio/ogg",
    ];
  }

  function chooseMime() {
    if (!window.MediaRecorder || typeof MediaRecorder.isTypeSupported !== "function") return "";
    for (const candidate of mimeCandidates()) {
      if (MediaRecorder.isTypeSupported(candidate)) return candidate;
    }
    return "";
  }

  function tick() {
    const elapsed = Math.min(20, (performance.now() - state.startedAt) / 1000);
    ui.timer.textContent = `${elapsed.toFixed(1)} s`;
    if (state.analyser) {
      const data = new Uint8Array(state.analyser.fftSize);
      state.analyser.getByteTimeDomainData(data);
      let peak = 0;
      for (const point of data) peak = Math.max(peak, Math.abs(point - 128));
      ui.level.value = Math.min(100, Math.round((peak / 128) * 140));
    }
    if (elapsed >= 20) {
      stopRecording();
    }
  }

  function releaseReviewUrls() {
    if (state.sampleUrl) {
      URL.revokeObjectURL(state.sampleUrl);
      state.sampleUrl = null;
    }
  }

  async function startRecording() {
    if (!window.isSecureContext) {
      setStatus(ui.recordStatus, "Microphone access requires HTTPS or localhost. Use Upload sample or open the private HTTPS recorder URL.", "error");
      return;
    }
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus(ui.recordStatus, "This browser does not expose microphone recording. Use Upload sample.", "error");
      return;
    }
    const mimeType = chooseMime();
    if (!window.MediaRecorder) {
      setStatus(ui.recordStatus, "This browser does not support MediaRecorder. Use Upload sample.", "error");
      return;
    }
    setStatus(ui.recordStatus, "Waiting for microphone permission…");
    ui.recordButton.disabled = true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: 1,
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: false,
        },
      });
      if (state.disposed) {
        for (const track of stream.getTracks()) {
          try { track.stop(); } catch (_) { /* already stopped */ }
        }
        return;
      }
      state.stream = stream;
      state.chunks = [];
      state.recording = true;
      state.startedAt = performance.now();
      const recorder = mimeType ? new MediaRecorder(stream, { mimeType }) : new MediaRecorder(stream);
      state.mediaRecorder = recorder;
      recorder.addEventListener("dataavailable", (event) => {
        if (event.data && event.data.size > 0) state.chunks.push(event.data);
      });
      recorder.addEventListener("stop", finishRecording, { once: true });
      recorder.addEventListener("error", () => {
        setStatus(ui.recordStatus, "Recording failed. Try again, or upload a file.", "error");
        state.recording = false;
        ui.stopButton.hidden = true;
        ui.recordButton.disabled = false;
        stopTracks();
      });

      const AudioContext = window.AudioContext || window.webkitAudioContext;
      if (AudioContext) {
        state.audioContext = new AudioContext();
        const source = state.audioContext.createMediaStreamSource(stream);
        state.analyser = state.audioContext.createAnalyser();
        state.analyser.fftSize = 1024;
        source.connect(state.analyser);
      }
      recorder.start(250);
      ui.stopButton.hidden = false;
      show(ui.recordedReview, false);
      setStatus(ui.recordStatus, "Recording. Speak naturally, then stop.");
      tick();
      state.levelTimer = setInterval(tick, 100);
    } catch (error) {
      ui.recordButton.disabled = false;
      stopTracks();
      let message = "Microphone access was not granted. Allow access in the browser, or use Upload sample.";
      if (error && (error.name === "NotFoundError" || error.name === "DevicesNotFoundError")) {
        message = "No microphone was found. Connect one and try again, or use Upload sample.";
      } else if (error && error.name === "NotAllowedError") {
        message = "Microphone permission was denied. Allow it in the browser, or use Upload sample.";
      }
      setStatus(ui.recordStatus, message, "error");
    }
  }

  function finishRecording() {
    const recorderMime = state.mediaRecorder && state.mediaRecorder.mimeType;
    stopTracks();
    state.recording = false;
    ui.stopButton.hidden = true;
    ui.recordButton.disabled = false;
    const blob = new Blob(state.chunks, { type: recorderMime || "application/octet-stream" });
    state.chunks = [];
    if (!blob.size) {
      setStatus(ui.recordStatus, "The recording was empty. Try again or upload a file.", "error");
      return;
    }
    releaseReviewUrls();
    state.sampleBlob = blob;
    state.sampleUrl = URL.createObjectURL(blob);
    ui.recordedAudio.src = state.sampleUrl;
    show(ui.recordedReview);
    setStatus(ui.recordStatus, "Listen to the sample, then use it or record again.", "ok");
    ui.level.value = 0;
  }

  function stopRecording() {
    if (state.mediaRecorder && state.mediaRecorder.state === "recording") {
      state.mediaRecorder.stop();
    } else {
      stopTracks();
    }
  }

  function switchTab(which) {
    const record = which === "record";
    ui.tabRecord.classList.toggle("active", record);
    ui.tabUpload.classList.toggle("active", !record);
    ui.tabRecord.setAttribute("aria-selected", String(record));
    ui.tabUpload.setAttribute("aria-selected", String(!record));
    show(ui.recordPanel, record);
    show(ui.uploadPanel, !record);
    if (!record) stopRecordingIfAny();
  }

  function resetClipSelection(upload) {
    state.upload = upload;
    ui.clipBadge.textContent = "sample checked";
    ui.clipBadge.className = "badge good";
    const durationMs = upload.duration_ms;
    const suggested = upload.checks.suggested_segment || { start_ms: 0, end_ms: Math.min(durationMs, 15000) };
    const minEnd = 5001;
    let startMs = suggested.start_ms;
    let endMs = suggested.end_ms;
    if (endMs - startMs < minEnd) endMs = Math.min(durationMs, startMs + minEnd);
    ui.startRange.max = String(durationMs);
    ui.endRange.max = String(durationMs);
    ui.startRange.value = String(startMs);
    ui.endRange.value = String(endMs);
    renderChecks(upload.checks);
    renderSelection();
  }

  function seconds(ms) { return `${(ms / 1000).toFixed(1)} s`; }

  function renderSelection() {
    let startMs = Number(ui.startRange.value);
    let endMs = Number(ui.endRange.value);
    if (endMs <= startMs) endMs = Math.min(Number(ui.endRange.max), startMs + 100);
    const length = endMs - startMs;
    ui.startOutput.textContent = seconds(startMs);
    ui.endOutput.textContent = seconds(endMs);
    ui.selection.textContent = `Selected ${seconds(length)}. Recommended: 10–15 seconds. Minimum: just over 5 seconds.`;
    ui.selection.classList.remove("error");
    if (length < 5001) {
      ui.selection.textContent += " This is too short to continue.";
      ui.selection.classList.add("error");
    }
    if (length > 20000) {
      ui.selection.textContent += " The backend limit is 20 seconds.";
      ui.selection.classList.add("error");
    }
    ui.continueButton.disabled = length < 5001 || length > 20000;
  }

  function warningLabel(code) {
    const labels = {
      too_short: ["Short source", "The full recording is under 6 seconds, but the clip must still be over 5 seconds."],
      long_source_select_a_clip: ["Long source", "Choose a short representative clip; the model conditions on a bounded window."],
      outside_recommended_10_15_seconds: ["Outside recommended length", "10–15 seconds usually gives the most stable reference."],
      possible_clipping: ["Possible clipping", "The recording may peak too high. Re-record at a lower level if it sounds distorted."],
      very_quiet: ["Very quiet", "The recording is quiet. A louder, close-mic sample may work better."],
      mostly_silent: ["Mostly silent", "A large part of this file is silence. Select a section with continuous speech."],
      source_is_stereo_will_be_downmixed: ["Stereo downmix", "The selected clip will be mixed to mono; your original is untouched."],
      low_source_sample_rate: ["Low sample rate", "The source sample rate is low; quality may be limited."],
      speaker_identity_not_verified: ["Identity not verified", "This service does not automatically verify who is speaking. The consent attestation is required."],
      background_noise_not_removed: ["No automatic denoise", "Background noise is not removed. Use a quiet recording if possible."],
    };
    return labels[code] || [code.replaceAll("_", " "), ""];
  }

  function renderChecks(checks) {
    ui.checks.innerHTML = "";
    const metrics = checks.metrics || {};
    const summary = document.createElement("div");
    summary.className = "check";
    const strong = document.createElement("strong");
    strong.textContent = checks.suitable ? "Sample looks usable" : "Review this sample";
    const detail = document.createElement("span");
    detail.textContent = `${seconds(metrics.source_duration_ms || 0)} · ${metrics.source_sample_rate || "?"} Hz · ${metrics.source_channels || "?"} channel(s)`;
    summary.append(strong, detail);
    ui.checks.append(summary);
    for (const code of checks.warnings || []) {
      const [title, text] = warningLabel(code);
      if (["still_processing"].includes(code)) continue;
      const item = document.createElement("div");
      item.className = "check warn";
      const content = document.createElement("div");
      const t = document.createElement("strong");
      t.textContent = title;
      const c = document.createElement("span");
      c.textContent = text;
      content.append(t, c);
      item.append(content);
      ui.checks.append(item);
    }
  }

  async function uploadSample(blob, label) {
    if (!blob || !blob.size) {
      setStatus(ui.uploadStatus, "Choose a non-empty audio file first.", "error");
      return;
    }
    if (state.session && state.session.limits && blob.size > state.session.limits.max_upload_bytes) {
      setStatus(ui.uploadStatus, "This file is larger than the enrollment limit.", "error");
      return;
    }
    setStatus(ui.uploadStatus, "Uploading and checking the audio…");
    ui.useRecordedButton.disabled = true;
    ui.useUploadButton.disabled = true;
    try {
      const response = await fetch(serviceUrl(`/api/v1/enrollment-sessions/${state.sessionId}/uploads`), {
        method: "POST",
        headers: { Authorization: `Bearer ${state.token}`, "Content-Type": blob.type || "application/octet-stream" },
        body: blob,
      });
      let payload = null;
      try { payload = await response.json(); } catch (_) { payload = null; }
      if (!response.ok) throw new Error(parseError(payload, `Upload failed (${response.status})`));
      resetClipSelection(payload);
      show(ui.clipPanel);
      ui.clipPanel.scrollIntoView({ behavior: "smooth", block: "start" });
      setStatus(ui.uploadStatus, "Sample uploaded. Choose the reference clip below.", "ok");
      setStatus(ui.recordStatus, label || "Sample uploaded.");
    } catch (error) {
      setStatus(ui.uploadStatus, error.message || "The sample could not be used.", "error");
    } finally {
      ui.useRecordedButton.disabled = false;
      ui.useUploadButton.disabled = false;
    }
  }

  async function fetchSession() {
    const session = await api(`/api/v1/enrollment-sessions/${state.sessionId}`);
    state.session = session;
    return session;
  }

  function showCompleted(session) {
    state.completed = true;
    show(ui.studio);
    show(ui.samplePanel, false);
    show(ui.clipPanel, false);
    show(ui.savePanel, false);
    show(ui.donePanel);
    ui.doneCopy.textContent = `Saved “${session.name}”. The profile ID is ${session.voice_id || "unavailable"}.`;
    ui.voiceName.value = session.name;
    if (session.audition) pollAudition(session.audition);
  }

  async function pollAudition(job) {
    if (state.pollTimer) clearTimeout(state.pollTimer);
    if (!job) return;
    if (job.status === "succeeded") {
      show(ui.auditionPending, false);
      ui.auditionProgress.value = 100;
      await loadAudition(job);
      return;
    }
    if (job.status === "failed" || job.status === "canceled") {
      show(ui.auditionPending, false);
      setStatus(ui.auditionStatus, job.error ? `Audition failed: ${job.error.message}` : "The audition did not finish.", "error");
      return;
    }
    show(ui.auditionPending);
    ui.auditionProgress.value = job.status === "running" ? 55 : 20;
    setStatus(ui.auditionStatus, job.status === "running" ? "Rendering the audition…" : "Waiting for the inference worker…");
    state.pollTimer = setTimeout(async () => {
      try {
        const session = await fetchSession();
        if (session.audition) await pollAudition(session.audition);
      } catch (error) {
        setStatus(ui.auditionStatus, error.message, "error");
      }
    }, 1200);
  }

  async function loadAudition(job) {
    const artifact = (job.artifacts || []).find((a) => a.kind === "output") || (job.artifacts || [])[0];
    if (!artifact) {
      setStatus(ui.auditionStatus, "The audition succeeded but no audio artifact was returned.", "error");
      return;
    }
    const response = await fetch(serviceUrl(`/api/v1/enrollment-sessions/${state.sessionId}/artifacts/${artifact.id}/download`), {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    if (!response.ok) {
      setStatus(ui.auditionStatus, "The audition is ready but could not be downloaded.", "error");
      return;
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    ui.auditionAudio.src = url;
    show(ui.auditionAudio);
    ui.auditionAudio.play().catch(() => {});
    ui.downloadAudition.href = url;
    ui.downloadAudition.download = `clawvoice-audition-${artifact.id}.wav`;
    show(ui.downloadAudition);
    setStatus(ui.auditionStatus, "Audition ready. This is reference-conditioned synthesis, not a trained model.", "ok");
  }

  async function createVoice(event) {
    event.preventDefault();
    if (!state.upload) return;
    if (!ui.authorized.checked) {
      setStatus(ui.saveStatus, "Confirm the consent attestation before creating the voice.", "error");
      return;
    }
    const startMs = Number(ui.startRange.value);
    const endMs = Number(ui.endRange.value);
    setStatus(ui.saveStatus, "Creating the voice and queueing an audition…");
    ui.createButton.disabled = true;
    try {
      const payload = await api(`/api/v1/enrollment-sessions/${state.sessionId}/voices`, { method: "POST" }, {
        upload_id: state.upload.id,
        name: ui.voiceName.value.trim(),
        authorized: true,
        start_ms: Math.round(startMs),
        end_ms: Math.round(endMs),
      });
      state.completed = true;
      show(ui.samplePanel, false);
      show(ui.savePanel, false);
      show(ui.clipPanel, false);
      show(ui.donePanel);
      ui.doneCopy.textContent = `Saved “${payload.voice.name}”. The profile ID is ${payload.voice.id}.`;
      ui.copyVoiceId.dataset.voiceId = payload.voice.id;
      setStatus(ui.auditionStatus, "Audition queued. The worker keeps the model loaded for the next job.", "ok");
      await pollAudition(payload.audition_job);
      await fetchSession();
    } catch (error) {
      setStatus(ui.saveStatus, error.message || "The voice could not be created.", "error");
      ui.createButton.disabled = false;
    }
  }

  async function init() {
    state.sessionId = detectSession();
    state.token = consumeFragment();
    const secure = window.isSecureContext;
    if (!secure) {
      const notice = document.createElement("p");
      notice.textContent = "This page is not a secure context. Recording is disabled; upload still works if the link is trusted.";
      ui.fatal.after(notice);
    }
    if (!state.sessionId || !state.token) {
      show(ui.expired);
      ui.expired.querySelector("h2").textContent = "Open the private recorder link";
      ui.expired.querySelector("p").textContent = "This page needs the single-enrollment link supplied by OpenClaw. Ask OpenClaw to start enrollment.";
      show(ui.expired);
      return;
    }
    ui.sessionBadge.textContent = "validating link";
    try {
      const session = await fetchSession();
      if (session.status === "expired") {
        show(ui.expired);
        return;
      }
      if (session.status === "completed" && session.voice_id) {
        showCompleted(session);
        return;
      }
      show(ui.studio);
      ui.sessionBadge.textContent = `Single use · expires ${new Date(session.expires_at).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"})}`;
      ui.voiceName.value = session.name || "";
      ui.voiceName.readOnly = session.mode === "revise";
      ui.createButton.textContent = session.mode === "revise" ? "Update voice reference" : "Create voice";
      const note = document.createElement("p");
      note.className = "hint";
      note.textContent = session.mode === "revise"
        ? "This updates the selected voice. Its ID and earlier reference versions are kept."
        : "The name below is a suggestion. You can change it before saving this new voice.";
      ui.voiceName.after(note);
      if (session.upload) {
        resetClipSelection(session.upload);
        show(ui.clipPanel);
      }
      if (!secure) {
        ui.recordButton.disabled = true;
        ui.recordButton.title = "Microphone recording requires a secure context";
      }
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        ui.expired.querySelector("h2").textContent = "This recorder link cannot be used";
        ui.expired.querySelector("p").textContent = "The single-enrollment credential is missing, expired, or already invalid. Ask OpenClaw for a new link.";
      } else {
        setStatus(ui.recordStatus, error.message || "The service could not be reached.", "error");
      }
      show(ui.expired);
      show(ui.studio, false);
    }
  }

  ui.recordButton.addEventListener("click", startRecording);
  ui.stopButton.addEventListener("click", stopRecording);
  ui.rerecordButton.addEventListener("click", () => {
    releaseReviewUrls();
    state.sampleBlob = null;
    ui.recordedAudio.removeAttribute("src");
    show(ui.recordedReview, false);
    startRecording();
  });
  ui.useRecordedButton.addEventListener("click", () => uploadSample(state.sampleBlob, "Recorded sample uploaded."));
  ui.tabRecord.addEventListener("click", () => switchTab("record"));
  ui.tabUpload.addEventListener("click", () => switchTab("upload"));
  ui.fileInput.addEventListener("change", () => {
    const file = ui.fileInput.files && ui.fileInput.files[0];
    if (!file) {
      show(ui.uploadReview, false);
      return;
    }
    state.sampleBlob = file;
    if (state.sampleUrl) URL.revokeObjectURL(state.sampleUrl);
    state.sampleUrl = URL.createObjectURL(file);
    ui.uploadedAudio.src = state.sampleUrl;
    show(ui.uploadReview);
    setStatus(ui.uploadStatus, `${file.name} selected.`, "");
  });
  ui.useUploadButton.addEventListener("click", () => uploadSample(state.sampleBlob, "Uploaded sample accepted."));
  ui.startRange.addEventListener("input", () => {
    if (Number(ui.startRange.value) >= Number(ui.endRange.value)) {
      ui.endRange.value = String(Math.min(Number(ui.endRange.max), Number(ui.startRange.value) + 100));
    }
    renderSelection();
  });
  ui.endRange.addEventListener("input", () => {
    if (Number(ui.endRange.value) <= Number(ui.startRange.value)) {
      ui.startRange.value = String(Math.max(0, Number(ui.endRange.value) - 100));
    }
    renderSelection();
  });
  ui.continueButton.addEventListener("click", () => {
    show(ui.savePanel);
    ui.savePanel.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  ui.chooseAgain.addEventListener("click", () => {
    show(ui.clipPanel, false);
    show(ui.savePanel, false);
    state.upload = null;
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  ui.saveForm.addEventListener("submit", createVoice);
  ui.copyVoiceId.addEventListener("click", async () => {
    const id = ui.copyVoiceId.dataset.voiceId;
    if (!id) return;
    try {
      await navigator.clipboard.writeText(id);
      ui.copyVoiceId.textContent = "Copied";
      setTimeout(() => { ui.copyVoiceId.textContent = "Copy voice ID"; }, 1500);
    } catch (_) {
      setStatus(ui.auditionStatus, `Voice ID: ${id}`, "");
    }
  });
  window.addEventListener("pagehide", () => {
    state.disposed = true;
    stopRecordingIfAny();
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) state.disposed = false;
  });
  window.addEventListener("beforeunload", () => {
    state.disposed = true;
    stopRecordingIfAny();
  });

  init();
})();
