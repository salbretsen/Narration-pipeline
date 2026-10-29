// Settings & Storage: server-validated paths, disk status, every tunable with validation.
import { get, post, put, del, html, setHTML, fmtBytes, fmtDate, toast, toastError, busy } from "../ui.js";
import { NARRATIVE_FIELDS, allFields, getPath, setPath, narrativeInput, readInput } from "./narrative-fields.js";

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


const SECTIONS = [
  ["chunking", "Segmentation", [
    ["min_chars", "Minimum segment length (characters)", "number", { min: 40, max: 600 }],
    ["max_chars", "Maximum segment length (characters)", "number", { min: 80, max: 1200 }],
    ["merge_short_paragraphs", "Merge consecutive short paragraphs (more stable TTS)", "bool"],
    ["speak_chapter_titles", "Narrate chapter titles", "bool"],
    ["normalize_for_speech", "Create pronounceable renditions (numbers, currency, abbreviations)", "bool"]]],
  ["qa", "Automatic QA", [
    ["enable_asr", "Verify every take with local Whisper ASR", "bool"],
    ["wer_threshold", "Word error rate threshold (0–1)", "number", { min: 0, max: 1, step: 0.01 }],
    ["max_attempts", "Total attempts per segment (retry budget)", "number", { min: 1, max: 10 }],
    ["max_dropped_words", "Max consecutive dropped words before flagging", "number", { min: 0, max: 20 }],
    ["max_inserted_words", "Max consecutive inserted words before flagging", "number", { min: 0, max: 20 }],
    ["dead_air_seconds", "Dead air limit inside a take (s)", "number", { min: 0.3, max: 10, step: 0.1 }],
    ["clip_ratio_limit", "Clipping limit (fraction of samples)", "number", { min: 0, max: 0.1, step: 0.0005 }],
    ["min_rms_dbfs", "Minimum speech level (dBFS)", "number", { min: -80, max: -10, step: 0.5 }],
    ["max_rms_dbfs", "Maximum speech level (dBFS)", "number", { min: -30, max: 0, step: 0.5 }],
    ["duration_ratio_min", "Shortest plausible duration (× expected)", "number", { min: 0.1, max: 1, step: 0.05 }],
    ["duration_ratio_max", "Longest plausible duration (× expected)", "number", { min: 1, max: 5, step: 0.05 }],
    ["words_per_minute", "Expected narration pace (words/min)", "number", { min: 60, max: 300 }]]],
  ["pauses", "Pauses & assembly", [
    ["sentence_ms", "Pause between segments within a paragraph (ms)", "number", { min: 0, max: 3000 }],
    ["paragraph_ms", "Pause between paragraphs (ms)", "number", { min: 0, max: 5000 }],
    ["scene_break_ms", "Pause at scene breaks (ms)", "number", { min: 0, max: 8000 }],
    ["after_heading_ms", "Pause after chapter titles/headings (ms)", "number", { min: 0, max: 8000 }],
    ["chapter_head_ms", "Room tone at chapter start (ms)", "number", { min: 0, max: 5000 }],
    ["chapter_tail_ms", "Room tone at chapter end (ms)", "number", { min: 0, max: 10000 }],
    ["room_tone_dbfs", "Room tone level (dBFS)", "number", { min: -120, max: -50 }],
    ["trim_silence", "Trim leading/trailing silence of each take", "bool"]]],
  ["models", "Models & synthesis", [
    ["custom_voice_model", "CustomVoice model (preset speakers)", "text"],
    ["base_model", "Base model (voice cloning)", "text"],
    ["voice_design_model", "VoiceDesign model", "text"],
    ["asr_model", "Whisper ASR model", "text"],
    ["language", "Narration language", "text"],
    ["temperature", "Sampling temperature (blank = model default)", "number?", { min: 0.05, max: 1.5, step: 0.05 }],
    ["top_p", "Top-p (blank = model default)", "number?", { min: 0.05, max: 1, step: 0.05 }],
    ["repetition_penalty", "Repetition penalty (blank = model default)", "number?", { min: 1, max: 2, step: 0.01 }],
    ["retry_temperatures", "Temperatures for retries (comma separated)", "list"],
    ["max_warm_models", "Models kept loaded in memory", "number", { min: 1, max: 4 }],
    ["allow_downloads", "Allow downloading model weights from Hugging Face", "bool"]]],
  ["export", "Export", [
    ["sample_rate", "Master sample rate (Hz)", "select", { options: [22050, 44100, 48000] }],
    ["mp3_bitrate_kbps", "MP3 bitrate (kbps, CBR)", "select", { options: [128, 160, 192, 256, 320] }],
    ["m4b_bitrate_kbps", "M4B AAC bitrate (kbps)", "select", { options: [64, 96, 128, 160] }],
    ["loudness_normalize", "Loudness-normalise chapter masters (two-pass EBU R128)", "bool"],
    ["target_lufs", "Target integrated loudness (LUFS)", "number", { min: -30, max: -12, step: 0.5 }],
    ["true_peak_db", "True-peak ceiling (dBTP)", "number", { min: -9, max: -0.5, step: 0.5 }],
    ["auto_assemble_drafts", "Assemble draft chapter masters automatically after generation", "bool"]]],
  ["worker", "Worker", [
    ["autostart", "Start the GPU worker with the web server", "bool"],
    ["prevent_sleep", "Prevent idle sleep while jobs run (caffeinate)", "bool"],
    ["max_consecutive_errors", "Stop a book after N consecutive synthesis failures", "number", { min: 1, max: 50 }]]],
  ["server", "Server (applies after restart)", [
    ["port", "Port", "number", { min: 1024, max: 65535 }],
    ["max_manuscript_mb", "Max manuscript upload (MB)", "number", { min: 1, max: 2000 }],
    ["max_reference_mb", "Max reference clip upload (MB)", "number", { min: 1, max: 200 }]]],
];

