"""Drive the v0.7.0 UI in a real browser (Edge) against a local MOCK-engine studio. No Mac, no GPU, no network.

    python tools/e2e/versions_ui.py OUT_DIR

Proves, in a real browser:
  * the import form starts on "Choose a narrator…", refuses to import without one, and keeps what was typed/chosen when the page redraws
  * "Narrate with another voice" creates a second version; the Book page and the library show the narrator dropdown; names read "Title - Narrator"
  * the review segment list keeps its scroll position and its "Load more" pages while a job is rendering and the page refreshes itself
  * deleting one narrator version, and removing the whole book, both need the typed name and leave the other books alone
Not covered: the operator (non-admin) view hiding the delete card (the server's 403 is covered by unit tests), real Safari, the real engine.
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
from conftest import make_text  # noqa: E402

from studio.context import Context  # noqa: E402
from studio.engines import MockASR, MockEngine  # noqa: E402
from studio.web.app import create_app  # noqa: E402
from studio.worker import Worker  # noqa: E402


class SlowEngine(MockEngine):
    """Stretches rendering over many seconds so the review page refreshes itself several times while we scroll."""

    def synthesize(self, *a, **k):
        time.sleep(0.45)
        return super().synthesize(*a, **k)


ctx = Context(home / "state")
app = create_app(ctx, start_worker=False)
PORT = 7894
app.state.port = PORT
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="on"))
threading.Thread(target=server.run, daemon=True).start()
time.sleep(1.5)
B = f"http://127.0.0.1:{PORT}"
H = {"X-Studio-Request": "1"}
api = httpx.Client(base_url=B, headers=H, timeout=30)

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def until(fn, timeout: float = 15.0, what: str = "condition") -> None:
    """Poll from Python: the app's CSP forbids unsafe-eval, which is what Playwright's string-based wait_for_function needs."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


def voice_id(name: str) -> int:
    return next(v["id"] for v in api.get("/api/voices").json()["voices"] if v["name"] == name)


def books() -> list[dict]:
    return api.get("/api/books").json()["books"]


from playwright.sync_api import sync_playwright  # noqa: E402

errors: list[str] = []
fx = home / "fx"
fx.mkdir()
(fx / "alpha.txt").write_text(make_text(2, title="Alpha Tale"), encoding="utf-8")
(fx / "beta.txt").write_text(make_text(2, title="Beta Tale", paras=4), encoding="utf-8")

