"""Drive the polished Queue page in a real browser (Edge) against a local MOCK-engine studio. No Mac, no GPU, no network.

    python tools/e2e/polish_queue.py OUT_DIR

Proves, in a real browser (headless Edge, mock engine, port 7933):
  * one "recently finished" row per book even after several segment regenerates; the "+N earlier runs" disclosure opens and lists them
  * "Recently finished" and "Assembly, export, preview & analysis jobs" are paginated 10 per page ("1-10 of N", Next / Prev work)
  * a running job shows "about ... left", a queued job "about ... processing", finished rows "processed in ..."
  * an open job log keeps its scroll position, stays open and never flashes "Loading..." while the page repaints during live updates
  * no console errors
Not covered: real Safari, the real engine, a real Mac, and estimate accuracy on real hardware (mock engine timings are not representative).
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

home = Path(tempfile.mkdtemp(prefix="studio-queue-"))
os.environ.update({"AUDIOBOOK_STUDIO_HOME": str(home / "state"), "STUDIO_ENGINE": "mock", "STUDIO_NO_WORKER": "1", "STUDIO_NO_UPDATE_CHECK": "1"})

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from conftest import make_text  # noqa: E402

from studio.context import Context  # noqa: E402
from studio.db import rows  # noqa: E402
from studio.engines import MockASR, MockEngine  # noqa: E402
from studio.web.app import create_app  # noqa: E402
from studio.worker import Worker  # noqa: E402

SLOW = [0.0]


class PacedEngine(MockEngine):
    """Mock engine whose per-segment delay can be raised so a job stays 'running' long enough to look at."""

    def synthesize(self, *a, **k):
        if SLOW[0]:
            time.sleep(SLOW[0])
        return super().synthesize(*a, **k)


ctx = Context(home / "state")
app = create_app(ctx, start_worker=False)
PORT = 7933
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


def until(fn, timeout: float = 20.0, what: str = "condition") -> None:
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


ryan = next(v["id"] for v in api.get("/api/voices").json()["voices"] if v["name"] == "Ryan")


def make_book(title: str, chapters: int = 2, queue: bool = True, paras: int = 3) -> int:
    prev = api.post("/api/imports", files={"file": (f"{title}.txt", make_text(chapters, paras=paras, title=title).encode(), "text/plain")}).json()
    return api.post(f"/api/imports/{prev['token']}/confirm", json={"title": title, "voice_id": ryan, "queue": queue}).json()["id"]


def idle() -> bool:
    return not api.get("/api/queue").json()["active"]


worker = Worker(ctx, tts=PacedEngine({}), asr=MockASR())
threading.Thread(target=worker.run_forever, kwargs={"idle_sleep": 0.25}, daemon=True).start()
until(lambda: api.get("/api/worker").json()["online"], 10, "worker online")

# ---------------------------------------------------------------------------------------------------- fixtures
ids = {t: make_book(t) for t in ("Alpha Tale", "Beta Tale", "Gamma Tale")}
until(idle, 60, "first full passes finished")
alpha = ids["Alpha Tale"]
with ctx.db.session() as conn:
    alpha_segs = [r["id"] for r in rows(conn, "SELECT id FROM segments WHERE book_id=? ORDER BY id", (alpha,))]
for sid in alpha_segs[1:4]:  # three small edits on one book: each makes another finished job
    assert api.post(f"/api/segments/{sid}/regenerate", json={}).status_code == 200
    until(idle, 30, "touch-up finished")
now = time.time()
# more finished books (seeded as completed jobs so the page needs a second page)
extra = [make_book(f"Seed {i:02d}", chapters=1, queue=False, paras=1) for i in range(14)]
with ctx.db.tx() as conn:
    for i, bid in enumerate(extra):
        t = now - 3600 * (i + 2)
        conn.execute("INSERT INTO jobs(kind, book_id, status, position, created_at, started_at, finished_at, updated_at, active_seconds) VALUES ('book',?,'completed',0,?,?,?,?,?)",
                     (bid, t - 400, t - 300, t, t, 120.0 + i))
    for i in range(13):  # utility jobs (assemble) so that card needs paging too
        t = now - 60 * i
        conn.execute("INSERT INTO jobs(kind, book_id, status, created_at, started_at, finished_at, updated_at, payload_json) VALUES ('assemble',?,'completed',?,?,?,?,'{}')",
                     (alpha, t - 5, t - 4, t, t))
    beta_job = conn.execute("SELECT MAX(id) FROM jobs WHERE book_id=? AND kind='book'", (ids["Beta Tale"],)).fetchone()[0]
    for i in range(90):  # a long log so the log box can scroll
        conn.execute("INSERT INTO job_logs(job_id, book_id, level, message, created_at) VALUES (?,?,?,?,?)", (beta_job, ids["Beta Tale"], "info", f"Synthetic log line {i:02d}", now - 100 + i))
q = api.get("/api/queue").json()
check("API: 3 finished jobs + 3 touch-ups on Alpha collapse to one finished entry per book",
      q["finished_total"] == 17 and sum(1 for j in q["finished"] if j["book_id"] == alpha) <= 1 and len(next(j for j in q["finished"] if j["book_id"] == alpha)["earlier_runs"]) == 3,
      f"finished_total={q['finished_total']}")

from playwright.sync_api import sync_playwright  # noqa: E402

errors: list[str] = []
with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True)
    pg = br.new_page(viewport={"width": 1280, "height": 1100})
    pg.add_init_script("""window.__loadingFlash = 0; window.__watch = false; window.__fetches = []; const _f = window.fetch; window.fetch = function(u, o) { window.__fetches.push(String(u).slice(0, 40)); return _f.apply(this, arguments); }; window.__states = 0; window.__stamps = []; window.addEventListener('studio:state', () => { window.__states++; window.__stamps.push(Math.round(Date.now() / 100) / 10); });
      new MutationObserver(() => { if (window.__watch && [...document.querySelectorAll('details.joblog[open] .log-box')].some((b) => b.textContent.includes('Loading'))) window.__loadingFlash++; })
        .observe(document, { childList: true, subtree: true, characterData: true });""")
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    # (Playwright's Python callbacks only run while a Playwright call is in flight, so count requests inside the page instead)
    queue_gets = lambda: pg.evaluate("window.__fetches.filter((u) => u.startsWith('/api/queue')).length")  # noqa: E731

    pg.goto(B + "/#/queue")
    pg.wait_for_selector("#qdone li.qrow", timeout=15000)

    # ---------------------------------------------------------------------------------------- one row per book + earlier runs
    rows_done = pg.locator("#qdone > li.qrow")
    check("finished card shows 10 rows on page 1", rows_done.count() == 10, str(rows_done.count()))
    check("finished pager reads '1–10 of 17'", "1–10 of 17" in pg.locator("#q-done .pager-range").inner_text(), pg.locator("#q-done .pager-range").inner_text())
    titles = [t.strip() for t in pg.locator("#qdone > li.qrow .q-title a").all_inner_texts()]
    check("every book appears at most once on the page (no repeated 'Alpha Tale')", len(titles) == len(set(titles)), str(titles))
    check("Alpha Tale (3 touch-ups) is one row", sum("Alpha Tale" in t for t in titles) == 1)
    alpha_row = pg.locator("#qdone > li.qrow", has_text="Alpha Tale")
    summ = alpha_row.locator("details.earlier > summary")
    check("'+3 earlier runs' disclosure is present", summ.count() == 1 and summ.inner_text().strip() == "+3 earlier runs", summ.inner_text() if summ.count() else "missing")
    check("earlier runs are hidden until opened", not alpha_row.locator(".earlier-list").is_visible())
    summ.click()
    until(lambda: alpha_row.locator(".earlier-list").is_visible(), 3, "earlier list visible")
    lines = alpha_row.locator(".earlier-list li").all_inner_texts()
    check("opened disclosure lists the 3 earlier runs, one line each (kind, when, segments, time)",
          len(lines) == 3 and sum("full pass" in l for l in lines) == 1 and sum("touch-up" in l for l in lines) == 2 and all("segment" in l for l in lines), str(lines))
    pg.screenshot(path=str(OUT / "queue-1-finished.png"))
    timing = [t for t in pg.locator("#qdone > li.qrow .q-eta").all_inner_texts()]
    check("finished rows say 'processed in ...' (actual) and never 'estimate'", all(t.strip().startswith("processed in") for t in timing) and not any("estimate" in t for t in timing), str(timing[:3]))

    # ---------------------------------------------------------------------------------------- pagination (finished)
    pg.click("#q-done button[data-pager=next]")
    until(lambda: "11–17 of 17" in pg.locator("#q-done .pager-range").inner_text(), 5, "page 2")
    check("page 2 has the remaining 7 rows", pg.locator("#qdone > li.qrow").count() == 7)
    first_titles_p2 = set(pg.locator("#qdone > li.qrow .q-title a").all_inner_texts())
    check("page 2 holds different books than page 1", not (first_titles_p2 & set(titles)))
    time.sleep(3.5)  # a refresh passes: the page must stay on page 2
    check("a refresh keeps the current page", "11–17 of 17" in pg.locator("#q-done .pager-range").inner_text())
    pg.click("#q-done button[data-pager=prev]")
    until(lambda: "1–10 of 17" in pg.locator("#q-done .pager-range").inner_text(), 5, "back to page 1")

    # ---------------------------------------------------------------------------------------- pagination (utility)
    head = pg.locator("#q-util thead th").all_inner_texts()
    check("utility table has a Book column", "book" in [h.lower() for h in head], str(head))
    check("utility card shows 10 rows", pg.locator("#q-util tbody tr").count() == 10)
    urange = pg.locator("#q-util .pager-range").inner_text()
    m = re.match(r"1–10 of (\d+)", urange)
    check("utility pager reads '1–10 of N' with N > 10", bool(m) and int(m.group(1)) > 10, urange)
    pg.click("#q-util button[data-pager=next]")
    until(lambda: urange not in pg.locator("#q-util .pager-range").inner_text(), 5, "utility page 2")
    check("utility page 2 shows the rest", 0 < pg.locator("#q-util tbody tr").count() <= 10)
    pg.click("#q-util button[data-pager=prev]")
    until(lambda: pg.locator("#q-util .pager-range").inner_text() == urange, 5, "utility back to page 1")
    check("utility rows link to the book", pg.locator("#q-util tbody tr a[href^='#/book/']").count() >= 1)

    # ---------------------------------------------------------------------------------------- open log keeps its scroll through live updates
    log_id = beta_job
    pg.click(f"#joblog-{log_id} > summary")
    until(lambda: "Synthetic log line" in pg.locator(f"#log-{log_id}").inner_text(), 5, "log loaded")
    box = f"#log-{log_id}"
    pg.evaluate(f"document.querySelector('{box}').scrollTop = 150")
    scroll0 = pg.evaluate(f"document.querySelector('{box}').scrollTop")
    check("the log box really scrolls (fixture sanity)", scroll0 > 50, str(scroll0))
    pg.evaluate("window.__watch = true")
    hits0 = queue_gets()

    # ---------------------------------------------------------------------------------------- running + queued estimates (live)
    SLOW[0] = 0.35
    runner = make_book("Runner Tale", chapters=6, paras=4)
    waiter = make_book("Waiter Tale", chapters=2, paras=3)
    until(lambda: pg.locator("#qlist .qrow.st-running").count() == 1, 20, "runner visible as running")
    until(lambda: "left" in pg.locator("#qlist .qrow.st-running .q-eta").inner_text(), 20, "remaining time shown")
    run_txt = pg.locator("#qlist .qrow.st-running .q-eta").inner_text()
    check("running job shows 'about ... left · done about HH:MM'", re.search(r"about .+ left · done about (\w{3} )?\d\d:\d\d", run_txt) is not None, run_txt)
    wait_txt = pg.locator("#qlist .qrow.st-queued .q-eta").inner_text()
    check("queued job shows 'about ... processing · starts about HH:MM'", re.search(r"about .+ processing · starts about (\w{3} )?\d\d:\d\d", wait_txt) is not None, wait_txt)
    pg.screenshot(path=str(OUT / "queue-2-running.png"))
    api_q = api.get("/api/queue").json()
    check("API: running job's eta_s agrees with its runtime.remaining_s",
          any(j["status"] == "running" and j["eta_s"] == j["runtime"]["remaining_s"] for j in api_q["active"]))
    # let several refreshes happen while the book renders
    until(lambda: queue_gets() - hits0 >= 4, 25, "several live refreshes")
    scroll1 = pg.evaluate(f"document.querySelector('{box}').scrollTop")
    check("open log keeps its scroll position across live repaints", abs(scroll1 - scroll0) <= 2, f"{scroll0} -> {scroll1}")
    check("open log stays open", pg.evaluate(f"document.querySelector('#joblog-{log_id}').open") is True)
    check("open log never flashed 'Loading…' during repaints", pg.evaluate("window.__loadingFlash") == 0, str(pg.evaluate("window.__loadingFlash")))

    # ---------------------------------------------------------------------------------------- everything finishes: still one row each
    SLOW[0] = 0.0
    until(idle, 90, "runner and waiter finished")
    until(lambda: pg.locator("#qdone > li.qrow", has_text="Runner Tale").count() == 1 and pg.locator("#qdone > li.qrow", has_text="Waiter Tale").count() == 1, 15, "runner and waiter in the finished list")
    check("finished rows still say 'processed in'", all(t.strip().startswith("processed in") for t in pg.locator("#qdone > li.qrow .q-eta").all_inner_texts()))
    check("Runner Tale and Waiter Tale each appear once", pg.locator("#qdone > li.qrow", has_text="Runner Tale").count() == 1 and pg.locator("#qdone > li.qrow", has_text="Waiter Tale").count() == 1)
    pg.screenshot(path=str(OUT / "queue-3-done.png"))
    br.close()

worker.stop_event.set()
bad = [e for e in errors if "favicon" not in e]
check("no console errors or page errors", not bad, "; ".join(bad[:4]))
print("\nFAILED:" if failures else "\nALL CHECKS PASSED", failures if failures else "")
sys.exit(1 if failures else 0)
