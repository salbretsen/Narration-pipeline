// Narrator Studio: every narrator as a tile (name, description, play). The name and description open the narrator's own page
// (narrator.js), where it is previewed, edited, retuned and given emotion reference clips. New narrators are added here.
import { api, get, post, html, setHTML, toast, toastError, busy, navigate, patchRegions, onState } from "../ui.js";
import { createLab } from "./voicelab.js";
import { KIND, tileState } from "./narrator-model.js";

let tab = "preset";
let addOpen = false;

const TONE = { clone: "approved", design: "info", preset: "good" };

function tile(v, local) {
  const t = tileState(v.sample, local);
  return html`<article class="card narrator-tile" id="voice-${v.id}" aria-labelledby="v-${v.id}">
    <a class="tile-link" href="#/narrator/${v.id}">
      <h3 id="v-${v.id}">${v.name}</h3>
      <p class="tile-desc small">${v.description || ""}</p>
    </a>
    <div class="tile-foot">
      <span class="badge tone-${TONE[v.kind] || "neutral"}">${KIND[v.kind]}</span>${v.builtin ? html`<span class="badge tone-neutral">built-in</span>` : ""}
      <button class="btn play-btn ${t.state}" type="button" data-act="play" data-id="${v.id}" aria-label="${t.label}: ${v.name}" title="${t.label}"
        ${t.busy ? html`aria-busy="true"` : ""}>${t.busy ? html`<span class="spinner" aria-hidden="true"></span>` : t.icon}</button>
    </div>
    ${t.state === "blocked" ? html`<p class="small muted tile-note">The browser blocked automatic playback. Press ▶ to listen.</p>` : ""}
  </article>`;
}

function createForms(speakers) {
  return html`<div class="tabs" role="tablist" aria-label="Narrator type">
      ${[["preset", "Preset speaker"], ["design", "Designed voice"], ["clone", "Clone an authorised voice"], ["record", "Record a reference voice"]].map(([k, l]) =>
        html`<button role="tab" type="button" class="tab ${tab === k ? "active" : ""}" aria-selected="${tab === k}" data-tab="${k}" id="tab-${k}" aria-controls="panel-${k}">${l}</button>`)}
    </div>
    <div role="tabpanel" id="panel-${tab}" aria-labelledby="tab-${tab}">
    ${tab === "preset" ? html`<form id="f-preset" class="form">
      <p class="muted small">Qwen3-TTS CustomVoice built-in speakers. Ryan and Aiden are native English speakers. No human recording is involved.</p>
      <div class="form-grid"><label>Name <input name="name" required maxlength="80" placeholder="e.g. Ryan – thriller"></label>
      <label>Speaker <select name="speaker">${Object.entries(speakers).map(([k, d]) => html`<option value="${k}" ${k === "Ryan" ? html`selected` : ""}>${k} – ${d}</option>`)}</select></label></div>
      <label>Delivery instruction <input name="instruct" maxlength="1000" value="Calm, clear, warm audiobook narration with natural pacing."></label>
      <button class="btn primary" type="submit">Create preset narrator</button></form>` : ""}
    ${tab === "design" ? html`<form id="f-design" class="form">
      <p class="muted small">Qwen3-TTS VoiceDesign creates a synthetic voice from a description. Consistency over a long book is limited – promote a good preview to a stable narrator before narrating a whole book.</p>
      <label>Name <input name="name" required maxlength="80"></label>
      <label>Voice description <textarea name="description" rows="3" required minlength="10" maxlength="1000" placeholder="A mature, warm female narrator with a low, steady voice and a slight Scottish accent; unhurried pacing."></textarea></label>
      <button class="btn primary" type="submit">Create designed voice</button></form>` : ""}
    ${tab === "clone" ? html`<form id="f-clone" class="form">
      <p class="muted small">Upload a clean 5–15 second recording (WAV, MP3, M4A or FLAC, max 25 MB, 3–30 s) and its <strong>exact</strong> transcript.
        The clip is converted locally to 24 kHz mono and validated; it never leaves this Mac. Cloning uses the Qwen3-TTS Base model with ref_audio/ref_text.</p>
      <div class="form-grid"><label>Name <input name="name" required maxlength="80"></label>
      <label>Speaker / rights holder <input name="rights_holder" required maxlength="120" placeholder="Full name of the person whose voice this is"></label></div>
      <label>Reference audio <input type="file" name="file" required accept=".wav,.mp3,.m4a,.flac,audio/*"></label>
      <label>Exact transcript of the clip <textarea name="ref_text" rows="2" required maxlength="1000"></textarea></label>
      <label>Permission details (optional) <input name="statement" maxlength="1000" placeholder="e.g. Written consent signed 2026-10-01, contract #123"></label>
      <label class="check consent"><input type="checkbox" name="owner_attest" value="true" required>
        I confirm this is my own voice, or I have the speaker's explicit written permission to create and commercially use a synthetic copy of it.
        I will not use this tool to impersonate anyone.</label>
      <button class="btn primary" type="submit">Upload &amp; create cloned narrator</button>
      <p class="small muted">No recording yet? Use the <button type="button" class="linklike" data-tab="record">Record a reference voice</button> tab to record one in your browser with guidance.</p></form>` : ""}
    ${tab === "record" ? html`<div id="lab-host"></div>` : ""}
    </div>`;
}

