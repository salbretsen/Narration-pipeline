// Settings & Storage: seven tabs (#/settings/<tab>) over ONE form with ONE Save; server-validated paths, disk status, every tunable.
import { get, post, put, del, html, setHTML, patchHTML, snapshotForm, applyForm, confirmDialog, fmtBytes, fmtDate, toast, toastError, busy } from "../ui.js";
import { allFields, getPath, narrativeInput } from "./narrative-fields.js";
import { SECTIONS, NARRATIVE_LABELS, TABS, DEFAULT_TAB, resolveTab, tabKeyTarget, changedNames, countByGroup, verifySummary, collectSettings } from "./settings-model.js";

/** Server-side folder browser: lists real folders on the hosting Mac (admin only). Resolves to a path or null. */
function browseFolder(startPath) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "modal browse";
    dlg.setAttribute("aria-labelledby", "br-title");
    document.body.appendChild(dlg);
    let cur = null, result = null;
    const close = () => { dlg.close(); };
    dlg.addEventListener("close", () => { dlg.remove(); resolve(result); });
    dlg.addEventListener("cancel", () => { result = null; });

    async function go(path) {
      let d;
      try { d = await get("/api/storage/browse" + (path ? `?path=${encodeURIComponent(path)}` : "")); }
      catch (e) { toast(e.message, "error", 9000); if (!cur) close(); return; }
      cur = d;
      let check = null;
      try { check = await post("/api/storage/validate", { path: d.path }); } catch (e) { /* shown in list header */ }
      setHTML(dlg, html`<div class="modal-inner">
        <h2 id="br-title">Choose a folder on ${d.hostname}</h2>
        <p class="small muted">You are browsing the Mac that hosts the studio, not this computer. Only folders are shown.</p>
        <form id="br-go" class="br-go"><label class="sr-only" for="br-path">Folder path</label>
          <input id="br-path" value="${d.path}" spellcheck="false"><button class="btn small" type="submit">Go</button></form>
        <div class="br-short" role="group" aria-label="Shortcuts">
          ${d.shortcuts.map((s) => html`<button type="button" class="btn small ghost" data-go="${s.path}">${s.label}</button>`)}
          <button type="button" class="btn small ghost" data-go="/">Macintosh HD (/)</button>
          ${d.volumes.map((v) => html`<button type="button" class="btn small ghost" data-go="${v.path}" title="${fmtBytes(v.free_bytes)} free">⏏ ${v.label}</button>`)}
        </div>
        <div class="br-status ${check && check.ok ? "ok" : "bad"}" role="status">
          <strong>${d.path}</strong> · ${d.writable ? "writable" : "NOT writable"}${d.free_bytes !== null ? html` · ${fmtBytes(d.free_bytes)} free of ${fmtBytes(d.total_bytes)}` : ""}
          ${check ? check.errors.map((e) => html`<div class="err">${e}</div>`) : ""}${check ? check.warnings.map((w) => html`<div class="warn-text">${w}</div>`) : ""}
        </div>
        <ul class="br-list" aria-label="Folders in ${d.path}">
          ${d.parent ? html`<li><button type="button" class="br-item" data-go="${d.parent}">↰ Up to ${d.parent}</button></li>` : ""}
          ${d.entries.map((e) => html`<li><button type="button" class="br-item" data-go="${e.path}">📁 ${e.name}${e.mount ? " (volume)" : ""}${e.writable ? "" : " 🔒"}</button></li>`)}
          ${!d.entries.length ? html`<li class="muted small br-empty">No sub-folders here.</li>` : ""}
        </ul>
        ${d.truncated ? html`<p class="small warn-text">Only the first ${d.entries.length} folders are shown. Type a path above to go deeper.</p>` : ""}
        <form id="br-new" class="br-go"><label class="sr-only" for="br-name">New folder name</label>
          <input id="br-name" placeholder="New folder name" maxlength="80"><button class="btn small" type="submit" ${d.writable ? "" : html`disabled`}>Create folder here</button></form>
        <div class="modal-actions">
          <button type="button" class="btn primary" data-use ${check && check.ok ? "" : html`disabled`}>Use this folder</button>
          <button type="button" class="btn ghost" data-cancel>Cancel</button></div></div>`);
      const inp = dlg.querySelector("#br-path"); if (inp && !dlg.dataset.focused) { dlg.dataset.focused = "1"; }
    }

    dlg.addEventListener("click", async (e) => {
      const g = e.target.closest("[data-go]");
      if (g) { go(g.dataset.go); return; }
      if (e.target.closest("[data-use]")) { result = cur.path; close(); return; }
      if (e.target.closest("[data-cancel]")) { result = null; close(); }
    });
    dlg.addEventListener("submit", async (e) => {
      e.preventDefault();
      if (e.target.id === "br-go") go(dlg.querySelector("#br-path").value.trim());
      else if (e.target.id === "br-new") {
        const name = dlg.querySelector("#br-name").value.trim();
        if (!name) return;
        try { const r = await post("/api/storage/mkdir", { parent: cur.path, name }); toast(`Created ${r.path}`, "success"); go(r.path); }
        catch (err) { toastError(err); }
      }
    });
    dlg.showModal();
    go(startPath || "");
  });
}


