// Unit tests for static/js/views/settings-model.js: tab routing, dirty tracking and (above all) that the saved payload is unchanged.
// Run: node tests/js/settings_model.test.mjs (also run by pytest). Pure logic only; the tabbed page itself is exercised in a real browser
// by tools/e2e/polish_settings.py.
import assert from "node:assert/strict";

globalThis.window = new EventTarget();
globalThis.document = { activeElement: null };
const M = await import("../../studio/web/static/js/views/settings-model.js");
const NF = await import("../../studio/web/static/js/views/narrative-fields.js");

let passed = 0, failed = 0;
async function test(name, fn) {
  try { await fn(); passed++; console.log("ok   " + name); }
  catch (e) { failed++; console.log("FAIL " + name + "\n" + (e && e.stack || e)); }
}

// ---- the v0.8.0 collect(), copied verbatim, as the reference for payload identity -------------------------------------------
function referenceCollect(form) {
  const out = {};
  const set = (path, v) => { const [a, b] = path.split("."); (out[a] = out[a] || {})[b] = v; };
  for (const [sec, , fields] of M.SECTIONS) for (const [key, , type] of fields) {
    const el = form.elements[`${sec}.${key}`]; if (!el) continue;
    if (type === "bool") set(`${sec}.${key}`, el.checked);
    else if (type === "number") set(`${sec}.${key}`, el.value === "" ? null : Number(el.value));
    else if (type === "number?") set(`${sec}.${key}`, el.value === "" ? null : Number(el.value));
    else if (type === "select") set(`${sec}.${key}`, Number(el.value));
    else if (type === "list") set(`${sec}.${key}`, el.value.split(",").map((x) => x.trim()).filter(Boolean).map(Number));
    else set(`${sec}.${key}`, el.value.trim());
  }
  const nar = {};
  for (const f of NF.allFields()) { const v = NF.readInput(form, "narrative.", f); if (v !== undefined) NF.setPath(nar, f[0], v); }
  out.narrative = nar;
  out.storage = {};
  form.querySelectorAll("[name^='storage.']").forEach((el) => { out.storage[el.name.split(".")[1]] = el.value.trim(); });
  return out;
}

/** A fake <form> holding one control per field of the page, with deliberately awkward values (blank optional numbers, padded text, a list). */
function fakeForm() {
  const elements = {};
  let n = 1;
  const put = (name, type, opt = {}) => {
    if (type === "bool") elements[name] = { checked: n++ % 2 === 0, value: "on" };
    else if (type === "select") elements[name] = { value: String(opt.options ? (Array.isArray(opt.options[0]) ? opt.options[0][0] : opt.options[1]) : 0) };
    else if (type === "text") elements[name] = { value: n++ % 3 === 0 ? "" : `  v${n}  ` };
    else if (type === "list") elements[name] = { value: " 0.5, 0.7 ,, 0.9 " };
    else if (type === "number?") elements[name] = { value: n++ % 2 ? "" : "0.8" };
    else elements[name] = { value: String(n++ * 10) };
  };
  for (const [sec, , fields] of M.SECTIONS) for (const [key, , type, opt] of fields) put(`${sec}.${key}`, type, opt);
  for (const [path, , type, opt] of NF.allFields()) put(`narrative.${path}`, type, opt);
  const storage = ["library_root", "audio_dir", "exports_dir"].map((k) => ({ name: `storage.${k}`, value: `  /Volumes/X/${k}  ` }));
  for (const s of storage) elements[s.name] = s;
  elements["secret.llm"] = { value: "never-collected" };
  return { elements, querySelectorAll: (sel) => (sel === "[name^='storage.']" ? storage : []) };
}

await test("collectSettings produces exactly the v0.8.0 payload", () => {
  const f = fakeForm();
  assert.deepEqual(M.collectSettings(f), referenceCollect(f));
});

