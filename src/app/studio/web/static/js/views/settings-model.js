// Settings page model: field definitions, tab routing, payload collection and dirty tracking. Pure (no DOM at import time), so it is
// covered by tests/js/settings_model.test.mjs. The saved payload shape is the one the server has always accepted.
import { allFields, setPath, readInput } from "./narrative-fields.js";

/** [section, title, fields]; a field is [key, label, type, options]. options.hint is small helper text shown under the label. */
export const SECTIONS = [
  ["chunking", "Segmentation", [
    ["min_chars", "Minimum segment length (chars)", "number", { min: 40, max: 600 }],
    ["max_chars", "Maximum segment length (chars)", "number", { min: 80, max: 1200 }],
    ["merge_short_paragraphs", "Merge short paragraphs", "bool", { hint: "Consecutive short paragraphs are joined for more stable TTS." }],
    ["speak_chapter_titles", "Narrate chapter titles", "bool"],
    ["normalize_for_speech", "Pronounceable renditions", "bool", { hint: "Numbers, currency and abbreviations are spoken naturally." }]]],
  ["qa", "Automatic QA", [
    ["enable_asr", "Verify takes with Whisper", "bool", { hint: "Master switch. When off, no take is checked by speech recognition." }],
    ["wer_threshold", "Word error rate limit (0–1)", "number", { min: 0, max: 1, step: 0.01 }],
    ["max_attempts", "Attempts per segment", "number", { min: 1, max: 10, hint: "Total retry budget before a segment is flagged." }],
    ["max_dropped_words", "Max dropped words in a row", "number", { min: 0, max: 20, hint: "More than this flags the take." }],
    ["max_inserted_words", "Max inserted words in a row", "number", { min: 0, max: 20, hint: "More than this flags the take." }],
    ["dead_air_seconds", "Dead air limit (s)", "number", { min: 0.3, max: 10, step: 0.1, hint: "Longest silence allowed inside a take." }],
    ["clip_ratio_limit", "Clipping limit", "number", { min: 0, max: 0.1, step: 0.0005, hint: "Fraction of samples." }],
    ["min_rms_dbfs", "Minimum speech level (dBFS)", "number", { min: -80, max: -10, step: 0.5 }],
    ["max_rms_dbfs", "Maximum speech level (dBFS)", "number", { min: -30, max: 0, step: 0.5 }],
    ["duration_ratio_min", "Shortest duration (× expected)", "number", { min: 0.1, max: 1, step: 0.05 }],
    ["duration_ratio_max", "Longest duration (× expected)", "number", { min: 1, max: 5, step: 0.05 }],
    ["words_per_minute", "Expected pace (words/min)", "number", { min: 60, max: 300 }]]],
  ["pauses", "Pauses & assembly", [
    ["sentence_ms", "Between segments (ms)", "number", { min: 0, max: 3000, hint: "Within a paragraph." }],
    ["paragraph_ms", "Between paragraphs (ms)", "number", { min: 0, max: 5000 }],
    ["scene_break_ms", "At scene breaks (ms)", "number", { min: 0, max: 8000 }],
    ["after_heading_ms", "After chapter titles (ms)", "number", { min: 0, max: 8000 }],
    ["chapter_head_ms", "Room tone at chapter start (ms)", "number", { min: 0, max: 5000 }],
    ["chapter_tail_ms", "Room tone at chapter end (ms)", "number", { min: 0, max: 10000 }],
    ["room_tone_dbfs", "Room tone level (dBFS)", "number", { min: -120, max: -50 }],
    ["trim_silence", "Trim silence around takes", "bool", { hint: "Leading and trailing silence of each take." }]]],
  ["models", "Models & synthesis", [
    ["custom_voice_model", "CustomVoice model", "text", { hint: "Preset speakers." }],
    ["base_model", "Base model", "text", { hint: "Voice cloning." }],
    ["voice_design_model", "VoiceDesign model", "text"],
    ["asr_model", "Whisper ASR model", "text"],
    ["language", "Narration language", "text"],
    ["temperature", "Sampling temperature", "number?", { min: 0.05, max: 1.5, step: 0.05, hint: "Blank = model default." }],
    ["top_p", "Top-p", "number?", { min: 0.05, max: 1, step: 0.05, hint: "Blank = model default." }],
    ["repetition_penalty", "Repetition penalty", "number?", { min: 1, max: 2, step: 0.01, hint: "Blank = model default." }],
    ["retry_temperatures", "Retry temperatures", "list", { hint: "Comma separated, one per retry." }],
    ["max_warm_models", "Models kept loaded in memory", "number", { min: 1, max: 4 }],
    ["cache_dir", "Model cache folder", "text", { hint: "Blank = default. May be an external disk. Applies after a restart." }],
    ["allow_downloads", "Allow downloading model weights", "bool", { hint: "From Hugging Face." }]]],
  ["export", "Export", [
    ["sample_rate", "Master sample rate (Hz)", "select", { options: [22050, 44100, 48000] }],
    ["mp3_bitrate_kbps", "MP3 bitrate (kbps)", "select", { options: [128, 160, 192, 256, 320], hint: "Constant bitrate." }],
    ["m4b_bitrate_kbps", "M4B AAC bitrate (kbps)", "select", { options: [64, 96, 128, 160] }],
    ["loudness_normalize", "Loudness-normalise chapters", "bool", { hint: "Two-pass EBU R128 on the chapter masters." }],
    ["target_lufs", "Target loudness (LUFS)", "number", { min: -30, max: -12, step: 0.5 }],
    ["true_peak_db", "True-peak ceiling (dBTP)", "number", { min: -9, max: -0.5, step: 0.5 }],
    ["auto_assemble_drafts", "Assemble drafts automatically", "bool", { hint: "Draft chapter masters are built after generation." }]]],
  ["worker", "Worker", [
    ["autostart", "Start the GPU worker with the web server", "bool"],
    ["prevent_sleep", "Prevent idle sleep while jobs run", "bool", { hint: "Uses caffeinate." }],
    ["max_consecutive_errors", "Stop a book after N failures in a row", "number", { min: 1, max: 50, hint: "Consecutive synthesis failures." }]]],
  ["server", "Server (applies after restart)", [
    ["port", "Port", "number", { min: 1024, max: 65535 }],
    ["max_manuscript_mb", "Max manuscript upload (MB)", "number", { min: 1, max: 2000 }],
    ["max_reference_mb", "Max reference clip upload (MB)", "number", { min: 1, max: 200 }]]],
];

