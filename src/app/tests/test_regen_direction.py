"""Regenerate with direction: a user's own direction (emotion / intensity / pacing / delivery note) reaches the voice even when the Director is off
or the book is a classic one, the delivery summary and the read-only preview say honestly what is sent, and the regenerate API takes an optional
direction. MOCK engine only (synthetic test tones): nothing here proves how a real model reacts to an instruction or a reference clip."""
from __future__ import annotations

import pytest

from conftest import needs_ffmpeg
from narrative_fixtures import TXT
from studio.db import row, rows, scalar
from studio.narrative import store as N_store
from test_delivery_audio import delivery, lab_recording, make_clone, plan_of
from test_narrative import RecordingEngine, all_segments, chars, import_semantic, put_settings, worker_with


def render_all(client, ctx, bid, eng=None):
    eng = eng or RecordingEngine()
    w = worker_with(ctx, eng)
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    return w, eng


def seg_row(ctx, sid):
    with ctx.db.session() as conn:
        return row(conn, "SELECT * FROM segments WHERE id=?", (sid,))


def narrator_segment(ctx, bid):
    """A narration segment (narrator voice, a preset in these books) long enough to be a normal line."""
    return next(s for s in all_segments(ctx, bid) if s["kind"] != "heading" and s["span_kind"] == "narration")


def counts(ctx):
    with ctx.db.session() as conn:
        return {t: scalar(conn, f"SELECT COUNT(*) FROM {t}", (), 0) for t in ("directions", "direction_log", "review_actions", "attempts", "jobs", "audit_log")}


# ------------------------------------------------------------------------------------------ the plan


def test_user_direction_reaches_a_preset_voice_with_the_director_off(client, ctx):
    bid = import_semantic(client)["id"]                                            # Director off (default)
    segs = all_segments(ctx, bid)
    seg = narrator_segment(ctx, bid)
    before = {s["id"]: plan_of(ctx, s["id"]) for s in segs}
    assert before[seg["id"]].instruct is None and "off for this book" in before[seg["id"]].delivery_reason
    r = client.patch(f"/api/segments/{seg['id']}/narrative", json={"emotion": "anger", "intensity": 5, "delivery": "shouting"})
    assert r.status_code == 200, r.text
    p = plan_of(ctx, seg["id"])
    assert p.instruct and "angry" in p.instruct and "shouting" in p.instruct
    assert p.delivery_applied and p.delivery_reason.startswith("direction set by you for this segment")
    assert p.gen_key != before[seg["id"]].gen_key and p.user_source
    # every other segment is untouched: same key, same (absent) instruction
    for s in segs:
        if s["id"] != seg["id"]:
            q = plan_of(ctx, s["id"])
            assert q.gen_key == before[s["id"]].gen_key and q.instruct is None and not q.user_source
    # clearing the direction restores the original key
    assert client.patch(f"/api/segments/{seg['id']}/narrative", json={"emotion": None, "intensity": None, "delivery": None}).status_code == 200
    assert plan_of(ctx, seg["id"]).gen_key == before[seg["id"]].gen_key


def test_saving_a_direction_with_the_director_off_requeues_a_rendered_segment(client, ctx):
    bid = import_semantic(client)["id"]
    render_all(client, ctx, bid)
    seg = narrator_segment(ctx, bid)
    assert seg_row(ctx, seg["id"])["status"] in ("passed", "flagged")
    round0 = seg_row(ctx, seg["id"])["round"]
    r = client.patch(f"/api/segments/{seg['id']}/narrative", json={"emotion": "fear", "intensity": 4})
    assert r.status_code == 200 and r.json()["regenerating"] is True and r.json()["status"] == "pending"
    assert seg_row(ctx, seg["id"])["round"] == round0 + 1
    # a direction that produces the same plan again does not churn the take
    w, eng = render_all(client, ctx, bid)
    assert any(c["sid"] == seg["sid"] and c["instruct"] for c in eng.log)
    r2 = client.patch(f"/api/segments/{seg['id']}/narrative", json={"emotion": "fear", "intensity": 4})
    assert r2.json()["regenerating"] is False


