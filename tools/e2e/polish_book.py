"""Drive the P2-A book page and Disk page in a real browser (Edge) against a local MOCK-engine studio. No Mac, no GPU, no network.

    python tools/e2e/polish_book.py OUT_DIR

Proves, in a real browser, against a worker that is rendering the book (so live updates really fire):
  * (a) text typed in the metadata title, the release approved_by / notes / attest checkbox and the delivery-instructions field is still there
        after 10 s of live updates, while the stats strip (a live region) visibly changed
  * (b) a cover upload with an unsaved metadata edit pending: the edit survives and the cover <img> src changes (twice)
  * (c) a chapter mini-player that is "playing" is not re-created by a live update that changes the chapters region; once it stops, the region catches up
  * (d) library -> book with a slow (1.5 s) book API: .view-loading (or the page) within 400 ms, the book page appears after the delay
  * (e) Disk & Backups with a slow (1.5 s) usage measurement: the page and "Measuring disk usage" placeholders appear at once, then the numbers
        fill in with a "Measured ..." line; Refresh re-measures
  * (f) no console errors
Not covered: real Safari, the real engine, a real Mac, real audio playback (the "playing" state of check (c) is simulated in the page).
"""
from __future__ import annotations

import os
import struct
import sys
import tempfile
import threading
import time
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "app"))
sys.path.insert(0, str(ROOT / "src" / "app" / "tests"))
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="polish-book-out-"))
OUT.mkdir(parents=True, exist_ok=True)

home = Path(tempfile.mkdtemp(prefix="studio-ui-"))
os.environ.update({"AUDIOBOOK_STUDIO_HOME": str(home / "state"), "STUDIO_ENGINE": "mock", "STUDIO_NO_WORKER": "1", "STUDIO_NO_UPDATE_CHECK": "1"})

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from book_synth import build  # noqa: E402
from conftest import make_text  # noqa: E402

from studio import diskmgmt as D  # noqa: E402
from studio import services as S  # noqa: E402
from studio.context import Context  # noqa: E402
from studio.engines import MockASR, MockEngine  # noqa: E402
from studio.web.app import create_app  # noqa: E402
from studio.worker import Worker  # noqa: E402

ctx = Context(home / "state")
app = create_app(ctx, start_worker=False)
PORT = 7931
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


def png(rgb: tuple[int, int, int]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(rgb) * 8 for _ in range(8))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


# ---- fixtures ---------------------------------------------------------------------------------------------------------------
ryan = next(v["id"] for v in api.get("/api/voices").json()["voices"] if v["name"] == "Ryan")


def make_book(title: str, chapters: int) -> int:
    prev = api.post("/api/imports", files={"file": (f"{title}.txt", make_text(chapters, paras=6, title=title).encode(), "text/plain")}).json()
    return api.post(f"/api/imports/{prev['token']}/confirm", json={"title": title, "voice_id": ryan, "queue": False}).json()["id"]


A_ID = make_book("Alpha Tale", 6)                 # rendered by the slow worker during check (a)/(b)
GAMMA_ID = build(ctx, 60, 4, title="Gamma Tale")  # synthetic rows + 3 placeholder masters: chapter players without a real render


class SlowEngine(MockEngine):
    def synthesize(self, *a, **k):
        time.sleep(0.45)
        return super().synthesize(*a, **k)


worker = Worker(ctx, tts=SlowEngine({}), asr=MockASR())
threading.Thread(target=worker.run_forever, kwargs={"idle_sleep": 0.5}, daemon=True).start()
until(lambda: api.get("/api/worker").json()["online"], 10, "worker online")
n_segments = api.get(f"/api/books/{A_ID}").json()["segments"]
print(f"INFO  Alpha Tale has {n_segments} segments; the slow worker needs about {n_segments * 0.45:.0f} s")

DELAY = {"book": 0.0, "usage": 0.0}
_real_get_book, _real_usage = S.get_book, D.usage