function field(section, [key, label, type, opt = {}], val) {
  const name = `${section}.${key}`;
  if (type === "bool") return html`<label class="check"><input type="checkbox" name="${name}" ${val ? html`checked` : ""}> ${label}</label>`;
  if (type === "select") return html`<label>${label}<select name="${name}">${opt.options.map((o) => html`<option value="${o}" ${o === val ? html`selected` : ""}>${o}</option>`)}</select></label>`;
  if (type === "list") return html`<label>${label}<input name="${name}" value="${(val || []).join(", ")}" inputmode="decimal"></label>`;
  if (type === "text") return html`<label>${label}<input name="${name}" value="${val ?? ""}" spellcheck="false"></label>`;
  return html`<label>${label}<input type="number" name="${name}" value="${val ?? ""}" min="${opt.min}" max="${opt.max}" step="${opt.step || 1}" ${type === "number" ? html`required` : ""}></label>`;
}

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

function narrativeCard(n, secrets) {
  return html`<section class="card" aria-labelledby="sec-narrative"><h2 id="sec-narrative">Narrative processing (semantic segmentation &amp; Narrative Director)</h2>
    <p class="small muted">These are the global defaults for books imported from now on. A book can override them on its own
      <em>Narrative</em> page. Segmentation and the Director never change manuscript text: the narration window holds only the words to be spoken, and
      context is given to the Director for analysis only.</p>
    ${NARRATIVE_FIELDS.map(([title, fields]) => html`<h3>${title}</h3>
      ${title.startsWith("Context") ? html`<p class="small muted">Total surrounding context: <strong id="nar-total">${n.context_before_chars + n.context_after_chars}</strong> characters.</p>` : ""}
      ${title.startsWith("Delivery") ? html`<p class="small muted">Base (voice cloning): <strong>no instruction input — not configurable.</strong> The Base model is conditioned only by the
        reference clip, so emotion direction is recorded but not applied. The 0.6B CustomVoice model ignores instructions as well.</p>` : ""}
      ${title.startsWith("Local LLM") ? html`<p class="small muted">Without an LLM the Director uses its deterministic rules engine, which is labelled as such wherever it is used.
        The API key is saved on the Mac in a private file (not in the settings file or database) and is write-only: it is never shown again.
        Key changes take effect immediately and do not need <em>Save settings</em>.</p>` : ""}
      <div class="form-grid">${fields.map((f) => narrativeInput("narrative.", f, getPath(n, f[0])))}</div>
      ${title.startsWith("Local LLM") ? keyField("llm", secrets && secrets.llm) : ""}`)}
  </section>`;
}

