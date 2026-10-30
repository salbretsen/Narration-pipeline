// Audio review / production QA workspace with paged segment navigation and keyboard shortcuts.
//
// Rendering model (keeps the page usable while jobs run and the live updates arrive):
//  - the page frame and the segment shell are painted once; read-only parts (status, players, findings, takes, history, the "what reaches
//    the voice" explanation, the segment list) are regions repainted by patchRegions ONLY when their markup changed;
//  - the editable fields (spoken text, note, direction form) are never rebuilt by a refresh: a field the user touched carries data-dirty
//    and keeps its value, an untouched one follows the server.
// The book is read from the light GET /api/books/{id}?view=review (keys used: id, title, display_title, flagged, chapters[{id, index, title,
// status_counts}]); a server that does not know the view answers with the full book, which carries the same keys.
import { get, post, patch, html, setHTML, patchRegions, badge, fmtClock, fmtDate, toast, busy, debounce, qs, onState, player, navigate, progressBar, sameState } from "../ui.js";

const FILTERS = [["issues", "Issues (flags + warnings)"], ["flagged", "Flagged only"], ["all", "All segments"], ["generated", "Generated"],
  ["passed", "QA passed"], ["approved", "Approved"], ["pending", "Not rendered yet"], ["overridden", "Spoken text edited"], ["noted", "With notes"],
  ["speaker_review", "Speaker needs review"], ["direction_review", "Direction needs review / stale"]];

const FALLBACK_VOCAB = { emotions: ["neutral", "calm", "joy", "excitement", "tenderness", "sadness", "grief", "fear", "anxiety", "anger", "surprise", "disgust", "determination", "humor", "resignation"],
  pacings: ["slow", "measured", "normal", "brisk", "fast"] };
let vocabCache = null;
async function loadVocab(opts) {
  if (vocabCache) return vocabCache;
  try { vocabCache = await get("/api/narrative/vocabulary", opts); return vocabCache; } catch { return FALLBACK_VOCAB; } // not cached: the next visit asks again
}

const DIRECTION_FIELDS = ["rd-emotion", "rd-intensity", "rd-pacing", "rd-delivery"];
const EDIT_FIELDS = new Set(["override-text", "note-text", ...DIRECTION_FIELDS]);

function diffView(diff) {
  if (!diff || !diff.length) return html`<span class="muted">No comparison available.</span>`;
  return html`${diff.map((o) => {
    if (o.op === "eq") return html`<span>${o.ref} </span>`;
    if (o.op === "sub") return html`<span class="d-sub" title="expected “${o.ref}”, heard “${o.hyp}”"><del>${o.ref}</del><ins>${o.hyp}</ins></span> `;
    if (o.op === "del") return html`<del class="d-del" title="missing in audio">${o.ref}</del> `;
    return html`<ins class="d-ins" title="extra in audio">${o.hyp}</ins> `;
  })}`;
}

function issueList(issues) {
  const shown = (issues || []).filter((i) => i.severity !== "info" || i.kind === "spelling" || i.kind === "asr_disabled");
  if (!shown.length) return html`<p class="good-text small">No issues detected by automatic QA.</p>`;
  return html`<ul class="issues">${shown.map((i) => html`<li class="sev-${i.severity}"><span class="badge tone-${i.severity === "error" ? "bad" : i.severity === "warning" ? "warn" : "neutral"}">${i.severity}</span>
    <span class="small muted">${i.source}</span> ${i.message}${i.at_s !== null && i.at_s !== undefined ? html` <span class="small muted">@ ${i.at_s}s</span>` : ""}</li>`)}</ul>`;
}

const isBusy = (s) => s.status === "pending" || s.status === "synthesizing";
const latestOf = (d) => d.attempts.find((a) => a.selected) || d.attempts[0];

// ---- read-only regions of the segment detail --------------------------------------------------------------------------------

function headTpl(d) {
  const s = d.segment;
  return html`<div class="seg-head">
      <div><h2 class="seg-title" id="seg-title"><code>${s.sid}</code></h2>
        <div class="small muted">Chapter ${d.chapter.idx}: ${d.chapter.title} · segment ${s.idx} · ${s.kind} · <a href="#/inspect/${s.id}">Inspect speaker, context &amp; direction</a>${d.chapter_timing ? html` · ${fmtClock(d.chapter_timing.start_s)} in chapter master` : ""}</div></div>
      <div class="seg-badges">${badge(s.status)} <span class="badge tone-neutral">attempts ${s.attempts_used}/${d.max_attempts} (round ${s.round})</span></div>
      <div class="btn-group" role="group" aria-label="Segment navigation">
        <button class="btn small ghost" data-act="prev" ${d.prev_id ? "" : html`disabled`} aria-label="Previous segment (K)">← Prev</button>
        <button class="btn small ghost" data-act="next" ${d.next_id ? "" : html`disabled`} aria-label="Next segment (J)">Next →</button></div>
    </div>
    ${s.flag_reason ? html`<div class="callout warn small"><strong>Why flagged:</strong> ${s.flag_reason}</div>` : ""}
    ${isBusy(s) ? html`<div class="callout info small" role="status"><span class="spinner" aria-hidden="true"></span> This segment is queued for (re)generation. The page updates automatically.</div>` : ""}`;
}

