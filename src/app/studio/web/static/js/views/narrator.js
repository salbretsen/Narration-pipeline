// One narrator's page (#/narrator/ID): details and versions, previews, emotion reference clips (cloned narrators), recording and
// uploading reference clips, and the tools that edit, copy or retune it. Opened from a tile in Narrator Studio (voices.js).
import { api, get, post, html, setHTML, toast, toastError, busy, dialog, confirmDialog, fmtDate, player, isAdmin, navigate, patchRegions, onState } from "../ui.js";
import { createLab } from "./voicelab.js";
import { KIND_LONG, SOURCES, NARRATOR_TABS, tabOf, tileState, similarityVerdict, byFamily, clipLine } from "./narrator-model.js";
import { DEFAULT_TEXT, lineage, editVoice, duplicateVoice, retuneVoice, promotePreview } from "./narrator-common.js";

const pforms = {};       // voiceId -> {text, instruct, verify}: the preview form, kept across visits
const lastPreview = {};  // voiceId -> preview job id
const notes = {};        // voiceId -> {tone, text}: a callout shown until dismissed ("Saved as version 3")
let takeSource = "clone";

const TONE = { clone: "approved", design: "info", preset: "good" };
const DONE = ["completed", "failed", "cancelled"];

function head(v, local) {
  const t = tileState(v.sample, local);
  const lin = lineage(v);
  return html`<div class="nr-head">
    <div><span class="badge tone-${TONE[v.kind] || "neutral"}">${KIND_LONG[v.kind]}</span>${v.builtin ? html` <span class="badge tone-neutral">built-in</span>` : ""}
      ${v.archived ? html` <span class="badge tone-warn">archived</span>` : ""}
      <p class="nr-desc">${v.description || ""}</p>${lin ? html`<p class="small muted">${lin}</p>` : ""}</div>
    <button class="btn play-btn ${t.state}" type="button" data-act="play" aria-label="${t.label}: ${v.name}" title="${t.label}" ${t.busy ? html`aria-busy="true"` : ""}>
      ${t.busy ? html`<span class="spinner" aria-hidden="true"></span>` : t.icon}</button>
  </div>
  ${t.state === "blocked" ? html`<p class="small muted">The browser blocked automatic playback. Press ▶ to listen.</p>` : ""}
  ${notes[v.id] ? html`<div class="callout ${notes[v.id].tone || "info"} small" role="status">${notes[v.id].text}
    <div class="form-actions"><button class="btn small ghost" type="button" data-act="note-dismiss">Dismiss</button></div></div>` : ""}`;
}

function overview(v) {
  const vv = v.version;
  return html`<section class="card" aria-labelledby="ov-h"><h2 id="ov-h">Details</h2>
    <dl class="kv small">
      <dt>Version</dt><dd>v${vv.version} · ${fmtDate(vv.created_at)}</dd>
      <dt>Model</dt><dd><code>${vv.model_id}</code></dd>
      ${vv.speaker ? html`<dt>Speaker</dt><dd>${vv.speaker}</dd>` : ""}
      ${vv.instruct ? html`<dt>${v.kind === "design" ? "Description" : "Delivery"}</dt><dd>${vv.instruct}</dd>` : ""}
      ${v.kind === "clone" ? html`<dt>Transcript</dt><dd>“${vv.ref_text}”</dd><dt>Base reference</dt><dd>${vv.ref_duration_s}s
        ${player(`/media/voice/${v.id}/reference`, `${v.name} reference clip`, "mini")}</dd>
        <dt>Consent</dt><dd>${vv.consent ? html`Attested for <strong>${vv.consent.rights_holder}</strong> on ${fmtDate(vv.consent.attested_at)}` : "–"}</dd>` : ""}
      <dt>Pace</dt><dd>x${Number(v.pace || 1).toFixed(2)} <a class="small" href="#/pronunciation">change</a></dd>
      <dt>Used by</dt><dd>${v.books_using} book(s)</dd>
    </dl>
    ${v.kind === "design" ? html`<p class="callout info small">VoiceDesign re-creates the voice from its description on every segment, so timbre can drift across a long book.
      Generate a preview you like on the <a href="#/narrator/${v.id}?tab=preview">Preview</a> tab and <strong>promote it to a stable narrator</strong>.</p>` : ""}
    ${v.kind === "clone" ? html`<p class="small muted">A cloned narrator reads with the voice of its reference clip. Emotion reaches it through its
      <a href="#/narrator/${v.id}?tab=emotions">emotion reference clips</a>.</p>` : ""}
  </section>
  <section class="card" aria-labelledby="vh-h"><h2 id="vh-h">Version history</h2>
    <div class="table-wrap"><table class="small"><thead><tr><th scope="col">Version</th><th scope="col">Created</th><th scope="col">Model</th><th scope="col">${v.kind === "clone" ? "Reference transcript" : "Instruction / description"}</th></tr></thead>
    <tbody>${(v.versions || []).map((x) => html`<tr${x.id === vv.id ? html` class="current"` : ""}><td>v${x.version}${x.id === vv.id ? " (current)" : ""}</td><td>${fmtDate(x.created_at)}</td>
      <td><code>${String(x.model_id || "").split("/").pop()}</code></td><td>${x.instruct || x.ref_text || "–"}</td></tr>`)}</tbody></table></div>
    <p class="small muted">Books keep the version they were narrated with until you switch them on the book page.</p></section>`;
}