// ------------------------------------------------------------------------------------------------ field rendering
/** Label text plus optional small helper text under it. */
const lab = (label, hint) => (hint ? html`<span class="lbl">${label}<span class="hint">${hint}</span></span>` : label);
const SEC = Object.fromEntries(SECTIONS.map(([sec, title, fields]) => [sec, { title, fields: Object.fromEntries(fields.map((f) => [f[0], f])) }]));
const NF = Object.fromEntries(allFields().map((f) => [f[0], f]));

function field(section, [key, label, type, opt = {}], val) {
  const name = `${section}.${key}`;
  const text = lab(label, opt.hint);
  if (type === "bool") return html`<label class="check"><input type="checkbox" name="${name}" ${val ? html`checked` : ""}> ${text}</label>`;
  if (type === "select") return html`<label>${text}<select name="${name}">${opt.options.map((o) => html`<option value="${o}" ${o === val ? html`selected` : ""}>${o}</option>`)}</select></label>`;
  if (type === "list") return html`<label>${text}<input name="${name}" value="${(val || []).join(", ")}" inputmode="decimal"></label>`;
  if (type === "text") return html`<label>${text}<input name="${name}" value="${val ?? ""}" spellcheck="false"></label>`;
  return html`<label>${text}<input type="number" name="${name}" value="${val ?? ""}" min="${opt.min}" max="${opt.max}" step="${opt.step || 1}" ${type === "number" ? html`required` : ""}></label>`;
}
/** Several fields of one SECTIONS group, in the order given. */
const sf = (s, sec, keys) => keys.map((k) => field(sec, SEC[sec].fields[k], s.settings[sec][k]));
/** One narrative field with this page's plain-language label (same control name, type and saved key as before). */
function nf(n, path) {
  const d = NF[path];
  const [label, hint] = NARRATIVE_LABELS[path] || [d[1]];
  return narrativeInput("narrative.", [path, lab(label, hint), d[2], d[3]], getPath(n, path));
}
const nfs = (n, paths) => paths.map((p) => nf(n, p));
const grid = (items, cls = "") => html`<div class="form-grid ${cls}">${items}</div>`;
const card = (id, title, intro, body) => html`<section class="card set-card" aria-labelledby="h-${id}"><h2 id="h-${id}">${title}</h2>
  ${intro ? html`<p class="small muted set-intro">${intro}</p>` : ""}${body}</section>`;
const tabLink = (id, text) => html`<button type="button" class="linklike" data-tab-link="${id}">${text}</button>`;

/** Write-only API key control: shows whether a key is saved, never the key. Buttons are handled in onClick. */
function keyField(slot, info) {
  const on = info && info.configured;
  return html`<div class="key-field" data-slot="${slot}">
    <label>API key (${info ? info.label : slot})
      <input type="password" name="secret.${slot}" autocomplete="new-password" spellcheck="false" maxlength="400"
        placeholder="${on ? "A key is saved. Type a new one to replace it." : "Paste the API key (leave empty if the endpoint needs none)"}"></label>
    <div class="btn-group"><button type="button" class="btn small" data-act="key-save" data-slot="${slot}">Save key</button>
      <button type="button" class="btn small ghost" data-act="key-clear" data-slot="${slot}" ${on ? "" : html`disabled`}>Remove key</button></div>
    <p class="small ${on ? "good-text" : "muted"}" role="status">${on ? html`✓ A key is saved (${fmtDate(info.updated_at)}). It is sent as a Bearer token to the URL above and cannot be displayed again.` : "No key saved."}</p></div>`;
}

