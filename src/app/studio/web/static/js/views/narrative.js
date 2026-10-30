// Narrative page for one book: analysis stages, project-level overrides, character directory, story notes.
import { get, post, put, patch, api, html, setHTML, badge, fmtAgo, toast, toastError, busy, dialog, confirmDialog, onState, ApiError, qs, navigate } from "../ui.js";
import { NARRATIVE_FIELDS, allFields, getPath, setPath, narrativeInput, readInput } from "./narrative-fields.js";

const STAGES = [
  ["structure", "1 · Structure & speakers (Pass 1)"], ["attribution", "2 · LLM attribution of flagged lines"], ["characters", "3 · Characters"],
  ["summaries", "4 · Chapter & scene summaries"], ["segmentation", "5 · Segmentation (Pass 2)"], ["director", "6 · Narrative Director"], ["continuity", "7 · Continuity"],
];
const STAGE_TONE = { done: "good", running: "info", error: "bad", skipped: "neutral", pending: "neutral" };
const GENDERS = [["", "unknown"], ["female", "female"], ["male", "male"], ["neutral", "neutral"]];
const EDIT_FIELDS = [["accent", "Accent"], ["speech_pattern", "Speech pattern"], ["personality", "Personality"], ["description", "Description"],
  ["voice_description", "Voice description"], ["delivery_pref", "Delivery preference (e.g. “speaks softly”)"]];

function statsText(stats) {
  const keep = Object.entries(stats || {}).filter(([, v]) => typeof v !== "object");
  return keep.map(([k, v]) => `${k.replace(/_/g, " ")}: ${typeof v === "number" && !Number.isInteger(v) ? v.toFixed(2) : v}`).join(" · ");
}

function provBadges(c) {
  const p = c.prov || {};
  const fields = Object.keys(p).filter((k) => k !== "name" && k !== "aliases");
  if (!fields.length) return "";
  return html`${fields.map((f) => html`<span class="badge prov tone-${p[f].confirmed ? "good" : "neutral"}" title="${f}: ${p[f].source}, confidence ${Math.round(p[f].confidence * 100)}%${p[f].confirmed ? ", confirmed" : ", inferred"}">${f} · ${p[f].source}${p[f].confirmed ? " ✓" : ""}</span>`)}`;
}

function resultText(r) {
  const bits = [];
  if (r.segments !== undefined) bits.push(`${r.segments} segment(s) use this character`);
  if (r.resegmented) bits.push(`re-cut: ${r.resegmented.kept} kept, ${r.resegmented.created} new, ${r.resegmented.retired} retired`);
  if (r.reset) bits.push(`${r.reset.length} rendered segment(s) queued for regeneration`);
  return bits.join(" · ");
}