function previewForm(v) {
  const pf = pforms[v.id] || {};
  return html`<section class="card" aria-labelledby="pv-h"><h2 id="pv-h">Preview</h2>
    <form class="preview-form" id="nr-preview-form">
      <label>Preview text <textarea name="text" rows="3" maxlength="600" required>${pf.text !== undefined ? pf.text : DEFAULT_TEXT}</textarea></label>
      ${v.kind !== "clone" ? html`<label>Extra delivery instruction (optional) <input name="instruct" maxlength="300" placeholder="e.g. tense, whispered" value="${pf.instruct || ""}"></label>` : ""}
      <label class="check"><input type="checkbox" name="verify" ${pf.verify ? html`checked` : ""}> Also verify with local Whisper (shows WER)</label>
      <div class="form-actions"><button class="btn primary" type="submit">Generate preview</button></div>
    </form>
    <div class="preview-out" id="nr-pv" aria-live="polite"></div></section>`;
}

function previewResult(v, j) {
  if (!j) return "";
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
      ${v.kind === "design" ? html`<button class="btn small primary" type="button" data-act="promote" data-job="${j.id}">Promote to stable narrator…</button>` : ""}</div>
  </div>`;
}

function emotionsIntro(v, refs) {
  const opts = refs.sources.filter((k) => SOURCES[k]);
  if (!opts.includes(takeSource)) takeSource = opts[0];
  return html`<section class="card" aria-labelledby="em-h"><h2 id="em-h">Emotion references</h2>
    <p class="small">A cloned narrator takes no written instructions; it sounds like its reference clip. Give it one short clip per emotion, in its own voice:
      when the reference bank is on for cloned voices (Settings → Delivery), a strongly directed segment uses the clip for its emotion, or a related emotion's
      clip, else the base reference.</p>
    <p class="small muted">Generate one take at a time, listen, then <strong>Adopt</strong> or <strong>Discard</strong> it. The speaker-similarity score says whether a take
      still sounds like ${v.name}; Whisper checks that every word was read.</p>
    <div class="form-grid"><label>New takes are made <select id="take-source">${opts.map((k) => html`<option value="${k}" ${k === takeSource ? html`selected` : ""}>${SOURCES[k].label}</option>`)}</select></label>
      <p class="small muted" id="take-source-hint">${SOURCES[takeSource].hint}</p></div>
    <div id="nr-emo"></div></section>`;
}

function takeRow(b, c, refs) {
  const chk = c.asr_check;
  const sv = similarityVerdict(c.similarity, refs.min_similarity, c.mock);
  return html`<li class="take" id="take-${c.id}">
    ${player(`/media/recording/${c.id}`, `${b.label} take`, "mini")}
    <span class="small">${(SOURCES[c.source] || {}).label || c.source} ·
      ${chk ? `Whisper WER ${(chk.wer * 100).toFixed(1)}% ${chk.matches ? "(every word read)" : "(words missing or wrong)"}` : "not checked"} ·
      <span class="tone-text-${sv.tone}">${sv.text}</span>${c.mock ? html` <span class="badge tone-warn">mock test tones</span>` : ""}</span>
    <span class="take-actions"><button class="btn small primary" type="button" data-act="adopt-take" data-bucket="${b.bucket}" data-label="${b.label}" data-rid="${c.id}"
      ${chk && chk.matches ? "" : html`disabled title="Only takes whose words all match can be adopted"`}>Adopt</button>
      <button class="btn small ghost" type="button" data-act="discard" data-rid="${c.id}" data-label="${b.label}">Discard</button></span></li>`;
}

function emotionRows(v, refs) {
  return html`${byFamily(refs.buckets).map((g) => html`<h3 class="emo-family">${g.label}</h3><ul class="emo-list">${g.buckets.map((b) => html`<li class="emo-row" id="emo-${b.bucket}">
    <div class="emo-head"><strong>${b.label}</strong>
      ${b.ref ? html`<span class="small muted">${clipLine(b.ref)}${b.ref.metrics && b.ref.metrics.similarity !== undefined && b.ref.metrics.similarity !== null ? ` · similarity ${Number(b.ref.metrics.similarity).toFixed(3)}` : ""}</span>
          ${b.ref.mock ? html`<span class="badge tone-warn">mock test tones</span>` : ""}
          ${player(`/media/voice/${v.id}/ref/${b.ref.id}`, `${b.label} reference clip`, "mini")}
          ${isAdmin() ? html`<button class="btn small ghost danger" type="button" data-act="ref-remove" data-ref="${b.ref.id}" data-label="${b.label}">Remove clip</button>` : ""}`
        : html`<span class="small muted">no clip yet (a related emotion's clip or the base reference is used)</span>`}</div>
    <div class="emo-actions">
      <button class="btn small" type="button" data-act="take" data-bucket="${b.bucket}" data-label="${b.label}" ${b.pending ? html`disabled` : ""}>Generate a take</button>
      <button class="btn small ghost" type="button" data-act="adopt-lab" data-bucket="${b.bucket}" data-label="${b.label}">Adopt a lab recording…</button>
      ${b.candidates.length || b.pending ? html`<button class="btn small ghost danger" type="button" data-act="discard-all" data-bucket="${b.bucket}" data-label="${b.label}">Discard all takes</button>` : ""}
    </div>
    ${b.pending ? html`<p class="small muted"><span class="spinner" aria-hidden="true"></span> ${(SOURCES[b.pending.source] || {}).label || "Take"}: ${b.pending.status === "queued" ? "waiting for the GPU worker…" : "synthesizing…"}</p>` : ""}
    ${b.candidates.length ? html`<ul class="take-list">${b.candidates.map((c) => takeRow(b, c, refs))}</ul>` : ""}
  </li>`)}</ul>`)}`;
}

function tools(v) {
  return html`<section class="card" aria-labelledby="tl-h"><h2 id="tl-h">Tools</h2>
    <ul class="tool-list">
      <li><button class="btn" type="button" data-act="edit">${v.builtin ? "Delivery…" : "Edit…"}</button>
        <span class="small muted">${v.kind === "clone" ? "Rename this narrator." : "Change its name and how it sounds (saved as a new version)."}</span></li>
      ${v.kind !== "clone" ? html`<li><button class="btn" type="button" data-act="duplicate">Duplicate &amp; retune…</button>
        <span class="small muted">An editable copy with a different delivery; ${v.name} itself is not changed.</span></li>` : ""}
      ${v.kind === "clone" && v.source_voice ? html`<li><button class="btn" type="button" data-act="retune">Retune from ‘${v.source_voice.name}’…</button>
        <span class="small muted">A new designed voice from the voice this clone came from, with a new description.</span></li>` : ""}
      ${!v.builtin && isAdmin() ? html`<li><button class="btn danger" type="button" data-act="archive" ${v.archived ? html`disabled` : ""}>Archive</button>
        <span class="small muted">No longer offered for new books. Existing books keep using it.</span></li>` : ""}
    </ul></section>`;
}

export async function render(view, parts, query, nav) {
  const id = Number(parts[0]);
  const signal = nav && nav.signal;
  let [v, list] = await Promise.all([get(`/api/voices/${id}`, { signal }), get("/api/voices", { signal })]);
  v.sample = (list.voices.find((x) => x.id === id) || {}).sample || { status: "none" };
  const tab = tabOf(query, v.kind);
  let refs = v.kind === "clone" && tab === "emotions" ? await get(`/api/voices/${id}/refs`, { signal }) : null;
  let pjob = null;
  if (tab === "preview" && lastPreview[id]) { try { pjob = await get(`/api/jobs/${lastPreview[id]}`, { signal }); } catch (e) { pjob = null; } }
  const local = {};
  const audio = new Audio();
  let disposed = false, seq = 0, applied = 0, lab = null;

  setHTML(view, html`<div class="page-head"><h1>${v.name}</h1><div class="head-actions"><a class="btn ghost" href="#/voices">← Narrator Studio</a></div></div>
    <div id="nr-head"></div>
    <div class="tabs" role="tablist" aria-label="Narrator sections">${NARRATOR_TABS.filter(([k]) => k !== "emotions" || v.kind === "clone").map(([k, l]) =>
      html`<a role="tab" class="tab ${tab === k ? "active" : ""}" aria-selected="${tab === k}" ${tab === k ? html`aria-current="page"` : ""} href="#/narrator/${id}?tab=${k}">${l}</a>`)}</div>
    <div role="tabpanel" id="nr-panel">
      ${tab === "overview" ? overview(v) : ""}
      ${tab === "preview" ? previewForm(v) : ""}
      ${tab === "emotions" ? emotionsIntro(v, refs) : ""}
      ${tab === "record" ? html`<section class="card" aria-labelledby="rc-h"><h2 id="rc-h">Record &amp; upload</h2>
        <p class="small muted">${v.kind === "clone"
          ? html`Record or upload a clip of ${v.name}'s speaker reading one of the <strong>Emotion reference</strong> scripts, check it with Whisper, then adopt it on the
            <a href="#/narrator/${id}?tab=emotions">Emotion references</a> tab. Saved takes are drafts for 14 days.`
          : "Record or upload a reference voice and turn it into a new cloned narrator."}</p><div id="lab-host"></div></section>` : ""}
      ${tab === "tools" ? tools(v) : ""}
    </div>`);

  const playingIn = (sel) => Array.from(view.querySelectorAll(`${sel} audio`)).some((a) => !a.paused && !a.ended);
  function paint() {
    if (disposed) return;
    const map = { "#nr-head": () => head(v, local) };
    if (tab === "preview") map["#nr-pv"] = () => previewResult(v, pjob);
    if (tab === "emotions" && refs) map["#nr-emo"] = () => emotionRows(v, refs);
    for (const sel of Object.keys(map)) if (sel !== "#nr-head" && playingIn(sel)) delete map[sel];  // never re-create a player while it plays
    patchRegions(view, map);
  }

  async function refresh() {
    if (disposed) return;
    const mine = ++seq;
    try {
      const [nl, nr, nj] = await Promise.all([
        get("/api/voices", { signal }),
        tab === "emotions" && v.kind === "clone" ? get(`/api/voices/${id}/refs`, { signal }) : null,
        tab === "preview" && lastPreview[id] && !(pjob && DONE.includes(pjob.status)) ? get(`/api/jobs/${lastPreview[id]}`, { signal }) : null]);
      if (disposed || mine < applied) return;
      applied = mine;
      const fresh = nl.voices.find((x) => x.id === id);
      if (fresh) v.sample = fresh.sample;
      if (nr) refs = nr;
      if (nj) pjob = nj;
    } catch (e) { return; }
    if (local.wanted && v.sample.status === "ready") { local.wanted = false; start(); }
    else if (local.wanted && v.sample.status === "none") { local.wanted = false; toast("The sample could not be generated. Generate a preview to see the error.", "warn", 9000); }
    paint();
  }

  function start() {
    audio.src = v.sample.url;
    audio.play().then(() => { local.playing = true; local.blocked = false; paint(); })
      .catch((e) => { if (e && e.name === "NotAllowedError") { local.blocked = true; paint(); } else toastError(e); });
  }
  const stopped = () => { if (local.playing) { local.playing = false; paint(); } };
  audio.addEventListener("ended", stopped);
  audio.addEventListener("pause", stopped);

  async function playSample() {
    if (local.playing) { audio.pause(); return; }
    if (v.sample.status === "ready") { start(); return; }
    if (v.sample.status === "none") { try { v.sample = await post(`/api/voices/${id}/sample`); } catch (e) { toastError(e); return; } }
    if (v.sample.status === "ready") start(); else local.wanted = true;
    paint();
  }

  async function reloadVoice() { try { v = { ...(await get(`/api/voices/${id}`)), sample: v.sample }; } catch (e) { /* keep */ } }

  async function adopt(b, bucket, label, rid, fromLab) {
    const body = { bucket, recording_id: rid };
    if (fromLab && refs && !refs.synthetic) {
      const res = await dialog({ title: `Adopt a clip for ${label}`, form: true, buttons: [{ label: "Adopt clip", value: "ok", kind: "primary" }],
        body: html`<p class="small">A recording must be of the same speaker as ${v.name}. (A take ${v.name} generated itself needs no statement.)</p>
          <label class="check"><input type="checkbox" name="owner_attest" value="true"> I own this recording, or have the speaker's written permission to clone it.</label>
          <label>Consent statement <input name="statement" maxlength="1000" placeholder="e.g. Recorded by the speaker on 2026-10-01 for this project"></label>` });
      if (!res) return;
      body.owner_attest = res.data.owner_attest === "true"; body.statement = res.data.statement || "";
    } else if (refs && refs.buckets.find((x) => x.bucket === bucket && x.ref)) {
      if (!(await confirmDialog(`Replace the ${label} clip?`, `The new take replaces ${v.name}'s current ${label.toLowerCase()} clip. Takes already narrated with the old clip keep their audio until you regenerate them.`, "Adopt"))) return;
    }
    try { refs = await busy(b, () => post(`/api/voices/${id}/refs/adopt`, body), `${label} reference clip adopted`); } catch (e) { return; }
    paint();
  }

  async function onClick(e) {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const act = b.dataset.act;
    if (act === "play") return playSample();
    if (act === "note-dismiss") { delete notes[id]; paint(); return; }
    if (act === "edit") {
      const out = await editVoice(b, v, list.speakers);
      if (out && out.duplicate !== undefined) { const nv = await duplicateVoice(b, v, list.speakers, list.voices, out.duplicate); if (nv) navigate(`#/narrator/${nv.id}?tab=preview`); return; }
      if (out && out.voice) {
        if (out.note) notes[id] = out.note;
        await reloadVoice();
        setHTML(view.querySelector("h1"), v.name);
        const panel = view.querySelector("#nr-panel");
        if (tab === "overview") setHTML(panel, overview(v));
        if (tab === "tools") setHTML(panel, tools(v));
        refresh();
      }
      return;
    }
    if (act === "duplicate") { const nv = await duplicateVoice(b, v, list.speakers, list.voices); if (nv) navigate(`#/narrator/${nv.id}?tab=preview`); return; }
    if (act === "retune") { const nv = await retuneVoice(b, v, list.voices); if (nv) navigate(`#/narrator/${nv.id}?tab=preview`); return; }
    if (act === "archive") {
      if (!(await confirmDialog("Archive narrator?", "It will no longer be offered for new books. Existing books keep using it.", "Archive", true))) return;
      try { await busy(b, () => post(`/api/voices/${id}/archive`), "Narrator archived"); } catch (err) { return; }
      navigate("#/voices"); return;
    }
    if (act === "promote") { const nv = await promotePreview(b, b.dataset.job, v.name); if (nv) navigate(`#/narrator/${nv.id}?tab=emotions`); return; }
    if (act === "take") {
      try {
        await busy(b, () => post(`/api/voices/${id}/refs/candidates`, { bucket: b.dataset.bucket, source: takeSource }), `${b.dataset.label} take queued (it runs between book segments)`);
      } catch (err) { return; }
      refresh(); return;
    }
    if (act === "discard") {
      try { refs = await api("DELETE", `/api/voices/${id}/refs/candidates/${b.dataset.rid}`); toast(`${b.dataset.label} take discarded`, "success"); } catch (err) { toastError(err); }
      paint(); return;
    }
    if (act === "discard-all") {
      if (!(await confirmDialog(`Discard every ${b.dataset.label.toLowerCase()} take?`, "Takes not adopted yet are deleted, and a take still waiting for the GPU worker is withdrawn. The adopted clip (if any) stays.", "Discard all", true))) return;
      try { refs = await api("DELETE", `/api/voices/${id}/refs/candidates?bucket=${encodeURIComponent(b.dataset.bucket)}`); toast(`${refs.discarded} ${b.dataset.label.toLowerCase()} take(s) discarded`, "success"); } catch (err) { toastError(err); }
      paint(); return;
    }
    if (act === "adopt-take") return adopt(b, b.dataset.bucket, b.dataset.label, b.dataset.rid, false);
    if (act === "adopt-lab") {
      let recs;
      try { recs = (await get("/api/recordings")).recordings.filter((r) => r.asr_check && r.asr_check.matches && (!r.target_voice_id || r.target_voice_id === id)); } catch (err) { toastError(err); return; }
      if (!recs.length) { toast("No lab recordings have passed the Whisper check yet. Record or upload one on the Record & upload tab and run its transcript check.", "warn", 9000); return; }
      const res = await dialog({ title: `Adopt a recording for ${b.dataset.label}`, form: true, buttons: [{ label: "Continue", value: "ok", kind: "primary" }],
        body: html`<label>Checked recording <select name="rid">${recs.map((r) => html`<option value="${r.id}">${r.label || r.script_title} · ${r.duration_s}s · ${fmtDate(r.created_at)}</option>`)}</select></label>
          <p class="small muted">Only recordings whose transcript matched the Whisper check are listed.</p>` });
      if (!res) return;
      return adopt(b, b.dataset.bucket, b.dataset.label, res.data.rid, true);
    }
    if (act === "ref-remove") {
      if (!(await confirmDialog("Remove reference clip?", `The ${b.dataset.label} clip is deleted. Takes already made with it keep their audio; new takes use a related clip or the base reference.`, "Remove", true))) return;
      try { refs = await api("DELETE", `/api/voices/${id}/refs/${b.dataset.ref}`); toast("Reference clip removed", "success"); } catch (err) { toastError(err); }
      paint();
    }
  }

  async function onSubmit(e) {
    const f = e.target;
    if (f.id !== "nr-preview-form") return;
    e.preventDefault();
    const fd = new FormData(f);
    let j;
    try { j = await busy(e.submitter, () => post(`/api/voices/${id}/preview`, { text: fd.get("text"), instruct: fd.get("instruct") || null, verify: fd.get("verify") === "on" })); } catch (err) { return; }
    lastPreview[id] = j.id; pjob = j; paint();
  }
  const onInput = (e) => {
    const f = e.target.closest && e.target.closest("#nr-preview-form");
    if (f) { const fd = new FormData(f); pforms[id] = { text: fd.get("text") || "", instruct: fd.get("instruct") || "", verify: fd.get("verify") === "on" }; }
    if (e.target.id === "take-source") {
      takeSource = e.target.value;
      const h = view.querySelector("#take-source-hint");
      if (h) h.textContent = (SOURCES[takeSource] || {}).hint || "";
    }
  };
  const onMediaIdle = () => setTimeout(refresh, 300);

  paint();
  if (tab === "record") {
    lab = createLab({ onVoiceCreated: (nv) => (nv && nv.id ? navigate(`#/narrator/${nv.id}`) : null) });
    view.querySelector("#lab-host").appendChild(lab.el);
    lab.init();
  }
  view.addEventListener("click", onClick);
  view.addEventListener("submit", onSubmit);
  view.addEventListener("input", onInput);
  view.addEventListener("change", onInput);
  view.addEventListener("ended", onMediaIdle, true);
  const off = onState(() => refresh(), 2000);
  return () => {
    disposed = true; off(); audio.pause(); audio.removeAttribute("src");
    view.removeEventListener("click", onClick); view.removeEventListener("submit", onSubmit);
    view.removeEventListener("input", onInput); view.removeEventListener("change", onInput);
    view.removeEventListener("ended", onMediaIdle, true);
    if (lab) lab.destroy();
  };
}
