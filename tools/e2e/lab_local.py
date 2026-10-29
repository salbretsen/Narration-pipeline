# usage: python lab_local.py BASE_URL OUTDIR [USER PASSWORD [FAKE_MIC_WAV]]
# Drives the Reference voice lab in Edge with a fake microphone (needs Playwright + msedge).
import sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE, OUT = sys.argv[1], Path(sys.argv[2]); OUT.mkdir(exist_ok=True, parents=True)
USER = (sys.argv[3] or None) if len(sys.argv) > 3 else None
PW = (sys.argv[4] or None) if len(sys.argv) > 4 else None
res = []


def chk(n, ok, d=""):
    res.append(ok); print(("PASS " if ok else "FAIL ") + n + (f"  [{str(d)[:200]}]" if d else ""), flush=True)


with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True, args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--use-file-for-fake-audio-capture=" + sys.argv[5]] if len(sys.argv) > 5 else ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"])
    ctx = br.new_context(viewport={"width": 1280, "height": 1000}, ignore_https_errors=True, permissions=["microphone"])
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "401" not in m.text else None)
    pg.goto(BASE + "/")
    if USER:
        pg.wait_for_selector("#login-form"); pg.fill("input[name=username]", USER); pg.fill("input[name=password]", PW); pg.click("#login-form button[type=submit]")
    pg.wait_for_selector(".stats", timeout=20000)
    pg.goto(BASE + "/#/voices"); pg.wait_for_selector("#tab-record", timeout=20000)
    tabs = pg.locator(".tabs .tab").all_inner_texts(); chk("Record tab is right of Clone tab in the Add a narrator card", tabs[-2:] == ["Clone an authorised voice", "Record a reference voice"], tabs)
    pg.click("#tab-record"); pg.wait_for_selector("#lab-h", timeout=20000)
    time.sleep(1.5)
    sec = pg.evaluate("window.isSecureContext")
    chk("page is a secure context (microphone allowed)", sec, BASE)
    pg.screenshot(path=str(OUT / "lab1-setup.png"), full_page=True)
    # step 1: microphone
    pg.click("[data-step=setup]"); pg.screenshot(path=str(OUT / "lab1-setup.png"), full_page=True)
    pg.click("[data-act=mic-on]")
    pg.wait_for_selector(".lab .meter-db:has-text('dBFS')", timeout=20000)
    levels = set()
    for _ in range(12):
        levels.add(pg.inner_text(".lab .meter-db")); time.sleep(0.3)
    chk("microphone opened through AudioWorklet (CSP allows it); live meter moves", len(levels) > 1, sorted(levels)[:5])
    pg.screenshot(path=str(OUT / "lab2-mic.png"), full_page=False)
    # step 2: room check
    pg.click("[data-step=room]"); pg.click("[data-act=room-run]")
    pg.wait_for_selector(".lab .callout:has-text('Room noise')", timeout=30000)
    chk("room check measured background noise", True, pg.inner_text(".lab .callout:has-text('Room noise')").split("\n")[0])
    pg.screenshot(path=str(OUT / "lab3-room.png"), full_page=False)
    # step 3: record
    pg.click("[data-step=record]"); pg.wait_for_selector("#lab-script")
    pg.select_option("#lab-script", "core-sounds")
    chk("teleprompter shows the chosen script", "Sixty-six sharp swallows" in pg.inner_text(".prompt-text"))
    pg.click("[data-act=rec-start]")
    pg.wait_for_selector(".rec-timer", timeout=15000)
    time.sleep(1.5); pg.screenshot(path=str(OUT / "lab4-recording.png"), full_page=False)
    time.sleep(8.5)
    t = pg.inner_text(".rec-timer")
    pg.click("[data-act=rec-stop]")
    pg.wait_for_selector(".take-preview", timeout=15000)
    chk("recording captured with a running timer", float(t.split()[0]) > 7, t)
    px = pg.evaluate("(() => { const c = document.getElementById('wave'); const d = c.getContext('2d').getImageData(0,0,c.width,c.height).data; let n=0; for (let i=3;i<d.length;i+=4) if (d[i]>0) n++; return n; })()")
    chk("waveform drawn for the take", px > 500, px)
    dur = pg.evaluate("new Promise(r => { const a = document.querySelector('.take-preview audio'); if (a.readyState >= 1) r(a.duration); else a.onloadedmetadata = () => r(a.duration); })")
    chk("local playback of the recorded WAV works", 7 < dur < 14, dur)
    pg.screenshot(path=str(OUT / "lab5-take.png"), full_page=True)
    pg.click("[data-act=rec-save]")
    pg.wait_for_selector(".lab .quality", timeout=40000)
    chk("take uploaded and analysed by the server", True, pg.inner_text(".lab .q-head").replace("\n", " "))
    pg.screenshot(path=str(OUT / "lab6-saved.png"), full_page=True)
    # step 4: review, check, audition
    pg.click("[data-step=review]"); pg.wait_for_selector(".take")
    pg.locator("[data-act=job-check]").first.click()
    pg.wait_for_selector("[data-job$=':check'] .callout", timeout=120000)
    chk("Whisper transcript check ran as a worker job", True, pg.inner_text("[data-job$=':check'] .callout")[:90].replace("\n", " "))
    pg.locator("[data-act=job-audition]").first.click()
    pg.wait_for_selector("[data-job$=':audition'] audio", timeout=240000)
    src = pg.get_attribute("[data-job$=':audition'] audio", "src")
    r = ctx.request.get(BASE + src, headers={"Range": "bytes=0-99"})
    chk("audition clip generated and streams (206)", r.status == 206, r.status)
    pg.screenshot(path=str(OUT / "lab7-review.png"), full_page=True)
    # step 5: create narrator
    pg.locator("[data-act=use-take]").first.click(); pg.wait_for_selector("#create-form")
    pg.fill("#create-form input[name=name]", "Recorded in the lab")
    pg.fill("#create-form input[name=rights_holder]", "Lab Tester (test recording)")
    pg.check("#create-form input[name=owner_attest]")
    pg.screenshot(path=str(OUT / "lab8-create.png"), full_page=False)
    pg.click("#create-form button[type=submit]")
    pg.wait_for_selector("article.voice:has-text('Recorded in the lab')", timeout=30000)
    chk("cloned narrator created from the recording and listed", True)
    names = pg.inner_text("article.voice:has-text('Recorded in the lab')")
    chk("narrator uses the Base model with consent on file", "Base-bf16" in names and "Lab Tester" in names)
    chk("no unexpected browser errors", not errs, errs[:3])
    br.close()
print("TOTAL", len(res), "PASS", sum(res))
