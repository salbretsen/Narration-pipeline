// Disk & Backups (administrators): usage per book and category, free space, purge/delete with typed confirmation, backup and restore.
// Every destructive call is first sent WITHOUT confirm (the server answers 409 with a preview), then repeated with confirm:true after the user agrees.
import { get, post, html, setHTML, dialog, confirmDialog, toast, toastError, fmtBytes, fmtDate } from "../ui.js";

const CATS = ["manuscripts", "segments", "alternates", "reports", "masters", "exports"];
const OTHER = { voices: "Voices", recordings: "Recordings", previews: "Previews", imports: "Import copies", database: "Database", backups: "Backups", logs: "Logs", model_cache: "Model cache" };

async function run(btn, fn, okMsg) {
  btn.disabled = true;
  try { const r = await fn(); if (okMsg) toast(okMsg, "success"); return r; } finally { btn.disabled = false; }
}

function freeLine(label, f) {
  if (!f) return html`<div><dt>${label}</dt><dd class="muted">unknown</dd></div>`;
  const low = f.free_bytes < 5 * 1024 ** 3;
  return html`<div><dt>${label}</dt><dd><span class="${low ? "err" : ""}">${fmtBytes(f.free_bytes)} free</span> of ${fmtBytes(f.total_bytes)}<br><span class="small muted"><code>${f.path}</code></span></dd></div>`;
}

async function guarded(path, body, previewText, okLabel) {
  // 1st call without confirm: the server refuses with 409 + preview (or a 409 "busy" message that we just show)
  try {
    return await post(path, { ...body, confirm: true, ...(await ask(path, body, previewText, okLabel)) });
  } catch (e) {
    if (e && e.cancelled) return null;
    throw e;
  }
}

async function ask(path, body, previewText, okLabel) {
  try {
    await post(path, { ...body, confirm: false });
  } catch (e) {
    if (e.status === 409 && e.details && typeof e.details === "object") {
      const ok = await confirmDialog("Please confirm", `${e.message} ${previewText ? previewText(e.details) : ""}`, okLabel, true);
      if (!ok) { const c = new Error("cancelled"); c.cancelled = true; throw c; }
      return {};
    }
    throw e;
  }
  return {};
}

function draw(view, u) {
  const labels = u.category_labels || {};
  setHTML(view, html`
  <div class="page-head"><h1>Disk &amp; Backups</h1><div class="head-actions">
    <button class="btn" data-act="refresh">Refresh</button>
    <button class="btn" data-act="dbbackup">Back up database</button></div></div>
  <section class="card" aria-labelledby="free-h"><h2 id="free-h">Free space</h2>
    <dl class="about-facts">${freeLine("Library volume", u.free.library)}${freeLine("Model cache", u.free.model_cache)}${freeLine("Backups folder", u.free.backups)}
      <div><dt>Library total</dt><dd>${fmtBytes(u.library_bytes)}</dd></div></dl>
  </section>
  <section class="card" aria-labelledby="books-h"><h2 id="books-h">Books</h2>
    ${u.books.length ? html`<div class="table-wrap"><table class="table compact"><thead><tr><th scope="col">Book</th>
      ${CATS.map((c) => html`<th scope="col" title="${labels[c] || c}">${labels[c] || c}</th>`)}<th scope="col">Total</th><th scope="col">Actions</th></tr></thead><tbody>
      ${u.books.map((b) => html`<tr><td>${b.display_title}${b.archived ? html` <span class="badge tone-neutral">archived</span>` : ""}${b.busy ? html` <span class="badge tone-warn">in queue</span>` : ""}</td>
        ${CATS.map((c) => html`<td>${fmtBytes(b.categories[c] || 0)}</td>`)}<td><strong>${fmtBytes(b.total_bytes)}</strong></td>
        <td class="actions">
          <button class="btn small" data-act="backup" data-id="${b.id}">Back up</button>
          <button class="btn small" data-act="purge-takes" data-id="${b.id}" ${b.busy ? "disabled" : ""} title="Remove superseded takes; selected and approved takes stay">Purge takes (${b.purgeable.takes}, ${fmtBytes(b.purgeable.takes_bytes)})</button>
          <button class="btn small" data-act="purge-exports" data-id="${b.id}" ${b.busy || !b.purgeable.exports_bytes ? "disabled" : ""}>Purge exports</button>
          <button class="btn small" data-act="purge-masters" data-id="${b.id}" ${b.busy || !b.purgeable.masters_bytes ? "disabled" : ""}>Purge masters</button>
          <button class="btn small danger" data-act="delete" data-id="${b.id}" data-title="${b.display_title}" ${b.busy ? "disabled" : ""}>Delete</button></td></tr>`)}
      <tr><th scope="row">All books</th>${CATS.map((c) => html`<td>${fmtBytes(u.category_totals[c] || 0)}</td>`)}<td><strong>${fmtBytes(u.library_bytes)}</strong></td><td></td></tr>
      </tbody></table></div>
      <p class="small muted">Books that are queued, rendering or paused cannot be purged or deleted; cancel their job first. Purging takes keeps every selected (and therefore every approved) take.</p>`
      : html`<p class="muted">No books yet.</p>`}
  </section>
  <section class="card" aria-labelledby="other-h"><h2 id="other-h">Everything else</h2>
    <dl class="about-facts">${Object.entries(OTHER).map(([k, l]) => html`<div><dt>${l}</dt><dd>${fmtBytes(u.other[k] || 0)}</dd></div>`)}</dl>
    <p class="small muted">Model cache <code>${u.paths.model_cache}</code> · Library <code>${u.paths.library}</code></p>
  </section>
  <section class="card" aria-labelledby="bk-h"><h2 id="bk-h">Backups</h2>
    <p class="small muted">Book backups are single zip files (database rows plus all audio and documents) in <code>${u.paths.backups}/books</code>. Restoring always creates a NEW book; the original is never touched.</p>
    ${u.backups.books.length ? html`<div class="table-wrap"><table class="table compact"><thead><tr><th scope="col">File</th><th scope="col">Book</th><th scope="col">Size</th><th scope="col">Made</th><th scope="col">Actions</th></tr></thead><tbody>
      ${u.backups.books.map((x) => html`<tr><td><code>${x.name}</code></td><td>${x.title || "(unreadable)"}</td><td>${fmtBytes(x.size_bytes)}</td><td>${fmtDate(x.modified)}</td>
        <td class="actions"><button class="btn small" data-act="restore" data-name="${x.name}" ${x.invalid ? "disabled" : ""}>Restore as new book</button>
        <button class="btn small danger" data-act="delbackup" data-name="${x.name}">Delete</button></td></tr>`)}</tbody></table></div>`
      : html`<p class="muted">No book backups yet.</p>`}
    <h3>Database backups</h3>
    ${u.backups.databases.length ? html`<ul class="small">${u.backups.databases.map((x) => html`<li><code>${x.name}</code> · ${fmtBytes(x.size_bytes)} · ${fmtDate(x.modified)}</li>`)}</ul>`
      : html`<p class="muted">None yet.</p>`}
  </section>`);
}

