"""Drive the P2-B Overview and Library pages in a real browser (Edge) against a local MOCK-engine studio. No Mac, no GPU, no network.

    python tools/e2e/polish_overview.py OUT_DIR

Proves, in a real browser, while a slow mock worker keeps rendering a book and a thread keeps adding activity events (so the live
views refresh over and over):
  * (a) Overview: the activity feed keeps its scroll position (scrollTop 150) for 8 s of live updates, a "N new events" hint appears
        instead of yanking the feed back, and the hint clears when the feed is scrolled back to the top
  * (a2) Overview: "Needs attention" is capped at 5 rows with "Show all (N)", the expanded state survives updates, and a book with
        several failed jobs is listed once
  * (b) Library: a narrator <select> stays selected and focused through 6 s of updates; picking a value switches the row's runtime line
  * (c) with a TEST-ONLY runtime shim injected into services._book_summary, running / queued / finished / idle render the right text on
        Overview and on Library (labels "audio" vs processing)
  * (d) no console or page errors
The `runtime` field is produced by the Queue agent's server code; here it is a shim in this process, so (c) proves the RENDERING only.
Not covered: real Safari, the real engine, a real Mac, the real server-side estimate.
"""
from __future__ import annotations

import os
import re
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

home = Path(tempfile.mkdtemp(prefix="studio-polish-"))
os.environ.update({"AUDIOBOOK_STUDIO_HOME": str(home / "state"), "STUDIO_ENGINE": "mock", "STUDIO_NO_WORKER": "1", "STUDIO_NO_UPDATE_CHECK": "1"})

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from conftest import make_text  # noqa: E402

from studio import services as S  # noqa: E402
from studio.context import Context  # noqa: E402
from studio.engines import MockASR, MockEngine  # noqa: E402
from studio.web.app import create_app  # noqa: E402
from studio.worker import Worker  # noqa: E402


class SlowEngine(MockEngine):
    """Stretches rendering over many seconds so the live pages keep getting new state."""

    def synthesize(self, *a, **k):
        time.sleep(0.45)
        return super().synthesize(*a, **k)


ctx = Context(home / "state")
app = create_app(ctx, start_worker=False)
PORT = 7932
app.state.port = PORT
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="on"))
threading.Thread(target=server.run, daemon=True).start()
time.sleep(1.5)
B = f"http://127.0.0.1:{PORT}"
api = httpx.Client(base_url=B, headers={"X-Studio-Request": "1"}, timeout=30)

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


def import_book(title: str, voice: str, queue: bool = False, paras: int = 3) -> int:
    prev = api.post("/api/imports", files={"file": (f"{title}.txt", make_text(2, paras=paras, title=title).encode(), "text/plain")}).json()
    return api.post(f"/api/imports/{prev['token']}/confirm", json={"title": title, "voice_id": voice_id(voice), "queue": queue}).json()["id"]


# ---- fixtures ----------------------------------------------------------------------------------------------------------
A_RYAN = import_book("Alpha Tale", "Ryan")
A_AIDEN = api.post(f"/api/books/{A_RYAN}/versions", json={"voice_id": voice_id("Aiden"), "instructions": "", "queue": False}).json()["id"]
B_ID = import_book("Beta Tale", "Ryan")
FLAGGED = import_book("Gamma Tale", "Ryan", paras=6)   # a book with many flagged segments -> a long "Needs attention" list
LONG = import_book("Long Tale", "Ryan", queue=True, paras=70)

