"""HTTP integration tests through FastAPI's TestClient with the real worker, scheduler,
SQLite database, FFmpeg assembly and the MOCK engine (synthetic test tones - not MLX)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import CHAPTERS, make_epub, make_text, make_wav_bytes, needs_ffmpeg
from studio.db import row, rows


def upload(client, name, data):
    r = client.post("/api/imports", files={"file": (name, data, "application/octet-stream")})
    assert r.status_code == 200, r.text
    return r.json()


def confirm(client, prev, voice_id, queue=False, title=None):
    r = client.post(f"/api/imports/{prev['token']}/confirm", json={"title": title or prev["title"], "voice_id": voice_id, "queue": queue})
    assert r.status_code == 200, r.text
    return r.json()


def ryan(client):
    return next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == "Ryan")


def import_two(client):
    v = ryan(client)
    p1 = upload(client, "first.txt", make_text(2, marker="Zanzibar").encode())
    p2 = upload(client, "second.epub", make_epub(CHAPTERS))
    assert p2["totals"]["included_chapters"] == 3 and p2["format"] == "epub"
    assert any(c["skipped_count"] for c in p2["chapters"])
    b1 = confirm(client, p1, v["id"], queue=True, title="First Book")
    b2 = confirm(client, p2, v["id"], queue=True, title="Second Book")
    return b1, b2


# ---------------------------------------------------------------------------------


def test_upload_queue_reorder_and_duplicate_protection(client, ctx):
    b1, b2 = import_two(client)
    q = client.get("/api/queue").json()["jobs"]
    assert [j["book_id"] for j in q] == [b1["id"], b2["id"]]
    # duplicate enqueue returns the existing job instead of a second one
    again = client.post("/api/queue", json={"book_id": b1["id"]}).json()
    assert again["created"] is False and again["id"] == q[0]["id"]
    with ctx.db.session() as conn:
        assert row(conn, "SELECT COUNT(*) n FROM jobs WHERE book_id=? AND kind='book'", (b1["id"],))["n"] == 1
    # reorder: second book first
    client.post(f"/api/jobs/{q[1]['id']}/move", json={"direction": "top"})
    q2 = client.get("/api/queue").json()["jobs"]
    assert [j["book_id"] for j in q2] == [b2["id"], b1["id"]]
    client.post("/api/queue/reorder", json={"job_ids": [q[0]["id"], q[1]["id"]]})
    assert [j["book_id"] for j in client.get("/api/queue").json()["jobs"]] == [b1["id"], b2["id"]]
    # hold / resume / cancel an unstarted job
    jid = q[1]["id"]
    assert client.post(f"/api/jobs/{jid}/pause").json()["status"] == "paused"
    assert client.post(f"/api/jobs/{jid}/resume").json()["status"] == "queued"
    assert client.post(f"/api/jobs/{jid}/cancel").json()["status"] == "cancelled"
    assert client.post(f"/api/jobs/{jid}/retry").status_code == 409
    # idempotent import confirmation
    assert client.get(f"/api/books/{b2['id']}").json()["state"] in ("imported", "incomplete")


def test_generation_flags_retries_review_actions(client, ctx, make_worker):
    b1, b2 = import_two(client)
    w = make_worker({"text:Zanzibar": ["drop", "repeat", "drop"]})
    steps = w.run_until_idle()
    assert steps > 0
    # FIFO: book 1 finished first, the flagged segment did not block anything
    for b in (b1, b2):
        d = client.get(f"/api/books/{b['id']}").json()
        assert d["pending"] == 0 and d["produced"] == d["segments"]
    with ctx.db.session() as conn:
        flagged = rows(conn, "SELECT * FROM segments WHERE status='flagged'")
        assert len(flagged) == 1 and "Zanzibar" in flagged[0]["original_text"]
        seg = flagged[0]
        atts = rows(conn, "SELECT * FROM attempts WHERE segment_id=?", (seg["id"],))
        assert len(atts) == 3 and all(a["engine"] == "mock" for a in atts)  # capped retries, honestly labelled
        assert seg["selected_attempt_id"] in [a["id"] for a in atts]
        assert {a["status"] for a in atts} == {"done"}
        # the best take was kept
        best = min(atts, key=lambda a: a["score"])
        assert seg["selected_attempt_id"] == best["id"]
        jobs = rows(conn, "SELECT status FROM jobs WHERE kind='book'")
        assert {j["status"] for j in jobs} == {"completed"}
    ov = client.get("/api/overview").json()
    assert ov["cards"]["qa_flags"] == 1 and ov["flags"][0]["id"] == seg["id"]
    # review detail exposes original/spoken/asr/diff/attempts
    d = client.get(f"/api/segments/{seg['id']}").json()
    assert d["segment"]["original_text"] == seg["original_text"]
    assert len(d["attempts"]) == 3 and any(i["kind"] in ("dropped", "repetition") for a in d["attempts"] for i in a["issues"])
    flt = client.get(f"/api/books/{b1['id']}/segments?filter=flagged").json()
    assert flt["total"] == 1
    # select another take, then edit the spoken rendition (original stays immutable) and regenerate
    other = next(a for a in d["attempts"] if not a["selected"])
    assert client.post(f"/api/segments/{seg['id']}/select-take", json={"attempt_id": other["id"]}).status_code == 200
    r = client.post(f"/api/segments/{seg['id']}/override", json={"text": "The ship was bound for Zan-zee-bar and would not return before winter."})
    assert r.status_code == 200
    w.run_until_idle()
    d2 = client.get(f"/api/segments/{seg['id']}").json()
    assert d2["segment"]["status"] == "passed" and d2["segment"]["round"] == 2
    assert d2["segment"]["original_text"] == seg["original_text"]
    sel = next(a for a in d2["attempts"] if a["selected"])
    assert "Zan-zee-bar" in sel["text_spoken"]
    # approve with a note; persisted
    assert client.post(f"/api/segments/{seg['id']}/approve", json={"note": "checked by ear"}).status_code == 200
    d3 = client.get(f"/api/segments/{seg['id']}").json()
    assert d3["segment"]["status"] == "approved" and d3["segment"]["note"] == "checked by ear"
    assert [r["action"] for r in d3["reviews"]][:2] == ["approve", "regenerate"]
    # SSE stream emits a state event
    with client.stream("GET", "/api/events?once=1") as resp:
        body = b"".join(resp.iter_bytes()).decode()
    assert "event: state" in body and '"worker"' in body


@needs_ffmpeg
def test_assembly_export_streaming_and_release(client, ctx, make_worker):
    from studio.audio import probe_summary

    v = ryan(client)
    p = upload(client, "book.epub", make_epub(CHAPTERS))
    b = confirm(client, p, v["id"], queue=True)
    w = make_worker({"text:herons had come back": ["silence", "silence", "silence"]})
    w.run_until_idle()  # also runs the automatic draft assembly job
    book = client.get(f"/api/books/{b['id']}").json()
    assert book["flagged"] == 1 and all(c["render_current"] for c in book["chapters"])
    # release blocked while a QA flag is open
    r = client.post(f"/api/books/{b['id']}/release", json={"approved_by": "QA Lead", "listen_scope": "full", "attest": True})
    assert r.status_code == 409 and "flagged" in r.json()["error"]
    # stream a take with byte ranges
    seg = client.get(f"/api/books/{b['id']}/segments?filter=generated").json()["items"][0]
    url = f"/media/attempt/{seg['attempt_id']}"
    full = client.get(url)
    assert full.status_code == 200 and full.headers["content-type"] == "audio/wav" and full.headers["accept-ranges"] == "bytes"
    part = client.get(url, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100 and part.headers["content-range"].endswith(f"/{len(full.content)}")
    assert part.content == full.content[:100]
    assert client.get(url, headers={"Range": "bytes=99999999-"}).status_code == 416
    assert client.head(url).headers["content-length"] == str(len(full.content))
    # chapter MP3 + WAV masters
    ch = book["chapters"][0]
    mp3 = client.get(f"/media/chapter/{ch['id']}.mp3")
    assert mp3.status_code == 200 and mp3.headers["content-type"] == "audio/mpeg" and len(mp3.content) > 1000
    assert client.get(f"/media/chapter/{ch['id']}.wav", headers={"Range": "bytes=0-43"}).status_code == 206
    dl = client.get(f"/media/chapter/{ch['id']}.mp3?download=1")
    assert "attachment" in dl.headers["content-disposition"] and "DRAFT" in dl.headers["content-disposition"]
    # draft M4B export (flag still open: draft exports are allowed, clearly labelled)
    j = client.post(f"/api/books/{b['id']}/export", json={"kind": "m4b"}).json()
    w.run_until_idle()
    assert client.get(f"/api/jobs/{j['id']}").json()["status"] == "completed"
    exp = client.get(f"/api/books/{b['id']}").json()["exports"][0]
    assert exp["draft"] == 1 and "DRAFT" in exp["label"]
    with ctx.db.session() as conn:
        path = row(conn, "SELECT path FROM exports WHERE id=?", (exp["id"],))["path"]
    info = probe_summary(path)
    assert info["codec"] == "aac" and len(info["chapters"]) == 3 and info["sample_rate"] == 44100
    assert [c["title"] for c in info["chapters"]] == [c[0] for c in CHAPTERS]
    m4b = client.get(f"/media/export/{exp['id']}")
    assert m4b.status_code == 200 and m4b.headers["content-type"] == "audio/mp4"
    # resolve the flag, re-assemble, sign off, export final
    flagged = client.get(f"/api/books/{b['id']}/segments?filter=flagged").json()["items"][0]
    client.post(f"/api/segments/{flagged['id']}/approve", json={"note": "pause is intentional"})
    r = client.post(f"/api/books/{b['id']}/release", json={"approved_by": "QA Lead", "listen_scope": "full", "attest": True})
    assert r.status_code == 200 and r.json()["state"] == "approved"
    j2 = client.post(f"/api/books/{b['id']}/export", json={"kind": "chapter_m4b", "chapter_id": ch["id"]}).json()
    j3 = client.post(f"/api/books/{b['id']}/export", json={"kind": "mp3_zip"}).json()
    w.run_until_idle()
    assert client.get(f"/api/jobs/{j2['id']}").json()["status"] == "completed"
    assert client.get(f"/api/jobs/{j3['id']}").json()["status"] == "completed"
    exps = client.get(f"/api/books/{b['id']}").json()["exports"]
    assert any(e["kind"] == "chapter_m4b" and not e["draft"] for e in exps)
    # report
    rep = client.post(f"/api/books/{b['id']}/report").json()
    html = Path(rep["html"]).read_text("utf-8")
    assert "QA report" in html and "<script" not in html
    # regenerating any segment revokes the approval
    client.post(f"/api/segments/{seg['id']}/regenerate", json={})
    assert client.get(f"/api/books/{b['id']}").json()["release"] is None


@needs_ffmpeg
def test_export_blocked_when_audio_missing(client, ctx, make_worker):
    v = ryan(client)
    b = confirm(client, upload(client, "b.txt", make_text(2).encode()), v["id"], queue=False)
    j = client.post(f"/api/books/{b['id']}/export", json={"kind": "m4b"}).json()
    make_worker().run_until_idle()
    job = client.get(f"/api/jobs/{j['id']}").json()
    assert job["status"] == "failed" and "blocked" in job["error"].lower()
    assert job["result"]["blocked"] and job["result"]["blocked"][0]["problem"] == "no audio"


def test_settings_paths_and_validation(client, tmp_path):
    lib = tmp_path / "AudiobookLibrary"
    r = client.put("/api/settings", json={"storage": {"library_root": str(lib)}, "qa": {"max_attempts": 4}})
    assert r.status_code == 200, r.text
    s = r.json()
    assert Path(s["settings"]["storage"]["library_root"]) == lib.resolve() and lib.is_dir()
    assert s["settings"]["qa"]["max_attempts"] == 4
    assert Path(s["effective_paths"]["segments"]) == lib.resolve() / "segments"
    assert client.put("/api/settings", json={"storage": {"library_root": "relative/dir"}}).status_code == 422
    assert client.put("/api/settings", json={"storage": {"segments_dir": str(tmp_path / ".." / "escape")}}).status_code == 422
    assert client.put("/api/settings", json={"chunking": {"min_chars": 900}}).status_code == 422
    assert client.put("/api/settings", json={"server": {"host": "0.0.0.0"}}).status_code == 422
    v = client.post("/api/storage/validate", json={"path": str(lib)}).json()
    assert v["ok"] and v["writable"] and v["free_bytes"] > 0
    st = client.get("/api/storage").json()
    assert "per-project registry" in st["policy"]
    # books imported earlier keep their recorded paths; new ones use the new root
    voice = ryan(client)
    b = confirm(client, upload(client, "x.txt", make_text(1).encode()), voice["id"])
    assert Path(b["storage"]["segments"]).parent == lib.resolve() / "segments"


def test_voice_upload_consent_and_validation(client, ctx, make_worker):
    wav = make_wav_bytes(6.0)
    form = {"name": "Author voice", "ref_text": "This is a short sample of my own speaking voice.", "rights_holder": "Jane Author", "owner_attest": "false"}
    r = client.post("/api/voices/clone", data=form, files={"file": ("me.wav", wav, "audio/wav")})
    assert r.status_code == 422 and "permission" in r.json()["error"]
    if shutil.which("ffmpeg"):
        form["owner_attest"] = "true"
        r = client.post("/api/voices/clone", data=form, files={"file": ("me.wav", wav, "audio/wav")})
        assert r.status_code == 200, r.text
        v = r.json()
        assert v["kind"] == "clone" and v["version"]["consent"]["rights_holder"] == "Jane Author"
        assert v["version"]["model_id"].endswith("Base-bf16") and v["version"]["speaker"] is None
        assert client.get(f"/media/voice/{v['id']}/reference").status_code == 200
        # too short / silent / garbage audio rejected
        r = client.post("/api/voices/clone", data={**form, "name": "short"}, files={"file": ("s.wav", make_wav_bytes(1.0), "audio/wav")})
        assert r.status_code == 422
        r = client.post("/api/voices/clone", data={**form, "name": "silent"}, files={"file": ("s.wav", make_wav_bytes(6, silent=True), "audio/wav")})
        assert r.status_code == 422
        # previews: real job through the worker, playable + downloadable WAV
        j = client.post(f"/api/voices/{v['id']}/preview", json={"text": "Testing the cloned narrator.", "verify": True}).json()
        make_worker().run_until_idle()
        j = client.get(f"/api/jobs/{j['id']}").json()
        assert j["status"] == "completed" and j["result"]["engine"] == "mock" and "not speech" in j["result"]["note"]
        assert client.get(f"/media/preview/{j['id']}").headers["content-type"] == "audio/wav"
    form["owner_attest"] = "true"
    r = client.post("/api/voices/clone", data={**form, "name": "garbage"}, files={"file": ("g.wav", b"RIFF0000WAVEnope" + b"\0" * 64, "audio/wav")})
    assert r.status_code == 422
    r = client.post("/api/voices/clone", data={**form, "name": "exe"}, files={"file": ("g.exe", wav, "application/octet-stream")})
    assert r.status_code == 422
    client.put("/api/settings", json={"server": {"max_reference_mb": 1}})
    r = client.post("/api/voices/clone", data={**form, "name": "big"}, files={"file": ("b.wav", make_wav_bytes(40.0), "audio/wav")})
    assert r.status_code == 413
    # preset + design voices, and an invalid speaker
    assert client.post("/api/voices/preset", json={"name": "Aiden calm", "speaker": "Aiden", "instruct": "calm"}).status_code == 200
    assert client.post("/api/voices/preset", json={"name": "Bad", "speaker": "Nobody"}).status_code == 422
    d = client.post("/api/voices/design", json={"name": "Designed", "description": "A warm mature female narrator, low and steady."}).json()
    p = client.patch(f"/api/voices/{d['id']}", json={"instruct": "A bright young male narrator with energy."}).json()
    assert p["version"]["version"] == 2


def test_security_cross_origin_host_and_traversal(client, ctx, make_worker):
    assert client.post("/api/queue", json={"book_id": 1}, headers={"X-Studio-Request": ""}).status_code == 403
    assert client.post("/api/queue", json={"book_id": 1}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/queue", json={"book_id": 1}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.get("/api/overview", headers={"Host": "evil.example:7861"}).status_code == 421
    r = client.get("/api/overview")
    assert "default-src 'self'" in r.headers["content-security-policy"] and r.headers["x-frame-options"] == "DENY"
    assert client.get("/media/attempt/999999").status_code == 404
    assert client.get("/media/export/..%2F..%2Fetc%2Fpasswd").status_code in (404, 422)
    assert client.get("/media/chapter/1.exe").status_code == 422
    # a tampered DB row pointing outside the registered storage is refused
    v = ryan(client)
    b = confirm(client, upload(client, "x.txt", make_text(1).encode()), v["id"], queue=True)
    make_worker().run_until_idle()
    outside = ctx.home / "secret.txt"
    outside.write_text("secret")
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO exports(book_id, kind, label, path, draft, created_at) VALUES (?,?,?,?,1,0)", (b["id"], "m4b", "evil", str(outside)))
        eid = conn.execute("SELECT MAX(id) FROM exports").fetchone()[0]
    assert client.get(f"/media/export/{eid}").status_code == 403
    # HTML escaping of manuscript text in reports
    p = upload(client, "xss.txt", b"Chapter 1\n\n<script>alert(1)</script> is not markup. " * 3)
    assert p["chapters"][0]["sample"].startswith("<script>")  # raw text is returned as data; the UI escapes it
    # malformed manuscript does not crash the server
    assert client.post("/api/imports", files={"file": ("bad.docx", b"PK\x03\x04junk", "application/octet-stream")}).status_code == 422
    assert client.post("/api/imports", files={"file": ("bad.pdf", b"%PDF-1.4", "application/pdf")}).status_code == 422


def test_crash_recovery_restart_and_no_endless_retry(client, ctx, make_worker):
    v = ryan(client)
    b = confirm(client, upload(client, "x.txt", make_text(2).encode()), v["id"], queue=True)
    w1 = make_worker()
    for _ in range(3):
        w1.step()
    done_before = client.get(f"/api/books/{b['id']}").json()["produced"]
    assert done_before >= 2
    with ctx.db.tx() as conn:
        segs = rows(conn, "SELECT * FROM segments WHERE book_id=? AND status='pending' ORDER BY id LIMIT 2", (b["id"],))
        # segment A: interrupted mid-attempt with budget left
        conn.execute("UPDATE segments SET status='synthesizing', attempts_used=1 WHERE id=?", (segs[0]["id"],))
        conn.execute("INSERT INTO attempts(segment_id, round, attempt_no, status, engine, fingerprint, text_spoken, created_at) VALUES (?,1,1,'in_progress','mock','x','t',0)", (segs[0]["id"],))
        # segment B: interrupted on its LAST allowed attempt -> must be flagged, not retried forever
        conn.execute("UPDATE segments SET status='synthesizing', attempts_used=3 WHERE id=?", (segs[1]["id"],))
        conn.execute("UPDATE jobs SET status='running' WHERE book_id=?", (b["id"],))
    # "restart": a new worker takes over the stale lease and recovers
    w2 = make_worker(take_over=True)
    info = w2.recover()
    assert info["aborted_attempts"] == 1 and info["requeued_segments"] == 1 and info["flagged_segments"] == 1 and info["requeued_jobs"] == 1
    w2.run_until_idle()
    with ctx.db.session() as conn:
        a = row(conn, "SELECT * FROM segments WHERE id=?", (segs[0]["id"],))
        bseg = row(conn, "SELECT * FROM segments WHERE id=?", (segs[1]["id"],))
        assert a["status"] == "passed" and a["attempts_used"] == 2  # the aborted attempt counted
        assert bseg["status"] == "flagged" and bseg["attempts_used"] == 3
        n_attempts_b = row(conn, "SELECT COUNT(*) n FROM attempts WHERE segment_id=?", (segs[1]["id"],))["n"]
    assert client.get(f"/api/books/{b['id']}").json()["produced"] > done_before
    # a further restart does not re-attempt the flagged segment
    w3 = make_worker(take_over=True)
    w3.recover()
    w3.run_until_idle()
    with ctx.db.session() as conn:
        assert row(conn, "SELECT COUNT(*) n FROM attempts WHERE segment_id=?", (segs[1]["id"],))["n"] == n_attempts_b


def test_missing_storage_pauses_book_and_others_continue(client, ctx, make_worker):
    v = ryan(client)
    b1 = confirm(client, upload(client, "a.txt", make_text(1).encode()), v["id"], queue=True, title="On missing drive")
    b2 = confirm(client, upload(client, "b.txt", make_text(1).encode()), v["id"], queue=True, title="On internal disk")
    seg_dir = Path(b1["storage"]["segments"])
    shutil.rmtree(seg_dir)
    seg_dir.write_text("not a directory")  # simulates an unusable/unmounted location
    w = make_worker()
    w.run_until_idle()
    q = {j["book_id"]: j for j in client.get("/api/queue").json()["jobs"]}
    assert q[b1["id"]]["status"] == "paused" and q[b1["id"]]["paused_reason"].startswith("storage")
    assert client.get(f"/api/books/{b2['id']}").json()["pending"] == 0
    seg_dir.unlink()  # drive "reconnected"
    w.run_until_idle()
    assert client.get(f"/api/books/{b1['id']}").json()["pending"] == 0


def test_engine_failure_fails_only_that_book(client, ctx, make_worker):
    v = ryan(client)
    b1 = confirm(client, upload(client, "a.txt", make_text(1, title="Broken").encode()), v["id"], queue=True)
    b2 = confirm(client, upload(client, "b.txt", make_text(1).encode()), v["id"], queue=True)
    with ctx.db.session() as conn:
        sids = [r["sid"] for r in rows(conn, "SELECT sid FROM segments WHERE book_id=?", (b1["id"],))]
    w = make_worker({sid: ["error"] * 3 for sid in sids})
    client.put("/api/settings", json={"worker": {"max_consecutive_errors": 2}})
    w.run_until_idle()
    q = {j["book_id"]: j for j in client.get("/api/queue").json()["jobs"]}
    assert q[b1["id"]]["status"] == "failed" and "consecutive" in q[b1["id"]]["error"]
    assert client.get(f"/api/books/{b2['id']}").json()["pending"] == 0
    assert client.get(f"/api/books/{b1['id']}").json()["state"] == "failed"
    # retry after "fixing" the engine
    assert client.post(f"/api/jobs/{q[b1['id']]['id']}/retry").json()["status"] == "queued"


def test_narrator_change_requires_confirmation(client, ctx, make_worker):
    v = ryan(client)
    b = confirm(client, upload(client, "a.txt", make_text(1).encode()), v["id"], queue=True)
    make_worker().run_until_idle()
    aiden = next(x for x in client.get("/api/voices").json()["voices"] if x["name"] == "Aiden")
    r = client.post(f"/api/books/{b['id']}/narrator", json={"voice_id": aiden["id"]})
    assert r.status_code == 409 and r.json()["details"]["rendered_segments"] > 0
    r = client.post(f"/api/books/{b['id']}/narrator", json={"voice_id": aiden["id"], "confirm_invalidate": True})
    assert r.status_code == 200
    d = r.json()
    assert d["pending"] == d["segments"] and d["voice"] == "Aiden"
    # old takes are kept but can never be selected into the book
    seg = client.get(f"/api/books/{b['id']}/segments?filter=all").json()["items"][0]
    det = client.get(f"/api/segments/{seg['id']}").json()
    assert det["attempts"] and not det["attempts"][0]["fingerprint_ok"]


def test_ui_shell_and_static_assets(client):
    r = client.get("/")
    assert r.status_code == 200 and "Audiobook Studio" in r.text and "/static/js/app.js" in r.text
    for p in ("/static/app.css", "/static/js/app.js", "/static/js/ui.js", "/static/js/views/review.js"):
        assert client.get(p).status_code == 200
    assert client.get("/api/doctor").json()["checks"]
    assert client.get("/api/about").json()["engine_mode"] == "mock"


def test_about_page_data_and_version(client):
    st = client.get("/api/auth/status").json()
    assert st["version"] == "0.4.0" and st["version_label"] == "v0.4.0"
    a = client.get("/api/about").json()
    assert a["version_label"] == "v0.4.0" and "stage" not in a
    assert a["developer"] == "Scott Albretsen" and a["company"] == "TSAL Solutions LLC" and "TSAL Solutions LLC" in a["copyright"]
    assert a["licenses"] and a["qa_limitations"] and "python" in a
    assert 'id="ver"' in client.get("/").text
