// Shared UI helpers: auto-escaping templates, API client, formatting, toasts, dialogs.

export class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
export const raw = (s) => new Raw(s);

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);

function fmtVal(v) {
  if (v === null || v === undefined || v === false) return "";
  if (v instanceof Raw) return v.s;
  if (Array.isArray(v)) return v.map(fmtVal).join("");
  return esc(String(v));
}

/** Tagged template that escapes every interpolation unless wrapped in raw(). */
export function html(strings, ...vals) {
  let out = "";
  strings.forEach((s, i) => { out += s; if (i < vals.length) out += fmtVal(vals[i]); });
  return new Raw(out);
}

export function setHTML(el, tpl) {
  el.innerHTML = tpl instanceof Raw ? tpl.s : esc(String(tpl));
  applyBars(el);
}

/** Progress bars are sized via the CSSOM (CSP forbids inline style attributes). */
export function applyBars(root) {
  root.querySelectorAll("[data-pct]").forEach((el) => {
    const v = Math.max(0, Math.min(100, Number(el.dataset.pct) || 0));
    el.style.width = v + "%";
  });
}

// ------------------------------------------------------------ in-place repaint (live views)
const tplString = (tpl) => (tpl instanceof Raw ? tpl.s : esc(String(tpl)));
/** Rendered markup of a template (what setHTML would assign). Pure. */
export const htmlOf = tplString;

/**
 * patchHTML(el, tpl): like setHTML(el, tpl) but survives a live repaint. Contract:
 *  - Preserved: scrollTop/scrollLeft of `el` and of every scrollable descendant that has an `id` (restored by id);
 *    keyboard focus and the text selection of the focused element, if it has an `id` that exists again afterwards;
 *    the open/closed state of every <details id="..."> (restored by id, so a section the user opened stays open).
 *  - NOT preserved: elements without an id, typed-but-unsaved form values (use snapshotForm/applyForm), <audio> position/paused
 *    state (views must skip the refresh while something is playing).
 *  - Give anything the user can scroll, focus or open a stable id in your template and it will survive repaints.
 * Returns el.
 */
export function patchHTML(el, tpl) {
  const scrolls = new Map();
  const note = (node) => { if (node.id && (node.scrollTop || node.scrollLeft)) scrolls.set(node.id, [node.scrollTop, node.scrollLeft]); };
  const opens = new Map();
  for (const node of el.querySelectorAll("[id]")) {
    note(node);
    if (String(node.tagName).toUpperCase() === "DETAILS") opens.set(node.id, !!node.open);
  }
  const selfTop = el.scrollTop || 0, selfLeft = el.scrollLeft || 0;
  const active = typeof document !== "undefined" ? document.activeElement : null;
  let focus = null;
  if (active && active !== el && active.id && el.contains(active)) {
    focus = { id: active.id };
    try { if (typeof active.selectionStart === "number") focus.sel = [active.selectionStart, active.selectionEnd, active.selectionDirection]; } catch { /* input types without selection */ }
  }
  setHTML(el, tpl);
  const byId = new Map();
  for (const node of el.querySelectorAll("[id]")) byId.set(node.id, node);
  for (const [id, open] of opens) { const n = byId.get(id); if (n) n.open = open; }
  if (selfTop) el.scrollTop = selfTop;
  if (selfLeft) el.scrollLeft = selfLeft;
  for (const [id, [top, left]] of scrolls) { const n = byId.get(id); if (n) { n.scrollTop = top; n.scrollLeft = left; } }
  if (focus) {
    const n = byId.get(focus.id);
    if (n && typeof n.focus === "function") {
      n.focus({ preventScroll: true });
      if (focus.sel) { try { n.setSelectionRange(focus.sel[0], focus.sel[1], focus.sel[2] || "none"); } catch { /* not a text control any more */ } }
    }
  }
  return el;
}

