// Multi-manuscript library: upload, import preview, metadata + narrator, add to queue.
import { api, get, post, html, setHTML, badge, progressBar, fmtDuration, fmtDate, toast, toastError, busy, onState } from "../ui.js";

// Previews survive navigation within the session (the server also keeps them for 48 h).
const pending = [];
let selectedToken = null;

function uploadCard() {
  return html`<section class="card" aria-labelledby="up-h">
    <h2 id="up-h">Upload manuscripts</h2>
    <p class="muted">EPUB (DRM-free), DOCX, TXT or Markdown. You can select several files; each gets its own preview before anything is queued.
      The original file is stored unchanged with its SHA-256 hash.</p>
    <label class="dropzone" id="dropzone" tabindex="0">
      <input type="file" id="file-input" accept=".epub,.docx,.txt,.md,.markdown" multiple class="sr-only">
      <span class="dz-icon" aria-hidden="true">⇪</span>
      <span><strong>Choose files</strong> or drag them here</span>
      <span class="small muted">Large manuscripts (50,000+ words) are fine.</span>
    </label>
    <ul class="upload-list" id="upload-list" aria-live="polite">${pending.map((p) => html`<li>
      <button type="button" class="linklike ${p.token === selectedToken ? "sel" : ""}" data-act="select" data-token="${p.token}">${p.filename}</button>
      <span class="small muted">${p.totals.words.toLocaleString()} words · ${p.totals.chapters} chapters</span>
      ${p.done ? html`<span class="badge tone-good">imported</span>` : ""}</li>`)}</ul>
  </section>`;
}

function previewPanel(p, voices) {
  if (!p) return html`<section class="card muted-card"><h2>Import preview</h2><p class="muted">Upload a manuscript to see its chapters, word count, segments and exactly what will and will not be read.</p></section>`;
  const t = p.totals;
  return html`<section class="card" aria-labelledby="pv-h">
    <h2 id="pv-h">Import preview: ${p.filename}</h2>
    <div class="kv-row">
      <div><span class="kv-k">Format</span> ${p.format.toUpperCase()}</div>
      <div><span class="kv-k">Words</span> ${t.words.toLocaleString()}</div>
      <div><span class="kv-k">Chapters</span> ${t.included_chapters} of ${t.chapters}</div>
      <div><span class="kv-k">Segments</span> ${t.segments.toLocaleString()}</div>
      <div><span class="kv-k">Est. length</span> ${fmtDuration(t.est_hours * 3600)}</div>
      <div><span class="kv-k">Not read</span> ${t.not_read}</div>
    </div>
    <p class="small muted">SHA-256 <code>${p.sha256}</code></p>
    ${p.warnings.length ? html`<div class="callout warn"><strong>Parse warnings</strong><ul>${p.warnings.map((w) => html`<li>${w}</li>`)}</ul></div>` : ""}
    <form id="confirm-form" class="form" novalidate>
      <fieldset><legend>Book metadata</legend>
        <div class="form-grid">
          <label>Title <input name="title" required maxlength="300" value="${p.title || ""}"></label>
          <label>Author <input name="author" maxlength="300" value="${p.author || ""}"></label>
          <label>Narrator credit <input name="narrator_credit" maxlength="200" placeholder="defaults to the voice name"></label>
          <label>Publisher <input name="publisher" maxlength="200"></label>
          <label>Year <input name="year" maxlength="20" inputmode="numeric"></label>
          <label>Genre <input name="genre" maxlength="60" value="Audiobook"></label>
        </div>
        <label>Description <textarea name="description" rows="2" maxlength="4000"></textarea></label>
      </fieldset>
      <fieldset><legend>Narrator</legend>
        <div class="form-grid">
          <label>Approved narrator voice
            <select name="voice_id" required>${voices.map((v) => html`<option value="${v.id}">${v.name} – ${v.kind === "preset" ? "preset " + (v.version.speaker || "") : v.kind === "clone" ? "cloned (consent on file)" : "designed"}</option>`)}</select></label>
          <label>Delivery instructions (optional)
            <input name="instructions" maxlength="1000" placeholder="e.g. Warm and intimate; slightly slower for dialogue"></label>
        </div>
        <p class="small muted">The narrator, model and settings are frozen for this book at import. Changing them later explicitly regenerates audio instead of mixing voices.</p>
      </fieldset>
      <fieldset><legend>Chapters to narrate</legend>
        <div class="table-wrap"><table class="table compact">
          <thead><tr><th scope="col">Read</th><th scope="col">#</th><th scope="col">Title</th><th scope="col">Words</th><th scope="col">Segments</th><th scope="col">Not read</th></tr></thead>
          <tbody>${p.chapters.map((c) => html`<tr class="${c.include ? "" : "excluded"}">
            <td><input type="checkbox" name="include" value="${c.index}" ${c.include ? html`checked` : ""} aria-label="Narrate ${c.title}"></td>
            <td>${c.index + 1}</td>
            <td><details><summary>${c.title}</summary><p class="small">${c.sample}${c.sample.length >= 400 ? "…" : ""}</p>
              ${c.headings.length ? html`<p class="small muted">Sub-headings: ${c.headings.join(" · ")}</p>` : ""}</details>
              ${c.include_reason ? html`<div class="small warn-text">${c.include_reason}</div>` : ""}</td>
            <td>${c.words.toLocaleString()}</td><td>${c.segments}</td>
            <td>${c.skipped_count ? html`<details><summary>${c.skipped_count} item(s)</summary><ul class="small">${c.skipped.map((s) => html`<li><em>${s.reason}</em>: ${s.text}</li>`)}</ul></details>` : "–"}</td>
          </tr>`)}</tbody></table></div>
      </fieldset>
      <div class="form-actions">
        <button type="submit" class="btn" data-queue="0">Add to library</button>
        <button type="submit" class="btn primary" data-queue="1">Add &amp; queue for narration</button>
        <button type="button" class="btn ghost" data-act="discard">Discard preview</button>
      </div>
    </form></section>`;
}

