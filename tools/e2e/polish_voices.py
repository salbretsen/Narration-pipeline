"""Drive Narrator Studio (v0.9.0: tiles + the narrator page) in a real browser (Edge) against a local MOCK-engine studio. No Mac, no GPU, no network.

    python tools/e2e/polish_voices.py OUT_DIR

Proves: (a) narrators are tiles with name, description and a play button; the button generates the sample once (queued, then ready) and then only
plays; (b) the name opens the narrator page, whose Tools tab "Delivery..." dialog on built-in Ryan saves version 2 with a callout; (c) "Duplicate &
retune..." on a designed voice opens the copy's page (lineage shown, preview form pre-filled); (d) a promoted clone's Emotion references tab lists 13
emotions, makes ONE take at a time, discards a take, adopts one and discards all; Retune from its source works; (e) typed preview text survives leaving
the page; (f) no console errors. Not covered: real audio quality, the real engine, real Safari autoplay rules.
"""
from __future__ import annotations

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

from studio.context import Context  # noqa: E402
from studio.engines import MockASR, MockEngine  # noqa: E402
from studio.web.app import create_app  # noqa: E402
from studio.worker import Worker  # noqa: E402

ctx = Context(home / "state")
app = create_app(ctx, start_worker=False)
PORT = 7936
app.state.port = PORT
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="on"))
threading.Thread(target=server.run, daemon=True).start()
time.sleep(1.5)
B = f"http://127.0.0.1:{PORT}"
api = httpx.Client(base_url=B, headers={"X-Studio-Request": "1"}, timeout=60)
failures: list[str] = []

DESC = "A gruff older male voice, low and slow, with a dry edge."
NEW_DESC = "A bright young female voice, quick and warm, with a light Irish lilt."
TYPED = "My own preview sentence typed by the owner."


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def until(fn, timeout: float = 20.0, what: str = "condition") -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return
        except Exception:
            pass
        time.sleep(0.15)
    raise AssertionError(f"timed out waiting for {what}")


def voices() -> dict:
    return {v["name"]: v for v in api.get("/api/voices").json()["voices"]}


# ---- data: a designed voice, and a clone promoted from a mock preview (worker thread renders the job)
worker = Worker(ctx, tts=MockEngine({}), asr=MockASR())
assert worker.acquire_lease()
stop = threading.Event()


def loop():
    while not stop.is_set():
        try:
            worker.run_until_idle()
        except Exception:
            pass
        time.sleep(0.3)


threading.Thread(target=loop, daemon=True).start()
design = api.post("/api/voices/design", json={"name": "Gruff captain", "description": DESC}).json()
pj = api.post(f"/api/voices/{design['id']}/preview", json={"text": "The harbour lights trembled on the black water tonight.", "verify": False}).json()
until(lambda: api.get(f"/api/jobs/{pj['id']}").json()["status"] == "completed", 30, "design preview")
clone = api.post("/api/voices/promote", json={"job_id": pj["id"], "name": "Gruff captain (stable)"})
assert clone.status_code == 200, clone.text

from playwright.sync_api import sync_playwright  # noqa: E402

errors: list[str] = []


def tile(pg, name):
    return pg.locator(".voice-grid .narrator-tile", has=pg.locator(f"h3:text-is('{name}')"))


def open_narrator(pg, name, tab=None):
    pg.goto(B + "/#/voices")
    pg.wait_for_selector(".voice-grid .narrator-tile", timeout=15000)
    tile(pg, name).locator("a.tile-link").click()
    pg.wait_for_selector(f"h1:text-is('{name}')", timeout=15000)
    if tab:
        pg.click(f"a.tab:has-text('{tab}')")
        pg.wait_for_selector("a.tab.active:has-text('%s')" % tab, timeout=10000)


