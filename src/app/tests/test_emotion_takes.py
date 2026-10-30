"""v0.9.0 emotion takes for cloned narrators: one take at a time, made by the clone itself (or from an adopted clip, or directed
through the CustomVoice model with the clone's speaker vector), judged by Whisper and speaker similarity, discarded or adopted.
Also: the per-emotion bank migration, narrator samples, and the "directable" clone delivery. Mock engine only: similarities are
labelled mock and are not measurements."""
from __future__ import annotations

import json
import sqlite3

import pytest

from conftest import make_wav_bytes, needs_ffmpeg
from studio import db as DB
from studio import services as SV
from studio.db import row
from test_delivery_audio import clone_book, delivery, direct, make_clone, plan_of
from test_narrative import RecordingEngine, worker_with


def refs(client, vid):
    r = client.get(f"/api/voices/{vid}/refs")
    assert r.status_code == 200, r.text
    return {x["bucket"]: x for x in r.json()["buckets"]}


def take(client, vid, bucket="anger", source="clone"):
    r = client.post(f"/api/voices/{vid}/refs/candidates", json={"bucket": bucket, "source": source})
    assert r.status_code == 200, r.text
    return r.json()["job"]


@needs_ffmpeg
def test_a_clone_makes_its_own_takes_one_at_a_time_and_adopts_them_without_new_consent(client, ctx, make_worker):
    w = make_worker()
    clone = make_clone(client)                                  # a clone of a HUMAN voice: takes made by the clone itself are allowed
    cid = clone["id"]
    info = client.get(f"/api/voices/{cid}/refs").json()
    assert info["sources"][:2] == ["clone", "intensify"] and "design" not in info["sources"]
    j = take(client, cid)
    p = j["payload"]
    assert p["voice_id"] == cid and p["source"] == "clone" and p["instruct"] is None and p["target_voice_id"] == cid
    assert "How dare you" in p["text"]                          # the emotion is in the words
    assert client.post(f"/api/voices/{cid}/refs/candidates", json={"bucket": "anger"}).status_code == 409
    assert refs(client, cid)["anger"]["pending"]["job_id"] == j["id"]
    take(client, cid, "joy")                                    # another emotion is independent
    w.run_until_idle()
    res = client.get(f"/api/jobs/{j['id']}").json()
    assert res["status"] == "completed", res.get("error")
    rid = res["result"]["recording_id"]
    b = refs(client, cid)["anger"]
    assert b["pending"] is None and [c["id"] for c in b["candidates"]] == [rid]
    c = b["candidates"][0]
    assert c["source"] == "clone" and c["mock"] is True and isinstance(c["similarity"], float) and c["asr_check"]["matches"]
    meta = SV.get_recording(ctx, rid)
    assert meta["label"].startswith("anger take 1 read by CloneTest") and meta["target_voice_id"] == cid
    # adopted without an attestation: the voice is the clone's own
    r = client.post(f"/api/voices/{cid}/refs/adopt", json={"bucket": "anger", "recording_id": rid})
    assert r.status_code == 200, r.text
    with ctx.db.session() as conn:
        ref = row(conn, "SELECT * FROM voice_refs WHERE voice_id=? AND bucket='anger'", (cid,))
    assert json.loads(ref["consent_json"])["type"] == "self" and json.loads(ref["metrics_json"])["candidate_source"] == "clone"
    # a second take numbers on, from a new seed
    j2 = take(client, cid)
    assert j2["payload"]["candidate"] == 2 and j2["payload"]["seed"] != p["seed"]


@needs_ffmpeg
def test_intensify_uses_the_adopted_clip_and_directable_reaches_the_custom_voice_model(client, ctx):
    eng = RecordingEngine()
    w = worker_with(ctx, eng)
    clone = make_clone(client)
    cid = clone["id"]
    assert client.post(f"/api/voices/{cid}/refs/candidates", json={"bucket": "fear", "source": "intensify"}).status_code == 422  # nothing adopted yet
    j = take(client, cid, "fear")
    w.run_until_idle()
    rid = client.get(f"/api/jobs/{j['id']}").json()["result"]["recording_id"]
    assert client.post(f"/api/voices/{cid}/refs/adopt", json={"bucket": "fear", "recording_id": rid}).status_code == 200
    with ctx.db.session() as conn:
        ref = row(conn, "SELECT * FROM voice_refs WHERE voice_id=? AND bucket='fear'", (cid,))
    ji = take(client, cid, "fear", "intensify")
    assert ji["payload"]["ref_override"] == ref["id"]
    eng.log.clear()
    w.run_until_idle()
    assert eng.log[-1]["ref_audio_path"] == ref["ref_audio_path"] and eng.log[-1]["ref_text"] == ref["ref_text"]
    jd = take(client, cid, "sadness", "directable")
    assert jd["payload"]["directable"] is True and jd["payload"]["instruct"] == "Speak with sadness."
    eng.log.clear()
    w.run_until_idle()
    call = eng.log[-1]
    assert call["kind"] == "clone" and call["directable_model"] == ctx.settings().models.custom_voice_model and call["instruct"] == "Speak with sadness."
    res = client.get(f"/api/jobs/{jd['id']}").json()["result"]
    assert "CustomVoice" in res["model_id"]
    # switched off in Settings: refused
    assert client.put("/api/settings", json={"models": {"directable_clones": False}}).status_code == 200
    assert client.post(f"/api/voices/{cid}/refs/candidates", json={"bucket": "joy", "source": "directable"}).status_code == 422
    assert "directable" not in client.get(f"/api/voices/{cid}/refs").json()["sources"]