function playersTpl(d) {
  const sel = d.attempts.find((a) => a.selected);
  return html`${sel && sel.playable ? html`<div><div class="small muted">Selected take ${sel.round}.${sel.attempt_no}</div>${player(`/media/attempt/${sel.id}`, "Selected take", "main-take")}</div>` : html`<p class="muted">No playable take yet.</p>`}
      ${d.chapter_timing ? html`<div><div class="small muted">In context (chapter master)</div>
        <audio class="player ctx-player" controls preload="none" src="/media/chapter/${d.chapter.id}.mp3" aria-label="Chapter master"></audio>
        <button class="btn small ghost" data-act="ctx" data-t="${d.chapter_timing.start_s}">Play from this segment</button></div>` : ""}`;
}

const spokenBadgeTpl = (d) => (d.segment.override_text ? html`<span class="badge tone-info">edited</span>` : html`<span class="small muted">(auto-normalised)</span>`);
const clearOverrideTpl = (d) => (d.segment.override_text ? html`<button class="btn small ghost" data-act="clear-override">Revert to automatic</button>` : "");

function asrTpl(d) {
  const latest = latestOf(d);
  return html`<h3>Recognised by Whisper ${latest && latest.wer !== null && latest.wer !== undefined ? html`<span class="badge tone-${latest.wer > 0.12 ? "warn" : "good"}">WER ${(latest.wer * 100).toFixed(1)}%</span>` : ""}</h3>
        <p class="asr-text">${latest && latest.asr_text !== null ? latest.asr_text : html`<span class="muted">–</span>`}</p>
        <div class="diff small" aria-label="Word comparison">${latest ? diffView(latest.diff) : ""}</div>
        <p class="small muted">Legend: <del class="d-del">missing</del> <ins class="d-ins">extra</ins> <span class="d-sub"><del>expected</del><ins>heard</ins></span>. Agreement with Whisper is not proof of correct pronunciation.</p>`;
}

function findingsTpl(d) {
  const latest = latestOf(d);
  if (!latest) return "";
  return html`<h3>Findings for take ${latest.round}.${latest.attempt_no}</h3>${issueList(latest.issues)}
      <div class="small muted">Acoustics: ${latest.acoustics && latest.acoustics.duration_s !== undefined ? html`${latest.acoustics.duration_s}s · speech ${latest.acoustics.rms_dbfs} dBFS · peak ${latest.acoustics.peak_dbfs} dBFS · longest pause ${latest.acoustics.longest_internal_silence_s}s${latest.acoustics.duration_ratio ? html` · duration ratio ${latest.acoustics.duration_ratio}` : ""}` : "–"}</div>`;
}

function actionButtonsTpl(d) {
  const s = d.segment, sel = d.attempts.find((a) => a.selected), b = isBusy(s);
  return html`<button class="btn primary" data-act="approve" ${sel && !b && s.status !== "approved" ? "" : html`disabled`}>Approve (A)</button>
      <button class="btn" data-act="regenerate" ${b ? html`disabled` : ""}>Regenerate (R)</button>`;
}

function directionButtonsTpl(d, pv) {
  const stored = pv && pv.user_direction && Object.keys(pv.user_direction).length;
  return html`<button class="btn" type="button" data-act="regen-dir" ${isBusy(d.segment) ? html`disabled` : ""}>Regenerate with this direction</button>
      <button class="btn ghost" type="button" data-act="preview-dir">Preview what will reach the voice</button>
      ${stored ? html`<button class="btn ghost" type="button" data-act="clear-dir" title="Remove the emotion, intensity, pacing and delivery note you set for this segment">Clear direction</button>` : ""}`;
}

