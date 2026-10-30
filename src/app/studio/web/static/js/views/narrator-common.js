// Dialogs and small views shared by the Narrator Studio tiles (voices.js) and the narrator page (narrator.js).
import { get, post, patch, html, toast, toastError, busy, dialog } from "../ui.js";

export const DEFAULT_TEXT = "Chapter One. The rain had stopped by the time Eleanor reached the harbour, and the lamps along the quay were reflected in long, trembling lines on the black water.";

export function lineage(v) {
  const d = v.derived_from;
  if (d) return d.relation === "retune" ? `Retuned from ${d.voice_name}` : `Copy of ${d.voice_name}`;
  if (v.kind === "clone" && v.source_voice) return `Promoted from the designed voice ${v.source_voice.name}${v.source_voice.archived ? " (archived)" : ""}`;
  return "";
}

const normNl = (t) => String(t || "").split("\r").join("").trim();
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

export function freeName(voices, base) {
  const taken = new Set((voices || []).map((x) => x.name.toLowerCase()));
  let n = base, i = 2;
  while (taken.has(n.toLowerCase())) n = base.replace(/\)$/, "") + ` ${i++})`;
  return n.slice(0, 80);
}

function versionNote(v) {
  const nv = v.version.version;
  if (v.builtin) return html`<p class="callout info small"><strong>${v.name}</strong> is a built-in speaker, so its name and speaker are fixed. You can change only its
    <strong>delivery instruction</strong>. Saving creates <strong>version ${nv + 1}</strong> of ${v.name}; previews and new books use it straight away, but books already
    narrated with version ${nv} keep that version until you switch them on the book page. To keep ${v.name} as it is and try a variation, use
    <strong>Duplicate &amp; retune…</strong>.</p>`;
  if (v.kind === "clone") return html`<p class="callout info small">A cloned voice is defined by its reference clip, so only its name can be edited here.
    Emotion reaches it through its emotion reference clips.
    ${v.source_voice ? html`To make a variant, use <strong>Retune from ‘${v.source_voice.name}’…</strong>` : "To make a variant, upload a new reference clip"}.</p>`;
  return html`<p class="callout info small">${v.kind === "design"
    ? html`You can change the <strong>name</strong> and the <strong>description</strong>. A new description makes VoiceDesign create a different-sounding voice.`
    : html`You can change the <strong>name</strong>, the <strong>speaker</strong> and the <strong>delivery instruction</strong>.`}
    Changing the sound saves <strong>version ${nv + 1}</strong> of this voice (renaming does not). Previews and new books use it straight away; books already narrated with
    version ${nv} keep it until you switch them on the book page. Prefer a separate voice? Use <strong>Duplicate &amp; retune…</strong>.</p>`;
}

/** Edit dialog. Returns {voice, note} after a save, {duplicate: instruct} when the user chose to duplicate instead, or null. */
export async function editVoice(b, v, speakers) {
  const nv = v.version.version;
  const res = await dialog({ title: v.builtin ? `Delivery for ${v.name} (built-in)` : `Edit ${v.name}`, form: true,
    buttons: [{ label: v.kind === "clone" ? "Save name" : v.builtin ? `Save as version ${nv + 1}` : "Save", value: "save", kind: "primary" },
      ...(v.kind !== "clone" ? [{ label: "Duplicate & retune instead…", value: "dup" }] : [])],
    body: html`${versionNote(v)}
      ${!v.builtin ? html`<label>Name <input name="name" required maxlength="80" value="${v.name}"></label>` : ""}
      ${v.kind === "preset" && !v.builtin ? html`<label>Speaker <select name="speaker">${Object.keys(speakers || {}).map((s) => html`<option ${s === v.version.speaker ? html`selected` : ""}>${s}</option>`)}</select></label>`
        : v.kind === "preset" ? html`<p class="small">Speaker: <strong>${v.version.speaker}</strong> (fixed)</p>` : ""}
      ${v.kind !== "clone" ? html`<label>${v.kind === "design" ? "Description" : "Delivery instruction"} <textarea name="instruct" rows="4" maxlength="1000" ${v.kind === "design" ? html`required minlength="10"` : ""}>${v.version.instruct || ""}</textarea></label>` : ""}` });
  if (!res) return null;
  if (res.value === "dup") return { duplicate: res.data.instruct };
  const body = {};
  if (res.data.name !== undefined && res.data.name.trim() !== v.name) body.name = res.data.name;
  if (res.data.instruct !== undefined && normNl(res.data.instruct) !== normNl(v.version.instruct)) body.instruct = res.data.instruct;
  if (res.data.speaker !== undefined && res.data.speaker !== v.version.speaker) body.speaker = res.data.speaker;
  if (!Object.keys(body).length) { toast("Nothing was changed.", "info"); return null; }
  let out;
  try { out = await busy(b, () => patch(`/api/voices/${v.id}`, body)); } catch (e) { return null; }
  const nver = out.version.version, made = nver !== nv, older = out.books_on_older_version || [];
  const vers = [...new Set(older.map((x) => x.voice_version).filter((x) => x))].sort((x, y) => x - y);
  const still = older.length ? ` ${plural(older.length, "book still uses", "books still use")} ${vers.length ? (vers.length === 1 ? `version ${vers[0]}` : `versions ${vers.join(" and ")}`) : "an earlier version"}.` : "";
  const msg = made ? `Saved as version ${nver}.${still}` : "Saved (name only: no new version).";
  toast(msg, made && older.length ? "warn" : "success", 10000);
  return { voice: out, note: made ? { tone: "good", text: `${msg} Previews and new books use version ${nver} now.${older.length ? " Switch those books from their book page to hear the new sound." : ""}` } : null };
}

