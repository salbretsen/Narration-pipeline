"""Direction made audible for cloned voices: pace and pauses from the Director's direction, and the emotion reference bank.
MOCK engine only (synthetic test tones, not speech, and no MLX/GPU inference): nothing here proves how a real Base model reacts to a clip."""
from __future__ import annotations

import json

import pytest

from conftest import make_speechlike_wav, make_text, make_wav_bytes, needs_ffmpeg
from studio import assembly as ASM
from studio import pipeline as PL
from studio import services as SV
from studio.config import NarrativeSettings
from studio.db import row, rows
from studio.narrative import continuity as CT
from studio.narrative import delivery as DL
from studio.narrative import refbank as RB
from studio.narrative import store as N_store
from studio.narrative import vocab as V
from studio.web.app import create_app
from studio.web.auth import AuthStore, is_admin_only
from test_narrative import RecordingEngine, all_segments, chars, import_semantic, put_settings, worker_with

TEXT = "The morning fog lay thick over the harbour, and the fishing boats rocked quietly at their moorings."


# ------------------------------------------------------------------------------------ helpers


def plan_of(ctx, segment_id: int):
    with ctx.db.session() as conn:
        seg = row(conn, "SELECT * FROM segments WHERE id=?", (segment_id,))
        book = row(conn, "SELECT * FROM books WHERE id=?", (seg["book_id"],))
        return N_store.plan_for_segment(conn, ctx, seg, book)


def delivery(client, **kw):
    r = client.put("/api/settings", json={"narrative": {"delivery": kw}})
    assert r.status_code == 200, r.text


def make_clone(client, name="CloneTest"):
    form = {"name": name, "ref_text": "This is a short sample of my own speaking voice.", "rights_holder": "Jane Author", "owner_attest": "true"}
    r = client.post("/api/voices/clone", data=form, files={"file": ("me.wav", make_wav_bytes(6.0), "audio/wav")})
    assert r.status_code == 200, r.text
    return r.json()


def clone_book(client, ctx, **narr):
    """A semantic book whose character Tom Briggs is voiced by a cloned voice; analysed (rules Director) but not yet narrated."""
    bid = import_semantic(client, director_enabled=True, **narr)["id"]
    clone = make_clone(client)
    tom = chars(client, bid)["Tom Briggs"]
    assert client.patch(f"/api/characters/{tom['id']}", json={"voice_id": clone["id"]}).status_code == 200
    client.post(f"/api/books/{bid}/analyze", json={})
    eng = RecordingEngine()
    w = worker_with(ctx, eng)
    w.run_until_idle()
    toms = [s for s in all_segments(ctx, bid) if s["character_id"] == tom["id"]]
    assert toms
    return bid, clone, toms, eng, w


def direct(client, seg_id, **fields):
    r = client.patch(f"/api/segments/{seg_id}/narrative", json=fields)
    assert r.status_code == 200, r.text


def lab_recording(client, ctx, w, ref_text=TEXT, transcript=None):
    """A lab recording with a Whisper check result stored (the mock ASR reads the sidecar transcript)."""
    r = client.post("/api/recordings", data={"script_id": "range-tense", "ref_text": ref_text}, files={"file": ("rec.wav", make_speechlike_wav(10.0), "audio/wav")})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    side = ctx.data_dir / "recordings" / rid / "reference-24k.wav.json"
    side.write_text(json.dumps({"meta": {"mock_transcript": transcript or ref_text}}))
    client.post(f"/api/recordings/{rid}/job", json={"mode": "check"})
    w.run_until_idle()
    return rid


# ------------------------------------------------------------------------------ pace mapping