/** Plain-language labels for the narrative fields on this page: path -> [label, hint]. The long originals in narrative-fields.js
 *  stay for the per-book editor; every field is still the same field (same name, same type, same saved key). */
export const NARRATIVE_LABELS = {
  semantic_segmentation: ["Semantic segmentation", "Two passes: structure and speakers first, then segments."],
  max_segment_chars: ["Maximum segment length (chars)", "A ceiling, not a target."],
  min_segment_chars: ["Avoid fragments shorter than (chars)"],
  tags_with_speaker: ["Character voice reads short tags", "Tags such as “she said.” stay with the speaker (fewer tiny segments)."],
  emotion_sensitivity: ["Emotion sensitivity (0–1)", "Also controls splitting at emotional turns."],
  attribution_threshold: ["Attribution confidence threshold (0–1)", "Below it a line is flagged for review."],
  auto_character_tracking: ["Discover characters automatically"],
  chapter_summaries: ["Chapter and scene summaries", "Verbatim sentences; never invented."],
  context_before_chars: ["Previous context (chars)"],
  context_after_chars: ["Following context (chars)"],
  director_enabled: ["Enable the Narrative Director", "Requires semantic segmentation."],
  director_lookahead: ["Directions prepared ahead", "Segments per batch."],
  director_chapter_plan: ["Chapter beat sheet", "Prepared before directing a chapter."],
  continuity_smoothing: ["Continuity smoothing", "Across neighbouring segments."],
  smoothing_strength: ["Smoothing strength (0–1)", "0 = off, 1 = strong."],
  reset_scope: ["Reset delivery state at"],
  cascade_limit: ["Recompute limit after an edit", "Most segments recomputed after one manual edit."],
  verify_mode: ["Verify with Whisper", "How much to check, once the master switch above is on."],
  verify_sample_rate: ["Sample share (0–1)", "Only used in “a sample” mode."],
  "delivery.custom_voice": ["CustomVoice instructions", "Emotion and style instruction for preset speakers."],
  "delivery.voice_design": ["VoiceDesign instructions", "The instruction is the voice description itself."],
  "delivery.max_instruct_chars": ["Longest instruction (chars)", "Sent to a model."],
  "delivery.clone": ["Cloned voices", "Emotion through reference clips."],
  "delivery.pace_from_direction": ["Pace from direction"],
  "delivery.pauses_from_direction": ["Pauses from direction"],
  "delivery.translator": ["Instruction translator", "Turns a direction into a model instruction."],
  "delivery.bank_min_intensity": ["Reference clip from intensity (1–5)", "Weakest emotion intensity that uses a reference clip."],
  "llm.provider": ["Provider"],
  "llm.base_url": ["Base URL", "For example http://127.0.0.1:1234/v1"],
  "llm.model": ["Model name"],
  "llm.chapter_model": ["Chapter pass model", "Thinking on. Blank = same as Model."],
  "llm.scene_model": ["Scene pass model", "Blank = same as Model."],
  "llm.translator_model": ["Translator model", "Blank = off."],
  "llm.api_key_env": ["Fallback key variable", "Environment variable (STUDIO_LLM_…) holding the API key, used only when no key is saved below."],
  "llm.timeout_s": ["Request timeout (s)"],
  "llm.temperature": ["Temperature"],
  "llm.max_tokens": ["Maximum reply tokens"],
  "llm.chapter_max_tokens": ["Chapter pass: maximum reply tokens", "Includes thinking."],
  "llm.chapter_timeout_s": ["Chapter pass: request timeout (s)"],
};

