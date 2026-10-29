// Audio review / production QA workspace with paged segment navigation and keyboard shortcuts.
import { get, post, html, setHTML, badge, fmtClock, fmtDate, toast, busy, debounce, qs, onState, player, navigate, progressBar } from "../ui.js";

const FILTERS = [["issues", "Issues (flags + warnings)"], ["flagged", "Flagged only"], ["all", "All segments"], ["generated", "Generated"],
  ["passed", "QA passed"], ["approved", "Approved"], ["pending", "Not rendered yet"], ["overridden", "Spoken text edited"], ["noted", "With notes"],
  ["speaker_review", "Speaker needs review"], ["direction_review", "Direction needs review / stale"]];

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

function detailPanel(d) {
  const s = d.segment;
  const sel = d.attempts.find((a) => a.selected);
  const latest = sel || d.attempts[0];
  const spoken = s.override_text || s.narration_text;
  const busyState = s.status === "pending" || s.status === "synthesizing";
  return html`<div class="seg-detail">
    <div class="seg-head">
      <div><h2 class="seg-title"><code>${s.sid}</code></h2>
        <div class="small muted">Chapter ${d.chapter.idx}: ${d.chapter.title} · segment ${s.idx} · ${s.kind} · <a href="#/inspect/${s.id}">Inspect speaker, context &amp; direction</a>${d.chapter_timing ? html` · ${fmtClock(d.chapter_timing.start_s)} in chapter master` : ""}</div></div>
      <div class="seg-badges">${badge(s.status)} <span class="badge tone-neutral">attempts ${s.attempts_used}/${d.max_attempts} (round ${s.round})</span></div>
      <div class="btn-group" role="group" aria-label="Segment navigation">
        <button class="btn small ghost" data-act="prev" ${d.prev_id ? "" : html`disabled`} aria-label="Previous segment (K)">← Prev</button>
        <button class="btn small ghost" data-act="next" ${d.next_id ? "" : html`disabled`} aria-label="Next segment (J)">Next →</button></div>
    </div>
    ${s.flag_reason ? html`<div class="callout warn small"><strong>Why flagged:</strong> ${s.flag_reason}</div>` : ""}
    ${busyState ? html`<div class="callout info small"><span class="spinner" aria-hidden="true"></span> This segment is queued for (re)generation. The page updates automatically.</div>` : ""}
    <div class="players">
      ${sel && sel.playable ? html`<div><div class="small muted">Selected take ${sel.round}.${sel.attempt_no}</div>${player(`/media/attempt/${sel.id}`, "Selected take", "main-take")}</div>` : html`<p class="muted">No playable take yet.</p>`}
      ${d.chapter_timing ? html`<div><div class="small muted">In context (chapter master)</div>
        <audio class="player ctx-player" controls preload="none" src="/media/chapter/${d.chapter.id}.mp3" aria-label="Chapter master"></audio>
        <button class="btn small ghost" data-act="ctx" data-t="${d.chapter_timing.start_s}">Play from this segment</button></div>` : ""}
    </div>
    <div class="tri">
      <section><h3>Original manuscript <span class="small muted">(immutable)</span></h3><p class="ms-text">${s.original_text}</p></section>
      <section><h3>Spoken rendition ${s.override_text ? html`<span class="badge tone-info">edited</span>` : html`<span class="small muted">(auto-normalised)</span>`}</h3>
        <textarea id="override-text" rows="5" maxlength="2000" aria-label="Spoken rendition">${spoken}</textarea>
        <div class="form-actions"><button class="btn small" data-act="override">Save &amp; regenerate (E)</button>
          ${s.override_text ? html`<button class="btn small ghost" data-act="clear-override">Revert to automatic</button>` : ""}</div>
        <p class="small muted">Use this for pronunciations, e.g. “Siobhan” → “Shiv-awn”. The manuscript text is never changed.</p></section>
      <section><h3>Recognised by Whisper ${latest && latest.wer !== null && latest.wer !== undefined ? html`<span class="badge tone-${latest.wer > 0.12 ? "warn" : "good"}">WER ${(latest.wer * 100).toFixed(1)}%</span>` : ""}</h3>
        <p class="asr-text">${latest && latest.asr_text !== null ? latest.asr_text : html`<span class="muted">–</span>`}</p>
        <div class="diff small" aria-label="Word comparison">${latest ? diffView(latest.diff) : ""}</div>
        <p class="small muted">Legend: <del class="d-del">missing</del> <ins class="d-ins">extra</ins> <span class="d-sub"><del>expected</del><ins>heard</ins></span>. Agreement with Whisper is not proof of correct pronunciation.</p></section>
    </div>
    ${latest ? html`<section><h3>Findings for take ${latest.round}.${latest.attempt_no}</h3>${issueList(latest.issues)}
      <div class="small muted">Acoustics: ${latest.acoustics && latest.acoustics.duration_s !== undefined ? html`${latest.acoustics.duration_s}s · speech ${latest.acoustics.rms_dbfs} dBFS · peak ${latest.acoustics.peak_dbfs} dBFS · longest pause ${latest.acoustics.longest_internal_silence_s}s${latest.acoustics.duration_ratio ? html` · duration ratio ${latest.acoustics.duration_ratio}` : ""}` : "–"}</div></section>` : ""}
    <div class="action-bar">
      <button class="btn primary" data-act="approve" ${sel && !busyState && s.status !== "approved" ? "" : html`disabled`}>Approve (A)</button>
      <button class="btn" data-act="regenerate" ${busyState ? html`disabled` : ""}>Regenerate (R)</button>
      <label class="note-field">Editor note <input id="note-text" maxlength="2000" value="${s.note || ""}" placeholder="Optional note for this segment"></label>
      <button class="btn ghost" data-act="note">Save note</button>
    </div>
    <section><h3>Takes &amp; attempt history (${d.attempts.length})</h3>
      <div class="table-wrap"><table class="table compact takes">
        <thead><tr><th scope="col">Take</th><th scope="col">Status</th><th scope="col">WER</th><th scope="col">Score</th><th scope="col">Issues</th><th scope="col">Audio</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
        <tbody>${d.attempts.map((a) => html`<tr class="${a.selected ? "selected-take" : ""}">
          <td>${a.round}.${a.attempt_no}${a.selected ? html` <span class="badge tone-good">selected</span>` : ""}<div class="small muted">${a.engine}${a.temperature ? ` · T=${a.temperature}` : ""} · seed ${a.seed}</div></td>
          <td>${a.status}${a.passed ? html` <span class="good-text">✓</span>` : ""}${a.error ? html`<div class="small err">${a.error}</div>` : ""}${!a.fingerprint_ok ? html`<div class="small warn-text">old narrator settings</div>` : ""}</td>
          <td>${a.wer === null || a.wer === undefined ? "–" : (a.wer * 100).toFixed(1) + "%"}</td>
          <td>${a.score === null || a.score === undefined ? "–" : a.score.toFixed(1)}</td>
          <td class="small">${(a.issues || []).filter((i) => i.severity === "error").length} errors, ${(a.issues || []).filter((i) => i.severity === "warning").length} warnings</td>
          <td>${a.playable ? html`${player(`/media/attempt/${a.id}`, `Take ${a.round}.${a.attempt_no}`, "mini")} <a class="small" href="/media/attempt/${a.id}?download=1">WAV</a>` : html`<span class="muted small">–</span>`}</td>
          <td>${!a.selected && a.playable && a.fingerprint_ok && a.status === "done" && !busyState ? html`<button class="btn small" data-act="select" data-attempt="${a.id}">Use this take</button>` : ""}</td>
        </tr>`)}</tbody></table></div></section>
    ${d.reviews.length ? html`<section><h3>Review history</h3><ol class="feed small">${d.reviews.map((r) => html`<li>${fmtDate(r.created_at)} – <strong>${r.action}</strong> by ${r.actor}${r.detail && r.detail.note ? html`: ${r.detail.note}` : ""}</li>`)}</ol></section>` : ""}
  </div>`;
}