function takesTpl(d) {
  const busyState = isBusy(d.segment);
  return html`<h3>Takes &amp; attempt history (${d.attempts.length})</h3>
      <div class="table-wrap"><table class="table compact takes">
        <thead><tr><th scope="col">Take</th><th scope="col">Status</th><th scope="col">WER</th><th scope="col">Score</th><th scope="col">Issues</th><th scope="col">Audio</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
        <tbody>${d.attempts.map((a) => html`<tr class="${a.selected ? "selected-take" : ""}">
          <td>${a.round}.${a.attempt_no}${a.selected ? html` <span class="badge tone-good">selected</span>` : ""}<div class="small muted">${a.engine}${a.temperature ? ` · T=${a.temperature}` : ""} · seed ${a.seed}</div></td>
          <td>${a.status}${a.passed ? html` <span class="good-text">✓</span>` : ""}${a.error ? html`<div class="small err">${a.error}</div>` : ""}${!a.fingerprint_ok ? html`<div class="small warn-text">old narrator settings</div>` : ""}${a.instruct_used ? html`<div class="small muted" title="the instruction this take was given">told: ${a.instruct_used}</div>` : ""}</td>
          <td>${a.wer === null || a.wer === undefined ? "–" : (a.wer * 100).toFixed(1) + "%"}</td>
          <td>${a.score === null || a.score === undefined ? "–" : a.score.toFixed(1)}</td>
          <td class="small">${(a.issues || []).filter((i) => i.severity === "error").length} errors, ${(a.issues || []).filter((i) => i.severity === "warning").length} warnings</td>
          <td>${a.playable ? html`${player(`/media/attempt/${a.id}`, `Take ${a.round}.${a.attempt_no}`, "mini")} <a class="small" href="/media/attempt/${a.id}?download=1">WAV</a>` : html`<span class="muted small">–</span>`}</td>
          <td>${!a.selected && a.playable && a.fingerprint_ok && a.status === "done" && !busyState ? html`<button class="btn small" data-act="select" data-attempt="${a.id}">Use this take</button>` : ""}</td>
        </tr>`)}</tbody></table></div>`;
}

const directionSummary = (dir) => Object.entries(dir || {}).filter(([, v]) => v !== null && v !== undefined && v !== "").map(([k, v]) => `${k} ${v}`).join(", ") || "cleared";

function historyTpl(d) {
  if (!d.reviews.length) return "";
  return html`<h3>Review history</h3><ol class="feed small">${d.reviews.map((r) => html`<li>${fmtDate(r.created_at)} – <strong>${r.action}</strong> by ${r.actor}${r.detail && r.detail.note ? html`: ${r.detail.note}` : ""}${r.detail && r.detail.direction ? html` <span class="muted">(direction: ${directionSummary(r.detail.direction)})</span>` : ""}</li>`)}</ol>`;
}

/** The honest account of what reaches the voice. `p` is a delivery-preview payload (GET /api/segments/{id}/delivery-preview). */
function explainTpl(p, forForm, err) {
  if (err) return html`<h3>What reaches the voice</h3><p class="small err">${err}</p>`;
  if (!p) return html`<h3>What reaches the voice</h3><p class="small muted">The delivery preview is not available for this segment.</p>`;
  const lead = forForm ? "With the direction in the form:" : p.source === "you" ? "As set by you for this segment:" : p.source === "director" ? "As directed for this segment:" : "Currently:";
  const dir = p.direction;
  const dirText = dir ? [dir.emotion && dir.emotion !== "neutral" ? `${dir.emotion}${dir.intensity ? ` ${dir.intensity}/5` : ""}` : "", dir.pacing && dir.pacing !== "normal" ? dir.pacing : "", dir.delivery || ""].filter(Boolean).join(", ") : "";
  const reason = (p.reason || "").replace(/^direction set by you for this segment; ?/, "");
  const extra = (p.notes || []).filter((n) => n !== p.reason);
  let body;
  if (p.voice_kind === "clone") {
    body = html`<p>Emotion can steer a cloned voice only through an emotion reference clip (<a href="#/voices">Narrator Studio</a>) and pace.</p>
      <ul class="small">${(p.notes || []).map((n) => html`<li>${n}</li>`)}</ul>`;
  } else if (p.applied && p.instruct) {
    body = html`<p>Sent to the voice as: <q class="instruct-text">${p.instruct}</q></p>${extra.length ? html`<ul class="small">${extra.map((n) => html`<li>${n}</li>`)}</ul>` : ""}`;
  } else {
    body = html`<p>No written instruction reaches this voice${reason ? html` (${reason})` : ""}.</p>${extra.length ? html`<ul class="small">${extra.map((n) => html`<li>${n}</li>`)}</ul>` : ""}`;
  }
  return html`<h3>What reaches the voice <span class="badge tone-neutral">${p.voice_kind}${p.voice ? ` · ${p.voice.name}` : ""}</span></h3>
    <p class="small muted">${lead}${dirText ? html` ${dirText}.` : " no emotion direction."}</p>${body}
    <p class="small"><a href="#/voices">Open Narrator Studio</a></p>`;
}

