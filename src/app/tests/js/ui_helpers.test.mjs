// Unit tests for the pure / DOM-light helpers in static/js/ui.js. Run: node tests/js/ui_helpers.test.mjs (also run by pytest).
// No DOM library: patchHTML / patchRegions are exercised against a tiny hand-written fake element, which covers the bookkeeping
// (what is saved and restored, and when markup is skipped) but NOT real browser behaviour (that is tools/e2e/foundation_ui.py).
import assert from "node:assert/strict";

// ui.js touches `window` / `document` only inside functions, so install fakes before calling those.
globalThis.window = new EventTarget();
globalThis.document = { activeElement: null };
const ui = await import("../../studio/web/static/js/ui.js");

let passed = 0, failed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log("ok   " + name); }
  catch (e) { failed++; console.log("FAIL " + name + "\n" + (e && e.stack || e)); }
}

// ---- fake DOM -------------------------------------------------------------------------------------------------------
class FakeNode {
  constructor(id, props = {}) { this.id = id; this.scrollTop = 0; this.scrollLeft = 0; this.tagName = "DIV"; this.focused = 0; Object.assign(this, props); }
  focus() { this.focused++; globalThis.document.activeElement = this; }
  setSelectionRange(a, b, d) { this.selectionStart = a; this.selectionEnd = b; this.selectionDirection = d; }
}
class FakeEl extends FakeNode {
  constructor(id, kids = []) { super(id); this.kids = kids; this.next = null; this.sets = 0; }
  querySelectorAll(sel) { return sel === "[id]" ? this.kids.filter((k) => k.id) : []; }
  contains(n) { return n === this || this.kids.includes(n); }
  set innerHTML(v) { this.sets++; this.html = v; if (this.next) { this.kids = this.next; this.next = null; } }
  get innerHTML() { return this.html; }
}

// ---- sameState -------------------------------------------------------------------------------------------------------
await test("sameState: equal and unequal payloads", () => {
  assert.equal(ui.sameState({ a: 1, b: [1, 2] }, { a: 1, b: [1, 2] }), true);
  assert.equal(ui.sameState({ a: 1 }, { a: 2 }), false);
  assert.equal(ui.sameState(null, null), true);
  assert.equal(ui.sameState(null, {}), false);
  assert.equal(ui.sameState(undefined, undefined), true);
  const circ = {}; circ.self = circ;
  assert.equal(ui.sameState(circ, {}), false); // unserialisable: treated as different, never throws
});

// ---- onState -----------------------------------------------------------------------------------------------------------
const emit = (detail) => window.dispatchEvent(new CustomEvent("studio:state", { detail }));
const tick = (ms = 15) => new Promise((r) => setTimeout(r, ms));

await test("onState: first delivery, skips identical payloads, delivers changes, always delivers polled", async () => {
  const seen = [];
  const off = ui.onState((d) => seen.push(d), 0);
  emit({ n: 1 }); await tick();
  emit({ n: 1 }); await tick();          // same data again: skipped
  emit({ n: 2 }); await tick();
  emit({ n: 2 }); await tick();
  emit({ polled: 1, n: 2 }); await tick();
  emit({ polled: 1, n: 2 }); await tick(); // polled payloads are always delivered
  assert.deepEqual(seen.map((d) => String(d.n) + (d.polled ? "p" : "")), ["1", "2", "2p", "2p"]);
  off();
  emit({ n: 99 }); await tick();
  assert.equal(seen.length, 4, "unsubscribed");
});

await test("onState: each subscriber has its own last-delivered payload", async () => {
  const a = [], b = [];
  const offA = ui.onState((d) => a.push(d.n), 0);
  emit({ n: 1 }); await tick();
  const offB = ui.onState((d) => b.push(d.n), 0);
  emit({ n: 1 }); await tick();   // A has seen it, B has not
  assert.deepEqual(a, [1]); assert.deepEqual(b, [1]);
  offA(); offB();
});

await test("onState: throttle coalesces bursts to the latest payload", async () => {
  const seen = [];
  const off = ui.onState((d) => seen.push(d.n), 60);
  emit({ n: 1 }); await tick(10);   // delivered (first)
  emit({ n: 2 }); emit({ n: 3 }); emit({ n: 4 });
  await tick(20);
  assert.deepEqual(seen, [1]);       // still inside the interval
  await tick(100);
  assert.deepEqual(seen, [1, 4]);
  off();
});

await test("onState: a burst that ends back at the delivered value delivers nothing", async () => {
  const seen = [];
  const off = ui.onState((d) => seen.push(d.n), 40);
  emit({ n: 1 }); await tick(60);
  emit({ n: 2 }); emit({ n: 1 }); await tick(100);
  assert.deepEqual(seen, [1]);
  off();
});