def test_user_direction_applies_on_a_classic_book(client, ctx):
    put_settings(client, {"semantic_segmentation": False})
    r = client.post("/api/imports", files={"file": ("flood.txt", TXT.encode(), "text/plain")})
    ryan = next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
    bid = client.post(f"/api/imports/{r.json()['token']}/confirm", json={"title": "Classic", "voice_id": ryan["id"], "queue": False}).json()["id"]
    segs = all_segments(ctx, bid)
    assert segs and all(s["src_start"] is None for s in segs)
    seg = next(s for s in segs if s["kind"] != "heading")
    base = plan_of(ctx, seg["id"])
    assert base.instruct is None
    assert client.patch(f"/api/segments/{seg['id']}/narrative", json={"emotion": "sadness", "intensity": 4}).status_code == 200
    p = plan_of(ctx, seg["id"])
    assert p.instruct and "sad" in p.instruct and p.gen_key != base.gen_key and "set by you" in p.delivery_reason


@needs_ffmpeg
def test_clone_pace_from_a_user_direction_and_honest_summary(client, ctx):
    bid = import_semantic(client)["id"]
    clone = make_clone(client)
    tom = chars(client, bid)["Tom Briggs"]
    assert client.patch(f"/api/characters/{tom['id']}", json={"voice_id": clone["id"]}).status_code == 200
    seg = next(s for s in all_segments(ctx, bid) if s["character_id"] == tom["id"])
    delivery(client, pace_from_direction="always")
    base = plan_of(ctx, seg["id"])
    assert client.patch(f"/api/segments/{seg['id']}/narrative", json={"pacing": "fast", "emotion": "anger", "intensity": 5}).status_code == 200
    p = plan_of(ctx, seg["id"])
    assert p.voice_kind == "clone" and p.pace == pytest.approx(1.08) and p.gen_key != base.gen_key
    assert p.instruct is None and p.delivery_applied is False                      # a clone takes no written instruction
    pv = client.get(f"/api/segments/{seg['id']}/delivery-preview").json()
    assert pv["voice_kind"] == "clone" and pv["applied"] is False and pv["instruct"] is None and pv["pace"] == pytest.approx(1.08)
    assert pv["source"] == "you" and pv["bank_clip"] is None and pv["bank_bucket"] == "tense" and pv["bank_enabled"] is False
    text = " ".join(pv["notes"])
    assert "cannot take written instructions" in text and "reference bank is switched off" in text and "Speed x1.08" in text
    assert pv["user_direction"]["emotion"] == "anger"


@needs_ffmpeg
def test_clone_bank_clip_is_chosen_for_a_user_direction_and_missing_clip_is_reported(client, ctx):
    bid = import_semantic(client, delivery={"clone": "reference_bank"})["id"]
    clone = make_clone(client)
    tom = chars(client, bid)["Tom Briggs"]
    assert client.patch(f"/api/characters/{tom['id']}", json={"voice_id": clone["id"]}).status_code == 200
    seg = next(s for s in all_segments(ctx, bid) if s["character_id"] == tom["id"])
    base = plan_of(ctx, seg["id"])
    assert client.patch(f"/api/segments/{seg['id']}/narrative", json={"emotion": "anger", "intensity": 5}).status_code == 200
    no_clip = plan_of(ctx, seg["id"])
    assert no_clip.ref_id is None and no_clip.delivery_applied is False and no_clip.gen_key == base.gen_key
    assert "no adopted reference clip" in no_clip.delivery_reason and no_clip.delivery_reason.startswith("direction set by you")
    pv = client.get(f"/api/segments/{seg['id']}/delivery-preview").json()
    assert pv["applied"] is False and pv["bank_enabled"] is True and pv["bank_clip"] is None
    assert "No 'tense' reference clip" in " ".join(pv["notes"]) and "differs only by chance" in " ".join(pv["notes"])
    # adopt a clip for the bucket: the user's direction now selects it
    w = worker_with(ctx, RecordingEngine())
    rid = lab_recording(client, ctx, w)
    assert client.post(f"/api/voices/{clone['id']}/refs/adopt", json={"bucket": "tense", "recording_id": rid, "owner_attest": True,
                                                                     "statement": "Recorded by the speaker for this project."}).status_code == 200
    p = plan_of(ctx, seg["id"])
    assert p.ref_id and p.ref_bucket == "tense" and p.delivery_applied is True and p.gen_key != base.gen_key
    pv = client.get(f"/api/segments/{seg['id']}/delivery-preview").json()
    assert pv["applied"] is True and pv["bank_clip"] == "tense" and "Uses the 'tense' reference clip" in " ".join(pv["notes"])