function pathStatus(v) {
  if (!v) return "";
  return html`<div class="path-status ${v.ok ? "ok" : "bad"}" role="status">
    ${v.resolved ? html`<div>Resolved: <code>${v.resolved}</code></div>` : ""}
    <div>${v.ok ? "✓ usable" : "✗ not usable"} · ${v.exists ? "exists" : "will be created"} · ${v.writable ? "writable" : "not writable"}
      ${v.free_bytes !== null ? html` · ${fmtBytes(v.free_bytes)} free of ${fmtBytes(v.total_bytes)}` : ""}
      ${v.usage_bytes !== null && v.usage_bytes !== undefined ? html` · ${fmtBytes(v.usage_bytes)} used here${v.usage_partial ? " (partial count)" : ""}` : ""}
      ${v.volume ? html` · volume ${v.volume} ${v.mounted ? "mounted" : "NOT mounted"}${v.removable ? " (removable)" : ""}` : ""}</div>
    ${v.errors.map((e) => html`<div class="err">${e}</div>`)}${v.warnings.map((w) => html`<div class="warn-text">${w}</div>`)}</div>`;
}

// ------------------------------------------------------------------------------------------------ tab panels
function storagePanel(s, storage, checks, localPage) {
  const st = s.settings.storage;
  const catByName = Object.fromEntries(storage.categories.map((c) => [c.category, c]));
  const row = (label, name, placeholder, status) => html`<div class="path-row"><label>${label} <input name="${name}" value="${st[name.split(".")[1]]}" placeholder="${placeholder}" spellcheck="false"></label>
    <div class="btn-group"><button type="button" class="btn small" data-act="browse" data-field="${name}">Browse…</button>
    <button type="button" class="btn small" data-act="validate" data-field="${name}">Check</button>
    ${localPage ? html`<button type="button" class="btn small ghost" data-act="choose" data-field="${name}">Finder…</button>` : ""}</div>
    ${pathStatus(checks[name] || status)}</div>`;
  return html`${card("stor", "Library location", html`A web page cannot hand the server access to arbitrary folders, so locations are typed as paths on this Mac
        (e.g. <code>~/AudiobookLibrary</code> or <code>/Volumes/AudiobooksSSD/Library</code>) and validated by the server.
        <strong>Browse…</strong> opens a folder browser that lists the real folders on <em>this Mac</em> (including external drives under /Volumes) from any computer on the network; you can create a new folder there too.
        ${localPage ? html`On the Mac itself, <strong>Finder…</strong> opens the native Finder picker.` : ""}`, html`
      <p class="callout info small"><strong>How changes apply:</strong> ${storage.policy}</p>
      ${row("Library root", "storage.library_root", s.library_root, storage.library_root)}
      <details id="set-cat-overrides"><summary>Per-category locations (optional overrides)</summary>
        <div class="set-overrides">${Object.entries(s.categories).map(([cat, label]) => row(label, `storage.${cat}_dir`, s.effective_paths[cat], catByName[cat]))}</div>
      </details>`)}
    ${card("reg", "Where existing books live", "", html`
      <ul class="small">${storage.registry.map((r) => html`<li><code>${r.root}</code> – ${r.books.length} book(s) ${r.available ? html`<span class="good-text">available</span>` : html`<span class="err">unavailable (drive disconnected?) – their jobs pause and resume automatically</span>`}</li>`)}</ul>
      <p class="small muted">App &amp; database: <code>${storage.app_home}</code> (database <code>${storage.database}</code> stays on the internal disk so queue state survives drive removal) · Model cache: <code>${storage.model_cache}</code></p>`)}`;
}

