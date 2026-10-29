// Settings & Storage: server-validated paths, disk status, every tunable with validation.
import { get, post, put, html, setHTML, fmtBytes, toast, busy } from "../ui.js";

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
        ${storage.native_chooser ? html`On macOS you can also use <strong>Choose…</strong>, which opens the real Finder folder picker on this Mac.` : ""}</p>
      <p class="callout info small"><strong>How changes apply:</strong> ${storage.policy}</p>
      <div class="path-row"><label>Library root <input name="storage.library_root" value="${st.library_root}" placeholder="${s.library_root}" spellcheck="false"></label>
        <div class="btn-group"><button type="button" class="btn small" data-act="validate" data-field="storage.library_root">Check</button>
        ${storage.native_chooser ? html`<button type="button" class="btn small ghost" data-act="choose" data-field="storage.library_root">Choose…</button>` : ""}</div>
        ${pathStatus(checks["storage.library_root"] || storage.library_root)}</div>
      <details><summary>Per-category locations (optional overrides)</summary>
      ${Object.entries(s.categories).map(([cat, label]) => html`<div class="path-row"><label>${label} <input name="storage.${cat}_dir" value="${st[cat + "_dir"]}" placeholder="${s.effective_paths[cat]}" spellcheck="false"></label>
        <div class="btn-group"><button type="button" class="btn small" data-act="validate" data-field="storage.${cat}_dir">Check</button>
        ${storage.native_chooser ? html`<button type="button" class="btn small ghost" data-act="choose" data-field="storage.${cat}_dir">Choose…</button>` : ""}</div>
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
    <div class="sticky-actions"><button class="btn primary" type="submit">Save settings</button>
      <span class="small muted">Saved settings apply to new imports; each existing book keeps its own snapshot.</span></div>
    </form>`);
    view.querySelector("#settings-form").addEventListener("submit", save);
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
    out.storage = {};
    form.querySelectorAll("[name^='storage.']").forEach((el) => { out.storage[el.name.split(".")[1]] = el.value.trim(); });
    return out;
  }

  async function save(e) {
    e.preventDefault();
    const form = e.target;
    if (!form.reportValidity()) return;
    s = await busy(e.submitter, () => put("/api/settings", collect(form)), "Settings saved");
    storage = await get("/api/storage");
    Object.keys(checks).forEach((k) => delete checks[k]);
    paint();
  }

  async function onClick(e) {
    const b = e.target.closest("button[data-act]"); if (!b) return;
    const input = view.querySelector(`[name="${b.dataset.field}"]`);
    if (b.dataset.act === "validate") {
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