/** Tabs in display order. The tab id is the URL part: #/settings/<id>. */
export const TABS = [
  { id: "storage", label: "Storage", blurb: "Where the library, audio and backups live" },
  { id: "production", label: "Production", blurb: "Segmenting, pauses, export, worker" },
  { id: "quality", label: "Quality & verification", blurb: "Automatic QA and Whisper checks" },
  { id: "models", label: "Voices & models", blurb: "Models, sampling, delivery per model" },
  { id: "narrative", label: "Narrative", blurb: "Semantic segmentation and the Director" },
  { id: "llm", label: "Local LLM", blurb: "Endpoint, models, API key" },
  { id: "server", label: "Server & advanced", blurb: "Port, upload limits" },
];
export const DEFAULT_TAB = TABS[0].id;

/** The tab named by the route parts after "settings" (["models"] -> "models"); anything unknown falls back to the first tab. */
export function resolveTab(parts) {
  const id = Array.isArray(parts) && parts.length ? String(parts[0]) : "";
  return TABS.some((t) => t.id === id) ? id : DEFAULT_TAB;
}

/** Keyboard navigation for a tab list: the index to move to for `key`, or null when the key is not a navigation key. */
export function tabKeyTarget(key, index, count) {
  if (count <= 0) return null;
  if (key === "ArrowRight" || key === "ArrowDown") return (index + 1) % count;
  if (key === "ArrowLeft" || key === "ArrowUp") return (index - 1 + count) % count;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return null;
}

/** Names of controls whose value differs between two snapshotForm() results (sorted). Write-only secret.* controls are never "changes". */
export function changedNames(base, cur) {
  const out = [];
  const names = new Set([...Object.keys(base || {}), ...Object.keys(cur || {})]);
  for (const name of names) {
    if (name.startsWith("secret.")) continue;
    if (JSON.stringify((base || {})[name]) !== JSON.stringify((cur || {})[name])) out.push(name);
  }
  return out.sort();
}

/** Count changed names per group: `groupOf(name)` returns the group (tab id) of a control name, or falsy to skip it. */
export function countByGroup(names, groupOf) {
  const out = {};
  for (const n of names) { const g = groupOf(n); if (g) out[g] = (out[g] || 0) + 1; }
  return out;
}

/** One sentence saying what the two Whisper settings add up to (qa.enable_asr is the master switch; verify_mode only matters when it is on). */
export function verifySummary(enableAsr, mode, rate) {
  if (!enableAsr) return { on: false, text: "Whisper verification is off: no take is checked, whatever the mode below says." };
  if (mode === "off") return { on: false, text: "Whisper verification is on, but the mode is “never”, so no take is checked." };
  if (mode === "sample") {
    const pct = Math.round(Math.max(0, Math.min(1, Number(rate) || 0)) * 100);
    return { on: true, text: `About ${pct}% of segments are checked by Whisper (a sample).` };
  }
  return { on: true, text: "Every segment is checked by Whisper." };
}

export function collectNarrative(form) {
  const out = {};
  for (const f of allFields()) {
    const v = readInput(form, "narrative.", f);
    if (v !== undefined) setPath(out, f[0], v);
  }
  return out;
}

/** The body of PUT /api/settings, read from the form's controls. Unchanged from v0.8.0: hiding a tab or a dependent field never removes a key. */
export function collectSettings(form) {
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
