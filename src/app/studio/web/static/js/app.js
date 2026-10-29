// App shell: hash router, live state stream (SSE with polling fallback), worker status.
import { get, post, parseHash, setHTML, html, toastError, toast, dialog, busy } from "./ui.js";
import * as overview from "./views/overview.js";
import * as library from "./views/library.js";
import * as queue from "./views/queue.js";
import * as voices from "./views/voices.js";
import * as review from "./views/review.js";
import * as book from "./views/book.js";
import * as settings from "./views/settings.js";
import * as about from "./views/about.js";
import * as narrative from "./views/narrative.js";
import * as inspector from "./views/inspector.js";
import * as updates from "./views/updates.js";
import * as disk from "./views/disk.js";
import * as pronunciation from "./views/pronunciation.js";

const ROUTES = { overview, library, queue, voices, review, book, settings, about, narrative, inspect: inspector, updates, disk, pronunciation };
let cleanup = null;
let currentKey = "";

async function route() {
  const { parts, query } = parseHash();
  const name = ROUTES[parts[0]] ? parts[0] : "overview";
  const key = location.hash;
  if (cleanup) { try { cleanup(); } catch (e) { console.error(e); } cleanup = null; }
  document.querySelectorAll("[data-nav]").forEach((a) => {
    const active = a.dataset.nav === name || ((name === "book" || name === "narrative") && a.dataset.nav === "library") || (name === "inspect" && a.dataset.nav === "review");
    a.classList.toggle("active", active);
    if (active) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
  });
  closeNav();
  const view = document.getElementById("view");
  const sameView = currentKey.split("?")[0].split("/")[1] === key.split("?")[0].split("/")[1];
  currentKey = key;
  try {
    cleanup = (await ROUTES[name].render(view, parts.slice(1), query)) || null;
  } catch (e) {
    console.error(e);
    setHTML(view, html`<div class="card error-card"><h1>Something went wrong</h1><p>${e.message || e}</p>
      <p><a href="#/overview">Back to overview</a></p></div>`);
  }
  if (!sameView) {
    const h1 = view.querySelector("h1");
    if (h1) { h1.setAttribute("tabindex", "-1"); h1.focus({ preventScroll: false }); }
    document.title = (h1 ? h1.textContent.trim() + " · " : "") + "Audiobook Studio";
  }
}

// ------------------------------------------------------------- live updates
let es = null, pollTimer = null, lastState = null, sseFailures = 0;

function dispatchState(state) {
  lastState = state;
  updateWorkerPill(state.worker);
  window.dispatchEvent(new CustomEvent("studio:state", { detail: state }));
}

function connectSSE() {
  if (es) es.close();
  es = new EventSource("/api/events");
  es.addEventListener("state", (ev) => {
    sseFailures = 0;
    setBanner(null);
    try { dispatchState(JSON.parse(ev.data)); } catch (e) { console.error(e); }
  });
  es.onerror = () => {
    sseFailures++;
    if (sseFailures >= 3) {
      es.close(); es = null;
      setBanner("Live updates interrupted – reconnecting…");
      startPolling();
      setTimeout(() => { stopPolling(); sseFailures = 0; connectSSE(); }, 15000);
    }
  };
}
function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    try {
      const w = await get("/api/worker");
      setBanner(null);
      dispatchState({ worker: w, polled: Date.now() });
    } catch { setBanner("The studio server is not responding. Your data is safe; queue state is stored on disk."); }
  }, 4000);
}
function stopPolling() { clearInterval(pollTimer); pollTimer = null; }

function setBanner(msg) {
  const b = document.getElementById("conn-banner");
  if (!msg) { b.hidden = true; return; }
  b.hidden = false; b.textContent = msg;
}

function updateWorkerPill(w) {
  if (!w) return;
  const pill = document.getElementById("worker-pill");
  pill.className = "topbar-status " + (w.online ? (w.state === "idle" ? "st-idle" : "st-busy") : "st-off");
  const label = w.online ? (w.state === "idle" ? "GPU worker idle" : `Working: ${w.message || w.state}`) : "GPU worker offline";
  pill.querySelector(".label").textContent = label;
  pill.title = w.online ? `Worker pid ${w.pid}, heartbeat ${w.heartbeat_age_s}s ago` : "No worker heartbeat. Restart it from System & Licenses.";
  const eb = document.getElementById("engine-badge");
  if (w.engine === "mock") { eb.hidden = false; eb.textContent = "MOCK ENGINE – test tones, not narration"; }
  else eb.hidden = true;
}

export const currentState = () => lastState;

// ------------------------------------------------------------------- nav
function closeNav() {
  document.body.classList.remove("nav-open");
  document.getElementById("nav-toggle").setAttribute("aria-expanded", "false");
}
let authUser = null;

function stopStream() { if (es) { es.close(); es = null; } stopPolling(); }

