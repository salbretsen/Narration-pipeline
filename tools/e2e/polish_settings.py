"""Drive the v0.8 tabbed Settings page and the global page-width rules in a real browser (Edge) against a local MOCK-engine studio.
No Mac, no GPU, no network.

    python tools/e2e/polish_settings.py OUT_DIR

Proves, in a real browser:
  * (a) every settings tab opens from its URL (#/settings/<tab>), by click and by keyboard (arrows wrap, Home/End), with role/aria state
  * (b) edits in two different tabs survive switching tabs, each tab shows an unsaved dot, ONE Save writes both (GET /api/settings),
        Discard resets, leaving the page with edits asks first, and a back/forward re-render restores the draft
  * (c) the PUT body equals the settings JSON apart from the edited fields (same sections and keys, nothing added or dropped)
  * (d) the narrative segment limits appear only while semantic segmentation is on, and are still saved when hidden; the Whisper
        summary follows the master switch
  * (e) layout: at 2560 px #view is at most 1500 px wide and centred in the content area on overview/library/queue/settings; no
        horizontal scrollbar at 360 and 768 px; screenshots of every view at 1280, 1920 and 2560 px go to OUT_DIR
  * (f) no console errors or warnings
Not covered: real Safari, the real engine, a real Mac.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "app"))
sys.path.insert(0, str(ROOT / "src" / "app" / "tests"))
OUT = Path(sys.argv[1])
OUT.mkdir(parents=True, exist_ok=True)

home = Path(tempfile.mkdtemp(prefix="studio-ui-"))
os.environ.update({"AUDIOBOOK_STUDIO_HOME": str(home / "state"), "STUDIO_ENGINE": "mock", "STUDIO_NO_WORKER": "1", "STUDIO_NO_UPDATE_CHECK": "1"})

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from conftest import make_text  # noqa: E402

from studio.context import Context  # noqa: E402
from studio.engines import MockASR, MockEngine  # noqa: E402
from studio.web.app import create_app  # noqa: E402
from studio.worker import Worker  # noqa: E402

ctx = Context(home / "state")
app = create_app(ctx, start_worker=False)
PORT = 7935
app.state.port = PORT
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="on"))
threading.Thread(target=server.run, daemon=True).start()
time.sleep(1.5)
B = f"http://127.0.0.1:{PORT}"
api = httpx.Client(base_url=B, headers={"X-Studio-Request": "1"}, timeout=30)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures.append(name)


def info(msg: str) -> None:
    print("INFO " + msg)


def until(fn, timeout: float = 15.0, what: str = "condition") -> None:
    """Poll from Python: the app's CSP forbids unsafe-eval, which is what Playwright's string-based wait_for_function needs."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return
        except Exception:
            pass
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# ---- fixtures: two books (one rendered, so the Review page has segments) ----------------------------------------------------
ryan = next(v["id"] for v in api.get("/api/voices").json()["voices"] if v["name"] == "Ryan")


def make_book(title: str, queue: bool) -> int:
    prev = api.post("/api/imports", files={"file": (f"{title}.txt", make_text(2, title=title).encode(), "text/plain")}).json()
    return api.post(f"/api/imports/{prev['token']}/confirm", json={"title": title, "voice_id": ryan, "queue": queue}).json()["id"]


A_ID = make_book("Alpha Tale", True)
B_ID = make_book("Beta Tale", False)
worker = Worker(ctx, tts=MockEngine({}), asr=MockASR())
threading.Thread(target=worker.run_forever, kwargs={"idle_sleep": 0.3}, daemon=True).start()
until(lambda: not [j for j in api.get("/api/queue").json()["jobs"] if j["status"] in ("queued", "running")], 90, "Alpha Tale rendered")

TABS = ["storage", "production", "quality", "models", "narrative", "llm", "server"]
VIEWS = {"overview": "#/overview", "library": "#/library", "queue": "#/queue", "voices": "#/voices", "review": f"#/review/{A_ID}", "review-picker": "#/review",
         "book": f"#/book/{A_ID}", "narrative": f"#/narrative/{A_ID}", "disk": "#/disk", "about": "#/about", "updates": "#/updates",
         "pronunciation": "#/pronunciation", **{f"settings-{t}": f"#/settings/{t}" for t in TABS}}


def flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flat(v, prefix + k + "."))
        else:
            out[prefix + k] = v
    return out


from playwright.sync_api import sync_playwright  # noqa: E402

errors: list[str] = []
puts: list[dict] = []
with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True)
    pg = br.new_page(viewport={"width": 1280, "height": 900})
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on("request", lambda r: puts.append(r.post_data_json) if r.method == "PUT" and r.url.endswith("/api/settings") else None)

    def goto(hash_: str, settle: str = "#view h1") -> None:
        """Load a route. A hash-only change does not reload, so wait until the router has swapped the page (the old page is removed)."""
        pg.goto(B + "/" + hash_)
        time.sleep(0.25)
        pg.wait_for_selector(settle, timeout=15000)
        until(lambda: pg.locator("#view > .view-page").count() == 1 and pg.locator("#view.view-stale").count() == 0, 15, "router settled")

    def sel_tab(t: str) -> bool:
        return pg.get_attribute(f"#tab-{t}", "aria-selected") == "true"

    def only_visible_panel(t: str) -> bool:
        return all(pg.locator(f"#panel-{x}").is_visible() == (x == t) for x in TABS)

    def val(name: str) -> str:
        return pg.eval_on_selector(f'[name="{name}"]', "e => e.type === 'checkbox' ? String(e.checked) : e.value")

    def status() -> str:
        return pg.inner_text("#set-status").strip()

    # ------------------------------------------------------------------ (a) every tab by URL, click and keyboard
    goto("#/settings", "#settings-form")
    check("(a) #/settings opens the Storage tab", sel_tab("storage") and only_visible_panel("storage"))
    check("(a) tablist / tab / tabpanel roles and wiring", pg.get_attribute(".set-tabs", "role") == "tablist" and pg.locator('[role="tab"]').count() == 7
          and pg.locator('[role="tabpanel"]').count() == 7 and pg.get_attribute("#tab-models", "aria-controls") == "panel-models"
          and pg.get_attribute("#panel-models", "aria-labelledby") == "tab-models")
    check("(a) only the selected tab is in the tab order", pg.eval_on_selector_all('[role="tab"]', "els => els.filter(e => e.tabIndex === 0).length") == 1)
    for t in TABS:
        goto(f"#/settings/{t}", "#settings-form")
        check(f"(a) URL #/settings/{t} opens that tab only", sel_tab(t) and only_visible_panel(t))
    pg.evaluate("location.hash = '#/settings/llm'")
    until(lambda: sel_tab("llm"), 5, "hashchange switches tab")
    check("(a) changing the hash switches tab without leaving the page", sel_tab("llm") and only_visible_panel("llm"))
    for t in TABS:
        pg.click(f"#tab-{t}")
        check(f"(a) click selects {t} and puts it in the URL", sel_tab(t) and only_visible_panel(t) and pg.url.endswith(f"#/settings/{t}"))
    pg.focus("#tab-storage")
    pg.keyboard.press("ArrowDown")
    check("(a) ArrowDown moves focus and selection to the next tab", sel_tab("production") and pg.evaluate("document.activeElement.id") == "tab-production")
    pg.keyboard.press("End")
    check("(a) End selects the last tab", sel_tab("server") and pg.evaluate("document.activeElement.id") == "tab-server")
    pg.keyboard.press("ArrowRight")
    check("(a) ArrowRight wraps to the first tab", sel_tab("storage"))
    pg.keyboard.press("ArrowUp")
    check("(a) ArrowUp wraps to the last tab", sel_tab("server"))
    pg.keyboard.press("Home")
    check("(a) Home selects the first tab", sel_tab("storage"))
    # the Production tab points at the Narrative tab and back
    pg.click("#tab-production")
    pg.click('#panel-production [data-tab-link="narrative"]')
    check("(a) the in-page link to the Narrative tab works", sel_tab("narrative") and pg.evaluate("document.activeElement.id") == "tab-narrative")

    # ------------------------------------------------------------------ (b) edits across tabs, dots, one Save, Discard, leave warning
    before = api.get("/api/settings").json()
    goto("#/settings/storage", "#settings-form")
    check("(b) no unsaved changes at first, Discard disabled", status() == "No unsaved changes" and pg.is_disabled("#set-discard") and pg.locator(".tab-dot:visible").count() == 0)
    pg.click("#tab-production")
    pg.fill('[name="chunking.min_chars"]', "123")
    pg.click("#tab-llm")
    pg.fill('[name="narrative.llm.model"]', "edit-model-x")
    pg.click("#tab-models")
    pg.fill('[name="models.temperature"]', "0.55")
    check("(b) three edits in three tabs are counted", status() == "3 unsaved changes", status())
    check("(b) each edited tab shows a dot, others do not", [pg.locator(f"#tab-{t} .tab-dot").is_visible() for t in TABS] == [t in ("production", "llm", "models") for t in TABS])
    check("(b) the dot has a screen-reader text", "unsaved" in (pg.text_content("#tab-llm .tab-dirty") or "").lower() and pg.locator("#tab-llm .tab-dirty").get_attribute("hidden") is None)
    for t in ["production", "storage", "llm", "production", "models"]:
        pg.click(f"#tab-{t}")
    check("(b) values are kept while switching tabs back and forth", val("chunking.min_chars") == "123" and val("narrative.llm.model") == "edit-model-x"
          and val("models.temperature") == "0.55")
    n_puts = len(puts)
    pg.click("#tab-narrative")
    pg.click("#set-save")
    until(lambda: len(puts) == n_puts + 1, 10, "one PUT")
    until(lambda: status() == "No unsaved changes", 10, "saved")
    time.sleep(0.4)
    after = api.get("/api/settings").json()
    check("(b) ONE Save wrote all three edits", after["settings"]["chunking"]["min_chars"] == 123 and after["settings"]["narrative"]["llm"]["model"] == "edit-model-x"
          and after["settings"]["models"]["temperature"] == 0.55)
    check("(b) exactly one PUT was sent", len(puts) == n_puts + 1)
    check("(b) dots and count cleared after saving, still on the same tab", pg.locator(".tab-dot:visible").count() == 0 and sel_tab("narrative"))

    # (c) payload identity: same sections and keys as the settings JSON, only the edits differ
    body = puts[-1]
    sent, was = flat(body), flat(before["settings"])
    expected = dict(was)
    expected["chunking.min_chars"] = 123
    expected["narrative.llm.model"] = "edit-model-x"
    expected["models.temperature"] = 0.55
    # Keys the form has never owned (v0.8.0 did not send them either; settings_model.test.mjs proves the body equals the v0.8.0 one):
    # engine, host / remote-access / HTTPS (deliberately not editable here), updater settings, version, GPU worker list.
    not_ui = {"models.engine", "server.allow_remote", "server.allowed_hosts", "server.host", "server.https_enabled", "server.https_port", "updates.auto_check",
              "updates.channel", "updates.check_interval_hours", "updates.keep_releases", "updates.repository", "version", "worker.gpu_workers"}
    check("(c) PUT has the settings sections the form owns (chunking, qa, pauses, models, export, worker, server, narrative, storage)",
          set(body) == {"chunking", "qa", "pauses", "models", "export", "worker", "server", "narrative", "storage"}, str(sorted(body)))
    check("(c) narrative carries llm and delivery", isinstance(body["narrative"].get("llm"), dict) and isinstance(body["narrative"].get("delivery"), dict))
    missing, extra = sorted(set(was) - set(sent)), sorted(set(sent) - set(was))
    info(f"payload leaf keys: sent {len(sent)}, settings {len(was)}, missing from PUT {missing}, extra in PUT {extra}")
    check("(c) PUT adds no key the settings do not have", not extra, str(extra))
    check("(c) every key the form owns is sent; only the known read-only keys are absent", set(missing) <= not_ui, str(sorted(set(missing) - not_ui)))
    diff = {k: (sent[k], expected[k]) for k in sent if k in expected and sent[k] != expected[k]}
    check("(c) PUT values equal the loaded values apart from the three edits", not diff, str(dict(list(diff.items())[:6])))

    # discard
    pg.click("#tab-production")
    pg.fill('[name="chunking.min_chars"]', "200")
    pg.click("#tab-quality")
    pg.uncheck('[name="qa.enable_asr"]') if val("qa.enable_asr") == "true" else pg.check('[name="qa.enable_asr"]')
    check("(b) two new edits are counted", status() == "2 unsaved changes", status())
    pg.click("#set-discard")
    check("(b) Discard resets both edits and clears the dots", val("chunking.min_chars") == "123" and val("qa.enable_asr") == str(after["settings"]["qa"]["enable_asr"]).lower()
          and status() == "No unsaved changes" and pg.locator(".tab-dot:visible").count() == 0)

    # leaving with edits asks first
    pg.click("#tab-production")
    pg.fill('[name="chunking.min_chars"]', "222")
    pg.click('a[data-nav="queue"]')
    time.sleep(0.4)
    check("(b) leaving the page with edits opens a confirmation, and stays", pg.locator("dialog.modal[open]").count() == 1 and "settings" in pg.url)
    pg.click("dialog.modal button.ghost")
    until(lambda: pg.locator("dialog.modal[open]").count() == 0, 5, "dialog closed")
    check("(b) Cancel keeps the page and the edit", "settings" in pg.url and val("chunking.min_chars") == "222")
    pg.click("#tab-models")
    check("(b) links that stay inside Settings are never blocked", sel_tab("models") and pg.locator("dialog.modal[open]").count() == 0)
    # back/forward style navigation cannot be blocked: the draft is restored when the page is shown again
    pg.evaluate("location.hash = '#/about'")
    pg.wait_for_selector("#view h1:has-text('About')", timeout=10000) if pg.locator("#view h1:has-text('About')").count() else time.sleep(0.8)
    pg.evaluate("location.hash = '#/settings/production'")
    pg.wait_for_selector("#settings-form", timeout=10000)
    until(lambda: val("chunking.min_chars") == "222", 5, "draft restored")
    check("(b) a draft is restored when the page is re-rendered after leaving by back/forward", val("chunking.min_chars") == "222" and status() == "1 unsaved change", status())
    pg.click('a[data-nav="queue"]')
    pg.wait_for_selector("dialog.modal[open]", timeout=5000)
    pg.click("dialog.modal button.danger")
    until(lambda: pg.evaluate("location.hash") == "#/queue", 8, "left after confirming")
    goto("#/settings/production", "#settings-form")
    check("(b) confirming 'Discard and leave' really discards", val("chunking.min_chars") == "123" and status() == "No unsaved changes")

    # ------------------------------------------------------------------ (d) dependent fields and the Whisper summary
    pg.click("#tab-narrative")
    sem = '[name="narrative.semantic_segmentation"]'
    on0 = val("narrative.semantic_segmentation") == "true"
    if not on0:
        pg.check(sem)
    check("(d) segment length limits are visible while semantic segmentation is on", pg.locator('[name="narrative.max_segment_chars"]').is_visible() and pg.locator('[name="narrative.min_segment_chars"]').is_visible())
    pg.uncheck(sem)
    check("(d) segment length limits hide when it is off", not pg.locator('[name="narrative.max_segment_chars"]').is_visible() and not pg.locator('[name="narrative.min_segment_chars"]').is_visible())
    n_puts = len(puts)
    pg.click("#set-save")
    until(lambda: len(puts) == n_puts + 1, 10, "PUT with hidden fields")
    check("(d) the hidden limits are still part of the saved payload", "max_segment_chars" in puts[-1]["narrative"] and "min_segment_chars" in puts[-1]["narrative"])
    until(lambda: pg.get_attribute("#set-save", "aria-busy") is None, 10, "save finished")
    time.sleep(0.8)   # the page repaints from the saved values once the storage summary has been re-read
    check("(d) the semantic setting saved as off", api.get("/api/settings").json()["settings"]["narrative"]["semantic_segmentation"] is False)
    pg.check(sem)
    check("(d) the limits come back when it is turned on again", pg.locator('[name="narrative.max_segment_chars"]').is_visible())
    pg.click("#set-discard")
    check("(d) Discard puts semantic segmentation back to the saved (off) state and hides the limits again", not pg.locator('[name="narrative.max_segment_chars"]').is_visible())
    pg.click("#tab-quality")
    pg.check('[name="qa.enable_asr"]')
    pg.select_option('[name="narrative.verify_mode"]', "every")
    check("(d) Whisper summary: master on + every", "every segment" in pg.inner_text("#verify-summary").lower())
    pg.select_option('[name="narrative.verify_mode"]', "sample")
    pg.fill('[name="narrative.verify_sample_rate"]', "0.4")
    check("(d) Whisper summary: sample share", "40%" in pg.inner_text("#verify-summary"))
    pg.uncheck('[name="qa.enable_asr"]')
    check("(d) Whisper summary: master switch off wins over the mode", "off" in pg.inner_text("#verify-summary").lower() and "no take is checked" in pg.inner_text("#verify-summary").lower())
    pg.click("#set-discard")

    # keep the settings as found for the screenshots
    api.put("/api/settings", json={"chunking": {"min_chars": before["settings"]["chunking"]["min_chars"]}, "narrative": {"llm": {"model": before["settings"]["narrative"]["llm"]["model"]}, "semantic_segmentation": before["settings"]["narrative"]["semantic_segmentation"]},
                                   "models": {"temperature": before["settings"]["models"]["temperature"]}})

    # ------------------------------------------------------------------ (e) layout
    def geom() -> dict:
        return pg.evaluate("""() => {
          const v = document.getElementById('view').getBoundingClientRect(), m = document.getElementById('main').getBoundingClientRect();
          const cs = getComputedStyle(document.getElementById('main'));
          const l = m.left + parseFloat(cs.paddingLeft), r = m.right - parseFloat(cs.paddingRight);
          const se = document.scrollingElement;
          return { vw: v.width, left: v.left - l, right: r - v.right, vleft: v.left, vright: innerWidth - v.right, scrollW: se.scrollWidth, clientW: se.clientWidth };
        }""")

    pg.set_viewport_size({"width": 2560, "height": 1300})
    for name in ("overview", "library", "queue", "settings-storage"):
        goto(VIEWS[name], "#view h1")
        time.sleep(0.6)
        g = geom()
        check(f"(e) 2560 px {name}: #view <= 1500 px wide", g["vw"] <= 1500.5, f"{g['vw']:.0f}px")
        check(f"(e) 2560 px {name}: #view centred in the content area (margins within 2 px)", abs(g["left"] - g["right"]) <= 2, f"left {g['left']:.1f}, right {g['right']:.1f}")
    goto(VIEWS["review"], ".review-layout")
    g = geom()
    check("(e) 2560 px review: workspace may use up to 1680 px", g["vw"] <= 1680.5 and g["vw"] > 1500, f"{g['vw']:.0f}px")

    for width, tag in ((1280, "1280"), (1920, "1920"), (2560, "2560")):
        pg.set_viewport_size({"width": width, "height": 1300 if width > 1920 else 1000})
        for name, h in VIEWS.items():
            goto(h, "#view h1" if name != "review" else ".review-layout")
            time.sleep(0.5)
            pg.screenshot(path=str(OUT / f"{tag}-{name}.png"))
    info(f"screenshots written to {OUT}")

    for width in (360, 768):
        pg.set_viewport_size({"width": width, "height": 900})
        for name, h in VIEWS.items():
            goto(h, "#view h1" if name != "review" else ".review-layout")
            time.sleep(0.5)
            g = geom()
            required = name.split("-")[0] in ("overview", "library", "queue", "settings")
            ok = g["scrollW"] <= g["clientW"]
            if required:
                check(f"(e) {width} px {name}: no horizontal scrollbar", ok, f"scrollWidth {g['scrollW']} / clientWidth {g['clientW']}")
            else:
                info(f"{width} px {name}: {'no horizontal scroll' if ok else 'HORIZONTAL SCROLL'} (scrollWidth {g['scrollW']} / clientWidth {g['clientW']})")
            if width == 360 and name in ("settings-storage", "settings-llm", "overview"):
                pg.screenshot(path=str(OUT / f"360-{name}.png"))
    # tabs become a horizontal scroller on narrow screens and stay usable
    pg.set_viewport_size({"width": 360, "height": 900})
    goto("#/settings/storage", "#settings-form")
    check("(e) 360 px: tabs are laid out in a row", pg.evaluate("getComputedStyle(document.querySelector('.set-tabs')).flexDirection") == "row")
    pg.click("#tab-server")
    check("(e) 360 px: a tab can be selected", sel_tab("server") and only_visible_panel("server"))

    br.close()

worker.stop_event.set()
bad = [e for e in errors if "favicon" not in e]
check("(f) no console errors or page errors", not bad, "; ".join(bad[:4]))
print("\nFAILED:" if failures else "\nALL CHECKS PASSED", failures if failures else "")
sys.exit(1 if failures else 0)