def test_effective_pace_mapping_clamp_and_modes():
    f = lambda pacing: {"pacing": pacing}  # noqa: E731
    for pacing, mult in (("slow", 0.92), ("measured", 0.96), ("normal", 1.0), ("brisk", 1.04), ("fast", 1.08)):
        assert DL.effective_pace(1.0, f(pacing), "always", True)[0] == pytest.approx(mult)
    assert set(DL.PACE_FACTORS) == set(V.PACINGS)
    assert DL.effective_pace(1.2, f("slow"), "always", False)[0] == pytest.approx(1.2 * 0.92)
    assert DL.effective_pace(1.25, f("fast"), "always", False)[0] == 1.25          # clamped
    assert DL.effective_pace(0.8, f("slow"), "always", False)[0] == 0.8            # clamped
    assert DL.effective_pace(1.1, f("slow"), "off", False)[0] == 1.1               # off: the voice's own pace only
    assert DL.effective_pace(1.0, None, "always", False)[0] == 1.0                 # no direction
    assert DL.effective_pace(1.0, f("slow"), "auto", True)[0] == 1.0               # an instruction already carries the pacing
    assert DL.effective_pace(1.0, f("slow"), "auto", False)[0] == pytest.approx(0.92)
    assert DL.effective_pace(1.0, f("slow"), "always", True)[0] == pytest.approx(0.92)


@needs_ffmpeg
def test_clone_gets_pace_from_direction_and_key_changes_only_when_it_applies(client, ctx):
    bid, clone, toms, eng, w = clone_book(client, ctx)
    seg = toms[0]
    direct(client, seg["id"], pacing="slow")
    base = plan_of(ctx, seg["id"])
    assert base.pace == 1.0 and not base.delivery_applied                           # defaults: nothing changes
    delivery(client, pace_from_direction="auto")
    auto = plan_of(ctx, seg["id"])
    assert auto.pace == pytest.approx(0.92) and auto.gen_key != base.gen_key and "slow" in auto.pace_reason
    assert auto.delivery_applied is False                                             # delivery_applied still means "emotion reached the model"
    delivery(client, pace_from_direction="off")
    assert plan_of(ctx, seg["id"]).gen_key == base.gen_key                           # reverting restores the key
    # a preset voice that receives an instruction is untouched in auto mode, but follows the direction in always mode
    preset_segs = [s for s in all_segments(ctx, bid) if s["character_id"] != toms[0]["character_id"]]
    with_instruct = [s for s in preset_segs if plan_of(ctx, s["id"]).instruct]
    assert with_instruct
    off_keys = {s["id"]: plan_of(ctx, s["id"]).gen_key for s in with_instruct}
    delivery(client, pace_from_direction="auto")
    assert all(plan_of(ctx, s["id"]).gen_key == off_keys[s["id"]] for s in with_instruct)
    delivery(client, pace_from_direction="always")
    always = {s["id"]: plan_of(ctx, s["id"]) for s in with_instruct}
    assert any(p.gen_key != off_keys[i] for i, p in always.items() if (p.direction or {}).get("pacing") not in (None, "normal"))
    # generate under auto: the stretch is recorded on the attempt
    delivery(client, pace_from_direction="auto")
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    with ctx.db.session() as conn:
        a = row(conn, "SELECT pace_used, delivery_applied FROM attempts WHERE segment_id=? ORDER BY id DESC", (seg["id"],))
        assert a["pace_used"] == pytest.approx(0.92) and a["delivery_applied"] == 0
        plain = rows(conn, "SELECT pace_used FROM attempts WHERE pace_used IS NOT NULL AND segment_id<>?", (seg["id"],))
    assert all(0.8 <= r["pace_used"] <= 1.25 for r in plain)


@needs_ffmpeg
def test_voice_pace_and_direction_pace_combine_with_clamp(client, ctx):
    bid, clone, toms, eng, w = clone_book(client, ctx)
    seg = toms[0]
    delivery(client, pace_from_direction="auto")
    with ctx.db.tx() as conn:
        conn.execute("UPDATE voices SET pace=1.2 WHERE id=?", (clone["id"],))
    direct(client, seg["id"], pacing="slow")
    assert plan_of(ctx, seg["id"]).pace == pytest.approx(1.2 * 0.92, abs=1e-6)
    direct(client, seg["id"], pacing="fast")
    assert plan_of(ctx, seg["id"]).pace == 1.25


