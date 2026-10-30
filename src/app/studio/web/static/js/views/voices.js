// Narrator / voice studio: preset speakers, designed voices, authorised clones, real previews.
import { api, get, post, patch, html, setHTML, toast, toastError, busy, dialog, confirmDialog, fmtDate, player, isAdmin } from "../ui.js";
import { createLab } from "./voicelab.js";

const DEFAULT_TEXT = "Chapter One. The rain had stopped by the time Eleanor reached the harbour, and the lamps along the quay were reflected in long, trembling lines on the black water.";
const previews = {}; // voiceId -> last preview job
const banks = {};    // voiceId -> GET /api/voices/{id}/refs (emotion reference bank of a cloned voice)
const cands = {};    // voiceId -> candidate jobs generated for the bank [{job_id, bucket, status, result, error}]
const pform = {};    // voiceId -> {text, instruct, verify}: what the owner typed in a card's preview form (kept across repaints and page visits)
const notes = {};    // voiceId -> {kind, text}: callouts shown at the top of a card until dismissed ("Saved as version 3", "New copy created. Next: ...")
let tab = "preset";
let flashId = null;  // the voice that was just created: its card is highlighted and scrolled into view

const KIND = { preset: "Preset speaker (CustomVoice)", design: "Designed voice (VoiceDesign)", clone: "Cloned voice (Base + reference)" };

function lineage(v) {
  const d = v.derived_from;
  if (d) return d.relation === "retune" ? `Retuned from ${d.voice_name}` : `Copy of ${d.voice_name}`;
  if (v.kind === "clone" && v.source_voice) return `Promoted from the designed voice ${v.source_voice.name}${v.source_voice.archived ? " (archived)" : ""}`;
  return "";
}

function noteBlock(v) {
  const n = notes[v.id];
  if (!n) return "";
  return html`<div class="callout ${n.tone || "info"} small voice-note" role="status">${n.text}
    <div class="form-actions">${n.preview ? html`<button class="btn small primary" type="button" data-act="preview-now" data-id="${v.id}">Preview with new delivery</button>` : ""}
      <button class="btn small ghost" type="button" data-act="note-dismiss" data-id="${v.id}">Dismiss</button></div></div>`;
}