const regionLast = new WeakMap();
/**
 * patchRegions(root, regions): regions = { "css selector": (el, i) => template }. For every element in `root` matching a selector
 * the template is rendered and patchHTML'd into it ONLY if the markup differs from what this helper painted there last time
 * (previous markup is kept in a WeakMap, so unchanged data touches no DOM at all). An element this helper has not painted yet is
 * painted once. Returns the number of regions repainted.
 */
export function patchRegions(root, regions) {
  let n = 0;
  for (const [sel, fn] of Object.entries(regions)) {
    root.querySelectorAll(sel).forEach((el, i) => {
      const tpl = fn(el, i);
      const str = tplString(tpl);
      if (regionLast.get(el) === str) return;
      regionLast.set(el, str);
      patchHTML(el, tpl instanceof Raw ? tpl : new Raw(str));
      n++;
    });
  }
  return n;
}

/**
 * pager(total, offset, limit, opts?): markup for "1–10 of 37" with Prev / Next buttons. No inline handlers; convention:
 *   <nav class="pager" data-total data-limit>  ...  <button data-pager="prev|next" data-offset="<offset to go to>" [disabled]>
 * Views delegate: wirePager(container, (newOffset) => { offset = newOffset; reload(); }) once per container (it listens on
 * the container, so it keeps working after repaints). opts.noun (default "") adds a word after the total ("of 37 books").
 * With total <= limit only the range text is shown (no buttons).
 */
export function pager(total, offset, limit, opts = {}) {
  total = Math.max(0, Number(total) || 0); limit = Math.max(1, Number(limit) || 1);
  offset = Math.min(Math.max(0, Number(offset) || 0), Math.max(0, total - 1));
  const from = total ? offset + 1 : 0, to = Math.min(total, offset + limit);
  const noun = opts.noun ? " " + opts.noun : "";
  const range = html`<span class="pager-range" aria-live="polite">${from}–${to} of ${total}${noun}</span>`;
  if (total <= limit) return html`<nav class="pager" aria-label="Pagination" data-total="${total}" data-limit="${limit}">${range}</nav>`;
  const prev = Math.max(0, offset - limit), next = offset + limit;
  return html`<nav class="pager" aria-label="Pagination" data-total="${total}" data-limit="${limit}">${range}
    <button class="btn small" type="button" data-pager="prev" data-offset="${prev}" aria-label="Previous page" ${offset <= 0 ? raw("disabled") : ""}>Prev</button>
    <button class="btn small" type="button" data-pager="next" data-offset="${next}" aria-label="Next page" ${next >= total ? raw("disabled") : ""}>Next</button></nav>`;
}
/** Delegate clicks on pager buttons inside `container`; calls handler(newOffset). Returns an unwire function. */
export function wirePager(container, handler) {
  const onClick = (e) => {
    const b = e.target.closest && e.target.closest("button[data-pager]");
    if (!b || b.disabled || !container.contains(b)) return;
    handler(Number(b.dataset.offset) || 0, b.dataset.pager);
  };
  container.addEventListener("click", onClick);
  return () => container.removeEventListener("click", onClick);
}

// The signed-in user, set by app.js. Mirrors the server: with sign-in off (loopback only) there is no user and everything is allowed.
// This only hides controls; the server enforces administrator-only actions itself.
let sessionUser = null;
export const setSessionUser = (u) => { sessionUser = u || null; };
export const isAdmin = () => !sessionUser || sessionUser.role === "admin";

/** Snapshot a form's values (checkbox groups by value), so a view that redraws itself can put back what the user had typed or chosen. */
export function snapshotForm(form) {
  const d = {};
  if (!form) return d;
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") { if (!d[el.name]) d[el.name] = {}; d[el.name][el.value] = el.checked; } else d[el.name] = el.value;
  }
  return d;
}
export function applyForm(form, snap) {
  if (!form || !snap) return;
  for (const el of form.elements) {
    if (!el.name || !(el.name in snap)) continue;
    if (el.type === "checkbox") { if (el.value in snap[el.name]) el.checked = snap[el.name][el.value]; } else el.value = snap[el.name];
  }
}

export class ApiError extends Error {
  constructor(message, status, details) { super(message); this.status = status; this.details = details; }
}

