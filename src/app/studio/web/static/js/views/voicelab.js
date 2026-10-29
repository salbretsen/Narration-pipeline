// Reference voice lab: guided in-browser recording of a reference clip for a cloned narrator.
import { api, get, post, patch, html, setHTML, toast, toastError, busy, confirmDialog, fmtDate, player } from "../ui.js";
import { Mic, micSupport, listMics, encodeWav, roomMetrics, envelope, dbfs, BLUETOOTH_RE } from "../recorder.js";

const STEPS = [["setup", "1 · Set up"], ["room", "2 · Room check"], ["record", "3 · Record"], ["review", "4 · Review & audition"], ["create", "5 · Create narrator"]];
const zoneOf = (db) => (db > -3 ? "clip" : db > -12 ? "hot" : db > -30 ? "good" : "low");
const ICON = { pass: "✓", warn: "!", fail: "✗" };

function diffView(diff) {
  if (!diff || !diff.length) return "";
  return html`${diff.map((o) => o.op === "eq" ? html`<span>${o.ref} </span>` : o.op === "sub" ? html`<span class="d-sub"><del>${o.ref}</del><ins>${o.hyp}</ins></span> `
    : o.op === "del" ? html`<del class="d-del">${o.ref}</del> ` : html`<ins class="d-ins">${o.hyp}</ins> `)}`;
}

function qualityBlock(q) {
  return html`<div class="quality q-${q.verdict}"><div class="q-head"><span class="badge tone-${q.verdict === "poor" ? "bad" : q.verdict === "usable" ? "warn" : "good"}">${q.verdict.toUpperCase()}</span> ${q.summary}</div>
    <ul class="q-list">${q.checks.map((c) => html`<li class="qc-${c.status}"><span class="qi" aria-hidden="true">${ICON[c.status]}</span><span class="sr-only">${c.status}:</span>
      <strong>${c.name}</strong> ${c.detail}${c.tip && c.status !== "pass" ? html`<div class="small tip">${c.tip}</div>` : ""}</li>`)}</ul></div>`;
}

