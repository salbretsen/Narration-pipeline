// Unit tests for static/js/runtime.js (fmtSpan / fmtClock / fmtRuntime). Run: node tests/js/runtime_fmt.test.mjs (also run by pytest).
import assert from "node:assert/strict";

globalThis.window = new EventTarget();
globalThis.document = { activeElement: null };
const { fmtSpan, fmtClock, fmtRuntime } = await import("../../studio/web/static/js/runtime.js");

let failed = 0;
function test(name, fn) {
  try { fn(); console.log("ok   " + name); } catch (e) { failed++; console.log("FAIL " + name + "\n" + (e && e.stack || e)); }
}
const text = (rt, o) => String(fmtRuntime(rt, o)).replace(/<[^>]+>/g, "");

test("fmtSpan", () => {
  assert.equal(fmtSpan(3 * 3600 + 12 * 60), "3 h 12 m");
  assert.equal(fmtSpan(2 * 3600 + 5 * 60), "2 h 05 m");
  assert.equal(fmtSpan(3599.7), "1 h 00 m");
  assert.equal(fmtSpan(310), "5 m 10 s");
  assert.equal(fmtSpan(45), "45 s");
  assert.equal(fmtSpan(0), "0 s");
  assert.equal(fmtSpan(null), "–");
});
test("fmtClock is local HH:MM, with a weekday beyond 12 h", () => {
  const soon = new Date(Date.now() + 3600 * 1000);
  assert.equal(fmtClock(soon.getTime() / 1000), `${String(soon.getHours()).padStart(2, "0")}:${String(soon.getMinutes()).padStart(2, "0")}`);
  assert.match(fmtClock(Date.now() / 1000 + 30 * 3600), /^(Sun|Mon|Tue|Wed|Thu|Fri|Sat) \d\d:\d\d$/);
});
test("fmtRuntime renders each state", () => {
  const now = Date.now() / 1000;
  const run = { state: "running", remaining_s: 4800, finish_at: now + 4800, starts_at: null, total_est_s: null, processing_s: null, estimated: true, basis: "b" };
  assert.match(text(run), /^about 1 h 20 m left · done about \d\d:\d\d$/);
  assert.equal(text(run, { compact: true }), "1 h 20 m left");
  const q = { state: "queued", total_est_s: 7500, starts_at: now + 3600, finish_at: now + 11100, remaining_s: null, processing_s: null, estimated: true, basis: "x" };
  assert.match(text(q), /^about 2 h 05 m processing · starts about \d\d:\d\d$/);
  assert.match(text({ ...q, basis: "paused, so it is not advancing; x", starts_at: null }), /^paused · about 2 h 05 m processing once resumed$/);
  assert.equal(text({ state: "finished", processing_s: 11520, estimated: false, basis: "" }), "processed in 3 h 12 m");
  assert.equal(text({ state: "idle", total_est_s: 600, basis: "" }), "about 10 m 00 s to narrate");
});
test("no data / absent", () => {
  assert.equal(String(fmtRuntime(null)), "");
  assert.equal(String(fmtRuntime(undefined)), "");
  assert.equal(text({ state: "running", remaining_s: null, basis: "estimate after 7 more segments" }), "estimate after 7 more segments");
  assert.equal(String(fmtRuntime({ state: "idle", total_est_s: null, basis: "nothing left to render" })), "");
});
test("basis is the tooltip and is escaped", () => {
  assert.match(String(fmtRuntime({ state: "finished", processing_s: 5, basis: 'a "b" <c>' })), /title="Processing time, not audio length\. Actual: a &quot;b&quot; &lt;c&gt;\."/);
});
if (failed) { console.log(`${failed} failed`); process.exit(1); }
console.log("all passed");
