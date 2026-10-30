// Update Center: check / download / verify / stage / install (after the current job or at the next segment boundary) / restart, with history.
// Everything the user sees is plain text (auto-escaped); release notes are never interpreted as HTML.
import { get, post, put, html, setHTML, busy, confirmDialog, toast, toastError, fmtAgo, fmtDate, fmtBytes } from "../ui.js";

const PHASES = [
  ["check", "Checking"], ["download", "Downloading"], ["verify", "Verifying signature & checksum"], ["stage", "Staging"],
  ["drain", "Waiting for the queue"], ["install", "Installing"], ["restart", "Restarting"], ["health", "Health check"],
];

const STATE = {
  idle: ["neutral", "Not checked yet"], checking: ["info", "Checking"], up_to_date: ["good", "Up to date"], available: ["info", "Update available"],
  downloading: ["info", "Downloading"], verifying: ["info", "Verifying"], staging: ["info", "Staging"], staged: ["good", "Ready to install"],
  waiting: ["warn", "Waiting for a safe point"], draining: ["warn", "Finishing the current segment"], installing: ["info", "Installing"],
  restarting: ["info", "Restarting"], health_check: ["info", "Health check"], succeeded: ["good", "Updated"], failed: ["bad", "Failed"],
  rolled_back: ["warn", "Rolled back"], disabled: ["neutral", "Switched off"], incompatible: ["warn", "Not compatible"], offline: ["warn", "Offline"],
};
const BUSY = new Set(["checking", "downloading", "verifying", "staging", "waiting", "draining", "installing", "restarting", "health_check"]);

function phaseRow([key, label], p) {
  const state = p ? p.state : "idle";
  const pct = state === "done" ? 100 : (p && p.pct != null ? p.pct : (state === "running" ? 15 : 0));
  const tone = state === "failed" ? "bad" : state === "done" ? "good" : state === "running" ? "info" : "neutral";
  const text = state === "done" ? "Done" : state === "failed" ? "Failed" : state === "running" ? "In progress" : "Waiting";
  return html`<li class="phase phase-${tone}">
    <div class="phase-head"><span>${label}</span><span class="small muted">${text}${p && p.detail ? html` · ${p.detail}` : ""}</span></div>
    <div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(pct)}" aria-label="${label}">
      <div class="progress-fill" data-pct="${pct}"></div></div></li>`;
}

function guidance(s) {
  if (!s.managed) return html`<div class="callout warn" role="status"><strong>This copy cannot update itself.</strong> ${s.not_managed_reason}</div>`;
  if (!s.signing_configured) return html`<div class="callout warn" role="status"><strong>Updates are switched off until the owner publishes a signing key.</strong>
    Every update must carry a signature that this installation can verify against a pinned public key; none is pinned yet, so nothing is downloaded or installed.</div>`;
  if (s.state === "offline") return html`<div class="callout warn" role="status"><strong>No connection to the update server.</strong> ${s.error} Nothing was changed; the studio keeps working.</div>`;
  if (s.state === "failed") return html`<div class="callout bad" role="alert"><strong>${s.error_kind === "untrusted" ? "The update could not be trusted." : "The update failed."}</strong> ${s.error || s.message}
    ${s.error_kind === "preflight" ? html`<br>Check that the data and application folders are reachable (an external disk may be disconnected) and try again.` : ""}</div>`;
  if (s.state === "rolled_back") return html`<div class="callout warn" role="alert"><strong>The update was rolled back.</strong> ${s.message} Your books, voices and queue were not changed.</div>`;
  if (s.state === "incompatible") return html`<div class="callout warn" role="status"><strong>A newer version exists but cannot be installed here.</strong> ${s.message}</div>`;
  if (s.state === "succeeded") return html`<div class="callout good" role="status"><strong>${s.message}</strong></div>`;
  if (s.queue_hold) return html`<div class="callout info" role="status">The queue is held for an update (${s.queue_hold.mode === "now" ? "no new segments start" : "the current book finishes, nothing new starts"}). Completed audio is safe on disk.</div>`;
  return "";
}

// One block per release between the installed version and the offered one (each release's notes already cover versions that were
// never published separately). Older indexes carry no list: fall back to the offered release's own notes.
function releaseNotes(latest) {
  const list = (latest.notes_since_installed || []).filter((r) => r.notes && r.notes.trim());
  if (!list.length && latest.notes && latest.notes.trim()) list.push({ version: latest.version, notes: latest.notes });
  if (!list.length) return html`<p class="small muted">The notes for this release are not in the update index. Use the link below to read them on the release page.</p>`;
  return list.map((r) => html`<h3 class="notes-ver">Version ${r.version}</h3><pre class="notes">${r.notes}</pre>`);
}

