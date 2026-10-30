// Segment Inspector: text, context windows, speakers, direction, delivery sent to the engine, boundaries, takes.
import { get, post, patch, html, setHTML, badge, fmtDate, fmtDuration, toast, toastError, busy, confirmDialog, onState, player, qs, navigate } from "../ui.js";

const DIR_FIELDS = [["emotion", "Emotion"], ["intensity", "Intensity"], ["pacing", "Pacing"], ["tension", "Tension"], ["delivery", "Delivery note"]];

function dirCell(d, k) { return d && d[k] !== undefined && d[k] !== "" ? String(d[k]) : "–"; }

function stateLine(n, label) {
  if (!n) return html`<div class="dir-nbr"><strong>${label}</strong> <span class="muted small">none in this chapter</span></div>`;
  const f = n.final;
  return html`<div class="dir-nbr"><strong>${label}</strong> <a href="#/inspect/${n.id}"><code>${n.sid}</code></a> ${badge(n.status)}
    ${f ? html`<span class="badge tone-info">${f.emotion} ${f.intensity}/5</span> <span class="badge tone-neutral">${f.pacing}</span>` : html`<span class="muted small">no direction yet</span>`}
    <div class="small muted">${n.text}</div></div>`;
}

export async function render(view, parts, query) {
  const sid = Number(parts[0]);
  const vocab = await get("/api/narrative/vocabulary");
  const st = { ins: null, det: null, chars: [], voices: [], mode: null, preview: null, bookId: null };
  // The page redraws itself on live updates; the A/B card lives in its own element next to the redrawn part, so a round in progress
  // (selected comparison, playing clips, chosen radio, checkbox) is never rebuilt by those refreshes.
  const mainEl = document.createElement("div");
  const abEl = document.createElement("section");
  abEl.className = "card"; abEl.id = "ab-card"; abEl.setAttribute("aria-labelledby", "ab-h");
  view.replaceChildren(mainEl, abEl);
  const ab = { comparisons: [], cmp: "directed_vs_undirected", rid: null, round: null, choice: "", other: false, creating: false, voting: false, error: "", note: "", dead: false, tally: null };

  async function load() {
    st.ins = await get(`/api/segments/${sid}/inspect`);
    st.det = await get(`/api/segments/${sid}`);
    st.bookId = st.det.book.id;
    const c = await get(`/api/books/${st.bookId}/characters`);
    st.chars = c.characters; st.voices = c.voices;
  }
  const charName = (cid) => (st.chars.find((c) => c.id === cid) || {}).name || (cid ? "?" : null);

  function words(i) {
    const out = []; const re = /\S+/g; let m;
    while ((m = re.exec(i.original_text))) out.push([m[0], i.src_start + m.index]);
    return out;
  }

  function textBlock(i) {
    if (!i.semantic) return html`<p class="ms-text">${i.original_text}</p>`;
    if (!st.mode) return html`<p class="ms-text">${i.original_text}</p>`;
    return html`<p class="ms-text word-pick" aria-label="Choose a word">${words(i).map(([w, off], k) => (k === 0
      ? html`<span>${w}</span> ` : html`<button type="button" class="wd" data-act="pick" data-off="${off}">${w}</button> `))}</p>`;
  }

  function spansTable(i) {
    if (!i.spans || !i.spans.length) return "";
    return html`<section><h3>Speakers in this segment</h3><div class="table-wrap"><table class="table compact"><thead><tr><th scope="col">Text</th><th scope="col">Kind</th>
      <th scope="col">Speaker</th><th scope="col">How decided</th><th scope="col"><span class="sr-only">Assign</span></th></tr></thead><tbody>
      ${i.spans.map((sp) => html`<tr class="${sp.needs_review ? "span-review" : ""}">
        <td class="small span-text">${sp.text.length > 90 ? sp.text.slice(0, 90) + "…" : sp.text}</td>
        <td><span class="badge tone-${sp.kind === "dialogue" ? "info" : "neutral"}">${sp.kind}</span></td>
        <td>${sp.kind !== "dialogue" ? html`<span class="muted small">narrator</span>` : (sp.speaker_id && !sp.needs_review ? html`<strong>${charName(sp.speaker_id) || "?"}</strong>` : html`<span class="badge tone-warn">not established</span>`)}</td>
        <td class="small">${sp.kind === "dialogue" ? html`${sp.attribution} · ${Math.round(sp.confidence * 100)}%${sp.locked ? html` <span class="badge tone-good">confirmed</span>` : ""}
          <div class="muted">${sp.evidence}</div>
          ${sp.candidates.length ? html`<div>Candidates: ${sp.candidates.map((c) => { const ch = st.chars.find((x) => x.name === c); return ch ? html`<button class="btn small ghost" data-act="assign" data-char="${ch.id}">Assign ${c}</button> ` : html`<span class="chip">${c}</span> `; })}</div>` : ""}` : ""}</td>
        <td></td></tr>`)}</tbody></table></div>
      <form id="assign-form" class="inline-form"><label>Assign every line in this segment to
        <select name="character_id">${st.chars.filter((c) => c.kind !== "narrator").map((c) => html`<option value="${c.id}" ${i.speaker.character_id === c.id ? html`selected` : ""}>${c.name}</option>`)}</select></label>
        <button class="btn small" type="submit">Assign &amp; lock</button></form>
      <p class="small muted">An assignment you make is locked: re-analysis never overwrites it.</p></section>`;
  }

  function directionPanel(i) {
    const d = i.direction;
    const voiceOpts = st.voices;
    const fin = d ? d.final : null;
    return html`<section><h3>Narrative direction ${d ? html`<span class="badge tone-neutral">${d.source}</span>${d.stale ? html` <span class="badge tone-warn">stale</span>` : ""}${d.needs_review ? html` <span class="badge tone-warn">review</span>` : ""}` : ""}</h3>
      ${d ? html`<div class="table-wrap"><table class="table compact dir-table"><thead><tr><th scope="col"></th>${DIR_FIELDS.map(([, l]) => html`<th scope="col">${l}</th>`)}</tr></thead><tbody>
        <tr><th scope="row">Raw (Director)</th>${DIR_FIELDS.map(([k]) => html`<td>${dirCell(d.raw, k)}</td>`)}</tr>
        <tr><th scope="row">After continuity</th>${DIR_FIELDS.map(([k]) => html`<td>${k === "delivery" ? "–" : dirCell(d.smoothed, k)}</td>`)}</tr>
        <tr><th scope="row">Your override</th>${DIR_FIELDS.map(([k]) => html`<td>${dirCell(d.user, k)}</td>`)}</tr>
        <tr class="final-row"><th scope="row">Final</th>${DIR_FIELDS.map(([k]) => html`<td><strong>${dirCell(d.final, k)}</strong></td>`)}</tr></tbody></table></div>
        <p class="small"><strong>Continuity:</strong> ${d.smoothing_note || "–"}</p>
        ${d.raw.confidence ? html`<p class="small muted">Confidence — speaker ${Math.round((d.raw.confidence.speaker || 0) * 100)}%, emotion ${Math.round((d.raw.confidence.emotion || 0) * 100)}%, overall ${Math.round((d.raw.confidence.overall || 0) * 100)}%</p>` : ""}
        ${d.raw.ambiguity ? html`<div class="callout warn small"><strong>Ambiguity:</strong> ${d.raw.ambiguity}</div>` : ""}
        <div class="two-col">
          <div><h4>Facts <span class="small muted">(quoted from the manuscript)</span></h4>${(d.raw.facts || []).length ? html`<ul class="small">${d.raw.facts.map((f) => html`<li>“${f}”</li>`)}</ul>` : html`<p class="small muted">none</p>`}</div>
          <div><h4>Inferences <span class="small muted">(interpretation)</span></h4>${(d.raw.inferences || []).length ? html`<ul class="small">${d.raw.inferences.map((f) => html`<li>${f}</li>`)}</ul>` : html`<p class="small muted">none</p>`}</div></div>
        ${(d.raw.notes || []).length ? html`<ul class="small warn-text">${d.raw.notes.map((n) => html`<li>${n}</li>`)}</ul>` : ""}`
      : html`<p class="muted small">No direction yet. The Narrative Director may be off for this book, or the analysis has not reached this segment. You can still set one by hand below.</p>`}
      <form id="dir-form" class="form"><div class="form-grid">
        <label>Emotion<select name="emotion"><option value="">(no override)</option>${vocab.emotions.map((e) => html`<option ${fin && d.user.emotion === e ? html`selected` : ""}>${e}</option>`)}</select></label>
        <label>Intensity (1–5)<input type="number" name="intensity" min="1" max="5" value="${d && d.user.intensity ? d.user.intensity : ""}"></label>
        <label>Tension (1–5)<input type="number" name="tension" min="1" max="5" value="${d && d.user.tension ? d.user.tension : ""}"></label>
        <label>Pacing<select name="pacing"><option value="">(no override)</option>${vocab.pacings.map((e) => html`<option ${d && d.user.pacing === e ? html`selected` : ""}>${e}</option>`)}</select></label></div>
        <label>Delivery note (max 240 chars)<input name="delivery" maxlength="240" value="${d && d.user.delivery ? d.user.delivery : ""}" placeholder="e.g. voice breaking, barely above a whisper"></label>
        <div class="form-actions"><button class="btn primary" type="submit">Save override &amp; regenerate this segment</button>
          <button class="btn ghost" type="button" data-act="clear-dir" ${d && Object.keys(d.user).length ? "" : html`disabled`}>Clear overrides</button>
          <button class="btn ghost" type="button" data-act="redirect">Re-run Director for this segment</button></div></form>
      <div class="dir-nbrs">${stateLine(i.previous, "Previous")}${stateLine(i.next, "Next")}</div>
      ${d && d.history && d.history.length ? "" : ""}
    </section>`;
  }

  function deliveryPanel(i) {
    const v = i.voice;
    return html`<section><h3>What is sent to the engine</h3>
      <dl class="kv small"><dt>Voice</dt><dd>${v.name} (${v.kind}) <code>${v.model_id}</code>${v.override ? html` <span class="badge tone-info">segment override</span>` : ""}</dd>
        <dt>Instruction</dt><dd>${i.delivery.instruct ? html`<code>${i.delivery.instruct}</code>` : html`<span class="muted">none</span>`}
          ${i.delivery.applied ? html` <span class="badge tone-good">applied</span>` : html` <span class="badge tone-neutral">not applied</span>`}
          <div class="muted">${i.delivery.reason}</div></dd>
        <dt>Spoken text</dt><dd><code class="spoken">${i.delivery.spoken_text_sent_to_tts}</code>
          <div class="muted">Exactly this string is given to the TTS engine. Context text and instructions are never part of it.</div></dd>
        <dt>Generation key</dt><dd><code>${i.delivery.gen_key}</code></dd></dl>
      <form id="voice-form" class="inline-form"><label>Voice profile for this segment
        <select name="voice_id"><option value="">Use the character's voice (or the book narrator)</option>
          ${st.voices.map((x) => html`<option value="${x.id}" ${v.override && v.id === x.id ? html`selected` : ""}>${x.name} (${x.kind})</option>`)}</select></label>
        <button class="btn small" type="submit">Apply</button></form></section>`;
  }

  function takes(i, d) {
    const byId = Object.fromEntries(d.attempts.map((a) => [a.id, a]));
    const busyState = i.status === "pending" || i.status === "synthesizing";
    return html`<section><h3>Generation history (${i.attempts.length})</h3>
      <div class="table-wrap"><table class="table compact takes"><thead><tr><th scope="col">Take</th><th scope="col">Matches current settings?</th><th scope="col">Instruction used</th>
        <th scope="col">WER</th><th scope="col">Audio</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead><tbody>
        ${i.attempts.map((a) => { const full = byId[a.id] || {}; return html`<tr class="${a.selected ? "selected-take" : ""}">
          <td>${a.round}.${a.attempt_no}${a.selected ? html` <span class="badge tone-good">selected</span>` : ""}<div class="small muted">${a.status}${a.passed ? " ✓" : ""} · ${a.duration_s ? fmtDuration(a.duration_s) : "–"}</div></td>
          <td>${a.gen_key ? html`<span class="badge tone-${a.current ? "good" : "warn"}">${a.current ? "current" : "outdated"}</span>` : html`<span class="muted small">legacy take</span>`}
            ${a.asr_skipped ? html` <span class="badge tone-neutral" title="Whisper verification was skipped for this take by the verification setting">ASR skipped</span>` : ""}</td>
          <td class="small">${a.instruct_used ? html`<code>${a.instruct_used}</code>` : html`<span class="muted">none</span>`}
            ${a.delivery_applied === 0 && a.instruct_used === null ? "" : ""}</td>
          <td>${a.wer === null || a.wer === undefined ? "–" : (a.wer * 100).toFixed(1) + "%"}</td>
          <td>${full.playable ? player(`/media/attempt/${a.id}`, `Take ${a.round}.${a.attempt_no}`, "mini") : html`<span class="muted small">–</span>`}</td>
          <td>${!a.selected && full.playable && full.fingerprint_ok && a.status === "done" && !busyState && (a.current || !a.gen_key) ? html`<button class="btn small" data-act="select" data-attempt="${a.id}">Use this take</button>` : ""}</td></tr>`; })}
      </tbody></table></div><p class="small muted">Play two takes one after the other to compare them. A take marked “outdated” was made with a different voice, delivery or text than this segment uses now.</p></section>`;
  }

  function previewPanel() {
    const p = st.preview;
    return html`<section><h3>Preview</h3>
      <p class="small muted">Renders this segment once with its own voice and delivery. Nothing is added to its takes.</p>
      <button class="btn" data-act="preview" ${p && p.pending ? html`disabled` : ""}>Preview this segment</button>
      ${p && p.pending ? html` <span class="small muted"><span class="spinner" aria-hidden="true"></span> job #${p.job} on the worker…</span>` : ""}
      ${p && p.error ? html`<div class="small err">${p.error}</div>` : ""}
      ${p && p.result ? html`<div class="preview-out">${player(`/media/preview/${p.job}`, "Segment preview")}
        <div class="small muted">${p.result.engine} · ${p.result.duration_s}s ${p.result.note ? html`· <strong>${p.result.note}</strong>` : ""}
          <br>Instruction: ${p.result.instruct ? html`<code>${p.result.instruct}</code>` : "none"} (${p.result.delivery_applied ? "applied" : p.result.delivery_reason})</div></div>` : ""}</section>`;
  }

  function paint() {
    const i = st.ins, d = st.det, s = d.segment;
    const busyState = i.status === "pending" || i.status === "synthesizing";
    setHTML(mainEl, html`<div class="page-head"><h1>Segment <code>${i.sid}</code></h1>
      <div class="head-actions"><a class="btn" href="#/review/${st.bookId}?seg=${sid}">Review view</a><a class="btn" href="#/narrative/${st.bookId}">Narrative page</a><button type="button" class="btn" data-act="ab-jump">Blind A/B</button>
        <a class="btn ghost ${d.prev_id ? "" : "disabled"}" ${d.prev_id ? html`href="#/inspect/${d.prev_id}"` : ""} aria-label="Previous segment">← Prev</a>
        <a class="btn ghost ${d.next_id ? "" : "disabled"}" ${d.next_id ? html`href="#/inspect/${d.next_id}"` : ""} aria-label="Next segment">Next →</a></div></div>
    <section class="card">
      <div class="seg-badges">${badge(i.status)} <span class="badge tone-neutral">chapter ${i.chapter.idx}: ${i.chapter.title}</span> <span class="badge tone-neutral">scene ${i.scene + 1}</span>
        <span class="badge tone-neutral">${i.span_kind}</span>
        ${i.needs_review ? html`<span class="badge tone-warn">speaker needs review</span>` : ""}${i.boundary_manual ? html`<span class="badge tone-info">manual boundary</span>` : ""}
        <span class="badge tone-neutral">speaker: ${i.speaker.name || "not established"}</span></div>
      ${i.review_reason ? html`<div class="callout warn small"><strong>Why review:</strong> ${i.review_reason}</div>` : ""}
      ${!i.semantic ? html`<div class="callout info small">This segment was cut by the classic segmenter: it has no source offsets, speaker analysis or boundary editing.
        <a href="#/narrative/${st.bookId}">Convert the book</a> to use them.</div>` : ""}
      ${s.flag_reason ? html`<div class="callout warn small"><strong>QA flag:</strong> ${s.flag_reason}</div>` : ""}

      <h3>Manuscript text <span class="small muted">(immutable)</span></h3>
      ${i.context ? html`<div class="ctx-block ctx-before" aria-label="Context before (analysis only)"><div class="ctx-label">Context before — analysis only, never spoken ${i.context.chapter_start ? "· chapter start" : ""}${i.context.scene_break_before ? " · includes a scene break" : ""}</div>
        <p>${i.context.before || "—"}</p></div>` : ""}
      <div class="seg-own">${textBlock(i)}</div>
      ${i.context ? html`<div class="ctx-block ctx-after" aria-label="Context after (analysis only)"><div class="ctx-label">Context after — analysis only, never spoken ${i.context.chapter_end ? "· chapter end" : ""}${i.context.scene_break_after ? " · includes a scene break" : ""}</div>
        <p>${i.context.after || "—"}</p></div>
        <p class="small muted">Context windows: ${i.settings.context_before_chars} characters before and ${i.settings.context_after_chars} after (Settings → Narrative). Shown here: ${i.context.before_chars} / ${i.context.after_chars}.</p>` : ""}

      ${i.semantic ? html`<div class="form-actions boundary-actions" role="group" aria-label="Segment boundaries">
        <button class="btn small ${st.mode === "split" ? "primary" : ""}" data-act="mode" data-mode="split" aria-pressed="${st.mode === "split"}">Split at a word…</button>
        <button class="btn small ${st.mode === "move_end" ? "primary" : ""}" data-act="mode" data-mode="move_end" aria-pressed="${st.mode === "move_end"}">Move the end earlier…</button>
        <button class="btn small" data-act="merge">Merge with next segment</button>
        ${st.mode ? html`<span class="small">Click the first word of the <strong>${st.mode === "split" ? "new second segment" : "text that moves to the next segment"}</strong>.</span>` : ""}</div>
        <p class="small muted">Only the segments whose text range changes are regenerated; neighbours keep their audio and their directions.</p>` : ""}
    </section>
    <div class="grid-2">
      <section class="card">${spansTable(i)}${deliveryPanel(i)}</section>
      <section class="card">${directionPanel(i)}</section></div>
    <section class="card">
      <div class="action-bar">
        <button class="btn primary" data-act="approve" ${i.status === "passed" || i.status === "flagged" ? "" : html`disabled`}>Approve</button>
        <button class="btn" data-act="regenerate" ${busyState ? html`disabled` : ""}>Regenerate</button>
        ${busyState ? html`<span class="small muted"><span class="spinner" aria-hidden="true"></span> queued for generation</span>` : ""}
        <a class="btn ghost" href="#/review/${st.bookId}?seg=${sid}">Edit spoken text &amp; notes in the review view</a></div>
      ${previewPanel()}
      ${takes(i, d)}
      ${i.history.length ? html`<details><summary>Direction history (${i.history.length})</summary><ol class="feed small">${i.history.map((h) => html`<li>${fmtDate(h.created_at)} · <strong>${h.source}</strong> by ${h.actor}
        ${h.payload && h.payload.final ? html`→ ${h.payload.final.emotion} ${h.payload.final.intensity}/5, ${h.payload.final.pacing}` : ""}${h.payload && h.payload.note ? html` <span class="muted">(${h.payload.note})</span>` : ""}</li>`)}</ol></details>` : ""}
    </section>`);
  }

  // ------------------------------------------------------------ blind A/B listening
  const abCmp = () => ab.comparisons.find((c) => c.id === ab.cmp) || null;

  function tallyBlock(t) {
    if (!t || !t.votes) return html`<p class="small muted">No votes yet for this comparison in this book.</p>`;
    return html`<p class="small">Votes so far in this book: <strong>${t.votes}</strong>. “${t.a_label}” won ${t.a_wins}, “${t.b_label}” won ${t.b_wins}, no real difference ${t.same}.
      ${t.a_share === null ? "" : html`“${t.a_label}” has ${Math.round(t.a_share * 100)}% of the ${t.non_tie} non-tie votes.`}
      “Sounds like a different narrator” was flagged ${t.other_narrator_flags} time(s).</p>`;
  }

  function paintAB() {
    const r = ab.round, c = abCmp();
    const voted = !!(r && r.voted), ready = !!(r && r.status === "ready" && !voted), failed = !!(r && r.status === "failed");
    const pending = ab.creating || !!(r && r.status === "pending");
    const lock = pending || ready;
    const clips = (r && r.sides ? r.sides : []).filter((s) => s.media_url);
    setHTML(abEl, html`<h2 id="ab-h" tabindex="-1">Compare settings (blind A/B)</h2>
      <p class="small muted">Renders this segment twice with two different settings and the same random seed, so only the settings differ. You hear two clips, X and Y, without being told which is which.
        Vote, then the answer is revealed. Nothing here changes the book's takes.</p>
      <div class="inline-form"><label>Comparison
        <select data-ab="cmp" ${lock ? html`disabled` : ""}>${ab.comparisons.map((x) => html`<option value="${x.id}" ${x.id === ab.cmp ? html`selected` : ""}>${x.title}</option>`)}</select></label>
        <button type="button" class="btn" data-act="ab-create" ${lock ? html`disabled` : ""}>Create A/B pair</button>
        <button type="button" class="btn ghost" data-act="ab-pick" ${lock ? html`disabled` : ""}>Pick a segment where they differ</button></div>
      ${c ? html`<p class="small muted">The two settings: <strong>${c.a_label}</strong> and <strong>${c.b_label}</strong>. Applies to: ${c.applies}.</p>` : ""}
      <div id="ab-live" role="status" aria-live="polite" class="small">${pending ? html`<span class="spinner" aria-hidden="true"></span> Rendering the two clips on the worker…` : ""}
        ${failed ? html`<span class="err">${r.error || "A clip failed to render."}</span>` : ""}${ab.error ? html`<span class="err">${ab.error}</span>` : ""}</div>
      ${r && r.mock && (ready || voted) ? html`<div class="callout warn small"><strong>MOCK ENGINE: tones, not speech.</strong> These clips are synthetic test tones, so a vote here says nothing about real narration.</div>` : ""}
      ${ready || voted ? html`<div class="two-col">${clips.map((s) => html`<div><h3>Clip ${s.side.toUpperCase()}${voted && r.revealed ? html` <span class="badge tone-info">${r.revealed[s.side]}</span>` : ""}</h3>
        ${player(s.media_url, `Clip ${s.side.toUpperCase()}`)}</div>`)}</div>` : ""}
      ${ready ? html`<fieldset class="ab-choice"><legend>Which clip sounds better?</legend>
          <label class="check"><input type="radio" name="ab-choice" value="x" ${ab.choice === "x" ? html`checked` : ""}> X sounds better</label>
          <label class="check"><input type="radio" name="ab-choice" value="y" ${ab.choice === "y" ? html`checked` : ""}> Y sounds better</label>
          <label class="check"><input type="radio" name="ab-choice" value="same" ${ab.choice === "same" ? html`checked` : ""}> No real difference</label></fieldset>
        <label class="check"><input type="checkbox" name="ab-other" ${ab.other ? html`checked` : ""}> Sounds like a different narrator</label>
        <div class="form-actions"><button type="button" class="btn primary" data-act="ab-vote" ${ab.voting ? html`disabled` : ""}>Submit vote</button>
          <button type="button" class="btn ghost" data-act="ab-new">Discard this pair</button></div>` : ""}
      ${voted ? html`<div class="callout good small"><strong>Vote recorded.</strong> Clip X was “${r.revealed.x}” and clip Y was “${r.revealed.y}”.
          You chose ${r.vote.winner === "same" ? "“no real difference”" : html`“${r.revealed[r.vote.choice]}”`}${r.vote.other_narrator ? " and flagged “sounds like a different narrator”" : ""}.</div>
        <div class="form-actions"><button type="button" class="btn" data-act="ab-new">Start another pair</button></div>` : ""}
      ${failed ? html`<div class="form-actions"><button type="button" class="btn" data-act="ab-new">Close</button></div>` : ""}
      ${tallyBlock(ab.tally && ab.tally[ab.cmp])}`);
  }

  function abReset() { ab.rid = null; ab.round = null; ab.choice = ""; ab.other = false; ab.error = ""; ab.voting = false; }

  async function abPoll(rid) {
    for (let n = 0; n < 400 && !ab.dead && ab.rid === rid; n++) {
      let r;
      try { r = await get(`/api/ab/${rid}`); } catch (e) { if (ab.rid === rid) { ab.error = e.message || "Could not read the A/B round."; ab.round = null; ab.rid = null; paintAB(); } return; }
      if (ab.dead || ab.rid !== rid) return;
      ab.round = r; ab.cmp = r.comparison || ab.cmp;
      if (r.status !== "pending") { paintAB(); return; }
      if (n === 0) paintAB();
      await new Promise((res) => setTimeout(res, 1500));
    }
  }

  async function abAct(name) {
    if (name === "ab-jump") { abEl.scrollIntoView({ block: "start" }); abEl.querySelector("h2").focus(); return; }
    if (name === "ab-new") { abReset(); paintAB(); return; }
    if (name === "ab-pick") {
      try {
        const r = await get(`/api/books/${st.bookId}/ab/random` + qs({ comparison: ab.cmp, exclude: sid }));
        toast(`Opening segment ${r.sid}, where the two settings sound different.`, "info");
        navigate(`#/inspect/${r.segment_id}`);
      } catch (e) { ab.error = e.message || "No segment differs for this comparison."; toastError(e); paintAB(); }
      return;
    }
    if (name === "ab-create") {
      abReset(); ab.creating = true; paintAB();
      try {
        const r = await post(`/api/segments/${sid}/ab`, { comparison: ab.cmp });
        ab.rid = r.round_id; ab.round = { status: "pending" };
      } catch (e) { ab.error = e.message || "Could not create the pair."; toastError(e); }
      ab.creating = false; paintAB();
      if (ab.rid) abPoll(ab.rid);
      return;
    }
    if (name === "ab-vote") {
      if (!ab.choice) { ab.error = "Choose X, Y or “No real difference” first."; paintAB(); const f = abEl.querySelector("input[name=ab-choice]"); if (f) f.focus(); return; }
      ab.error = ""; ab.voting = true;
      try {
        const v = await post(`/api/ab/${ab.rid}/vote`, { choice: ab.choice, other_narrator: ab.other });
        if (!ab.tally) ab.tally = {};
        ab.tally[v.comparison] = v.tally;
        ab.round = await get(`/api/ab/${ab.rid}`);
        toast("Vote recorded", "success");
      } catch (e) { ab.error = e.message || "Could not record the vote."; toastError(e); }
      ab.voting = false; paintAB();
    }
  }

  function onChange(e) {
    const t = e.target;
    if (!abEl.contains(t)) return;
    if (t.name === "ab-choice") ab.choice = t.value;
    else if (t.name === "ab-other") ab.other = t.checked;
    else if (t.dataset && t.dataset.ab === "cmp") { ab.cmp = t.value; paintAB(); const s = abEl.querySelector("[data-ab=cmp]"); if (s) s.focus(); }
  }

  async function reload() { await load(); paint(); }

  async function goToOffset(chapterId, off) {
    let after = null;
    for (let n = 0; n < 20; n++) {
      const r = await get(`/api/books/${st.bookId}/segments` + qs({ chapter_id: chapterId, limit: 200, after }));
      const hit = r.items.find((x) => x.src_start !== null && x.src_start <= off && off < x.src_end) || null;
      if (hit) { navigate(`#/inspect/${hit.id}`); return; }
      if (!r.next) break;
      after = r.next;
    }
    navigate(`#/review/${st.bookId}`);
  }

  async function patchNarr(body, msg) {
    const r = await busy(null, () => patch(`/api/segments/${sid}/narrative`, body), msg);
    if (r.regenerating) toast("Only this segment was reset and queued for regeneration.", "info", 6000);
    await reload();
  }

  async function act(name, t) {
    if (name.startsWith("ab-")) return abAct(name);
    const i = st.ins;
    if (name === "mode") { st.mode = st.mode === t.dataset.mode ? null : t.dataset.mode; paint(); return; }
    if (name === "pick") {
      const off = Number(t.dataset.off), op = st.mode; st.mode = null;
      const label = op === "split" ? "Split here?" : "Move the end of this segment here?";
      if (!(await confirmDialog(label, "This changes only this segment's boundary. The affected segments are re-cut and queued for regeneration; the rest of the book is untouched.", "Apply"))) { paint(); return; }
      const r = await busy(t, () => post(`/api/segments/${sid}/boundary`, { op, offset: off }));
      toast(`Re-cut: ${r.kept} unchanged, ${r.created} new, ${r.retired} retired.`, "success", 7000);
      await goToOffset(i.chapter.id, off);
      return;
    }
    if (name === "merge") {
      if (!(await confirmDialog("Merge with the next segment?", "The two segments become one (up to 1200 characters). Their audio is regenerated as one segment.", "Merge"))) return;
      const r = await busy(t, () => post(`/api/segments/${sid}/boundary`, { op: "merge_next" }));
      toast(`Merged: ${r.created} new, ${r.retired} retired.`, "success", 7000);
      await goToOffset(i.chapter.id, i.src_start);
      return;
    }
    if (name === "assign") { await patchNarr({ character_id: Number(t.dataset.char) }, "Speaker assigned and locked"); return; }
    if (name === "clear-dir") { await patchNarr({ emotion: null, intensity: null, tension: null, pacing: null, delivery: null }, "Overrides cleared"); return; }
    if (name === "redirect") { await busy(t, () => post(`/api/segments/${sid}/redirect`), "Director re-run queued (runs on the worker)"); return; }
    if (name === "approve") { await busy(t, () => post(`/api/segments/${sid}/approve`, { note: null }), "Segment approved"); await reload(); return; }
    if (name === "regenerate") { await busy(t, () => post(`/api/segments/${sid}/regenerate`, { note: null }), "Queued for regeneration"); await reload(); return; }
    if (name === "select") { await busy(t, () => post(`/api/segments/${sid}/select-take`, { attempt_id: Number(t.dataset.attempt) }), "Take selected"); await reload(); return; }
    if (name === "preview") {
      const job = await busy(t, () => post(`/api/segments/${sid}/preview`, {}));
      st.preview = { job: job.id, pending: true }; paint();
      for (let n = 0; n < 180 && st.preview && st.preview.job === job.id; n++) {
        await new Promise((r) => setTimeout(r, 1000));
        if (!view.isConnected || !view.querySelector(".preview-out, [data-act=preview]")) return;
        const j = await get(`/api/jobs/${job.id}`);
        if (j.status === "completed") { st.preview = { job: job.id, result: j.result }; paint(); return; }
        if (["failed", "cancelled"].includes(j.status)) { st.preview = { job: job.id, error: j.error || j.status }; paint(); return; }
      }
    }
  }

  async function onClick(e) {
    const t = e.target.closest("[data-act]"); if (!t || !view.contains(t)) return;
    try { await act(t.dataset.act, t); } catch { /* toast shown */ }
  }
  async function onSubmit(e) {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      if (e.target.id === "dir-form") {
        const body = {};
        ["emotion", "pacing", "delivery"].forEach((k) => { body[k] = fd.get(k) || null; });
        ["intensity", "tension"].forEach((k) => { body[k] = fd.get(k) ? Number(fd.get(k)) : null; });
        await patchNarr(body, "Override saved");
      } else if (e.target.id === "voice-form") {
        await patchNarr({ voice_id: fd.get("voice_id") ? Number(fd.get("voice_id")) : null }, "Voice profile updated");
      } else if (e.target.id === "assign-form") {
        await patchNarr({ character_id: Number(fd.get("character_id")) }, "Speaker assigned and locked");
      }
    } catch { /* toast shown */ }
  }

  await load();
  paint();
  try { const c = await get(`/api/books/${st.bookId}/ab`); ab.comparisons = c.comparisons; ab.tally = c.tally; } catch { /* the card then offers no comparisons */ }
  const resume = query && query.get ? query.get("ab") : null;
  if (resume && /^[0-9a-f]{32}$/.test(resume)) { ab.rid = resume; ab.round = { status: "pending" }; }
  paintAB();
  if (ab.rid) abPoll(ab.rid);
  view.addEventListener("click", onClick);
  view.addEventListener("change", onChange);
  view.addEventListener("submit", onSubmit);
  const off = onState(async () => {
    const ae = document.activeElement;
    if (ae && view.contains(ae) && ["input", "textarea", "select"].includes(ae.tagName.toLowerCase())) return;
    if ([...view.querySelectorAll("audio")].some((x) => !x.paused) || st.mode || (st.preview && st.preview.pending)) return;
    try { await reload(); } catch { /* transient */ }
  }, 3000);
  return () => { ab.dead = true; st.preview = null; off(); view.removeEventListener("click", onClick); view.removeEventListener("change", onChange); view.removeEventListener("submit", onSubmit); };
}
