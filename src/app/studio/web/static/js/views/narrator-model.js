// Pure helpers for Narrator Studio (tiles and the narrator page). No DOM, no network: unit-tested in tests/js/narrator_model.test.mjs.

export const KIND = { preset: "Preset speaker", design: "Designed voice", clone: "Cloned voice" };
export const KIND_LONG = { preset: "Preset speaker (CustomVoice)", design: "Designed voice (VoiceDesign)", clone: "Cloned voice (Base + reference)" };

export const SOURCES = {
  clone: { label: "Read by the narrator", hint: "The narrator itself reads an emotional script: it keeps its own voice; the emotion comes from the words." },
  intensify: { label: "Intensify the adopted clip", hint: "The same, starting from this emotion's adopted clip instead of the base reference, to push it further." },
  directable: { label: "Directed (written instruction)", hint: "The narrator's own voice in the instruction-following model, with the emotion as a written instruction (stronger, experimental)." },
  design: { label: "From the designed voice", hint: "The designed voice this clone was promoted from, asked for the emotion. Often sounds like a different person." },
};

/** What a tile's play button shows. sample = {status, url?, job_id?}; local = {wanted, playing, blocked} for this tile. */
export function tileState(sample, local = {}) {
  const st = (sample && sample.status) || "none";
  if (local.playing) return { state: "playing", label: "Stop", icon: "■", busy: false };
  if (st === "ready") return local.blocked ? { state: "blocked", label: "Ready: press to play", icon: "▶", busy: false }
    : { state: "ready", label: "Play sample", icon: "▶", busy: false };
  if (st === "queued" || st === "running") return { state: st, label: st === "queued" ? "Waiting for the GPU worker" : "Generating the sample", icon: "…", busy: true };
  return { state: "none", label: "Generate and play a sample", icon: "▶", busy: false };
}

/** Plain-words verdict for a speaker similarity (cosine of Base speaker-encoder vectors vs the narrator's base reference).
 *  Bands come from one Mac calibration (docs/clone_instruct_experiment.json): the narrator's own takes 0.985-0.996, other people 0.93-0.965. */
export function similarityVerdict(sim, min, mock) {
  if (sim === null || sim === undefined) return { tone: "neutral", text: "similarity not measured" };
  const v = Number(sim).toFixed(3);
  if (mock) return { tone: "neutral", text: `similarity ${v} (mock engine: not a measurement)` };
  if (min !== null && min !== undefined && sim < min) return { tone: "bad", text: `similarity ${v}: below the adoption limit ${Number(min).toFixed(3)}` };
  if (sim >= 0.985) return { tone: "good", text: `similarity ${v}: sounds like the narrator` };
  if (sim >= 0.97) return { tone: "warn", text: `similarity ${v}: close, listen carefully` };
  return { tone: "bad", text: `similarity ${v}: likely a different voice` };
}

/** Group the 13 emotion rows by family for display, keeping the server's order inside each family. */
export const FAMILY_LABEL = { warm: "Warm", high: "Bright and high", low: "Low and heavy", tense: "Tense", hot: "Hot" };
export function byFamily(buckets) {
  const order = ["warm", "high", "low", "tense", "hot"];
  const groups = new Map(order.map((f) => [f, []]));
  for (const b of buckets || []) {
    if (!groups.has(b.family)) groups.set(b.family, []);
    groups.get(b.family).push(b);
  }
  return [...groups].filter(([, list]) => list.length).map(([family, list]) => ({ family, label: FAMILY_LABEL[family] || family, buckets: list }));
}

/** One line describing a clip's provenance. */
export function clipLine(ref) {
  if (!ref) return "";
  const src = ref.source === "synthetic" ? ((ref.metrics && ref.metrics.candidate_source) ? (SOURCES[ref.metrics.candidate_source] || {}).label || "synthetic" : "synthetic") : "recorded";
  return `${ref.duration_s}s · ${src}`;
}

export const NARRATOR_TABS = [["overview", "Overview"], ["emotions", "Emotion references"], ["preview", "Preview"], ["record", "Record & upload"], ["tools", "Tools"]];
export const tabOf = (q, kind) => {
  const t = q && q.get ? q.get("tab") : null;
  const ok = NARRATOR_TABS.some(([k]) => k === t) && (t !== "emotions" || kind === "clone");
  return ok ? t : "overview";
};