def test_calibrated_wpm_undoes_the_recorded_stretch(client, ctx):
    r = client.post("/api/imports", files={"file": ("A.txt", make_text(2, title="A").encode(), "application/octet-stream")})
    voice = next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
    b = client.post(f"/api/imports/{r.json()['token']}/confirm", json={"title": "A", "voice_id": voice["id"], "queue": False}).json()
    words = "word " * 30
    with ctx.db.tx() as conn:
        seg = row(conn, "SELECT id FROM segments WHERE book_id=?", (b["id"],))["id"]
        for i in range(12):  # natural pace 150 wpm; those takes were stretched x1.2, so they are 1/1.2 as long
            conn.execute("""INSERT INTO attempts(segment_id, attempt_no, status, engine, fingerprint, text_spoken, created_at, passed, duration_s, pace_used)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""", (seg, i + 1, "done", "mock", "f", words, 1.0, 1, 12.0 / 1.2, 1.2))
        got = PL.calibrated_wpm(conn, b["id"], 155.0)
        assert got == pytest.approx(150.0, abs=0.5)                                 # not 180: the stretch is not double counted
        conn.execute("UPDATE attempts SET pace_used=NULL, duration_s=12.0")
        assert PL.calibrated_wpm(conn, b["id"], 155.0) == pytest.approx(150.0, abs=0.5)  # NULL means unstretched


@needs_ffmpeg
def test_segment_preview_is_stretched_like_production(client, ctx):
    bid, clone, toms, eng, w = clone_book(client, ctx)
    seg = toms[0]
    direct(client, seg["id"], pacing="slow")

    def preview():
        j = client.post(f"/api/segments/{seg['id']}/preview", json={}).json()
        w.run_until_idle()
        return client.get(f"/api/jobs/{j['id']}").json()["result"]

    plain = preview()
    delivery(client, pace_from_direction="auto")
    slow = preview()
    assert slow["pace"] == pytest.approx(0.92) and slow["duration_s"] > plain["duration_s"] * 1.05
    assert plain["duration_s"] / slow["duration_s"] == pytest.approx(0.92, abs=0.02)
    assert slow["gen_key"] != plain["gen_key"]


# ------------------------------------------------------------------------------ pauses


def test_final_of_carries_a_validated_pause_hint():
    raw = {"emotion": "fear", "intensity": 3, "pacing": "slow", "tension": 2, "delivery": "x", "pause": "long"}
    sm = {k: raw[k] for k in CT.FIELDS}
    assert CT.final_of(raw, sm, {})["pause"] == "long"
    assert CT.final_of({**raw, "pause": "dramatic"}, sm, {})["pause"] == "none"
    assert CT.final_of({k: v for k, v in raw.items() if k != "pause"}, sm, {})["pause"] == "none"   # older Director output
    assert CT.final_of(raw, sm, {"pause": "beat"})["pause"] == "beat"                                  # a user override wins
    assert CT.final_of(raw, sm, {"pause": ""})["pause"] == "long"


def _book_with_audio(client, ctx, **narr):
    bid = import_semantic(client, director_enabled=True, **narr)["id"]
    client.post(f"/api/books/{bid}/analyze", json={})
    w = worker_with(ctx, RecordingEngine())
    w.run_until_idle()
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    return bid, w


def _hint(ctx, seg_id, pause):
    with ctx.db.tx() as conn:
        d = row(conn, "SELECT final_json FROM directions WHERE segment_id=?", (seg_id,))
        f = json.loads(d["final_json"])
        f["pause"] = pause
        conn.execute("UPDATE directions SET final_json=? WHERE segment_id=?", (json.dumps(f), seg_id))


