// Pronunciation: a global and per-book dictionary of respellings, plus per-voice pace.
// Entries only change the text sent to the voice; the manuscript itself is never edited.
import { get, post, patch, put, del, html, setHTML, busy, dialog, confirmDialog, toast, toastError, qs } from "../ui.js";

// Run a change; if it would re-render finished segments the server answers 409 with a count, so ask first.
async function withConfirm(btn, fn, okMsg) {
  try {
    return await busy(btn, () => fn(false), okMsg);
  } catch (e) {
    if (e.status === 409 && e.details && e.details.stale_segments) {
      const ok = await confirmDialog("Re-render affected passages?",
        `${e.details.stale_segments} finished segment(s) would be spoken differently, so they are marked for regeneration. Earlier takes are kept as history. Continue?`, "Apply and regenerate");
      if (!ok) return null;
      return busy(btn, () => fn(true), okMsg);
    }
    return null;
  }
}

function entryForm(e = {}) {
  const sel = (v, a) => (v === a ? html`selected` : "");
  return html`<label>Written as <input name="written" required maxlength="80" value="${e.written || ""}"></label>
    <label>Spoken as <input name="spoken" required maxlength="200" value="${e.spoken || ""}"></label>
    <p class="small muted">Spell it the way it should sound, for example Hermione becomes "Her-my-oh-nee". Use the Try box afterwards.</p>
    <label>Match <select name="whole_word"><option value="1" ${sel(e.whole_word !== false, true)}>Whole words only</option><option value="0" ${sel(e.whole_word === false, true)}>Anywhere inside words</option></select></label>
    <label>Capitals <select name="case_sensitive"><option value="0" ${sel(!e.case_sensitive, true)}>Ignore capitals</option><option value="1" ${sel(!!e.case_sensitive, true)}>Match capitals exactly</option></select></label>`;
}
const fromForm = (d) => ({ written: d.written, spoken: d.spoken, whole_word: d.whole_word === "1", case_sensitive: d.case_sensitive === "1" });

