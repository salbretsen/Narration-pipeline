"""Drive the v0.8 router / live-update foundation in a real browser (Edge) against a local MOCK-engine studio. No Mac, no GPU, no network.

    python tools/e2e/foundation_ui.py OUT_DIR

Proves, in a real browser:
  * (a) Library -> click a book while the book API is slow: the "Loading..." status (.view-loading) appears within 400 ms and the old page
        is dimmed (#view.view-stale) instead of staying interactive; both are gone once the book paints
  * (b) a rapid double navigation (book A, slow, then book B, fast) ends on B and A never paints afterwards
  * (c) with an idle (mock) worker heartbeating, the Overview page refetches /api/overview at most twice in 8 s (the old once-a-second
        SSE-driven repaint loop is gone)
Not covered: real Safari, the real engine, a real Mac.
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

from studio import services as S  # noqa: E402
from studio.context import Context  # noqa: E402
from studio.engines import MockASR, MockEngine  # noqa: E402
from studio.web.app import create_app  # noqa: E402
from studio.worker import Worker  # noqa: E402

ctx = Context(home / "state")
app = create_app(ctx, start_worker=False)
PORT = 7895
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


# ---- fixtures: two books, an idle worker --------------------------------------------------------------------------------
ryan = next(v["id"] for v in api.get("/api/voices").json()["voices"] if v["name"] == "Ryan")


def make_book(title: str) -> int:
    prev = api.post("/api/imports", files={"file": (f"{title}.txt", make_text(2, title=title).encode(), "text/plain")}).json()
    return api.post(f"/api/imports/{prev['token']}/confirm", json={"title": title, "voice_id": ryan, "queue": False}).json()["id"]


A_ID, B_ID = make_book("Alpha Tale"), make_book("Beta Tale")
worker = Worker(ctx, tts=MockEngine({}), asr=MockASR())
threading.Thread(target=worker.run_forever, kwargs={"idle_sleep": 2.0}, daemon=True).start()
until(lambda: api.get("/api/worker").json()["online"], 10, "idle worker online")

# slow the book API on demand (the app runs in this process, so patching the service function is enough)
DELAY: dict[int, float] = {}
_real_get_book = S.get_book


def slow_get_book(c, book_id):
    time.sleep(DELAY.get(book_id, 0.0))
    return _real_get_book(c, book_id)


S.get_book = slow_get_book

from playwright.sync_api import sync_playwright  # noqa: E402

errors: list[str] = []
with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True)
    pg = br.new_page(viewport={"width": 1280, "height": 900})
    pg.add_init_script("window.__states = 0; window.addEventListener('studio:state', () => { window.__states++; });")  # live pushes seen by the page
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

    # ------------------------------------------------------------------ (a) loading state + dimmed old page
    DELAY.update({A_ID: 1.5, B_ID: 1.5})
    pg.goto(B + "/#/library")
    pg.wait_for_selector(f"a[href='#/book/{A_ID}']", timeout=15000)
    check("(a) library rendered before navigating", pg.locator("#view.view-stale").count() == 0 and pg.locator(".view-loading").count() == 0)
    t0 = time.time()
    pg.click(f"a[href='#/book/{A_ID}']")
    stale_now = pg.locator("#view.view-stale").count() == 1
    check("(a) old page is marked view-stale immediately and aria-busy", stale_now and pg.get_attribute("#view", "aria-busy") == "true")
    pg.wait_for_selector(".view-loading", timeout=400)
    seen_ms = (time.time() - t0) * 1000
    check("(a) .view-loading shows within 400 ms of the click", seen_ms < 400, f"{seen_ms:.0f} ms")
    check("(a) loading element is an accessible status with a spinner", pg.get_attribute(".view-loading", "role") == "status" and pg.locator(".view-loading .spinner").count() == 1)
    old_opacity = pg.evaluate("getComputedStyle(document.querySelector('#view > .view-page:not(.view-pending)')).opacity")
    pe = pg.evaluate("getComputedStyle(document.querySelector('#view > .view-page:not(.view-pending)')).pointerEvents")
    # opacity animates (.12 s) after a .1 s delay; give it a moment
    until(lambda: float(pg.evaluate("getComputedStyle(document.querySelector('#view > .view-page:not(.view-pending)')).opacity")) <= 0.55, 2, "dimmed")
    check("(a) old page is dimmed and not clickable while waiting", pe == "none", f"opacity was {old_opacity}, pointer-events {pe}")
    check("(a) old page content still on screen while waiting (library table)", pg.locator("#view table").count() >= 1 or pg.locator("#view .table").count() >= 1 or pg.locator("#view a[href^='#/book/']").count() >= 1)
    pg.screenshot(path=str(OUT / "1-loading.png"))
    until(lambda: "Alpha Tale" in pg.locator("#view h1").first.inner_text(), 6, "book A painted")
    check("(a) loading status and stale class are gone once the book paints",
          pg.locator(".view-loading").count() == 0 and pg.locator("#view.view-stale").count() == 0 and pg.get_attribute("#view", "aria-busy") is None)
    check("(a) exactly one page host inside #view", pg.locator("#view > .view-page").count() == 1 and pg.locator("#view > .view-pending").count() == 0)

    # ------------------------------------------------------------------ (b) rapid double navigation
    DELAY.update({A_ID: 1.5, B_ID: 0.1})
    pg.evaluate("location.hash = '#/library'")
    pg.wait_for_selector(f"a[href='#/book/{A_ID}']", timeout=15000)
    pg.evaluate(f"location.hash = '#/book/{A_ID}'")
    time.sleep(0.05)
    pg.evaluate(f"location.hash = '#/book/{B_ID}'")
    until(lambda: "Beta Tale" in pg.locator("#view h1").first.inner_text(), 5, "book B painted")
    painted_a = False
    end = time.time() + 2.6   # A's slow request returns at about 1.5 s: watch well past that
    while time.time() < end:
        h1 = pg.locator("#view h1")
        if h1.count() and "Alpha Tale" in h1.first.inner_text():
            painted_a = True
        time.sleep(0.05)
    check("(b) ends on book B", "Beta Tale" in pg.locator("#view h1").first.inner_text() and pg.url.endswith(f"/book/{B_ID}"))
    check("(b) book A never paints over book B", not painted_a)
    check("(b) one page host, no loading leftovers", pg.locator("#view > .view-page").count() == 1 and pg.locator(".view-loading").count() == 0 and pg.locator("#view.view-stale").count() == 0)
    check("(b) document title follows the page", pg.title().startswith("Beta Tale"), pg.title())

    # ------------------------------------------------------------------ (c) no refetch loop while idle
    DELAY.clear()
    pg.goto(B + "/#/overview")
    pg.wait_for_selector("#view h1", timeout=15000)
    time.sleep(1.5)  # let the first SSE event and any first refresh settle
    hits: list[float] = []
    pg.on("request", lambda r: hits.append(time.time()) if "/api/overview" in r.url else None)
    states0 = pg.evaluate("window.__states")
    time.sleep(8.0)
    pushes = pg.evaluate("window.__states") - states0
    check("(c) worker is online and idle during the measurement", api.get("/api/worker").json()["online"] is True)
    check("(c) <= 2 /api/overview requests in 8 s on an idle Overview page", len(hits) <= 2, f"{len(hits)} requests")
    check("(c) the server pushed <= 1 state event in 8 s while nothing changed (was about one every 1-2 s)", pushes <= 1, f"{pushes} events")
    print(f"INFO  in 8 s idle: {len(hits)} /api/overview requests, {pushes} SSE state pushes")
    br.close()

worker.stop_event.set()
bad = [e for e in errors if "favicon" not in e]
check("no console errors or page errors", not bad, "; ".join(bad[:4]))
print("\nFAILED:" if failures else "\nALL CHECKS PASSED", failures if failures else "")
sys.exit(1 if failures else 0)