@needs_ffmpeg
def test_pause_hints_extend_gaps_and_change_the_digest_only_when_enabled(client, ctx):
    bid, w = _book_with_audio(client, ctx)
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (bid,))
        ch = row(conn, "SELECT * FROM chapters WHERE book_id=? ORDER BY idx", (bid,))
        segs = rows(conn, "SELECT * FROM segments WHERE chapter_id=? ORDER BY idx", (ch["id"],))
        d0 = ASM.takes_digest(conn, book, ch["id"], ctx)
    sentence = next(s for s in segs[:-1] if s["pause_kind"] == "sentence")
    paragraph = next((s for s in segs[:-1] if s["pause_kind"] == "paragraph"), None)
    other = next((s for s in segs[:-1] if s["pause_kind"] not in ("sentence", "paragraph")), None)
    _hint(ctx, sentence["id"], "beat")
    if paragraph:
        _hint(ctx, paragraph["id"], "long")
    if other:
        _hint(ctx, other["id"], "long")
    with ctx.db.session() as conn:
        assert ASM.takes_digest(conn, book, ch["id"], ctx) == d0                    # feature off: hints are ignored, digest unchanged
        assert ASM.pause_hints(conn, ctx, book, ch["id"]) == {}
    put_settings(client, {"delivery": {"pauses_from_direction": True}})
    with ctx.db.session() as conn:
        hints = ASM.pause_hints(conn, ctx, book, ch["id"])
        d1 = ASM.takes_digest(conn, book, ch["id"], ctx)
    assert hints[sentence["sid"]] == "beat" and (not other or other["sid"] not in hints)  # only sentence/paragraph segments can carry one
    assert d1 != d0

    def gaps():
        ASM.assemble_chapter(ctx, bid, ch["id"])
        with ctx.db.session() as conn:
            m = json.loads(row(conn, "SELECT segment_map_json FROM chapter_renders WHERE chapter_id=? AND kind='wav'", (ch["id"],))["segment_map_json"])
        by = {x["sid"]: x for x in m}
        return {a["sid"]: round(b["start_s"] - a["end_s"], 3) for a, b in zip(m, m[1:])}, by

    g_on, _ = gaps()
    assert g_on[sentence["sid"]] == pytest.approx(0.35, abs=0.01)                    # max(300 ms, 350 ms)
    if paragraph:
        assert g_on[paragraph["sid"]] == pytest.approx(0.9, abs=0.01)                # max(800 ms, 900 ms)
    if other:
        base = {"scene": 1.8, "heading": 1.5, "chapter_end": 2.5}.get(other["pause_kind"])
        if base:
            assert g_on[other["sid"]] == pytest.approx(base, abs=0.01)               # long hint does not shorten or alter non-sentence gaps
    put_settings(client, {"delivery": {"pauses_from_direction": False}})
    g_off, _ = gaps()
    assert g_off[sentence["sid"]] == pytest.approx(0.3, abs=0.01)
    with ctx.db.session() as conn:
        assert ASM.takes_digest(conn, book, ch["id"], ctx) == d0


def test_beat_does_not_shorten_a_longer_paragraph_gap():
    assert max(800, ASM.PAUSE_HINT_MS["beat"]) == 800 and max(800, ASM.PAUSE_HINT_MS["long"]) == 900
    assert ASM.PAUSE_HINT_MS == {"beat": 350, "long": 900} and ASM.HINTABLE_KINDS == ("sentence", "paragraph")


# --------------------------------------------------------------------------- refbank (pure)


def test_every_emotion_maps_to_a_bucket_or_the_base_reference():
    for e in V.EMOTIONS:
        assert RB.bucket_for(e) in (None, *RB.BUCKETS), e
    assert RB.bucket_for("neutral") is None and RB.bucket_for("calm") is None
    assert RB.bucket_for("fear") == "tense" == RB.bucket_for("anger") == RB.bucket_for("anxiety")
    assert RB.bucket_for("sadness") == "sorrow" == RB.bucket_for("grief")
    assert RB.bucket_for("tenderness") == "tender" and RB.bucket_for("joy") == "tender"
    assert RB.bucket_for("excitement") == "bright"
    assert RB.bucket_for("unheard-of") is None and RB.bucket_for(None) is None
    assert set(RB.BUCKETS) == {"tense", "tender", "sorrow", "bright"}