with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True, args=["--autoplay-policy=no-user-gesture-required"])
    pg = br.new_page(viewport={"width": 1280, "height": 1000})
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.goto(B + "/#/voices")
    pg.wait_for_selector(".voice-grid .narrator-tile", timeout=15000)

    # ---------------------------------------------------------------- (a) tiles and the play button
    ryan = tile(pg, "Ryan")
    check("(a) a tile shows the name, the description and a play button",
          ryan.locator("h3").inner_text() == "Ryan" and ryan.locator(".tile-desc").inner_text().strip() != "" and ryan.locator("button.play-btn").count() == 1)
    check("(a) the tile has no preview form or edit buttons any more", ryan.locator("textarea").count() == 0 and ryan.locator("button:has-text('Edit')").count() == 0)
    check("(a) 'Add a narrator' is a collapsed section", pg.locator("#add-narrator").evaluate("d => !d.open"))
    pg.screenshot(path=str(OUT / "a-tiles.png"), full_page=True)
    ryan.locator("button.play-btn").click()
    until(lambda: voices()["Ryan"]["sample"]["status"] == "ready", 30, "Ryan sample generated")
    pg.wait_for_selector(".narrator-tile:has(h3:text-is('Ryan')) button.play-btn.playing, .narrator-tile:has(h3:text-is('Ryan')) button.play-btn.ready", timeout=15000)
    check("(a) ▶ generated the sample and the tile is now playable", tile(pg, "Ryan").locator("button.play-btn.playing, button.play-btn.ready").count() == 1)
    second = api.post(f"/api/voices/{voices()['Ryan']['id']}/sample").json()
    check("(a) asking again just plays the kept sample (nothing new is generated)", second["status"] == "ready")

    # ---------------------------------------------------------------- (b) narrator page: built-in Ryan delivery saves version 2
    open_narrator(pg, "Ryan", "Tools")
    pg.click("#nr-panel button:has-text('Delivery…')")
    pg.wait_for_selector("dialog[open]")
    dlg = pg.inner_text("dialog[open]")
    check("(b) dialog explains built-in, fixed name/speaker, version 2 and that books keep the old version",
          "built-in speaker" in dlg and "fixed" in dlg and "version 2" in dlg and "keep that version" in dlg, dlg[:300])
    pg.fill("dialog[open] textarea[name=instruct]", "Low, tense, whispered thriller narration.")
    pg.click("dialog[open] button:has-text('Save as version 2')")
    pg.wait_for_selector(".toast:has-text('Saved as version 2')", timeout=10000)
    until(lambda: voices()["Ryan"]["version"]["version"] == 2, 10, "Ryan v2 via API")
    pg.wait_for_selector("#nr-head .callout:has-text('version 2')", timeout=10000)
    check("(b) the narrator page carries a 'saved as version 2' callout", True)
    check("(b) a new version needs a new sample", voices()["Ryan"]["sample"]["status"] == "none")
    pg.click("a.tab:has-text('Overview')")
    pg.wait_for_selector("#vh-h", timeout=10000)
    check("(b) the overview lists both versions", pg.locator("#nr-panel tbody tr").count() == 2)
    pg.screenshot(path=str(OUT / "b-ryan-overview.png"), full_page=True)

    # ---------------------------------------------------------------- (c) duplicate & retune a designed voice
    open_narrator(pg, "Gruff captain", "Tools")
    pg.click("#nr-panel button:has-text('Duplicate & retune…')")
    pg.wait_for_selector("dialog[open]")
    check("(c) dialog prefills the description of the current version", pg.input_value("dialog[open] textarea[name=instruct]") == DESC)
    pg.fill("dialog[open] input[name=name]", "Bright sailor")
    pg.fill("dialog[open] textarea[name=instruct]", NEW_DESC)
    pg.click("dialog[open] button:has-text('Create copy')")
    pg.wait_for_selector("h1:text-is('Bright sailor')", timeout=10000)
    check("(c) the copy's page opens on its Preview tab", pg.locator("a.tab.active:has-text('Preview')").count() == 1)
    check("(c) the copy shows its lineage", "Copy of Gruff captain" in pg.inner_text("#nr-head"))
    check("(c) the preview text is pre-filled with the default sample", "Chapter One" in pg.locator("#nr-preview-form textarea[name=text]").input_value())
    pg.click("#nr-preview-form button[type=submit]")
    until(lambda: pg.locator("#nr-pv audio").count() >= 1, 30, "preview audio on the copy")
    check("(c) a preview of the copy renders, with Promote", pg.locator("#nr-pv button:has-text('Promote to stable narrator')").count() == 1)

    # ---------------------------------------------------------------- (d) a clone's emotion references: one take at a time, discard, adopt
    open_narrator(pg, "Gruff captain (stable)", "Emotion references")
    pg.wait_for_selector("#nr-emo .emo-row", timeout=15000)
    check("(d) all 13 emotions are listed, anger and fear separately", pg.locator("#nr-emo .emo-row").count() == 13
          and pg.locator("#emo-anger").count() == 1 and pg.locator("#emo-fear").count() == 1)
    opts = pg.locator("#take-source option").all_inner_texts()
    check("(d) takes can come from the narrator itself, intensify, directed or the designed voice", len(opts) == 4 and opts[0] == "Read by the narrator", opts)
    pg.click("#emo-anger button:has-text('Generate a take')")
    pg.wait_for_selector("#emo-anger .take", timeout=30000)
    check("(d) one take appeared (not three)", pg.locator("#emo-anger .take").count() == 1)
    check("(d) the take shows Whisper and similarity", "Whisper WER" in pg.inner_text("#emo-anger .take") and "similarity" in pg.inner_text("#emo-anger .take"))
    pg.screenshot(path=str(OUT / "d-emotion-take.png"), full_page=True)
    pg.click("#emo-anger .take button:has-text('Discard')")
    until(lambda: pg.locator("#emo-anger .take").count() == 0, 10, "take discarded")
    check("(d) Discard removes the take", True)
    pg.click("#emo-anger button:has-text('Generate a take')")
    pg.wait_for_selector("#emo-anger .take", timeout=30000)
    pg.click("#emo-anger .take button:has-text('Adopt')")
    pg.wait_for_selector("#emo-anger button:has-text('Remove clip')", timeout=10000)
    check("(d) Adopt makes it the anger clip", "Read by the narrator" in pg.inner_text("#emo-anger .emo-head"))
    pg.click("#emo-fear button:has-text('Generate a take')")
    pg.wait_for_selector("#emo-fear .take", timeout=30000)
    pg.click("#emo-fear button:has-text('Discard all takes')")
    pg.wait_for_selector("dialog[open]")
    pg.click("dialog[open] button:has-text('Discard all')")
    until(lambda: pg.locator("#emo-fear .take").count() == 0, 10, "all fear takes discarded")
    check("(d) Discard all removes every take of that emotion", True)
    pg.screenshot(path=str(OUT / "d-emotion-adopted.png"), full_page=True)
    pg.click("a.tab:has-text('Tools')")
    pg.wait_for_selector("#nr-panel button:has-text('Retune from')", timeout=10000)
    pg.click("#nr-panel button:has-text('Retune from')")
    pg.wait_for_selector("dialog[open] textarea[name=instruct]")
    check("(d) retune dialog prefills the source description", pg.input_value("dialog[open] textarea[name=instruct]") == DESC)
    pg.fill("dialog[open] input[name=name]", "Gruff captain (retune 2)")
    pg.click("dialog[open] button:has-text('Create designed voice')")
    pg.wait_for_selector("h1:text-is('Gruff captain (retune 2)')", timeout=10000)
    check("(d) the retuned voice's page opens with its lineage", "Retuned from Gruff captain" in pg.inner_text("#nr-head"))

    # ---------------------------------------------------------------- (e) typed preview text survives leaving the page
    open_narrator(pg, "Ryan", "Preview")
    pg.fill("#nr-preview-form textarea[name=text]", TYPED)
    pg.goto(B + "/#/queue")
    pg.wait_for_selector("h1")
    open_narrator(pg, "Ryan", "Preview")
    check("(e) typed preview text survives leaving and re-entering", pg.locator("#nr-preview-form textarea[name=text]").input_value() == TYPED)
    check("(e) the sidebar keeps Narrator Studio highlighted on a narrator page", pg.locator("[data-nav=voices].active").count() == 1)
    pg.set_viewport_size({"width": 390, "height": 900})
    pg.goto(B + "/#/voices")
    pg.wait_for_selector(".voice-grid .narrator-tile", timeout=15000)
    check("(e) phone width: no horizontal scroll", pg.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"))
    pg.screenshot(path=str(OUT / "e-phone.png"), full_page=True)
    br.close()

stop.set()
bad = [e for e in errors if "favicon" not in e]
check("(f) no console errors or page errors", not bad, "; ".join(bad[:4]))
print("\nFAILED:" if failures else "\nALL CHECKS PASSED", failures if failures else "")
sys.exit(1 if failures else 0)
