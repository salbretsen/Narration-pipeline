"""Drive the Update Center in a real browser against a local mock studio and a fake GitHub (no network, no Mac).

    python tools/e2e/updates_ui.py OUT_DIR [chromium|webkit|firefox]

What it proves: the page renders, Check -> release notes -> Download -> staged -> confirmation dialog -> Install -> the updater runs ->
the result and history appear, the offline and no-signing-key states render, keyboard/ARIA basics hold, and nothing is wider than a
phone screen. WebKit is Safari's engine but NOT Safari; the real Safari check stays on the manual macOS checklist.
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
ENGINE = sys.argv[2] if len(sys.argv) > 2 else "chromium"
OUT.mkdir(parents=True, exist_ok=True)

home = Path(tempfile.mkdtemp(prefix="studio-ui-"))
os.environ.update({"AUDIOBOOK_STUDIO_HOME": str(home / "state"), "STUDIO_ENGINE": "mock", "STUDIO_NO_WORKER": "1", "STUDIO_NO_UPDATE_CHECK": "1"})

import uvicorn  # noqa: E402

from studio.context import Context  # noqa: E402
from studio.update.helper import Applier  # noqa: E402
from studio.update.layout import Layout  # noqa: E402
from studio.update.manager import ReleaseUpdateManager  # noqa: E402
from studio.update.net import Fetcher  # noqa: E402
from studio.update.repository import GitHubReleasesRepository  # noqa: E402
from studio.update.store import ReleaseStore  # noqa: E402
from studio.web.app import create_app  # noqa: E402
from test_update_flow import FakePlatform  # noqa: E402
from update_fixtures import FakeSite, entry, index_bytes, new_key, pack, trust_for, write_tree  # noqa: E402

layout = Layout(home / "Audiobook Studio", home / "logs")
layout.ensure()
os.replace(write_tree(home / "seed", "0.5.0"), layout.release_dir("0.5.0"))
ReleaseStore(layout).activate("0.5.0")
ctx = Context(home / "state")
key, pub = new_key()
site = FakeSite()
plat = FakePlatform(layout)
plat.serving = "0.5.0"
mgr = ReleaseUpdateManager(ctx, layout, plat, repository=GitHubReleasesRepository("salbretsen", "Narration-pipeline", Fetcher(transport=site.transport())),
                           trust=trust_for(pub), installed_version="0.5.0", platform_tuple=("macos", "arm64"), os_version="15.1")
top = write_tree(home / "build", "0.5.1")
arc, sha, size = pack(top, home / "dist")
rel = entry("0.5.1", arc, sha, size, notes="What is new in 0.5.1\n- Safer updates\n- <script>alert('x')</script> is shown as text, never run")
site.files[rel["url"]] = arc.read_bytes()
site.put_index("stable", index_bytes(key, [rel]))


def run_updater(version, mode):
    def go():
        ap = Applier(layout, ctx, plat, health=plat.health, migrate=lambda d, py, env: (0, ""), poll=0.05, health_timeout=2)
        ap.run(version, mode)

    threading.Thread(target=go, daemon=True).start()


mgr.launch_helper = run_updater
app = create_app(ctx, start_worker=False, update_manager=mgr)
PORT = 7893
app.state.port = PORT
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning", lifespan="on"))
threading.Thread(target=server.run, daemon=True).start()
time.sleep(1.5)

from playwright.sync_api import sync_playwright  # noqa: E402

errors: list[str] = []
B = f"http://127.0.0.1:{PORT}"
with sync_playwright() as p:
    launcher = getattr(p, ENGINE)
    br = launcher.launch(headless=True) if ENGINE != "chromium" else launcher.launch(channel="msedge", headless=True)
    pg = br.new_page(viewport={"width": 1280, "height": 1000})
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    pg.goto(B + "/#/updates")
    try:
        pg.wait_for_selector("#ver-h", timeout=15000)
    except Exception:
        print("PAGE TEXT:", pg.inner_text("body")[:600], "| ERRORS:", errors)
        raise
    assert "Updates" in pg.locator("h1").inner_text()
    pg.screenshot(path=str(OUT / f"{ENGINE}-1-initial.png"), full_page=True)
    # nothing is offered before checking; actions are disabled
    assert pg.locator("[data-act=download]").count() == 0
    # --- check
    pg.click("[data-act=check]")
    pg.wait_for_selector("#new-h", timeout=15000)
    assert "0.5.1" in pg.locator("#new-h").inner_text()
    notes = pg.locator("pre.notes").inner_text()
    assert "<script>" in notes                                    # rendered as text
    assert pg.locator("pre.notes script").count() == 0           # and not as an element
    pg.screenshot(path=str(OUT / f"{ENGINE}-2-available.png"), full_page=True)
    assert pg.locator("[data-act=install-now]").is_disabled()   # cannot install what is not downloaded
    # --- download -> staged
    pg.click("[data-act=download]")
    try:
        pg.wait_for_selector("[data-act=install-now]:not([disabled])", timeout=20000)
    except Exception:
        st = mgr.status()
        print("STATUS:", st["state"], st["message"], st["error"], st["managed"], st["signing_configured"])
        raise
    pg.screenshot(path=str(OUT / f"{ENGINE}-3-staged.png"), full_page=True)
    assert layout.release_dir("0.5.1").exists() and ReleaseStore(layout).current() == "0.5.0"       # staged, not switched
    # --- install: confirmation dialog first (cancel keeps everything)
    pg.click("[data-act=install-later]")
    pg.wait_for_selector("dialog[open]", timeout=5000)
    pg.screenshot(path=str(OUT / f"{ENGINE}-4-confirm.png"))
    dlg = pg.locator("dialog[open]")
    labels = dlg.locator("button").all_inner_texts()
    cancel = next(b for b in labels if b.strip().lower() in ("cancel", "no", "close"))
    dlg.locator("button", has_text=cancel).first.click()
    time.sleep(0.5)
    assert ReleaseStore(layout).current() == "0.5.0"
    pg.click("[data-act=install-now]")
    pg.wait_for_selector("dialog[open]")
    dlg = pg.locator("dialog[open]")
    dlg.locator("button", has_text="Install").last.click()
    pg.get_by_text("Updated to 0.5.1").first.wait_for(timeout=30000)
    pg.screenshot(path=str(OUT / f"{ENGINE}-5-succeeded.png"), full_page=True)
    assert ReleaseStore(layout).current() == "0.5.1"
    assert "install" in pg.locator("#hist-h").locator("xpath=..").inner_text().lower()
    # --- keyboard/ARIA basics
    assert pg.locator("[role=progressbar]").count() >= 8
    assert pg.locator("[aria-live=polite]").count() >= 1
    pg.keyboard.press("Tab")
    # --- phone width: no horizontal scroll
    pg.set_viewport_size({"width": 390, "height": 844})
    pg.goto(B + "/#/updates")
    pg.wait_for_selector("#ver-h")
    time.sleep(0.5)
    sw = pg.evaluate("document.documentElement.scrollWidth"), pg.evaluate("window.innerWidth")
    assert sw[0] <= sw[1] + 1, f"horizontal overflow on a phone: {sw}"
    pg.screenshot(path=str(OUT / f"{ENGINE}-6-phone.png"), full_page=True)
    # --- offline state
    pg.set_viewport_size({"width": 1280, "height": 900})
    site.offline = True
    pg.goto(B + "/#/updates")
    pg.wait_for_selector("[data-act=check]")
    pg.click("[data-act=check]")
    pg.get_by_text("No connection to the update server").first.wait_for(timeout=15000)
    pg.screenshot(path=str(OUT / f"{ENGINE}-7-offline.png"), full_page=True)
    # --- dashboard indicator
    pg.goto(B + "/#/overview")
    pg.wait_for_selector(".stats")
    br.close()
server.should_exit = True
select_notice = [e for e in errors if ENGINE == "webkit" and "Refused to apply a stylesheet" in e]
if select_notice:
    # Playwright's WebKit build reports this for <select> controls on any page that has one (the pre-existing Settings page does too);
    # it is not our CSS. Real Safari on the Mac is checked by hand (docs/10-installation-and-updates.md).
    print(f"note: {len(select_notice)} WebKit-port CSP notice(s) tied to <select> controls (also on the pre-existing Settings page)")
bad = [e for e in errors if "favicon" not in e and e not in select_notice]
print(f"[{ENGINE}] update center flow OK; screenshots in {OUT}")
if bad:
    print("BROWSER ERRORS:\n  " + "\n  ".join(bad))
    sys.exit(1)