def test_segments_without_a_user_direction_keep_their_keys(client, ctx):
    """Director on and off alike: nothing changes for a segment nobody directed (the gen_key is the cache identity of every existing take)."""
    bid = import_semantic(client)["id"]
    off = {s["id"]: plan_of(ctx, s["id"]).gen_key for s in all_segments(ctx, bid)}
    client.put("/api/settings", json={"narrative": {"director_enabled": True}})
    client.post(f"/api/books/{bid}/analyze", json={})
    w = worker_with(ctx, RecordingEngine())
    w.run_until_idle()
    on = {s["id"]: plan_of(ctx, s["id"]).gen_key for s in all_segments(ctx, bid)}
    again = {s["id"]: plan_of(ctx, s["id"]).gen_key for s in all_segments(ctx, bid)}
    assert on == again and set(on) == set(off)
    with ctx.db.session() as conn:
        seg = row(conn, "SELECT * FROM segments WHERE id=?", (next(iter(on)),))
        book = row(conn, "SELECT * FROM books WHERE id=?", (bid,))
        assert N_store.plan_for_segment(conn, ctx, seg, book, user_override=None).gen_key == on[seg["id"]]
        assert N_store.plan_for_segment(conn, ctx, seg, book, user_override={}).gen_key == on[seg["id"]]


# ------------------------------------------------------------------------------------ the services / API


def test_regenerate_with_direction_end_to_end(client, ctx):
    bid = import_semantic(client)["id"]
    w, eng = render_all(client, ctx, bid)
    seg = narrator_segment(ctx, bid)
    s0 = seg_row(ctx, seg["id"])
    assert s0["status"] in ("passed", "flagged") and s0["round"] == 1
    r = client.post(f"/api/segments/{seg['id']}/regenerate", json={"note": "the character is shouting here",
                                                                    "direction": {"emotion": "anger", "intensity": 5, "delivery": "shouting"}})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "pending" and out["delivery"]["voice_kind"] == "preset" and out["delivery"]["applied"] is True
    assert "angry" in out["delivery"]["instruct"] and "shouting" in out["delivery"]["instruct"]
    assert out["delivery"]["source"] == "you" and out["delivery"]["user_direction"]["emotion"] == "anger"
    assert seg_row(ctx, seg["id"])["status"] == "pending" and seg_row(ctx, seg["id"])["round"] == 2
    eng.log.clear()
    w.run_until_idle()
    mine = [c for c in eng.log if c["sid"] == seg["sid"]]
    assert mine and "angry" in mine[-1]["instruct"] and "shouting" in mine[-1]["instruct"]
    s1 = seg_row(ctx, seg["id"])
    assert s1["status"] in ("passed", "flagged") and s1["selected_attempt_id"]
    with ctx.db.session() as conn:
        a = row(conn, "SELECT * FROM attempts WHERE id=?", (s1["selected_attempt_id"],))
        assert a["round"] == 2 and "angry" in (a["instruct_used"] or "") and a["delivery_applied"] == 1
        acts = rows(conn, "SELECT action, detail_json FROM review_actions WHERE segment_id=? ORDER BY id", (seg["id"],))
        user = row(conn, "SELECT user_json FROM directions WHERE segment_id=?", (seg["id"],))["user_json"]
    assert [x["action"] for x in acts].count("regenerate") >= 1 and any("shouting" in x["detail_json"] for x in acts if x["action"] == "regenerate")
    assert "anger" in user and "shouting" in user
    # the detail the Review page reads still carries the same fields
    det = client.get(f"/api/segments/{seg['id']}").json()
    assert {"segment", "chapter", "attempts", "reviews", "prev_id", "next_id", "chapter_timing", "max_attempts"} <= set(det)
    assert det["reviews"][0]["action"] in ("regenerate", "approve", "select-take") or det["reviews"]


