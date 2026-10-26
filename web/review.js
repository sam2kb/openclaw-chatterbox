(() => {
  "use strict";
  const prefix = document.querySelector('meta[name="clawvoice-base-path"]').content;
  const relative = location.pathname.slice(prefix.length);
  const match = relative.match(/^\/review\/(rev_[a-f0-9]{24})$/);
  const token = new URLSearchParams(location.hash.slice(1)).get("token");
  history.replaceState(null, "", location.pathname);
  const status = document.getElementById("review-status");
  const urls = [];
  const endpoint = match ? `${prefix}/api/v1/reviews/${match[1]}` : "";
  async function request(path, options = {}) {
    const result = await fetch(path, { ...options, headers: {Authorization: `Bearer ${token}`, ...(options.headers || {})}});
    if (!result.ok) throw new Error(result.status === 401 ? "This review link has expired or was revoked. Ask OpenClaw for a new link." : "The audio is unavailable. Ask OpenClaw to check this review.");
    return result;
  }
  function text(tag, value, parent) {
    const node = document.createElement(tag); node.textContent = value; parent.append(node); return node;
  }
  async function start() {
    if (!match || !token) throw new Error("Open the complete private review link supplied by OpenClaw.");
    const data = await (await request(endpoint)).json();
    status.textContent = data.expires_at ? `Draft review expires ${new Date(data.expires_at).toLocaleString()}. Keep your chosen master before this date.` : "Retained masters: this review has no automatic expiry. OpenClaw can revoke the link.";
    for (const [index, artifact] of data.artifacts.entries()) {
      const section = document.createElement("section"); section.className = "panel"; document.getElementById("takes").append(section);
      const quality = artifact.manifest.quality || {};
      text("h2", artifact.manifest.asset?.name || `Take ${index + 1}`, section);
      if (artifact.manifest.model?.real_model === false) text("p", "Test backend audio: this is not a real voice-quality sample.", section);
      text("p", `${(artifact.duration_ms / 1000).toFixed(2)} s · ${artifact.sample_rate} Hz · ${artifact.channels} channel · ${artifact.kind}`, section);
      text("p", artifact.expires_at ? "Draft: not retained as a production asset." : "Retained original: use this exact file without generating it again.", section);
      text("h3", "Requested wording", section); text("p", artifact.expected_text || "The original request text is no longer available.", section);
      text("h3", "Transcription check", section);
      text("p", quality.wording ? (quality.wording.status === "asr_agreement" ? "The automatic transcript agrees with the requested words. Listening is still required." : "The automatic transcript differs from the requested words. Listen before deciding whether it is speech or transcription error.") : "No transcription check recorded.", section);
      if (quality.wording) text("p", quality.wording.transcript, section);
      if (quality.signal) {
        const signal = quality.signal;
        text("p", `Signal check: ${signal.possible_clipping ? "possible clipping" : "no clipping flag"}; ${signal.very_quiet ? "very quiet" : "level measured"}. This does not judge naturalness or voice similarity.`, section);
      }
      const audio = document.createElement("audio"); audio.controls = true; audio.preload = "metadata"; section.append(audio);
      const reply = text("p", quality.listening ? `Listening decision: ${quality.listening.decision}` : "Listening decision: not reviewed", section);
      const response = await request(`${endpoint}/artifacts/${artifact.id}/download`);
      const bytes = await response.arrayBuffer();
      const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(v => v.toString(16).padStart(2, "0")).join("");
      if (digest !== artifact.sha256) throw new Error("Audio checksum mismatch. Ask OpenClaw to fetch a fresh copy.");
      const url = URL.createObjectURL(new Blob([bytes], {type: artifact.media_type})); urls.push(url); audio.src = url;
      const download = document.createElement("a"); download.className = "button secondary"; download.textContent = "Download original"; download.href = url; download.download = `${artifact.id}-original.wav`; section.append(download);
      for (const [decision, label] of [["accepted", "Wording and sound accepted"], ["rejected", "This take needs work"]]) {
        const button = text("button", label, section); button.className = "button ghost"; button.type = "button";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            await request(`${endpoint}/artifacts/${artifact.id}/listen`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({audio_sha256: artifact.sha256, decision})});
            reply.textContent = artifact.expires_at ? `Listening decision: ${decision}. Tell OpenClaw which take to keep as the production asset.` : `Listening decision: ${decision}. This original is retained.`;
          } catch (error) {reply.textContent = error.message;} finally {button.disabled = false;}
        });
      }
    }
  }
  start().catch(error => {status.textContent = error.message; status.setAttribute("role", "alert");});
  addEventListener("pagehide", () => urls.forEach(url => URL.revokeObjectURL(url)));
})();
