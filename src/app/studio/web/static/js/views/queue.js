// Durable FIFO queue: reorder (drag or keyboard buttons), pause/resume/retry/cancel, logs, estimates.
import { get, post, html, setHTML, badge, progressBar, fmtDuration, fmtAgo, toast, busy, onState, confirmDialog } from "../ui.js";

function etaText(j, tp) {
  if (j.status === "paused" || j.status === "failed") return "–";
  if (j.eta_s === null || j.eta_s === undefined) {
    return tp.samples < 20 ? html`<span class="muted small">estimate after ${20 - tp.samples} more segments</span>` : "–";
  }
  return html`≈ ${fmtDuration(j.eta_s)}<div class="small muted">finishes in ≈ ${fmtDuration(j.eta_finish_s)}</div>`;
}

function jobRow(j, tp, movable) {
  const active = ["queued", "running", "paused"].includes(j.status);
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
      <details class="joblog" data-id="${j.id}"><summary>Log</summary><div class="log-box" id="log-${j.id}">Loading…</div></details>
    </div>
    <div class="q-eta">${etaText(j, tp)}</div>
    <div class="q-actions">
      ${movable ? html`<div class="btn-group" role="group" aria-label="Reorder ${j.title}">
        <button class="btn small ghost" data-act="top" data-id="${j.id}" title="Move to top" aria-label="Move ${j.title} to top">⤒</button>
        <button class="btn small ghost" data-act="up" data-id="${j.id}" aria-label="Move ${j.title} up">↑</button>
        <button class="btn small ghost" data-act="down" data-id="${j.id}" aria-label="Move ${j.title} down">↓</button></div>` : ""}
      ${j.status === "running" && !j.pause_requested ? html`<button class="btn small" data-act="pause" data-id="${j.id}">Pause</button>` : ""}
      ${j.status === "queued" ? html`<button class="btn small" data-act="pause" data-id="${j.id}">Hold</button>` : ""}
      ${j.status === "paused" || j.pause_requested ? html`<button class="btn small primary" data-act="resume" data-id="${j.id}">Resume</button>` : ""}
      ${j.status === "failed" ? html`<button class="btn small primary" data-act="retry" data-id="${j.id}">Retry</button>` : ""}
      ${active && !j.cancel_requested ? html`<button class="btn small danger ghost" data-act="cancel" data-id="${j.id}">Cancel</button>` : ""}
    </div></li>`;
}

export async function render(view) {
  let data = await get("/api/queue");
  let dragId = null;

  const paint = () => {
    const tp = data.throughput;
    const active = data.jobs.filter((j) => ["queued", "running", "paused"].includes(j.status));
    const done = data.jobs.filter((j) => !["queued", "running", "paused"].includes(j.status));
    const openLogs = [...view.querySelectorAll("details.joblog[open]")].map((d) => d.dataset.id);
    setHTML(view, html`<div class="page-head"><h1>Queue</h1></div>
      <p class="muted">Books are narrated one segment at a time on a single GPU worker, in the order below. Other books keep going if one fails.
      Pausing takes effect between segments because a running MLX call cannot be interrupted safely.
      ${tp.samples ? html` Median time per segment: <strong>${tp.median_s}s</strong> (${tp.samples} measured).` : ""}</p>
      <section class="card" aria-labelledby="act-h"><h2 id="act-h">Active (${active.length})</h2>
        ${active.length ? html`<ol class="qlist" id="qlist">${active.map((j) => jobRow(j, tp, j.status !== "running"))}</ol>`
          : html`<p class="muted">Nothing queued. Add books from the <a href="#/library">Library</a>.</p>`}
      </section>
      <section class="card" aria-labelledby="util-h"><h2 id="util-h">Assembly, export &amp; preview jobs</h2>
        ${data.utility.length ? html`<div class="table-wrap"><table class="table compact"><thead><tr><th scope="col">Job</th><th scope="col">Book</th><th scope="col">Status</th><th scope="col">When</th></tr></thead>
          <tbody>${data.utility.map((u) => html`<tr><td>#${u.id} ${u.kind}${u.payload && u.payload.kind ? ` (${u.payload.kind})` : ""}</td><td>${u.title || "–"}</td>
          <td>${badge(u.status)}${u.error ? html`<div class="small err">${u.error}</div>` : ""}</td><td class="small">${fmtAgo(u.finished_at || u.created_at)}</td></tr>`)}</tbody></table></div>`
          : html`<p class="muted">None yet.</p>`}</section>
      <section class="card" aria-labelledby="done-h"><h2 id="done-h">Recently finished (7 days)</h2>
        ${done.length ? html`<ol class="qlist">${done.map((j) => jobRow(j, tp, false))}</ol>` : html`<p class="muted">None.</p>`}</section>`);
    openLogs.forEach((id) => { const d = view.querySelector(`details.joblog[data-id="${id}"]`); if (d) { d.open = true; loadLog(id); } });
  };

  async function loadLog(id) {
    const box = view.querySelector(`#log-${id}`);
    if (!box) return;
    const { logs } = await get(`/api/jobs/${id}/log`);
    setHTML(box, logs.length ? html`<ol>${logs.map((l) => html`<li class="lvl-${l.level}"><span class="muted">${fmtAgo(l.created_at)}</span> ${l.message}</li>`)}</ol>` : html`<p class="muted">No entries.</p>`);
  }

  const refresh = async () => { data = await get("/api/queue"); paint(); };

  async function onClick(e) {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const id = Number(b.dataset.id), act = b.dataset.act;
    if (["up", "down", "top"].includes(act)) {
      data = await busy(b, () => post(`/api/jobs/${id}/move`, { direction: act }));
      paint();
      const again = view.querySelector(`button[data-act="${act}"][data-id="${id}"]`); if (again) again.focus();
      return;
    }
    if (act === "cancel" && !(await confirmDialog("Cancel job?", "Generated segments are kept; the book can be queued again later.", "Cancel job", true))) return;
    await busy(b, () => post(`/api/jobs/${id}/${act}`), { pause: "Pause requested", resume: "Resumed", retry: "Queued for retry", cancel: "Cancelled" }[act]);
    await refresh();
  }
  function onToggle(e) {
    const d = e.target;
    if (d.matches && d.matches("details.joblog") && d.open) loadLog(d.dataset.id);
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
    data = await post("/api/queue/reorder", { job_ids: ids });
    toast("Queue order saved", "success", 2000);
    paint();
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
    off();
    view.removeEventListener("click", onClick); view.removeEventListener("toggle", onToggle, true);
    view.removeEventListener("dragstart", onDragStart); view.removeEventListener("dragover", onDragOver);
    view.removeEventListener("drop", onDrop); view.removeEventListener("dragend", onDragEnd);
  };
}
