// Book page: metadata, narrator, chapters & players, assembly, exports, QA report, release signoff, storage.
//
// Two kinds of content (v0.8.1):
//  * FORM cards (metadata, new-version / replace-narrator, release signoff, move) are drawn ONCE. A live update never touches them, so
//    nothing the reader is typing is lost. They are re-seeded from the server only after the reader's own successful save (seedForm,
//    which never overwrites a field the reader has changed since).
//  * LIVE regions (header, version bar, banner, stats, narrative, chapters + players, exports, release status, voice info, storage, delete,
//    log, cover) have a stable id and are repainted in place by patchRegions(), and only when their markup changed.
import { api, get, post, patch, html, badge, progressBar, fmtDuration, fmtDate, fmtBytes, fmtAgo, toast, toastError, busy, dialog, confirmDialog, onState, player, ApiError,
  isAdmin, navigate, setHTML, patchRegions } from "../ui.js";

// ------------------------------------------------------------------------------------------------ live templates (pure)
const busyJobOf = (b) => b.utility_jobs.find((j) => ["queued", "running"].includes(j.status));
const lastUtilOf = (b) => b.utility_jobs[0];

function headTpl(b, id) {
  const activeBookJob = b.job && ["queued", "running", "paused"].includes(b.job.status);
  return html`<div class="page-head"><h1 id="bk-h1" tabindex="-1">${b.display_title}</h1>
    <div class="head-actions">${badge(b.state)}
      <a class="btn primary" id="bk-review" href="#/review/${id}?filter=${b.flagged ? "flagged" : "all"}">Review audio</a>
      ${b.pending && !activeBookJob ? html`<button class="btn" id="bk-queue" data-act="queue">Queue generation</button>` : ""}</div></div>`;
}

function versionsTpl(b) {
  if (b.versions.length <= 1) return "";
  return html`<div class="card version-bar"><label>Narrator version
      <select id="ver-pick">${b.versions.map((v) => html`<option value="${v.id}" ${v.current ? html`selected` : ""}>${v.narrator || "–"} · ${Math.round(v.progress * 100)}% rendered${v.archived ? " · archived" : ""}</option>`)}</select></label>
    <span class="small muted">${b.versions.length} narrations of this manuscript. Each has its own audio, review and exports.</span></div>`;
}

function bannerTpl(b) {
  return html`<div class="callout ${b.release ? "good" : "info"} small">${b.release
    ? html`<strong>Approved for publication</strong> by ${b.release.approved_by} on ${fmtDate(b.release.approved_at)} (${b.release.listen_scope} listen). Exports made after approval are final; earlier ones stay labelled DRAFT.`
    : html`<strong>Draft.</strong> Exports are labelled DRAFT until a human signs off below. This studio produces local publication candidates only – nothing is uploaded to Audible, ACX or any host.`}</div>`;
}

function statsTpl(b) {
  return html`<div class="stats compact">
      <div class="stat"><div class="stat-value">${b.produced}/${b.segments}</div><div class="stat-label">segments rendered</div>${progressBar(b.progress, "Rendered")}</div>
      <div class="stat ${b.flagged ? "warn" : "good"}"><div class="stat-value">${b.flagged}</div><div class="stat-label">need review</div></div>
      <div class="stat"><div class="stat-value">${b.passed}</div><div class="stat-label">QA passed</div></div>
      <div class="stat approved"><div class="stat-value">${b.approved}</div><div class="stat-label">approved</div></div>
      <div class="stat"><div class="stat-value">${fmtDuration(b.audio_duration_s)}</div><div class="stat-label">audio (est. ${fmtDuration(b.est_duration_s)})</div></div>
    </div>
    ${b.last_error ? html`<div class="callout bad small"><strong>Last error:</strong> ${b.last_error}</div>` : ""}`;
}

function narrativeTpl(b, id) {
  const n = b.narrative;
  if (!n) return "";
  return html`<section class="card" aria-labelledby="nar-sum-h"><h2 id="nar-sum-h">Narrative analysis</h2>
    ${n.semantic
      ? html`<p class="small">Semantic segmentation: <strong>${n.characters}</strong> character(s) · <strong>${n.directed}</strong>/${n.segments} segments directed ·
          ${n.needs_review ? html`<a class="warn-text" href="#/review/${id}?filter=speaker_review"><strong>${n.needs_review}</strong> line(s) need a speaker</a>` : html`<span class="good-text">no speaker flags</span>`}
          ${n.direction_review ? html` · <a class="warn-text" href="#/review/${id}?filter=direction_review">${n.direction_review} direction(s) to review</a>` : ""}</p>`
      : html`<p class="small muted">This book uses the classic segmenter. Convert it to get speaker analysis, a character directory and the Narrative Director.</p>`}
    <a class="btn" href="#/narrative/${id}">Open narrative page</a></section>`;
}