// ---- the shell of one segment (painted once per selected segment) ---------------------------------------------------------------

function detailShell(d, pv, vocab) {
  const s = d.segment;
  const spoken = s.override_text || s.narration_text;
  const ud = (pv && pv.user_direction) || {};
  const opt = (v, cur, label) => html`<option value="${v}" ${String(cur) === String(v) ? html`selected` : ""}>${label || v}</option>`;
  return html`<div class="seg-detail" data-seg-id="${s.id}">
    <div id="rv-head"></div>
    <div class="players" id="rv-players"></div>
    <div class="tri">
      <section><h3>Original manuscript <span class="small muted">(immutable)</span></h3><p class="ms-text">${s.original_text}</p></section>
      <section><h3>Spoken rendition <span id="rv-spoken-badge" class="r-contents"></span></h3>
        <textarea id="override-text" rows="5" maxlength="2000" aria-label="Spoken rendition">${spoken}</textarea>
        <div class="form-actions"><button class="btn small" data-act="override">Save &amp; regenerate (E)</button><span id="rv-clear-override" class="r-contents"></span></div>
        <p class="small muted">Use this for pronunciations, e.g. “Siobhan” → “Shiv-awn”. The manuscript text is never changed.</p></section>
      <section id="rv-asr"></section>
    </div>
    <section id="rv-findings"></section>
    <div class="action-bar">
      <span id="rv-act-btns" class="r-contents"></span>
      <label class="note-field">Editor note <input id="note-text" maxlength="2000" value="${s.note || ""}" placeholder="Optional note for this segment"></label>
      <button class="btn ghost" data-act="note">Save note</button>
    </div>
    <div class="regen-wrap">
      <details id="regen-direction" class="regen-dir">
        <summary>Regenerate with direction…</summary>
        <p class="small muted">Tell the narrator how this line should sound, for example when the first take was spoken normally but in the book the character is shouting. The direction is kept for this segment until you clear it.</p>
        <div class="regen-form">
          <label for="rd-emotion">Emotion<select id="rd-emotion"><option value="">No change</option>${vocab.emotions.map((e) => opt(e, ud.emotion))}</select></label>
          <label for="rd-intensity">Intensity<select id="rd-intensity"><option value="">Default</option>${[1, 2, 3, 4, 5].map((n) => opt(n, ud.intensity, `${n}${n === 1 ? " (barely)" : n === 5 ? " (extreme)" : ""}`))}</select></label>
          <label for="rd-pacing">Pacing<select id="rd-pacing"><option value="">No change</option>${vocab.pacings.map((e) => opt(e, ud.pacing))}</select></label>
          <label for="rd-delivery" class="regen-wide">Delivery note (e.g. shouting, furious)<input id="rd-delivery" maxlength="240" value="${ud.delivery || ""}" placeholder="Optional, up to 240 characters"></label>
        </div>
        <div class="form-actions" id="rv-dir-btns"></div>
      </details>
      <div class="regen-explain" id="rv-explain" aria-live="polite"></div>
    </div>
    <section id="rv-takes"></section>
    <section id="rv-history"></section>
  </div>`;
}

async function bookPicker(view, opts) {
  const { books } = await get("/api/books", opts);
  setHTML(view, html`<div class="page-head"><h1>Review &amp; QA</h1></div>
    <section class="card"><h2>Choose a book to review</h2>
    ${books.length ? html`<ul class="book-pick">${books.map((b) => html`<li><a href="#/review/${b.id}?filter=${b.flagged ? "flagged" : "all"}"><strong>${b.display_title}</strong></a>
      ${badge(b.state)} <span class="small muted">${b.produced}/${b.segments} rendered · ${b.flagged} flagged · ${b.approved} approved</span>
      ${progressBar(b.progress, b.display_title)}</li>`)}</ul>` : html`<p class="muted">No books yet. <a href="#/library">Import a manuscript</a>.</p>`}</section>`);
}