def test_choose_rules():
    on = NarrativeSettings(delivery={"clone": "reference_bank", "bank_min_intensity": 3})
    off = NarrativeSettings()
    refs = {"tense": {"id": 7, "bucket": "tense"}}
    fear4 = {"emotion": "fear", "intensity": 4}
    assert RB.choose(on, fear4, "clone", refs)["id"] == 7
    assert RB.choose(on, {"emotion": "fear", "intensity": 3}, "clone", refs)["id"] == 7        # threshold is inclusive
    assert RB.choose(on, {"emotion": "fear", "intensity": 2}, "clone", refs) is None             # below the threshold
    assert RB.choose(off, fear4, "clone", refs) is None                                          # setting off
    assert RB.choose(on, fear4, "preset", refs) is None and RB.choose(on, fear4, "design", refs) is None
    assert RB.choose(on, {"emotion": "joy", "intensity": 5}, "clone", refs) is None             # bucket without a clip
    assert RB.choose(on, {"emotion": "neutral", "intensity": 5}, "clone", refs) is None         # base reference
    assert RB.choose(on, None, "clone", refs) is None
    assert RB.choose(NarrativeSettings(delivery={"clone": "reference_bank", "bank_min_intensity": 5}), fear4, "clone", refs) is None
    assert "no adopted reference clip" in RB.select(on, {"emotion": "joy", "intensity": 5}, "clone", refs)[1]


# --------------------------------------------------------------------- bank in the plan + end to end


@needs_ffmpeg
def test_plan_swaps_the_reference_only_when_a_clip_is_chosen(client, ctx):
    bid, clone, toms, eng, w = clone_book(client, ctx)
    seg = toms[0]
    direct(client, seg["id"], emotion="fear", intensity=4)
    base = plan_of(ctx, seg["id"])
    delivery(client, clone="reference_bank")
    no_clip = plan_of(ctx, seg["id"])
    assert no_clip.gen_key == base.gen_key and no_clip.ref_id is None                          # bank on, nothing adopted: identical key
    assert no_clip.delivery_applied is False and "no adopted reference clip" in no_clip.delivery_reason
    rid = lab_recording(client, ctx, w)
    r = client.post(f"/api/voices/{clone['id']}/refs/adopt", json={"bucket": "tense", "recording_id": rid, "owner_attest": True, "statement": "Recorded by the speaker for this project."})
    assert r.status_code == 200, r.text
    with ctx.db.session() as conn:
        ref = row(conn, "SELECT * FROM voice_refs WHERE voice_id=?", (clone["id"],))
    p = plan_of(ctx, seg["id"])
    assert p.ref_id == ref["id"] and p.ref_bucket == "tense" and p.voice.ref_audio_path == ref["ref_audio_path"] and p.voice.ref_text == ref["ref_text"]
    assert p.delivery_applied is True and "'fear' → 'tense'" in p.delivery_reason
    assert p.gen_key != base.gen_key
    delivery(client, clone="off")
    assert plan_of(ctx, seg["id"]).gen_key == base.gen_key and plan_of(ctx, seg["id"]).voice.ref_audio_path != ref["ref_audio_path"]
    delivery(client, clone="reference_bank")
    direct(client, seg["id"], emotion="fear", intensity=2)                                     # below the threshold: base reference again
    low = plan_of(ctx, seg["id"])
    assert low.ref_id is None and not low.delivery_applied and "threshold" in low.delivery_reason
    insp = client.get(f"/api/segments/{seg['id']}/inspect").json()["delivery"]
    assert insp["ref_id"] is None and "pace" in insp
    direct(client, seg["id"], emotion="fear", intensity=4)
    insp = client.get(f"/api/segments/{seg['id']}/inspect").json()["delivery"]
    assert insp["ref_id"] == ref["id"] and insp["ref_bucket"] == "tense" and insp["applied"] is True