# 9 flagged segments + 3 failed jobs on one book and 1 on another (latest-failure dedupe)
with ctx.db.tx() as conn:
    ids = [r[0] for r in conn.execute("SELECT id FROM segments WHERE book_id=? ORDER BY id LIMIT 9", (FLAGGED,)).fetchall()]
    for i in ids:
        conn.execute("UPDATE segments SET status='flagged', flag_reason='Mismatch with transcript' WHERE id=?", (i,))
    for n in range(3):
        conn.execute("INSERT INTO jobs(book_id, kind, status, error, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                     (B_ID, "book", "failed", f"failure {n}", time.time() - 100 + n, time.time() - 100 + n))
    for n in range(35):
        ctx.event(conn, "test", f"Seeded activity event number {n}: something happened to a book that takes a while to describe", level="info")

# TEST-ONLY runtime shim (the real field is produced by the Queue agent's server code)
now = time.time()
RT = {
    A_RYAN: {"state": "running", "remaining_s": 4800.0, "finish_at": now + 4800, "estimated": True, "basis": "median of 200 recent segments"},
    A_AIDEN: {"state": "finished", "processing_s": 3 * 3600 + 12 * 60, "estimated": False, "basis": "sum of job active time"},
    B_ID: {"state": "queued", "total_est_s": 2 * 3600 + 300.0, "starts_at": now + 1800, "estimated": True, "basis": "estimate after 20 segments"},
    FLAGGED: {"state": "idle", "total_est_s": 5400.0, "estimated": True, "basis": "median of 200 recent segments"},
}
_real_summary = S._book_summary


def shim(conn, b, st, *rest, **kw):
    d = _real_summary(conn, b, st, *rest, **kw)
    rt = RT.get(b["id"])
    if rt:
        d["runtime"] = dict(rt)
    return d


S._book_summary = shim

worker = Worker(ctx, tts=SlowEngine({}), asr=MockASR())
assert worker.acquire_lease()
threading.Thread(target=worker.run_until_idle, daemon=True).start()

stop_events = threading.Event()


def keep_adding_events() -> None:
    n = 0
    while not stop_events.is_set():
        time.sleep(1.2)
        n += 1
        with ctx.db.tx() as conn:
            ctx.event(conn, "test", f"Live activity event {n}", level="info")


from playwright.sync_api import sync_playwright  # noqa: E402

errors: list[str] = []
with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True)
    pg = br.new_page(viewport={"width": 1280, "height": 900})
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    hits: list[float] = []
    pg.on("request", lambda r: hits.append(time.time()) if "/api/overview" in r.url else None)

    # ------------------------------------------------------------------ (c) runtime states on Overview
    pg.goto(B + "/#/overview")
    pg.wait_for_selector("#ov-feed", timeout=15000)
    pg.wait_for_selector("#ov-recent .rt-line", timeout=15000)

    def ov_row(book_id: int) -> str:
        return pg.locator("#ov-recent tr", has=pg.locator(f"a[href='#/book/{book_id}']")).first.inner_text()

    t = ov_row(A_RYAN)
    check("(c) overview running: time left + done clock", re.search(r"about 1 h 20 m left · done about (\w{3} )?\d\d:\d\d", t) is not None, t)
    t = ov_row(B_ID)
    check("(c) overview queued: processing estimate + start clock", re.search(r"about 2 h 05 m processing · starts about (\w{3} )?\d\d:\d\d", t) is not None, t)
    t = ov_row(A_AIDEN)
    check("(c) overview finished: processed in", "processed in 3 h 12 m" in t, t)
    t = ov_row(FLAGGED)
    check("(c) overview idle: estimate if queued", "about 1 h 30 m to narrate" in t, t)
    check("(c) overview: audio length column is labelled 'Audio length'", "audio length" in pg.locator("#ov-recent thead").inner_text().lower() and "est." in ov_row(A_RYAN))
    tip = pg.locator("#ov-recent .rt-running").first.get_attribute("title")
    check("(c) overview: tooltip carries the basis", tip is not None and "median of 200 recent segments" in tip and "not audio length" in tip, str(tip))
    pg.screenshot(path=str(OUT / "1-overview.png"), full_page=True)

    # ------------------------------------------------------------------ (a2) attention cap
    rows = pg.locator("#ov-att-list > li").count()
    toggle = pg.locator("#ov-att-toggle")
    fail_rows = pg.locator("#ov-att-list li.att-bad").count()
    check("(a2) failed jobs for one book are listed once", fail_rows == 1, f"{fail_rows} failed rows")
    check("(a2) attention list capped at 5 rows with a Show all (N) toggle", rows == 5 and toggle.count() == 1 and re.fullmatch(r"Show all \(\d+\)", toggle.inner_text().strip()) is not None,
          f"{rows} rows, toggle {toggle.inner_text() if toggle.count() else None}")
    total = int(re.search(r"\((\d+)\)", toggle.inner_text()).group(1))
    check("(a2) total = 1 failed book + 9 flags", total == 10, str(total))
    toggle.click()
    check("(a2) clicking Show all expands the list", pg.locator("#ov-att-list > li").count() == total)

    # ------------------------------------------------------------------ (a) feed keeps its scroll position
    threading.Thread(target=keep_adding_events, daemon=True).start()
    pg.evaluate("document.getElementById('ov-feed').scrollTop = 150")
    pg.wait_for_timeout(300)
    st0 = pg.evaluate("document.getElementById('ov-feed').scrollTop")
    check("(a) feed is scrollable and scrolled to 150", st0 == 150, str(st0))
    n0 = len(hits)
    pg.wait_for_timeout(8000)
    st1 = pg.evaluate("document.getElementById('ov-feed').scrollTop")
    refreshes = len(hits) - n0
    check("(a) the page refreshed itself while we watched (live updates were firing)", refreshes >= 2, f"{refreshes} /api/overview requests")
    check("(a) feed scrollTop unchanged after 8 s of live updates", st1 == 150, f"{st0} -> {st1}")
    check("(a) 'N new events' hint appears instead of yanking the feed", pg.locator("#ov-feed-new").count() == 1, "hint missing")
    check("(a2) expanded attention list stayed expanded through updates", pg.locator("#ov-att-list > li").count() == total)
    pg.screenshot(path=str(OUT / "2-feed-scrolled.png"))
    pg.evaluate("document.getElementById('ov-feed').scrollTop = 0")
    until(lambda: pg.locator("#ov-feed-new").count() == 0, 5, "hint cleared at top")
    check("(a) hint clears once the feed is back at the top", True)
    pg.click("#ov-att-toggle")
    check("(a2) Show fewer collapses to 5", pg.locator("#ov-att-list > li").count() == 5)

    # ------------------------------------------------------------------ (c) runtime states on Library, (b) narrator select
    pg.goto(B + "/#/library")
    pg.wait_for_selector("#lib-wrap tbody tr", timeout=15000)
    work_sel = pg.locator("#lib-wrap select[data-work]")
    check("(b) the two-narrator book has a narrator selector", work_sel.count() == 1)

    def lib_row(book_id: int) -> str:
        return pg.locator("#lib-wrap tr", has=pg.locator(f"a[href='#/book/{book_id}']")).first.inner_text()

    sel_id = work_sel.first.get_attribute("id")
    shown_first = work_sel.first.input_value()
    check("(c) library shows the newest version first (finished: processed in)", shown_first == str(A_AIDEN) and "processed in 3 h 12 m" in lib_row(A_AIDEN), lib_row(A_AIDEN))
    t = lib_row(B_ID)
    check("(c) library queued: processing estimate + start clock", re.search(r"about 2 h 05 m processing · starts about (\w{3} )?\d\d:\d\d", t) is not None, t)
    check("(c) library idle: estimate if queued", "about 1 h 30 m to narrate" in lib_row(FLAGGED), lib_row(FLAGGED))
    check("(c) library: audio length column is labelled 'audio'", "audio" in lib_row(B_ID).lower())
    pg.focus(f"#{sel_id}")
    pg.select_option(f"#{sel_id}", value=str(A_RYAN))
    until(lambda: pg.locator(f"#{sel_id}").input_value() == str(A_RYAN) and "left" in pg.locator("#lib-wrap tr", has=pg.locator(f"#{sel_id}")).first.inner_text(), 5, "version switch repainted")
    t = pg.locator("#lib-wrap tr", has=pg.locator(f"#{sel_id}")).first.inner_text()
    check("(c) library running (selected version): time left + done clock", re.search(r"about 1 h 20 m left · done about (\w{3} )?\d\d:\d\d", t) is not None, t)
    pg.wait_for_timeout(6000)
    still = pg.locator(f"#{sel_id}").input_value()
    focused = pg.evaluate("document.activeElement && document.activeElement.id")
    check("(b) narrator selection survives 6 s of live updates", still == str(A_RYAN), still)
    check("(b) the select keeps keyboard focus (not replaced under the user)", focused == sel_id, str(focused))
    pg.screenshot(path=str(OUT / "3-library.png"), full_page=True)
    pg.click(f"a[href='#/book/{B_ID}']")
    until(lambda: "Beta Tale" in pg.locator("#view h1").first.inner_text(), 6, "book page painted after clicking a library link")
    check("(b) clicking a library book navigates", pg.url.endswith(f"/book/{B_ID}"))
    br.close()

stop_events.set()
worker.stop_event.set()
bad = [e for e in errors if "favicon" not in e]
check("(d) no console errors or page errors", not bad, "; ".join(bad[:4]))
print("\nFAILED:" if failures else "\nALL CHECKS PASSED", failures if failures else "")
sys.exit(1 if failures else 0)
