// Overview & progress dashboard. The page is a fixed shell with live regions (cards, strip, recent books, attention list, activity feed,
// worker and update notices); each region is repainted in place only when its markup changed, so scroll positions and focus survive.
import { get, html, setHTML, patchRegions, badge, progressBar, fmtDuration, fmtAgo, fmtDate, onState, toastError } from "../ui.js";
import { fmtRuntime } from "../runtime.js";

const ATTENTION_CAP = 5;
let attentionShowAll = false; // kept in module memory so it survives navigation and repaints

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

function updateNotice(u) {
  if (!u || !u.managed) return "";
  if (u.state === "failed" || u.state === "rolled_back") return html`<div class="callout warn" role="alert"><strong>Update problem.</strong> ${u.message} <a href="#/updates">Open Updates</a></div>`;
  if (u.state === "staged") return html`<div class="callout info" role="status"><strong>Version ${u.staged_version} is downloaded and verified.</strong> <a href="#/updates">Install it</a> when you are ready.</div>`;
  if (u.state === "available") return html`<div class="callout info" role="status"><strong>Version ${u.latest_version} is available.</strong> <a href="#/updates">See what is new</a></div>`;
  if (["waiting", "draining", "installing", "restarting", "health_check", "downloading", "verifying", "staging"].includes(u.state))
    return html`<div class="callout info" role="status"><strong>Update in progress:</strong> ${u.message} <a href="#/updates">Details</a></div>`;
  return "";
}

function workerNotice(w) {
  if (w.online) return "";
  return html`<div class="callout warn" role="alert"><strong>The GPU worker is offline.</strong> Queued books will start when it is running.
     It restarts automatically; you can also restart it on <a href="#/about">System &amp; Licenses</a>.</div>`;
}

function cards(d) {
  const c = d.cards;
  const segFrac = c.segments_total ? c.segments_produced / c.segments_total : 0;
  return html`<div class="stats">
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
  </div>`;
}

function strip(d) {
  const st = d.segment_states, c = d.cards;
  return html`<ul class="legend">
      <li><span class="badge tone-info">Draft audio rendered</span> <strong>${st.draft_rendered.toLocaleString()}</strong> segments have a take</li>
      <li><span class="badge tone-good">QA passed</span> <strong>${st.qa_passed.toLocaleString()}</strong> passed automatic ASR + acoustic checks</li>
      <li><span class="badge tone-warn">Human review required</span> <strong>${st.needs_review.toLocaleString()}</strong> flagged after the retry budget</li>
      <li><span class="badge tone-approved">Approved</span> <strong>${st.approved_segments.toLocaleString()}</strong> segments approved by a reviewer; ${c.approved_projects} book(s) signed off for publication</li>
    </ul>`;
}

function recent(d) {
  if (!d.recent.length) return html`<div class="empty"><p>No books yet.</p><a class="btn primary" href="#/library">Import your first manuscript</a></div>`;
  return html`<div class="table-wrap"><table class="table recent">
      <thead><tr><th scope="col">Title</th><th scope="col">Narrator</th><th scope="col">Progress</th><th scope="col">Audio length</th><th scope="col">State</th></tr></thead>
      <tbody>${d.recent.map((b) => { const rt = fmtRuntime(b.runtime); return html`<tr>
        <td><a href="#/book/${b.id}"><strong>${b.display_title}</strong></a><div class="muted small">${b.source || ""} · imported ${fmtDate(b.created_at)}</div>
            ${liveLine(b)}${b.last_error ? html`<div class="err small">Last error: ${b.last_error}</div>` : ""}</td>
        <td>${b.voice || "–"}</td>
        <td class="nowrap">${progressBar(b.progress, `${b.display_title} progress`)}<div class="small muted">${b.produced}/${b.segments} segments${b.flagged ? html` · <span class="warn-text">${b.flagged} flagged</span>` : ""}</div>
          ${rt.toString() ? html`<div class="small rt-line">${rt}</div>` : ""}</td>
        <td class="small nowrap">est. ${fmtDuration(b.est_duration_s)}<br>actual ${b.audio_duration_s ? fmtDuration(b.audio_duration_s) : "–"}</td>
        <td>${badge(b.state)}</td></tr>`; })}</tbody></table></div>`;
}

/** The list only shows each book's LATEST failed job (the server list is newest first, so keep the first row per book). */
export function latestFailures(failed) {
  const seen = new Set();
  return failed.filter((f) => (seen.has(f.book_id) ? false : (seen.add(f.book_id), true)));
}

function attention(d) {
  const failed = latestFailures(d.failed_jobs);
  const items = [
    ...failed.map((f) => ({ bad: true, f })),
    ...d.flags.map((f) => ({ bad: false, f })),
  ];
  if (!items.length) return html`<p class="muted">Nothing needs attention right now.</p>`;
  const shown = attentionShowAll ? items : items.slice(0, ATTENTION_CAP);
  const li = ({ bad, f }) => bad
    ? html`<li class="att-bad"><a href="#/queue"><strong>${f.title}</strong> – generation failed</a><div class="small">${f.error || ""}</div></li>`
    : html`<li><a href="#/review/${f.book_id}?seg=${f.id}&filter=flagged"><strong>${f.title}</strong> · ch. ${f.chapter_idx} · <code>${f.sid}</code></a>
        <div class="small">${f.flag_reason || "Flagged by automatic QA"}</div></li>`;
  return html`<ul class="attention ${attentionShowAll ? "att-scroll" : ""}" id="ov-att-list">${shown.map(li)}</ul>
    ${items.length > ATTENTION_CAP ? html`<button type="button" class="btn small" id="ov-att-toggle" data-act="att-toggle" aria-expanded="${attentionShowAll ? "true" : "false"}">${attentionShowAll ? "Show fewer" : `Show all (${items.length})`}</button>` : ""}`;
}