function productionPanel(s) {
  return html`${card("chunking", "Segmentation", html`Limits for classic books (semantic segmentation off). Books imported with semantic segmentation use the limits on the ${tabLink("narrative", "Narrative tab")}.`,
      grid(sf(s, "chunking", ["min_chars", "max_chars", "merge_short_paragraphs", "speak_chapter_titles", "normalize_for_speech"])))}
    ${card("pauses", "Pauses & assembly", "How long the silence is between segments and around chapters.",
      grid(sf(s, "pauses", ["sentence_ms", "paragraph_ms", "scene_break_ms", "after_heading_ms", "chapter_head_ms", "chapter_tail_ms", "room_tone_dbfs", "trim_silence"])))}
    ${card("export", "Export", "Format and loudness of the finished MP3 and M4B files.",
      grid(sf(s, "export", ["sample_rate", "mp3_bitrate_kbps", "m4b_bitrate_kbps", "target_lufs", "true_peak_db", "loudness_normalize", "auto_assemble_drafts"])))}
    ${card("worker", "Worker", "The background process that does the synthesis.", grid(sf(s, "worker", ["autostart", "prevent_sleep", "max_consecutive_errors"])))}`;
}

function qualityPanel(s) {
  const n = s.settings.narrative;
  return html`${card("verify", "Whisper verification", html`Every take can be transcribed by a local Whisper model and compared with the text. <strong>Verify takes with Whisper</strong> is the
        master switch: when it is off, nothing is verified and the mode below is ignored. When it is on, <strong>Verify with Whisper</strong> decides how much is checked:
        every segment, a repeatable sample (the share applies only in that mode), or never.`, html`
      ${grid(sf(s, "qa", ["enable_asr"]))}
      <div id="verify-modes" class="form-grid">${nfs(n, ["verify_mode", "verify_sample_rate"])}</div>
      <p id="verify-summary" class="callout info small" role="status"></p>`)}
    ${card("qa", "Pass / fail limits", "A take that breaks one of these is retried, then flagged for review.",
      grid(sf(s, "qa", ["wer_threshold", "max_attempts", "max_dropped_words", "max_inserted_words"])))}
    <details id="set-qa-audio" class="card set-card set-details"><summary>Audio checks (levels, clipping, dead air, pace)</summary>
      <p class="small muted set-intro">Rarely changed. They decide when a take sounds wrong even though the words are right.</p>
      ${grid(sf(s, "qa", ["dead_air_seconds", "clip_ratio_limit", "min_rms_dbfs", "max_rms_dbfs", "duration_ratio_min", "duration_ratio_max", "words_per_minute"]))}</details>`;
}

function modelsPanel(s) {
  const n = s.settings.narrative;
  return html`${card("models", "Models", html`Engine: <strong>${s.engine_mode}</strong>. Model changes apply to books imported afterwards; use “Apply current global settings” on a book to switch it explicitly.`,
      grid(sf(s, "models", ["custom_voice_model", "base_model", "voice_design_model", "asr_model", "language", "directable_clones"])))}
    ${card("sampling", "Sampling", "Blank values use each model's own default.", grid(sf(s, "models", ["temperature", "top_p", "repetition_penalty", "retry_temperatures"])))}
    <details id="set-models-adv" class="card set-card set-details"><summary>Memory and downloads</summary>
      <p class="small muted set-intro">Where the models are cached and how many stay in memory.</p>
      ${grid(sf(s, "models", ["max_warm_models", "cache_dir", "allow_downloads"]))}</details>
    ${card("delivery", "Delivery instructions per model", html`How the Director's emotion and style directions reach each Qwen3-TTS model.
        Base (voice cloning) takes <strong>no instruction input</strong>: it is conditioned only by the reference clip, so emotion reaches a clone through its emotion
        reference clips, or (directable, experimental) through its own speaker vector in the CustomVoice model, which does follow instructions.
        The 0.6B CustomVoice model ignores instructions as well.`,
      grid(nfs(n, ["delivery.custom_voice", "delivery.voice_design", "delivery.max_instruct_chars", "delivery.clone", "delivery.bank_min_intensity", "delivery.bank_min_similarity", "delivery.pace_from_direction", "delivery.pauses_from_direction", "delivery.translator"])))}`;
}