function voiceCard(v) {
  const vv = v.version;
  const pj = previews[v.id];
  const pf = pform[v.id] || {};
  const lin = lineage(v);
  return html`<article class="card voice ${flashId === v.id ? "voice-new" : ""}" id="voice-${v.id}" data-voice-card="${v.id}" aria-labelledby="v-${v.id}">
    <header class="voice-head"><h3 id="v-${v.id}">${v.name}</h3><span class="badge tone-${v.kind === "clone" ? "approved" : v.kind === "design" ? "info" : "good"}">${KIND[v.kind]}</span>
      ${v.builtin ? html`<span class="badge tone-neutral">built-in</span>` : ""}</header>
    <p class="small">${v.description}</p>
    ${lin ? html`<p class="small muted voice-lineage">${lin}</p>` : ""}
    ${noteBlock(v)}
    <dl class="kv small">
      <dt>Version</dt><dd>v${vv.version} · ${fmtDate(vv.created_at)}</dd>
      <dt>Model</dt><dd><code>${vv.model_id}</code></dd>
      ${vv.speaker ? html`<dt>Speaker</dt><dd>${vv.speaker}</dd>` : ""}
      ${vv.instruct ? html`<dt>${v.kind === "design" ? "Description" : "Delivery"}</dt><dd>${vv.instruct}</dd>` : ""}
      ${v.kind === "clone" ? html`<dt>Transcript</dt><dd>“${vv.ref_text}”</dd><dt>Reference</dt><dd>${vv.ref_duration_s}s
        ${player(`/media/voice/${v.id}/reference`, `${v.name} reference clip`, "mini")}</dd>
        <dt>Consent</dt><dd>${vv.consent ? html`Attested for <strong>${vv.consent.rights_holder}</strong> on ${fmtDate(vv.consent.attested_at)}` : "–"}</dd>` : ""}
      <dt>Used by</dt><dd>${v.books_using} book(s)</dd>
    </dl>
    ${v.kind === "clone" ? html`<div class="bank" id="bank-${v.id}">${bankPanel(v)}</div>` : ""}
    ${v.kind === "design" ? html`<p class="callout info small">VoiceDesign re-creates the voice from its description on every segment, so timbre can drift across a long book.
      For consistent narration, generate a preview you like and <strong>promote it to a stable narrator</strong> (it becomes a clone reference).</p>` : ""}
    <form class="preview-form" data-voice="${v.id}">
      <label>Preview text <textarea name="text" rows="3" maxlength="600" required>${pf.text !== undefined ? pf.text : DEFAULT_TEXT}</textarea></label>
      ${v.kind !== "clone" ? html`<label>Extra delivery instruction (optional) <input name="instruct" maxlength="300" placeholder="e.g. tense, whispered" value="${pf.instruct || ""}"></label>` : ""}
      <label class="check"><input type="checkbox" name="verify" ${pf.verify ? html`checked` : ""}> Also verify with local Whisper (shows WER)</label>
      <div class="form-actions"><button class="btn primary" type="submit">Generate preview</button>
        <button class="btn ghost" type="button" data-act="edit" data-id="${v.id}">${v.builtin ? "Delivery…" : "Edit…"}</button>
        ${v.kind !== "clone" ? html`<button class="btn ghost" type="button" data-act="duplicate" data-id="${v.id}" title="Make an editable copy with different delivery instructions">Duplicate &amp; retune…</button>` : ""}
        ${v.kind === "clone" && v.source_voice ? html`<button class="btn ghost" type="button" data-act="retune" data-id="${v.id}" title="Make a designed copy of the voice this clone came from, with new instructions">Retune from ‘${v.source_voice.name}’…</button>` : ""}
        ${!v.builtin && isAdmin() ? html`<button class="btn ghost danger" type="button" data-act="archive" data-id="${v.id}">Archive</button>` : ""}</div>
    </form>
    <div class="preview-out" id="pv-${v.id}" aria-live="polite">${pj ? previewResult(v, pj) : ""}</div>
  </article>`;
}

const fmtDelta = (x, unit) => (x === null || x === undefined ? "–" : `${x > 0 ? "+" : ""}${x} ${unit}`);

function driftText(m) {
  if (!m) return "";
  return `pitch ${fmtDelta(m.f0_delta_hz, "Hz")} · brightness ${fmtDelta(m.centroid_delta_hz, "Hz")}`;
}

function candidateRow(v, c) {
  const r = c.result || {};
  if (c.status === "failed" || c.status === "cancelled") return html`<li class="small err">${c.bucket} candidate failed: ${c.error || c.status}</li>`;
  if (c.status !== "completed" || !r.recording_id) return html`<li class="small muted"><span class="spinner" aria-hidden="true"></span> ${c.bucket} candidate ${c.status === "running" ? "synthesizing…" : "waiting for the GPU worker…"}</li>`;
  const chk = r.asr_check;
  return html`<li class="small">
    <strong>${c.bucket}</strong> candidate ${chk ? `· Whisper WER ${(chk.wer * 100).toFixed(1)}% ${chk.matches ? "(matches)" : "(does not match)"}` : "· not checked"}
    ${r.drift && !r.drift.error ? `· drift from base: ${driftText(r.drift)}` : ""}
    ${c.note ? html`<span class="badge tone-warn">mock test tones</span>` : ""}
    ${player(`/media/recording/${r.recording_id}`, `${c.bucket} candidate`, "mini")}
    <button class="btn small primary" type="button" data-act="bank-adopt-cand" data-voice="${v.id}" data-bucket="${c.bucket}" data-rid="${r.recording_id}"
      ${chk && chk.matches ? "" : html`disabled`}>Adopt for ${c.bucket}</button></li>`;
}

