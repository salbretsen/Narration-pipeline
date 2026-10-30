"""Drive the Review & QA page (regenerate with direction, live refresh, loading state) in a real browser (Edge) against a local MOCK-engine studio.
No Mac, no GPU, no network.

    python tools/e2e/polish_review.py OUT_DIR

Proves, in a real browser:
  * (a) text typed into the spoken-rendition box, the note and the direction form survives eight seconds of live refreshes while the worker
        keeps re-rendering other segments (and the page really did refetch the segment meanwhile)
  * (b) emotion anger + intensity 5 + delivery note "shouting": Preview explains the instruction that reaches the voice; "Regenerate with this
        direction" queues the segment (pending), the worker re-renders it, and the stored user direction has emotion anger
  * (c) on a cloned narrator the explanation says a clone cannot take written instructions
  * (d) opening a book from the Review picker shows the "Loading..." state within 400 ms while the book endpoint is artificially slow
  * (e) no console errors
Not covered: real audio, real engine, real Safari, a real Mac. The mock engine makes tones, so nothing here says how a real model reacts.
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
from conftest import make_wav_bytes  # noqa: E402
from narrative_fixtures import TXT  # noqa: E402

from studio import services as S  # noqa: E402
from studio.context import Context  # noqa: E402
from studio.engines import MockASR, MockEngine  # noqa: E402
from studio.web.app import create_app  # noqa: E402
from studio.worker import Worker  # noqa: E402

ctx = Context(home / "state")
app = create_app(ctx, start_worker=False)
PORT = 7934
app.state.port = PORT
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="on"))
threading.Thread(target=server.run, daemon=True).start()
time.sleep(1.5)
B = f"http://127.0.0.1:{PORT}"
api = httpx.Client(base_url=B, headers={"X-Studio-Request": "1"}, timeout=60)
failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
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
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


# ---- data: a semantic book with the rules Director, a clone narrator book, a worker that keeps running ------------------------
r = api.put("/api/settings", json={"narrative": {"semantic_segmentation": True, "director_enabled": True}})
assert r.status_code == 200, r.text
voices = api.get("/api/voices").json()["voices"]
ryan = next(v["id"] for v in voices if v["name"] == "Ryan")
clone = api.post("/api/voices/clone", data={"name": "Clone Test", "ref_text": "This is a short sample of my own speaking voice.", "rights_holder": "Jane Author",
                                            "owner_attest": "true"}, files={"file": ("me.wav", make_wav_bytes(6.0), "audio/wav")})
assert clone.status_code == 200, clone.text


def make_book(title: str, voice_id: int) -> int:
    prev = api.post("/api/imports", files={"file": (f"{title}.txt", TXT.encode(), "text/plain")}).json()
    return api.post(f"/api/imports/{prev['token']}/confirm", json={"title": title, "voice_id": voice_id, "queue": False}).json()["id"]


BOOK = make_book("The Flood", ryan)
CLONE_BOOK = make_book("The Flood (clone)", clone.json()["id"])
for b in (BOOK, CLONE_BOOK):
    api.post(f"/api/books/{b}/analyze", json={})
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
until(lambda: all(api.get(f"/api/books/{b}/narrative").json()["directed"] >= 1 for b in (BOOK, CLONE_BOOK)), 60, "directions")
for b in (BOOK, CLONE_BOOK):
    api.post("/api/queue", json={"book_id": b})


def segments(book: int) -> list[dict]:
    return api.get(f"/api/books/{book}/segments", params={"filter": "all", "limit": 200}).json()["items"]


until(lambda: all(s["status"] in ("passed", "flagged", "approved") for s in segments(BOOK)) and all(s["status"] in ("passed", "flagged", "approved") for s in segments(CLONE_BOOK)), 90, "books rendered")
narration = [s for s in segments(BOOK) if s["kind"] != "heading" and s["span_kind"] == "narration"]
assert len(narration) >= 3, [s["span_kind"] for s in segments(BOOK)]
TARGET = narration[0]["id"]
OTHERS = [s["id"] for s in narration[1:]]
clone_narration = [s for s in segments(CLONE_BOOK) if s["kind"] != "heading" and s["span_kind"] == "narration"][0]["id"]

# slow the book API on demand (the app runs in this process, so patching the service function is enough)
DELAY = {"s": 0.0}
_real_get_book = S.get_book


def slow_get_book(c, book_id):
    time.sleep(DELAY["s"])
    return _real_get_book(c, book_id)


S.get_book = slow_get_book

_real_get_book_review = S.get_book_review  # the Review page uses the light payload (GET /api/books/{id}?view=review) when the server has it


def slow_get_book_review(c, book_id):
    time.sleep(DELAY["s"])
    return _real_get_book_review(c, book_id)


S.get_book_review = slow_get_book_review

from playwright.sync_api import sync_playwright  # noqa: E402

errors: list[str] = []
detail_gets: list[float] = []
with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True)
    pg = br.new_page(viewport={"width": 1280, "height": 1100})
    pg.add_init_script("window.__states = 0; window.addEventListener('studio:state', () => { window.__states++; });")
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.on("request", lambda r: detail_gets.append(time.time()) if r.method == "GET" and r.url.split("?")[0].endswith(f"/api/segments/{TARGET}") else None)

    # ------------------------------------------------------------------ (a) typed text survives live refreshes
    pg.goto(B + f"/#/review/{BOOK}?seg={TARGET}&filter=all")
    pg.wait_for_selector("#override-text", timeout=15000)
    pg.wait_for_selector("#rv-explain h3", timeout=10000)
    check("(a) regenerate-with-direction panel is present and collapsed", pg.locator("details#regen-direction").count() == 1 and not pg.evaluate("document.querySelector('#regen-direction').open"))
    check("(a) the explanation is visible beside the collapsed panel", pg.locator("#rv-explain").is_visible() and "What reaches the voice" in pg.inner_text("#rv-explain"))
    original = pg.input_value("#override-text")
    pg.click("details#regen-direction > summary")
    pg.fill("#note-text", "typed note that must survive")
    pg.fill("#override-text", original + " (my edit)")
    pg.select_option("#rd-emotion", "anger")
    pg.fill("#rd-delivery", "shouting")
    pg.screenshot(path=str(OUT / "1-typed.png"), full_page=True)
    n_before = len(detail_gets)
    states_before = pg.evaluate("window.__states")
    stop_churn = threading.Event()

    def churn():  # keeps the worker busy with OTHER segments so live updates keep arriving
        i = 0
        while not stop_churn.is_set():
            try:
                api.post(f"/api/segments/{OTHERS[i % len(OTHERS)]}/regenerate", json={"note": "churn"})
            except Exception:
                pass
            i += 1
            stop_churn.wait(1.2)

    t = threading.Thread(target=churn, daemon=True)
    t.start()
    pg.wait_for_timeout(8000)  # pumps the browser events (a plain sleep would not deliver the request callbacks)
    stop_churn.set()
    t.join(2)
    refreshes = len(detail_gets) - n_before
    check("(a) the page really refreshed meanwhile (live updates arrived and the segment was refetched)", refreshes >= 1 and pg.evaluate("window.__states") > states_before,
          f"{refreshes} refetches, {pg.evaluate('window.__states') - states_before} pushes")
    check("(a) note kept", pg.input_value("#note-text") == "typed note that must survive", pg.input_value("#note-text"))
    check("(a) spoken-text edit kept", pg.input_value("#override-text") == original + " (my edit)")
    check("(a) direction form kept (emotion + delivery note)", pg.input_value("#rd-emotion") == "anger" and pg.input_value("#rd-delivery") == "shouting")
    check("(a) the direction panel is still open", pg.evaluate("document.querySelector('#regen-direction').open") is True)
    until(lambda: all(s["status"] in ("passed", "flagged", "approved") for s in segments(BOOK)), 60, "churn settled")

    # ------------------------------------------------------------------ (b) preview, then regenerate with direction
    pg.fill("#note-text", "")
    pg.select_option("#rd-emotion", "anger")
    pg.select_option("#rd-intensity", "5")
    pg.fill("#rd-delivery", "shouting")
    pg.click("button[data-act=preview-dir]")
    until(lambda: "Sent to the voice as" in pg.inner_text("#rv-explain"), 10, "preview text")
    txt = pg.inner_text("#rv-explain")
    check("(b) preview names the instruction that reaches a preset voice", "angry" in txt and "shouting" in txt, txt[:300])
    check("(b) preview wrote nothing", api.get(f"/api/segments/{TARGET}/delivery-preview").json()["user_direction"].get("emotion") != "anger")
    pg.screenshot(path=str(OUT / "2-preview.png"), full_page=True)
    r0 = api.get(f"/api/segments/{TARGET}").json()["segment"]["round"]
    with pg.expect_response(lambda r: r.url.endswith(f"/api/segments/{TARGET}/regenerate") and r.request.method == "POST") as ri:
        pg.click("button[data-act=regen-dir]")
    body = ri.value.json()
    check("(b) regenerate-with-direction answered pending with a delivery summary", ri.value.status == 200 and body["status"] == "pending" and body["delivery"]["applied"] is True, str(body)[:300])
    seg_now = api.get(f"/api/segments/{TARGET}").json()["segment"]
    check("(b) the segment was queued (round incremented)", seg_now["round"] == r0 + 1, str(seg_now["round"]))
    try:
        until(lambda: api.get(f"/api/segments/{TARGET}").json()["segment"]["status"] in ("passed", "flagged"), 30, "re-rendered")
    except AssertionError:
        print("DEBUG segment", api.get(f"/api/segments/{TARGET}").json()["segment"]["status"], "jobs", [(j["id"], j["book_id"], j["status"], j["remaining"], j["error"]) for j in api.get("/api/queue").json()["jobs"]], "worker", api.get("/api/worker").json())
        raise
    det = api.get(f"/api/segments/{TARGET}").json()
    sel = next(a for a in det["attempts"] if a["selected"])
    check("(b) the new take was rendered with the instruction", sel["round"] == r0 + 1 and "angry" in (sel["instruct_used"] or "") and "shouting" in (sel["instruct_used"] or ""), str(sel["instruct_used"]))
    check("(b) the stored user direction has emotion anger", api.get(f"/api/segments/{TARGET}/delivery-preview").json()["user_direction"].get("emotion") == "anger")
    until(lambda: "Clear direction" in pg.inner_text("#rv-dir-btns"), 10, "clear button")
    pg.screenshot(path=str(OUT / "3-regenerated.png"), full_page=True)
    pg.click("button[data-act=clear-dir]")
    until(lambda: not api.get(f"/api/segments/{TARGET}/delivery-preview").json()["user_direction"], 15, "direction cleared")
    try:  # the page clears the form after its PATCH returns; the API can show the change a moment earlier
        until(lambda: pg.input_value("#rd-emotion") == "" and pg.input_value("#rd-delivery") == "", 5, "form cleared")
    except AssertionError:
        pass
    check("(b) Clear direction empties the form", pg.input_value("#rd-emotion") == "" and pg.input_value("#rd-delivery") == "")
    until(lambda: api.get(f"/api/segments/{TARGET}").json()["segment"]["status"] in ("passed", "flagged"), 30, "back to rendered")

    # ------------------------------------------------------------------ (c) clone narrator: honest explanation
    pg.goto(B + f"/#/review/{CLONE_BOOK}?seg={clone_narration}&filter=all")
    pg.wait_for_selector("#rd-emotion", timeout=15000)
    pg.click("details#regen-direction > summary")
    pg.select_option("#rd-emotion", "anger")
    pg.select_option("#rd-intensity", "5")
    until(lambda: "cannot take written instructions" in pg.inner_text("#rv-explain"), 10, "clone explanation")
    txt = pg.inner_text("#rv-explain")
    check("(c) the clone explanation says a cloned voice takes no written instruction", "cannot take written instructions" in txt and "Narrator Studio" in txt, txt[:400])
    check("(c) it names the missing reference clip", "reference" in txt.lower())
    check("(c) link to Narrator Studio", pg.locator("#rv-explain a[href='#/voices']").count() >= 1)
    pg.screenshot(path=str(OUT / "4-clone.png"), full_page=True)

    # ------------------------------------------------------------------ (d) loading state from the picker
    DELAY["s"] = 1.5
    pg.goto(B + "/#/review")
    pg.wait_for_selector(".book-pick a", timeout=15000)
    t0 = time.time()
    pg.click(f".book-pick a[href^='#/review/{BOOK}']")
    pg.wait_for_selector(".view-loading", timeout=400)
    seen = (time.time() - t0) * 1000
    check("(d) the loading state shows within 400 ms of clicking a book in the picker", seen < 400, f"{seen:.0f} ms")
    pg.wait_for_selector("#seg-list .seg-item", timeout=8000)
    check("(d) the review page paints once the book arrives and the loading state is gone", pg.locator(".view-loading").count() == 0 and "Review:" in pg.inner_text("#view h1"))
    DELAY["s"] = 0.0
    br.close()

stop.set()
bad = [e for e in errors if "favicon" not in e]
check("(e) no console errors or page errors", not bad, "; ".join(bad[:4]))
print("\nFAILED:" if failures else "\nALL CHECKS PASSED", failures if failures else "")
sys.exit(1 if failures else 0)