function narrativePanel(s) {
  const n = s.settings.narrative;
  return html`<p class="small muted set-intro">These are the global defaults for books imported from now on. A book can override them on its own
      <em>Narrative</em> page. Segmentation and the Director never change manuscript text: the narration window holds only the words to be spoken, and
      context is given to the Director for analysis only.</p>
    ${card("nseg", "Semantic segmentation", html`Turn it on to split by structure and speaker instead of by length alone. The segment length limits below appear only when it is on.
        Classic books use the limits on the ${tabLink("production", "Production tab")}.`, html`
      ${grid(nfs(n, ["semantic_segmentation", "tags_with_speaker", "emotion_sensitivity"]))}
      <div class="form-grid set-dep" data-needs="narrative.semantic_segmentation">${nfs(n, ["max_segment_chars", "min_segment_chars"])}</div>
      <p class="small muted set-dep" data-needs="narrative.semantic_segmentation">Semantic books ignore the classic limits on the Production tab.</p>`)}
    ${card("nchars", "Speakers & characters", "", grid(nfs(n, ["attribution_threshold", "auto_character_tracking", "chapter_summaries"])))}
    ${card("nctx", "Context windows (analysis only, never spoken)", html`Total surrounding context: <strong id="nar-total">${n.context_before_chars + n.context_after_chars}</strong> characters.`,
      grid(nfs(n, ["context_before_chars", "context_after_chars"])))}
    ${card("ndir", "Narrative Director & continuity", "The Director plans how each segment should be delivered; it needs semantic segmentation.",
      grid(nfs(n, ["director_enabled", "director_lookahead", "director_chapter_plan", "continuity_smoothing", "smoothing_strength", "reset_scope", "cascade_limit"])))}`;
}

function llmPanel(s) {
  const n = s.settings.narrative;
  return html`<p class="small muted set-intro">Without an LLM the Director uses its deterministic rules engine, which is labelled as such wherever it is used.
      The API key is saved on the Mac in a private file (not in the settings file or database) and is write-only: it is never shown again.
      Key changes take effect immediately and do not need <em>Save settings</em>.</p>
    ${card("llm-ep", "Endpoint (OpenAI-compatible)", "",  html`${grid(nfs(n, ["llm.provider", "llm.base_url", "llm.model", "llm.api_key_env"]))}
      ${keyField("llm", s.secrets && s.secrets.llm)}
      <div class="btn-group"><button type="button" class="btn small" data-act="llm-test">Test connection</button></div>
      <div id="llm-test-result" class="small" role="status" aria-live="polite"></div>
      <p class="small muted">Tests the address and model typed above (even before saving), using the saved key. It sends one tiny question and touches no book.</p>`)}
    ${card("llm-models", "Models for other tasks", "Blank means the same as Model above (the translator is off when blank).", grid(nfs(n, ["llm.chapter_model", "llm.scene_model", "llm.translator_model"])))}
    ${card("llm-lim", "Timeouts & limits", "", grid(nfs(n, ["llm.timeout_s", "llm.temperature", "llm.max_tokens", "llm.chapter_max_tokens", "llm.chapter_timeout_s"])))}`;
}

function serverPanel(s) {
  return card("server", "Server", html`These apply after the studio restarts. The studio binds to <code>${s.settings.server.host}</code>. Remote access is deliberately not configurable here: it would expose an unauthenticated service.`,
    grid(sf(s, "server", ["port", "max_manuscript_mb", "max_reference_mb"])));
}

// ------------------------------------------------------------------------------------------------ the page
/** Edits left behind when the page is re-rendered (browser back / forward, a typed URL) so they are not silently lost. */
let draft = null;