@needs_ffmpeg
def test_takes_can_be_discarded_one_by_one_or_all_at_once(client, ctx, make_worker):
    w = make_worker()
    cid = make_clone(client)["id"]
    jobs = []
    for _ in range(2):
        jobs.append(take(client, cid, "grief"))
        w.run_until_idle()
    rids = [client.get(f"/api/jobs/{j['id']}").json()["result"]["recording_id"] for j in jobs]
    assert len(refs(client, cid)["grief"]["candidates"]) == 2
    other = make_clone(client, "Somebody else")
    assert client.delete(f"/api/voices/{other['id']}/refs/candidates/{rids[0]}").status_code == 404     # not this narrator's take
    r = client.delete(f"/api/voices/{cid}/refs/candidates/{rids[0]}")
    assert r.status_code == 200 and [c["id"] for c in {x["bucket"]: x for x in r.json()["buckets"]}["grief"]["candidates"]] == [rids[1]]
    assert client.get("/api/recordings").json()["recordings"] and all(m["id"] != rids[0] for m in client.get("/api/recordings").json()["recordings"])
    queued = take(client, cid, "grief")                                                               # still waiting for the worker
    r = client.delete(f"/api/voices/{cid}/refs/candidates", params={"bucket": "grief"})
    assert r.status_code == 200 and r.json()["discarded"] == 2
    g = {x["bucket"]: x for x in r.json()["buckets"]}["grief"]
    assert g["candidates"] == [] and g["pending"] is None
    assert client.get(f"/api/jobs/{queued['id']}").json()["status"] == "cancelled"
    assert client.delete(f"/api/voices/{cid}/refs/candidates", params={"bucket": "furious"}).status_code == 422
    # discarding takes is not an admin-only action (removing an adopted clip is)
    from studio.web.auth import is_admin_only
    assert not is_admin_only("DELETE", f"/api/voices/{cid}/refs/candidates/{rids[1]}") and not is_admin_only("DELETE", f"/api/voices/{cid}/refs/candidates")


@needs_ffmpeg
def test_the_similarity_gate_refuses_a_take_that_sounds_like_someone_else(client, ctx, make_worker):
    w = make_worker()
    cid = make_clone(client)["id"]
    j = take(client, cid, "joy")
    w.run_until_idle()
    rid = client.get(f"/api/jobs/{j['id']}").json()["result"]["recording_id"]
    assert client.put("/api/settings", json={"narrative": {"delivery": {"bank_min_similarity": 0.97}}}).status_code == 200
    SV.update_recording_meta(ctx, rid, similarity=0.93, engine="mlx-qwen3-tts")      # what a real measurement of another voice looks like
    r = client.post(f"/api/voices/{cid}/refs/adopt", json={"bucket": "joy", "recording_id": rid})
    assert r.status_code == 422 and "does not sound enough like" in r.json()["error"]
    SV.update_recording_meta(ctx, rid, similarity=0.99)
    assert client.post(f"/api/voices/{cid}/refs/adopt", json={"bucket": "joy", "recording_id": rid}).status_code == 200


def test_legacy_family_buckets_migrate_to_emotions(tmp_path):
    path = tmp_path / "old.db"
    d = DB.Database(path)
    with d.session() as c:  # a v6 database with the four pre-v0.9 buckets
        c.execute("PRAGMA foreign_keys = OFF")
        for version, sql in DB.MIGRATIONS:
            if version > 6:
                break
            for stmt in DB._split_sql(sql):
                c.execute(stmt)
            c.execute(f"PRAGMA user_version = {version}")
        c.execute("INSERT INTO voices(id, uid, name, kind, created_at, updated_at) VALUES (1,'u','V','clone',1,1)")
        for i, b in enumerate(("tense", "tender", "sorrow", "bright"), 1):
            c.execute("INSERT INTO voice_refs(id,voice_id,bucket,ref_audio_path,ref_text,ref_sha256,source,consent_json,created_at) VALUES (?,1,?,?,'t','x','recording','{}',1)",
                      (i, b, f"/refs/{b}.wav"))
    assert d.migrate() == (6, DB.LATEST_VERSION)
    with sqlite3.connect(path) as c:
        got = dict(c.execute("SELECT bucket, ref_audio_path FROM voice_refs").fetchall())
    assert got == {"fear": "/refs/tense.wav", "tenderness": "/refs/tender.wav", "sadness": "/refs/sorrow.wav", "joy": "/refs/bright.wav"}