export async function api(method, path, body, opts = {}) {
  const headers = { "X-Studio-Request": "1", Accept: "application/json" };
  let payload;
  if (body instanceof FormData) payload = body;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  let res;
  try {
    res = await fetch(path, { method, headers, body: payload, credentials: "same-origin", signal: opts.signal });
  } catch (e) {
    if (e.name === "AbortError") throw e;
    throw new ApiError("Cannot reach the studio server. Is it running?", 0);
  }
  let data = null;
  const text = await res.text();
  try { data = text ? JSON.parse(text) : null; } catch { data = { error: text }; }
  if (res.status === 401 && !path.startsWith("/api/auth/")) window.dispatchEvent(new Event("studio:unauth"));
  if (!res.ok) throw new ApiError((data && (data.error || data.detail)) || `${res.status} ${res.statusText}`, res.status, data && data.details);
  return data;
}
export const get = (p, o) => api("GET", p, undefined, o);
export const post = (p, b = {}) => api("POST", p, b);
export const put = (p, b) => api("PUT", p, b);
export const patch = (p, b) => api("PATCH", p, b);
export const del = (p) => api("DELETE", p);

// ------------------------------------------------------------------ formatting
export function fmtDuration(s) {
  if (s === null || s === undefined || isNaN(s)) return "–";
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(sec).padStart(2, "0")}s`;
  return `${sec}s`;
}
export function fmtClock(s) {
  if (s === null || s === undefined) return "–";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = (s % 60).toFixed(1);
  return `${h ? h + ":" : ""}${String(m).padStart(h ? 2 : 1, "0")}:${sec.padStart(4, "0")}`;
}
export function fmtDate(t) {
  if (!t) return "–";
  const d = new Date(t * 1000);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" }) + " " +
    d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
}
export function fmtAgo(t) {
  if (!t) return "–";
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return fmtDate(t);
}
export function fmtBytes(n) {
  if (n === null || n === undefined) return "–";
  const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}
export const pct = (x) => `${(Math.max(0, x || 0) * 100).toFixed(x > 0 && x < 0.1 ? 1 : 0)}%`;

export const STATE_LABEL = {
  imported: "Imported", queued: "Queued", generating: "Generating", paused: "Paused", failed: "Failed",
  incomplete: "Incomplete", needs_review: "Human review required", qa_passed: "QA passed – awaiting signoff",
  approved: "Approved for publication", running: "Running", completed: "Completed", cancelled: "Cancelled",
  pending: "Not rendered", synthesizing: "Rendering", passed: "QA passed", flagged: "Needs review",
};
export const STATE_TONE = {
  imported: "neutral", queued: "info", generating: "info", running: "info", paused: "warn", failed: "bad", incomplete: "warn",
  needs_review: "warn", qa_passed: "good", approved: "approved", completed: "good", cancelled: "neutral",
  pending: "neutral", synthesizing: "info", passed: "good", flagged: "warn",
};
export function badge(state, label) {
  return html`<span class="badge tone-${STATE_TONE[state] || "neutral"}">${label || STATE_LABEL[state] || state}</span>`;
}
export function progressBar(fraction, label) {
  const p = Math.round((fraction || 0) * 1000) / 10;
  return html`<div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${p}" aria-label="${label || "Progress"}">
    <div class="progress-fill" data-pct="${p}"></div></div>`;
}

// ---------------------------------------------------------------- toasts/dialog
export function toast(message, kind = "info", timeout = 5000) {
  const box = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.setAttribute("role", kind === "error" ? "alert" : "status");
  el.textContent = message;
  const close = document.createElement("button");
  close.className = "toast-close"; close.type = "button"; close.setAttribute("aria-label", "Dismiss"); close.textContent = "×";
  close.onclick = () => el.remove();
  el.appendChild(close);
  box.appendChild(el);
  if (timeout) setTimeout(() => el.remove(), timeout);
}
export function toastError(e) {
  if (e && e.name === "AbortError") return;
  toast(e && e.message ? e.message : String(e), "error", 9000);
}

/** Accessible modal built on <dialog>. Resolves to the clicked button value or null. */
export function dialog({ title, body, buttons = [{ label: "OK", value: "ok", kind: "primary" }], form = false }) {
  return new Promise((resolve) => {
    const dlg = document.createElement("dialog");
    dlg.className = "modal";
    dlg.setAttribute("aria-labelledby", "dlg-title");
    setHTML(dlg, html`<form method="dialog" class="modal-inner">
      <h2 id="dlg-title">${title}</h2>
      <div class="modal-body">${body}</div>
      <div class="modal-actions">${buttons.map((b) => html`<button class="btn ${b.kind || ""}" value="${b.value}" type="submit">${b.label}</button>`)}
        <button class="btn ghost" value="" type="submit" formnovalidate>Cancel</button></div></form>`);
    document.body.appendChild(dlg);
    dlg.addEventListener("close", () => {
      const v = dlg.returnValue || null;
      const data = form ? Object.fromEntries(new FormData(dlg.querySelector("form")).entries()) : null;
      dlg.remove();
      resolve(form ? (v ? { value: v, data } : null) : v);
    });
    dlg.showModal();
    const first = dlg.querySelector("input, textarea, select, button.primary");
    if (first) first.focus();
  });
}
export async function confirmDialog(title, message, okLabel = "Confirm", danger = false) {
  const v = await dialog({ title, body: html`<p>${message}</p>`, buttons: [{ label: okLabel, value: "ok", kind: danger ? "danger" : "primary" }] });
  return v === "ok";
}

/** Run an async action with a busy button and toast feedback. */
export async function busy(btn, fn, okMsg) {
  if (btn) { btn.disabled = true; btn.setAttribute("aria-busy", "true"); }
  try {
    const r = await fn();
    if (okMsg) toast(okMsg, "success");
    return r;
  } catch (e) { toastError(e); throw e; }
  finally { if (btn) { btn.disabled = false; btn.removeAttribute("aria-busy"); } }
}

export function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}
export function qs(params) {
  const u = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== "") u.set(k, v); });
  const s = u.toString(); return s ? "?" + s : "";
}
export function parseHash() {
  const h = location.hash.replace(/^#\/?/, "");
  const [path, query] = h.split("?");
  const parts = (path || "overview").split("/").filter(Boolean);
  return { parts, query: new URLSearchParams(query || "") };
}
export function navigate(hash) { if (location.hash !== hash) location.hash = hash; }

/** True when two state payloads carry the same data (JSON string compare; the server emits stable key order). Pure. */
export function sameState(a, b) {
  if (a === b) return true;
  try { return JSON.stringify(a) === JSON.stringify(b); } catch { return false; }
}

/**
 * Subscribe to live state pushes. Returns an unsubscribe function. The callback receives the state payload and is
 * called: on the first delivery; whenever the payload differs from the last one delivered to THIS subscriber; and on
 * every polled-fallback payload (state.polled). Deliveries are throttled to at most one per minInterval ms.
 */
export function onState(fn, minInterval = 1500) {
  let last = 0, timer = null, lastDetail = null, delivered = null, hasDelivered = false;
  const stale = (d) => hasDelivered && !(d && d.polled) && sameState(d, delivered);
  const fire = () => {
    timer = null;
    if (stale(lastDetail)) return;
    last = Date.now(); delivered = lastDetail; hasDelivered = true;
    fn(lastDetail);
  };
  const h = (e) => {
    lastDetail = e.detail;
    if (timer || stale(lastDetail)) return;
    const wait = Math.max(0, minInterval - (Date.now() - last));
    timer = setTimeout(fire, wait);
  };
  window.addEventListener("studio:state", h);
  return () => { window.removeEventListener("studio:state", h); clearTimeout(timer); };
}

/** Render an <audio> element for a media URL (browsers seek via HTTP Range requests). */
export function player(src, label, extraClass = "") {
  return html`<audio class="player ${extraClass}" controls preload="none" src="${src}" aria-label="${label}"></audio>`;
}