function showLogin(message) {
  stopStream();
  if (cleanup) { try { cleanup(); } catch (e) { console.error(e); } cleanup = null; }
  document.body.classList.add("signed-out");
  document.getElementById("user-box").hidden = true;
  document.getElementById("worker-pill").hidden = true;
  const view = document.getElementById("view");
  setHTML(view, html`<div class="login-card card"><h1>Sign in</h1>
    <p class="muted">Audiobook Studio on this network requires an account.</p>
    ${message ? html`<div class="callout warn" role="alert">${message}</div>` : ""}
    <form id="login-form" class="form">
      <label>User name <input name="username" autocomplete="username" required autofocus></label>
      <label>Password <input name="password" type="password" autocomplete="current-password" required></label>
      <button class="btn primary" type="submit">Sign in</button>
    </form></div>`);
  document.getElementById("login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    try {
      await busy(e.submitter, () => post("/api/auth/login", { username: fd.get("username"), password: fd.get("password") }));
      await start();
    } catch (err) { /* toast shown */ }
  });
}

function showForcedPasswordChange() {
  stopStream();
  document.body.classList.add("signed-out");
  document.getElementById("user-box").hidden = true;
  document.getElementById("worker-pill").hidden = true;
  setHTML(document.getElementById("view"), html`<div class="login-card card"><h1>Choose a new password</h1>
    <p class="muted">You signed in with the starter password. Choose your own before continuing.</p>
    <form id="newpw-form" class="form">
      <label>New password (10+ characters) <input name="new" type="password" autocomplete="new-password" minlength="10" required autofocus></label>
      <label>Type it again <input name="confirm" type="password" autocomplete="new-password" minlength="10" required></label>
      <button class="btn primary" type="submit">Save and continue</button>
    </form></div>`);
  document.getElementById("newpw-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    if (fd.get("new") !== fd.get("confirm")) { toast("The two passwords do not match. Nothing was changed.", "error", 9000); return; }
    try {
      await busy(e.submitter, () => post("/api/auth/password", { new: fd.get("new"), confirm: fd.get("confirm") }));
      await start();
    } catch (err) { /* toast shown */ }
  });
}

function showUser() {
  const box = document.getElementById("user-box");
  if (!authUser) { box.hidden = true; return; }
  box.hidden = false;
  box.querySelector(".uname").textContent = `${authUser.name} (${authUser.role})`;
}

function showVersion(st) {
  const el = document.getElementById("ver");
  if (el && st.version_label) { el.textContent = `Audiobook Studio ${st.version_label}`; el.title = "About Audiobook Studio"; }
}

async function refreshUpdateDot() {
  try {
    const u = await get("/api/update/summary");
    const dot = document.getElementById("update-dot");
    if (dot) dot.hidden = !(u.managed && ["available", "staged", "failed", "rolled_back"].includes(u.state));
  } catch (e) { /* not signed in yet, or restarting */ }
}

async function start() {
  const st = await get("/api/auth/status");
  showVersion(st);
  if (st.auth_required && !st.user) { showLogin(); return; }
  if (st.user && st.user.must_change) { showForcedPasswordChange(); return; }
  authUser = st.user;
  document.body.classList.remove("signed-out");
  document.getElementById("worker-pill").hidden = false;
  showUser();
  try { updateWorkerPill(await get("/api/worker")); } catch (e) { /* shown by banner */ }
  refreshUpdateDot();
  setInterval(refreshUpdateDot, 300000);
  window.addEventListener("hashchange", refreshUpdateDot);
  connectSSE();
  route();
}

window.addEventListener("studio:unauth", () => showLogin("Your session ended. Please sign in again."));

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("signout").addEventListener("click", async () => {
    await post("/api/auth/logout").catch(() => {});
    authUser = null; showLogin();
  });
  document.getElementById("chpw").addEventListener("click", async () => {
    const res = await dialog({ title: "Change password", form: true, buttons: [{ label: "Change", value: "ok", kind: "primary" }],
      body: html`<label>Current password <input name="current" type="password" autocomplete="current-password" required></label>
        <label>New password (10+ characters) <input name="new" type="password" autocomplete="new-password" minlength="10" required></label>
        <label>Type the new password again <input name="confirm" type="password" autocomplete="new-password" minlength="10" required></label>` });
    if (!res) return;
    if (res.data.new !== res.data.confirm) { toast("The two new passwords do not match. Nothing was changed.", "error", 9000); return; }
    try { await post("/api/auth/password", { current: res.data.current, new: res.data.new, confirm: res.data.confirm }); toast("Password changed", "success"); }
    catch (e) { toastError(e); }
  });
  document.getElementById("nav-toggle").addEventListener("click", (e) => {
    const open = !document.body.classList.contains("nav-open");
    document.body.classList.toggle("nav-open", open);
    e.currentTarget.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeNav(); });
  window.addEventListener("hashchange", () => { if (authUser !== null || !document.body.classList.contains("signed-out")) route(); });
  window.addEventListener("unhandledrejection", (e) => { if (e.reason && e.reason.name !== "AbortError" && !e.reason.status) console.error(e.reason); });
  start().catch(toastError);
});