function draw(view, s, hist, isAdmin) {
  const [tone, label] = STATE[s.state] || ["neutral", s.state];
  const busyNow = BUSY.has(s.state) || !!s.busy;
  const latest = s.latest;
  const canAct = isAdmin && s.managed && s.signing_configured && !busyNow;
  const staged = s.state === "staged" && s.staged_version;
  setHTML(view, html`
  <div class="page-head"><h1>Updates</h1><div class="head-actions">
    <button class="btn" data-act="check" ${canAct || (isAdmin && !busyNow) ? "" : "disabled"}>Check for updates</button></div></div>
  ${isAdmin ? "" : html`<div class="callout info small" role="note">You can see update status here. An administrator installs updates.</div>`}
  ${guidance(s)}
  <section class="card" aria-labelledby="ver-h"><h2 id="ver-h">Version</h2>
    <dl class="about-facts">
      <div><dt>Installed</dt><dd>${s.installed_version}${s.build ? html` <span class="muted small">(build ${s.build})</span>` : ""}</dd></div>
      <div><dt>Channel</dt><dd>${s.channel}</dd></div>
      <div><dt>Latest compatible</dt><dd>${latest ? latest.version : (s.state === "up_to_date" ? "You have it" : "–")}</dd></div>
      <div><dt>Status</dt><dd><span class="badge tone-${tone}">${label}</span></dd></div>
      <div><dt>Last checked</dt><dd>${s.last_check ? fmtAgo(s.last_check) : "never"}${s.last_success_check && s.last_success_check !== s.last_check ? html` <span class="muted small">(last success ${fmtAgo(s.last_success_check)})</span>` : ""}</dd></div>
      <div><dt>Previous version kept</dt><dd>${s.previous_version || "–"}</dd></div>
    </dl>
    <p class="small muted" aria-live="polite">${s.message || ""}</p>
  </section>
  ${latest ? html`<section class="card" aria-labelledby="new-h"><h2 id="new-h">Version ${latest.version}</h2>
    <p class="small muted">Published ${fmtDate(Date.parse(latest.published) / 1000)} · ${fmtBytes(latest.size)} download · needs macOS ${latest.min_os}+ ·
      ${latest.signed ? "release index signature verified" : "NOT signature-verified (development channel)"}</p>
    <details open><summary>What's new since ${s.installed_version}</summary>${releaseNotes(latest)}
      ${latest.notes_url ? html`<p class="small"><a href="${latest.notes_url}" target="_blank" rel="noopener noreferrer">View the full release notes on the release page</a></p>` : ""}</details>
    <div class="row-actions">
      <button class="btn ${staged ? "" : "primary"}" data-act="download" ${canAct && !staged && s.state !== "incompatible" ? "" : "disabled"}>Download &amp; verify</button>
      <button class="btn primary" data-act="install-now" ${canAct && staged ? "" : "disabled"}>Install now</button>
      <button class="btn" data-act="install-later" ${canAct && staged ? "" : "disabled"}>Install after current book</button>
      ${s.state === "waiting" ? html`<button class="btn danger" data-act="cancel" ${isAdmin ? "" : "disabled"}>Cancel installation</button>` : ""}
    </div>
    <p class="small muted">Nothing is downloaded or installed without your click. "Install now" finishes the segment being narrated, then restarts;
      unfinished books resume where they stopped. "After current book" lets the running book finish first and starts no other book meanwhile.</p>
  </section>` : ""}
  <section class="card" aria-labelledby="prog-h"><h2 id="prog-h">Progress</h2>
    <ol class="phases" aria-live="polite">${PHASES.map((p) => phaseRow(p, (s.phases || {})[p[0]]))}</ol></section>
  <section class="card" aria-labelledby="pref-h"><h2 id="pref-h">Automatic checks</h2>
    <form id="prefs" class="form-grid">
      <label class="check"><input type="checkbox" name="auto_check" ${s.auto_check ? "checked" : ""} ${isAdmin ? "" : "disabled"}> Check for updates automatically (never downloads or installs by itself)</label>
      <label>How often <select name="check_interval_hours" ${isAdmin ? "" : "disabled"}>
        ${[[6, "every 6 hours"], [24, "daily"], [72, "every 3 days"], [168, "weekly"]].map(([v, t]) => html`<option value="${v}" ${Number(s.check_interval_hours) === v ? "selected" : ""}>${t}</option>`)}</select></label>
      <label>Channel <select name="channel" ${isAdmin ? "" : "disabled"}>
        ${["stable", "beta"].map((v) => html`<option value="${v}" ${s.channel === v ? "selected" : ""}>${v === "stable" ? "Stable (recommended)" : "Beta (earlier, less tested)"}</option>`)}</select></label>
      <div><button class="btn" type="submit" ${isAdmin ? "" : "disabled"}>Save</button></div>
    </form>
    <p class="small muted">Updates come from this project's GitHub Releases (${s.repository}). Only releases signed with the key pinned in this installation are accepted.</p>
  </section>
  <section class="card" aria-labelledby="svc-h"><h2 id="svc-h">Service</h2>
    <p class="small">The studio runs as a background service managed by macOS (launchd) and starts when you log in.</p>
    <button class="btn" data-act="restart" ${isAdmin && s.managed && !busyNow ? "" : "disabled"}>Restart service</button>
    <button class="btn" data-act="rollback" ${isAdmin && s.managed && !busyNow && s.can_rollback ? "" : "disabled"}>Roll back to previous version${s.can_rollback ? ` (${s.can_rollback})` : ""}</button>
    ${s.can_rollback ? "" : html`<p class="small muted">No previous version is kept on this Mac, so there is nothing to roll back to.</p>`}
    <p class="small muted">Platform ${s.platform}. Installed releases kept for rollback: ${(s.installed_releases || []).join(", ") || "–"}.</p>
  </section>
  <section class="card" aria-labelledby="hist-h"><h2 id="hist-h">History</h2>
    ${hist.length ? html`<div class="table-wrap"><table class="table compact"><thead><tr><th scope="col">When</th><th scope="col">Event</th><th scope="col">Version</th><th scope="col">Result</th></tr></thead>
      <tbody>${hist.map((h) => html`<tr><td class="small nowrap">${fmtAgo(h.at)}</td><td>${h.event.replace(/_/g, " ")}</td><td>${h.version || "–"}</td>
        <td><span class="badge tone-${h.ok ? "good" : "bad"}">${h.ok ? "ok" : "failed"}</span> <span class="small">${h.message}</span></td></tr>`)}</tbody></table></div>`
      : html`<p class="muted">No update activity yet.</p>`}
  </section>`);
}