export async function render(view, parts, query, nav) {
  const signal = nav && nav.signal;
  const [updateSummary0, first] = await Promise.all([get("/api/update/summary", { signal }).catch((e) => { if (e.name === "AbortError") throw e; return null; }), get("/api/overview", { signal })]);
  let updateSummary = updateSummary0, data = first;
  let feedSeen = Math.max(0, ...first.events.map((e) => e.id || 0)); // newest event id seen while the feed was at the top

  setHTML(view, html`
  <div class="page-head"><h1>Overview</h1>
    <div class="head-actions"><a class="btn primary" href="#/library">Import manuscripts</a><a class="btn" href="#/queue">Open queue</a></div></div>
  <div id="ov-update"></div>
  <div id="ov-worker"></div>
  <section aria-labelledby="sum-h"><h2 id="sum-h" class="sr-only">Summary</h2><div id="ov-cards"></div></section>
  <section class="card" aria-labelledby="states-h">
    <h2 id="states-h">Segment lifecycle</h2>
    <div id="ov-states"></div>
    <p class="muted small">Automatic QA is evidence, not proof: Whisper agreement does not guarantee correct pronunciation or a compelling performance. Release requires a human signoff.</p>
  </section>
  <div class="grid-ov">
  <section class="card" aria-labelledby="recent-h">
    <h2 id="recent-h">Recent projects</h2>
    <div id="ov-recent"></div>
  </section>
  <section class="card" aria-labelledby="att-h">
    <h2 id="att-h">Needs attention</h2>
    <div id="ov-attention"></div>
    <h3>Activity</h3>
    <div id="ov-activity"></div>
  </section></div>`);

  const feedAway = () => { const el = view.querySelector("#ov-feed"); return !!el && el.scrollTop > 8; };
  function activity() {
    if (!feedAway()) feedSeen = Math.max(feedSeen, ...data.events.map((e) => e.id || 0));
    const fresh = data.events.filter((e) => (e.id || 0) > feedSeen).length;
    return html`<div class="feed-hint">${fresh ? html`<button type="button" class="btn small feed-new" id="ov-feed-new" data-act="feed-top">${fresh} new event${fresh === 1 ? "" : "s"} – back to top</button>` : ""}</div>
      <ol class="feed" id="ov-feed" tabindex="0" aria-label="Recent activity">${data.events.map((e) => html`<li class="lvl-${e.level}"><span class="muted small">${fmtAgo(e.created_at)}</span> ${e.message}</li>`)}</ol>`;
  }
  const paintAll = () => patchRegions(view, {
    "#ov-update": () => updateNotice(updateSummary),
    "#ov-worker": () => workerNotice(data.worker),
    "#ov-cards": () => cards(data),
    "#ov-states": () => strip(data),
    "#ov-recent": () => recent(data),
    "#ov-attention": () => attention(data),
    "#ov-activity": () => activity(),
  });
  const paintActivity = () => patchRegions(view, { "#ov-activity": () => activity() });
  paintAll();

  const onClick = (e) => {
    const t = e.target.closest && e.target.closest("[data-act]");
    if (!t || !view.contains(t)) return;
    if (t.dataset.act === "att-toggle") { attentionShowAll = !attentionShowAll; paintAll(); }
    else if (t.dataset.act === "feed-top") {
      const feed = view.querySelector("#ov-feed");
      if (feed) feed.scrollTop = 0;
      feedSeen = Math.max(feedSeen, ...data.events.map((x) => x.id || 0));
      paintActivity();
      if (feed) feed.focus({ preventScroll: true });
    }
  };
  // Scrolling the feed back to the top clears the "new" hint (scroll does not bubble: listen in the capture phase).
  const onScroll = (e) => { if (e.target && e.target.id === "ov-feed") paintActivity(); };
  view.addEventListener("click", onClick);
  view.addEventListener("scroll", onScroll, true);

  let n = 0, inflight = false, stopped = false, dirty = false;
  const ctl = new AbortController();
  async function refresh() {
    if (stopped) return;
    if (document.hidden) { dirty = true; return; }
    if (inflight) { dirty = true; return; }
    inflight = true; dirty = false;
    try {
      const [u, d] = await Promise.all([
        n++ % 5 === 0 ? get("/api/update/summary", { signal: ctl.signal }).catch(() => updateSummary) : Promise.resolve(updateSummary),
        get("/api/overview", { signal: ctl.signal }),
      ]);
      if (stopped) return;
      updateSummary = u; data = d;
      paintAll();
    } catch (e) { if (e.status) toastError(e); }
    finally { inflight = false; }
  }
  const onVisible = () => { if (!document.hidden && dirty) refresh(); };
  document.addEventListener("visibilitychange", onVisible);
  const off = onState(refresh, 2000);
  return () => {
    stopped = true; ctl.abort(); off();
    view.removeEventListener("click", onClick);
    view.removeEventListener("scroll", onScroll, true);
    document.removeEventListener("visibilitychange", onVisible);
  };
}