@needs_ffmpeg
def test_clone_narrator_end_to_end_uses_the_bank_clip(client, ctx):
    bid, clone, toms, eng, w = clone_book(client, ctx, delivery={"clone": "reference_bank"})
    seg = toms[0]
    rid = lab_recording(client, ctx, w)
    assert client.post(f"/api/voices/{clone['id']}/refs/adopt", json={"bucket": "tense", "recording_id": rid, "owner_attest": True,
                                                                     "statement": "Recorded by the speaker for this project."}).status_code == 200
    direct(client, seg["id"], emotion="fear", intensity=4)
    eng.log.clear()
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    with ctx.db.session() as conn:
        ref = row(conn, "SELECT * FROM voice_refs WHERE voice_id=?", (clone["id"],))
        a = row(conn, "SELECT * FROM attempts WHERE segment_id=? ORDER BY id DESC", (seg["id"],))
        base_ref = row(conn, "SELECT ref_audio_path FROM voice_versions WHERE voice_id=?", (clone["id"],))["ref_audio_path"]
    mine = [c for c in eng.log if c["sid"] == seg["sid"]]
    assert mine and mine[-1]["ref_audio_path"] == ref["ref_audio_path"] and mine[-1]["ref_text"] == ref["ref_text"] and not mine[-1]["instruct"]
    assert a["ref_id_used"] == ref["id"] and a["delivery_applied"] == 1 and a["instruct_used"] is None
    others = [c for c in eng.log if c["kind"] == "clone" and c["sid"] != seg["sid"]]
    assert all(c["ref_audio_path"] == base_ref for c in others)                                # segments without strong direction keep the base clip


# ---------------------------------------------------------------------------- services: adopt / remove


@needs_ffmpeg
def test_adopt_refuses_unchecked_mismatched_and_unconsented_clips_and_replaces_by_bucket(client, ctx, make_worker):
    w = make_worker()
    clone = make_clone(client)
    cid = clone["id"]
    good = {"owner_attest": True, "statement": "Recorded by the speaker for this project."}
    adopt = lambda rid, bucket="tense", **kw: client.post(f"/api/voices/{cid}/refs/adopt", json={"bucket": bucket, "recording_id": rid, **kw})  # noqa: E731
    # never checked
    r = client.post("/api/recordings", data={"script_id": "range-tense", "ref_text": TEXT}, files={"file": ("rec.wav", make_speechlike_wav(10.0), "audio/wav")})
    unchecked = r.json()["id"]
    assert adopt(unchecked, **good).status_code == 422 and "Whisper check" in adopt(unchecked, **good).json()["error"]
    # checked but the transcript does not match
    bad = lab_recording(client, ctx, w, transcript="something completely different was said here today")
    res = adopt(bad, **good)
    assert res.status_code == 422 and "does not match" in json.dumps(res.json())
    # a good recording needs consent
    ok = lab_recording(client, ctx, w)
    assert adopt(ok).status_code == 422                                                         # no attestation
    assert adopt(ok, owner_attest=True, statement="short").status_code == 422                   # statement too short
    assert adopt(ok, bucket="furious", **good).status_code == 422                               # unknown bucket
    assert adopt("rec_deadbeef", **good).status_code == 404
    first = adopt(ok, **good)
    assert first.status_code == 200, first.text
    b = {x["bucket"]: x for x in first.json()["buckets"]}
    assert b["tense"]["ref"]["source"] == "recording" and b["tense"]["ref"]["duration_s"] > 3
    assert {"f0_delta_hz", "centroid_delta_hz", "gain_db", "wer"} <= set(b["tense"]["ref"]["metrics"]) and b["sorrow"]["ref"] is None
    with ctx.db.session() as conn:
        ref = row(conn, "SELECT * FROM voice_refs WHERE voice_id=?", (cid,))
        consent = json.loads(ref["consent_json"])
        base_path = row(conn, "SELECT ref_audio_path FROM voice_versions WHERE voice_id=?", (cid,))["ref_audio_path"]
    assert consent["owner_attest"] is True and "project" in consent["statement"]
    assert ref["ref_audio_path"].replace("\\", "/").endswith("/refs/tense.wav") and base_path != ref["ref_audio_path"]
    assert client.get(f"/media/voice/{cid}/ref/{ref['id']}").status_code == 200
    assert client.get(f"/media/voice/{cid}/ref/9999").status_code == 404
    # replacing a bucket keeps exactly one row and gives the new clip its own id
    ok2 = lab_recording(client, ctx, w)
    second = adopt(ok2, **good)
    assert second.status_code == 200
    with ctx.db.session() as conn:
        refs = rows(conn, "SELECT * FROM voice_refs WHERE voice_id=?", (cid,))
    assert len(refs) == 1 and refs[0]["id"] != ref["id"] and refs[0]["source_ref"] == ok2
    # presets have no bank
    preset = next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
    assert client.get(f"/api/voices/{preset['id']}/refs").status_code == 422
    assert client.post(f"/api/voices/{preset['id']}/refs/candidates", json={"bucket": "tense"}).status_code == 422
    # candidates need a designed source voice: a human clone has none
    assert client.post(f"/api/voices/{cid}/refs/candidates", json={"bucket": "tense"}).status_code == 422
    # removal deletes the row and the files
    rm = client.delete(f"/api/voices/{cid}/refs/{refs[0]['id']}")
    assert rm.status_code == 200 and all(x["ref"] is None for x in rm.json()["buckets"])
    from pathlib import Path
    assert not Path(refs[0]["ref_audio_path"]).exists()
    assert client.delete(f"/api/voices/{cid}/refs/{refs[0]['id']}").status_code == 404