function bankPanel(v) {
  const b = banks[v.id];
  const cs = cands[v.id] || [];
  if (!b) return html`<p class="small muted"><span class="spinner" aria-hidden="true"></span> Loading reference clips…</p>`;
  const canGen = b.synthetic && b.source_voice;
  return html`<h4>Reference clips (emotion bank)</h4>
    <p class="small muted">A cloned voice cannot take text instructions: it is steered only by its reference clip, plus pace. Add short clips of the same voice in
      other registers; when the reference bank is switched on for cloned voices in Settings, a strongly directed segment uses the matching clip. Without a clip, the base reference is used.</p>
    ${b.synthetic && !b.source_voice ? html`<p class="callout warn small">The designed voice this clone came from could not be found, so candidates cannot be generated automatically.</p>` : ""}
    <ul class="bank-list">${b.buckets.map((x) => html`<li class="small">
      <strong>${x.label}</strong>
      ${x.ref ? html`<span class="muted">· ${x.ref.duration_s}s · ${x.ref.source === "synthetic" ? "synthetic" : "recorded"} · ${driftText(x.ref.metrics)}</span>
          ${x.ref.mock ? html`<span class="badge tone-warn">mock test tones</span>` : ""}
          ${player(`/media/voice/${v.id}/ref/${x.ref.id}`, `${x.label} reference clip`, "mini")}
          ${isAdmin() ? html`<button class="btn small ghost danger" type="button" data-act="bank-remove" data-voice="${v.id}" data-ref="${x.ref.id}" data-label="${x.label}">Remove</button>` : ""}`
        : html`<span class="muted">· no clip (uses the base reference)</span>`}
      ${canGen ? html`<button class="btn small" type="button" data-act="bank-gen" data-voice="${v.id}" data-bucket="${x.bucket}">Generate candidates…</button>` : ""}
      <button class="btn small ghost" type="button" data-act="bank-adopt" data-voice="${v.id}" data-bucket="${x.bucket}" data-label="${x.label}">Adopt a lab recording…</button>
    </li>`)}</ul>
    ${cs.length ? html`<div aria-live="polite"><h5>Candidates</h5><ul class="bank-list">${cs.map((c) => candidateRow(v, c))}</ul>
      <p class="small muted">Candidates are also saved as drafts in the Record tab (14 days), where you can compare them.</p></div>` : ""}`;
}

function previewResult(v, j) {
  if (j.status === "queued" || j.status === "running") return html`<p class="muted"><span class="spinner" aria-hidden="true"></span> ${j.status === "queued" ? "Waiting for the GPU worker (previews run between book segments)…" : "Synthesizing…"}</p>`;
  if (j.status === "failed") return html`<p class="err">Preview failed: ${j.error}</p>`;
  if (j.status !== "completed" || !j.result) return "";
  const r = j.result;
  return html`<div class="preview-result">
    ${player(`/media/preview/${j.id}`, `${v.name} preview`)}
    <div class="small muted">${r.duration_s}s audio · ${r.engine} · ${r.rtf ? `RTF ${r.rtf.toFixed(2)}` : ""} ${r.peak_mem_gb ? `· peak ${r.peak_mem_gb.toFixed(1)} GB` : ""}</div>
    ${r.note ? html`<p class="callout warn small">${r.note}</p>` : ""}
    ${r.asr_text !== undefined ? html`<p class="small">Whisper heard: “${r.asr_text}” · WER ${(r.wer * 100).toFixed(1)}%</p>` : ""}
    <div class="form-actions"><a class="btn small" href="/media/preview/${j.id}?download=1" download>Download WAV</a>
      ${v.kind === "design" ? html`<button class="btn small primary" data-act="promote" data-job="${j.id}" data-name="${v.name}">Promote to stable narrator…</button>` : ""}</div>
  </div>`;
}

