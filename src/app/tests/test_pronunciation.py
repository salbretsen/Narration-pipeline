"""Pronunciation dictionary and per-voice pace (C2). MOCK engine only: synthetic tones, not speech, so
audible quality of respellings and of the time stretch is NOT tested here."""
from __future__ import annotations

import numpy as np

from conftest import make_text
from studio import audio as A
from studio import pronunciation as PR
from studio.db import row, rows
from test_integration import confirm, ryan, upload


def E(written, spoken, cs=False, ww=True):
    return {"written": written, "spoken": spoken, "case_sensitive": cs, "whole_word": ww}


# ------------------------------------------------------------------ pure logic


def test_apply_whole_word_case_and_phrases():
    ents = [E("Hermione", "Her-my-oh-nee"), E("St. Louis", "Saint Louis"), E("Nice", "Neece", cs=True), E("cat", "kat", ww=False)]
    assert PR.apply(ents, "hermione and HERMIONE") == "Her-my-oh-nee and Her-my-oh-nee"
    assert PR.apply(ents, "from St.  Louis today") == "from Saint Louis today"           # whitespace-tolerant phrase
    assert PR.apply(ents, "Nice nice") == "Neece nice"                                    # case-sensitive entry
    assert PR.apply(ents, "concatenate") == "conkatenate"                                 # not whole word
    assert PR.apply([E("cat", "kat")], "concatenate cat") == "concatenate kat"            # whole word default
    assert PR.apply([E("a", "b"), E("b", "c")], "a b") == "b c"                           # no chained rewrites
    assert PR.apply([E("Ann", "Anne"), E("Ann Lee", "An Lee")], "Ann Lee met Ann") == "An Lee met Anne"  # longest wins
    assert PR.apply([], "x") == "x"


def test_time_stretch_length_and_pitch():
    sr = 24000
    t = np.arange(sr * 3) / sr
    a = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    for rate in (0.8, 1.25):
        b = A.time_stretch(a, sr, rate)
        assert abs(len(b) / (len(a) / rate) - 1) < 0.01
        mid = b[sr: 2 * sr]
        f = np.argmax(np.abs(np.fft.rfft(mid))) * sr / len(mid)
        assert abs(f - 220) < 5                      # pitch preserved
    assert A.time_stretch(a, sr, 1.0) is not None and len(A.time_stretch(a, sr, 1.0)) == len(a)


# ------------------------------------------------------------------ API and regeneration


def book_with_audio(client, make_worker):
    b = confirm(client, upload(client, "a.txt", make_text(1).encode()), ryan(client)["id"], queue=True)
    make_worker(take_over=True).run_until_idle()
    return b


def regenerate(client, make_worker, book_id):
    assert client.post("/api/queue", json={"book_id": book_id}).status_code == 200
    make_worker(take_over=True).run_until_idle()


def seg_rows(ctx, book_id):
    with ctx.db.session() as conn:
        return rows(conn, "SELECT id, sid, status, original_text, override_text, narration_text, round FROM segments WHERE book_id=? ORDER BY id", (book_id,))


def test_crud_validation_and_try(client, ctx):
    r = client.post("/api/pronunciations", json={"written": "Eleanor", "spoken": "El-uh-nor"})
    assert r.status_code == 200, r.text
    eid = r.json()["entry"]["id"]
    assert client.post("/api/pronunciations", json={"written": "eleanor", "spoken": "x"}).status_code == 409   # duplicate
    assert client.post("/api/pronunciations", json={"written": "", "spoken": "x"}).status_code == 422
    assert client.post("/api/pronunciations", json={"written": "a", "spoken": "b", "book_id": 999}).status_code == 404
    lst = client.get("/api/pronunciations").json()
    assert [e["written"] for e in lst["global"]] == ["Eleanor"] and lst["pace_range"] == [0.8, 1.25]
    assert client.post("/api/pronunciations/try", json={"text": "Eleanor sat."}).json()["output"] == "El-uh-nor sat."
    r = client.patch(f"/api/pronunciations/{eid}", json={"enabled": False})
    assert r.status_code == 200 and r.json()["entry"]["enabled"] is False
    assert client.post("/api/pronunciations/try", json={"text": "Eleanor sat."}).json()["changed"] is False
    assert client.delete(f"/api/pronunciations/{eid}").status_code == 200
    assert client.delete(f"/api/pronunciations/{eid}").status_code == 404


def test_book_entry_overrides_global_and_scopes(client, ctx):
    b = confirm(client, upload(client, "a.txt", make_text(1).encode()), ryan(client)["id"])
    other = confirm(client, upload(client, "b.txt", make_text(1, title="Other Book").encode()), ryan(client)["id"], title="Other")
    client.post("/api/pronunciations", json={"written": "Hale", "spoken": "Hayle"})
    client.post("/api/pronunciations", json={"written": "Hale", "spoken": "Hal", "book_id": b["id"]})
    t = {"text": "Mr. Hale left.", "book_id": b["id"]}
    assert client.post("/api/pronunciations/try", json=t).json()["output"] == "Mr. Hal left."
    assert client.post("/api/pronunciations/try", json={**t, "book_id": other["id"]}).json()["output"] == "Mr. Hayle left."
    book_list = client.get(f"/api/pronunciations?book_id={b['id']}").json()
    assert len(book_list["book"]) == 1 and len(book_list["global"]) == 1