def test_regenerate_with_a_direction_that_changes_nothing_still_regenerates(client, ctx):
    """The same direction twice: the second request changes no key, so the regenerate itself must do the work."""
    bid = import_semantic(client)["id"]
    w, eng = render_all(client, ctx, bid)
    seg = narrator_segment(ctx, bid)
    d = {"emotion": "fear", "intensity": 4}
    assert client.post(f"/api/segments/{seg['id']}/regenerate", json={"direction": d}).status_code == 200
    w.run_until_idle()
    r0 = seg_row(ctx, seg["id"])["round"]
    r = client.post(f"/api/segments/{seg['id']}/regenerate", json={"direction": d, "note": "again"})
    assert r.status_code == 200 and r.json()["status"] == "pending"
    assert seg_row(ctx, seg["id"])["round"] == r0 + 1


def test_plain_regenerate_still_works_and_changes_no_direction(client, ctx):
    bid = import_semantic(client)["id"]
    w, eng = render_all(client, ctx, bid)
    seg = narrator_segment(ctx, bid)
    before = counts(ctx)["directions"]
    r = client.post(f"/api/segments/{seg['id']}/regenerate", json={"note": "just again"})
    assert r.status_code == 200 and r.json()["status"] == "pending" and "delivery" not in r.json()
    assert client.post(f"/api/segments/{seg['id']}/regenerate", json={}).status_code == 200      # already pending
    assert counts(ctx)["directions"] == before
    assert client.post(f"/api/segments/{seg['id']}/regenerate", json={"direction": {"emotion": "anger"}, "surprise": 1}).status_code == 422   # extra="forbid"
    assert client.post(f"/api/segments/{seg['id']}/regenerate", json={"direction": {"mood": "angry"}}).status_code == 422


def test_direction_validation(client, ctx):
    bid = import_semantic(client)["id"]
    seg = narrator_segment(ctx, bid)
    url = f"/api/segments/{seg['id']}/regenerate"
    assert client.post(url, json={"direction": {"emotion": "furious"}}).status_code == 422
    assert client.post(url, json={"direction": {"pacing": "sprint"}}).status_code == 422
    assert client.post(url, json={"direction": {"intensity": 9}}).status_code == 422
    assert client.post(url, json={"direction": {"delivery": "x" * 241}}).status_code == 422
    with ctx.db.session() as conn:
        assert not row(conn, "SELECT 1 AS x FROM directions WHERE segment_id=?", (seg["id"],))             # nothing stored by the refused requests
    ok = client.post(url, json={"direction": {"emotion": "anger", "delivery": "  furious   and loud "}})
    assert ok.status_code == 200
    with ctx.db.session() as conn:
        import json as _json

        user = _json.loads(row(conn, "SELECT user_json FROM directions WHERE segment_id=?", (seg["id"],))["user_json"])
    assert user == {"emotion": "anger", "intensity": 3, "delivery": "furious and loud"}          # an emotion without a strength means 3