@needs_ffmpeg
def test_synthetic_clone_candidates_become_labelled_lab_recordings_and_can_be_adopted(client, ctx, make_worker):
    w = make_worker()
    d = client.post("/api/voices/design", json={"name": "Gruff captain", "description": "A gruff older male voice, low and slow, with a dry edge."}).json()
    pj = client.post(f"/api/voices/{d['id']}/preview", json={"text": "The harbour lights trembled on the black water tonight.", "verify": False}).json()
    w.run_until_idle()
    clone = client.post("/api/voices/promote", json={"job_id": pj["id"], "name": "Gruff captain (stable)"}).json()
    with ctx.db.session() as conn:
        consent = json.loads(row(conn, "SELECT consent_json FROM voice_versions WHERE id=?", (clone["version"]["id"],))["consent_json"])
    assert consent["source_voice_id"] == d["id"] and consent["source_voice_name"] == "Gruff captain"
    info = client.get(f"/api/voices/{clone['id']}/refs").json()
    assert info["synthetic"] is True and info["source_voice"]["id"] == d["id"]
    # a designed source may even be archived by now
    client.post(f"/api/voices/{d['id']}/archive")
    out = client.post(f"/api/voices/{clone['id']}/refs/candidates", json={"bucket": "tense", "n": 3})
    assert out.status_code == 200, out.text
    jobs = out.json()["jobs"]
    assert len(jobs) == 3 and len({j["payload"]["seed"] for j in jobs}) == 3
    assert all(j["payload"]["to_lab"] and j["payload"]["voice_id"] == d["id"] and "tense, urgent, strained" in j["payload"]["instruct"] for j in jobs)
    w.run_until_idle()
    results = [client.get(f"/api/jobs/{j['id']}").json() for j in jobs]
    assert all(r["status"] == "completed" and r["result"]["recording_id"] for r in results), [r.get("error") for r in results]
    rid = results[0]["result"]["recording_id"]
    meta = client.get("/api/recordings").json()["recordings"]
    rec = next(m for m in meta if m["id"] == rid)
    assert rec["source"] == "synthetic" and rec["source_voice_id"] == d["id"] and rec["bucket"] == "tense"
    assert rec["engine"] == "mock" and "not speech" in rec["note"] and "[mock]" in rec["label"]       # honestly labelled
    assert rec["asr_check"]["matches"] is True and "f0_delta_hz" in rec["drift"] and "centroid_delta_hz" in rec["drift"]
    assert results[0]["result"]["note"].startswith("Mock engine")
    # adopting needs no human consent for a synthetic clone, but only for candidates this clone's own source voice made
    r = client.post(f"/api/voices/{clone['id']}/refs/adopt", json={"bucket": "tense", "recording_id": rid})
    assert r.status_code == 200, r.text
    ref = next(x for x in r.json()["buckets"] if x["bucket"] == "tense")["ref"]
    assert ref["source"] == "synthetic" and ref["mock"] is True
    human = lab_recording(client, ctx, w)
    refused = client.post(f"/api/voices/{clone['id']}/refs/adopt", json={"bucket": "tender", "recording_id": human, "owner_attest": True, "statement": "Recorded by the speaker."})
    assert refused.status_code == 422 and "synthetic" in refused.json()["error"]
    # another designed voice's candidate is refused too
    d2 = client.post("/api/voices/design", json={"name": "Soft nurse", "description": "A soft younger female voice, gentle and warm."}).json()
    other = SV.enqueue_preview(ctx, d2["id"], "x " * 20, "gentle", True, 5, {"to_lab": True, "bucket": "tender", "script_id": "range-warm", "target_voice_id": clone["id"]})
    w.run_until_idle()
    orid = client.get(f"/api/jobs/{other['id']}").json()["result"]["recording_id"]
    assert client.post(f"/api/voices/{clone['id']}/refs/adopt", json={"bucket": "tender", "recording_id": orid}).status_code == 422
    # a clone promoted before v0.7.1 only names its source in rights_holder: still found
    with ctx.db.tx() as conn:
        vv = row(conn, "SELECT id, consent_json FROM voice_versions WHERE id=?", (clone["version"]["id"],))
        c = json.loads(vv["consent_json"])
        c.pop("source_voice_id")
        c.pop("source_voice_name")
        conn.execute("UPDATE voice_versions SET consent_json=? WHERE id=?", (json.dumps(c), vv["id"]))
    assert client.get(f"/api/voices/{clone['id']}/refs").json()["source_voice"]["id"] == d["id"]