export async function render(view) {
  let u;
  try { u = await get("/api/disk/usage"); }
  catch (e) {
    setHTML(view, html`<div class="page-head"><h1>Disk &amp; Backups</h1></div><div class="callout info" role="status">${e.status === 403 ? "Disk management is available to administrators only." : e.message}</div>`);
    return;
  }
  draw(view, u);
  const reload = async () => { draw(view, await get("/api/disk/usage")); };
  const onClick = async (ev) => {
    const btn = ev.target.closest("button[data-act]");
    if (!btn) return;
    const id = btn.dataset.id;
    try {
      switch (btn.dataset.act) {
        case "refresh": await run(btn, reload); break;
        case "dbbackup": { const r = await run(btn, () => post("/api/disk/db-backup"), "Database backed up"); await reload(); break; }
        case "backup": { await run(btn, () => post(`/api/disk/books/${id}/backup`), "Backup written to the backups folder"); await reload(); break; }
        case "purge-takes": case "purge-exports": case "purge-masters": {
          const scope = btn.dataset.act.slice(6);
          const r = await guarded(`/api/disk/books/${id}/purge`, { scope }, (d) => `${d.items} item(s), ${fmtBytes(d.bytes)}.`, "Purge");
          if (r) { toast(`Freed ${fmtBytes(r.freed_bytes)}`, "success"); await reload(); }
          break;
        }
        case "delete": {
          const title = btn.dataset.title;
          const res = await dialog({ title: "Delete book permanently", form: true, buttons: [{ label: "Delete forever", value: "ok", kind: "danger" }],
            body: html`<p>This permanently deletes <strong>${title}</strong> with all its audio, exports and history. Back it up first if you may want it again.</p>
              <label>Type the book title to confirm <input name="typed" autocomplete="off" required></label>` });
          if (!res) break;
          if (res.data.typed.trim() !== title.trim()) { toast("The title did not match. Nothing was deleted.", "error", 8000); break; }
          const r = await post(`/api/disk/books/${id}/delete`, { confirm: true });
          toast(`Deleted. Freed ${fmtBytes(r.freed_bytes)}`, "success");
          await reload();
          break;
        }
        case "restore": {
          const r = await run(btn, () => post("/api/disk/restore", { name: btn.dataset.name }));
          toast(`Restored as "${r.title}"${r.warnings.length ? ". " + r.warnings.join(" ") : ""}`, r.warnings.length ? "info" : "success", 9000);
          await reload();
          break;
        }
        case "delbackup": {
          const r = await guarded("/api/disk/backups/delete", { name: btn.dataset.name }, (d) => `${fmtBytes(d.bytes)}.`, "Delete backup");
          if (r) { toast("Backup deleted", "success"); await reload(); }
          break;
        }
      }
    } catch (e) { toastError(e); }
  };
  view.addEventListener("click", onClick);
  return () => view.removeEventListener("click", onClick);
}
