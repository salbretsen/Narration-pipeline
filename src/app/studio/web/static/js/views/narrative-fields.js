// Shared description of the narrative settings, used by the global Settings page and the per-book overrides editor.
import { html } from "../ui.js";

// [path, label, type, options]  (path may be nested: delivery.x / llm.x)
export const NARRATIVE_FIELDS = [
  ["Segmentation", [
    ["semantic_segmentation", "Intelligent semantic segmentation (two-pass: structure and speakers, then segments)", "bool"],
    ["max_segment_chars", "Maximum narration segment length (a ceiling, not a target)", "number", { min: 80, max: 1200 }],
    ["min_segment_chars", "Avoid fragments shorter than (characters)", "number", { min: 0, max: 600 }],
    ["tags_with_speaker", "Let a character voice read short tags such as “she said.” (fewer tiny segments)", "bool"],
    ["emotion_sensitivity", "Emotion detection sensitivity (0–1; also controls splitting at emotional turns)", "number", { min: 0, max: 1, step: 0.05 }]]],
  ["Speakers & characters", [
    ["attribution_threshold", "Speaker attribution confidence threshold (below it a line is flagged for review)", "number", { min: 0, max: 1, step: 0.05 }],
    ["auto_character_tracking", "Discover characters automatically", "bool"],
    ["chapter_summaries", "Build chapter and scene summaries (verbatim sentences; never invented)", "bool"]]],
  ["Context windows (analysis only — never spoken)", [
    ["context_before_chars", "Previous context (characters)", "number", { min: 0, max: 8000 }],
    ["context_after_chars", "Following context (characters)", "number", { min: 0, max: 8000 }]]],
  ["Narrative Director & continuity", [
    ["director_enabled", "Enable the Narrative Director (requires semantic segmentation)", "bool"],
    ["director_lookahead", "Directions prepared ahead of synthesis (segments per batch)", "number", { min: 1, max: 200 }],
    ["continuity_smoothing", "Cross-segment continuity smoothing", "bool"],
    ["smoothing_strength", "Smoothing strength (0 = off … 1 = strong)", "number", { min: 0, max: 1, step: 0.05 }],
    ["reset_scope", "Reset delivery state at", "select", { options: [["none", "never"], ["scene", "every scene break"], ["chapter", "every chapter start (fade at scene breaks)"]] }],
    ["cascade_limit", "Most segments recomputed after one manual edit", "number", { min: 1, max: 500 }]]],
  ["Whisper verification", [
    ["verify_mode", "Verify generated audio with Whisper", "select", { options: [["every", "every segment"], ["sample", "a sample"], ["off", "never"]] }],
    ["verify_sample_rate", "Share of segments verified in sample mode (0–1)", "number", { min: 0, max: 1, step: 0.05 }]]],
  ["Delivery instructions per Qwen3-TTS model", [
    ["delivery.custom_voice", "CustomVoice (preset speakers) — emotion/style instruction", "select", { options: [["instruct", "send as instruct"], ["off", "off"]] }],
    ["delivery.voice_design", "VoiceDesign — instruction is the voice description itself", "select", { options: [["append", "append delivery to the description"], ["off", "off"]] }],
    ["delivery.max_instruct_chars", "Longest instruction sent to a model (characters)", "number", { min: 40, max: 400 }]]],
  ["Local LLM (OpenAI-compatible endpoint)", [
    ["llm.provider", "Provider", "select", { options: [["none", "none (rules engine only)"], ["openai_compatible", "OpenAI-compatible server"]] }],
    ["llm.base_url", "Base URL (e.g. http://127.0.0.1:1234/v1)", "text"],
    ["llm.model", "Model name", "text"],
    ["llm.api_key_env", "Environment variable that holds the API key (leave blank if none)", "text"],
    ["llm.timeout_s", "Request timeout (s)", "number", { min: 5, max: 1800 }],
    ["llm.temperature", "Temperature", "number", { min: 0, max: 1.5, step: 0.05 }],
    ["llm.max_tokens", "Maximum reply tokens", "number", { min: 100, max: 8000 }]]],
];

export const getPath = (obj, path) => path.split(".").reduce((o, k) => (o === undefined || o === null ? undefined : o[k]), obj);

export function setPath(obj, path, value) {
  const keys = path.split(".");
  let o = obj;
  keys.slice(0, -1).forEach((k) => { o = o[k] = o[k] || {}; });
  o[keys[keys.length - 1]] = value;
}

/** One input for a field; `prefix` namespaces the form control names. */
export function narrativeInput(prefix, [path, label, type, opt = {}], val) {
  const name = `${prefix}${path}`;
  if (type === "bool") return html`<label class="check"><input type="checkbox" name="${name}" ${val ? html`checked` : ""}> ${label}</label>`;
  if (type === "select") return html`<label>${label}<select name="${name}">${opt.options.map(([v, l]) => html`<option value="${v}" ${v === val ? html`selected` : ""}>${l}</option>`)}</select></label>`;
  if (type === "text") return html`<label>${label}<input name="${name}" value="${val ?? ""}" spellcheck="false" autocomplete="off"></label>`;
  return html`<label>${label}<input type="number" name="${name}" value="${val ?? ""}" min="${opt.min}" max="${opt.max}" step="${opt.step || 1}"></label>`;
}

/** Read a control's value back with the right type; undefined when blank. */
export function readInput(form, prefix, [path, , type]) {
  const el = form.elements[`${prefix}${path}`];
  if (!el) return undefined;
  if (type === "bool") return el.checked;
  if (el.value === "") return type === "text" ? "" : undefined;
  if (type === "number") return Number(el.value);
  return el.value.trim();
}

export const allFields = () => NARRATIVE_FIELDS.flatMap(([, fields]) => fields);