function createForms(speakers, english) {
  return html`<section class="card" aria-labelledby="new-h"><h2 id="new-h">Add a narrator</h2>
    <div class="tabs" role="tablist" aria-label="Narrator type">
      ${[["preset", "Preset speaker"], ["design", "Designed voice"], ["clone", "Clone an authorised voice"], ["record", "Record a reference voice"]].map(([k, l]) =>
        html`<button role="tab" type="button" class="tab ${tab === k ? "active" : ""}" aria-selected="${tab === k}" data-tab="${k}" id="tab-${k}" aria-controls="panel-${k}">${l}</button>`)}
    </div>
    <div role="tabpanel" id="panel-${tab}" aria-labelledby="tab-${tab}">
    ${tab === "preset" ? html`<form id="f-preset" class="form">
      <p class="muted small">Qwen3-TTS CustomVoice built-in speakers. Ryan and Aiden are native English speakers. No human recording is involved.</p>
      <div class="form-grid"><label>Name <input name="name" required maxlength="80" placeholder="e.g. Ryan – thriller"></label>
      <label>Speaker <select name="speaker">${Object.entries(speakers).map(([k, d]) => html`<option value="${k}" ${k === "Ryan" ? html`selected` : ""}>${k} – ${d}${english.includes(k) ? "" : ""}</option>`)}</select></label></div>
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
    </div></section>`;
}

export async function render(view) {
  let data = await get("/api/voices");
  const lab = createLab({ onVoiceCreated: () => reload() });
  const timers = new Set();

  const paint = () => {
    setHTML(view, html`<div class="page-head"><h1>Narrator Studio</h1></div>
      <p class="muted">Every preview is real local synthesis on this Mac's GPU. Voice settings are versioned: changing a voice's delivery creates a new version;
        books keep the version they were created with until you explicitly switch them (which regenerates their audio).
        To try a different delivery without touching a voice that is already in use, use <strong>Duplicate &amp; retune…</strong> on its card.</p>
      ${createForms(data.speakers, data.english_speakers)}
      <div class="voice-grid">${data.voices.map(voiceCard)}</div>`);
    const host = view.querySelector("#lab-host");
    if (host) host.appendChild(lab.el);  // the lab keeps its own state while the tab is switched or the page repaints
    wireForms();
    data.voices.filter((v) => v.kind === "clone" && !banks[v.id]).forEach((v) => loadBank(v.id));
    if (flashId !== null) {
      const card = view.querySelector(`#voice-${flashId}`);
      flashId = null;
      if (card) { card.scrollIntoView({ block: "center" }); const h = card.querySelector("h3"); if (h) { h.setAttribute("tabindex", "-1"); h.focus({ preventScroll: true }); } }
    }
  };

  async function reload() { data = await get("/api/voices"); paint(); }

  function paintBank(vid) {
    const v = data.voices.find((x) => x.id === vid), host = view.querySelector(`#bank-${vid}`);
    if (v && host) setHTML(host, bankPanel(v));
  }

  async function loadBank(vid) {
    try { banks[vid] = await get(`/api/voices/${vid}/refs`); } catch (e) { toastError(e); return; }
    paintBank(vid);
  }

  function pollCandidate(vid, c) {
    const t = setInterval(async () => {
      try {
        const j = await get(`/api/jobs/${c.job_id}`);
        c.status = j.status; c.result = j.result; c.error = j.error; c.note = j.result && j.result.note;
        paintBank(vid);
        if (["completed", "failed", "cancelled"].includes(j.status)) { clearInterval(t); timers.delete(t); }
      } catch (e) { clearInterval(t); timers.delete(t); toastError(e); }
    }, 2000);
    timers.add(t);
  }

  async function adoptRecording(vid, bucket, rid, label, fromCandidate) {
    const v = data.voices.find((x) => x.id === vid);
    const b = banks[vid];
    const body = { bucket, recording_id: rid };
    if (!(b && b.synthetic)) {
      const res = await dialog({ title: `Adopt a clip for ${label}`, form: true, buttons: [{ label: "Adopt clip", value: "ok", kind: "primary" }],
        body: html`<p class="small">This recording must be of the same speaker as ${v.name}.</p>
          <label class="check"><input type="checkbox" name="owner_attest" value="true" required> I own this recording, or have the speaker's written permission to clone it.</label>
          <label>Consent statement <input name="statement" required minlength="10" maxlength="1000" placeholder="e.g. Recorded by the speaker on 2026-10-01 for this project"></label>` });
      if (!res) return;
      body.owner_attest = res.data.owner_attest === "true"; body.statement = res.data.statement || "";
    } else if (!fromCandidate) {
      const ok = await confirmDialog("Adopt this clip?", `It replaces any ${label} clip ${v.name} already has. Existing takes made with the old clip are not changed until you regenerate them.`, "Adopt");
      if (!ok) return;
    }
    try { banks[vid] = await post(`/api/voices/${vid}/refs/adopt`, body); toast(`${label} reference clip adopted`, "success"); } catch (e) { toastError(e); }
    paintBank(vid);
  }

  function pollPreview(voiceId, jobId) {
    const t = setInterval(async () => {
      try {
        const j = await get(`/api/jobs/${jobId}`);
        previews[voiceId] = j;
        const out = view.querySelector(`#pv-${voiceId}`);
        const v = data.voices.find((x) => x.id === voiceId);
        if (out && v) setHTML(out, previewResult(v, j));
        if (["completed", "failed", "cancelled"].includes(j.status)) { clearInterval(t); timers.delete(t); }
      } catch (e) { clearInterval(t); timers.delete(t); toastError(e); }
    }, 1500);
    timers.add(t);
  }

  function wireForms() {
    view.querySelectorAll(".preview-form").forEach((f) => f.addEventListener("submit", async (e) => {
      e.preventDefault();
      const fd = new FormData(f), vid = Number(f.dataset.voice);
      const j = await busy(e.submitter, () => post(`/api/voices/${vid}/preview`, { text: fd.get("text"), instruct: fd.get("instruct") || null, verify: fd.get("verify") === "on" }));
      previews[vid] = j;
      const v = data.voices.find((x) => x.id === vid);
      setHTML(view.querySelector(`#pv-${vid}`), previewResult(v, j));
      pollPreview(vid, j.id);
    }));
    const fp = view.querySelector("#f-preset");
    if (fp) fp.addEventListener("submit", async (e) => {
      e.preventDefault(); const fd = new FormData(fp);
      await busy(e.submitter, () => post("/api/voices/preset", { name: fd.get("name"), speaker: fd.get("speaker"), instruct: fd.get("instruct") }), "Narrator created");
      await reload();
    });
    const fdz = view.querySelector("#f-design");
    if (fdz) fdz.addEventListener("submit", async (e) => {
      e.preventDefault(); const fd = new FormData(fdz);
      await busy(e.submitter, () => post("/api/voices/design", { name: fd.get("name"), description: fd.get("description") }), "Designed voice created");
      await reload();
    });
    const fc = view.querySelector("#f-clone");
    if (fc) fc.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!fc.reportValidity()) return;
      const fd = new FormData(fc);
      fd.set("owner_attest", fc.owner_attest.checked ? "true" : "false");
      const v = await busy(e.submitter, () => api("POST", "/api/voices/clone", fd));
      toast(`Cloned narrator '${v.name}' created (${v.version.ref_duration_s}s reference)`, "success");
      (v.warnings || []).forEach((w) => toast(w, "warn", 9000));
      await reload();
    });
  }

  const normNl = (t) => String(t || "").split("\r").join("").trim();
  const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

  function versionNote(v) {
    const nv = v.version.version;
    if (v.builtin) return html`<p class="callout info small"><strong>${v.name}</strong> is a built-in speaker, so its name and speaker are fixed. You can change only its
      <strong>delivery instruction</strong>. Saving creates <strong>version ${nv + 1}</strong> of ${v.name}; previews and new books use it straight away, but books already
      narrated with version ${nv} keep that version until you switch them on the book page. To keep ${v.name} as it is and try a variation, use
      <strong>Duplicate &amp; retune…</strong>.</p>`;
    if (v.kind === "clone") return html`<p class="callout info small">A cloned voice is defined by its reference clip and cannot take instructions, so only its name can be edited here.
      ${v.source_voice ? html`To make a variant, use <strong>Retune from ‘${v.source_voice.name}’…</strong>` : "To make a variant, upload a new reference clip"}.</p>`;
    return html`<p class="callout info small">${v.kind === "design"
      ? html`You can change the <strong>name</strong> and the <strong>description</strong>. A new description makes VoiceDesign create a different-sounding voice.`
      : html`You can change the <strong>name</strong>, the <strong>speaker</strong> and the <strong>delivery instruction</strong>.`}
      Changing the sound saves <strong>version ${nv + 1}</strong> of this voice (renaming does not). Previews and new books use it straight away; books already narrated with
      version ${nv} keep it until you switch them on the book page. Prefer a separate voice? Use <strong>Duplicate &amp; retune…</strong>.</p>`;
  }

  async function editVoice(b, v) {
    const nv = v.version.version;
    const res = await dialog({ title: v.builtin ? `Delivery for ${v.name} (built-in)` : `Edit ${v.name}`, form: true,
      buttons: [{ label: v.kind === "clone" ? "Save name" : v.builtin ? `Save as version ${nv + 1}` : "Save", value: "save", kind: "primary" },
        ...(v.kind !== "clone" ? [{ label: "Duplicate & retune instead…", value: "dup" }] : [])],
      body: html`${versionNote(v)}
        ${!v.builtin ? html`<label>Name <input name="name" required maxlength="80" value="${v.name}"></label>` : ""}
        ${v.kind === "preset" && !v.builtin ? html`<label>Speaker <select name="speaker">${Object.keys(data.speakers).map((s) => html`<option ${s === v.version.speaker ? html`selected` : ""}>${s}</option>`)}</select></label>`
          : v.kind === "preset" ? html`<p class="small">Speaker: <strong>${v.version.speaker}</strong> (fixed)</p>` : ""}
        ${v.kind !== "clone" ? html`<label>${v.kind === "design" ? "Description" : "Delivery instruction"} <textarea name="instruct" rows="4" maxlength="1000" ${v.kind === "design" ? html`required minlength="10"` : ""}>${v.version.instruct || ""}</textarea></label>` : ""}` });
    if (!res) return;
    if (res.value === "dup") { await duplicateVoice(b, v, res.data.instruct); return; }
    const body = {};
    if (res.data.name !== undefined && res.data.name.trim() !== v.name) body.name = res.data.name;
    if (res.data.instruct !== undefined && normNl(res.data.instruct) !== normNl(v.version.instruct)) body.instruct = res.data.instruct;
    if (res.data.speaker !== undefined && res.data.speaker !== v.version.speaker) body.speaker = res.data.speaker;
    if (!Object.keys(body).length) { toast("Nothing was changed.", "info"); return; }
    let out;
    try { out = await busy(b, () => patch(`/api/voices/${v.id}`, body)); } catch (e) { return; }
    const nver = out.version.version, made = nver !== nv, older = out.books_on_older_version || [];
    const vers = [...new Set(older.map((x) => x.voice_version).filter((x) => x))].sort((x, y) => x - y);
    const still = older.length ? ` ${plural(older.length, "book still uses", "books still use")} ${vers.length ? (vers.length === 1 ? `version ${vers[0]}` : `versions ${vers.join(" and ")}`) : "an earlier version"}.` : "";
    const msg = made ? `Saved as version ${nver}.${still}` : "Saved (name only: no new version).";
    toast(msg, made && older.length ? "warn" : "success", 10000);
    if (made) notes[v.id] = { tone: "good", preview: true, text: `${msg} Previews and new books use version ${nver} now.${older.length ? " Switch those books from their book page to hear the new sound." : ""}` };
    await reload();
  }

  const freeName = (base) => {
    const taken = new Set(data.voices.map((x) => x.name.toLowerCase()));
    let n = base, i = 2;
    while (taken.has(n.toLowerCase())) n = base.replace(/\)$/, "") + ` ${i++})`;
    return n.slice(0, 80);
  };

  async function duplicateVoice(b, v, instruct) {
    const text = instruct !== undefined ? instruct : (v.version.instruct || "");
    const res = await dialog({ title: `Duplicate & retune ${v.name}`, form: true, buttons: [{ label: "Create copy", value: "ok", kind: "primary" }],
      body: html`<p class="callout info small">Creates a <strong>new, separate voice</strong> from ${v.name}’s current settings (version ${v.version.version}) with the delivery you choose below.
        ${v.name} itself and the books using it are not changed. Then generate a preview of the copy, and keep refining it until you like it.</p>
        <label>Name of the new voice <input name="name" required maxlength="80" value="${freeName(v.name + " (retune)")}"></label>
        ${v.kind === "preset" ? html`<label>Speaker <select name="speaker">${Object.keys(data.speakers).map((s) => html`<option ${s === v.version.speaker ? html`selected` : ""}>${s}</option>`)}</select></label>` : ""}
        <label>${v.kind === "design" ? "Voice description" : "Delivery instruction"} <textarea name="instruct" rows="5" maxlength="1000" ${v.kind === "design" ? html`required minlength="10"` : ""}>${text}</textarea></label>` });
    if (!res) return;
    const body = { name: res.data.name };
    if (res.data.instruct !== undefined) body.instruct = res.data.instruct;
    if (res.data.speaker !== undefined && res.data.speaker !== v.version.speaker) body.speaker = res.data.speaker;
    await createdCopy(b, () => post(`/api/voices/${v.id}/duplicate`, body), v, `Copy of ${v.name} created`);
  }

  async function retuneVoice(b, v) {
    const src = v.source_voice;
    let srcFull;
    try { srcFull = await get(`/api/voices/${src.id}`); } catch (e) { toastError(e); return; }
    const res = await dialog({ title: `Retune from ‘${src.name}’`, form: true, buttons: [{ label: "Create designed voice", value: "ok", kind: "primary" }],
      body: html`<p class="callout info small">${v.name} is a stable clone and cannot take instructions. This creates a <strong>new designed voice</strong> from ‘${src.name}’
        (the voice ${v.name} was made from), with the description below. ${v.name} is not changed.
        ${src.archived ? " (The source voice is archived; it can still be used.)" : ""}</p>
        <label>Name of the new voice <input name="name" required maxlength="80" value="${freeName(src.name + " (retune)")}"></label>
        <label>Voice description <textarea name="instruct" rows="5" required minlength="10" maxlength="1000">${srcFull.version.instruct || ""}</textarea></label>` });
    if (!res) return;
    await createdCopy(b, () => post(`/api/voices/${v.id}/retune`, { name: res.data.name, instruct: res.data.instruct }), v, "Retuned design voice created", true);
  }

  async function createdCopy(b, fn, from, okMsg, fromClone) {
    let out;
    try { out = await busy(b, fn, okMsg); } catch (e) { return; }
    flashId = out.id;
    const prev = pform[from.id] || {};
    pform[out.id] = { text: prev.text !== undefined ? prev.text : DEFAULT_TEXT, instruct: "", verify: false };
    notes[out.id] = { tone: "good", text: out.kind === "design"
      ? `New design voice created${fromClone ? ` from ‘${from.source_voice.name}’` : ` as a copy of ${from.name}`}. Next: generate a preview, pick one you like, then Promote to stable narrator.`
      : `New voice created as a copy of ${from.name}. Next: generate a preview to hear the new delivery.` };
    await reload();
  }

  async function onClick(e) {
    const tb = e.target.closest("[data-tab]");
    if (tb) {
      if (tab === "record" && tb.dataset.tab !== "record") lab.releaseMic();  // never leave the microphone on in the background
      tab = tb.dataset.tab; paint(); const n = view.querySelector(`#tab-${tab}`); if (n) n.focus(); return; }
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const act = b.dataset.act;
    if (act === "archive") {
      if (!(await confirmDialog("Archive voice?", "It will no longer be offered for new books. Existing books keep using it.", "Archive", true))) return;
      await busy(b, () => post(`/api/voices/${b.dataset.id}/archive`), "Voice archived"); await reload();
    } else if (act === "edit") {
      await editVoice(b, data.voices.find((x) => x.id === Number(b.dataset.id)));
    } else if (act === "duplicate") {
      await duplicateVoice(b, data.voices.find((x) => x.id === Number(b.dataset.id)));
    } else if (act === "retune") {
      await retuneVoice(b, data.voices.find((x) => x.id === Number(b.dataset.id)));
    } else if (act === "note-dismiss") {
      delete notes[Number(b.dataset.id)]; paint();
    } else if (act === "preview-now") {
      const vid = Number(b.dataset.id);
      delete notes[vid]; paint();
      const nf = view.querySelector(`.preview-form[data-voice="${vid}"]`);
      if (nf) { nf.scrollIntoView({ block: "center" }); nf.requestSubmit(); }
    } else if (act === "bank-gen") {
      const vid = Number(b.dataset.voice), bucket = b.dataset.bucket;
      try {
        const out = await busy(b, () => post(`/api/voices/${vid}/refs/candidates`, { bucket, n: 3 }), "Generating 3 candidates (they run between book segments)");
        cands[vid] = [...out.jobs.map((j) => ({ job_id: j.id, bucket, status: j.status })), ...(cands[vid] || [])].slice(0, 12);
        cands[vid].filter((c) => c.status === "queued").forEach((c) => pollCandidate(vid, c));
        paintBank(vid);
      } catch (e) { /* busy() already showed the error */ }
    } else if (act === "bank-adopt-cand") {
      await adoptRecording(Number(b.dataset.voice), b.dataset.bucket, b.dataset.rid, b.dataset.bucket, true);
    } else if (act === "bank-adopt") {
      const vid = Number(b.dataset.voice), bucket = b.dataset.bucket;
      let recs;
      try { recs = (await get("/api/recordings")).recordings.filter((r) => r.asr_check && r.asr_check.matches); } catch (e) { toastError(e); return; }
      if (!recs.length) { toast("No recordings have passed the Whisper check yet. Record or generate one, then run the transcript check in the Record tab.", "warn", 9000); return; }
      const res = await dialog({ title: `Adopt a recording for ${b.dataset.label}`, form: true, buttons: [{ label: "Continue", value: "ok", kind: "primary" }],
        body: html`<label>Checked recording <select name="rid">${recs.map((r) => html`<option value="${r.id}">${r.label || r.script_title} · ${r.duration_s}s · ${fmtDate(r.created_at)}</option>`)}</select></label>
          <p class="small muted">Only recordings whose transcript matched the Whisper check are listed. Audition them in the Record tab.</p>` });
      if (!res) return;
      await adoptRecording(vid, bucket, res.data.rid, b.dataset.label, false);
    } else if (act === "bank-remove") {
      if (!(await confirmDialog("Remove reference clip?", `The ${b.dataset.label} clip is deleted. Takes already made with it keep their audio; new takes use the base reference.`, "Remove", true))) return;
      const vid = Number(b.dataset.voice);
      try { banks[vid] = await api("DELETE", `/api/voices/${vid}/refs/${b.dataset.ref}`); toast("Reference clip removed", "success"); } catch (e) { toastError(e); }
      paintBank(vid);
    } else if (act === "promote") {
      const res = await dialog({ title: "Promote preview to a stable narrator", form: true, buttons: [{ label: "Create narrator", value: "ok", kind: "primary" }],
        body: html`<p>This preview becomes the reference clip of a new cloned narrator, so every segment uses the same voice.</p>
          <label>Name <input name="name" required maxlength="80" value="${b.dataset.name} (stable)"></label>` });
      if (!res) return;
      await busy(b, () => post("/api/voices/promote", { job_id: Number(b.dataset.job), name: res.data.name }), "Stable narrator created");
      await reload();
    }
  }

  // remember what is typed in each card's preview form, so a repaint (tab switch, reload after an edit) never resets it
  const rememberPreviewForm = (e) => {
    const f = e.target.closest && e.target.closest(".preview-form");
    if (!f) return;
    const fd = new FormData(f);
    pform[Number(f.dataset.voice)] = { text: fd.get("text") || "", instruct: fd.get("instruct") || "", verify: fd.get("verify") === "on" };
  };
  view.addEventListener("input", rememberPreviewForm);
  view.addEventListener("change", rememberPreviewForm);
  view.addEventListener("click", onClick);
  paint();
  lab.init();
  Object.entries(previews).forEach(([vid, j]) => { if (["queued", "running"].includes(j.status)) pollPreview(Number(vid), j.id); });
  Object.entries(cands).forEach(([vid, cs]) => cs.filter((c) => ["queued", "running"].includes(c.status)).forEach((c) => pollCandidate(Number(vid), c)));
  return () => { view.removeEventListener("click", onClick); view.removeEventListener("input", rememberPreviewForm); view.removeEventListener("change", rememberPreviewForm); timers.forEach(clearInterval); lab.destroy(); };
}