with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True)
    pg = br.new_page(viewport={"width": 1280, "height": 1000})
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

    # ------------------------------------------------------------------ import form
    pg.goto(B + "/#/library")
    pg.wait_for_selector("#file-input", state="attached", timeout=15000)
    pg.set_input_files("#file-input", str(fx / "alpha.txt"))
    pg.wait_for_selector("#confirm-form", timeout=15000)
    check("import: narrator starts on the placeholder (not the first voice)", pg.input_value("select[name=voice_id]") == "")
    pg.fill("input[name=title]", "My Typed Title")
    pg.click("button[data-queue='0']")
    pg.wait_for_timeout(800)
    check("import: refused without a narrator", len(books()) == 0 and pg.locator("#confirm-form").count() == 1)
    ryan = voice_id("Ryan")
    pg.select_option("select[name=voice_id]", value=str(ryan))
    pg.set_input_files("#file-input", str(fx / "beta.txt"))           # a second upload redraws the whole page and selects the new preview
    until(lambda: pg.input_value("input[name=title]").startswith("Beta"), 15, "Beta preview")
    pg.click("button[data-act=select]:has-text('alpha.txt')")          # back to the first preview
    until(lambda: pg.input_value("input[name=title]") == "My Typed Title", 5, "restored title")
    check("import: typed title survives the redraw", pg.input_value("input[name=title]") == "My Typed Title")
    check("import: chosen narrator survives the redraw", pg.input_value("select[name=voice_id]") == str(ryan), pg.input_value("select[name=voice_id]"))
    pg.screenshot(path=str(OUT / "1-import-form.png"), full_page=True)
    pg.click("button[data-queue='0']")
    until(lambda: pg.locator("#lib-wrap tbody tr").count() == 1, 15, "library row")
    a = next(b for b in books() if b["title"] == "My Typed Title")
    check("import: book created with the chosen narrator and typed title", a["title"] == "My Typed Title" and a["narrator"] == "Ryan" and a["display_title"] == "My Typed Title - Ryan")
    pg.click("[data-act=discard]")                                      # drop the Beta preview

    # a second, long book renders in the background from here on: it makes the pages refresh themselves and keeps a job active
    big = api.post("/api/imports", files={"file": ("long.txt", make_text(2, paras=70, title="Long Tale").encode(), "text/plain")}).json()
    lb = api.post(f"/api/imports/{big['token']}/confirm", json={"title": "Long Tale", "voice_id": ryan, "queue": True}).json()
    worker = Worker(ctx, tts=SlowEngine({}), asr=MockASR())
    assert worker.acquire_lease()
    threading.Thread(target=worker.run_until_idle, daemon=True).start()

    # ------------------------------------------------------------------ narrate with another voice
    pg.goto(B + f"/#/book/{a['id']}")
    pg.wait_for_selector("#ver-form", timeout=15000)
    check("book page: title carries the narrator", pg.locator("h1").inner_text() == "My Typed Title - Ryan", pg.locator("h1").inner_text())
    check("book page: no version switcher with a single narration", pg.locator("#ver-pick").count() == 0)
    check("book page: new-version voice starts on the placeholder", pg.input_value("#ver-form select[name=voice_id]") == "")
    pg.click("#ver-form button[data-queue='0']")
    pg.wait_for_timeout(600)
    check("book page: no version created without a voice", len(books()) == 2)
    pg.select_option("#ver-form select[name=voice_id]", value=str(voice_id("Aiden")))
    pg.wait_for_timeout(3500)                                           # the page refreshes itself; the choice must not be lost
    check("book page: chosen voice survives live refreshes", pg.input_value("#ver-form select[name=voice_id]") == str(voice_id("Aiden")))
    pg.click("#ver-form button[data-queue='0']")
    until(lambda: pg.locator("h1").inner_text() == "My Typed Title - Aiden", 15, "Aiden version page")
    pg.wait_for_selector("#ver-pick", timeout=5000)
    check("book page: the previous page's chosen voice does not leak into the new version's page", pg.input_value("#ver-form select[name=voice_id]") == "")
    opts = pg.locator("#ver-pick option").all_inner_texts()
    check("book page: switcher lists both narrators", len(opts) == 2 and any(o.startswith("Ryan") for o in opts) and any(o.startswith("Aiden") for o in opts), str(opts))
    ver = next(b for b in books() if b["narrator"] == "Aiden")
    check("API: both versions share a work", ver["work_uid"] == a["work_uid"] and ver["version_count"] == 2)
    pg.screenshot(path=str(OUT / "2-version-page.png"), full_page=True)
    pg.select_option("#ver-pick", label=next(o for o in opts if o.startswith("Ryan")))
    until(lambda: pg.locator("h1").inner_text() == "My Typed Title - Ryan", 5, "Ryan version page")
    check("book page: switcher navigates to the other narration", pg.url.endswith(f"/book/{a['id']}"), pg.url)

    # ------------------------------------------------------------------ library grouping
    pg.goto(B + "/#/library")
    pg.wait_for_selector("#lib-wrap tbody tr", timeout=15000)
    check("library: one row for a book with two narrators (plus the other book)", pg.locator("#lib-wrap tbody tr").count() == 2)
    sel = pg.locator("#lib-wrap select[data-work]")
    check("library: narrator dropdown with both narrators", sel.count() == 1 and sel.locator("option").count() == 2)
    sel.select_option(label="Aiden")
    until(lambda: pg.locator("#lib-wrap tr:has(select[data-work]) a").first.get_attribute("href") == f"#/book/{ver['id']}", 5, "row switched to Aiden")
    check("library: picking a narrator switches the row to that version", True)
    pg.screenshot(path=str(OUT / "3-library-grouped.png"), full_page=True)
    pg.goto(B + "/#/queue")
    pg.wait_for_timeout(500)

    # ------------------------------------------------------------------ review list while a job renders
    pg.goto(B + f"/#/review/{lb['id']}")
    pg.wait_for_selector("#seg-list li", timeout=15000)
    check("review: header carries the narrator", "Long Tale - Ryan" in pg.locator("h1").inner_text(), pg.locator("h1").inner_text())
    if pg.locator("[data-act=more]").count():
        pg.click("[data-act=more]")
        until(lambda: pg.locator("#seg-list li").count() > 60, 10, "more segments")
    n0 = pg.locator("#seg-list li").count()
    pg.evaluate("document.querySelector('#seg-list').scrollTop = 700")
    pg.wait_for_timeout(300)
    top0 = pg.evaluate("document.querySelector('#seg-list').scrollTop")
    made0 = sum(1 for s in api.get(f"/api/books/{lb['id']}/segments?limit=200").json()["items"] if s["status"] != "pending")
    pg.wait_for_timeout(9000)                                           # the page refreshes on every progress event while the worker renders
    made1 = sum(1 for s in api.get(f"/api/books/{lb['id']}/segments?limit=200").json()["items"] if s["status"] != "pending")
    top1 = pg.evaluate("document.querySelector('#seg-list').scrollTop")
    n1 = pg.locator("#seg-list li").count()
    check("review: the job really progressed during the test", made1 > made0, f"{made0} -> {made1}")
    dims = pg.evaluate("(() => { const e = document.querySelector('#seg-list'); const c = getComputedStyle(e); return [e.scrollHeight, e.clientHeight, c.overflowY, c.maxHeight, e.parentElement.className, getComputedStyle(e.parentElement).maxHeight]; })()")
    check("review: scroll position kept across live refreshes", top0 > 100 and abs(top1 - top0) < 5, f"{top0} -> {top1}; list dims {dims}")
    check("review: segments loaded with 'Load more' kept", n1 >= n0 > 60, f"{n0} -> {n1}")
    pg.screenshot(path=str(OUT / "4-review-scrolled.png"))

    # ------------------------------------------------------------------ delete
    pg.goto(B + f"/#/book/{ver['id']}")
    pg.wait_for_selector("[data-act=del-version]", timeout=15000)
    check("delete card offers both actions with two narrations",
          pg.locator("[data-act=del-version]").count() == 1 and pg.locator("[data-act=del-book]").count() == 1)
    pg.click("[data-act=del-version]")
    pg.wait_for_selector("dialog[open] input[name=typed]", timeout=5000)
    pg.screenshot(path=str(OUT / "5-delete-dialog.png"))
    pg.fill("dialog[open] input[name=typed]", "wrong name")
    pg.click("dialog[open] button:has-text('Delete forever')")
    pg.wait_for_timeout(800)
    check("delete version: a wrong typed name deletes nothing", len(books()) == 3, str(len(books())))
    pg.click("[data-act=del-version]")
    pg.wait_for_selector("dialog[open] input[name=typed]", timeout=5000)
    pg.fill("dialog[open] input[name=typed]", "My Typed Title - Aiden")
    pg.click("dialog[open] button:has-text('Delete forever')")
    until(lambda: pg.evaluate("location.hash") == f"#/book/{a['id']}", 10, "back on the Ryan book")
    left = books()
    check("delete version: only the Aiden narration is gone", len(left) == 2 and all(b["id"] != ver["id"] for b in left) and any(b["id"] == a["id"] for b in left))
    pg.wait_for_selector("[data-act=del-book]", timeout=10000)
    check("delete card: single narration offers only 'Remove this book'", pg.locator("[data-act=del-version]").count() == 0)
    pg.click("[data-act=del-book]")
    pg.wait_for_selector("dialog[open] input[name=typed]", timeout=5000)
    pg.fill("dialog[open] input[name=typed]", "My Typed Title")
    pg.click("dialog[open] button:has-text('Delete forever')")
    until(lambda: pg.evaluate("location.hash") == "#/library", 10, "library")
    left = books()
    check("remove book: that book is gone, the unrelated one is untouched", [b["title"] for b in left] == ["Long Tale"], str([b["title"] for b in left]))
    # a book with an active job cannot be removed
    pg.goto(B + f"/#/book/{lb['id']}")
    pg.wait_for_selector("[data-act=del-book]", timeout=10000)
    pg.click("[data-act=del-book]")
    pg.wait_for_selector(".toast", timeout=5000)
    check("remove book: refused while its job is active", len(books()) == 1 and pg.locator("dialog[open]").count() == 0)
    br.close()

bad = [e for e in errors if "favicon" not in e and "status of 409" not in e]   # 409s are the deliberate confirm-preview / busy refusals
check("no console errors or page errors", not bad, "; ".join(bad[:4]))
print("\nFAILED:" if failures else "\nALL CHECKS PASSED", failures if failures else "")
sys.exit(1 if failures else 0)