def test_delivery_preview_is_read_only_and_explains_the_inputs(client, ctx):
    bid = import_semantic(client)["id"]
    w, eng = render_all(client, ctx, bid)
    seg = narrator_segment(ctx, bid)
    key0 = plan_of(ctx, seg["id"]).gen_key
    c0 = counts(ctx)
    cur = client.get(f"/api/segments/{seg['id']}/delivery-preview")
    assert cur.status_code == 200 and cur.json()["applied"] is False and cur.json()["source"] == "none"
    r = client.get(f"/api/segments/{seg['id']}/delivery-preview", params={"emotion": "anger", "intensity": "5", "pacing": "fast", "delivery": "furious"})
    assert r.status_code == 200, r.text
    pv = r.json()
    assert pv["segment_id"] == seg["id"] and pv["voice_kind"] == "preset" and pv["applied"] is True and pv["source"] == "you"
    assert "angry" in pv["instruct"] and "furious" in pv["instruct"] and pv["direction"]["emotion"] == "anger" and pv["direction"]["intensity"] == 5
    assert set(pv) >= {"voice_kind", "voice", "model_id", "applied", "instruct", "pace", "pace_reason", "bank_clip", "bank_bucket", "bank_enabled",
                       "reason", "source", "direction", "user_direction", "notes", "segment_id"}
    assert pv["user_direction"] == {}                                                           # nothing was saved
    assert counts(ctx) == c0 and plan_of(ctx, seg["id"]).gen_key == key0 and seg_row(ctx, seg["id"])["status"] != "pending"
    assert client.get(f"/api/segments/{seg['id']}/delivery-preview", params={"emotion": "furious"}).status_code == 422
    assert client.get(f"/api/segments/{seg['id']}/delivery-preview", params={"intensity": "abc"}).status_code == 422
    assert client.get("/api/segments/99999/delivery-preview").status_code == 404
    # with a stored direction an empty parameter clears that field in the preview only
    assert client.patch(f"/api/segments/{seg['id']}/narrative", json={"emotion": "fear", "intensity": 4, "delivery": "whispering"}).status_code == 200
    stored = client.get(f"/api/segments/{seg['id']}/delivery-preview").json()
    assert stored["user_direction"] == {"emotion": "fear", "intensity": 4, "delivery": "whispering"} and "whispering" in stored["instruct"]
    cleared = client.get(f"/api/segments/{seg['id']}/delivery-preview", params={"delivery": ""}).json()
    assert "whispering" not in (cleared["instruct"] or "") and "fearful" in cleared["instruct"]
    assert client.get(f"/api/segments/{seg['id']}/delivery-preview").json()["user_direction"]["delivery"] == "whispering"


def test_regenerate_with_direction_with_the_director_on(client, ctx):
    bid = import_semantic(client, director_enabled=True)["id"]
    client.post(f"/api/books/{bid}/analyze", json={})
    w = worker_with(ctx, RecordingEngine())
    w.run_until_idle()
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    seg = narrator_segment(ctx, bid)
    r = client.post(f"/api/segments/{seg['id']}/regenerate", json={"direction": {"emotion": "anger", "intensity": 5, "delivery": "shouting"}})
    assert r.status_code == 200, r.text
    d = r.json()["delivery"]
    assert d["applied"] and "angry" in d["instruct"] and "shouting" in d["instruct"] and d["source"] == "you"
    prev = client.get(f"/api/segments/{seg['id']}/delivery-preview", params={"emotion": "joy", "intensity": "3"}).json()
    assert "joyful" in prev["instruct"] and prev["source"] == "you"
    assert seg_row(ctx, seg["id"])["status"] == "pending"
    # the Director-on cascade resets takes on its own; a job must exist for them or they would stay pending forever
    with ctx.db.session() as conn:
        assert row(conn, "SELECT id FROM jobs WHERE book_id=? AND kind='book' AND status IN ('queued','running')", (bid,))
    w.run_until_idle()
    assert not [s for s in all_segments(ctx, bid) if s["status"] in ("pending", "synthesizing")]
    assert seg_row(ctx, seg["id"])["status"] in ("passed", "flagged")


def test_segment_detail_timing_comes_from_the_chapter_map(client, ctx):
    """segment_detail finds its chapter-master timing inside SQLite now; the output must be unchanged."""
    import json as _json

    from studio import services as SV

    bid = import_semantic(client)["id"]
    segs = [s for s in all_segments(ctx, bid) if s["ch_idx"] == 1]
    entries = [{"sid": s["sid"], "start_s": round(i * 2.5, 3), "end_s": round(i * 2.5 + 2.0, 3)} for i, s in enumerate(segs)]
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO chapter_renders(chapter_id, kind, path, duration_s, segment_map_json, takes_digest, created_at) VALUES (?,?,?,?,?,?,?)",
                     (segs[0]["chapter_id"], "wav", "x.wav", 99.0, _json.dumps(entries), "d", 1.0))
    for i, s in enumerate(segs):
        assert SV.segment_detail(ctx, s["id"])["chapter_timing"] == entries[i]
    other = next(s for s in all_segments(ctx, bid) if s["ch_idx"] == 2)
    assert SV.segment_detail(ctx, other["id"])["chapter_timing"] is None
