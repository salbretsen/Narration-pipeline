// Book page: metadata, narrator, chapters & players, assembly, exports, QA report, release signoff, storage.
import { api, get, post, patch, html, setHTML, badge, progressBar, fmtDuration, fmtDate, fmtBytes, fmtAgo, toast, busy, dialog, confirmDialog, onState, player, ApiError } from "../ui.js";

export async function render(view, parts) {
  const id = Number(parts[0]);
  let b = await get(`/api/books/${id}`);
  let voices = (await get("/api/voices")).voices;

  const paint = () => {
    const rc = b.release_check;
    const busyJob = b.utility_jobs.find((j) => ["queued", "running"].includes(j.status));
    const lastUtil = b.utility_jobs[0];
    const activeBookJob = b.job && ["queued", "running", "paused"].includes(b.job.status);
    setHTML(view, html`<div class="page-head"><h1>${b.title}</h1>
      <div class="head-actions">${badge(b.state)}
        <a class="btn primary" href="#/review/${id}?filter=${b.flagged ? "flagged" : "all"}">Review audio</a>
        ${b.pending && !activeBookJob ? html`<button class="btn" data-act="queue">Queue generation</button>` : ""}</div></div>
    <div class="callout ${b.release ? "good" : "info"} small">${b.release
      ? html`<strong>Approved for publication</strong> by ${b.release.approved_by} on ${fmtDate(b.release.approved_at)} (${b.release.listen_scope} listen). Exports made after approval are final; earlier ones stay labelled DRAFT.`
      : html`<strong>Draft.</strong> Exports are labelled DRAFT until a human signs off below. This studio produces local publication candidates only – nothing is uploaded to Audible, ACX or any host.`}</div>
    <div class="stats compact">
      <div class="stat"><div class="stat-value">${b.produced}/${b.segments}</div><div class="stat-label">segments rendered</div>${progressBar(b.progress, "Rendered")}</div>
      <div class="stat ${b.flagged ? "warn" : "good"}"><div class="stat-value">${b.flagged}</div><div class="stat-label">need review</div></div>
      <div class="stat"><div class="stat-value">${b.passed}</div><div class="stat-label">QA passed</div></div>
      <div class="stat approved"><div class="stat-value">${b.approved}</div><div class="stat-label">approved</div></div>
      <div class="stat"><div class="stat-value">${fmtDuration(b.audio_duration_s)}</div><div class="stat-label">audio (est. ${fmtDuration(b.est_duration_s)})</div></div>
    </div>
    ${b.last_error ? html`<div class="callout bad small"><strong>Last error:</strong> ${b.last_error}</div>` : ""}

    <section class="card" aria-labelledby="ch-h"><h2 id="ch-h">Chapters</h2>
      <div class="form-actions"><button class="btn" data-act="assemble" ${busyJob ? html`disabled` : ""}>Assemble chapter masters (WAV + MP3)</button>
        ${busyJob ? html`<span class="small muted"><span class="spinner" aria-hidden="true"></span> ${busyJob.kind} job #${busyJob.id} ${busyJob.status}…</span>` : ""}
        ${lastUtil && lastUtil.status === "failed" ? html`<span class="small err">Last ${lastUtil.kind} job: ${lastUtil.error}</span>` : ""}</div>
      <div class="table-wrap"><table class="table">
        <thead><tr><th scope="col">#</th><th scope="col">Chapter</th><th scope="col">Segments</th><th scope="col">Master</th><th scope="col">Downloads</th></tr></thead>
        <tbody>${b.chapters.map((c) => {
          const sc = c.status_counts, mp3 = c.renders.mp3, wav = c.renders.wav;
          return html`<tr><td>${c.index}</td>
            <td><strong>${c.title}</strong><div class="small muted">${c.words.toLocaleString()} words · est. ${fmtDuration(c.est_duration_s)}</div></td>
            <td class="small">${(sc.passed || 0) + (sc.approved || 0) + (sc.flagged || 0)}/${c.segments} rendered${sc.flagged ? html` · <a href="#/review/${id}?chapter=${c.id}&filter=flagged" class="warn-text">${sc.flagged} flagged</a>` : ""}
              ${c.missing_audio ? html`<div class="err">${c.missing_audio} missing – assembly blocked</div>` : ""}</td>
            <td>${mp3 ? html`${player(`/media/chapter/${c.id}.mp3`, `Chapter ${c.index} master`, "mini")}<div class="small muted">${fmtDuration(mp3.duration_s)} · ${mp3.draft ? "DRAFT" : "final"}${c.render_current ? "" : html` · <span class="warn-text">outdated</span>`}</div>` : html`<span class="small muted">not assembled</span>`}</td>
            <td class="small nowrap">${mp3 ? html`<a href="/media/chapter/${c.id}.mp3?download=1">MP3</a> · ` : ""}${wav ? html`<a href="/media/chapter/${c.id}.wav?download=1">WAV</a> · ` : ""}
              <button class="linklike" data-act="chapter-m4b" data-ch="${c.id}" ${c.missing_audio ? html`disabled` : ""}>M4B</button></td></tr>`;
        })}</tbody></table></div>
    </section>

    <div class="grid-2">
    <section class="card" aria-labelledby="ex-h"><h2 id="ex-h">Exports</h2>
      <p class="small muted">Exports never omit missing segments: if any segment lacks usable audio the export is blocked and the gaps are listed. Outputs are verified with ffprobe (codec, duration, chapter markers).</p>
      <div class="form-actions">
        <button class="btn primary" data-act="export-m4b" ${busyJob ? html`disabled` : ""}>Build ${b.release ? "final" : "DRAFT"} M4B (chapter-marked AAC)</button>
        <button class="btn" data-act="export-zip" ${busyJob ? html`disabled` : ""}>Chapter MP3s (ZIP)</button>
        <button class="btn" data-act="report">QA report (HTML + JSON)</button></div>
      ${lastUtil && lastUtil.status === "failed" && lastUtil.result_json ? blockedList(lastUtil) : ""}
      ${b.exports.length ? html`<ul class="exports">${b.exports.map((e) => html`<li><a href="/media/export/${e.id}">${e.label}</a>
        ${e.draft ? html`<span class="badge tone-warn">DRAFT</span>` : html`<span class="badge tone-approved">final</span>`}
        <span class="small muted">${fmtBytes(e.size_bytes)}${e.duration_s ? " · " + fmtDuration(e.duration_s) : ""} · ${fmtAgo(e.created_at)}</span>
        ${e.kind === "report_html" ? html` · <a class="small" href="/media/export/${e.id}?download=0" target="_blank" rel="noopener">view</a>` : ""}</li>`)}</ul>` : html`<p class="muted small">No exports yet.</p>`}
    </section>

    <section class="card" aria-labelledby="rel-h"><h2 id="rel-h">Release signoff</h2>
      ${b.release ? html`<p>Approved by <strong>${b.release.approved_by}</strong> · ${fmtDate(b.release.approved_at)} · ${b.release.listen_scope} listen.</p>
        <button class="btn danger ghost" data-act="revoke">Revoke approval</button>`
      : html`${rc.ready ? html`<p class="good-text">All audio is accounted for and no automatic QA flags are open.</p>` : html`<p><strong>Not ready:</strong></p><ul class="blockers">${rc.blockers.map((x) => html`<li>${x}</li>`)}</ul>`}
        <form id="release-form" class="form">
          <label>Approved by <input name="approved_by" required minlength="2" maxlength="120" autocomplete="name"></label>
          <fieldset class="inline"><legend>Listening review performed</legend>
            <label class="check"><input type="radio" name="listen_scope" value="full" checked> Full listen (recommended)</label>
            <label class="check"><input type="radio" name="listen_scope" value="partial"> Partial / spot check</label></fieldset>
          <label>Notes <input name="notes" maxlength="2000"></label>
          <label class="check consent"><input type="checkbox" name="attest" required> I reviewed this audiobook, I hold the rights to the manuscript and the narrator voice,
            and I approve these files as publication candidates. I understand automatic QA (Whisper agreement) is not proof of correct pronunciation or performance,
            and that distributor requirements must be checked separately.</label>
          <button class="btn primary" type="submit" ${rc.ready ? "" : html`disabled`}>Record release approval</button>
        </form>`}
    </section></div>

    <div class="grid-2">
    <section class="card" aria-labelledby="meta-h"><h2 id="meta-h">Metadata &amp; cover</h2>
      <form id="meta-form" class="form"><div class="form-grid">
        <label>Title <input name="title" required maxlength="300" value="${b.title}"></label>
        <label>Author <input name="author" maxlength="300" value="${b.author}"></label>
        <label>Narrator credit <input name="narrator_credit" maxlength="200" value="${b.narrator_credit}"></label>
        <label>Publisher <input name="publisher" maxlength="200" value="${b.publisher}"></label>
        <label>Year <input name="year" maxlength="20" value="${b.year}"></label>
        <label>Genre <input name="genre" maxlength="60" value="${b.genre}"></label></div>
        <label>Description <textarea name="description" rows="2" maxlength="4000">${b.description}</textarea></label>
        <button class="btn" type="submit">Save metadata</button></form>
      <div class="cover-row">${b.has_cover ? html`<img class="cover" src="/media/cover/${id}?v=${Date.now()}" alt="Cover of ${b.title}">` : html`<div class="cover placeholder" aria-hidden="true">No cover</div>`}
        <label class="btn small">Upload cover (JPEG/PNG)<input type="file" id="cover-input" accept=".jpg,.jpeg,.png" class="sr-only"></label></div>
    </section>

    <section class="card" aria-labelledby="nar-h"><h2 id="nar-h">Narrator</h2>
      <p><strong>${b.voice_detail.name}</strong> (${b.voice_detail.kind}, v${b.voice_detail.version}) · <code class="small">${b.voice_detail.model_id}</code>
        ${b.voice_detail.speaker ? html` · speaker ${b.voice_detail.speaker}` : ""}</p>
      ${b.voice_outdated ? html`<p class="callout info small">This voice has a newer version. Switching regenerates the whole book.</p>` : ""}
      <form id="nar-form" class="form">
        <label>Voice <select name="voice_id">${voices.map((v) => html`<option value="${v.id}" ${v.id === b.voice_detail.id ? html`selected` : ""}>${v.name} (${v.kind})</option>`)}</select></label>
        <label>Delivery instructions <input name="instructions" maxlength="1000" value="${b.instructions}"></label>
        <button class="btn" type="submit">Apply narrator change…</button></form>
      <p class="small muted">Book settings are a snapshot taken at import (fingerprint <code>${b.synth_fingerprint}</code>). Takes with another fingerprint are never mixed into this book.</p>
      <button class="btn ghost small" data-act="resnapshot">Apply current global settings to this book…</button>
    </section></div>

    <div class="grid-2">
    <section class="card" aria-labelledby="ms-h"><h2 id="ms-h">Manuscript manifest</h2>
      <p class="small">${b.manuscript.name} · ${b.manuscript.format} · ${fmtBytes(b.manuscript.size_bytes)} · <a href="/media/manuscript/${id}">download original</a><br>SHA-256 <code class="small">${b.manuscript.sha256}</code></p>
      <details><summary>What is and is not narrated (${(b.manuscript.manifest.chapters || []).length} source sections)</summary>
        <ul class="small">${(b.manuscript.manifest.chapters || []).map((c) => html`<li>${c.narrated ? "✓" : "✗"} ${c.title} (${c.words} words)${c.excluded_reason ? html` – <em>${c.excluded_reason}</em>` : ""}
          ${c.not_read.length ? html`<ul>${c.not_read.map((n) => html`<li class="muted">${n.reason}: ${n.text}</li>`)}</ul>` : ""}</li>`)}</ul></details>
    </section>
    <section class="card" aria-labelledby="st-h"><h2 id="st-h">Storage</h2>
      <dl class="kv small">${Object.entries(b.storage).map(([k, v]) => html`<dt>${k}</dt><dd><code>${v}</code></dd>`)}</dl>
      <form id="move-form" class="form"><label>Move this project to another library root <input name="new_root" placeholder="/Volumes/AudiobooksSSD/Library" maxlength="4096"></label>
        <button class="btn small" type="submit">Copy, verify &amp; switch…</button>
        <p class="small muted">Files are copied and verified (size + SHA-256) before the database is switched. The old copy is kept; delete it yourself once satisfied.</p></form>
      <button class="btn small ghost" data-act="archive">${b.archived ? "Unarchive" : "Archive book"}</button>
    </section></div>

    <section class="card" aria-labelledby="log-h"><h2 id="log-h">Project log</h2>
      <ol class="feed small">${b.logs.map((l) => html`<li class="lvl-${l.level}"><span class="muted">${fmtAgo(l.created_at)}</span> ${l.message}</li>`)}</ol></section>`);
    wire();
  };

  function blockedList(job) {
    let r = {};
    try { r = JSON.parse(job.result_json) || {}; } catch { r = {}; }
    if (!r.blocked || !r.blocked.length) return "";
    return html`<details class="callout bad small" open><summary>Blocked: ${r.blocked.length} problem(s)</summary><ul>${r.blocked.slice(0, 40).map((x) => html`<li>Chapter ${x.chapter}: <code>${x.sid || ""}</code> ${x.problem || ""}</li>`)}</ul></details>`;
  }

  const reload = async () => { b = await get(`/api/books/${id}`); paint(); };

  async function narratorChange(body, btn) {
    try {
      await busy(btn, () => post(`/api/books/${id}/narrator`, body));
      toast("Narrator updated", "success");
    } catch (e) {
      if (e instanceof ApiError && e.status === 409 && e.details && e.details.rendered_segments !== undefined) {
        if (await confirmDialog("Regenerate the whole book?", e.message, "Change narrator & regenerate", true)) {
          await busy(btn, () => post(`/api/books/${id}/narrator`, { ...body, confirm_invalidate: true }), "Narrator changed; all segments will be regenerated");
          await post("/api/queue", { book_id: id }).catch(() => {});
        }
      }
    }
    await reload();
  }

  function wire() {
    view.querySelector("#meta-form").addEventListener("submit", async (e) => {
      e.preventDefault(); const fd = Object.fromEntries(new FormData(e.target).entries());
      b = await busy(e.submitter, () => patch(`/api/books/${id}`, fd), "Metadata saved"); paint();
    });
    view.querySelector("#nar-form").addEventListener("submit", async (e) => {
      e.preventDefault(); const fd = new FormData(e.target);
      await narratorChange({ voice_id: Number(fd.get("voice_id")), instructions: fd.get("instructions") }, e.submitter);
    });
    view.querySelector("#cover-input").addEventListener("change", async (e) => {
      const f = e.target.files[0]; if (!f) return;
      const fd = new FormData(); fd.append("file", f, f.name);
      b = await busy(null, () => api("POST", `/api/books/${id}/cover`, fd), "Cover saved"); paint();
    });
    const rf = view.querySelector("#release-form");
    if (rf) rf.addEventListener("submit", async (e) => {
      e.preventDefault(); if (!rf.reportValidity()) return;
      const fd = new FormData(rf);
      b = await busy(e.submitter, () => post(`/api/books/${id}/release`, { approved_by: fd.get("approved_by"), listen_scope: fd.get("listen_scope"), attest: fd.get("attest") === "on", notes: fd.get("notes") || "" }),
        "Release approval recorded. Export again to produce final (non-draft) files.");
      paint();
    });
    view.querySelector("#move-form").addEventListener("submit", async (e) => {
      e.preventDefault(); const root = new FormData(e.target).get("new_root").trim(); if (!root) return;
      if (!(await confirmDialog("Move project?", `Copy all files of '${b.title}' under ${root}, verify them, then switch the project to the new location?`, "Move"))) return;
      const r = await busy(e.submitter, () => post(`/api/books/${id}/move`, { new_root: root }));
      toast(`Moved: ${r.files} files verified. Old copy kept.`, "success", 9000); await reload();
    });
  }

  async function onClick(e) {
    const t = e.target.closest("[data-act]"); if (!t || !view.contains(t)) return;
    const a = t.dataset.act;
    try {
      if (a === "queue") { const j = await busy(t, () => post("/api/queue", { book_id: id })); toast(j.created ? "Queued" : "Already queued", "success"); }
      else if (a === "assemble") await busy(t, () => post(`/api/books/${id}/assemble`), "Assembly queued (runs on the worker between segments)");
      else if (a === "export-m4b") await busy(t, () => post(`/api/books/${id}/export`, { kind: "m4b" }), "M4B export queued");
      else if (a === "export-zip") await busy(t, () => post(`/api/books/${id}/export`, { kind: "mp3_zip" }), "MP3 ZIP export queued");
      else if (a === "chapter-m4b") await busy(t, () => post(`/api/books/${id}/export`, { kind: "chapter_m4b", chapter_id: Number(t.dataset.ch) }), "Chapter M4B export queued");
      else if (a === "report") { const r = await busy(t, () => post(`/api/books/${id}/report`)); toast(`QA report written (${r.findings} findings)`, "success"); }
      else if (a === "revoke") {
        const res = await dialog({ title: "Revoke release approval", form: true, buttons: [{ label: "Revoke", value: "ok", kind: "danger" }], body: html`<label>Reason <input name="reason" maxlength="500"></label>` });
        if (!res) return; await busy(t, () => post(`/api/books/${id}/release/revoke`, { reason: res.data.reason || "" }), "Approval revoked");
      } else if (a === "resnapshot") {
        try { await busy(t, () => post(`/api/books/${id}/resnapshot`, { confirm_invalidate: false }), "Current QA/pause/export settings applied"); }
        catch (err) {
          if (err.status === 409 && (await confirmDialog("Regenerate audio?", err.message, "Apply & regenerate", true))) {
            await busy(t, () => post(`/api/books/${id}/resnapshot`, { confirm_invalidate: true }), "Settings applied; segments marked for regeneration");
          }
        }
      } else if (a === "archive") { await busy(t, () => post(`/api/books/${id}/archive`, { archived: !b.archived }), b.archived ? "Unarchived" : "Archived"); }
    } catch { /* toast shown */ }
    await reload();
  }

  view.addEventListener("click", onClick);
  paint();
  const off = onState(async () => {
    const ae = document.activeElement;
    if (ae && view.contains(ae) && ["input", "textarea", "select"].includes(ae.tagName.toLowerCase())) return;
    if ([...view.querySelectorAll("audio")].some((x) => !x.paused)) return;
    try { b = await get(`/api/books/${id}`); paint(); } catch { /* transient */ }
  }, 3000);
  return () => { off(); view.removeEventListener("click", onClick); };
}