export async function render(view) {
  let isAdmin = false;
  try { const a = await get("/api/auth/status"); isAdmin = !a.auth_required || (a.user && a.user.role === "admin"); } catch { /* shown elsewhere */ }
  let status = null, hist = [], timer = null, lastKey = "", reconnecting = false, editing = false;

  const refresh = async (force = false) => {
    try {
      [status, hist] = await Promise.all([get("/api/update/status"), get("/api/update/history").then((r) => r.history)]);
      reconnecting = false;
    } catch (e) {
      if (!e.status) { reconnecting = true; setHTML(view, html`<div class="page-head"><h1>Updates</h1></div><div class="callout info" role="status">
        <strong>The studio is restarting.</strong> This page reconnects by itself; your work is safe on disk.</div>`); }
      return;
    }
    const key = JSON.stringify([status, hist.length]);
    if (key === lastKey && !force) return;
    if (editing && document.activeElement && view.contains(document.activeElement) && document.activeElement.matches("select, input")) return;
    lastKey = key;
    draw(view, status, hist, isAdmin);
  };

  const onClick = async (e) => {
    const b = e.target.closest("[data-act]"); if (!b || b.disabled) return;
    const act = b.dataset.act;
    try {
      if (act === "check") await busy(b, () => post("/api/update/check"));
      else if (act === "download") await busy(b, () => post("/api/update/download"));
      else if (act === "cancel") await busy(b, () => post("/api/update/cancel"), "Installation cancelled");
      else if (act === "restart") {
        if (!(await confirmDialog("Restart the service?", "The studio restarts in a few seconds. The segment being narrated finishes or is retried; queued work resumes automatically.", "Restart"))) return;
        await busy(b, () => post("/api/update/restart"), "Restarting…");
      } else if (act === "rollback") {
        const v = status.can_rollback;
        if (!v) return;
        if (!(await confirmDialog(`Roll back to version ${v}?`, `The studio goes back from ${status.installed_version} to ${v} at the next safe segment boundary and restarts. A backup of your database is taken first. If the older version cannot open your data, nothing is changed. Books, voices and settings are kept.`, "Roll back"))) return;
        await busy(b, () => post("/api/update/rollback", { version: v, mode: "now", confirm: true }), "Rollback scheduled");
      } else if (act === "install-now" ||act === "install-later") {
        const now = act === "install-now", v = status.staged_version;
        const msg = now ? `Version ${v} installs at the next safe segment boundary and the studio restarts. A backup of your database is taken first, and the previous version is kept for automatic rollback.`
          : `The book being narrated finishes first, then version ${v} installs and the studio restarts. No other book starts in the meantime.`;
        if (!(await confirmDialog(`Install version ${v}?`, msg, "Install"))) return;
        await busy(b, () => post("/api/update/install", { version: v, mode: now ? "now" : "after_job", confirm: true }), "Installation scheduled");
      }
    } catch { /* toast shown by busy */ }
    refresh(true);
  };
  const onSubmit = async (e) => {
    if (e.target.id !== "prefs") return;
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      await busy(e.submitter, () => put("/api/update/preferences", { auto_check: fd.get("auto_check") === "on", check_interval_hours: Number(fd.get("check_interval_hours")), channel: fd.get("channel") }), "Saved");
    } catch { /* toast */ }
    editing = false; refresh(true);
  };
  const onFocus = () => { editing = true; };
  view.addEventListener("click", onClick);
  view.addEventListener("submit", onSubmit);
  view.addEventListener("focusin", onFocus);
  view.addEventListener("focusout", () => { editing = false; });
  await refresh(true);
  timer = setInterval(() => refresh(false), 1500);
  return () => { clearInterval(timer); view.removeEventListener("click", onClick); view.removeEventListener("submit", onSubmit); view.removeEventListener("focusin", onFocus); };
}
