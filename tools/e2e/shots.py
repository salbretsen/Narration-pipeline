import sys, time
from playwright.sync_api import sync_playwright
OUT = sys.argv[1]
B = "http://127.0.0.1:7871"
errors = []
with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True)
    pg = br.new_page(viewport={"width": 1440, "height": 1000}, color_scheme="light")
    pg.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type in ("error", "warning") else None)
    pg.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
    def shot(hash_, name, sel, full=True):
        pg.goto(B + "/" + hash_)
        pg.wait_for_selector(sel, timeout=15000)
        time.sleep(1.2)
        pg.screenshot(path=f"{OUT}/{name}.png", full_page=full)
        print("shot", name)
    shot("#/overview", "01-overview", ".stats")
    shot("#/library", "02-library", "#lib-h")
    shot("#/queue", "03-queue", "#act-h")
    shot("#/voices", "04-voices", ".voice-grid .narrator-tile")
    shot("#/review/1?filter=flagged", "05-review-flagged", ".seg-detail")
    shot("#/book/1", "06-book-exports-release", "#rel-h")
    shot("#/settings", "07-settings", "#stor-h")
    shot("#/about", "08-system", "#d-h")
    # --- real UI interactions ---
    # 1) generate a voice preview via the UI and wait for the player
    pg.goto(B + "/#/voices"); pg.wait_for_selector(".narrator-tile a.tile-link")
    pg.goto(B + "/" + pg.locator(".narrator-tile a.tile-link").first.get_attribute("href") + "?tab=preview"); pg.wait_for_selector("#nr-preview-form")
    pg.locator("#nr-preview-form button[type=submit]").click()
    pg.wait_for_selector(".preview-result audio", timeout=30000)
    print("preview generated via UI; audio src:", pg.locator(".preview-result audio").first.get_attribute("src"))
    pg.screenshot(path=f"{OUT}/09-voice-preview.png", full_page=False)
    # 2) approve the selected flagged segment with the keyboard shortcut
    pg.goto(B + "/#/review/1?filter=flagged"); pg.wait_for_selector(".seg-detail")
    sid = pg.locator(".seg-title code").inner_text()
    pg.locator("body").click(position={"x": 5, "y": 300})
    pg.keyboard.press("a")
    pg.wait_for_selector(".toast-success", timeout=10000)
    print("approved via keyboard:", sid, "->", pg.locator(".toast-success").first.inner_text().split("\n")[0])
    # 3) import preview through the upload control
    pg.goto(B + "/#/library"); pg.wait_for_selector("#file-input", state="attached")
    pg.set_input_files("#file-input", sys.argv[2])
    pg.wait_for_selector("#confirm-form", timeout=15000)
    time.sleep(0.8)
    pg.screenshot(path=f"{OUT}/10-import-preview.png", full_page=True)
    print("import preview rendered")
    # dark mode + mobile
    pg2 = br.new_page(viewport={"width": 390, "height": 844}, color_scheme="dark")
    pg2.on("pageerror", lambda e: errors.append(f"pageerror(mobile): {e}"))
    pg2.goto(B + "/#/overview"); pg2.wait_for_selector(".stats"); time.sleep(1)
    pg2.screenshot(path=f"{OUT}/11-overview-mobile-dark.png", full_page=False)
    sw = pg2.evaluate("document.documentElement.scrollWidth")
    print("mobile scrollWidth", sw)
    br.close()
print("ERRORS:", errors or "none")