@needs_ffmpeg
def test_narrator_sample_is_generated_once_and_kept(client, ctx, make_worker):
    w = make_worker()
    ryan = next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
    assert ryan["sample"] == {"status": "none"}
    assert client.get(f"/media/voice/{ryan['id']}/sample").status_code == 404
    first = client.post(f"/api/voices/{ryan['id']}/sample").json()
    assert first["status"] == "queued" and client.post(f"/api/voices/{ryan['id']}/sample").json()["job_id"] == first["job_id"]   # deduplicated
    assert next(v for v in client.get("/api/voices").json()["voices"] if v["id"] == ryan["id"])["sample"]["status"] == "queued"
    w.run_until_idle()
    st = client.get(f"/api/voices/{ryan['id']}/sample").json()
    assert st["status"] == "ready" and st["url"].startswith(f"/media/voice/{ryan['id']}/sample")
    assert client.get(f"/media/voice/{ryan['id']}/sample").status_code == 200
    assert client.post(f"/api/voices/{ryan['id']}/sample").json()["status"] == "ready"                       # just plays
    w._cleanup_previews(keep=0)                                                                               # the previews folder is pruned...
    assert client.get(f"/media/voice/{ryan['id']}/sample").status_code == 200                                 # ...the sample is not
    # a new version of the voice needs a new sample (and the old one is cleaned up when it is made)
    assert client.patch(f"/api/voices/{ryan['id']}", json={"instruct": "Brisk, bright and cheerful narration."}).status_code == 200
    assert client.get(f"/api/voices/{ryan['id']}/sample").json()["status"] == "none"
    client.post(f"/api/voices/{ryan['id']}/sample")
    w.run_until_idle()
    with ctx.db.session() as conn:
        v = row(conn, "SELECT uid FROM voices WHERE id=?", (ryan["id"],))
    files = list(SV.sample_path(ctx, v["uid"], 1).parent.glob("sample-v*.wav"))
    assert len(files) == 1


@needs_ffmpeg
def test_directable_clone_delivery_sends_the_instruction_through_the_custom_voice_model(client, ctx):
    bid, clone, toms, eng, w = clone_book(client, ctx)
    seg = toms[0]
    direct(client, seg["id"], emotion="anger", intensity=4)
    base = plan_of(ctx, seg["id"])
    assert base.directable_model is None and base.instruct is None
    delivery(client, clone="directable")
    p = plan_of(ctx, seg["id"])
    assert p.directable_model == ctx.settings().models.custom_voice_model and p.delivery_applied is True
    assert p.instruct and "angry" in p.instruct and p.gen_key != base.gen_key and "directable clone" in p.delivery_reason
    pv = client.get(f"/api/segments/{seg['id']}/delivery-preview").json()
    assert pv["applied"] is True and "Directable clone" in " ".join(pv["notes"])
    direct(client, seg["id"], emotion="anger", intensity=2)                                        # weak direction: the clone narrates as usual
    low = plan_of(ctx, seg["id"])
    assert low.directable_model is None and low.gen_key == base.gen_key and "threshold" in low.delivery_reason
    direct(client, seg["id"], emotion="anger", intensity=4)
    eng.log.clear()
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    mine = [c for c in eng.log if c["sid"] == seg["sid"]]
    assert mine and mine[-1]["directable_model"] == ctx.settings().models.custom_voice_model and "angry" in (mine[-1]["instruct"] or "")
    others = [c for c in eng.log if c["kind"] == "clone" and c["sid"] != seg["sid"]]
    assert others and all(c["directable_model"] is None for c in others)
    assert client.put("/api/settings", json={"models": {"directable_clones": False}}).status_code == 200
    off = plan_of(ctx, seg["id"])
    assert off.directable_model is None and "switched off" in off.delivery_reason


def test_cosine_and_mock_speaker_vectors(tmp_path):
    from studio.engines import MockEngine, cosine

    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0) and cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine(None, [1]) is None and cosine([1, 2], [1]) is None
    a = tmp_path / "a.wav"
    a.write_bytes(make_wav_bytes(3.0))
    sim = MockEngine().speaker_similarity(a, a, "m")
    assert sim == pytest.approx(1.0)


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="node is not installed")
def test_narrator_model_node_suite():
    import subprocess
    from pathlib import Path

    r = subprocess.run(["node", str(Path(__file__).parent / "js" / "narrator_model.test.mjs")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_narrator_pages_are_served(client):
    for p in ("/static/js/views/narrator.js", "/static/js/views/narrator-common.js", "/static/js/views/narrator-model.js", "/static/js/views/voices.js"):
        assert client.get(p).status_code == 200, p
