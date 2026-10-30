// Durable FIFO queue: reorder (drag or keyboard buttons), pause/resume/retry/cancel, logs, time estimates, one "recently finished" row per
// book (touch-ups folded into "+N earlier runs"), and paginated finished / utility cards. Live regions are patched in place.
import { get, post, html, setHTML, patchRegions, pager, wirePager, badge, progressBar, fmtAgo, qs, toast, busy, onState, confirmDialog } from "../ui.js";
import { fmtRuntime, fmtSpan } from "../runtime.js";

const PAGE = 10;

function activeRow(j, movable, logs) {
  return html`<li class="qrow st-${j.status}" data-id="${j.id}" ${movable ? html`draggable="true"` : ""}>
    ${movable ? html`<span class="grip" aria-hidden="true" title="Drag to reorder">⠿</span>` : html`<span class="grip-pad"></span>`}
    <div class="q-main">
      <div class="q-title"><a href="#/book/${j.book_id}"><strong>${j.title}</strong></a> ${badge(j.status)}
        ${j.pause_requested ? html`<span class="badge tone-warn">pausing after this segment</span>` : ""}
        ${j.cancel_requested ? html`<span class="badge tone-bad">cancelling after this segment</span>` : ""}</div>
      ${progressBar(j.progress, `${j.title} progress`)}
      <div class="small muted">${j.segments - j.remaining}/${j.segments} segments · ${j.flagged} flagged · ${j.segment_retries} retries
        ${j.current_segment_sid && j.status === "running" ? html` · now <code>${j.current_segment_sid}</code>` : ""}
        ${j.retries ? html` · job retried ${j.retries}×` : ""}</div>
      ${j.paused_reason ? html`<div class="small warn-text">Paused: ${j.paused_reason}</div>` : ""}
      ${j.error ? html`<div class="small err">${j.error}</div>` : ""}
      ${j.last_log ? html`<div class="small muted">Last: ${j.last_log.message} (${fmtAgo(j.last_log.created_at)})</div>` : ""}
      ${logDetails(j.id, logs)}
    </div>
    <div class="q-eta">${fmtRuntime(j.runtime)}</div>
    <div class="q-actions">
      ${movable ? html`<div class="btn-group" role="group" aria-label="Reorder ${j.title}">
        <button class="btn small ghost" id="qb-top-${j.id}" data-act="top" data-id="${j.id}" title="Move to top" aria-label="Move ${j.title} to top">⤒</button>
        <button class="btn small ghost" id="qb-up-${j.id}" data-act="up" data-id="${j.id}" aria-label="Move ${j.title} up">↑</button>
        <button class="btn small ghost" id="qb-down-${j.id}" data-act="down" data-id="${j.id}" aria-label="Move ${j.title} down">↓</button></div>` : ""}
      ${j.status === "running" && !j.pause_requested ? html`<button class="btn small" id="qb-pause-${j.id}" data-act="pause" data-id="${j.id}">Pause</button>` : ""}
      ${j.status === "queued" ? html`<button class="btn small" id="qb-pause-${j.id}" data-act="pause" data-id="${j.id}">Hold</button>` : ""}
      ${j.status === "paused" || j.pause_requested ? html`<button class="btn small primary" id="qb-resume-${j.id}" data-act="resume" data-id="${j.id}">Resume</button>` : ""}
      ${!j.cancel_requested ? html`<button class="btn small danger ghost" id="qb-cancel-${j.id}" data-act="cancel" data-id="${j.id}">Cancel</button>` : ""}
    </div></li>`;
}