def test_dictionary_change_marks_only_affected_segments_stale(client, ctx, make_worker):
    b = book_with_audio(client, make_worker)
    before = seg_rows(ctx, b["id"])
    assert before and all(s["status"] == "passed" for s in before)
    affected = [s for s in before if "herons" in (s["narration_text"] or "")]
    assert affected and len(affected) < len(before)

    r = client.post("/api/pronunciations", json={"written": "herons", "spoken": "hair-uns", "book_id": b["id"]})
    assert r.status_code == 409 and r.json()["details"]["stale_segments"] == len(affected)
    with ctx.db.session() as conn:                                            # rolled back: nothing changed
        assert row(conn, "SELECT COUNT(*) AS n FROM pronunciations")["n"] == 0
    assert all(s["status"] == "passed" for s in seg_rows(ctx, b["id"]))

    r = client.post("/api/pronunciations", json={"written": "herons", "spoken": "hair-uns", "book_id": b["id"], "confirm_invalidate": True})
    assert r.status_code == 200 and r.json()["stale_segments"] == len(affected)
    after = {s["id"]: s for s in seg_rows(ctx, b["id"])}
    for s in before:
        a = after[s["id"]]
        if s in affected:
            assert a["status"] == "pending" and a["round"] == s["round"] + 1
        else:
            assert a["status"] == "passed" and a["round"] == s["round"]
        # the manuscript text and any override are immutable
        assert a["original_text"] == s["original_text"] and a["narration_text"] == s["narration_text"] and a["override_text"] == s["override_text"]

    regenerate(client, make_worker, b["id"])  # the engine receives the respelling
    with ctx.db.session() as conn:
        for s in affected:
            att = row(conn, "SELECT text_spoken FROM attempts WHERE segment_id=? ORDER BY id DESC LIMIT 1", (s["id"],))
            assert "hair-uns" in att["text_spoken"] and "herons" not in att["text_spoken"].lower()
    assert all(s["status"] == "passed" for s in seg_rows(ctx, b["id"]))

    # Removing the entry sends the same segments back again; the earlier take (same key) is reused, not re-rendered.
    eid = client.get(f"/api/pronunciations?book_id={b['id']}").json()["book"][0]["id"]
    assert client.delete(f"/api/pronunciations/{eid}").status_code == 409
    assert client.delete(f"/api/pronunciations/{eid}?confirm_invalidate=true").json()["stale_segments"] == len(affected)
    regenerate(client, make_worker, b["id"])
    with ctx.db.session() as conn:
        assert row(conn, "SELECT COUNT(*) AS n FROM review_actions WHERE action='cache_reuse'")["n"] >= len(affected)


def test_entry_that_matches_nothing_invalidates_nothing(client, ctx, make_worker):
    b = book_with_audio(client, make_worker)
    r = client.post("/api/pronunciations", json={"written": "Zanzibarian", "spoken": "zan-zi-bar-ee-an"})   # no confirmation needed
    assert r.status_code == 200 and r.json()["stale_segments"] == 0
    assert all(s["status"] == "passed" for s in seg_rows(ctx, b["id"]))


def test_voice_pace_validation_and_staleness(client, ctx, make_worker):
    v = ryan(client)
    assert v["pace"] == 1.0
    assert client.put(f"/api/voices/{v['id']}/pace", json={"pace": 2.0}).status_code == 422
    assert client.put(f"/api/voices/{v['id']}/pace", json={"pace": 0.5}).status_code == 422
    b = book_with_audio(client, make_worker)
    n = len(seg_rows(ctx, b["id"]))
    r = client.put(f"/api/voices/{v['id']}/pace", json={"pace": 1.1})
    assert r.status_code == 409 and r.json()["details"]["stale_segments"] == n
    r = client.put(f"/api/voices/{v['id']}/pace", json={"pace": 1.1, "confirm_invalidate": True})
    assert r.status_code == 200 and r.json()["stale_segments"] == n
    assert next(x for x in client.get("/api/voices").json()["voices"] if x["id"] == v["id"])["pace"] == 1.1
    regenerate(client, make_worker, b["id"])
    with ctx.db.session() as conn:
        done = rows(conn, "SELECT a.duration_s, a.text_spoken FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=? AND a.round>0", (b["id"],))
    assert done and all(d["duration_s"] for d in done)
    assert client.put(f"/api/voices/{v['id']}/pace", json={"pace": 1.1}).json()["stale_segments"] == 0   # unchanged: no-op
