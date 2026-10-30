"""Drive the Narrator Studio polish (P2-G) in a real browser (Edge) against a local MOCK-engine studio. No Mac, no GPU, no network.

    python tools/e2e/polish_voices.py OUT_DIR

Proves: (a) the built-in Ryan "Delivery..." dialog explains what can change and saving creates version 2 with a toast; (b) "Duplicate & retune..."
on a designed voice creates a copy with the edited description, the new card shows its lineage and a pre-filled preview form; (c) a promoted clone
shows "Retune from ...", which creates a designed copy plus the next-step callout; (d) typed preview text survives a trip to the Record tab and
back; (e) no console errors. Not covered: real audio quality, the real engine, real Safari.
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


def card(pg, name):
    return pg.locator("article.voice", has=pg.locator(f"h3:text-is('{name}')"))


with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True)
    pg = br.new_page(viewport={"width": 1280, "height": 1000})
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.goto(B + "/#/voices")
    pg.wait_for_selector(".voice-grid article.voice", timeout=15000)

    # ---------------------------------------------------------------- (a) built-in Ryan: honest delivery dialog, saving makes v2
    ryan = card(pg, "Ryan")
    check("(a) Ryan card shows 'Delivery...' (not Edit) and 'Duplicate & retune...'",
          ryan.locator("button:has-text('Delivery…')").count() == 1 and ryan.locator("button:has-text('Duplicate & retune…')").count() == 1)
    ryan.locator("button:has-text('Delivery…')").click()
    pg.wait_for_selector("dialog[open]")
    dlg = pg.inner_text("dialog[open]")
    check("(a) dialog explains built-in, fixed name/speaker, version 2 and that books keep the old version",
          "built-in speaker" in dlg and "fixed" in dlg and "version 2" in dlg and "keep that version" in dlg and "Duplicate & retune" in dlg, dlg[:400])
    check("(a) built-in dialog has no name field", pg.locator("dialog[open] input[name=name]").count() == 0)
    pg.screenshot(path=str(OUT / "a-ryan-dialog.png"))
    pg.fill("dialog[open] textarea[name=instruct]", "Low, tense, whispered thriller narration.")
    pg.click("dialog[open] button:has-text('Save as version 2')")
    pg.wait_for_selector(".toast:has-text('Saved as version 2')", timeout=10000)
    check("(a) toast says Saved as version 2", True)
    until(lambda: voices()["Ryan"]["version"]["version"] == 2, 10, "Ryan v2 via API")
    check("(a) API: Ryan is at version 2 with the new instruction", voices()["Ryan"]["version"]["instruct"] == "Low, tense, whispered thriller narration.")
    pg.wait_for_selector("article.voice:has(h3:text-is('Ryan')) .voice-note", timeout=5000)
    check("(a) the Ryan card carries a 'saved as version 2' callout with 'Preview with new delivery'",
          "version 2" in card(pg, "Ryan").locator(".voice-note").inner_text() and card(pg, "Ryan").locator("button:has-text('Preview with new delivery')").count() == 1)
    pg.screenshot(path=str(OUT / "a-ryan-saved.png"), full_page=True)

    # ---------------------------------------------------------------- (b) duplicate & retune a designed voice
    gruff = card(pg, "Gruff captain")
    gruff.locator("button:has-text('Duplicate & retune…')").click()
    pg.wait_for_selector("dialog[open]")
    check("(b) dialog prefills the description of the current version", pg.input_value("dialog[open] textarea[name=instruct]") == DESC)
    pg.fill("dialog[open] input[name=name]", "Bright sailor")
    pg.fill("dialog[open] textarea[name=instruct]", NEW_DESC)
    pg.screenshot(path=str(OUT / "b-duplicate-dialog.png"))
    pg.click("dialog[open] button:has-text('Create copy')")
    pg.wait_for_selector("article.voice:has(h3:text-is('Bright sailor'))", timeout=10000)
    new = card(pg, "Bright sailor")
    check("(b) the copy card shows its lineage 'Copy of Gruff captain'", "Copy of Gruff captain" in new.inner_text())
    check("(b) the copy's top line and description use the edited text", NEW_DESC in new.locator("p.small").first.inner_text())
    check("(b) a next-step callout invites a preview", "Next: generate a preview" in new.locator(".voice-note").inner_text())
    check("(b) the copy's preview text is pre-filled with the default sample", "Chapter One" in new.locator("textarea[name=text]").input_value())
    check("(b) the new card is scrolled into view / highlighted", new.evaluate("el => { const r = el.getBoundingClientRect(); return r.top < innerHeight && r.bottom > 0; }"))
    v = voices()
    check("(b) API: copy is design/non-builtin with derived_from, original untouched",
          v["Bright sailor"]["derived_from"]["voice_id"] == design["id"] and v["Bright sailor"]["version"]["instruct"] == NEW_DESC and not v["Bright sailor"]["builtin"]
          and v["Gruff captain"]["version"]["instruct"] == DESC and v["Gruff captain"]["version"]["version"] == 1)
    pg.screenshot(path=str(OUT / "b-copy-card.png"), full_page=True)
    # the copy can be previewed straight away (mock worker renders it)
    new.locator("button:has-text('Generate preview')").click()
    until(lambda: card(pg, "Bright sailor").locator("audio").count() >= 1, 30, "preview audio on the copy")
    check("(b) a preview of the copy renders", True)

    # ---------------------------------------------------------------- (c) retune from a promoted clone
    stable = card(pg, "Gruff captain (stable)")
    check("(c) the clone card shows 'Retune from ‘Gruff captain’…'", stable.locator("button:has-text('Retune from ‘Gruff captain’…')").count() == 1)
    check("(c) the clone card shows its source lineage", "Promoted from the designed voice Gruff captain" in stable.inner_text())
    stable.locator("button:has-text('Retune from')").click()
    pg.wait_for_selector("dialog[open] textarea[name=instruct]")
    check("(c) retune dialog prefills the source description and a unique name",
          pg.input_value("dialog[open] textarea[name=instruct]") == DESC and pg.input_value("dialog[open] input[name=name]") == "Gruff captain (retune)")
    pg.fill("dialog[open] input[name=name]", "Gruff captain (retune 2)")
    pg.fill("dialog[open] textarea[name=instruct]", "A gruff older male voice, slower still, with a weary dry edge.")
    pg.click("dialog[open] button:has-text('Create designed voice')")
    pg.wait_for_selector("article.voice:has(h3:text-is('Gruff captain (retune 2)'))", timeout=10000)
    rt = card(pg, "Gruff captain (retune 2)")
    check("(c) the retuned card shows 'Retuned from Gruff captain'", "Retuned from Gruff captain" in rt.inner_text())
    note = rt.locator(".voice-note").inner_text()
    check("(c) callout: generate a preview, pick one, promote", "Next: generate a preview, pick one you like, then Promote to stable narrator" in note, note)
    v = voices()
    check("(c) API: design voice with retune lineage; clone unchanged",
          v["Gruff captain (retune 2)"]["kind"] == "design" and v["Gruff captain (retune 2)"]["derived_from"]["relation"] == "retune"
          and v["Gruff captain (stable)"]["version"]["version"] == 1)
    pg.screenshot(path=str(OUT / "c-retune-card.png"), full_page=True)

    # ---------------------------------------------------------------- (d) typed preview text survives a tab switch and a reload
    ryan = card(pg, "Ryan")
    ryan.locator("textarea[name=text]").fill(TYPED)
    ryan.locator("input[name=instruct]").fill("slowly")
    ryan.locator("input[name=verify]").check()
    pg.click("#tab-record")
    pg.wait_for_selector("#lab-host", timeout=5000)
    pg.click("#tab-preset")
    pg.wait_for_selector("#f-preset", timeout=5000)
    ryan = card(pg, "Ryan")
    check("(d) preview text survives switching to the Record tab and back", ryan.locator("textarea[name=text]").input_value() == TYPED)
    check("(d) instruct and verify checkbox survive too", ryan.locator("input[name=instruct]").input_value() == "slowly" and ryan.locator("input[name=verify]").is_checked())
    pg.goto(B + "/#/queue")
    pg.wait_for_selector("h1")
    pg.goto(B + "/#/voices")
    pg.wait_for_selector(".voice-grid article.voice", timeout=15000)
    check("(d) ...and leaving and re-entering the page", card(pg, "Ryan").locator("textarea[name=text]").input_value() == TYPED)
    br.close()

stop.set()
bad = [e for e in errors if "favicon" not in e]
check("(e) no console errors or page errors", not bad, "; ".join(bad[:4]))
print("\nFAILED:" if failures else "\nALL CHECKS PASSED", failures if failures else "")
sys.exit(1 if failures else 0)