/** The log disclosure. Both ids are stable and the content comes from a cache, so a live repaint keeps it open, scrolled and filled. */
function logDetails(id, logs) {
  const entries = logs.get(id);
  const body = entries === undefined ? "Loading…"
    : entries.length ? html`<ol>${entries.map((l) => html`<li class="lvl-${l.level}"><span class="muted">${fmtAgo(l.created_at)}</span> ${l.message}</li>`)}</ol>` : html`<p class="muted">No entries.</p>`;
  return html`<details class="joblog" id="joblog-${id}" data-id="${id}"><summary>Log</summary><div class="log-box" id="log-${id}">${body}</div></details>`;
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

function runLine(r) {
  const what = r.status === "completed" ? r.kind_hint : `${r.kind_hint} (${r.status})`;
  return html`<li><strong>${what}</strong> · ${fmtAgo(r.finished_at)} · ${plural(r.segments_rendered, "segment")} · ${fmtSpan(r.active_seconds)}</li>`;
}

function doneRow(j, logs) {
  const n = (j.earlier_runs || []).length, latest = j.latest_run;
  const timing = j.status === "completed" ? fmtRuntime(j.runtime)
    : html`<span class="rt rt-finished">${j.status === "failed" ? "failed after" : "cancelled after"} ${fmtSpan(j.active_seconds)}</span>`;
  return html`<li class="qrow qrow-done st-${j.status}" data-id="${j.id}">
    <span class="grip-pad"></span>
    <div class="q-main">
      <div class="q-title"><a href="#/book/${j.book_id}"><strong>${j.title}</strong></a> ${badge(j.status)}
        <span class="small muted">${fmtAgo(j.finished_at)}${latest ? ` · last run: ${latest.kind_hint}, ${plural(latest.segments_rendered, "segment")}` : ""}</span></div>
      <div class="small muted">${j.segments - j.remaining}/${j.segments} segments · ${j.flagged} flagged</div>
      ${j.error ? html`<div class="small err">${j.error}</div>` : ""}
      ${n ? html`<details class="earlier" id="earlier-${j.book_id}"><summary>+${n} earlier ${n === 1 ? "run" : "runs"}</summary>
        <ul class="earlier-list">${j.earlier_runs.map(runLine)}</ul></details>` : ""}
      ${logDetails(j.id, logs)}
    </div>
    <div class="q-eta">${timing}</div>
    <div class="q-actions">${j.status === "failed" ? html`<button class="btn small primary" id="qb-retry-${j.id}" data-act="retry" data-id="${j.id}">Retry</button>` : ""}</div></li>`;
}

function utilRow(u) {
  return html`<tr><td>#${u.id} ${u.kind === "analyze" ? "Narrative analysis" : u.kind}${u.kind === "preview" && u.payload && u.payload.segment_id ? " (segment)" : ""}${u.payload && u.payload.kind ? ` (${u.payload.kind})` : ""}</td>
    <td>${u.book_id && u.title ? html`<a href="#/book/${u.book_id}">${u.title}</a>` : (u.title || "–")}</td>
    <td>${badge(u.status)}${u.error ? html`<div class="small err">${u.error}</div>` : ""}</td><td class="small">${fmtAgo(u.finished_at || u.created_at)}</td></tr>`;
}

export async function render(view) {
  let finishedOffset = 0, utilityOffset = 0, dragId = null, gone = false;
  const logs = new Map();   // job id -> log entries (so repaints never flash "Loading…")
  const url = () => "/api/queue" + qs({ finished_offset: finishedOffset, finished_limit: PAGE, utility_offset: utilityOffset, utility_limit: PAGE });
  let data = await get(url());

  setHTML(view, html`<div class="page-head"><h1>Queue</h1></div>
    <p class="muted" id="q-intro"></p>
    <section class="card" id="q-active" aria-labelledby="act-h"></section>
    <section class="card" id="q-util" aria-labelledby="util-h"></section>
    <section class="card" id="q-done" aria-labelledby="done-h"></section>`);

  const paint = () => {
    const tp = data.throughput || {};
    patchRegions(view, {
      "#q-intro": () => html`Books are narrated one segment at a time on a single GPU worker, in the order below. Other books keep going if one fails.
        Pausing takes effect between segments because a running MLX call cannot be interrupted safely.
        ${tp.samples ? html` Median time per segment: <strong>${tp.median_s}s</strong> (${tp.samples} measured).` : ""}
        Time estimates use the speed measured for each book's narrator model and leave out time spent waiting or paused.`,
      "#q-active": () => html`<h2 id="act-h">Active (${data.active.length})</h2>
        ${data.active.length ? html`<ol class="qlist" id="qlist">${data.active.map((j) => activeRow(j, j.status !== "running", logs))}</ol>`
          : html`<p class="muted">Nothing queued. Add books from the <a href="#/library">Library</a>.</p>`}`,
      "#q-util": () => html`<h2 id="util-h">Assembly, export, preview &amp; analysis jobs</h2>
        ${data.utility.length ? html`<div class="table-wrap"><table class="table compact"><thead><tr><th scope="col">Job</th><th scope="col">Book</th><th scope="col">Status</th><th scope="col">When</th></tr></thead>
          <tbody>${data.utility.map(utilRow)}</tbody></table></div>${pager(data.utility_total, data.utility_offset, data.utility_limit)}`
          : html`<p class="muted">None yet.</p>`}`,
      "#q-done": () => html`<h2 id="done-h">Recently finished (${data.window_days || 30} days)</h2>
        <p class="small muted">One row per book: its latest run. Small edits such as regenerating a segment are listed under "earlier runs".</p>
        ${data.finished.length ? html`<ol class="qlist" id="qdone">${data.finished.map((j) => doneRow(j, logs))}</ol>${pager(data.finished_total, data.finished_offset, data.finished_limit)}`
          : html`<p class="muted">None.</p>`}`,
    });
  };

  async function loadLog(id) {
    try {
      const r = await get(`/api/jobs/${id}/log`);
      if (gone) return;
      const prev = logs.get(id);
      if (prev && JSON.stringify(prev) === JSON.stringify(r.logs)) return;
      logs.set(id, r.logs);
      paint();
    } catch { /* the log is advisory */ }
  }
  const reloadOpenLogs = () => view.querySelectorAll("details.joblog[open]").forEach((d) => loadLog(Number(d.dataset.id)));

  async function refresh() {
    let d = await get(url());
    // a page that no longer exists (jobs aged out or were removed): step back to the last page that does
    if (d.finished_offset > 0 && d.finished.length === 0 && d.finished_total > 0) { finishedOffset = Math.floor((d.finished_total - 1) / d.finished_limit) * d.finished_limit; d = await get(url()); }
    if (d.utility_offset > 0 && d.utility.length === 0 && d.utility_total > 0) { utilityOffset = Math.floor((d.utility_total - 1) / d.utility_limit) * d.utility_limit; d = await get(url()); }
    if (gone) return;
    data = d;
    paint();
    reloadOpenLogs();
  }

  const page = (which) => async (offset, dir) => {
    if (which === "finished") finishedOffset = offset; else utilityOffset = offset;
    await refresh();
    const sec = view.querySelector(which === "finished" ? "#q-done" : "#q-util");
    const again = sec && (sec.querySelector(`button[data-pager="${dir}"]:not([disabled])`) || sec.querySelector("button[data-pager]:not([disabled])"));
    if (again) again.focus({ preventScroll: true });
  };
  const unwire = [wirePager(view.querySelector("#q-done"), page("finished")), wirePager(view.querySelector("#q-util"), page("utility"))];

  async function onClick(e) {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const id = Number(b.dataset.id), act = b.dataset.act;
    if (["up", "down", "top"].includes(act)) {
      await busy(b, () => post(`/api/jobs/${id}/move`, { direction: act }));
      await refresh();
      const again = view.querySelector(`#qb-${act}-${id}`); if (again) again.focus();
      return;
    }
    if (act === "cancel" && !(await confirmDialog("Cancel job?", "Generated segments are kept; the book can be queued again later.", "Cancel job", true))) return;
    await busy(b, () => post(`/api/jobs/${id}/${act}`), { pause: "Pause requested", resume: "Resumed", retry: "Queued for retry", cancel: "Cancelled" }[act]);
    await refresh();
  }
  function onToggle(e) {
    const d = e.target;
    if (d.matches && d.matches("details.joblog") && d.open && !logs.has(Number(d.dataset.id))) loadLog(Number(d.dataset.id));
  }
  function onDragStart(e) { const li = e.target.closest(".qrow[draggable=true]"); if (!li) return; dragId = Number(li.dataset.id); li.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; }
  function onDragOver(e) { if (dragId === null) return; const li = e.target.closest(".qrow"); if (li) { e.preventDefault(); view.querySelectorAll(".drop-target").forEach((x) => x.classList.remove("drop-target")); li.classList.add("drop-target"); } }
  async function onDrop(e) {
    if (dragId === null) return;
    e.preventDefault();
    const target = e.target.closest(".qrow");
    const ids = [...view.querySelectorAll("#qlist .qrow[draggable=true]")].map((x) => Number(x.dataset.id)).filter((x) => x !== dragId);
    const at = target ? ids.indexOf(Number(target.dataset.id)) : ids.length;
    ids.splice(at < 0 ? ids.length : at, 0, dragId);
    dragId = null;
    await post("/api/queue/reorder", { job_ids: ids });
    toast("Queue order saved", "success", 2000);
    await refresh();
  }
  function onDragEnd() { dragId = null; view.querySelectorAll(".dragging,.drop-target").forEach((x) => x.classList.remove("dragging", "drop-target")); }

  view.addEventListener("click", onClick);
  view.addEventListener("toggle", onToggle, true);
  view.addEventListener("dragstart", onDragStart);
  view.addEventListener("dragover", onDragOver);
  view.addEventListener("drop", onDrop);
  view.addEventListener("dragend", onDragEnd);
  paint();
  const off = onState(() => { if (dragId === null) refresh().catch(() => {}); }, 2500);
  return () => {
    gone = true;
    off();
    unwire.forEach((u) => u());
    view.removeEventListener("click", onClick); view.removeEventListener("toggle", onToggle, true);
    view.removeEventListener("dragstart", onDragStart); view.removeEventListener("dragover", onDragOver);
    view.removeEventListener("drop", onDrop); view.removeEventListener("dragend", onDragEnd);
  };
}