export async function render(view, parts, query, nav) {
  const opts = { signal: nav && nav.signal };
  if (!parts[0]) { await bookPicker(view, opts); return null; }
  const bookId = Number(parts[0]);
  const wantSeg = query.get("seg") ? Number(query.get("seg")) : null;

  async function loadBook() {
    try { return await get(`/api/books/${bookId}?view=review`, opts); } catch (e) {
      if (e && [400, 404, 422].includes(e.status)) return get(`/api/books/${bookId}`, opts); // a server without the light view
      throw e;
    }
  }
  const st = { filter: query.get("filter") || "", chapter: query.get("chapter") || "", q: "", items: [], next: null, total: 0,
    selected: wantSeg, detail: null, preview: null, previewForm: false, previewErr: "", names: {}, vocab: FALLBACK_VOCAB, book: null };

  const listUrl = (append) => `/api/books/${bookId}/segments` + qs({ filter: st.filter, chapter_id: st.chapter, q: st.q, after: append ? st.next : "", limit: 60 });
  const detailP = (id) => (id ? get(`/api/segments/${id}`, opts) : Promise.resolve(null));
  const previewP = (id) => (id ? get(`/api/segments/${id}/delivery-preview`, opts).catch(() => null) : Promise.resolve(null));
  const takeList = (res, append) => { st.items = append ? st.items.concat(res.items) : res.items; st.next = res.next; st.total = res.total; };

  // ---- first paint: everything in parallel (the default filter needs the book only when the link did not say which one)
  let bookRes, charRes, listRes, detRes, pvRes;
  const common = [loadVocab(opts), get(`/api/books/${bookId}/characters`, opts).catch(() => null)];
  if (st.filter) {
    [st.vocab, charRes, bookRes, listRes, detRes, pvRes] = await Promise.all([...common, loadBook(), get(listUrl(false), opts), detailP(wantSeg), previewP(wantSeg)]);
  } else {
    [st.vocab, charRes, bookRes] = await Promise.all([...common, loadBook()]);
    st.filter = bookRes.flagged ? "flagged" : "all";
    [listRes, detRes, pvRes] = await Promise.all([get(listUrl(false), opts), detailP(wantSeg), previewP(wantSeg)]);
  }
  st.book = bookRes;
  ((charRes && charRes.characters) || []).forEach((c) => { st.names[c.id] = c.name; });
  takeList(listRes, false);
  if (!st.selected && st.items.length) {
    st.selected = st.items[0].id;
    [detRes, pvRes] = await Promise.all([detailP(st.selected), previewP(st.selected)]);
  }
  st.detail = detRes; st.preview = pvRes;
  if (st.selected) history.replaceState(null, "", `#/review/${bookId}` + qs({ seg: st.selected, filter: st.filter, chapter: st.chapter }));

  const $ = (sel) => view.querySelector(sel);

  function listItems() {
    return html`${st.items.map((s) => html`<li><button type="button" id="seg-${s.id}" class="seg-item ${s.id === st.selected ? "sel" : ""}" data-seg="${s.id}" aria-current="${s.id === st.selected ? "true" : "false"}">
      <span class="seg-item-top"><code>c${s.chapter_idx}·${s.idx}</code> ${badge(s.status)} ${s.wer !== null && s.wer !== undefined ? html`<span class="small muted">${(s.wer * 100).toFixed(0)}%</span>` : ""}
      ${s.needs_review ? html`<span class="badge tone-warn" title="speaker not established - open Inspect to assign">speaker?</span>` : ""}
      ${s.character_id && st.names[s.character_id] ? html`<span class="small muted">${st.names[s.character_id]}</span>` : ""}
      ${s.override_text ? html`<span class="badge tone-info" title="spoken text edited">✎</span>` : ""}${s.note ? html`<span class="badge tone-neutral" title="has note">✉</span>` : ""}</span>
      <span class="seg-snip">${s.original_text.slice(0, 110)}${s.original_text.length > 110 ? "…" : ""}</span></button></li>`)}`;
  }
  const moreButton = () => (st.next ? html`<button class="btn small" data-act="more">Load more</button>` : "");
  const chapterOptions = () => html`<option value="">All chapters</option>${(st.book.chapters || []).map((c) => html`<option value="${c.id}" ${String(c.id) === String(st.chapter) ? html`selected` : ""}>
            ${c.index}. ${c.title} (${((c.status_counts || {}).flagged || 0)} flagged)</option>`)}`;

  function paintFrame() {
    const b = st.book;
    setHTML(view, html`<div class="page-head"><h1>Review: ${b.display_title || b.title}</h1>
      <div class="head-actions"><a class="btn" href="#/book/${bookId}">Book, exports &amp; release</a>
        <button class="btn ghost" type="button" data-act="help" aria-expanded="false">Keyboard shortcuts</button></div></div>
      <div class="kbd-help card" id="kbd-help" hidden><strong>Shortcuts:</strong> <kbd>J</kbd>/<kbd>K</kbd> next/previous · <kbd>A</kbd> approve · <kbd>R</kbd> regenerate (without a direction) ·
        <kbd>E</kbd> edit spoken text · <kbd>P</kbd> play/pause selected take · <kbd>/</kbd> search</div>
      <div class="review-layout">
        <aside class="card review-side" aria-label="Segments">
          <label>Chapter <select id="f-chapter">${chapterOptions()}</select></label>
          <label>Show <select id="f-filter">${FILTERS.map(([k, l]) => html`<option value="${k}" ${k === st.filter ? html`selected` : ""}>${l}</option>`)}</select></label>
          <label>Search text or ID <input id="f-q" type="search" value="${st.q}" placeholder="word or sid"></label>
          <p class="small muted" id="rv-count" aria-live="polite">${st.total.toLocaleString()} segment(s)</p>
          <ul class="seg-list" id="seg-list">${listItems()}</ul>
          <div id="seg-more">${moreButton()}</div>
        </aside>
        <section class="card review-main" aria-label="Segment detail"></section>
      </div>`);
    $("#f-chapter").addEventListener("change", async (e) => { st.chapter = e.target.value; await loadList(); repaintList(); });
    $("#f-filter").addEventListener("change", async (e) => { st.filter = e.target.value; await loadList(); repaintList(); });
    $("#f-q").addEventListener("input", debounce(async (e) => { st.q = e.target.value.trim(); await loadList(); repaintList(); }, 350));
    paintDetail();
  }

  async function loadList(append = false) { takeList(await get(listUrl(append)), append); }
  /** Re-fetch what the reader has loaded already: one request when it fits the server's page cap of 200, so "Load more" pages survive a refresh. */
  async function reloadList() {
    const want = st.items.length;
    if (want <= 60) { await loadList(); return; }
    takeList(await get(`/api/books/${bookId}/segments` + qs({ filter: st.filter, chapter_id: st.chapter, q: st.q, limit: Math.min(200, want) })), false);
    while (st.next && st.items.length < want) await loadList(true);
  }

  /** Repaint the segment list in place. `reveal` scrolls the selected segment into view: only for an explicit pick (click, J/K, deep link),
   *  never for a live refresh, or a reader who scrolled away from the selected segment is pulled back to it on every update. */
  function repaintList(reveal = false) {
    const sc = ($("#seg-list") || {}).scrollTop || 0;
    patchRegions(view, { "#seg-list": () => listItems(), "#seg-more": () => moreButton(), "#f-chapter": () => chapterOptions() });
    const cnt = $("#rv-count");
    if (cnt) cnt.textContent = `${st.total.toLocaleString()} segment(s)`;
    const list = $("#seg-list");
    if (list && sc) list.scrollTop = sc;
    const sel = $(".seg-item.sel");
    if (sel && reveal) sel.scrollIntoView({ block: "nearest" });
  }

  // ---- detail
  const directionDirty = () => DIRECTION_FIELDS.some((id) => { const el = $("#" + id); return el && el.dataset.dirty; });
  function regions() {
    const d = st.detail, pv = st.preview;
    return {
      "#rv-head": () => headTpl(d), "#rv-players": () => playersTpl(d), "#rv-spoken-badge": () => spokenBadgeTpl(d), "#rv-clear-override": () => clearOverrideTpl(d),
      "#rv-asr": () => asrTpl(d), "#rv-findings": () => findingsTpl(d), "#rv-act-btns": () => actionButtonsTpl(d), "#rv-dir-btns": () => directionButtonsTpl(d, pv),
      "#rv-explain": () => explainTpl(pv, st.previewForm, st.previewErr), "#rv-takes": () => takesTpl(d), "#rv-history": () => historyTpl(d),
    };
  }
  function paintDetail() {
    const main = $(".review-main");
    if (!main) return;
    if (!st.detail) { setHTML(main, html`<p class="muted">Select a segment to review it.</p>`); return; }
    setHTML(main, detailShell(st.detail, st.preview, st.vocab));
    patchRegions(main, regions());
  }
  function syncField(id, value) {
    const el = $("#" + id);
    if (!el || el.dataset.dirty || document.activeElement === el) return; // never overwrite what the reader is typing
    const v = value === null || value === undefined ? "" : String(value);
    if (el.value !== v) el.value = v;
  }
  /** Refresh a segment that is already on screen: repaint changed read-only regions, and let untouched fields follow the server. */
  function repaintDetail() {
    const main = $(".review-main");
    const shell = main && main.querySelector(".seg-detail");
    if (!st.detail || !shell || Number(shell.dataset.segId) !== st.detail.segment.id) { paintDetail(); return; }
    patchRegions(main, regions());
    const s = st.detail.segment, ud = (st.preview && st.preview.user_direction) || {};
    syncField("override-text", s.override_text || s.narration_text);
    syncField("note-text", s.note || "");
    if (!st.previewForm) { syncField("rd-emotion", ud.emotion); syncField("rd-intensity", ud.intensity); syncField("rd-pacing", ud.pacing); syncField("rd-delivery", ud.delivery); }
  }
  function repaintExplain() {
    const main = $(".review-main");
    if (!main || !st.detail) return;
    const r = regions();
    patchRegions(main, { "#rv-explain": r["#rv-explain"], "#rv-dir-btns": r["#rv-dir-btns"] });
  }

  function formDirection() {
    const v = (id) => ($("#" + id) || {}).value || "";
    return { emotion: v("rd-emotion") || null, intensity: v("rd-intensity") ? Number(v("rd-intensity")) : null, pacing: v("rd-pacing") || null, delivery: v("rd-delivery").trim() || null };
  }
  let previewSeq = 0;
  /** Ask the server what the form's direction would send to this segment's voice (reads only; nothing is saved). */
  async function formPreview() {
    const id = st.selected;
    if (!id) return;
    const seq = ++previewSeq;
    const p = new URLSearchParams();
    Object.entries(formDirection()).forEach(([k, v]) => p.set(k, v === null ? "" : v));
    try {
      const pv = await get(`/api/segments/${id}/delivery-preview?${p}`);
      if (seq !== previewSeq || id !== st.selected) return;
      st.preview = { ...pv, user_direction: (st.preview && st.preview.user_direction) || pv.user_direction }; st.previewForm = true; st.previewErr = "";
    } catch (e) {
      if (seq !== previewSeq || id !== st.selected) return;
      st.previewErr = `Could not check: ${e.message}`;
    }
    repaintExplain();
  }
  const autoPreview = debounce(() => { formPreview().catch(() => {}); }, 350);
  const clearDirty = (ids) => ids.forEach((id) => { const el = $("#" + id); if (el) delete el.dataset.dirty; });

  async function select(id) {
    st.selected = id;
    const [det, pv] = await Promise.all([detailP(id), previewP(id)]);
    if (st.selected !== id) return; // a newer click won
    st.detail = det; st.preview = pv; st.previewForm = false; st.previewErr = "";
    history.replaceState(null, "", `#/review/${bookId}` + qs({ seg: id, filter: st.filter, chapter: st.chapter }));
    paintDetail(); repaintList(true);
    const h = $(".seg-title"); if (h) { h.setAttribute("tabindex", "-1"); h.focus({ preventScroll: true }); }
  }
  async function step(dir) {
    const idx = st.items.findIndex((s) => s.id === st.selected);
    let target = st.items[idx + dir];
    if (!target && dir > 0 && st.next) { await loadList(true); target = st.items[idx + 1]; }
    if (!target && st.detail) { const id = dir > 0 ? st.detail.next_id : st.detail.prev_id; if (id) return select(id); }
    if (target) await select(target.id);
  }
  /** Refresh in place (also used by the live progress updates while a job runs): never rebuilds the page, so scrolling and typing are undisturbed. */
  async function refreshAll(forcePreview = false) {
    const sel = st.selected;
    const [det, b] = await Promise.all([detailP(sel), loadBook().catch(() => null), reloadList()]);
    if (b) st.book = b;
    if (sel !== st.selected) { repaintList(); return; } // the reader moved to another segment meanwhile
    const changed = !sameState(det, st.detail);
    st.detail = det;
    if (changed || forcePreview || !st.preview) {
      const pv = await previewP(sel);
      if (sel !== st.selected) { repaintList(); return; }
      if (!st.previewForm || !directionDirty()) { st.preview = pv; st.previewForm = false; st.previewErr = ""; }
      else if (pv) st.preview = { ...st.preview, user_direction: pv.user_direction };
    }
    repaintList();
    repaintDetail();
  }

  async function act(name, btn) {
    const sid = st.selected;
    if (!sid && !["more", "help", "prev", "next"].includes(name)) return;
    const noteVal = ($("#note-text") || {}).value || null;
    if (name === "approve") {
      await busy(btn, () => post(`/api/segments/${sid}/approve`, { note: noteVal }), "Segment approved");
      await refreshAll();
      if (st.filter === "flagged" || st.filter === "issues") { const nxt = st.items[0]; if (nxt) await select(nxt.id); }
    } else if (name === "regenerate") {
      await busy(btn, () => post(`/api/segments/${sid}/regenerate`, { note: noteVal }), "Queued for regeneration (front of the queue)");
      await refreshAll();
    } else if (name === "regen-dir") {
      const res = await busy(btn, () => post(`/api/segments/${sid}/regenerate`, { note: noteVal, direction: formDirection() }), "Direction saved; queued for regeneration (front of the queue)");
      clearDirty(DIRECTION_FIELDS);
      st.previewForm = false;
      await refreshAll(true);
      if (res && res.delivery && st.selected === sid) { st.preview = { segment_id: sid, ...res.delivery }; st.previewErr = ""; repaintExplain(); }
    } else if (name === "preview-dir") {
      await formPreview();
      const ex = $("#rv-explain"); if (ex) ex.scrollIntoView({ block: "nearest" });
    } else if (name === "clear-dir") {
      await busy(btn, () => patch(`/api/segments/${sid}/narrative`, { emotion: null, intensity: null, pacing: null, tension: null, delivery: null }), "Direction cleared");
      clearDirty(DIRECTION_FIELDS); st.previewForm = false;
      DIRECTION_FIELDS.forEach((id) => { const el = $("#" + id); if (el) el.value = ""; });
      await refreshAll(true);
    } else if (name === "override") {
      const text = $("#override-text").value;
      await busy(btn, () => post(`/api/segments/${sid}/override`, { text, regenerate: true }), "Spoken rendition saved; regenerating");
      clearDirty(["override-text"]);
      await refreshAll();
    } else if (name === "clear-override") {
      await busy(btn, () => post(`/api/segments/${sid}/override`, { text: null, regenerate: true }), "Reverted to automatic rendition; regenerating");
      clearDirty(["override-text"]);
      await refreshAll();
    } else if (name === "note") {
      await busy(btn, () => post(`/api/segments/${sid}/note`, { note: $("#note-text").value }), "Note saved");
      clearDirty(["note-text"]);
      await refreshAll();
    } else if (name === "select") {
      await busy(btn, () => post(`/api/segments/${sid}/select-take`, { attempt_id: Number(btn.dataset.attempt) }), "Take selected");
      await refreshAll();
    } else if (name === "prev") await step(-1);
    else if (name === "next") await step(1);
    else if (name === "more") { await loadList(true); repaintList(); }
    else if (name === "ctx") {
      const a = $(".ctx-player");
      if (a) { a.currentTime = Number(btn.dataset.t) || 0; a.play().catch(() => toast("Press play to start audio", "info")); }
    } else if (name === "help") {
      const h = $("#kbd-help"); h.hidden = !h.hidden; btn.setAttribute("aria-expanded", String(!h.hidden));
    }
  }

  async function onClick(e) {
    const seg = e.target.closest("[data-seg]");
    if (seg && view.contains(seg)) { await select(Number(seg.dataset.seg)); return; }
    const b = e.target.closest("[data-act]");
    if (b && view.contains(b)) { try { await act(b.dataset.act, b); } catch { /* toast already shown */ } }
  }
  function onEdit(e) {
    const t = e.target;
    if (!t || !EDIT_FIELDS.has(t.id) || !view.contains(t)) return;
    t.dataset.dirty = "1";
    if (t.id.startsWith("rd-")) autoPreview();
  }
  function onKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const tag = (e.target.tagName || "").toLowerCase();
    if (["input", "textarea", "select"].includes(tag)) { if (e.key === "Escape") e.target.blur(); return; }
    const k = e.key.toLowerCase();
    const map = { j: "next", k: "prev", a: "approve", r: "regenerate" };
    if (map[k]) { e.preventDefault(); const b = view.querySelector(`[data-act="${map[k]}"]`); if (b && !b.disabled) act(map[k], b).catch(() => {}); }
    else if (k === "e") { e.preventDefault(); const t = $("#override-text"); if (t) t.focus(); }
    else if (k === "p") { e.preventDefault(); const a = $(".main-take"); if (a) { if (a.paused) a.play().catch(() => {}); else a.pause(); } }
    else if (k === "/") { e.preventDefault(); $("#f-q").focus(); }
  }

  paintFrame();
  repaintList(true); // a deep link (?seg=) opens with its segment in view
  view.addEventListener("click", onClick);
  view.addEventListener("input", onEdit);
  view.addEventListener("change", onEdit);
  document.addEventListener("keydown", onKey);
  let lastSeg = null;
  const off = onState(async (state) => {
    if (!state || state.segments === lastSeg) return;
    lastSeg = state.segments;
    if ([...view.querySelectorAll("audio")].some((a) => !a.paused)) return; // don't interrupt playback
    try { await refreshAll(); } catch { /* transient */ }
  }, 3000);
  return () => { off(); view.removeEventListener("click", onClick); view.removeEventListener("input", onEdit); view.removeEventListener("change", onEdit); document.removeEventListener("keydown", onKey); };
}
export { navigate };