// ---- patchHTML ---------------------------------------------------------------------------------------------------------
await test("patchHTML: restores scroll of the element and of id'd descendants, details state, focus and selection", () => {
  const list = new FakeNode("list", { scrollTop: 340, scrollLeft: 5 });
  const anon = new FakeNode("", { scrollTop: 77 }); // no id: cannot be restored, must not crash
  const det = new FakeNode("more", { tagName: "DETAILS", open: true });
  const shut = new FakeNode("less", { tagName: "details", open: false });
  const box = new FakeNode("note", { tagName: "TEXTAREA", selectionStart: 2, selectionEnd: 5, selectionDirection: "forward" });
  const el = new FakeEl("view", [list, anon, det, shut, box]);
  el.scrollTop = 900;
  document.activeElement = box;
  const list2 = new FakeNode("list"), det2 = new FakeNode("more", { tagName: "DETAILS", open: false }), shut2 = new FakeNode("less", { tagName: "DETAILS", open: true });
  const box2 = new FakeNode("note", { tagName: "TEXTAREA", value: "x" });
  el.next = [list2, det2, shut2, box2];
  const ret = ui.patchHTML(el, ui.html`<p>${"new"}</p>`);
  assert.equal(ret, el);
  assert.equal(el.html, "<p>new</p>");
  assert.equal(el.scrollTop, 900);
  assert.deepEqual([list2.scrollTop, list2.scrollLeft], [340, 5]);
  assert.equal(det2.open, true); assert.equal(shut2.open, false);
  assert.equal(box2.focused, 1);
  assert.deepEqual([box2.selectionStart, box2.selectionEnd, box2.selectionDirection], [2, 5, "forward"]);
});

await test("patchHTML: focus on an element that is gone is dropped quietly; non-focused fields are left alone", () => {
  const gone = new FakeNode("old", { tagName: "INPUT", selectionStart: 0, selectionEnd: 0 });
  const el = new FakeEl("v", [gone]);
  document.activeElement = gone;
  const fresh = new FakeNode("other", { tagName: "INPUT" });
  el.next = [fresh];
  ui.patchHTML(el, ui.html`<i></i>`);
  assert.equal(fresh.focused, 0);
  document.activeElement = null;
  const a = new FakeNode("a"); const el2 = new FakeEl("v2", [a]); const a2 = new FakeNode("a"); el2.next = [a2];
  ui.patchHTML(el2, "plain <text>");        // a plain string is escaped, like setHTML
  assert.equal(el2.html, "plain &lt;text&gt;");
  assert.equal(a2.focused, 0);
});

await test("patchHTML: focus outside the patched element is not touched", () => {
  const outside = new FakeNode("elsewhere");
  const el = new FakeEl("v", [new FakeNode("a")]);
  document.activeElement = outside;
  el.next = [new FakeNode("a")];
  ui.patchHTML(el, ui.html`<b></b>`);
  assert.equal(outside.focused, 0);
});

// ---- patchRegions ------------------------------------------------------------------------------------------------------
await test("patchRegions: repaints a region only when its markup changed", () => {
  const r1 = new FakeEl("r1"), r2 = new FakeEl("r2");
  const root = { querySelectorAll: (sel) => (sel === ".a" ? [r1] : sel === ".b" ? [r2] : []) };
  let a = 1, b = 1;
  const regions = { ".a": () => ui.html`<i>${a}</i>`, ".b": () => ui.html`<i>${b}</i>` };
  assert.equal(ui.patchRegions(root, regions), 2);   // first paint
  assert.equal(ui.patchRegions(root, regions), 0);   // nothing changed: no DOM writes
  assert.deepEqual([r1.sets, r2.sets], [1, 1]);
  b = 2;
  assert.equal(ui.patchRegions(root, regions), 1);
  assert.deepEqual([r1.sets, r2.sets], [1, 2]);
  assert.equal(r2.html, "<i>2</i>");
  assert.equal(ui.patchRegions({ querySelectorAll: () => [] }, regions), 0);  // missing regions are ignored
});

// ---- pager ---------------------------------------------------------------------------------------------------------------
const pg = (...a) => ui.pager(...a).s;
await test("pager: range text and prev/next state", () => {
  let h = pg(37, 0, 10);
  assert.match(h, /1–10 of 37/);
  assert.match(h, /data-pager="prev" data-offset="0" aria-label="Previous page" disabled/);
  assert.match(h, /data-pager="next" data-offset="10" aria-label="Next page" >/);
  h = pg(37, 10, 10);
  assert.match(h, /11–20 of 37/); assert.doesNotMatch(h, /disabled/);
  assert.match(h, /data-pager="prev" data-offset="0"/); assert.match(h, /data-pager="next" data-offset="20"/);
  h = pg(37, 30, 10);
  assert.match(h, /31–37 of 37/); assert.match(h, /data-pager="next" data-offset="40" aria-label="Next page" disabled/);
  assert.match(h, /data-pager="prev" data-offset="20" aria-label="Previous page" >/);
});
await test("pager: short and empty lists show no buttons; noun and clamping", () => {
  assert.doesNotMatch(pg(5, 0, 10), /button/); assert.match(pg(5, 0, 10), /1–5 of 5/);
  assert.match(pg(0, 0, 10), /0–0 of 0/);
  assert.match(pg(37, 0, 10, { noun: "books" }), /of 37 books/);
  assert.match(pg(37, 999, 10), /37–37 of 37/);   // offset past the end is clamped
  assert.doesNotMatch(pg(37, 0, 10), /on(click|change)=|style=/);
});
await test("wirePager: delegates clicks, ignores disabled buttons and other targets", () => {
  let handler = null; const calls = [];
  const container = { addEventListener: (t, f) => { handler = f; }, removeEventListener: () => { handler = null; }, contains: () => true };
  const off = ui.wirePager(container, (o, which) => calls.push([o, which]));
  const btn = (extra) => ({ closest: () => ({ disabled: false, dataset: { offset: "20", pager: "next" }, ...extra }) });
  handler({ target: btn({}) });
  handler({ target: btn({ disabled: true }) });
  handler({ target: { closest: () => null } });
  assert.deepEqual(calls, [[20, "next"]]);
  off(); assert.equal(handler, null);
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