await test("the payload has every section and never carries the write-only API key", () => {
  const p = M.collectSettings(fakeForm());
  assert.deepEqual(Object.keys(p).sort(), ["chunking", "export", "models", "narrative", "pauses", "qa", "server", "storage", "worker"]);
  assert.ok(!JSON.stringify(p).includes("never-collected"));
  assert.deepEqual(p.models.retry_temperatures, [0.5, 0.7, 0.9]);
  assert.equal(typeof p.narrative.llm, "object");
  assert.equal(typeof p.narrative.delivery, "object");
  assert.equal(p.storage.library_root, "/Volumes/X/library_root");
});

await test("every settings field is defined once and every narrative label override names a real field", () => {
  const names = M.SECTIONS.flatMap(([sec, , fs]) => fs.map(([k]) => `${sec}.${k}`));
  assert.equal(new Set(names).size, names.length);
  const paths = new Set(NF.allFields().map((f) => f[0]));
  for (const p of Object.keys(M.NARRATIVE_LABELS)) assert.ok(paths.has(p), "unknown narrative path " + p);
  for (const p of paths) assert.ok(M.NARRATIVE_LABELS[p], "no page label for " + p);   // every field is shown on this page
});

await test("resolveTab: sub-route to tab, unknown falls back to storage", () => {
  assert.equal(M.resolveTab([]), "storage");
  assert.equal(M.resolveTab(undefined), "storage");
  for (const t of M.TABS) assert.equal(M.resolveTab([t.id]), t.id);
  assert.equal(M.resolveTab(["nope"]), "storage");
  assert.deepEqual(M.TABS.map((t) => t.id), ["storage", "production", "quality", "models", "narrative", "llm", "server"]);
});

await test("tabKeyTarget: arrows wrap, Home/End jump, other keys ignored", () => {
  assert.equal(M.tabKeyTarget("ArrowDown", 0, 7), 1);
  assert.equal(M.tabKeyTarget("ArrowRight", 6, 7), 0);
  assert.equal(M.tabKeyTarget("ArrowUp", 0, 7), 6);
  assert.equal(M.tabKeyTarget("ArrowLeft", 3, 7), 2);
  assert.equal(M.tabKeyTarget("Home", 4, 7), 0);
  assert.equal(M.tabKeyTarget("End", 4, 7), 6);
  assert.equal(M.tabKeyTarget("a", 4, 7), null);
  assert.equal(M.tabKeyTarget("Home", 0, 0), null);
});

await test("changedNames: text, number and checkbox differences; secrets and unchanged values are ignored", () => {
  const base = { "a.x": "1", "a.flag": { on: false }, "b.y": "q", "secret.llm": "" };
  assert.deepEqual(M.changedNames(base, { ...base }), []);
  assert.deepEqual(M.changedNames(base, { ...base, "a.x": "2", "a.flag": { on: true }, "secret.llm": "typed" }), ["a.flag", "a.x"]);
  assert.deepEqual(M.changedNames(base, { ...base, "a.x": "1 " }), ["a.x"]);   // whitespace is a change until trimmed on save
  assert.deepEqual(M.changedNames(base, { ...base, "new": "z" }), ["new"]);
  assert.deepEqual(M.changedNames(null, null), []);
});

await test("countByGroup counts per tab and skips unknown names", () => {
  const tab = { "a": "storage", "b": "storage", "c": "llm" };
  assert.deepEqual(M.countByGroup(["a", "b", "c", "zzz"], (n) => tab[n]), { storage: 2, llm: 1 });
});

await test("verifySummary explains how the master switch and the mode combine", () => {
  assert.equal(M.verifySummary(false, "every", 0.25).on, false);
  assert.match(M.verifySummary(false, "every", 0.25).text, /off/i);
  assert.equal(M.verifySummary(true, "off", 0.25).on, false);
  assert.match(M.verifySummary(true, "sample", 0.25).text, /25%/);
  assert.equal(M.verifySummary(true, "sample", "0.5").text.includes("50%"), true);
  assert.equal(M.verifySummary(true, "every", 0.25).on, true);
  assert.match(M.verifySummary(true, "every", 0).text, /Every segment/);
});

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed ? 1 : 0);