async function bookPicker(view) {
  const { books } = await get("/api/books");
  setHTML(view, html`<div class="page-head"><h1>Review &amp; QA</h1></div>
    <section class="card"><h2>Choose a book to review</h2>
    ${books.length ? html`<ul class="book-pick">${books.map((b) => html`<li><a href="#/review/${b.id}?filter=${b.flagged ? "flagged" : "all"}"><strong>${b.title}</strong></a>
      ${badge(b.state)} <span class="small muted">${b.produced}/${b.segments} rendered · ${b.flagged} flagged · ${b.approved} approved</span>
      ${progressBar(b.progress, b.title)}</li>`)}</ul>` : html`<p class="muted">No books yet. <a href="#/library">Import a manuscript</a>.</p>`}</section>`);
}

export async function render(view, parts, query) {
  if (!parts[0]) { await bookPicker(view); return null; }
  const bookId = Number(parts[0]);
  const book = await get(`/api/books/${bookId}`);
  const st = { filter: query.get("filter") || (book.flagged ? "flagged" : "all"), chapter: query.get("chapter") || "", q: "", items: [], next: null, total: 0,
    selected: query.get("seg") ? Number(query.get("seg")) : null, detail: null, names: {} };
  try { (await get(`/api/books/${bookId}/characters`)).characters.forEach((c) => { st.names[c.id] = c.name; }); } catch { /* optional */ }

  async function loadList(append = false) {
    const res = await get(`/api/books/${bookId}/segments` + qs({ filter: st.filter, chapter_id: st.chapter, q: st.q, after: append ? st.next : "", limit: 60 }));
    st.items = append ? st.items.concat(res.items) : res.items;
    st.next = res.next; st.total = res.total;
  }
  async function loadDetail(id) {
    st.selected = id;
    st.detail = id ? await get(`/api/segments/${id}`) : null;
    history.replaceState(null, "", `#/review/${bookId}` + qs({ seg: id, filter: st.filter, chapter: st.chapter }));
  }

  function listItems() {
    return html`${st.items.map((s) => html`<li><button type="button" class="seg-item ${s.id === st.selected ? "sel" : ""}" data-seg="${s.id}" aria-current="${s.id === st.selected ? "true" : "false"}">
      <span class="seg-item-top"><code>c${s.chapter_idx}·${s.idx}</code> ${badge(s.status)} ${s.wer !== null && s.wer !== undefined ? html`<span class="small muted">${(s.wer * 100).toFixed(0)}%</span>` : ""}
      ${s.needs_review ? html`<span class="badge tone-warn" title="speaker not established - open Inspect to assign">speaker?</span>` : ""}
      ${s.character_id && st.names[s.character_id] ? html`<span class="small muted">${st.names[s.character_id]}</span>` : ""}
      ${s.override_text ? html`<span class="badge tone-info" title="spoken text edited">✎</span>` : ""}${s.note ? html`<span class="badge tone-neutral" title="has note">✉</span>` : ""}</span>
      <span class="seg-snip">${s.original_text.slice(0, 110)}${s.original_text.length > 110 ? "…" : ""}</span></button></li>`)}`;
  }

  function paint() {
    setHTML(view, html`<div class="page-head"><h1>Review: ${book.title}</h1>
      <div class="head-actions"><a class="btn" href="#/book/${bookId}">Book, exports &amp; release</a>
        <button class="btn ghost" type="button" data-act="help" aria-expanded="false">Keyboard shortcuts</button></div></div>
      <div class="kbd-help card" id="kbd-help" hidden><strong>Shortcuts:</strong> <kbd>J</kbd>/<kbd>K</kbd> next/previous · <kbd>A</kbd> approve · <kbd>R</kbd> regenerate ·
        <kbd>E</kbd> edit spoken text · <kbd>P</kbd> play/pause selected take · <kbd>/</kbd> search</div>
      <div class="review-layout">
        <aside class="card review-side" aria-label="Segments">
          <label>Chapter <select id="f-chapter"><option value="">All chapters</option>${book.chapters.map((c) => html`<option value="${c.id}" ${String(c.id) === String(st.chapter) ? html`selected` : ""}>
            ${c.index}. ${c.title} (${(c.status_counts.flagged || 0)} flagged)</option>`)}</select></label>
          <label>Show <select id="f-filter">${FILTERS.map(([k, l]) => html`<option value="${k}" ${k === st.filter ? html`selected` : ""}>${l}</option>`)}</select></label>
          <label>Search text or ID <input id="f-q" type="search" value="${st.q}" placeholder="word or sid"></label>
          <p class="small muted" aria-live="polite">${st.total.toLocaleString()} segment(s)</p>
          <ul class="seg-list" id="seg-list">${listItems()}</ul>
          ${st.next ? html`<button class="btn small" data-act="more">Load more</button>` : ""}
        </aside>
        <section class="card review-main" aria-live="polite" aria-label="Segment detail">${st.detail ? detailPanel(st.detail) : html`<p class="muted">Select a segment to review it.</p>`}</section>
      </div>`);
    const sel = view.querySelector(".seg-item.sel");
    if (sel) sel.scrollIntoView({ block: "nearest" });
    view.querySelector("#f-chapter").addEventListener("change", async (e) => { st.chapter = e.target.value; await loadList(); paint(); });
    view.querySelector("#f-filter").addEventListener("change", async (e) => { st.filter = e.target.value; await loadList(); paint(); });
    view.querySelector("#f-q").addEventListener("input", debounce(async (e) => { st.q = e.target.value.trim(); await loadList(); repaintList(); }, 350));
  }
  function repaintList() {
    const ul = view.querySelector("#seg-list");
    if (ul) setHTML(ul, listItems());
    const cnt = view.querySelector(".review-side p[aria-live]");
    if (cnt) cnt.textContent = `${st.total.toLocaleString()} segment(s)`;
  }

  async function select(id) { await loadDetail(id); paint(); const h = view.querySelector(".seg-title"); if (h) { h.setAttribute("tabindex", "-1"); h.focus({ preventScroll: true }); } }
  async function step(dir) {
    const idx = st.items.findIndex((s) => s.id === st.selected);
    let target = st.items[idx + dir];
    if (!target && dir > 0 && st.next) { await loadList(true); target = st.items[idx + 1]; }
    if (!target && st.detail) { const id = dir > 0 ? st.detail.next_id : st.detail.prev_id; if (id) return select(id); }
    if (target) await select(target.id);
  }
  async function refreshAll() {
    await loadList();
    if (st.selected) st.detail = await get(`/api/segments/${st.selected}`);
    paint();
  }

  async function act(name, btn) {
    const sid = st.selected;
    if (!sid && !["more", "help", "prev", "next"].includes(name)) return;
    if (name === "approve") {
      await busy(btn, () => post(`/api/segments/${sid}/approve`, { note: (view.querySelector("#note-text") || {}).value || null }), "Segment approved");
      await refreshAll();
      if (st.filter === "flagged" || st.filter === "issues") { const nxt = st.items[0]; if (nxt) await select(nxt.id); }
    } else if (name === "regenerate") {
      await busy(btn, () => post(`/api/segments/${sid}/regenerate`, { note: (view.querySelector("#note-text") || {}).value || null }), "Queued for regeneration (front of the queue)");
      await refreshAll();
    } else if (name === "override") {
      const text = view.querySelector("#override-text").value;
      await busy(btn, () => post(`/api/segments/${sid}/override`, { text, regenerate: true }), "Spoken rendition saved; regenerating");
      await refreshAll();
    } else if (name === "clear-override") {
      await busy(btn, () => post(`/api/segments/${sid}/override`, { text: null, regenerate: true }), "Reverted to automatic rendition; regenerating");
      await refreshAll();
    } else if (name === "note") {
      await busy(btn, () => post(`/api/segments/${sid}/note`, { note: view.querySelector("#note-text").value }), "Note saved");
      await refreshAll();
    } else if (name === "select") {
      await busy(btn, () => post(`/api/segments/${sid}/select-take`, { attempt_id: Number(btn.dataset.attempt) }), "Take selected");
      await refreshAll();
    } else if (name === "prev") await step(-1);
    else if (name === "next") await step(1);
    else if (name === "more") { await loadList(true); paint(); }
    else if (name === "ctx") {
      const a = view.querySelector(".ctx-player");
      if (a) { a.currentTime = Number(btn.dataset.t) || 0; a.play().catch(() => toast("Press play to start audio", "info")); }
    } else if (name === "help") {
      const h = view.querySelector("#kbd-help"); h.hidden = !h.hidden; btn.setAttribute("aria-expanded", String(!h.hidden));
    }
  }

  async function onClick(e) {
    const seg = e.target.closest("[data-seg]");
    if (seg) { await select(Number(seg.dataset.seg)); return; }
    const b = e.target.closest("[data-act]");
    if (b && view.contains(b)) { try { await act(b.dataset.act, b); } catch { /* toast already shown */ } }
  }
  function onKey(e) {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const tag = (e.target.tagName || "").toLowerCase();
    if (["input", "textarea", "select"].includes(tag)) { if (e.key === "Escape") e.target.blur(); return; }
    const k = e.key.toLowerCase();
    const map = { j: "next", k: "prev", a: "approve", r: "regenerate" };
    if (map[k]) { e.preventDefault(); const b = view.querySelector(`[data-act="${map[k]}"]`); if (b && !b.disabled) act(map[k], b).catch(() => {}); }
    else if (k === "e") { e.preventDefault(); const t = view.querySelector("#override-text"); if (t) t.focus(); }
    else if (k === "p") { e.preventDefault(); const a = view.querySelector(".main-take"); if (a) { if (a.paused) a.play().catch(() => {}); else a.pause(); } }
    else if (k === "/") { e.preventDefault(); view.querySelector("#f-q").focus(); }
  }

  await loadList();
  if (!st.selected && st.items.length) st.selected = st.items[0].id;
  if (st.selected) await loadDetail(st.selected);
  paint();
  view.addEventListener("click", onClick);
  document.addEventListener("keydown", onKey);
  let lastSeg = null;
  const off = onState(async (state) => {
    if (!state || state.segments === lastSeg) return;
    lastSeg = state.segments;
    const ae = document.activeElement;
    if (ae && ["textarea", "input"].includes((ae.tagName || "").toLowerCase()) && view.contains(ae)) return; // don't clobber edits
    if (view.querySelector("audio:not([paused])") && [...view.querySelectorAll("audio")].some((a) => !a.paused)) return; // don't interrupt playback
    try { await refreshAll(); } catch { /* transient */ }
  }, 3000);
  return () => { off(); view.removeEventListener("click", onClick); document.removeEventListener("keydown", onKey); };
}
export { navigate };