def test_removing_a_reference_clip_is_admin_only(ctx):
    assert is_admin_only("DELETE", "/api/voices/3/refs/12")
    assert not is_admin_only("POST", "/api/voices/3/refs/adopt") and not is_admin_only("POST", "/api/voices/3/refs/candidates")
    assert not is_admin_only("GET", "/api/voices/3/refs")
    from fastapi.testclient import TestClient

    store = AuthStore(ctx.config_dir / "auth.json")
    store.set_user("boss", "correct-horse-battery", "admin")
    store.set_user("clerk", "correct-horse-battery2", "operator")
    app = create_app(ctx, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:7861", headers={"X-Studio-Request": "1"}) as c:
        assert c.post("/api/auth/login", json={"username": "clerk", "password": "correct-horse-battery2"}).status_code == 200
        assert c.delete("/api/voices/1/refs/1").status_code == 403
        assert c.get("/api/voices/1/refs").status_code != 403
    with TestClient(app, base_url="http://127.0.0.1:7861", headers={"X-Studio-Request": "1"}) as c:
        assert c.post("/api/auth/login", json={"username": "boss", "password": "correct-horse-battery"}).status_code == 200
        assert c.delete("/api/voices/1/refs/1").status_code != 403


def test_disk_usage_counts_bank_clips(client, ctx, tmp_path):
    from studio import diskmgmt

    voice = next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
    before = diskmgmt.usage(ctx)["other"]["voices"]
    f = tmp_path / "clip.wav"
    f.write_bytes(b"x" * 4096)
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO voice_refs(voice_id,bucket,ref_audio_path,ref_text,ref_sha256,source,consent_json,created_at) VALUES (?,?,?,?,?,?,?,1)",
                     (voice["id"], "tense", str(f), "t", "x", "recording", "{}"))
    assert diskmgmt.usage(ctx)["other"]["voices"] == before + 4096