export async function render(view, parts, query, nav) {
  const signal = nav && nav.signal;
  let data = await get("/api/voices", { signal });
  const lab = createLab({ onVoiceCreated: (v) => (v && v.id ? navigate(`#/narrator/${v.id}`) : refresh()) });
  const audio = new Audio();
  const local = {};          // voiceId -> {wanted, playing, blocked}
  let disposed = false, seq = 0, applied = 0;
  const L = (id) => (local[id] = local[id] || {});

  setHTML(view, "");
  const shell = document.createElement("div");
  view.appendChild(shell);
  setHTML(shell, html`<div class="page-head"><h1>Narrator Studio</h1></div>
    <p class="muted">Press ▶ to hear a narrator (the first time, a short sample is generated on this Mac's GPU and kept). Open a narrator to preview it,
      edit or retune it, and give a cloned narrator its emotion reference clips.</p>
    <div class="voice-grid" id="voice-grid" aria-live="polite"></div>
    <details class="card add-narrator" id="add-narrator" ${addOpen ? "open" : ""}><summary><h2 id="new-h">Add a narrator</h2></summary>
      <div id="create-host"></div></details>`);

  const grid = () => html`${data.voices.map((v) => tile(v, local[v.id] || {}))}`;
  const paintGrid = () => { if (!disposed) patchRegions(shell, { "#voice-grid": grid }); };
  const paintCreate = () => {
    const host = shell.querySelector("#create-host");
    setHTML(host, createForms(data.speakers));
    const lh = host.querySelector("#lab-host");
    if (lh) lh.appendChild(lab.el);  // the lab keeps its own state while the tab is switched
    wireForms(host);
  };

  async function refresh() {
    const mine = ++seq;
    try {
      const d = await get("/api/voices", { signal });
      if (disposed || mine < applied) return;
      applied = mine; data = d;
    } catch (e) { return; }
    for (const v of data.voices) {  // a requested sample that is now ready plays straight away
      const l = local[v.id];
      if (!l || !l.wanted) continue;
      if (v.sample.status === "ready") { l.wanted = false; start(v); }
      else if (v.sample.status === "none") { l.wanted = false; toast(`The sample for ${v.name} could not be generated. Open the narrator to preview it and see the error.`, "warn", 9000); }
    }
    paintGrid();
  }

  function start(v) {
    for (const k of Object.keys(local)) local[k].playing = false;
    audio.src = v.sample.url;
    audio.play().then(() => { L(v.id).playing = true; L(v.id).blocked = false; paintGrid(); })
      .catch((e) => { if (e && e.name === "NotAllowedError") { L(v.id).blocked = true; paintGrid(); } else toastError(e); });
  }

  async function play(b) {
    const v = data.voices.find((x) => x.id === Number(b.dataset.id));
    if (!v) return;
    if (L(v.id).playing) { audio.pause(); return; }
    if (v.sample.status === "ready") { L(v.id).blocked = false; start(v); return; }
    if (v.sample.status === "queued" || v.sample.status === "running") { L(v.id).wanted = true; return; }
    try { v.sample = await post(`/api/voices/${v.id}/sample`); } catch (e) { toastError(e); return; }
    L(v.id).wanted = v.sample.status !== "ready";
    if (v.sample.status === "ready") start(v);
    paintGrid();
  }

  const stopped = () => { let ch = false; for (const k of Object.keys(local)) if (local[k].playing) { local[k].playing = false; ch = true; } if (ch) paintGrid(); };
  audio.addEventListener("ended", stopped);
  audio.addEventListener("pause", stopped);

  function wireForms(host) {
    const fp = host.querySelector("#f-preset");
    if (fp) fp.addEventListener("submit", async (e) => {
      e.preventDefault(); const fd = new FormData(fp);
      try { const v = await busy(e.submitter, () => post("/api/voices/preset", { name: fd.get("name"), speaker: fd.get("speaker"), instruct: fd.get("instruct") }), "Narrator created"); navigate(`#/narrator/${v.id}`); } catch (err) { /* shown */ }
    });
    const fdz = host.querySelector("#f-design");
    if (fdz) fdz.addEventListener("submit", async (e) => {
      e.preventDefault(); const fd = new FormData(fdz);
      try { const v = await busy(e.submitter, () => post("/api/voices/design", { name: fd.get("name"), description: fd.get("description") }), "Designed voice created"); navigate(`#/narrator/${v.id}?tab=preview`); } catch (err) { /* shown */ }
    });
    const fc = host.querySelector("#f-clone");
    if (fc) fc.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!fc.reportValidity()) return;
      const fd = new FormData(fc);
      fd.set("owner_attest", fc.owner_attest.checked ? "true" : "false");
      let v;
      try { v = await busy(e.submitter, () => api("POST", "/api/voices/clone", fd)); } catch (err) { return; }
      toast(`Cloned narrator '${v.name}' created (${v.version.ref_duration_s}s reference)`, "success");
      (v.warnings || []).forEach((w) => toast(w, "warn", 9000));
      navigate(`#/narrator/${v.id}`);
    });
  }

  function onClick(e) {
    const tb = e.target.closest("[data-tab]");
    if (tb) {
      if (tab === "record" && tb.dataset.tab !== "record") lab.releaseMic();  // never leave the microphone on in the background
      tab = tb.dataset.tab; paintCreate(); const n = shell.querySelector(`#tab-${tab}`); if (n) n.focus(); return;
    }
    const b = e.target.closest("button[data-act=play]");
    if (b) play(b);
  }
  const onToggle = (e) => { if (e.target.id === "add-narrator") { addOpen = e.target.open; if (!addOpen) lab.releaseMic(); } };

  paintGrid();
  paintCreate();
  lab.init();
  view.addEventListener("click", onClick);
  view.addEventListener("toggle", onToggle, true);
  const off = onState(() => refresh(), 2000);
  return () => {
    disposed = true; off(); audio.pause(); audio.removeAttribute("src");
    view.removeEventListener("click", onClick); view.removeEventListener("toggle", onToggle, true); lab.destroy();
  };
}
