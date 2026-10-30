// Unit tests for static/js/views/narrator-model.js. Run: node tests/js/narrator_model.test.mjs (also run by pytest).
import assert from "node:assert/strict";

const M = await import("../../studio/web/static/js/views/narrator-model.js");

let failed = 0;
function test(name, fn) {
  try { fn(); console.log("ok   " + name); } catch (e) { failed++; console.log("FAIL " + name + "\n" + (e && e.stack || e)); }
}

test("tileState: generate, wait, ready, playing, blocked autoplay", () => {
  assert.equal(M.tileState({ status: "none" }).state, "none");
  assert.equal(M.tileState(undefined).state, "none");
  const q = M.tileState({ status: "queued", job_id: 4 });
  assert.equal(q.state, "queued"); assert.equal(q.busy, true);
  assert.equal(M.tileState({ status: "running" }).busy, true);
  assert.equal(M.tileState({ status: "ready", url: "/x" }).state, "ready");
  assert.equal(M.tileState({ status: "ready", url: "/x" }, { playing: true }).state, "playing");
  assert.equal(M.tileState({ status: "ready", url: "/x" }, { blocked: true }).state, "blocked");
  assert.equal(M.tileState({ status: "queued" }, { playing: true }).icon, "■");   // stop always wins
});

test("similarityVerdict bands, the adoption limit and mock labelling", () => {
  assert.equal(M.similarityVerdict(null).text, "similarity not measured");
  assert.equal(M.similarityVerdict(0.99).tone, "good");
  assert.equal(M.similarityVerdict(0.975).tone, "warn");
  assert.equal(M.similarityVerdict(0.95).tone, "bad");
  assert.match(M.similarityVerdict(0.975, 0.98).text, /below the adoption limit 0\.980/);
  const mock = M.similarityVerdict(0.5, 0.98, true);
  assert.equal(mock.tone, "neutral"); assert.match(mock.text, /mock engine/);
});

test("byFamily groups emotions in a stable family order and keeps unknown families", () => {
  const g = M.byFamily([{ bucket: "anger", family: "hot" }, { bucket: "joy", family: "warm" }, { bucket: "grief", family: "low" },
    { bucket: "humor", family: "warm" }, { bucket: "odd", family: "strange" }]);
  assert.deepEqual(g.map((x) => x.family), ["warm", "low", "hot", "strange"]);
  assert.deepEqual(g[0].buckets.map((b) => b.bucket), ["joy", "humor"]);
  assert.equal(g[0].label, "Warm");
});

test("tabOf: emotions only for clones, unknown tabs fall back to the overview", () => {
  const q = (t) => new URLSearchParams(t ? `tab=${t}` : "");
  assert.equal(M.tabOf(q("emotions"), "clone"), "emotions");
  assert.equal(M.tabOf(q("emotions"), "preset"), "overview");
  assert.equal(M.tabOf(q("tools"), "design"), "tools");
  assert.equal(M.tabOf(q("bogus"), "clone"), "overview");
  assert.equal(M.tabOf(q(""), "clone"), "overview");
});

test("clipLine and every source has a label and a hint", () => {
  assert.equal(M.clipLine(null), "");
  assert.equal(M.clipLine({ duration_s: 8.2, source: "recording" }), "8.2s · recorded");
  assert.equal(M.clipLine({ duration_s: 9, source: "synthetic", metrics: { candidate_source: "clone" } }), "9s · Read by the narrator");
  for (const k of ["clone", "intensify", "directable", "design"]) assert.ok(M.SOURCES[k].label && M.SOURCES[k].hint, k);
});

console.log(`\n${5 - failed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
