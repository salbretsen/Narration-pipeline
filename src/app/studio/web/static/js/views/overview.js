// Overview & progress dashboard.
import { get, html, setHTML, badge, progressBar, fmtDuration, fmtAgo, fmtDate, onState, toastError } from "../ui.js";

function card(label, value, sub, tone = "") {
  return html`<div class="stat ${tone}"><div class="stat-value">${value}</div><div class="stat-label">${label}</div>${sub ? html`<div class="stat-sub">${sub}</div>` : ""}</div>`;
}

function liveLine(b) {
  if (!b.job) return "";
  const j = b.job;
  if (j.status === "running") {
    const ch = b.current_chapter ? `Chapter ${b.current_chapter.idx}: ${b.current_chapter.title}` : "";
    return html`<div class="live"><span class="pulse" aria-hidden="true"></span> ${ch}${j.current_segment_sid ? html` · <code>${j.current_segment_sid}</code>` : ""}${b.retries ? html` · ${b.retries} retr${b.retries === 1 ? "y" : "ies"} so far` : ""}</div>`;
  }
  if (j.status === "paused") return html`<div class="live warn">Paused${j.paused_reason ? html` – ${j.paused_reason}` : ""}</div>`;
  return "";
}

function draw(view, d) {
  const c = d.cards, st = d.segment_states;
  const segFrac = c.segments_total ? c.segments_produced / c.segments_total : 0;
  setHTML(view, html`
  <div class="page-head"><h1>Overview</h1>
    <div class="head-actions"><a class="btn primary" href="#/library">Import manuscripts</a><a class="btn" href="#/queue">Open queue</a></div></div>
  ${!d.worker.online ? html`<div class="callout warn" role="alert"><strong>The GPU worker is offline.</strong> Queued books will start when it is running.
     It restarts automatically; you can also restart it on <a href="#/about">System &amp; Licenses</a>.</div>` : ""}
  <section aria-labelledby="sum-h"><h2 id="sum-h" class="sr-only">Summary</h2>
  <div class="stats">
    ${card("Books", c.books)}
    ${card("Queued", c.queued, null, c.queued ? "info" : "")}
    ${card("Running", c.running, d.worker.online ? (d.worker.state === "idle" ? "worker idle" : "worker busy") : "worker offline", c.running ? "info" : "")}
    ${card("Paused", c.paused, null, c.paused ? "warn" : "")}
    ${card("Completed jobs", c.completed)}
    ${card("Failed jobs", c.failed, null, c.failed ? "bad" : "")}
    ${card("Segments produced", `${c.segments_produced.toLocaleString()} / ${c.segments_total.toLocaleString()}`, html`${progressBar(segFrac, "Segments produced")}`)}
    ${card("QA flags open", c.qa_flags, c.qa_flags ? "need a human listen" : "none open", c.qa_flags ? "warn" : "good")}
    ${card("Approved projects", c.approved_projects, null, c.approved_projects ? "approved" : "")}
    ${card("Audio produced", `${c.draft_audio_hours.toFixed(2)} h`, `${c.mastered_audio_hours.toFixed(2)} h mastered into chapters`)}
  </div></section>

  <section class="card" aria-labelledby="states-h">
    <h2 id="states-h">Segment lifecycle</h2>
    <ul class="legend">
      <li><span class="badge tone-info">Draft audio rendered</span> <strong>${st.draft_rendered.toLocaleString()}</strong> segments have a take</li>
      <li><span class="badge tone-good">QA passed</span> <strong>${st.qa_passed.toLocaleString()}</strong> passed automatic ASR + acoustic checks</li>
      <li><span class="badge tone-warn">Human review required</span> <strong>${st.needs_review.toLocaleString()}</strong> flagged after the retry budget</li>
      <li><span class="badge tone-approved">Approved</span> <strong>${st.approved_segments.toLocaleString()}</strong> segments approved by a reviewer; ${c.approved_projects} book(s) signed off for publication</li>
    </ul>
    <p class="muted small">Automatic QA is evidence, not proof: Whisper agreement does not guarantee correct pronunciation or a compelling performance. Release requires a human signoff.</p>
  </section>

  <div class="grid-ov">
  <section class="card" aria-labelledby="recent-h">
    <h2 id="recent-h">Recent projects</h2>
    ${d.recent.length ? html`<div class="table-wrap"><table class="table recent">
      <thead><tr><th scope="col">Title</th><th scope="col">Narrator</th><th scope="col">Progress</th><th scope="col">Duration</th><th scope="col">State</th></tr></thead>
      <tbody>${d.recent.map((b) => html`<tr>
        <td><a href="#/book/${b.id}"><strong>${b.title}</strong></a><div class="muted small">${b.source || ""} · imported ${fmtDate(b.created_at)}</div>
            ${liveLine(b)}${b.last_error ? html`<div class="err small">Last error: ${b.last_error}</div>` : ""}</td>
        <td>${b.voice || "–"}</td>
        <td class="nowrap">${progressBar(b.progress, `${b.title} progress`)}<div class="small muted">${b.produced}/${b.segments} segments${b.flagged ? html` · <span class="warn-text">${b.flagged} flagged</span>` : ""}</div></td>
        <td class="small nowrap">est. ${fmtDuration(b.est_duration_s)}<br>actual ${b.audio_duration_s ? fmtDuration(b.audio_duration_s) : "–"}</td>
        <td>${badge(b.state)}</td></tr>`)}</tbody></table></div>`
      : html`<div class="empty"><p>No books yet.</p><a class="btn primary" href="#/library">Import your first manuscript</a></div>`}
  </section>

  <section class="card" aria-labelledby="att-h">
    <h2 id="att-h">Needs attention</h2>
    ${d.failed_jobs.length ? html`<ul class="attention">${d.failed_jobs.map((f) => html`<li class="att-bad"><a href="#/queue"><strong>${f.title}</strong> – generation failed</a><div class="small">${f.error || ""}</div></li>`)}</ul>` : ""}
    ${d.flags.length ? html`<ul class="attention">${d.flags.map((f) => html`<li><a href="#/review/${f.book_id}?seg=${f.id}&filter=flagged"><strong>${f.title}</strong> · ch. ${f.chapter_idx} · <code>${f.sid}</code></a>
        <div class="small">${f.flag_reason || "Flagged by automatic QA"}</div></li>`)}</ul>`
      : (!d.failed_jobs.length ? html`<p class="muted">Nothing needs attention right now.</p>` : "")}
    <h3>Activity</h3>
    <ol class="feed">${d.events.map((e) => html`<li class="lvl-${e.level}"><span class="muted small">${fmtAgo(e.created_at)}</span> ${e.message}</li>`)}</ol>
  </section></div>`);
}

export async function render(view) {
  draw(view, await get("/api/overview"));
  return onState(async () => {
    try { draw(view, await get("/api/overview")); } catch (e) { if (e.status) toastError(e); }
  }, 2000);
}