function chaptersTpl(b, id) {
  const busyJob = busyJobOf(b), lastUtil = lastUtilOf(b);
  return html`<h2 id="ch-h">Chapters</h2>
      <div class="form-actions"><button class="btn" id="bk-assemble" data-act="assemble" ${busyJob ? html`disabled` : ""}>Assemble chapter masters (WAV + MP3)</button>
        ${busyJob ? html`<span class="small muted"><span class="spinner" aria-hidden="true"></span> ${busyJob.kind} job #${busyJob.id} ${busyJob.status}…</span>` : ""}
        ${lastUtil && lastUtil.status === "failed" ? html`<span class="small err">Last ${lastUtil.kind} job: ${lastUtil.error}</span>` : ""}</div>
      <div class="table-wrap" id="bk-chapters-wrap"><table class="table">
        <thead><tr><th scope="col">#</th><th scope="col">Chapter</th><th scope="col">Segments</th><th scope="col">Master</th><th scope="col">Downloads</th></tr></thead>
        <tbody>${b.chapters.map((c) => {
          const sc = c.status_counts, mp3 = c.renders.mp3, wav = c.renders.wav;
          return html`<tr><td>${c.index}</td>
            <td><strong>${c.title}</strong>${c.title_overridden ? html` <span class="small muted">(renamed; original: ${c.original_title})</span>` : ""}
              <div class="small"><button class="linklike" id="ch-title-${c.id}" data-act="chapter-title" data-ch="${c.id}">Edit title</button> · <button class="linklike" id="ch-regen-${c.id}" data-act="chapter-regen" data-ch="${c.id}" ${busyJob ? html`disabled` : ""}>Regenerate chapter</button></div>
              <div class="small muted">${c.words.toLocaleString()} words · est. ${fmtDuration(c.est_duration_s)}</div></td>
            <td class="small">${(sc.passed || 0) + (sc.approved || 0) + (sc.flagged || 0)}/${c.segments} rendered${sc.flagged ? html` · <a href="#/review/${id}?chapter=${c.id}&filter=flagged" class="warn-text">${sc.flagged} flagged</a>` : ""}
              ${c.missing_audio ? html`<div class="err">${c.missing_audio} missing – assembly blocked</div>` : ""}</td>
            <td>${mp3 ? html`${player(`/media/chapter/${c.id}.mp3`, `Chapter ${c.index} master`, "mini")}<div class="small muted">${fmtDuration(mp3.duration_s)} · ${mp3.draft ? "DRAFT" : "final"}${c.render_current ? "" : html` · <span class="warn-text">outdated</span>`}</div>` : html`<span class="small muted">not assembled</span>`}</td>
            <td class="small nowrap">${mp3 ? html`<a href="/media/chapter/${c.id}.mp3?download=1">MP3</a> · ` : ""}${wav ? html`<a href="/media/chapter/${c.id}.wav?download=1">WAV</a> · ` : ""}
              <button class="linklike" id="ch-m4b-${c.id}" data-act="chapter-m4b" data-ch="${c.id}" ${c.missing_audio ? html`disabled` : ""}>M4B</button></td></tr>`;
        })}</tbody></table></div>`;
}

function blockedList(job) {
  let r = {};
  try { r = JSON.parse(job.result_json) || {}; } catch { r = {}; }
  if (!r.blocked || !r.blocked.length) return "";
  return html`<details id="bk-blocked" class="callout bad small" open><summary>Blocked: ${r.blocked.length} problem(s)</summary><ul>${r.blocked.slice(0, 40).map((x) => html`<li>Chapter ${x.chapter}: <code>${x.sid || ""}</code> ${x.problem || ""}</li>`)}</ul></details>`;
}

function exportsTpl(b) {
  const busyJob = busyJobOf(b), lastUtil = lastUtilOf(b);
  return html`<h2 id="ex-h">Exports</h2>
      <p class="small muted">Exports never omit missing segments: if any segment lacks usable audio the export is blocked and the gaps are listed. Outputs are verified with ffprobe (codec, duration, chapter markers).</p>
      <div class="form-actions">
        <button class="btn primary" id="bk-export-m4b" data-act="export-m4b" ${busyJob ? html`disabled` : ""}>Build ${b.release ? "final" : "DRAFT"} M4B (chapter-marked AAC)</button>
        <button class="btn" id="bk-export-zip" data-act="export-zip" ${busyJob ? html`disabled` : ""}>Chapter MP3s (ZIP)</button>
        <button class="btn" id="bk-report" data-act="report">QA report (HTML + JSON)</button></div>
      ${lastUtil && lastUtil.status === "failed" && lastUtil.result_json ? blockedList(lastUtil) : ""}
      ${b.exports.length ? html`<ul class="exports">${b.exports.map((e) => html`<li><a href="/media/export/${e.id}">${e.label}</a>
        ${e.draft ? html`<span class="badge tone-warn">DRAFT</span>` : html`<span class="badge tone-approved">final</span>`}
        <span class="small muted">${fmtBytes(e.size_bytes)}${e.duration_s ? " · " + fmtDuration(e.duration_s) : ""} · ${fmtAgo(e.created_at)}</span>
        ${e.kind === "report_html" ? html` · <a class="small" href="/media/export/${e.id}?download=0" target="_blank" rel="noopener">view</a>` : ""}</li>`)}</ul>` : html`<p class="muted small">No exports yet.</p>`}`;
}

function releaseTpl(b) {
  const rc = b.release_check;
  return b.release
    ? html`<p>Approved by <strong>${b.release.approved_by}</strong> · ${fmtDate(b.release.approved_at)} · ${b.release.listen_scope} listen.</p>
        <button class="btn danger ghost" id="bk-revoke" data-act="revoke">Revoke approval</button>`
    : html`${rc.ready ? html`<p class="good-text">All audio is accounted for and no automatic QA flags are open.</p>` : html`<p><strong>Not ready:</strong></p><ul class="blockers">${rc.blockers.map((x) => html`<li>${x}</li>`)}</ul>`}`;
}

function voiceTpl(b) {
  return html`<p><strong>${b.voice_detail.name}</strong> (${b.voice_detail.kind}, v${b.voice_detail.version}) · <code class="small">${b.voice_detail.model_id}</code>
        ${b.voice_detail.speaker ? html` · speaker ${b.voice_detail.speaker}` : ""}</p>
      ${b.voice_outdated ? html`<p class="callout info small">This voice has a newer version. Switching regenerates the whole book.</p>` : ""}`;
}

const verNoteTpl = (b) => html`Starts a new version of this book (same text, chapters and characters) in a different voice. The ${b.voice_detail.name} narration stays exactly as it is.`;
const fingerprintTpl = (b) => html`Book settings are a snapshot taken at import (fingerprint <code>${b.synth_fingerprint}</code>). Takes with another fingerprint are never mixed into this book.`;
const storageTpl = (b) => html`${Object.entries(b.storage).map(([k, v]) => html`<dt>${k}</dt><dd><code>${v}</code></dd>`)}`;
const archiveTpl = (b) => html`<button class="btn small ghost" id="bk-archive-btn" data-act="archive">${b.archived ? "Unarchive" : "Archive book"}</button>`;

function deleteTpl(b) {
  if (!isAdmin()) return "";
  return html`<section class="card" aria-labelledby="del-h"><h2 id="del-h">Delete</h2>
      <p class="small muted">Administrators only. Deleting removes the audio, exports and history for good and cannot be undone; use Disk &amp; Backups to back a book up first. A book with a queued, rendering or paused job cannot be deleted.</p>
      ${b.versions.length > 1
        ? html`<p class="small">This book has <strong>${b.versions.length} narrator versions</strong>. You can delete just this one, or the whole book.</p>
          <button class="btn danger" type="button" id="bk-del-version" data-act="del-version">Delete the ${b.narrator} version…</button>
          <button class="btn danger" type="button" id="bk-del-book" data-act="del-book">Remove book and all ${b.versions.length} versions…</button>`
        : html`<button class="btn danger" type="button" id="bk-del-book" data-act="del-book">Remove this book…</button>`}
    </section>`;
}

const logTpl = (b) => html`<h2 id="log-h">Project log</h2>
      <ol class="feed small">${b.logs.map((l) => html`<li class="lvl-${l.level}"><span class="muted">${fmtAgo(l.created_at)}</span> ${l.message}</li>`)}</ol>`;

// ------------------------------------------------------------------------------------------------ form helpers
const seeds = new WeakMap();   // field -> the value the server last gave it (what "unchanged" means)
const fieldsOf = (form) => Array.from(form.elements).filter((e) => e.name && e.type !== "submit" && e.type !== "button");
function markSeeds(form) { if (form) fieldsOf(form).forEach((el) => seeds.set(el, el.value)); }
/** True when the reader has changed any field of `form` since it was drawn or last seeded. */
export function formDirty(form) { return !!form && fieldsOf(form).some((el) => el.type !== "file" && seeds.has(el) && el.value !== seeds.get(el)); }
/**
 * seedForm(form, values, submitted?): put server `values` into the named fields. A field is overwritten only if the reader has not
 * touched it: after their own save `submitted` is what they sent (still equal to it = untouched since); otherwise untouched means equal
 * to the last seeded value. Either way the baseline moves to the server value, so a field the reader kept editing stays dirty.
 */
export function seedForm(form, values, submitted) {
  if (!form) return;
  for (const [name, v] of Object.entries(values)) {
    const el = form.elements.namedItem(name);
    if (!el || !("value" in el) || typeof el.length === "number" && !el.tagName) continue;
    const next = v === null || v === undefined ? "" : String(v);
    const untouched = submitted ? el.value === String(submitted[name] ?? "") : el.value === seeds.get(el);
    if (untouched) el.value = next;
    seeds.set(el, next);
  }
}
const META_FIELDS = ["title", "author", "narrator_credit", "publisher", "year", "genre", "description"];
const metaValues = (b) => Object.fromEntries(META_FIELDS.map((k) => [k, b[k]]));

// ------------------------------------------------------------------------------------------------ page
export async function render(view, parts, query, nav) {
  const id = Number(parts[0]);
  const signal = nav && nav.signal;
  // first paint: both requests in parallel
  const [first, vs] = await Promise.all([get(`/api/books/${id}`, { signal }), get("/api/voices", { signal })]);
  let b = first;
  const voices = vs.voices;
  let disposed = false, seq = 0, applied = 0, inflight = false, queued = false, coverBust = 0;

  const regions = () => ({
    "#bk-head": () => headTpl(b, id),
    "#bk-versions": () => versionsTpl(b),
    "#bk-banner": () => bannerTpl(b),
    "#bk-stats": () => statsTpl(b),
    "#bk-narrative": () => narrativeTpl(b, id),
    "#bk-chapters": () => chaptersTpl(b, id),
    "#bk-exports": () => exportsTpl(b),
    "#bk-release-live": () => releaseTpl(b),
    "#bk-voice": () => voiceTpl(b),
    "#bk-ver-note": () => verNoteTpl(b),
    "#bk-fingerprint": () => fingerprintTpl(b),
    "#bk-cover-slot": () => (b.has_cover
      ? html`<img class="cover" id="cover-img" src="/media/cover/${id}?v=${b.cover_version || 0}${coverBust ? "&u=" + coverBust : ""}" alt="Cover of ${b.title}">`
      : html`<div class="cover placeholder" id="cover-img" aria-hidden="true">No cover</div>`),
    "#bk-storage": () => storageTpl(b),
    "#bk-archive": () => archiveTpl(b),
    "#bk-delete": () => deleteTpl(b),
    "#bk-log": () => logTpl(b),
  });

  const isField = (n) => n && ["input", "textarea", "select"].includes(String(n.tagName).toLowerCase());

  /** Repaint the live regions whose markup changed. Never touches a form card. */
  function patchLive() {
    if (disposed) return;
    const map = regions();
    const playing = Array.from(view.querySelectorAll("#bk-chapters audio")).some((a) => !a.paused && !a.ended);
    if (playing) delete map["#bk-chapters"];                      // never re-create a mini player while it plays
    const ae = document.activeElement;
    for (const sel of Object.keys(map)) {                         // a region holding the field the reader is in (the version picker) waits
      const el = view.querySelector(sel);
      if (el && ae && el !== ae && el.contains(ae) && isField(ae)) delete map[sel];
    }
    patchRegions(view, map);
    const rf = view.querySelector("#release-form");               // the release form is drawn once; only its visibility / submit state follows the data
    if (rf) {
      rf.hidden = !!b.release;
      const sub = rf.querySelector("button[type=submit]");
      if (sub) sub.disabled = !b.release_check.ready;
    }
    document.title = `${b.display_title} · Audiobook Studio`;
  }

  setHTML(view, html`
    <div id="bk-head"></div>
    <div id="bk-versions"></div>
    <div id="bk-banner"></div>
    <div id="bk-stats"></div>
    <div id="bk-narrative"></div>
    <section class="card" id="bk-chapters" aria-labelledby="ch-h"></section>

    <div class="grid-2">
    <section class="card" id="bk-exports" aria-labelledby="ex-h"></section>

    <section class="card" aria-labelledby="rel-h"><h2 id="rel-h">Release signoff</h2>
      <div id="bk-release-live"></div>
      <form id="release-form" class="form">
        <label>Approved by <input name="approved_by" required minlength="2" maxlength="120" autocomplete="name"></label>
        <fieldset class="inline"><legend>Listening review performed</legend>
          <label class="check"><input type="radio" name="listen_scope" value="full" checked> Full listen (recommended)</label>
          <label class="check"><input type="radio" name="listen_scope" value="partial"> Partial / spot check</label></fieldset>
        <label>Notes <input name="notes" maxlength="2000"></label>
        <label class="check consent"><input type="checkbox" name="attest" required> I reviewed this audiobook, I hold the rights to the manuscript and the narrator voice,
          and I approve these files as publication candidates. I understand automatic QA (Whisper agreement) is not proof of correct pronunciation or performance,
          and that distributor requirements must be checked separately.</label>
        <button class="btn primary" type="submit">Record release approval</button>
      </form>
    </section></div>

    <div class="grid-2">
    <section class="card" aria-labelledby="meta-h"><h2 id="meta-h">Metadata &amp; cover</h2>
      <form id="meta-form" class="form"><div class="form-grid">
        <label>Title <input name="title" required maxlength="300" value="${b.title}"></label>
        <label>Author <input name="author" maxlength="300" value="${b.author}"></label>
        <label>Narrator credit <input name="narrator_credit" maxlength="200" value="${b.narrator_credit}"></label>
        <label>Publisher <input name="publisher" maxlength="200" value="${b.publisher}"></label>
        <label>Year <input name="year" maxlength="20" value="${b.year}"></label>
        <label>Genre <input name="genre" maxlength="60" value="${b.genre}"></label></div>
        <label>Description <textarea name="description" rows="2" maxlength="4000">${b.description}</textarea></label>
        <button class="btn" type="submit">Save metadata</button></form>
      <div class="cover-row"><span id="bk-cover-slot"></span>
        <label class="btn small">Upload cover (JPEG/PNG)<input type="file" id="cover-input" accept=".jpg,.jpeg,.png" class="sr-only"></label></div>
    </section>

    <section class="card" aria-labelledby="nar-h"><h2 id="nar-h">Narrator</h2>
      <div id="bk-voice"></div>
      <h3>Narrate with another voice</h3>
      <p class="small muted" id="bk-ver-note"></p>
      <form id="ver-form" class="form">
        <label>Voice <select name="voice_id" required><option value="" disabled selected>Choose a narrator…</option>${voices.map((v) => html`<option value="${v.id}">${v.name} (${v.kind})${b.versions.some((x) => x.narrator === v.name) ? " – has a version" : ""}</option>`)}</select></label>
        <label>Delivery instructions <input name="instructions" maxlength="1000" value="${b.instructions}"></label>
        <div class="form-actions"><button class="btn primary" type="submit" data-queue="0">Create new version</button>
          <button class="btn" type="submit" data-queue="1">Create &amp; queue for narration</button></div></form>
      <details id="replace-narrator"><summary>Replace this version's narrator instead</summary>
        <p class="small muted">This changes the voice of <strong>this</strong> version and regenerates its audio; the earlier take is kept as history only. Use “Narrate with another voice” above to keep it.</p>
        <form id="nar-form" class="form">
          <label>Voice <select name="voice_id">${voices.map((v) => html`<option value="${v.id}" ${v.id === b.voice_detail.id ? html`selected` : ""}>${v.name} (${v.kind})</option>`)}</select></label>
          <label>Delivery instructions <input name="instructions" maxlength="1000" value="${b.instructions}"></label>
          <button class="btn" type="submit">Apply narrator change…</button></form></details>
      <p class="small muted" id="bk-fingerprint"></p>
      <button class="btn ghost small" id="bk-resnapshot" data-act="resnapshot">Apply current global settings to this book…</button>
    </section></div>

    <div class="grid-2">
    <section class="card" aria-labelledby="ms-h"><h2 id="ms-h">Manuscript manifest</h2>
      <p class="small">${b.manuscript.name} · ${b.manuscript.format} · ${fmtBytes(b.manuscript.size_bytes)} · <a href="/media/manuscript/${id}">download original</a><br>SHA-256 <code class="small">${b.manuscript.sha256}</code></p>
      <details id="manifest-details"><summary>What is and is not narrated (${(b.manuscript.manifest.chapters || []).length} source sections)</summary>
        <ul class="small">${(b.manuscript.manifest.chapters || []).map((c) => html`<li>${c.narrated ? "✓" : "✗"} ${c.title} (${c.words} words)${c.excluded_reason ? html` – <em>${c.excluded_reason}</em>` : ""}
          ${c.not_read.length ? html`<ul>${c.not_read.map((n) => html`<li class="muted">${n.reason}: ${n.text}</li>`)}</ul>` : ""}</li>`)}</ul></details>
    </section>
    <section class="card" aria-labelledby="st-h"><h2 id="st-h">Storage</h2>
      <dl class="kv small" id="bk-storage"></dl>
      <form id="move-form" class="form"><label>Move this project to another library root <input name="new_root" placeholder="/Volumes/AudiobooksSSD/Library" maxlength="4096"></label>
        <button class="btn small" type="submit">Copy, verify &amp; switch…</button>
        <p class="small muted">Files are copied and verified (size + SHA-256) before the database is switched. The old copy is kept; delete it yourself once satisfied.</p></form>
      <div id="bk-archive"></div>
    </section></div>

    <div id="bk-delete"></div>

    <section class="card" id="bk-log" aria-labelledby="log-h"></section>`);
  const $ = (sel) => view.querySelector(sel);
  patchLive();
  ["#meta-form", "#ver-form", "#nar-form", "#release-form", "#move-form"].forEach((s) => markSeeds($(s)));

  // ---- fetching: live refreshes never run over a newer response, and never repaint a form
  async function load() {
    const mine = ++seq;
    const nb = await get(`/api/books/${id}`, { signal });
    if (disposed || mine < applied) return null;
    applied = mine; b = nb;
    return nb;
  }
  async function reload() { try { if (await load()) patchLive(); } catch (e) { if (!e || e.name !== "AbortError") { /* transient */ } } }
  async function refreshLive() {
    if (disposed || (typeof document !== "undefined" && document.hidden)) return;
    if (inflight) { queued = true; return; }
    inflight = true;
    try { await reload(); } finally { inflight = false; if (queued && !disposed) { queued = false; refreshLive(); } }
  }

  /** Two-step admin delete: ask the server what would go (409 + preview), then require the name to be typed before sending confirm:true. */
  async function deleteFlow(path, { heading, typed, intro }) {
    let preview;
    try { await post(path, { confirm: false }); return false; }
    catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.details) preview = e.details;
      else { toastError(e); return false; }       // e.g. the book still has an active job
    }
    const res = await dialog({ title: heading, form: true, buttons: [{ label: "Delete forever", value: "ok", kind: "danger" }],
      body: html`<p>${intro(preview)}</p><label>Type <strong>${typed}</strong> to confirm <input name="typed" autocomplete="off" required></label>` });
    if (!res) return false;
    if (res.data.typed.trim() !== typed.trim()) { toast("That did not match. Nothing was deleted.", "error", 8000); return false; }
    const r = await busy(null, () => post(path, { confirm: true }));
    toast(`Deleted. Freed ${fmtBytes(r.freed_bytes)}`, "success");
    return true;
  }

  /** A narrator change alters the voice, the instructions and the fingerprint. Show what the server kept in the replace-narrator form (what the
   *  reader submitted, unless they typed more since) and in the new-version form's instructions (only if the reader has not touched it). */
  function reseedNarratorForms(submitted) {
    seedForm($("#nar-form"), { voice_id: b.voice_detail.id, instructions: b.instructions }, submitted);
    seedForm($("#ver-form"), { instructions: b.instructions });
  }

  async function narratorChange(body, btn) {
    const submitted = { voice_id: String(body.voice_id), instructions: body.instructions };
    try {
      await busy(btn, () => post(`/api/books/${id}/narrator`, body));
      toast("Narrator updated", "success");
      await reload(); reseedNarratorForms(submitted);
      return;
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.details && e.details.rendered_segments !== undefined) {
        if (await confirmDialog("Regenerate the whole book?", e.message, "Change narrator & regenerate", true)) {
          await busy(btn, () => post(`/api/books/${id}/narrator`, { ...body, confirm_invalidate: true }), "Narrator changed; all segments will be regenerated");
          await post("/api/queue", { book_id: id }).catch(() => {});
          await reload(); reseedNarratorForms(submitted);
          return;
        }
      }
    }
    await reload();
  }

  function wire() {
    $("#ver-form").addEventListener("submit", async (e) => {
      e.preventDefault(); const fd = new FormData(e.target);
      if (!fd.get("voice_id")) { toast("Choose a narrator for the new version.", "error"); e.target.voice_id.focus(); return; }
      const queue = !!(e.submitter && e.submitter.dataset.queue === "1");
      try {
        const nb = await busy(e.submitter, () => post(`/api/books/${id}/versions`, { voice_id: Number(fd.get("voice_id")), instructions: fd.get("instructions"), queue }));
        toast(`“${nb.display_title}” created${queue ? " and queued" : ""}. The ${b.narrator} narration is unchanged.`, "success", 8000);
        navigate(`#/book/${nb.id}`);
      } catch { /* toast shown */ }
    });
    $("#meta-form").addEventListener("submit", async (e) => {
      e.preventDefault(); const sent = Object.fromEntries(new FormData(e.target).entries());
      try {
        const nb = await busy(e.submitter, () => patch(`/api/books/${id}`, sent), "Metadata saved");
        b = nb; applied = ++seq;
        seedForm($("#meta-form"), metaValues(nb), sent);   // the reader's own save: show what the server kept (trimmed), keep anything typed since
        patchLive();
      } catch { /* toast shown */ }
    });
    $("#nar-form").addEventListener("submit", async (e) => {
      e.preventDefault(); const fd = new FormData(e.target);
      await narratorChange({ voice_id: Number(fd.get("voice_id")), instructions: fd.get("instructions") }, e.submitter);
    });
    $("#cover-input").addEventListener("change", async (e) => {
      const input = e.target, f = input.files[0]; if (!f) return;
      const fd = new FormData(); fd.append("file", f, f.name);
      const before = b.cover_version;
      try {
        const nb = await busy(null, () => api("POST", `/api/books/${id}/cover`, fd), "Cover saved");
        if (nb.cover_version === before) coverBust++;      // replaced within the same second: still force the browser to refetch
        b = nb; applied = ++seq;
        patchLive();                                       // only the cover slot (and anything else that really changed) is repainted; the metadata form is untouched
      } catch { /* toast shown */ }
      input.value = "";
    });
    const rf = $("#release-form");
    rf.addEventListener("submit", async (e) => {
      e.preventDefault(); if (!rf.reportValidity()) return;
      const fd = new FormData(rf);
      try {
        const nb = await busy(e.submitter, () => post(`/api/books/${id}/release`, { approved_by: fd.get("approved_by"), listen_scope: fd.get("listen_scope"), attest: fd.get("attest") === "on", notes: fd.get("notes") || "" }),
          "Release approval recorded. Export again to produce final (non-draft) files.");
        b = nb; applied = ++seq;
        rf.reset(); markSeeds(rf);                          // approved: the form is hidden and starts empty if the approval is ever revoked
        patchLive();
      } catch { /* toast shown */ }
    });
    $("#move-form").addEventListener("submit", async (e) => {
      e.preventDefault(); const form = e.target, root = new FormData(form).get("new_root").trim(); if (!root) return;
      if (!(await confirmDialog("Move project?", `Copy all files of '${b.title}' under ${root}, verify them, then switch the project to the new location?`, "Move"))) return;
      try {
        const r = await busy(e.submitter, () => post(`/api/books/${id}/move`, { new_root: root }));
        toast(`Moved: ${r.files} files verified. Old copy kept.`, "success", 9000);
        form.reset(); markSeeds(form);
        await reload();
      } catch { /* toast shown */ }
    });
    view.addEventListener("change", onChange);
  }

  function onChange(e) {
    const pick = e.target.closest && e.target.closest("#ver-pick");
    if (pick && view.contains(pick)) navigate(`#/book/${pick.value}`);
  }

  async function onClick(e) {
    const t = e.target.closest("[data-act]"); if (!t || !view.contains(t)) return;
    const a = t.dataset.act;
    try {
      if (a === "queue") { const j = await busy(t, () => post("/api/queue", { book_id: id })); toast(j.created ? "Queued" : "Already queued", "success"); }
      else if (a === "assemble") await busy(t, () => post(`/api/books/${id}/assemble`), "Assembly queued (runs on the worker between segments)");
      else if (a === "export-m4b") await busy(t, () => post(`/api/books/${id}/export`, { kind: "m4b" }), "M4B export queued");
      else if (a === "export-zip") await busy(t, () => post(`/api/books/${id}/export`, { kind: "mp3_zip" }), "MP3 ZIP export queued");
      else if (a === "chapter-m4b") await busy(t, () => post(`/api/books/${id}/export`, { kind: "chapter_m4b", chapter_id: Number(t.dataset.ch) }), "Chapter M4B export queued");
      else if (a === "chapter-title") {
        const c = b.chapters.find((x) => x.id === Number(t.dataset.ch));
        const res = await dialog({ title: `Chapter ${c.index} title`, form: true, buttons: [{ label: "Save", value: "ok", kind: "primary" }],
          body: html`<label>Title (empty restores the original) <input name="title" maxlength="300" value="${c.title}"></label><p class="small muted">Original: ${c.original_title}. Renaming never regenerates audio; re-assemble to update the chapter master tags.</p>` });
        if (!res) return;
        await busy(t, () => patch(`/api/books/${id}/chapters/${c.id}`, { title: res.data.title || "" }), "Chapter title saved");
      } else if (a === "chapter-regen") {
        const cid = Number(t.dataset.ch);
        try { await busy(t, () => post(`/api/books/${id}/chapters/${cid}/regenerate`, { confirm_invalidate: false }), "Chapter queued for regeneration"); }
        catch (err) {
          if (err.status === 409 && (await confirmDialog("Regenerate this chapter?", err.message, "Regenerate chapter", true))) {
            await busy(t, () => post(`/api/books/${id}/chapters/${cid}/regenerate`, { confirm_invalidate: true }), "Chapter queued for regeneration");
          }
        }
      }
      else if (a === "report") { const r = await busy(t, () => post(`/api/books/${id}/report`)); toast(`QA report written (${r.findings} findings)`, "success"); }
      else if (a === "revoke") {
        const res = await dialog({ title: "Revoke release approval", form: true, buttons: [{ label: "Revoke", value: "ok", kind: "danger" }], body: html`<label>Reason <input name="reason" maxlength="500"></label>` });
        if (!res) return; await busy(t, () => post(`/api/books/${id}/release/revoke`, { reason: res.data.reason || "" }), "Approval revoked");
      } else if (a === "resnapshot") {
        try { await busy(t, () => post(`/api/books/${id}/resnapshot`, { confirm_invalidate: false }), "Current QA/pause/export settings applied"); }
        catch (err) {
          if (err.status === 409 && (await confirmDialog("Regenerate audio?", err.message, "Apply & regenerate", true))) {
            await busy(t, () => post(`/api/books/${id}/resnapshot`, { confirm_invalidate: true }), "Settings applied; segments marked for regeneration");
          }
        }
      } else if (a === "archive") { await busy(t, () => post(`/api/books/${id}/archive`, { archived: !b.archived }), b.archived ? "Unarchived" : "Archived"); }
      else if (a === "del-version") {
        const ok = await deleteFlow(`/api/disk/books/${id}/delete`, { heading: `Delete the ${b.narrator} version`, typed: b.display_title,
          intro: (p) => `This permanently deletes “${b.display_title}” with all its audio, exports and history (${fmtBytes(p.bytes)}). The other ${p.other_versions} narrator version(s) are not affected.` });
        if (ok) { const other = b.versions.find((v) => v.id !== id); navigate(other ? `#/book/${other.id}` : "#/library"); return; }
      } else if (a === "del-book") {
        const ok = await deleteFlow(`/api/disk/books/${id}/delete-all-versions`, { heading: "Remove book", typed: b.title,
          intro: (p) => `This permanently removes “${b.title}” and ${p.versions.length === 1 ? "its narration" : `all ${p.versions.length} narrator versions`}, with all audio, exports and history (${fmtBytes(p.bytes)}).` });
        if (ok) { navigate("#/library"); return; }
      }
    } catch { /* toast shown */ }
    await reload();
  }

  // a chapter player that stopped (pause / ended) frees the chapters region to catch up with what it missed
  let idleTimer = null;
  const onMediaIdle = (e) => { if (e.target && e.target.closest && e.target.closest("#bk-chapters")) { clearTimeout(idleTimer); idleTimer = setTimeout(refreshLive, 400); } };
  const onVisible = () => { if (!document.hidden) refreshLive(); };

  wire();
  view.addEventListener("click", onClick);
  view.addEventListener("pause", onMediaIdle, true);
  view.addEventListener("ended", onMediaIdle, true);
  document.addEventListener("visibilitychange", onVisible);
  const off = onState(() => { refreshLive(); }, 3000);
  return () => {
    disposed = true; off(); clearTimeout(idleTimer);
    view.removeEventListener("click", onClick); view.removeEventListener("change", onChange);
    view.removeEventListener("pause", onMediaIdle, true); view.removeEventListener("ended", onMediaIdle, true);
    document.removeEventListener("visibilitychange", onVisible);
  };
}