/** Duplicate & retune dialog for a preset or designed voice. Returns the new voice or null. */
export async function duplicateVoice(b, v, speakers, voices, instruct) {
  const text = instruct !== undefined ? instruct : (v.version.instruct || "");
  const res = await dialog({ title: `Duplicate & retune ${v.name}`, form: true, buttons: [{ label: "Create copy", value: "ok", kind: "primary" }],
    body: html`<p class="callout info small">Creates a <strong>new, separate voice</strong> from ${v.name}’s current settings (version ${v.version.version}) with the delivery you choose below.
      ${v.name} itself and the books using it are not changed. Then generate a preview of the copy, and keep refining it until you like it.</p>
      <label>Name of the new voice <input name="name" required maxlength="80" value="${freeName(voices, v.name + " (retune)")}"></label>
      ${v.kind === "preset" ? html`<label>Speaker <select name="speaker">${Object.keys(speakers || {}).map((s) => html`<option ${s === v.version.speaker ? html`selected` : ""}>${s}</option>`)}</select></label>` : ""}
      <label>${v.kind === "design" ? "Voice description" : "Delivery instruction"} <textarea name="instruct" rows="5" maxlength="1000" ${v.kind === "design" ? html`required minlength="10"` : ""}>${text}</textarea></label>` });
  if (!res) return null;
  const body = { name: res.data.name };
  if (res.data.instruct !== undefined) body.instruct = res.data.instruct;
  if (res.data.speaker !== undefined && res.data.speaker !== v.version.speaker) body.speaker = res.data.speaker;
  try { return await busy(b, () => post(`/api/voices/${v.id}/duplicate`, body), `Copy of ${v.name} created`); } catch (e) { return null; }
}

/** Retune a clone from the designed voice it came from. Returns the new designed voice or null. */
export async function retuneVoice(b, v, voices) {
  const src = v.source_voice;
  let srcFull;
  try { srcFull = await get(`/api/voices/${src.id}`); } catch (e) { toastError(e); return null; }
  const res = await dialog({ title: `Retune from ‘${src.name}’`, form: true, buttons: [{ label: "Create designed voice", value: "ok", kind: "primary" }],
    body: html`<p class="callout info small">${v.name} is a stable clone. This creates a <strong>new designed voice</strong> from ‘${src.name}’
      (the voice ${v.name} was made from), with the description below. ${v.name} is not changed.
      ${src.archived ? " (The source voice is archived; it can still be used.)" : ""}</p>
      <label>Name of the new voice <input name="name" required maxlength="80" value="${freeName(voices, src.name + " (retune)")}"></label>
      <label>Voice description <textarea name="instruct" rows="5" required minlength="10" maxlength="1000">${srcFull.version.instruct || ""}</textarea></label>` });
  if (!res) return null;
  try { return await busy(b, () => post(`/api/voices/${v.id}/retune`, { name: res.data.name, instruct: res.data.instruct }), "Retuned design voice created"); } catch (e) { return null; }
}

/** Promote a designed voice's preview to a stable cloned narrator. Returns the new clone or null. */
export async function promotePreview(b, jobId, name) {
  const res = await dialog({ title: "Promote preview to a stable narrator", form: true, buttons: [{ label: "Create narrator", value: "ok", kind: "primary" }],
    body: html`<p>This preview becomes the reference clip of a new cloned narrator, so every segment uses the same voice.</p>
      <label>Name <input name="name" required maxlength="80" value="${name} (stable)"></label>` });
  if (!res) return null;
  try { return await busy(b, () => post("/api/voices/promote", { job_id: Number(jobId), name: res.data.name }), "Stable narrator created"); } catch (e) { return null; }
}
