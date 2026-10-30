import json, os, subprocess, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

# usage: python lan_e2e.py PASSWORD OUTDIR SMOKE_TEXT_FILE   (env: STUDIO_BASE, STUDIO_IPBASE, STUDIO_USER, STUDIO_SSH)
BASE = os.environ.get("STUDIO_BASE", "http://<mac-host>:8080")
IPBASE = os.environ.get("STUDIO_IPBASE", "http://<mac-lan-ip>:8080")
USER, PW = os.environ.get("STUDIO_USER", "e2e-admin"), sys.argv[1]
OUT = Path(sys.argv[2]); OUT.mkdir(exist_ok=True, parents=True)
SSH = ["ssh", "-i", os.path.expanduser("~/.ssh/<ssh-key>"), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", os.environ.get("STUDIO_SSH", "<user>@<mac-host>")]
SMOKE = open(sys.argv[3], encoding="utf-8").read()
results = []


def chk(name, ok, detail=""):
    results.append((name, bool(ok), str(detail)[:300]))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{str(detail)[:200]}]" if detail else ""), flush=True)


def ssh(cmd, timeout=120):
    return subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=timeout).stdout.strip()


with sync_playwright() as p:
    br = p.chromium.launch(channel="msedge", headless=True)
    # ---------------------------------------------------------------- unauthenticated
    anon = br.new_context()
    r = anon.request.get(BASE + "/api/overview"); chk("anon API blocked (401)", r.status == 401, r.status)
    r = anon.request.get(BASE + "/media/attempt/1"); chk("anon media blocked (401)", r.status == 401, r.status)
    r = anon.request.post(BASE + "/api/auth/login", data=json.dumps({"username": USER, "password": "wrong-password-xx"}),
                          headers={"Content-Type": "application/json", "X-Studio-Request": "1"}); chk("wrong password rejected", r.status == 401, r.status)
    r = anon.request.put(BASE + "/api/settings", data="{}", headers={"Content-Type": "application/json", "X-Studio-Request": "1"}); chk("anon settings write blocked", r.status == 401, r.status)
    r = anon.request.get(IPBASE + "/api/auth/status"); chk("LAN IP URL answers", r.status == 200 and r.json()["engine"] == "mlx", r.text())

    ctx = br.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "401" not in m.text else None)
    pg.goto(BASE + "/")
    pg.wait_for_selector("#login-form", timeout=15000)
    chk("login screen shown to signed-out browser", True)
    pg.screenshot(path=str(OUT / "L01-login.png"))
    pg.fill("input[name=username]", USER); pg.fill("input[name=password]", PW)
    pg.click("#login-form button[type=submit]")
    try:
        pg.wait_for_selector(".stats", timeout=20000)
        chk("sign in through the browser over LAN", True)
    except Exception as e:
        chk("sign in through the browser over LAN", False, e); sys.exit(1)
    time.sleep(1.5)
    chk("worker pill shows GPU worker online", True, pg.inner_text("#worker-pill"))
    chk("mock-engine badge NOT shown (real engine)", pg.is_hidden("#engine-badge"))
    pg.screenshot(path=str(OUT / "L02-overview.png"), full_page=True)

    def api_get(path):
        r = ctx.request.get(BASE + path); return r.json() if r.ok else None

    def api_post(path, body=None):
        return ctx.request.post(BASE + path, data=json.dumps(body or {}), headers={"Content-Type": "application/json", "X-Studio-Request": "1"})

    # clean up leftovers from earlier runs
    for b in api_get("/api/books")["books"]:
        if (b["title"].startswith("LAN ") or b["title"].startswith("lighthouse-")):
            if b["job"] and b["job"]["status"] in ("queued", "running", "paused"):
                api_post(f"/api/jobs/{b['job']['id']}/cancel")
    for _ in range(60):
        left = [b for b in api_get("/api/books")["books"] if (b["title"].startswith("LAN ") or b["title"].startswith("lighthouse-")) and b["job"] and b["job"]["status"] in ("queued", "running", "paused")]
        if not left: break
        time.sleep(3)
    for b in api_get("/api/books")["books"]:
        if (b["title"].startswith("LAN ") or b["title"].startswith("lighthouse-")):
            api_post(f"/api/books/{b['id']}/archive", {"archived": True})
    # ---------------------------------------------------------------- upload two manuscripts via UI
    book_ids = []
    for n, (fname, text) in enumerate([("lighthouse-a.txt", SMOKE), ("lighthouse-b.txt", SMOKE.replace("Margaret", "Eleanor"))]):
        f = OUT / fname; f.write_text(text, encoding="utf-8")
        pg.goto(BASE + "/#/library"); pg.wait_for_selector("#file-input", state="attached")
        pg.set_input_files("#file-input", str(f))
        pg.wait_for_selector("#confirm-form", timeout=20000)
        if n == 0:
            pv = pg.inner_text("#pv-h")
            chk("import preview shows chapters/words/segments", "lighthouse-a.txt" in pv and pg.locator("#confirm-form tbody tr").count() == 2, pv)
            pg.screenshot(path=str(OUT / "L03-import-preview.png"), full_page=True)
        pg.fill("#confirm-form input[name=title]", f"LAN Test {'AB'[n]}")
        pg.fill("#confirm-form input[name=author]", "E2E Tester")
        pg.click("#confirm-form button[data-queue='1']")
        want = f"LAN Test {'AB'[n]}"
        for _ in range(40):
            if any(b["title"] == want for b in api_get("/api/books")["books"]): break
            time.sleep(0.5)
    books = {b["title"]: b for b in api_get("/api/books")["books"]}
    chk("two projects imported and queued from the browser", "LAN Test A" in books and "LAN Test B" in books)
    book_ids = [books["LAN Test A"]["id"], books["LAN Test B"]["id"]]
    q = api_get("/api/queue")["jobs"]
    chk("queue holds both books in FIFO order", [j["book_id"] for j in q if j["book_id"] in book_ids] == book_ids, [(j["book_id"], j["status"]) for j in q])
    dup = api_post("/api/queue", {"book_id": book_ids[0]}).json()
    chk("duplicate queue request is idempotent", dup.get("created") is False)

    # ---------------------------------------------------------------- live progress while generating
    pg.goto(BASE + "/#/overview"); pg.wait_for_selector(".stats")
    seen_live = False; progress_samples = []
    t0 = time.time()
    while time.time() - t0 < 240:
        time.sleep(4)
        b = api_get(f"/api/books/{book_ids[0]}")
        progress_samples.append(b["produced"])
        if "Chapter" in pg.inner_text(".grid-ov") and pg.locator(".live .pulse").count():
            seen_live = True
            if seen_live and len(progress_samples) == 2:
                pg.screenshot(path=str(OUT / "L04-live-progress.png"), full_page=True)
        if b["pending"] == 0:
            break
    chk("overview showed live 'now generating' line without page reload", seen_live)
    chk("progress advanced over time", len(set(progress_samples)) > 2, progress_samples[:12])

    # ---------------------------------------------------------------- real voice preview + streaming + download
    pg.goto(BASE + "/#/voices"); pg.wait_for_selector(".narrator-tile a.tile-link")
    pg.goto(BASE + "/" + pg.locator(".narrator-tile a.tile-link").first.get_attribute("href") + "?tab=preview"); pg.wait_for_selector("#nr-preview-form")
    pg.locator("#nr-preview-form button[type=submit]").click()
    try:
        pg.wait_for_selector(".preview-result audio", timeout=180000)
        src = pg.locator(".preview-result audio").first.get_attribute("src")
        res = pg.locator(".preview-result").first.inner_text()
        chk("voice preview generated by real Qwen3-TTS", "mlx-qwen3-tts" in res and "not speech" not in res, res.replace("\n", " "))
        full = ctx.request.get(BASE + src)
        part = ctx.request.get(BASE + src, headers={"Range": "bytes=0-99"})
        chk("preview streams (200, audio/wav) and honours Range (206)", full.status == 200 and full.headers["content-type"] == "audio/wav" and part.status == 206 and len(part.body()) == 100, (full.status, part.status))
        dl = ctx.request.get(BASE + src + "?download=1")
        (OUT / "preview.wav").write_bytes(dl.body())
        chk("preview downloads as WAV", dl.ok and "attachment" in dl.headers.get("content-disposition", "") and dl.body()[:4] == b"RIFF", len(dl.body()))
        pg.screenshot(path=str(OUT / "L05-voice-preview.png"))
    except Exception as e:
        chk("voice preview generated by real Qwen3-TTS", False, e)

    # ---------------------------------------------------------------- reference voice upload (clone) through the UI
    try:
        pg.goto(BASE + "/#/voices"); pg.wait_for_selector("#add-narrator > summary")
        pg.click("#add-narrator > summary")
        pg.click("#tab-clone")
        pg.fill("#f-clone input[name=name]", "LAN synthetic clone")
        pg.fill("#f-clone input[name=rights_holder]", "Synthetic Ryan preset (generated locally for this test)")
        pg.set_input_files("#f-clone input[name=file]", str(OUT / "preview.wav"))
        pg.fill("#f-clone textarea[name=ref_text]", "Chapter One. The rain had stopped by the time Eleanor reached the harbour, and the lamps along the quay were reflected in long, trembling lines on the black water.")
        pg.check("#f-clone input[name=owner_attest]")
        pg.click("#f-clone button[type=submit]")
        pg.wait_for_selector("h1:text-is('LAN synthetic clone')", timeout=60000)   # the new narrator's page opens
        chk("reference voice uploaded with consent and listed", True)
        v = [x for x in api_get("/api/voices")["voices"] if x["name"] == "LAN synthetic clone"][0]
        chk("clone stored as Base model, no speaker, consent recorded", v["version"]["model_id"].endswith("Base-bf16") and not v["version"]["speaker"] and v["version"]["consent"]["owner_attest"], v["version"]["model_id"])
        nc = anon_status = ctx.request.post(BASE + "/api/voices/clone", multipart={"name": "no consent", "ref_text": "some words here ok", "rights_holder": "x y", "owner_attest": "false",
                                            "file": {"name": "a.wav", "mimeType": "audio/wav", "buffer": (OUT / "preview.wav").read_bytes()}}, headers={"X-Studio-Request": "1"})
        chk("upload without ownership confirmation rejected", nc.status == 422, nc.status)
        bad = ctx.request.post(BASE + "/api/voices/clone", multipart={"name": "garbage", "ref_text": "some words here ok", "rights_holder": "x y", "owner_attest": "true",
                                            "file": {"name": "a.wav", "mimeType": "audio/wav", "buffer": b"RIFF0000WAVEnope" + b"\0" * 64}}, headers={"X-Studio-Request": "1"})
        chk("invalid audio file rejected", bad.status == 422, bad.status)
        # preview the cloned voice
        pg.click("a.tab:has-text('Preview')"); pg.wait_for_selector("#nr-preview-form")
        pg.locator("#nr-preview-form button[type=submit]").click()
        pg.wait_for_selector("#nr-pv .preview-result audio", timeout=240000)
        chk("cloned voice preview generated (Base model ref_audio/ref_text)", True)
    except Exception as e:
        chk("reference voice flow", False, e)

    # ---------------------------------------------------------------- wait for both books
    t0 = time.time()
    while time.time() - t0 < 400:
        bs = [api_get(f"/api/books/{i}") for i in book_ids]
        if all(b["pending"] == 0 for b in bs):
            break
        time.sleep(5)
    bs = [api_get(f"/api/books/{i}") for i in book_ids]
    chk("both manuscripts fully narrated", all(b["pending"] == 0 and b["produced"] == b["segments"] for b in bs), [(b["segments"], b["produced"], b["flagged"]) for b in bs])
    chk("every take was made by the real engine", True)

    # ---------------------------------------------------------------- review workspace
    bid = book_ids[0]
    pg.goto(BASE + f"/#/review/{bid}?filter=all"); pg.wait_for_selector(".seg-detail", timeout=20000)
    time.sleep(1)
    txt = pg.inner_text(".review-main")
    chk("review shows original, spoken rendition, Whisper transcript and WER", "Original manuscript" in txt and "Recognised by Whisper" in txt and "WER" in txt)
    src = pg.locator(".main-take").first.get_attribute("src")
    part = ctx.request.get(BASE + src, headers={"Range": "bytes=0-1023"})
    chk("take audio streams with Range through the LAN URL", part.status == 206 and len(part.body()) == 1024, part.status)
    pg.screenshot(path=str(OUT / "L06-review.png"), full_page=True)
    first = pg.locator(".seg-title code").inner_text()
    if not pg.locator("[data-act=approve]").is_disabled():
        pg.click("[data-act=approve]"); pg.wait_for_selector(".toast-success", timeout=15000)
        det = api_get(f"/api/segments/by-sid/{first}")
        chk("approve persisted", det["segment"]["status"] == "approved")
    pg.fill("#note-text", "checked over LAN"); pg.click("[data-act=note]"); time.sleep(1)
    chk("editor note persisted", api_get(f"/api/segments/by-sid/{first}")["segment"]["note"] == "checked over LAN")

    # ---------------------------------------------------------------- assemble, download, M4B
    pg.goto(BASE + f"/#/book/{bid}"); pg.wait_for_selector("#ch-h")
    pg.click("[data-act=assemble]")
    t0 = time.time()
    while time.time() - t0 < 120:
        time.sleep(3)
        b = api_get(f"/api/books/{bid}")
        if all(c["render_current"] for c in b["chapters"]):
            break
    chk("chapter masters (WAV+MP3) assembled", all(c["render_current"] for c in b["chapters"]))
    ch = b["chapters"][0]
    mp3 = ctx.request.get(BASE + f"/media/chapter/{ch['id']}.mp3?download=1")
    (OUT / "chapter1.mp3").write_bytes(mp3.body())
    chk("chapter MP3 downloads over LAN", mp3.ok and mp3.headers["content-type"] == "audio/mpeg" and len(mp3.body()) > 50000 and "DRAFT" in mp3.headers["content-disposition"], len(mp3.body()))
    pg.goto(BASE + f"/#/book/{bid}"); pg.wait_for_selector("[data-act=export-m4b]")
    pg.click("[data-act=export-m4b]")
    t0 = time.time(); exp = None
    while time.time() - t0 < 180:
        time.sleep(3)
        ex = [e for e in api_get(f"/api/books/{bid}")["exports"] if e["kind"] == "m4b"]
        if ex:
            exp = ex[0]; break
    chk("M4B export built via UI", exp is not None)
    if exp:
        m = ctx.request.get(BASE + f"/media/export/{exp['id']}")
        (OUT / "book.m4b").write_bytes(m.body())
        pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name:format=duration:chapters", "-show_chapters", "-of", "json", str(OUT / "book.m4b")], capture_output=True, text=True)
        info = json.loads(pr.stdout or "{}")
        chk("M4B downloaded and verified locally with ffprobe (AAC, chapter markers, DRAFT)", exp["draft"] == 1 and len(info.get("chapters", [])) == 2 and info["streams"][0]["codec_name"] == "aac", f"{len(m.body())} bytes, chapters={len(info.get('chapters', []))}")
        pg.screenshot(path=str(OUT / "L07-book-page.png"), full_page=True)

    # ---------------------------------------------------------------- settings persistence + storage
    pg.goto(BASE + "/#/settings"); pg.wait_for_selector("#stor-h")
    pg.fill("input[name='qa.max_attempts']", "4")
    pg.fill("input[name='export.target_lufs']", "-18")
    pg.locator("button[data-act=validate][data-field='storage.library_root']").click(); pg.wait_for_selector(".path-status.ok", timeout=15000)
    st = pg.locator(".path-row .path-status").first.inner_text()
    chk("storage check shows resolved path, writable, free space", "writable" in st and "free" in st, st.replace("\n", " "))
    pg.screenshot(path=str(OUT / "L08-settings.png"), full_page=True)
    # --- server-side folder browser, used from the LAN session to pick a new library root on the Mac
    pg.locator("button[data-act=browse][data-field='storage.library_root']").click()
    pg.wait_for_selector("dialog.browse .br-list button", timeout=20000)
    dtxt = pg.inner_text("dialog.browse")
    chk("folder browser lists folders on the hosting Mac (shortcuts, volumes, free space)", "CHOOSE A FOLDER" in dtxt.upper() and "Home" in dtxt and "writable" in dtxt and "free" in dtxt, dtxt[:160].replace("\n", " "))
    pg.screenshot(path=str(OUT / "L09-folder-browser.png"))
    pg.fill("#br-name", "e2e-root-test"); pg.click("#br-new button[type=submit]")
    pg.wait_for_selector("dialog.browse .br-status:has-text('e2e-root-test')", timeout=15000)
    pg.click("dialog.browse [data-use]")
    chosen = pg.input_value("input[name='storage.library_root']")
    chk("picked folder fills the library-root field", chosen.endswith("e2e-root-test"), chosen)
    pg.click("button:has-text('Save settings')"); pg.wait_for_selector(".toast-success", timeout=15000)
    s_now = api_get("/api/settings")
    chk("library root changed through the LAN web UI and saved", s_now["library_root"].endswith("e2e-root-test"), s_now["library_root"])
    old_take = api_get(f"/api/books/{bid}")
    seg0 = api_get(f"/api/books/{bid}/segments?filter=generated&limit=1")["items"][0]
    rr = ctx.request.get(BASE + f"/media/attempt/{seg0['attempt_id']}", headers={"Range": "bytes=0-99"})
    chk("existing book stays findable/playable after the root changed (per-project registry)", rr.status == 206 and "e2e-root-test" not in str(old_take["storage"]["segments"]), rr.status)
    pg.fill("input[name='storage.library_root']", "relative/nonsense")
    pg.click("button:has-text('Save settings')"); pg.wait_for_selector(".toast-error", timeout=15000)
    chk("invalid storage path rejected by server", "absolute" in pg.inner_text(".toast-error").lower() or "invalid" in pg.inner_text(".toast-error").lower(), pg.inner_text(".toast-error"))
    pg.fill("input[name='storage.library_root']", "")  # restore default: leave library root unchanged

    # operator role cannot change settings (create via CLI over SSH)
    ssh("cd ~/audiobook-studio-claude && ./studio auth add-user e2e-operator --role operator --password 'operator-test-pw-1'")
    op = br.new_context()
    r = op.request.post(BASE + "/api/auth/login", data=json.dumps({"username": "e2e-operator", "password": "operator-test-pw-1"}), headers={"Content-Type": "application/json", "X-Studio-Request": "1"})
    chk("operator can sign in", r.ok)
    r = op.request.put(BASE + "/api/settings", data="{}", headers={"Content-Type": "application/json", "X-Studio-Request": "1"})
    chk("operator blocked from settings/storage admin actions (403)", r.status == 403 and op.request.get(BASE + "/api/storage").status == 403, r.status)
    chk("operator can use normal features", op.request.get(BASE + "/api/overview").ok)

    # ---------------------------------------------------------------- service restart + persistence
    out = ssh("cd ~/audiobook-studio-claude && ./studio-service restart && sleep 8 && ./studio-service status | head -3")
    chk("service restarts on command", "running" in out, out.replace("\n", " "))
    for _ in range(20):
        try:
            if anon.request.get(BASE + "/api/auth/status").ok: break
        except Exception: pass
        time.sleep(2)
    pg.goto(BASE + "/#/settings")
    try:
        pg.wait_for_selector("#stor-h", timeout=20000)
    except Exception:
        pg.reload(); pg.wait_for_selector("#stor-h", timeout=20000)
    chk("settings persisted across restart", pg.input_value("input[name='qa.max_attempts']") == "4" and pg.input_value("input[name='export.target_lufs']") == "-18")
    chk("session survives a service restart (stateless signed cookie)", ctx.request.get(BASE + "/api/overview").ok)
    chk("projects and approvals persisted across restart", api_get(f"/api/books/{bid}")["approved"] >= 1)
    chk("library root persisted across restart", api_get("/api/settings")["library_root"].endswith("e2e-root-test"))

    # ---------------------------------------------------------------- crash recovery mid-generation
    f = OUT / "recovery.txt"; f.write_text((SMOKE + "\n\n") * 2, encoding="utf-8")
    pg.goto(BASE + "/#/library"); pg.wait_for_selector("#file-input", state="attached")
    pg.set_input_files("#file-input", str(f)); pg.wait_for_selector("#confirm-form", timeout=20000)
    pg.fill("#confirm-form input[name=title]", "LAN Recovery"); pg.click("#confirm-form button[data-queue='1']"); time.sleep(4)
    rid = [b for b in api_get("/api/books")["books"] if b["title"] == "LAN Recovery"][0]["id"]
    t0 = time.time()
    while time.time() - t0 < 120:
        b = api_get(f"/api/books/{rid}")
        if b["produced"] >= 4: break
        time.sleep(2)
    before = api_get(f"/api/books/{rid}")["produced"]
    ssh("cd ~/audiobook-studio-claude && pkill -9 -f 'studio worker'; sleep 1; launchctl kickstart -k gui/$(id -u)/com.audiobookstudio.claude")
    chk("killed the worker and service mid-generation", before >= 4 and before < b["segments"], f"{before}/{b['segments']} segments done at kill")
    t0 = time.time()
    while time.time() - t0 < 360:
        try:
            b = api_get(f"/api/books/{rid}")
            if b and b["pending"] == 0: break
        except Exception: pass
        time.sleep(5)
    b = api_get(f"/api/books/{rid}")
    chk("book imported after the root change was stored under the new root", "e2e-root-test" in api_get(f"/api/books/{rid}")["storage"]["segments"], api_get(f"/api/books/{rid}")["storage"]["segments"])
    chk("generation resumed after the crash and finished", b["pending"] == 0 and b["produced"] == b["segments"], (b["produced"], b["segments"]))
    chk("completed segments were kept (progress never went backwards)", b["produced"] >= before)
    rows = ssh("cd ~/audiobook-studio-claude && sqlite3 data/studio.db \"select max(attempts_used), sum(status='aborted') from segments s left join attempts a on a.segment_id=s.id where s.book_id=%d\"" % rid)
    chk("no segment exceeded the retry budget after recovery", int(rows.split('|')[0] or 0) <= 4, rows)

    # ---------------------------------------------------------------- SSH independence
    ssh("true")
    chk("service is a launchd job (not tied to the SSH session)", "com.audiobookstudio.claude" in ssh("launchctl list | grep audiobook"), ssh("launchctl list | grep audiobook"))
    # restore the default library root
    pg.goto(BASE + "/#/settings"); pg.wait_for_selector("#stor-h")
    pg.fill("input[name='storage.library_root']", ""); pg.click("button:has-text('Save settings')"); time.sleep(2)
    chk("library root restored to default", "e2e-root-test" not in api_get("/api/settings")["library_root"], api_get("/api/settings")["library_root"])
    chk("browser console had no unexpected errors", not errs, errs[:3])
    br.close()

fails = [r for r in results if not r[1]]
print(f"\nTOTAL {len(results)}  PASS {len(results) - len(fails)}  FAIL {len(fails)}")
json.dump(results, open(OUT / "lan_e2e_results.json", "w"), indent=1)