function bookList(books) {
  return html`<section class="card" aria-labelledby="lib-h">
    <h2 id="lib-h">Library (${books.length})</h2>
    ${books.length ? html`<div class="table-wrap"><table class="table">
      <thead><tr><th scope="col">Title</th><th scope="col">Narrator</th><th scope="col">Progress</th><th scope="col">Length</th><th scope="col">State</th><th scope="col"><span class="sr-only">Actions</span></th></tr></thead>
      <tbody>${books.map((b) => html`<tr>
        <td><a href="#/book/${b.id}"><strong>${b.title}</strong></a><div class="small muted">${b.author || ""}${b.author ? " · " : ""}${b.source || ""} · ${fmtDate(b.created_at)}</div></td>
        <td>${b.voice || "–"}</td>
        <td class="nowrap">${progressBar(b.progress, `${b.title} progress`)}<div class="small muted">${b.produced}/${b.segments}${b.flagged ? html` · <span class="warn-text">${b.flagged} flagged</span>` : ""}</div></td>
        <td class="small">${fmtDuration(b.audio_duration_s || b.est_duration_s)}${b.audio_duration_s ? "" : " est."}</td>
        <td>${badge(b.state)}</td>
        <td class="actions">
          ${b.pending && !(b.job && ["queued", "running", "paused"].includes(b.job.status)) ? html`<button class="btn small primary" data-act="queue" data-id="${b.id}">Queue</button>` : ""}
          <a class="btn small" href="#/review/${b.id}">Review</a></td></tr>`)}</tbody></table></div>`
      : html`<p class="muted">No books yet.</p>`}
  </section>`;
}

export async function render(view) {
  let voices = (await get("/api/voices")).voices;
  let books = (await get("/api/books")).books;

  const paint = () => {
    const p = pending.find((x) => x.token === selectedToken && !x.done);
    setHTML(view, html`<div class="page-head"><h1>Library &amp; Import</h1></div>
      <div class="grid-import">${uploadCard()}${previewPanel(p, voices)}</div><div id="lib-wrap">${bookList(books)}</div>`);
    wire();
  };

  async function upload(files) {
    for (const f of files) {
      const fd = new FormData();
      fd.append("file", f, f.name);
      toast(`Analysing ${f.name}…`, "info", 2500);
      try {
        const p = await api("POST", "/api/imports", fd);
        pending.push(p);
        selectedToken = p.token;
        toast(`${f.name}: ${p.totals.words.toLocaleString()} words in ${p.totals.chapters} chapters`, "success");
      } catch (e) { toastError(new Error(`${f.name}: ${e.message}`)); }
      paint();
    }
  }

  function wire() {
    const input = view.querySelector("#file-input");
    const dz = view.querySelector("#dropzone");
    input.addEventListener("change", () => upload([...input.files]));
    dz.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
    ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
    dz.addEventListener("drop", (e) => upload([...e.dataTransfer.files]));
    const form = view.querySelector("#confirm-form");
    if (form) {
      let queueFlag = false;
      form.querySelectorAll("button[type=submit]").forEach((b) => b.addEventListener("click", () => { queueFlag = b.dataset.queue === "1"; }));
      form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const fd = new FormData(form);
        if (!fd.get("title").trim()) { toast("Title is required", "error"); form.title.focus(); return; }
        const include = fd.getAll("include").map(Number);
        if (!include.length) { toast("Select at least one chapter to narrate.", "error"); return; }
        const body = { title: fd.get("title"), author: fd.get("author"), narrator_credit: fd.get("narrator_credit"), publisher: fd.get("publisher"),
          year: fd.get("year"), genre: fd.get("genre") || "Audiobook", description: fd.get("description"), voice_id: Number(fd.get("voice_id")),
          instructions: fd.get("instructions"), include, queue: queueFlag };
        const btn = e.submitter || form.querySelector("button[type=submit]");
        const book = await busy(btn, () => post(`/api/imports/${selectedToken}/confirm`, body));
        const p = pending.find((x) => x.token === selectedToken); if (p) p.done = true;
        toast(`'${book.title}' imported: ${book.segments} segments${queueFlag ? " – queued" : ""}`, "success");
        books = (await get("/api/books")).books;
        selectedToken = (pending.find((x) => !x.done) || {}).token || null;
        paint();
      });
    }
  }

  async function onClick(e) {
    const t = e.target.closest("[data-act]");
    if (!t || !view.contains(t)) return;
    const act = t.dataset.act;
    if (act === "select") { selectedToken = t.dataset.token; paint(); }
    else if (act === "discard") {
      const i = pending.findIndex((x) => x.token === selectedToken); if (i >= 0) pending.splice(i, 1);
      selectedToken = (pending.find((x) => !x.done) || {}).token || null; paint();
    } else if (act === "queue") {
      const j = await busy(t, () => post("/api/queue", { book_id: Number(t.dataset.id) }));
      toast(j.created ? "Added to the queue" : "Already in the queue", "success");
      books = (await get("/api/books")).books; paint();
    }
  }

  paint();
  view.addEventListener("click", onClick);
  const off = onState(async () => {
    try {
      books = (await get("/api/books")).books;
      const wrap = view.querySelector("#lib-wrap");
      if (wrap) setHTML(wrap, bookList(books)); // only the list: never disturb a form being filled in
    } catch (e) { /* transient; the next update retries */ }
  }, 4000);
  return () => { off(); view.removeEventListener("click", onClick); };
}