export async function render(view, parts, query, nav) {
  const localPage = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  const signal = nav && nav.signal;
  const [settings0, storage0] = await Promise.all([get("/api/settings", { signal }), get("/api/storage", { signal })]);
  let s = settings0, storage = storage0;
  let tab = resolveTab(parts);
  let base = {};          // snapshotForm() of the values as loaded or last saved
  let nameTab = {};       // control name -> tab id
  let dirtyCount = 0;
  let discarding = false; // the user confirmed leaving: do not keep a draft
  const checks = {};

  const PANELS = { storage: () => storagePanel(s, storage, checks, localPage), production: () => productionPanel(s), quality: () => qualityPanel(s),
    models: () => modelsPanel(s), narrative: () => narrativePanel(s), llm: () => llmPanel(s), server: () => serverPanel(s) };

  const paintHTML = () => html`<div class="page-head"><h1>Settings &amp; Storage</h1></div>
    <form id="settings-form" class="form set-form" novalidate>
      <div class="set-layout">
        <div class="set-tabs" role="tablist" aria-label="Settings sections" aria-orientation="vertical">
          ${TABS.map((t) => html`<button type="button" role="tab" class="set-tab" id="tab-${t.id}" data-tab="${t.id}" aria-controls="panel-${t.id}"
            aria-selected="${t.id === tab ? "true" : "false"}" tabindex="${t.id === tab ? "0" : "-1"}"><span class="set-tab-label">${t.label}</span>
            <span class="tab-dot" hidden aria-hidden="true"></span><span class="sr-only tab-dirty" hidden> (unsaved changes)</span>
            <span class="set-tab-blurb">${t.blurb}</span></button>`)}
        </div>
        <div class="set-panels">
          ${TABS.map((t) => html`<section role="tabpanel" class="set-panel" id="panel-${t.id}" data-tab="${t.id}" aria-labelledby="tab-${t.id}" tabindex="0" ${t.id === tab ? "" : html`hidden`}>
            ${PANELS[t.id]()}</section>`)}
        </div>
      </div>
      <div class="sticky-actions set-bar">
        <span class="set-status small" id="set-status" role="status" aria-live="polite">No unsaved changes</span>
        <span class="small muted set-note">Saved settings apply to new imports; each existing book keeps its own snapshot.</span>
        <span class="set-bar-btns"><button class="btn ghost" type="button" id="set-discard" data-act="discard" disabled>Discard changes</button>
          <button class="btn primary" type="submit" id="set-save">Save settings</button></span>
      </div>
    </form>`;

  const form = () => view.querySelector("#settings-form");
  const isDirty = () => dirtyCount > 0;

  /** Show a tab: panels use the hidden attribute (never removed), so edits in other tabs stay in the form. */
  function selectTab(id, opts = {}) {
    if (!TABS.some((t) => t.id === id)) id = DEFAULT_TAB;
    tab = id;
    view.querySelectorAll('[role="tab"]').forEach((b) => { const on = b.dataset.tab === id; b.setAttribute("aria-selected", on ? "true" : "false"); b.tabIndex = on ? 0 : -1; });
    view.querySelectorAll('[role="tabpanel"]').forEach((p) => { p.hidden = p.dataset.tab !== id; });
    // replaceState changes the URL without a hashchange, so the router does not re-render the page and no edit is touched
    if (opts.url !== false) { try { history.replaceState(history.state, "", "#/settings/" + id); } catch { /* sandboxed: the tab still works */ } }
    if (opts.focus) { const b = view.querySelector(`#tab-${id}`); if (b) b.focus(); }
  }

  function syncDeps() {
    const f = form(); if (!f) return;
    f.querySelectorAll("[data-needs]").forEach((el) => { const c = f.elements[el.dataset.needs]; el.hidden = !(c && c.checked); });
  }
  function syncVerify() {
    const f = form(); if (!f) return;
    const v = verifySummary(f.elements["qa.enable_asr"].checked, f.elements["narrative.verify_mode"].value, f.elements["narrative.verify_sample_rate"].value);
    const out = view.querySelector("#verify-summary");
    if (out) { out.textContent = v.text; out.classList.toggle("info", v.on); out.classList.toggle("warn", !v.on); }
    const modes = view.querySelector("#verify-modes");
    if (modes) {
      modes.classList.toggle("is-off", !f.elements["qa.enable_asr"].checked);
      const rate = f.elements["narrative.verify_sample_rate"].closest("label");
      if (rate) rate.classList.toggle("is-inactive", f.elements["narrative.verify_mode"].value !== "sample");
    }
  }
  function syncTotal() {
    const f = form(); const out = view.querySelector("#nar-total");
    if (f && out) out.textContent = String((Number(f.elements["narrative.context_before_chars"].value) || 0) + (Number(f.elements["narrative.context_after_chars"].value) || 0));
  }

  function updateDirty() {
    const f = form(); if (!f) return;
    const names = changedNames(base, snapshotForm(f));
    dirtyCount = names.length;
    const counts = countByGroup(names, (n) => nameTab[n]);
    view.querySelectorAll('[role="tab"]').forEach((b) => {
      const on = !!counts[b.dataset.tab];
      b.querySelector(".tab-dot").hidden = !on;
      b.querySelector(".tab-dirty").hidden = !on;
      b.classList.toggle("is-dirty", on);
    });
    const st = view.querySelector("#set-status");
    st.textContent = dirtyCount ? `${dirtyCount} unsaved change${dirtyCount === 1 ? "" : "s"}` : "No unsaved changes";
    st.classList.toggle("dirty", dirtyCount > 0);
    view.querySelector("#set-discard").disabled = !dirtyCount;
  }

  function paint() {
    patchHTML(view, paintHTML());
    const f = form();
    nameTab = {};
    for (const el of f.elements) { if (!el.name) continue; const p = el.closest('[role="tabpanel"]'); if (p) nameTab[el.name] = p.dataset.tab; }
    base = snapshotForm(f);
    if (draft && (!nav || nav.isCurrent())) { applyForm(f, draft); draft = null; toast("Restored your unsaved settings changes.", "info", 6000); }
    syncDeps(); syncVerify(); syncTotal(); updateDirty();
  }

  /** The first invalid control opens its tab and details before the browser's message is shown. Hidden dependent fields are skipped. */
  function validateAll() {
    const f = form();
    for (const el of f.elements) {
      if (!el.willValidate || el.checkValidity() || el.closest("[data-needs][hidden]")) continue;
      for (let d = el.closest("details"); d; d = d.parentElement && d.parentElement.closest("details")) d.open = true;
      const p = el.closest('[role="tabpanel"]'); if (p) selectTab(p.dataset.tab);
      el.reportValidity();
      return false;
    }
    return true;
  }

  async function save(e) {
    e.preventDefault();
    const f = e.target;
    if (!validateAll()) return;
    try { s = await busy(e.submitter, () => put("/api/settings", collectSettings(f)), "Settings saved"); }
    catch { return; } // the server's validation message is already shown
    storage = await get("/api/storage");
    Object.keys(checks).forEach((k) => delete checks[k]);
    draft = null;
    paint();
  }

  function setField(input, value) { input.value = value; input.dispatchEvent(new Event("input", { bubbles: true })); }

  async function onClick(e) {
    const tb = e.target.closest('[role="tab"]');
    if (tb) { selectTab(tb.dataset.tab); return; }
    const link = e.target.closest("[data-tab-link]");
    if (link) { selectTab(link.dataset.tabLink, { focus: true }); return; }
    const b = e.target.closest("button[data-act]"); if (!b) return;
    if (b.dataset.act === "discard") {
      applyForm(form(), base); syncDeps(); syncVerify(); syncTotal(); updateDirty();
      toast("Unsaved changes discarded.", "info", 3000);
      return;
    }
    if (b.dataset.act === "key-save" || b.dataset.act === "key-clear") {
      const slot = b.dataset.slot, el = view.querySelector(`[name="secret.${slot}"]`);
      try {
        if (b.dataset.act === "key-save") {
          if (!el.value.trim()) { toast("Paste the API key first.", "error"); return; }
          const r = await busy(b, () => put(`/api/secrets/${slot}`, { value: el.value }), "API key saved");
          s.secrets = r.secrets;
        } else {
          const r = await busy(b, () => del(`/api/secrets/${slot}`), "API key removed");
          s.secrets = r.secrets;
        }
      } catch { return; } // the server's message is already shown
      el.value = "";
      const f = el.closest(".key-field"), tmp = document.createElement("div");
      setHTML(tmp, keyField(slot, s.secrets[slot]));
      f.replaceWith(tmp.firstElementChild);
      return;
    }
    if (b.dataset.act === "llm-test") {
      const out = view.querySelector("#llm-test-result");
      setHTML(out, html`Testing...`);
      try {
        const r = await busy(b, () => post("/api/narrative/llm/test", {
          base_url: view.querySelector('[name="narrative.llm.base_url"]').value.trim(),
          model: view.querySelector('[name="narrative.llm.model"]').value.trim(),
          chapter_model: view.querySelector('[name="narrative.llm.chapter_model"]')?.value.trim() || "",
          scene_model: view.querySelector('[name="narrative.llm.scene_model"]')?.value.trim() || "",
          translator_model: view.querySelector('[name="narrative.llm.translator_model"]')?.value.trim() || "" }));
        setHTML(out, html`<ul class="plain">${r.checks.map((c) => html`<li class="${c.ok ? "good-text" : "bad-text"}">${c.ok ? "✓" : "✗"} <strong>${c.name}:</strong> ${c.detail}</li>`)}</ul>
          <p><strong>${r.ok ? "The endpoint and model work." : "The test failed."}</strong> (${r.seconds} s)</p>`);
      } catch { setHTML(out, html``); } // the server's message is already shown
      return;
    }
    const input = view.querySelector(`[name="${b.dataset.field}"]`);
    if (b.dataset.act === "browse") {
      const picked = await browseFolder(input.value.trim() || input.placeholder || "");
      if (picked) { setField(input, picked); toast("Folder selected. Press Save settings to apply it.", "success", 7000); }
    } else if (b.dataset.act === "validate") {
      const path = input.value.trim() || input.placeholder;
      checks[b.dataset.field] = await busy(b, () => post("/api/storage/validate", { path }));
      const row = b.closest(".path-row"); const old = row.querySelector(".path-status");
      const tmp = document.createElement("div"); setHTML(tmp, pathStatus(checks[b.dataset.field]));
      if (old) old.replaceWith(tmp.firstElementChild); else row.appendChild(tmp.firstElementChild);
    } else if (b.dataset.act === "choose") {
      toast("A Finder window opened on this Mac – choose a folder there.", "info", 6000);
      const r = await busy(b, () => post("/api/storage/choose-folder"));
      if (r.path) { setField(input, r.path); toast("Folder selected – press Check, then Save.", "success"); }
    }
  }

  function onInput(e) {
    if (!e.target.name) return;
    syncDeps(); syncVerify(); syncTotal(); updateDirty();
  }

  function onKeydown(e) {
    const tb = e.target.closest && e.target.closest('[role="tab"]'); if (!tb) return;
    const tabs = Array.from(view.querySelectorAll('[role="tab"]'));
    const to = tabKeyTarget(e.key, tabs.indexOf(tb), tabs.length);
    if (to === null) return;
    e.preventDefault();
    selectTab(tabs[to].dataset.tab, { focus: true });
  }

  /** Leaving through a link while edits are unsaved asks first (links inside Settings only switch tabs and are never blocked). */
  async function onLeaveClick(e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest && e.target.closest("a[href^='#/']");
    if (!a || !isDirty()) return;
    const href = a.getAttribute("href");
    if (/^#\/settings(\/|\?|$)/.test(href)) return;
    e.preventDefault(); e.stopPropagation();
    const n = dirtyCount;
    const ok = await confirmDialog("Discard unsaved settings?", `You have ${n} unsaved change${n === 1 ? "" : "s"} on this page. Leaving now discards ${n === 1 ? "it" : "them"}.`, "Discard and leave", true);
    if (ok) { discarding = true; location.hash = href; }
  }
  const onBeforeUnload = (e) => { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } };

  view.addEventListener("click", onClick);
  view.addEventListener("input", onInput);
  view.addEventListener("change", onInput);
  view.addEventListener("keydown", onKeydown);
  view.addEventListener("submit", save);
  document.addEventListener("click", onLeaveClick, true);
  window.addEventListener("beforeunload", onBeforeUnload);
  paint();
  selectTab(tab, { url: false });
  return () => {
    // Keep unsaved edits for a re-render of this page (back / forward); drop them when the user chose to discard.
    draft = isDirty() && !discarding ? snapshotForm(form()) : null;
    view.removeEventListener("click", onClick);
    view.removeEventListener("input", onInput);
    view.removeEventListener("change", onInput);
    view.removeEventListener("keydown", onKeydown);
    view.removeEventListener("submit", save);
    document.removeEventListener("click", onLeaveClick, true);
    window.removeEventListener("beforeunload", onBeforeUnload);
  };
}