export async function render(view, _parts, query) {
  let scope = query.get("book") || "global";
  const [books, voiceData] = await Promise.all([get("/api/books").then((r) => r.books), get("/api/voices")]);
  let data = null;

  async function load() {
    data = await get("/api/pronunciations" + qs({ book_id: scope === "global" ? "" : scope }));
    paint();
  }

  function rows(list, label) {
    if (!list.length) return html`<p class="muted">No ${label} entries yet.</p>`;
    return html`<div class="table-wrap"><table class="table"><caption class="sr-only">${label} pronunciations</caption>
      <thead><tr><th>Written</th><th>Spoken</th><th>Match</th><th></th></tr></thead><tbody>
      ${list.map((e) => html`<tr>
        <td>${e.written}</td><td>${e.spoken}</td>
        <td class="small muted">${e.whole_word ? "whole word" : "inside words"}${e.case_sensitive ? ", exact capitals" : ""}${e.enabled ? "" : ", OFF"}</td>
        <td><button class="btn ghost small" type="button" data-act="edit" data-id="${e.id}">Edit…</button>
          <button class="btn ghost small" type="button" data-act="toggle" data-id="${e.id}">${e.enabled ? "Turn off" : "Turn on"}</button>
          <button class="btn ghost small danger" type="button" data-act="delete" data-id="${e.id}">Delete</button></td></tr>`)}
      </tbody></table></div>`;
  }

  function paint() {
    const isBook = scope !== "global";
    setHTML(view, html`<div class="page-head"><h1>Pronunciation</h1></div>
      <section class="card" aria-labelledby="pr-h"><h2 id="pr-h">Dictionary</h2>
        <p class="muted">Tell the narrator how to say names and unusual words. The book text is never changed, only what the voice is given.
          A book's own entries win over the global ones. Changing an entry re-renders only the passages that contain it.</p>
        <label>Dictionary for
          <select id="pr-scope"><option value="global" ${isBook ? "" : html`selected`}>All books (global)</option>
            ${books.map((b) => html`<option value="${b.id}" ${String(b.id) === scope ? html`selected` : ""}>${b.title}</option>`)}</select></label>
        <h3>${isBook ? "This book" : "Global"}</h3>
        ${rows(isBook ? data.book : data.global, isBook ? "book" : "global")}
        <p><button class="btn primary" type="button" data-act="add">Add entry…</button></p>
        ${isBook ? html`<h3>Global entries that also apply</h3>${rows(data.global, "global")}` : ""}
      </section>
      <section class="card" aria-labelledby="try-h"><h2 id="try-h">Try it</h2>
        <form id="try-form" class="form"><label>Sample text <textarea name="text" rows="2" maxlength="2000" required></textarea></label>
          <button class="btn" type="submit">Show what the voice will be given</button></form>
        <p id="try-out" class="small" aria-live="polite"></p>
        <p class="small muted">Real speech is only produced on the Mac; the respelling is a hint to the model, so listen to a preview after adding an entry.</p>
      </section>
      <section class="card" aria-labelledby="pace-h"><h2 id="pace-h">Voice pace</h2>
        <p class="muted">Speed each voice up or down between ${data.pace_range[0]} and ${data.pace_range[1]} (1.0 is natural). The speech model has no pace control,
          so the studio stretches finished audio without changing pitch; keep it modest because extreme values can sound slightly processed.</p>
        <div class="table-wrap"><table class="table"><caption class="sr-only">Pace per voice</caption><thead><tr><th>Voice</th><th>Pace</th><th></th></tr></thead><tbody>
          ${voiceData.voices.map((v) => html`<tr><td>${v.name}</td>
            <td><input type="number" aria-label="Pace for ${v.name}" data-pace="${v.id}" min="${data.pace_range[0]}" max="${data.pace_range[1]}" step="0.05" value="${v.pace}"></td>
            <td><button class="btn small" type="button" data-act="pace" data-id="${v.id}">Save</button></td></tr>`)}
        </tbody></table></div></section>`);
  }

  async function onChange(e) {
    if (e.target.id === "pr-scope") { scope = e.target.value; await load(); }
  }

  async function onSubmit(e) {
    if (e.target.id !== "try-form") return;
    e.preventDefault();
    const text = new FormData(e.target).get("text");
    try {
      const r = await post("/api/pronunciations/try", { text, book_id: scope === "global" ? null : Number(scope) });
      const out = view.querySelector("#try-out");
      out.textContent = r.changed ? `Spoken as: ${r.output}` : "No entry matches this text.";
    } catch (err) { toastError(err); }
  }

  async function onClick(e) {
    const b = e.target.closest("button[data-act]");
    if (!b) return;
    const act = b.dataset.act;
    const all = [...data.global, ...(data.book || [])];
    const entry = all.find((x) => x.id === Number(b.dataset.id));
    try {
      if (act === "add") {
        const res = await dialog({ title: scope === "global" ? "Add global entry" : "Add entry for this book", form: true, body: entryForm(), buttons: [{ label: "Add", value: "ok", kind: "primary" }] });
        if (!res) return;
        const body = { ...fromForm(res.data), book_id: scope === "global" ? null : Number(scope) };
        if (await withConfirm(b, (c) => post("/api/pronunciations", { ...body, confirm_invalidate: c }), "Entry added")) await load();
      } else if (act === "edit") {
        const res = await dialog({ title: "Edit entry", form: true, body: entryForm(entry), buttons: [{ label: "Save", value: "ok", kind: "primary" }] });
        if (!res) return;
        if (await withConfirm(b, (c) => patch(`/api/pronunciations/${entry.id}`, { ...fromForm(res.data), confirm_invalidate: c }), "Entry saved")) await load();
      } else if (act === "toggle") {
        if (await withConfirm(b, (c) => patch(`/api/pronunciations/${entry.id}`, { enabled: !entry.enabled, confirm_invalidate: c }), "Entry updated")) await load();
      } else if (act === "delete") {
        if (!(await confirmDialog("Delete entry?", `"${entry.written}" will be read normally again.`, "Delete", true))) return;
        if (await withConfirm(b, (c) => del(`/api/pronunciations/${entry.id}${c ? "?confirm_invalidate=true" : ""}`), "Entry deleted")) await load();
      } else if (act === "pace") {
        const input = view.querySelector(`[data-pace="${b.dataset.id}"]`);
        const pace = Number(input.value);
        if (await withConfirm(b, (c) => put(`/api/voices/${b.dataset.id}/pace`, { pace, confirm_invalidate: c }), "Pace saved")) toast("Preview the voice to hear the new pace.", "info");
      }
    } catch (err) { toastError(err); }
  }

  await load();
  view.addEventListener("click", onClick);
  view.addEventListener("change", onChange);
  view.addEventListener("submit", onSubmit);
  return () => { view.removeEventListener("click", onClick); view.removeEventListener("change", onChange); view.removeEventListener("submit", onSubmit); };
}