function collectNarrative(form) {
  const out = {};
  for (const f of allFields()) {
    const v = readInput(form, "narrative.", f);
    if (v !== undefined) setPath(out, f[0], v);
  }
  return out;
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

export async function render(view) {
  const localPage = ["localhost", "127.0.0.1", "[::1]"].includes(location.hostname);
  let s = await get("/api/settings");
  let storage = await get("/api/storage");
  const checks = {};

  const paint = () => {
    const st = s.settings.storage;
    const catByName = Object.fromEntries(storage.categories.map((c) => [c.category, c]));
    setHTML(view, html`<div class="page-head"><h1>Settings &amp; Storage</h1></div>
    <form id="settings-form" class="form" novalidate>
    <section class="card" aria-labelledby="stor-h"><h2 id="stor-h">Storage locations</h2>
      <p class="small">A web page cannot hand the server access to arbitrary folders, so locations are typed as paths on this Mac
        (e.g. <code>~/AudiobookLibrary</code> or <code>/Volumes/AudiobooksSSD/Library</code>) and validated by the server.
        <strong>Browse…</strong> opens a folder browser that lists the real folders on <em>this Mac</em> (including external drives under /Volumes) from any computer on the network; you can create a new folder there too.
        ${localPage ? html`On the Mac itself, <strong>Finder…</strong> opens the native Finder picker.` : ""}</p>
      <p class="callout info small"><strong>How changes apply:</strong> ${storage.policy}</p>
      <div class="path-row"><label>Library root <input name="storage.library_root" value="${st.library_root}" placeholder="${s.library_root}" spellcheck="false"></label>
        <div class="btn-group"><button type="button" class="btn small" data-act="browse" data-field="storage.library_root">Browse…</button>
        <button type="button" class="btn small" data-act="validate" data-field="storage.library_root">Check</button>
        ${localPage ? html`<button type="button" class="btn small ghost" data-act="choose" data-field="storage.library_root">Finder…</button>` : ""}</div>
        ${pathStatus(checks["storage.library_root"] || storage.library_root)}</div>
      <details><summary>Per-category locations (optional overrides)</summary>
      ${Object.entries(s.categories).map(([cat, label]) => html`<div class="path-row"><label>${label} <input name="storage.${cat}_dir" value="${st[cat + "_dir"]}" placeholder="${s.effective_paths[cat]}" spellcheck="false"></label>
        <div class="btn-group"><button type="button" class="btn small" data-act="browse" data-field="storage.${cat}_dir">Browse…</button>
        <button type="button" class="btn small" data-act="validate" data-field="storage.${cat}_dir">Check</button>
        ${localPage ? html`<button type="button" class="btn small ghost" data-act="choose" data-field="storage.${cat}_dir">Finder…</button>` : ""}</div>
        ${pathStatus(checks[`storage.${cat}_dir`] || catByName[cat])}</div>`)}
      </details>
      <h3>Where existing books live</h3>
      <ul class="small">${storage.registry.map((r) => html`<li><code>${r.root}</code> – ${r.books.length} book(s) ${r.available ? html`<span class="good-text">available</span>` : html`<span class="err">unavailable (drive disconnected?) – their jobs pause and resume automatically</span>`}</li>`)}</ul>
      <p class="small muted">App &amp; database: <code>${storage.app_home}</code> (database <code>${storage.database}</code> stays on the internal disk so queue state survives drive removal) · Model cache: <code>${storage.model_cache}</code></p>
    </section>
    ${SECTIONS.map(([sec, title, fields]) => html`<section class="card" aria-labelledby="sec-${sec}"><h2 id="sec-${sec}">${title}</h2>
      ${sec === "models" ? html`<p class="small muted">Engine: <strong>${s.engine_mode}</strong>. Model changes apply to books imported afterwards; use “Apply current global settings” on a book to switch it explicitly.</p>` : ""}
      ${sec === "server" ? html`<p class="small muted">The studio binds to <code>${s.settings.server.host}</code>. Remote access is deliberately not configurable here: it would expose an unauthenticated service.</p>` : ""}
      <div class="form-grid">${fields.map((f) => field(sec, f, s.settings[sec][f[0]]))}</div></section>`)}
    ${narrativeCard(s.settings.narrative, s.secrets)}
    <div class="sticky-actions"><button class="btn primary" type="submit">Save settings</button>
      <span class="small muted">Saved settings apply to new imports; each existing book keeps its own snapshot.</span></div>
    </form>`);
    view.querySelector("#settings-form").addEventListener("submit", save);
    const total = () => {
      const f = view.querySelector("#settings-form");
      const out = view.querySelector("#nar-total");
      if (out) out.textContent = String((Number(f.elements["narrative.context_before_chars"].value) || 0) + (Number(f.elements["narrative.context_after_chars"].value) || 0));
    };
    ["context_before_chars", "context_after_chars"].forEach((k) => view.querySelector(`[name="narrative.${k}"]`).addEventListener("input", total));
  };

  function collect(form) {
    const out = {};
    const set = (path, v) => { const [a, b] = path.split("."); (out[a] = out[a] || {})[b] = v; };
    for (const [sec, , fields] of SECTIONS) for (const [key, , type] of fields) {
      const el = form.elements[`${sec}.${key}`]; if (!el) continue;
      if (type === "bool") set(`${sec}.${key}`, el.checked);
      else if (type === "number") set(`${sec}.${key}`, el.value === "" ? null : Number(el.value));
      else if (type === "number?") set(`${sec}.${key}`, el.value === "" ? null : Number(el.value));
      else if (type === "select") set(`${sec}.${key}`, Number(el.value));
      else if (type === "list") set(`${sec}.${key}`, el.value.split(",").map((x) => x.trim()).filter(Boolean).map(Number));
      else set(`${sec}.${key}`, el.value.trim());
    }
    out.narrative = collectNarrative(form);
    out.storage = {};
    form.querySelectorAll("[name^='storage.']").forEach((el) => { out.storage[el.name.split(".")[1]] = el.value.trim(); });
    return out;
  }

  async function save(e) {
    e.preventDefault();
    const form = e.target;
    if (!form.reportValidity()) return;
    try { s = await busy(e.submitter, () => put("/api/settings", collect(form)), "Settings saved"); }
    catch { return; } // the server's validation message is already shown
    storage = await get("/api/storage");
    Object.keys(checks).forEach((k) => delete checks[k]);
    paint();
  }

  async function onClick(e) {
    const b = e.target.closest("button[data-act]"); if (!b) return;
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
    const input = view.querySelector(`[name="${b.dataset.field}"]`);
    if (b.dataset.act === "browse") {
      const picked = await browseFolder(input.value.trim() || input.placeholder || "");
      if (picked) { input.value = picked; toast("Folder selected. Press Save settings to apply it.", "success", 7000); }
    } else if (b.dataset.act === "validate") {
      const path = input.value.trim() || input.placeholder;
      checks[b.dataset.field] = await busy(b, () => post("/api/storage/validate", { path }));
      const row = b.closest(".path-row"); const old = row.querySelector(".path-status");
      const tmp = document.createElement("div"); setHTML(tmp, pathStatus(checks[b.dataset.field]));
      if (old) old.replaceWith(tmp.firstElementChild); else row.appendChild(tmp.firstElementChild);
    } else if (b.dataset.act === "choose") {
      toast("A Finder window opened on this Mac – choose a folder there.", "info", 6000);
      const r = await busy(b, () => post("/api/storage/choose-folder"));
      if (r.path) { input.value = r.path; toast("Folder selected – press Check, then Save.", "success"); }
    }
  }
  view.addEventListener("click", onClick);
  paint();
  return () => view.removeEventListener("click", onClick);
}