export function createLab({ onVoiceCreated }) {
  const el = document.createElement("section");
  el.className = "lab";
  el.setAttribute("aria-labelledby", "lab-h");
  const mic = new Mic();
  const L = { step: "setup", scripts: [], auditionText: "", takes: [], devices: [], deviceId: "", status: {}, room: null, scriptId: "core-harbour", cur: null, recState: "idle",
    countdown: 0, count: true, selected: null, jobs: {}, support: micSupport(), openErr: "", saved: null, saveErr: null, roomRun: null, ready: false, cmp: new Set(), compare: null, cmpErr: "" };
  let raf = 0, timers = new Set(), destroyed = false;

  const script = () => L.scripts.find((s) => s.id === L.scriptId);
  const micOn = () => mic.isOpen;

  // ------------------------------------------------------------------ live meter
  function updateLive() {
    const on = micOn(), db = dbfs(mic.peak), pct = on ? Math.max(0, Math.min(100, ((db + 60) / 60) * 100)) : 0;
    el.querySelectorAll(".meter-fill").forEach((f) => { f.style.width = pct + "%"; f.dataset.zone = zoneOf(db); });
    el.querySelectorAll(".meter-db").forEach((t) => { t.textContent = on ? `${db.toFixed(0)} dBFS` : "mic off"; });
    if (L.recState === "recording") {
      const t = mic.elapsed;
      el.querySelectorAll(".rec-timer").forEach((n) => { n.textContent = t.toFixed(1) + " s"; n.dataset.state = t < 5 ? "short" : t <= 20 ? "ok" : "long"; });
      if (t >= 30) stopRecording();
    }
  }
  function loop() { raf = 0; if (destroyed || !el.isConnected) return; updateLive(); if (micOn()) raf = requestAnimationFrame(loop); }
  function ensureLoop() { if (!raf && micOn()) raf = requestAnimationFrame(loop); }
  const later = (fn, ms) => { const t = setTimeout(() => { timers.delete(t); fn(); }, ms); timers.add(t); return t; };

  // ------------------------------------------------------------------ actions
  async function init() {
    try {
      const [sc, rec, st] = await Promise.all([get("/api/reference-scripts"), get("/api/recordings"), get("/api/auth/status")]);
      L.scripts = sc.scripts; L.auditionText = sc.audition_text; L.takes = rec.recordings; L.status = st;
      if (L.takes.length) L.step = "review";
    } catch (e) { toastError(e); }
    L.ready = true;
    paint();
  }

  async function openMic() {
    L.openErr = "";
    try {
      const label = await mic.open(L.deviceId);
      L.devices = await listMics();
      if (!L.deviceId) { const cur = L.devices.find((d) => d.label === label); if (cur) L.deviceId = cur.id; }
      ensureLoop();
    } catch (e) {
      L.openErr = e && e.name === "NotAllowedError" ? "The browser blocked the microphone. Click the lock/camera icon in the address bar, allow the microphone for this site, then try again."
        : e && e.name === "NotFoundError" ? "No microphone was found. Plug one in and try again." : `Could not open the microphone: ${e.message || e}`;
    }
    paint();
  }

  async function closeMic() { await mic.close(); paint(); }

  async function runRoomCheck() {
    if (!micOn()) return;
    L.room = null; L.roomRun = { n: 3 };
    paint();
    for (let n = 3; n >= 1; n--) { L.roomRun = { n }; paintRoomRun(); await new Promise((r) => later(r, 1000)); if (destroyed) return; }
    mic.start();
    const t0 = performance.now();
    while (performance.now() - t0 < 4000) { L.roomRun = { rec: ((performance.now() - t0) / 4000) * 100 }; paintRoomRun(); await new Promise((r) => later(r, 100)); if (destroyed) return; }
    const { samples, sampleRate } = mic.stop();
    L.room = roomMetrics(samples, sampleRate);
    L.roomRun = null;
    paint();
  }
  function paintRoomRun() {
    const n = el.querySelector("#room-run"); if (!n) return;
    const r = L.roomRun || {};
    n.textContent = r.n ? `Stay completely silent in ${r.n}…` : `Recording room tone… ${Math.round(r.rec || 0)}%`;
  }

  async function startRecording() {
    if (!micOn() || L.recState !== "idle") return;
    L.saved = null; L.saveErr = null;
    if (L.count) {
      L.recState = "countdown";
      for (let n = 3; n >= 1; n--) { L.countdown = n; paint(); await new Promise((r) => later(r, 1000)); if (destroyed || L.recState !== "countdown") return; }
    }
    L.recState = "recording";
    mic.start();
    paint();
    ensureLoop();
  }

  function stopRecording() {
    if (L.recState !== "recording") return;
    const { samples, sampleRate } = mic.stop();
    const wav = encodeWav(samples, sampleRate);
    if (L.cur && L.cur.url) URL.revokeObjectURL(L.cur.url);
    const text = (el.querySelector("#lab-text") || {}).value || (script() ? script().text : "");
    L.cur = { blob: wav, url: URL.createObjectURL(wav), seconds: samples.length / sampleRate, env: envelope(samples), local: roomMetrics(samples, sampleRate), text };
    L.recState = "recorded";
    paint();
    drawWave();
  }

  function discardTake() {
    if (L.cur && L.cur.url) URL.revokeObjectURL(L.cur.url);
    L.cur = null; L.recState = "idle"; L.saveErr = null; paint();
  }

  async function saveTake(btn) {
    const text = (el.querySelector("#lab-text") || {}).value.trim();
    const fd = new FormData();
    fd.append("file", L.cur.blob, "recording.wav");
    fd.append("script_id", L.scriptId === "own" ? "" : L.scriptId);
    fd.append("ref_text", text);
    try {
      const meta = await busy(btn, () => api("POST", "/api/recordings", fd));
      L.takes.unshift(meta); L.saved = meta; L.saveErr = null;
      if (L.cur && L.cur.url) URL.revokeObjectURL(L.cur.url);
      L.cur = null; L.recState = "idle";
    } catch (e) { L.saveErr = { message: e.message, quality: e.details && e.details.quality }; }
    paint();
  }

  async function runJob(rid, mode, btn) {
    const body = { mode };
    if (mode === "audition") body.text = (el.querySelector("#audition-text") || {}).value || L.auditionText;
    const key = `${rid}:${mode}`;
    try {
      const j = await busy(btn, () => post(`/api/recordings/${rid}/job`, body));
      L.jobs[key] = j; paintJob(rid, mode);
      const t = setInterval(async () => {
        try {
          const cur = await get(`/api/jobs/${j.id}`);
          L.jobs[key] = cur; paintJob(rid, mode);
          if (["completed", "failed", "cancelled"].includes(cur.status)) { clearInterval(t); timers.delete(t); }
        } catch (e) { clearInterval(t); timers.delete(t); toastError(e); }
      }, 1500);
      timers.add(t);
    } catch (e) { /* toast shown */ }
  }

  function jobHtml(rid, mode) {
    const j = L.jobs[`${rid}:${mode}`];
    if (!j) return "";
    if (j.status === "queued" || j.status === "running")
      return html`<p class="muted small"><span class="spinner" aria-hidden="true"></span> ${j.status === "queued" ? "Waiting for the GPU worker…" : mode === "check" ? "Listening with Whisper…" : "Cloning and synthesizing…"}</p>`;
    if (j.status === "failed") return html`<p class="err small">Failed: ${j.error}</p>`;
    const r = j.result || {};
    if (mode === "check") {
      return html`<div class="callout ${r.matches ? "good" : "warn"} small"><strong>${r.matches ? "Transcript matches what was said." : "Whisper heard something different."}</strong>
        <div class="diff">${diffView(r.diff)}</div><div class="muted">Whisper heard: “${r.asr_text}”</div>
        ${r.matches ? "" : html`<div>The clone learns the words you typed alongside your voice, so a wrong transcript damages the result. Edit the transcript to match exactly what you said, or re-record.</div>`}</div>`;
    }
    return html`<div class="callout info small">${player(`/media/preview/${j.id}`, "Audition result")}
      <div>Whisper agreement with the text: ${(100 - r.wer * 100).toFixed(0)}% · ${r.duration_s}s · ${r.engine}${r.rtf ? ` · RTF ${r.rtf.toFixed(2)}` : ""}</div>
      ${r.note ? html`<div class="warn-text">${r.note}</div>` : ""}<div>Does it sound like <em>you</em>, at the pace and warmth you want for the whole book? If not, try another take.</div></div>`;
  }
  function paintJob(rid, mode) {
    const n = el.querySelector(`[data-job="${rid}:${mode}"]`); if (n) setHTML(n, jobHtml(rid, mode));
    const c = el.querySelector("#cmp-table"); if (c && L.compare) setHTML(c, compareTable());
  }

  // ------------------------------------------------------------------ multi-take comparison
  async function runCompare() {
    const ids = L.takes.map((t) => t.id).filter((id) => L.cmp.has(id));
    if (ids.length < 2) { L.compare = null; L.cmpErr = "Tick at least two takes to compare."; paint(); return; }
    L.cmpErr = "";
    L.compare = await get(`/api/recordings/compare?ids=${ids.join(",")}`);
    paint();
  }
  async function checkAll(btn) {
    for (const r of (L.compare ? L.compare.rows : [])) await runJob(r.id, "check", btn);
  }
  async function uploadTake(form, btn) {
    const fd = new FormData(form);
    const file = fd.get("file");
    if (!file || !file.size) { toast("Choose an audio file first.", "error"); return; }
    try {
      const meta = await busy(btn, () => api("POST", "/api/recordings", fd));
      L.takes.unshift(meta); L.cmp.add(meta.id); L.compare = null;
      toast(`Take added (${meta.quality.verdict}). Tick takes and press Compare.`, "success");
    } catch (e) { toastError(e); }
    paint();
  }
  function matchCell(id) {
    const j = L.jobs[`${id}:check`];
    if (!j) return html`<span class="muted">not checked</span>`;
    if (j.status === "failed") return html`<span class="err">failed</span>`;
    if (j.status !== "completed") return html`<span class="muted">checking…</span>`;
    const r = j.result || {};
    return html`<span class="badge tone-${r.matches ? "good" : "warn"}">${r.matches ? "matches" : "differs"}</span> <span class="small muted">WER ${((r.wer || 0) * 100).toFixed(0)}%</span>`;
  }
  function compareTable() {
    const rows = [...L.compare.rows].sort((a, b) => a.rank - b.rank);
    return html`<table class="table cmp"><caption class="sr-only">Takes ranked by advisory quality score</caption>
      <thead><tr><th scope="col">Take</th><th scope="col">Verdict</th><th scope="col">Length</th><th scope="col">Peak</th><th scope="col">Clipping</th>
        <th scope="col">Noise floor</th><th scope="col">Voice-to-noise</th><th scope="col">Transcript</th><th scope="col">Listen</th><th scope="col">Choose</th></tr></thead>
      <tbody>${rows.map((r) => html`<tr class="${r.recommended ? "chosen" : ""}"><th scope="row">#${r.rank} ${r.label || r.script_title}${r.recommended ? html` <span class="badge tone-good">suggested</span>` : ""}</th>
        <td><span class="badge tone-${r.verdict === "poor" ? "bad" : r.verdict === "usable" ? "warn" : "good"}">${r.verdict}</span></td>
        <td>${Number(r.duration_s).toFixed(1)} s</td><td>${Number(r.peak_dbfs).toFixed(0)} dBFS</td><td>${r.clip_ratio > 0.0005 ? html`<span class="err">clipped</span>` : "none"}</td>
        <td>${Number(r.noise_floor_dbfs).toFixed(0)} dBFS</td><td>${r.snr_db} dB</td><td>${matchCell(r.id)}</td>
        <td>${player(`/media/recording/${r.id}`, `Take #${r.rank}`)}</td>
        <td><button class="btn small primary" data-act="use-take" data-id="${r.id}" ${r.verdict === "poor" ? html`disabled title="Rated poor quality"` : ""}>Use for narrator →</button></td></tr>`)}</tbody></table>`;
  }
  function comparePanel() {
    return html`<section class="compare" aria-labelledby="cmp-h"><h4 id="cmp-h">Compare takes side by side</h4>
      <p class="small muted">Tick two or more takes below (or add one from a file), then press Compare. The suggestion is advisory: it ranks by quality verdict, warnings and voice-to-noise,
        so always listen. Run the Whisper check on all to see which transcripts match what was actually said.</p>
      <form id="upload-form" class="form lab-row"><label>Audio file <input type="file" name="file" accept="audio/*,.wav,.mp3,.m4a,.flac"></label>
        <label>Exact words <input name="ref_text" required minlength="5" maxlength="1000" placeholder="What is said in the file"></label>
        <label>Label <input name="label" maxlength="80" placeholder="optional"></label>
        <button class="btn small" type="submit">Add take from file</button></form>
      <div class="form-actions"><button class="btn primary" data-act="cmp-run" ${L.cmp.size >= 2 ? "" : html`disabled`}>Compare ${L.cmp.size} take${L.cmp.size === 1 ? "" : "s"}</button>
        ${L.compare ? html`<button class="btn" data-act="cmp-check">Whisper-check all</button>` : ""}</div>
      ${L.cmpErr ? html`<div class="callout warn small" role="alert">${L.cmpErr}</div>` : ""}
      ${L.compare ? html`<div id="cmp-table" aria-live="polite">${compareTable()}</div>` : ""}</section>`;
  }

  async function createNarrator(form, btn) {
    const fd = new FormData(form);
    if (fd.get("owner_attest") !== "on") { toast("Please confirm the permission statement first.", "error"); return; }
    try {
      const v = await busy(btn, () => post("/api/voices/from-recording", { recording_id: L.selected, name: fd.get("name"), rights_holder: fd.get("rights_holder"),
        owner_attest: true, statement: fd.get("statement") || "" }));
      toast(`Narrator '${v.name}' created from your recording. Preview it below.`, "success", 8000);
      (v.warnings || []).forEach((w) => toast(w, "warn", 9000));
      L.step = "review"; L.selected = null;
      paint();
      onVoiceCreated(v);
    } catch (e) { /* toast shown */ }
  }

  // ------------------------------------------------------------------ rendering
  function guidance() {
    return html`<details class="guide" ${L.step === "setup" ? html`open` : ""}><summary>Before you start: how to get a great reference</summary>
      <div class="guide-grid">
        <div><h4>The room</h4><ul>
          <li>Pick the <strong>quietest room</strong> you have. Switch off fans, air conditioning and anything that hums; close windows; silence phones.</li>
          <li><strong>Soft surfaces beat bare walls.</strong> Curtains, rugs, sofas, or even recording inside a wardrobe of hanging clothes removes the echo a clone would otherwise copy.</li>
          <li>Record at a quiet time of day (late evening is often best).</li></ul></div>
        <div><h4>The microphone</h4><ul>
          <li>A USB microphone is ideal; a laptop's built-in microphone works if the room is quiet.</li>
          <li><strong>Avoid Bluetooth headsets and AirPods.</strong> They compress the sound to phone quality.</li>
          <li>Keep your mouth <strong>a hand-span (15-20 cm)</strong> from the mic, slightly off to one side to avoid "p" and "b" pops. Stay at the same distance all session.</li>
          <li>Set the input level so loud words peak in the <strong>green/amber zone</strong>, never in the red. The studio switches echo cancellation, noise suppression and auto-gain <em>off</em> on purpose.</li></ul></div>
        <div><h4>Your delivery</h4><ul>
          <li>Read as if <strong>narrating to one listener</strong>: warm, steady, about the pace you would use for an audiobook. Don't act, whisper or shout.</li>
          <li>Start speaking half a second after pressing record; leave half a second at the end.</li>
          <li>If you stumble, stop and <strong>re-take</strong>. Sip water; speak from the same posture each time.</li>
          <li>The clone copies your pace, pitch, energy and even the room, so <strong>record how you want the whole book to sound</strong>.</li></ul></div>
        <div><h4>What makes a good reference</h4><ul>
          <li>One speaker, <strong>8-15 seconds</strong> of continuous speech, no music, no echo, no clipping.</li>
          <li>The transcript must match your words <strong>exactly</strong>. Use the scripts provided, or read a page from your own book (best match for the real work).</li>
          <li>Record <strong>2-4 takes</strong> of the core scripts, audition them as a narrator, and keep the best.</li></ul></div>
      </div></details>`;
  }

  function secureNotice() {
    const s = L.support, st = L.status || {};
    if (s.ok) return "";
    const port = st.https_port;
    const httpsUrl = port ? `https://${location.hostname}:${port}/${location.hash}` : null;
    return html`<div class="callout warn" role="alert"><strong>Your browser has disabled the microphone on this address.</strong>
      <p>Browsers only allow recording on <em>secure</em> pages (https:// or localhost). This page is plain http:// on the network.</p>
      <ul>
        ${httpsUrl ? html`<li><strong>Easiest:</strong> open the secure address <a href="${httpsUrl}">${httpsUrl}</a>. The first time, the browser warns that the certificate is not trusted: that is expected for a self-signed certificate on your own network. Choose <em>Advanced → Continue</em>, or install the certificate (below) to remove the warning.</li>`
          : html`<li>HTTPS is not enabled on this studio. On the Mac run the installer with <code>--lan</code> (enables HTTPS on port 8443) or set <code>server.https_enabled</code>.</li>`}
        <li>Or open the studio on the Mac itself at <code>http://localhost:${location.port || 80}</code>, which browsers treat as secure.</li>
        <li>Or record with Voice Memos / your phone, then use <em>Clone an authorised voice</em> below to upload the file.</li></ul>
      ${st.cert_sha256 ? html`<details><summary>Trust the studio's certificate (removes the warning)</summary>
        <p><a href="/tls/studio-certificate.pem" download>Download the certificate</a>. SHA-256 fingerprint: <code>${st.cert_sha256}</code></p>
        <ul class="small"><li><strong>Windows:</strong> double-click the file → Install Certificate → Current User → Place in <em>Trusted Root Certification Authorities</em>. Restart the browser.</li>
        <li><strong>Mac:</strong> open it in Keychain Access, then double-click it and set Trust → <em>Always Trust</em>.</li></ul></details>` : ""}</div>`;
  }

  function stepper() {
    return html`<ol class="steps" role="tablist" aria-label="Reference voice steps">${STEPS.map(([k, label]) => html`<li><button type="button" role="tab" class="step ${L.step === k ? "active" : ""}"
      aria-selected="${L.step === k}" data-step="${k}">${label}${k === "review" && L.takes.length ? html` <span class="badge tone-neutral">${L.takes.length}</span>` : ""}</button></li>`)}</ol>`;
  }

  function meter() {
    return html`<div class="meter-wrap"><div class="meter" role="img" aria-label="Microphone level"><div class="meter-fill" data-zone="low"></div></div>
      <div class="meter-scale small muted"><span>quiet</span><span class="meter-db">mic off</span><span>loud</span></div></div>`;
  }

  function setupPanel() {
    const label = mic.label;
    return html`${secureNotice()}${guidance()}
      <div class="lab-panel"><h3>Choose and test your microphone</h3>
        ${L.support.ok ? html`
          <div class="lab-row"><label>Microphone <select id="lab-device" ${micOn() ? "" : html`aria-describedby="lab-hint"`}>
            <option value="">System default</option>${L.devices.map((d) => html`<option value="${d.id}" ${d.id === L.deviceId ? html`selected` : ""}>${d.label}</option>`)}</select></label>
            ${micOn() ? html`<button class="btn" data-act="mic-off">Turn off</button>` : html`<button class="btn primary" data-act="mic-on">Turn on microphone</button>`}</div>
          <p id="lab-hint" class="small muted">The browser will ask for permission. Nothing is recorded or sent until you press Record.</p>`
          : html`<p class="muted">Microphone recording is unavailable until the page is opened securely (see the notice above).</p>`}
        ${L.openErr ? html`<div class="callout bad small" role="alert">${L.openErr}</div>` : ""}
        ${micOn() ? html`${BLUETOOTH_RE.test(label) ? html`<div class="callout warn small">“${label}” looks like a Bluetooth or headset microphone. These sound thin and compressed; a wired or USB microphone gives a far better clone.</div>` : ""}
          <p class="small">Using: <strong>${label || "default microphone"}</strong> · ${mic.sampleRate} Hz</p>${meter()}
          <p class="small muted">Say a sentence at your normal narrating volume. The bar should sit in the <strong>green</strong> zone and only brush amber on loud words, never red. If it is too low, move closer or raise the input volume in your system sound settings.</p>` : ""}
        <div class="form-actions"><button class="btn primary" data-step="room" ${micOn() ? "" : html`disabled`}>Next: check the room →</button></div>
      </div>`;
  }

  function roomPanel() {
    const r = L.room;
    let verdict = "";
    if (r) {
      const f = r.noiseFloor, cls = f <= -50 ? "good" : f <= -42 ? "warn" : "bad";
      verdict = html`<div class="callout ${cls} small"><strong>Room noise: ${f.toFixed(0)} dBFS (${f <= -55 ? "very quiet" : f <= -50 ? "quiet - good" : f <= -42 ? "audible background" : "too noisy"})</strong>
        <div>${f <= -50 ? "This room is fine for a reference recording." : f <= -42 ? "Usable, but try switching off fans/air conditioning, closing windows, or moving to a smaller, softer room for a better clone." : "A clone will reproduce this noise. Find a quieter room or time before recording."}</div>
        <div class="muted">Loudest sound while you were silent: ${r.peak.toFixed(0)} dBFS</div></div>`;
    }
    return html`<div class="lab-panel"><h3>Check the room</h3>
      <p>Record four seconds of <strong>total silence</strong> so the studio can measure your room's background noise. Don't speak, breathe quietly, and keep the room as it will be during recording.</p>
      ${micOn() ? meter() : html`<div class="callout warn small">Turn on the microphone in step 1 first.</div>`}
      <div class="form-actions"><button class="btn primary" data-act="room-run" ${micOn() && !L.roomRun ? "" : html`disabled`}>Measure room noise</button>
        <span id="room-run" class="muted" aria-live="polite">${L.roomRun ? "…" : ""}</span></div>
      ${verdict}
      <div class="form-actions"><button class="btn ghost" data-step="setup">← Back</button><button class="btn primary" data-step="record" ${micOn() ? "" : html`disabled`}>Next: record →</button></div></div>`;
  }

  function recordPanel() {
    const s = script(), own = L.scriptId === "own";
    const text = own ? "" : s ? s.text : "";
    const core = L.scripts.filter((x) => x.tier === "core"), range = L.scripts.filter((x) => x.tier === "range");
    const rs = L.recState;
    return html`<div class="lab-panel"><h3>Record a take</h3>
      ${micOn() ? "" : html`<div class="callout warn small">Turn on the microphone in step 1 first.</div>`}
      <label>Script <select id="lab-script" ${rs === "idle" ? "" : html`disabled`}>
        <optgroup label="Recommended - neutral narration (record these first)">${core.map((x) => html`<option value="${x.id}" ${x.id === L.scriptId ? html`selected` : ""}>${x.title} (~${Math.round(x.est_seconds)}s)</option>`)}</optgroup>
        <optgroup label="Optional emotional range (separate variants only - read the note in step 4)">${range.map((x) => html`<option value="${x.id}" ${x.id === L.scriptId ? html`selected` : ""}>${x.title} (~${Math.round(x.est_seconds)}s)</option>`)}</optgroup>
        <optgroup label="Your own text"><option value="own" ${own ? html`selected` : ""}>Read a page from my own book (best match)</option></optgroup></select></label>
      ${s ? html`<p class="small muted"><strong>${s.category}.</strong> ${s.purpose}</p>` : html`<p class="small muted">Read 40-60 words from the book you will narrate, in the voice you want. Type exactly what you read below after recording.</p>`}
      <div class="prompter ${rs === "recording" ? "live" : ""}" aria-live="off">${rs === "countdown" ? html`<div class="count">${L.countdown}</div>` : ""}
        <p class="prompt-text">${own ? "Read your chosen passage naturally. Then type it exactly in the box below." : text}</p></div>
      ${micOn() ? meter() : ""}
      <div class="rec-controls">
        ${rs === "idle" || rs === "countdown" ? html`<button class="btn primary rec-btn" data-act="rec-start" ${micOn() && rs === "idle" ? "" : html`disabled`}>● Record</button>
          <label class="check"><input type="checkbox" id="lab-count" ${L.count ? html`checked` : ""}> 3-2-1 countdown</label>` : ""}
        ${rs === "recording" ? html`<button class="btn danger rec-btn" data-act="rec-stop">■ Stop</button><span class="rec-timer" data-state="short" aria-live="off">0.0 s</span><span class="small muted">aim for 8-15 s</span>` : ""}
        ${rs === "recorded" ? html`<button class="btn" data-act="rec-discard">↺ Re-record</button>` : ""}</div>
      ${rs === "recorded" && L.cur ? takePreview(own) : ""}
      ${L.saved ? html`<div class="callout good small"><strong>Take saved.</strong> ${qualityBlock(L.saved.quality)}
        <div class="form-actions"><button class="btn primary" data-step="review">Review &amp; audition →</button><button class="btn" data-act="rec-another">Record another take</button></div></div>` : ""}
      ${L.saveErr ? html`<div class="callout bad small" role="alert"><strong>${L.saveErr.message}</strong>${L.saveErr.quality ? qualityBlock(L.saveErr.quality) : ""}</div>` : ""}
      <div class="form-actions"><button class="btn ghost" data-step="room">← Back</button><button class="btn ghost" data-step="review">Skip to review →</button></div></div>`;
  }

  function takePreview(own) {
    const c = L.cur, lo = c.local;
    const warn = [];
    if (c.seconds < 5) warn.push("That take is quite short; 8-15 seconds is ideal.");
    if (c.seconds > 25) warn.push("That take is long; trim to 8-15 seconds if you can.");
    if (lo.peak > -1) warn.push("It clipped: the loudest peak hit the limit. Lower the input volume or move back.");
    if (lo.noiseFloor > -42) warn.push("Background noise is high in this take.");
    return html`<div class="take-preview"><canvas id="wave" width="800" height="90" role="img" aria-label="Waveform of your take"></canvas>
      <audio class="player" controls src="${c.url}" aria-label="Your take"></audio>
      <p class="small muted">${c.seconds.toFixed(1)} s · peak ${lo.peak.toFixed(0)} dBFS · noise floor ${lo.noiseFloor.toFixed(0)} dBFS. Listen first: no stumbles, clicks, breaths over words or background sounds?</p>
      ${warn.map((w) => html`<div class="callout warn small">${w}</div>`)}
      <label>Exact words you said <textarea id="lab-text" rows="4" maxlength="1000">${own ? c.text : c.text}</textarea></label>
      <p class="small muted">This must match your recording word for word. If you changed or skipped words, edit it here (or re-record). The studio can double-check it with Whisper after saving.</p>
      <div class="form-actions"><button class="btn primary" data-act="rec-save">Save this take &amp; analyse</button><button class="btn ghost" data-act="rec-discard">Discard</button></div></div>`;
  }

  function drawWave() {
    const cv = el.querySelector("#wave"); if (!cv || !L.cur) return;
    const g = cv.getContext("2d"), w = cv.width, h = cv.height, e = L.cur.env;
    g.clearRect(0, 0, w, h);
    const cs = getComputedStyle(el);
    g.fillStyle = cs.getPropertyValue("--accent").trim() || "#3559d6";
    const bw = w / e.length;
    for (let i = 0; i < e.length; i++) { const bh = Math.max(1, e[i] * h * 0.95); g.fillRect(i * bw, (h - bh) / 2, Math.max(1, bw - 0.5), bh); }
  }

  function takeCard(t) {
    const sel = L.selected === t.id;
    return html`<article class="take ${sel ? "chosen" : ""}" aria-labelledby="t-${t.id}">
      <header class="take-head"><label class="check"><input type="checkbox" data-cmp="${t.id}" ${L.cmp.has(t.id) ? html`checked` : ""} aria-label="Compare this take"> compare</label><h4 id="t-${t.id}">${t.script_title}</h4><span class="badge tone-${t.quality.verdict === "poor" ? "bad" : t.quality.verdict === "usable" ? "warn" : "good"}">${t.quality.verdict}</span>
        <span class="small muted">${t.duration_s}s · ${fmtDate(t.created_at)}</span></header>
      ${player(`/media/recording/${t.id}`, `Take: ${t.script_title}`)}
      <details><summary>Quality report</summary>${qualityBlock(t.quality)}</details>
      <p class="small">Transcript: “${t.ref_text}” <button class="linklike" data-act="edit-text" data-id="${t.id}">edit</button></p>
      <div class="form-actions">
        <button class="btn small" data-act="job-check" data-id="${t.id}">Verify words with Whisper</button>
        <button class="btn small" data-act="job-audition" data-id="${t.id}">Audition as narrator</button>
        <button class="btn small primary" data-act="use-take" data-id="${t.id}" ${t.quality.verdict === "poor" ? html`disabled title="Rated poor quality"` : ""}>Use for narrator →</button>
        <button class="btn small ghost danger" data-act="del-take" data-id="${t.id}">Delete</button></div>
      <div data-job="${t.id}:check" aria-live="polite">${jobHtml(t.id, "check")}</div>
      <div data-job="${t.id}:audition" aria-live="polite">${jobHtml(t.id, "audition")}</div></article>`;
  }

  function reviewPanel() {
    return html`<div class="lab-panel"><h3>Review your takes and audition them</h3>
      ${L.takes.length ? html`
        <label>Audition passage (the same text is synthesized from each take so you can compare fairly)
          <textarea id="audition-text" rows="3" maxlength="600">${L.auditionText}</textarea></label>
        ${comparePanel()}<div class="takes">${L.takes.map(takeCard)}</div>`
        : html`<p class="muted">No takes yet. <button class="linklike" data-step="record">Record your first take</button>.</p>`}
      <details class="guide"><summary>Should I record several takes? Should they differ in emotion?</summary>
        <ul>
          <li><strong>Several takes of the same neutral script:</strong> yes, worth it. Small differences in energy, mic distance and breath change the clone noticeably. Audition 2-4 takes with the same test passage and keep the one that sounds most like you at the pace and warmth you want.</li>
          <li><strong>One narrator uses one reference clip.</strong> The model conditions on a single clip, so extra takes are <em>candidates</em>, not something blended together.</li>
          <li><strong>Emotional range:</strong> keep the narrator's reference neutral-to-warm and consistent. A clone tends to inherit the <em>delivery</em> of its reference, so an excited or whispered reference makes everything excited or whispered. If a book needs a very different mood, create a separate narrator from an emotional take and choose it for that book.
            <span class="muted">(Measured on this studio's Base model with synthetic references: the clone's pitch variation matched its reference almost one-to-one, and a joyful reference gave about twice the pitch variation of a neutral one. A single clip that <em>mixed</em> two moods gave an in-between, hard-to-predict result.)</span></li>
          <li><strong>Why not combine clips?</strong> You can join two short clips into one reference (up to 30 seconds), but the result is a blend you cannot control. Prefer one clean, consistent clip.</li></ul></details>
      <div class="form-actions"><button class="btn ghost" data-step="record">← Record more</button></div></div>`;
  }

  function createPanel() {
    const t = L.takes.find((x) => x.id === L.selected);
    return html`<div class="lab-panel"><h3>Create the cloned narrator</h3>
      ${!t ? html`<p class="muted">Choose a take first: in step 4 press <strong>Use for narrator</strong>.</p><button class="btn" data-step="review">← To your takes</button>`
        : html`<p>Using <strong>${t.script_title}</strong> (${t.duration_s}s, quality <strong>${t.quality.verdict}</strong>)</p>${player(`/media/recording/${t.id}`, "Chosen take")}
        <form id="create-form" class="form">
          <div class="form-grid"><label>Narrator name <input name="name" required maxlength="80" placeholder="e.g. My voice - warm"></label>
            <label>Whose voice is this? <input name="rights_holder" required maxlength="120" placeholder="Full name of the speaker"></label></div>
          <label>Permission details (optional) <input name="statement" maxlength="1000" placeholder="e.g. My own voice, recorded by me"></label>
          <label class="check consent"><input type="checkbox" name="owner_attest" required> I confirm this is my own voice, or I have the speaker's explicit permission
            to create and commercially use a synthetic copy of it. I will not use this tool to impersonate anyone.</label>
          <div class="form-actions"><button class="btn ghost" type="button" data-step="review">← Back</button><button class="btn primary" type="submit">Create cloned narrator</button></div>
        </form>`}</div>`;
  }

  function paint() {
    if (destroyed) return;
    const focusId = document.activeElement && el.contains(document.activeElement) ? document.activeElement.id : "";
    const body = { setup: setupPanel, room: roomPanel, record: recordPanel, review: reviewPanel, create: createPanel }[L.step]();
    setHTML(el, html`<h3 id="lab-h">Reference voice lab <span class="badge tone-info">record in your browser</span></h3>
      <p class="muted">Develop a reference voice for a cloned narrator by <strong>recording it right here</strong>. The lab walks you through setup, a room check, guided recording with sample scripts,
        quality analysis, a Whisper transcript check and an audition, before you create the narrator. Recordings stay on this Mac.</p>
      ${stepper()}${L.ready ? body : html`<p class="muted">Loading…</p>`}`);
    if (focusId) { const n = el.querySelector("#" + focusId); if (n) n.focus(); }
    updateLive(); ensureLoop();
    if (L.recState === "recorded") drawWave();
  }

  // ------------------------------------------------------------------ events
  el.addEventListener("click", async (e) => {
    const st = e.target.closest("[data-step]");
    if (st && el.contains(st) && !st.disabled) {
      L.step = st.dataset.step;
      if (L.step === "room" || L.step === "record") ensureLoop();
      paint(); const h = el.querySelector(".lab-panel h3"); if (h) { h.setAttribute("tabindex", "-1"); h.focus(); }
      return;
    }
    const b = e.target.closest("[data-act]"); if (!b || !el.contains(b)) return;
    const act = b.dataset.act, id = b.dataset.id;
    try {
      if (act === "mic-on") { const sel = el.querySelector("#lab-device"); L.deviceId = sel ? sel.value : ""; await busy(b, openMic); }
      else if (act === "mic-off") await closeMic();
      else if (act === "room-run") await runRoomCheck();
      else if (act === "rec-start") await startRecording();
      else if (act === "rec-stop") stopRecording();
      else if (act === "rec-discard") discardTake();
      else if (act === "rec-another") { L.saved = null; paint(); }
      else if (act === "rec-save") await saveTake(b);
      else if (act === "job-check") await runJob(id, "check", b);
      else if (act === "job-audition") await runJob(id, "audition", b);
      else if (act === "cmp-run") await busy(b, runCompare);
      else if (act === "cmp-check") await checkAll(b);
      else if (act === "use-take") { L.selected = id; L.step = "create"; paint(); }
      else if (act === "del-take") {
        if (!(await confirmDialog("Delete this take?", "The recording is removed from this Mac. Narrators already created from it are not affected.", "Delete", true))) return;
        await api("DELETE", `/api/recordings/${id}`);
        L.takes = L.takes.filter((t) => t.id !== id); L.cmp.delete(id); L.compare = null; if (L.selected === id) L.selected = null; paint();
      } else if (act === "edit-text") {
        const t = L.takes.find((x) => x.id === id);
        const v = window.prompt("Exactly what is said in this recording:", t.ref_text);
        if (v && v.trim() && v.trim() !== t.ref_text) { const m = await patch(`/api/recordings/${id}`, { ref_text: v.trim() }); Object.assign(t, m); paint(); toast("Transcript updated. Run the Whisper check again to confirm.", "success"); }
      }
    } catch (err) { toastError(err); }
  });
  el.addEventListener("change", (e) => {
    if (e.target.dataset && e.target.dataset.cmp) {
      if (e.target.checked) L.cmp.add(e.target.dataset.cmp); else L.cmp.delete(e.target.dataset.cmp);
      L.compare = null; paint(); return;
    }
    if (e.target.id === "lab-script") { L.scriptId = e.target.value; L.saved = null; paint(); }
    else if (e.target.id === "lab-count") L.count = e.target.checked;
    else if (e.target.id === "lab-device" && micOn()) { L.deviceId = e.target.value; openMic(); }
  });
  el.addEventListener("submit", (e) => { if (e.target.id === "upload-form") { e.preventDefault(); uploadTake(e.target, e.submitter); return; } if (e.target.id === "create-form") { e.preventDefault(); createNarrator(e.target, e.submitter); } });

  return {
    el, init,
    releaseMic() {
      if (L.recState === "recording" || L.recState === "countdown") { L.recState = "idle"; }
      L.roomRun = null;
      mic.close().then(() => paint());  // repaint so the lab shows the microphone as off when you come back
    },
    destroy() { destroyed = true; timers.forEach((t) => { clearTimeout(t); clearInterval(t); }); if (raf) cancelAnimationFrame(raf); mic.close(); if (L.cur && L.cur.url) URL.revokeObjectURL(L.cur.url); },
  };
}