def slow_get_book(c, book_id):
    time.sleep(DELAY["book"])
    return _real_get_book(c, book_id)


def slow_usage(c, *a, **k):
    time.sleep(DELAY["usage"])
    return _real_usage(c, *a, **k)


S.get_book, D.usage = slow_get_book, slow_usage

from playwright.sync_api import sync_playwright  # noqa: E402

errors: list[str] = []
with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True)
    pg = br.new_page(viewport={"width": 1280, "height": 1000})
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

    # ------------------------------------------------------------------ (a) typed values survive live updates
    pg.goto(f"{B}/#/book/{A_ID}")
    pg.wait_for_selector("#bk-stats .stat-value", timeout=15000)
    api.post("/api/queue", json={"book_id": A_ID})
    until(lambda: api.get(f"/api/books/{A_ID}").json()["produced"] > 0, 15, "worker rendering")
    pg.fill("#meta-form [name=title]", "Alpha Tale (edited, not saved)")
    pg.fill("#meta-form [name=author]", "Unsaved Author")
    pg.fill("#meta-form [name=description]", "unsaved description text")
    pg.fill("#release-form [name=approved_by]", "Ada Lovelace")
    pg.fill("#release-form [name=notes]", "typed notes that must stay")
    pg.check("#release-form [name=attest]")
    pg.check("#release-form [value=partial]")
    pg.fill("#ver-form [name=instructions]", "slower, warmer delivery")
    pg.click("#replace-narrator summary")
    pg.fill("#nar-form [name=instructions]", "replace-form instructions")
    stats0 = pg.inner_text("#bk-stats")
    # count real DOM mutations of the live stats region; forms must see none
    pg.evaluate("""() => { window.__formMut = 0; window.__statsMut = 0;
        const f = new MutationObserver(() => { window.__formMut++; });
        for (const s of ['#meta-form', '#release-form', '#ver-form', '#nar-form']) f.observe(document.querySelector(s), { childList: true, subtree: true });
        const m = new MutationObserver(() => { window.__statsMut++; });
        m.observe(document.querySelector('#bk-stats'), { childList: true, subtree: true }); }""")
    time.sleep(10.0)
    check("(a) metadata title kept", pg.input_value("#meta-form [name=title]") == "Alpha Tale (edited, not saved)", pg.input_value("#meta-form [name=title]"))
    check("(a) author and description kept", pg.input_value("#meta-form [name=author]") == "Unsaved Author" and pg.input_value("#meta-form [name=description]") == "unsaved description text")
    check("(a) release approved_by and notes kept", pg.input_value("#release-form [name=approved_by]") == "Ada Lovelace" and pg.input_value("#release-form [name=notes]") == "typed notes that must stay")
    check("(a) release attest checkbox and listen scope kept", pg.is_checked("#release-form [name=attest]") and pg.is_checked("#release-form [value=partial]"))
    check("(a) delivery instructions kept in both narrator forms", pg.input_value("#ver-form [name=instructions]") == "slower, warmer delivery" and pg.input_value("#nar-form [name=instructions]") == "replace-form instructions")
    stats1 = pg.inner_text("#bk-stats")
    check("(a) live updates did fire: the stats strip changed during the 10 s", stats1 != stats0, f"{stats0.splitlines()[0]} -> {stats1.splitlines()[0]}")
    check("(a) the live region was mutated but no form card was touched", pg.evaluate("window.__statsMut") > 0 and pg.evaluate("window.__formMut") == 0,
          f"stats mutations {pg.evaluate('window.__statsMut')}, form mutations {pg.evaluate('window.__formMut')}")
    check("(a) page title (h1) still the saved title", "Alpha Tale" in pg.inner_text("#bk-h1") and "edited" not in pg.inner_text("#bk-h1"))
    pg.screenshot(path=str(OUT / "1-forms-kept.png"))

    # ------------------------------------------------------------------ (b) cover upload keeps a pending metadata edit
    check("(b) no cover yet: placeholder shown", pg.locator("#bk-cover-slot img").count() == 0)
    pg.set_input_files("#cover-input", files=[{"name": "c1.png", "mimeType": "image/png", "buffer": png((200, 30, 30))}])
    until(lambda: pg.locator("#bk-cover-slot img").count() == 1, 10, "cover image")
    src1 = pg.get_attribute("#cover-img", "src")
    check("(b) cover <img> appears with a versioned URL", src1 and "/media/cover/" in src1 and "v=" in src1, src1 or "")
    check("(b) pending metadata edit survived the cover upload", pg.input_value("#meta-form [name=title]") == "Alpha Tale (edited, not saved)" and pg.input_value("#release-form [name=approved_by]") == "Ada Lovelace")
    time.sleep(1.2)
    pg.set_input_files("#cover-input", files=[{"name": "c2.png", "mimeType": "image/png", "buffer": png((20, 40, 220))}])
    until(lambda: pg.get_attribute("#cover-img", "src") != src1, 10, "cover src change")
    check("(b) a second upload changes the <img> src", pg.get_attribute("#cover-img", "src") != src1, pg.get_attribute("#cover-img", "src") or "")
    check("(b) edit still pending after the second upload", pg.input_value("#meta-form [name=title]") == "Alpha Tale (edited, not saved)")
    # the user's own save re-seeds the form from the server
    pg.fill("#meta-form [name=title]", "  Alpha Tale Renamed  ")
    pg.click("#meta-form button[type=submit]")
    until(lambda: "Alpha Tale Renamed" in pg.inner_text("#bk-h1"), 10, "saved title in header")
    check("(b) own save: server-trimmed value shown in the field and the header", pg.input_value("#meta-form [name=title]") == "Alpha Tale Renamed")
    check("(b) own save keeps the other metadata fields (they were saved too)", pg.input_value("#meta-form [name=author]") == "Unsaved Author")
    pg.screenshot(path=str(OUT / "2-cover.png"))

    # ------------------------------------------------------------------ (c) a playing chapter player is not re-created
    pg.goto(f"{B}/#/book/{GAMMA_ID}")
    pg.wait_for_selector("#bk-chapters audio", timeout=15000)
    pg.evaluate("""() => { const a = document.querySelector('#bk-chapters audio'); a.__mark = 'original'; a.__playing = true;
        Object.defineProperty(a, 'paused', { get() { return !a.__playing; } }); }""")
    with ctx.db.tx() as conn:   # change the chapters markup (flagged counts) and the stats at once
        conn.execute("UPDATE segments SET status='flagged', updated_at=? WHERE book_id=? AND id IN (SELECT id FROM segments WHERE book_id=? LIMIT 6)", (time.time(), GAMMA_ID, GAMMA_ID))
    until(lambda: "flagged" in pg.inner_text("#bk-stats") or pg.locator("#bk-stats .stat.warn").count() > 0, 15, "stats region updated")
    time.sleep(1.0)
    check("(c) while 'playing', the player element is the same one (not re-created)", pg.evaluate("document.querySelector('#bk-chapters audio').__mark") == "original")
    check("(c) ...but the rest of the page did update", pg.locator("#bk-stats .stat.warn").count() == 1)
    pg.evaluate("""() => { const a = document.querySelector('#bk-chapters audio'); a.__playing = false; a.dispatchEvent(new Event('pause')); }""")
    until(lambda: pg.evaluate("document.querySelector('#bk-chapters audio').__mark") != "original", 10, "chapters region caught up")
    check("(c) after it stops, the chapters region catches up (flagged links appear)", pg.locator("#bk-chapters a.warn-text").count() >= 1)

    # ------------------------------------------------------------------ (d) library -> book with a slow book API
    DELAY["book"] = 1.5
    pg.goto(f"{B}/#/library")
    pg.wait_for_selector(f"a[href='#/book/{A_ID}']", timeout=15000)
    t0 = time.time()
    pg.click(f"a[href='#/book/{A_ID}']")
    pg.wait_for_selector(".view-loading", timeout=400)
    seen_ms = (time.time() - t0) * 1000
    check("(d) loading status shows within 400 ms of the click", seen_ms < 400, f"{seen_ms:.0f} ms")
    until(lambda: pg.locator("#bk-head h1").count() == 1 and pg.locator(".view-loading").count() == 0, 6, "book page painted")
    total_ms = (time.time() - t0) * 1000
    check("(d) the book page painted after the slow API (>= 1.5 s) and the loading status is gone", total_ms >= 1400 and pg.locator("#view.view-stale").count() == 0, f"{total_ms:.0f} ms")
    check("(d) all live regions are filled on first paint", pg.locator("#bk-stats .stat").count() >= 4 and pg.locator("#bk-chapters tbody tr").count() >= 1 and pg.locator("#bk-log li, #bk-log p").count() >= 0)
    DELAY["book"] = 0.0
    # with a fast API the same click reaches the finished page quickly
    pg.goto(f"{B}/#/library")
    pg.wait_for_selector(f"a[href='#/book/{A_ID}']", timeout=15000)
    t0 = time.time()
    pg.click(f"a[href='#/book/{A_ID}']")
    pg.wait_for_selector("#bk-head h1", timeout=5000)
    print(f"INFO  library click -> finished book page (fast API, {n_segments} segments rendering): {(time.time() - t0) * 1000:.0f} ms")

    # ------------------------------------------------------------------ (e) Disk page: placeholders first, then data
    DELAY["usage"] = 1.5
    D.invalidate_usage(ctx)
    t0 = time.time()
    pg.goto(f"{B}/#/disk")
    pg.wait_for_selector("#view h1", timeout=3000)
    first_paint_ms = (time.time() - t0) * 1000
    check("(e) the Disk page heading is up before the slow measurement returns", first_paint_ms < 1400, f"{first_paint_ms:.0f} ms")
    pg.wait_for_selector("#dk-books [role=status]", timeout=1200)
    check("(e) placeholder 'Measuring disk usage' cards are shown first", "Measuring disk usage" in pg.inner_text("#dk-books") and "Measuring" in pg.inner_text("#dk-age"))
    check("(e) the Refresh button is disabled while measuring", pg.is_disabled("#dk-refresh"))
    pg.screenshot(path=str(OUT / "3-disk-measuring.png"))
    until(lambda: pg.locator("#dk-books table").count() == 1, 8, "usage table")
    check("(e) the numbers fill in (books table, free space) and the placeholder is gone", "Measuring disk usage" not in pg.inner_text("#dk-books") and "free" in pg.inner_text("#dk-free"))
    check("(e) 'Measured ...' line shown and Refresh enabled", pg.inner_text("#dk-age").startswith("Measured") and not pg.is_disabled("#dk-refresh"), pg.inner_text("#dk-age"))
    check("(e) backups card is filled", pg.locator("#dk-backups h3").count() == 1)
    DELAY["usage"] = 0.0
    hits = []
    pg.on("request", lambda r: hits.append(r.url) if "/api/disk/usage" in r.url else None)
    pg.click("#dk-refresh")
    until(lambda: any("refresh=1" in h for h in hits), 5, "refresh request")
    check("(e) Refresh forces a re-measure (?refresh=1)", any("refresh=1" in h for h in hits), str(hits))
    pg.screenshot(path=str(OUT / "4-disk-done.png"))
    br.close()

worker.stop_event.set()
bad = [e for e in errors if "favicon" not in e]
check("(f) no console errors or page errors", not bad, "; ".join(bad[:4]))
print("\nFAILED:" if failures else "\nALL CHECKS PASSED", failures if failures else "")
sys.exit(1 if failures else 0)
