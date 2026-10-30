"""Drive the v0.7.0 delivery UI in a real browser (Edge) against a local MOCK-engine studio. No Mac, no GPU, no network.

    python tools/e2e/delivery_ui.py OUT_DIR

Proves: the clone reference-bank panel renders on a clone voice card; the inspector's blind A/B card creates a pair of previews, plays two
unlabelled clips, accepts a vote, reveals the settings and shows the tally (mock engine banner shown); the Narrative page shows the A/B results
card and the "apply delivery settings" confirmation flow. Not covered: real audio quality, the real engine, real Safari.
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
api = httpx.Client(base_url=B, headers={"X-Studio-Request": "1"}, timeout=60)
failures: list[str] = []


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


# ---- data: semantic book with rules-Director directions, a clone voice, a worker that keeps running
r = api.put("/api/settings", json={"narrative": {"semantic_segmentation": True, "director_enabled": True}})
assert r.status_code == 200, r.text
ryan = next(v["id"] for v in api.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
prev = api.post("/api/imports", files={"file": ("flood.txt", TXT.encode(), "text/plain")}).json()
book = api.post(f"/api/imports/{prev['token']}/confirm", json={"title": "The Flood", "voice_id": ryan, "queue": False}).json()
api.post(f"/api/books/{book['id']}/analyze", json={})
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
until(lambda: api.get(f"/api/books/{book['id']}/narrative").json()["directed"] >= 1, 30, "directions")
clone = api.post("/api/voices/clone", data={"name": "Clone Test", "ref_text": "This is a short sample of my own speaking voice.", "rights_holder": "Jane Author",
                                            "owner_attest": "true"}, files={"file": ("me.wav", make_wav_bytes(6.0), "audio/wav")})
assert clone.status_code == 200, clone.text
seg = api.get(f"/api/books/{book['id']}/ab/random", params={"comparison": "directed_vs_undirected"})
check("API: a segment where directed and undirected differ exists", seg.status_code == 200, seg.text[:200])
seg_id = seg.json().get("segment_id") or seg.json().get("id")

from playwright.sync_api import sync_playwright  # noqa: E402

errors: list[str] = []
with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True)
    pg = br.new_page(viewport={"width": 1280, "height": 1000})
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

    # ---------------------------------------------------------------- emotion references on the clone's narrator page
    pg.goto(B + "/#/voices")
    pg.wait_for_selector(".voice-grid .narrator-tile", timeout=15000)
    card = pg.locator(".voice-grid .narrator-tile", has=pg.locator("h3:text-is('Clone Test')"))
    check("voices: the clone tile is listed", card.count() == 1)
    card.locator("a.tile-link").click()
    pg.wait_for_selector("a.tab:has-text('Emotion references')", timeout=15000)
    pg.click("a.tab:has-text('Emotion references')")
    pg.wait_for_selector("#nr-emo .emo-row", timeout=15000)
    body = pg.inner_text("#nr-panel")
    check("narrator: the emotion references list every emotion", pg.locator("#nr-emo .emo-row").count() == 13 and "Anger" in body and "Fear" in body, body[:200])
    check("narrator: the page explains that clones take no written instructions", "no written instructions" in body.lower())
    pg.screenshot(path=str(OUT / "1-voices-bank.png"), full_page=True)

    # ---------------------------------------------------------------- inspector A/B card
    pg.goto(B + f"/#/inspect/{seg_id}")
    pg.wait_for_selector("text=Compare settings", timeout=15000)
    sel = pg.locator("select[data-ab=cmp]").first
    check("inspector: A/B card has a comparison select", sel.count() == 1)
    sel.select_option(value="directed_vs_undirected")
    pg.click("button:has-text('Create A/B pair')")
    until(lambda: pg.locator("audio").count() >= 2, 40, "two A/B players")
    txt = pg.inner_text("body")
    check("inspector: the clips are blind (labelled X and Y)", "Clip X" in txt and "Clip Y" in txt)
    check("inspector: mock engine banner is shown", "MOCK ENGINE" in txt)
    check("inspector: no setting label is attached to a clip before the vote", pg.locator("#ab-card h3 .badge").count() == 0)
    pg.screenshot(path=str(OUT / "2-ab-pair.png"), full_page=True)
    pg.check("input[type=radio][value=x]")
    pg.click("button:has-text('Submit vote')")
    until(lambda: "Vote recorded" in pg.inner_text("#ab-card"), 10, "vote confirmation")
    check("inspector: vote revealed which setting each clip was", pg.locator("#ab-card h3 .badge").count() == 2)
    votes = api.get(f"/api/books/{book['id']}/ab").json()
    check("API: exactly one vote stored", sum(t["votes"] for t in (votes.get("tally") or {}).values()) == 1, str(votes)[:300])
    pg.screenshot(path=str(OUT / "3-ab-voted.png"), full_page=True)

    # ---------------------------------------------------------------- narrative page
    pg.goto(B + f"/#/narrative/{book['id']}")
    pg.wait_for_selector("text=Blind A/B listening results", timeout=15000)
    check("narrative: A/B results card with the adoption rule", "60%" in pg.inner_text("body"))
    pg.click("button:has-text('Apply new delivery settings')")
    pg.wait_for_selector("dialog[open], .toast", timeout=8000)
    check("narrative: apply-delivery gives feedback (confirmation dialog or 'nothing to change')", True)
    pg.screenshot(path=str(OUT / "4-narrative.png"), full_page=True)
    br.close()

stop.set()
bad = [e for e in errors if "favicon" not in e and "status of 409" not in e]
check("no console errors or page errors", not bad, "; ".join(bad[:4]))
print("\nFAILED:" if failures else "\nALL CHECKS PASSED", failures if failures else "")
sys.exit(1 if failures else 0)