export async function render(view, parts) {
  const id = Number(parts[0]);
  const book = await get(`/api/books/${id}`);
  const vocab = await get("/api/narrative/vocabulary");
  let status = await get(`/api/books/${id}/narrative`);
  let bset = await get(`/api/books/${id}/narrative/settings`);
  let chars = await get(`/api/books/${id}/characters`);
  let notes = (await get(`/api/books/${id}/notes`)).notes;

  setHTML(view, html`<div class="page-head"><h1>Narrative: ${book.title}</h1>
    <div class="head-actions"><a class="btn" href="#/book/${id}">Book page</a><a class="btn" href="#/review/${id}">Review audio</a></div></div>
    <div id="nar-status"></div><div id="nar-ab"></div><div id="nar-over"></div><div id="nar-chars"></div><div id="nar-notes"></div>`);
  const $ = (sel) => view.querySelector(sel);

  // ------------------------------------------------------------------ status
  function paintStatus() {
    const s = status, job = s.job, active = job && ["queued", "running", "paused"].includes(job.status);
    setHTML($("#nar-status"), html`<section class="card" aria-labelledby="st-h"><h2 id="st-h">Analysis pipeline</h2>
      ${s.semantic ? "" : html`<div class="callout info"><strong>This book uses the classic segmenter.</strong> Speaker analysis, characters, the Narrative Director and the
        segment inspector's boundary editing need semantic segmentation (source offsets and speaker attribution).
        <div class="form-actions"><button class="btn primary" data-act="convert">Convert to semantic segmentation…</button></div></div>`}
      <div class="stats compact">
        <div class="stat"><div class="stat-value">${s.semantic_segments}/${s.segments}</div><div class="stat-label">segments with source offsets</div></div>
        <div class="stat ${s.needs_review ? "warn" : "good"}"><div class="stat-value">${s.needs_review}</div><div class="stat-label"><a href="#/review/${id}?filter=speaker_review">speaker needs review</a></div></div>
        <div class="stat"><div class="stat-value">${s.directed}</div><div class="stat-label">segments directed</div></div>
        <div class="stat ${s.direction_review ? "warn" : ""}"><div class="stat-value">${s.direction_review}</div><div class="stat-label"><a href="#/review/${id}?filter=direction_review">direction needs review</a></div></div>
        <div class="stat ${s.stale ? "warn" : ""}"><div class="stat-value">${s.stale}</div><div class="stat-label">stale directions</div></div>
        <div class="stat"><div class="stat-value">${s.characters}</div><div class="stat-label">characters</div></div>
      </div>
      <p class="small muted">Director: ${s.settings.director_enabled ? html`<strong>on</strong>` : "off"} · engine: ${s.settings.llm === "none" || !s.settings.model ? "deterministic rules (no LLM)" : html`LLM <code>${s.settings.model}</code>`}
        · cache: ${s.director_cache ? `${s.director_cache.n} entries, ${s.director_cache.hits} hits` : "–"}</p>
      <ol class="stage-list">${STAGES.map(([k, label]) => {
        const st = s.stages[k] || { status: "pending", stats: {} };
        return html`<li><span class="stage-name">${label}</span> <span class="badge tone-${STAGE_TONE[st.status] || "neutral"}">${st.status}</span>
          ${st.finished_at ? html`<span class="small muted">${fmtAgo(st.finished_at)}</span>` : ""}
          ${st.error ? html`<div class="small err">${st.error}</div>` : ""}
          ${statsText(st.stats) ? html`<div class="small muted">${statsText(st.stats)}</div>` : ""}</li>`;
      })}</ol>
      ${job ? html`<p class="small">Latest analysis job #${job.id}: ${badge(job.status)} ${job.error ? html`<span class="err">${job.error}</span>` : ""} <span class="muted">${fmtAgo(job.updated_at)}</span>
        ${active ? html`<span class="spinner" aria-hidden="true"></span> runs on the worker between narration steps (see <a href="#/queue">Queue</a>)` : ""}</p>` : ""}
      <div class="form-actions">
        <button class="btn primary" data-act="analyze" ${active || !s.semantic ? html`disabled` : ""}>Run analysis</button>
        <button class="btn" data-act="rebuild" ${active || !s.semantic ? html`disabled` : ""} title="Parse the stored manuscript again; characters, locked speakers and manual edits are kept">Re-run structure analysis…</button>
        <button class="btn" data-act="recompute" ${s.directed ? "" : html`disabled`} title="Re-run smoothing from stored directions; no LLM calls">Recompute continuity</button>
        <button class="btn" data-act="reseg" ${s.semantic ? "" : html`disabled`} title="Pass 2 again; unchanged segments keep their audio">Re-segment</button>
        <button class="btn" data-act="apply-delivery" ${s.segments ? "" : html`disabled`} title="Regenerate only the rendered segments whose sound would change under the delivery settings now in force">Apply new delivery settings to existing takes…</button>
        <a class="btn ghost" href="/api/books/${id}/narrative/metadata" target="_blank" rel="noopener">Segment metadata (JSON)</a>
      </div>
      <p class="small muted">Run analysis prepares speaker resolution, summaries, segments and directions. It runs on the GPU worker as a queued job and never changes the manuscript.</p>
    </section>`);
  }

  // ------------------------------------------------------------- A/B results
  function paintAB() {
    const ab = status.ab || {};
    setHTML($("#nar-ab"), html`<section class="card" aria-labelledby="abr-h"><h2 id="abr-h">Blind A/B listening results</h2>
      <p class="small muted">Compare two delivery settings on the same segment without knowing which clip is which (open any segment in the inspector, or start a round below).
        Votes are kept per book.</p>
      <div class="callout info small"><strong>Rule of thumb:</strong> adopt a feature if it wins at least 60% of non-tie votes and “another narrator” flags stay at or below 10%.</div>
      <div class="table-wrap"><table class="table compact"><thead><tr><th scope="col">Comparison</th><th scope="col">Votes</th><th scope="col">First setting, share of non-tie votes</th>
        <th scope="col">“Another narrator” flags</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead><tbody>
        ${Object.entries(ab).map(([k, t]) => html`<tr><th scope="row">${t.title}<div class="small muted">First: ${t.a_label}<br>Second: ${t.b_label}</div></th>
          <td>${t.votes}<div class="small muted">${t.a_wins} first · ${t.b_wins} second · ${t.same} no difference</div></td>
          <td>${t.a_share === null ? html`<span class="muted">no non-tie votes yet</span>` : html`<strong>${Math.round(t.a_share * 100)}%</strong> of ${t.non_tie}
            <span class="badge tone-${t.a_share >= 0.6 ? "good" : "neutral"}">${t.a_share >= 0.6 ? "at or above 60%" : "below 60%"}</span>`}</td>
          <td>${t.other_narrator_flags}${t.other_narrator_share === null ? "" : html` (${Math.round(t.other_narrator_share * 100)}%)
            <span class="badge tone-${t.other_narrator_share <= 0.1 ? "good" : "warn"}">${t.other_narrator_share <= 0.1 ? "at or below 10%" : "above 10%"}</span>`}</td>
          <td><button class="btn small" data-act="ab-round" data-cmp="${k}">Start a round on a random differing segment</button></td></tr>`)}
      </tbody></table></div></section>`);
  }

  async function abRound(cmp, btn) {
    const seg = await busy(btn, () => get(`/api/books/${id}/ab/random` + qs({ comparison: cmp })));
    const round = await busy(btn, () => post(`/api/segments/${seg.segment_id}/ab`, { comparison: cmp }));
    toast(`Segment ${seg.sid} chosen. Rendering the two clips; listen in the inspector.`, "info");
    navigate(`#/inspect/${seg.segment_id}?ab=${round.round_id}`);
  }

  async function applyDelivery(btn) {
    let first = null;
    try {
      first = await post(`/api/books/${id}/narrative/apply-delivery`, {});
    } catch (e) {
      if (!(e instanceof ApiError && e.status === 409 && e.details && e.details.segments !== undefined)) { toastError(e); return; }
      const d = e.details;
      const res = await dialog({ title: "Apply new delivery settings?", form: true, buttons: [{ label: "Regenerate", value: "go", kind: "primary" }], body: html`
        <p>${e.message}</p>
        <p class="small">Segments whose sound does not change keep their audio. Any release approval is revoked, and the regenerated segments are queued first.</p>
        ${d.approved ? html`<label class="check"><input type="checkbox" name="include_approved"> Also regenerate approved segments (${d.approved} would change)</label>` : ""}` });
      if (!res) return;
      const r = await busy(btn, () => post(`/api/books/${id}/narrative/apply-delivery`, { confirm: true, include_approved: res.data.include_approved === "on" }));
      toast(`${r.reset} segment(s) marked for regeneration and queued first.`, "success", 8000);
      status = await get(`/api/books/${id}/narrative`); paintStatus(); paintAB();
      return;
    }
    toast(first.message || "Nothing to apply.", "info", 8000);
  }

  // --------------------------------------------------------------- overrides
  function paintOverrides() {
    const eff = bset.effective, ov = bset.overrides, def = bset.defaults;
    setHTML($("#nar-over"), html`<section class="card" aria-labelledby="ov-h"><h2 id="ov-h">Project settings for this book</h2>
      <p class="small muted">Tick “override” to use a value for this book only; unticked fields follow the global defaults in
        <a href="#/settings">Settings</a>. Changing the overrides is an administrator action.</p>
      <form id="ov-form" class="form"><div class="ov-grid">
        ${NARRATIVE_FIELDS.flatMap(([, fields]) => fields).filter((f) => f[0] !== "semantic_segmentation").map((f) => {
          const has = getPath(ov, f[0]) !== undefined;
          return html`<div class="ov-row ${has ? "on" : ""}"><label class="check ov-check"><input type="checkbox" name="ov:${f[0]}" ${has ? html`checked` : ""}> override</label>
            ${narrativeInput("val:", f, getPath(eff, f[0]))}<div class="small muted">default: ${String(getPath(def, f[0]))}</div></div>`;
        })}</div>
        <button class="btn" type="submit">Save project settings</button></form></section>`);
  }

  async function saveOverrides(form, btn) {
    const patchObj = {};
    for (const f of allFields().filter((x) => x[0] !== "semantic_segmentation")) {
      const on = form.elements[`ov:${f[0]}`].checked;
      const v = readInput(form, "val:", f);
      const was = getPath(bset.overrides, f[0]) !== undefined;
      if (on && v !== undefined) setPath(patchObj, f[0], v);
      else if (was) setPath(patchObj, f[0], null); // remove an override that exists; do not send empty groups
    }
    try {
      bset = await busy(btn, () => put(`/api/books/${id}/narrative/settings`, { overrides: patchObj }), "Project settings saved");
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) toast("Only an administrator can change a project's narrative settings.", "error", 9000);
    }
    status = await get(`/api/books/${id}/narrative`);
    paintOverrides(); paintStatus();
  }

  // -------------------------------------------------------------- characters
  const charName = (cid) => (chars.characters.find((c) => c.id === cid) || {}).name || "?";

  function paintChars() {
    const list = chars.characters;
    setHTML($("#nar-chars"), html`<section class="card" aria-labelledby="ch-h"><h2 id="ch-h">Characters (${list.filter((c) => c.kind !== "narrator").length})</h2>
      <p class="small muted">Characters keep one identity across chapters. Things you set here are confirmed and are never overwritten by analysis. Assigning a different voice re-cuts only the
        segments that character speaks in and regenerates only the audio that actually changes.</p>
      <div class="table-wrap"><table class="table compact char-table"><thead><tr><th scope="col">Character</th><th scope="col">Aliases</th><th scope="col">Gender</th>
        <th scope="col">Voice</th><th scope="col">Lines</th><th scope="col">Provenance</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
        <tbody>${list.map((c) => html`<tr data-char="${c.id}">
          <td><strong>${c.name}</strong> ${c.kind === "narrator" ? html`<span class="badge tone-info">narrator</span>` : ""}
            ${c.first_chapter ? html`<div class="small muted">first seen: chapter ${c.first_chapter}</div>` : ""}</td>
          <td class="small">${c.aliases.length ? c.aliases.map((a) => html`<span class="chip">${a}</span> `) : html`<span class="muted">–</span>`}</td>
          <td class="small">${c.gender || html`<span class="muted">unknown</span>`}</td>
          <td>${c.kind === "narrator" ? html`<span class="small">book narrator</span>`
            : html`<label class="sr-only" for="v-${c.id}">Voice for ${c.name}</label><select id="v-${c.id}" data-voice="${c.id}"><option value="">Book narrator voice</option>
              ${chars.voices.map((v) => html`<option value="${v.id}" ${c.voice && c.voice.id === v.id ? html`selected` : ""}>${v.name} (${v.kind})</option>`)}</select>`}</td>
          <td class="small">${c.segments} segment(s)${c.review_count ? html`<div class="warn-text">${c.review_count} to review</div>` : ""}</td>
          <td class="small prov-cell">${provBadges(c)}</td>
          <td class="nowrap">${c.kind === "narrator" ? "" : html`<button class="btn small" data-act="char-edit" data-id="${c.id}">Edit</button>
            <button class="btn small ghost" data-act="char-merge" data-id="${c.id}">Merge…</button>`}</td></tr>`)}</tbody></table></div>
      <form id="char-new" class="inline-form"><label>New character <input name="name" maxlength="80" required placeholder="Name"></label><button class="btn small" type="submit">Add</button></form>
    </section>`);
  }

  async function reloadChars() {
    chars = await get(`/api/books/${id}/characters`);
    status = await get(`/api/books/${id}/narrative`);
    paintChars(); paintStatus();
  }

  async function editChar(cid) {
    const c = chars.characters.find((x) => x.id === cid);
    const res = await dialog({ title: `Edit ${c.name}`, form: true, buttons: [{ label: "Save", value: "save", kind: "primary" }], body: html`
      <label>Name <input name="name" maxlength="80" value="${c.name}" required></label>
      <label>Aliases (comma separated: other names, titles, nicknames)<input name="aliases" value="${c.aliases.join(", ")}"></label>
      <label>Gender (used to resolve “he said / she said”)<select name="gender">${GENDERS.map(([v, l]) => html`<option value="${v}" ${v === c.gender ? html`selected` : ""}>${l}</option>`)}</select></label>
      ${EDIT_FIELDS.map(([k, l]) => html`<label>${l}<input name="${k}" maxlength="600" value="${c[k] || ""}"></label>`)}` });
    if (!res) return;
    const d = res.data, body = {};
    if (d.name.trim() !== c.name) body.name = d.name.trim();
    const aliases = d.aliases.split(",").map((x) => x.trim()).filter(Boolean);
    if (aliases.join("|") !== c.aliases.join("|")) body.aliases = aliases;
    if (d.gender !== c.gender) body.gender = d.gender;
    EDIT_FIELDS.forEach(([k]) => { if ((d[k] || "") !== (c[k] || "")) body[k] = d[k]; });
    if (!Object.keys(body).length) return;
    const r = await busy(null, () => patch(`/api/characters/${cid}`, body), "Character saved (confirmed)");
    if (r.voice_changed) toast(resultText(r), "info", 8000);
    await reloadChars();
  }

  async function mergeChar(cid) {
    const c = chars.characters.find((x) => x.id === cid);
    const others = chars.characters.filter((x) => x.id !== cid && x.kind !== "narrator");
    if (!others.length) { toast("There is no other character to merge into.", "info"); return; }
    const res = await dialog({ title: `Merge ${c.name} into…`, form: true, buttons: [{ label: "Merge", value: "merge", kind: "danger" }], body: html`
      <p class="small">All of ${c.name}'s lines move to the character you choose, and “${c.name}” becomes an alias so the same person is recognised in later chapters.</p>
      <label>Merge into <select name="into">${others.map((o) => html`<option value="${o.id}">${o.name}</option>`)}</select></label>` });
    if (!res) return;
    const into = Number(res.data.into);
    if (!(await confirmDialog("Merge characters?", `Merge “${c.name}” into “${charName(into)}”? Segments that change voice are regenerated; others keep their audio.`, "Merge", true))) return;
    const r = await busy(null, () => post(`/api/characters/${cid}/merge`, { into_id: into }), "Characters merged");
    toast(resultText(r), "info", 8000);
    await reloadChars();
  }

  // ------------------------------------------------------------------- notes
  function paintNotes() {
    const sums = notes.filter((n) => n.kind === "summary"), facts = notes.filter((n) => n.kind !== "summary");
    setHTML($("#nar-notes"), html`<section class="card" aria-labelledby="nt-h"><h2 id="nt-h">Story notes</h2>
      <p class="small muted">Facts you add here are treated as confirmed and given to the Director. Summaries are made of sentences copied word for word from the chapter
        (<em>extractive</em>); an LLM summary, if present, is only an unconfirmed interpretation and is rejected if it names anything that is not in the chapter.</p>
      <h3>Facts (${facts.length})</h3>
      ${facts.length ? html`<ul class="notes">${facts.map((n) => html`<li>${n.text} <span class="badge tone-${n.confirmed ? "good" : "neutral"}">${n.source}${n.confirmed ? " ✓" : ""}</span>
        ${n.character_id ? html`<span class="small muted">· ${charName(n.character_id)}</span>` : ""}
        <button class="btn small ghost danger" data-act="note-del" data-id="${n.id}" aria-label="Delete note">Delete</button></li>`)}</ul>` : html`<p class="muted small">None yet.</p>`}
      <form id="note-new" class="form"><label>Add a story fact (e.g. “Elena is Hale's former patient”)<input name="text" minlength="3" maxlength="600" required></label>
        <label>About <select name="character_id"><option value="">the story in general</option>${chars.characters.filter((c) => c.kind !== "narrator").map((c) => html`<option value="${c.id}">${c.name}</option>`)}</select></label>
        <button class="btn small" type="submit">Add fact</button></form>
      <details><summary>Chapter and scene summaries (${sums.length})</summary>
        <ul class="notes">${sums.map((n) => html`<li><strong>Chapter ${n.chapter_idx}${n.scene_idx === null || n.scene_idx === undefined ? "" : `, scene ${n.scene_idx + 1}`}</strong>
          <span class="badge tone-${n.source === "extractive" ? "good" : "warn"}">${n.source === "extractive" ? "extractive — verbatim sentences" : "LLM summary — inferred, unconfirmed"}</span>
          <div class="small">${n.text}</div></li>`)}</ul></details>
    </section>`);
  }

  // ------------------------------------------------------------------- events
  async function runAnalysis(body, btn, okMsg) {
    try {
      await busy(btn, () => post(`/api/books/${id}/analyze`, body), okMsg);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.details && e.details.rendered_segments !== undefined) {
        if (await confirmDialog("Re-cut this book?", e.message, "Convert & regenerate", true))
          await busy(btn, () => post(`/api/books/${id}/analyze`, { ...body, confirm: true }), "Conversion queued");
      }
    }
    status = await get(`/api/books/${id}/narrative`);
    paintStatus();
  }

  async function onClick(e) {
    const t = e.target.closest("[data-act]"); if (!t || !view.contains(t)) return;
    const a = t.dataset.act;
    try {
      if (a === "analyze") await runAnalysis({}, t, "Analysis queued");
      else if (a === "convert") await runAnalysis({ rebuild: true }, t, "Conversion queued");
      else if (a === "rebuild") { if (await confirmDialog("Re-run structure analysis?", "The stored manuscript is parsed again. Characters, speakers you confirmed and boundary edits are kept; segments whose text range is unchanged keep their audio.", "Re-run")) await runAnalysis({ rebuild: true }, t, "Analysis queued"); }
      else if (a === "recompute") { const r = await busy(t, () => post(`/api/books/${id}/continuity/recompute`)); toast(`Recomputed ${r.recomputed} direction(s); ${r.changed.length} changed, ${r.audio_reset.length} segment(s) queued for regeneration.`, "success", 8000); status = await get(`/api/books/${id}/narrative`); paintStatus(); }
      else if (a === "reseg") { const r = await busy(t, () => post(`/api/books/${id}/resegment`)); toast(`Re-segmented: ${r.kept} unchanged, ${r.created} new, ${r.retired} retired; ${r.audio_reset} regenerating.`, "success", 8000); status = await get(`/api/books/${id}/narrative`); paintStatus(); }
      else if (a === "apply-delivery") await applyDelivery(t);
      else if (a === "ab-round") await abRound(t.dataset.cmp, t);
      else if (a === "char-edit") await editChar(Number(t.dataset.id));
      else if (a === "char-merge") await mergeChar(Number(t.dataset.id));
      else if (a === "note-del") { await busy(t, () => api("DELETE", `/api/notes/${t.dataset.id}`), "Note deleted"); notes = (await get(`/api/books/${id}/notes`)).notes; paintNotes(); }
    } catch (err) { /* toast shown by busy() */ }
  }

  async function onChange(e) {
    const sel = e.target.closest("select[data-voice]"); if (!sel) return;
    const cid = Number(sel.dataset.voice);
    try {
      const r = await busy(sel, () => patch(`/api/characters/${cid}`, { voice_id: sel.value ? Number(sel.value) : null }), "Voice assigned");
      if (r.voice_changed) toast(resultText(r) || "Voice assigned", "info", 9000);
    } catch { /* toast shown */ }
    await reloadChars();
  }

  async function onSubmit(e) {
    e.preventDefault();
    try {
      if (e.target.id === "ov-form") await saveOverrides(e.target, e.submitter);
      else if (e.target.id === "char-new") {
        await busy(e.submitter, () => post(`/api/books/${id}/characters`, { name: new FormData(e.target).get("name") }), "Character added");
        await reloadChars();
      } else if (e.target.id === "note-new") {
        const fd = new FormData(e.target);
        await busy(e.submitter, () => post(`/api/books/${id}/notes`, { text: fd.get("text"), character_id: fd.get("character_id") ? Number(fd.get("character_id")) : null }), "Fact added");
        notes = (await get(`/api/books/${id}/notes`)).notes; paintNotes();
      }
    } catch { /* toast shown */ }
  }

  paintStatus(); paintAB(); paintOverrides(); paintChars(); paintNotes();
  view.addEventListener("click", onClick);
  view.addEventListener("change", onChange);
  view.addEventListener("submit", onSubmit);
  const off = onState(async () => {
    try { status = await get(`/api/books/${id}/narrative`); paintStatus(); paintAB(); } catch { /* transient */ }
  }, 3000);
  return () => { off(); view.removeEventListener("click", onClick); view.removeEventListener("change", onChange); view.removeEventListener("submit", onSubmit); };
}
export { toastError };
