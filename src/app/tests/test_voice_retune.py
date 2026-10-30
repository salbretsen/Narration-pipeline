"""Duplicate & retune voices (P2-G): copy a preset/designed voice with new delivery, retune a promoted clone from its source design
voice, derived_from lineage, description sync after an edit, constant query count of GET /api/voices. MOCK engine only."""
from __future__ import annotations

import json

import pytest

from conftest import needs_ffmpeg
from studio import services as S
from studio.db import row, rows
from studio.web.auth import AuthStore

DESC = "A gruff older male voice, low and slow, with a dry edge."
NEW_DESC = "A bright young female voice, quick and warm, with a light Irish lilt."


def _voices(client):
    return {v["name"]: v for v in client.get("/api/voices").json()["voices"]}


def _design(client, name="Gruff captain", desc=DESC):
    r = client.post("/api/voices/design", json={"name": name, "description": desc})
    assert r.status_code == 200, r.text
    return r.json()


def _promote(client, make_worker, design, name="Gruff captain (stable)"):
    w = make_worker if hasattr(make_worker, "run_until_idle") else make_worker()
    pj = client.post(f"/api/voices/{design['id']}/preview", json={"text": "The harbour lights trembled on the black water tonight.", "verify": False}).json()
    w.run_until_idle()
    r = client.post("/api/voices/promote", json={"job_id": pj["id"], "name": name})
    assert r.status_code == 200, r.text
    return r.json()


def _consent(ctx, voice):
    with ctx.db.session() as conn:
        return json.loads(row(conn, "SELECT consent_json FROM voice_versions WHERE id=?", (voice["version"]["id"],))["consent_json"])


def _versions(ctx, vid):
    with ctx.db.session() as conn:
        return rows(conn, "SELECT * FROM voice_versions WHERE voice_id=? ORDER BY version", (vid,))


# ------------------------------------------------------------------ update_voice keeps the card summary current


def test_editing_a_design_voice_updates_the_card_description(client):
    d = _design(client)
    assert d["description"] == DESC
    r = client.patch(f"/api/voices/{d['id']}", json={"instruct": NEW_DESC})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["version"]["version"] == 2 and out["version"]["instruct"] == NEW_DESC and out["description"] == NEW_DESC
    assert _voices(client)["Gruff captain"]["description"] == NEW_DESC


def test_description_sync_keeps_explicit_description_and_name_only_edits(client):
    d = _design(client)
    out = client.patch(f"/api/voices/{d['id']}", json={"instruct": NEW_DESC, "description": "My own summary"}).json()
    assert out["description"] == "My own summary" and out["version"]["version"] == 2
    out = client.patch(f"/api/voices/{d['id']}", json={"name": "Renamed"}).json()
    assert out["name"] == "Renamed" and out["version"]["version"] == 2 and out["description"] == "My own summary"
    ryan = _voices(client)["Ryan"]
    out = client.patch(f"/api/voices/{ryan['id']}", json={"instruct": "Hushed and tense."}).json()
    assert out["version"]["version"] == 2 and out["description"] == ryan["description"]   # presets keep their own summary


# ------------------------------------------------------------------ duplicate


def test_duplicate_a_builtin_preset_creates_an_editable_custom_preset(client, ctx):
    ryan = _voices(client)["Ryan"]
    r = client.post(f"/api/voices/{ryan['id']}/duplicate", json={"name": "Ryan - thriller", "instruct": "Low, tense, whispered thriller narration."})
    assert r.status_code == 200, r.text
    c = r.json()
    assert c["id"] != ryan["id"] and c["builtin"] is False and c["kind"] == "preset" and c["name"] == "Ryan - thriller"
    assert c["version"]["speaker"] == "Ryan" and c["version"]["instruct"] == "Low, tense, whispered thriller narration." and c["version"]["version"] == 1
    assert c["version"]["model_id"] == ryan["version"]["model_id"] and c["version"]["language"] == ryan["version"]["language"]
    assert c["derived_from"]["voice_id"] == ryan["id"] and c["derived_from"]["voice_name"] == "Ryan" and c["derived_from"]["version"] == 1
    assert c["derived_from"]["relation"] == "copy"
    assert _consent(ctx, c)["type"] == "built-in synthetic speaker" and _consent(ctx, c)["derived_from"] == c["derived_from"]
    # the new voice is a normal custom voice: it can be edited and archived, the built-in source cannot
    assert client.patch(f"/api/voices/{c['id']}", json={"name": "Ryan thriller 2"}).status_code == 200
    # the original is untouched
    after = _voices(client)["Ryan"]
    assert after["version"]["id"] == ryan["version"]["id"] and len(_versions(ctx, ryan["id"])) == 1 and after["derived_from"] is None
    # other speaker for a preset; unsupported speaker is refused
    r = client.post(f"/api/voices/{ryan['id']}/duplicate", json={"name": "Aiden-ish", "speaker": "Aiden"})
    assert r.status_code == 200 and r.json()["version"]["speaker"] == "Aiden" and r.json()["version"]["instruct"] == ryan["version"]["instruct"]
    assert client.post(f"/api/voices/{ryan['id']}/duplicate", json={"name": "Bad", "speaker": "Nobody"}).status_code == 422


def test_duplicate_a_design_voice_copies_the_description_and_can_override_it(client, ctx):
    d = _design(client)
    same = client.post(f"/api/voices/{d['id']}/duplicate", json={"name": "Gruff captain copy"}).json()
    assert same["version"]["instruct"] == DESC and same["description"] == DESC and same["kind"] == "design"
    assert same["version"]["model_id"] == d["version"]["model_id"]
    c = client.post(f"/api/voices/{d['id']}/duplicate", json={"name": "Bright sailor", "instruct": NEW_DESC}).json()
    assert c["version"]["instruct"] == NEW_DESC and c["description"] == NEW_DESC and c["builtin"] is False
    assert c["derived_from"] == {"relation": "copy", "voice_id": d["id"], "voice_name": "Gruff captain", "version": 1}
    assert _consent(ctx, c)["type"] == "designed synthetic voice"
    # the original keeps its description and its single version
    o = client.get(f"/api/voices/{d['id']}").json()
    assert o["version"]["instruct"] == DESC and o["description"] == DESC and len(o["versions"]) == 1 and o["derived_from"] is None
    # a copy is made from the CURRENT version of an edited source
    client.patch(f"/api/voices/{d['id']}", json={"instruct": "A second description that is long enough."})
    c3 = client.post(f"/api/voices/{d['id']}/duplicate", json={"name": "From v2"}).json()
    assert c3["version"]["instruct"] == "A second description that is long enough." and c3["derived_from"]["version"] == 2
    # validation
    assert client.post(f"/api/voices/{d['id']}/duplicate", json={"name": "Short", "instruct": "too short"}).status_code == 422
    assert client.post(f"/api/voices/{d['id']}/duplicate", json={"name": "Spk", "speaker": "Ryan"}).status_code == 422
    assert client.post(f"/api/voices/{d['id']}/duplicate", json={"name": "Extra", "bogus": 1}).status_code == 422


@needs_ffmpeg
def test_duplicate_names_must_be_unique_and_a_clone_cannot_be_duplicated(client, make_worker):
    d = _design(client)
    assert client.post(f"/api/voices/{d['id']}/duplicate", json={"name": "gruff CAPTAIN"}).status_code == 409
    assert client.post(f"/api/voices/{d['id']}/duplicate", json={"name": "   "}).status_code == 422
    assert client.post("/api/voices/99999/duplicate", json={"name": "x"}).status_code == 404
    clone = _promote(client, make_worker, d)
    r = client.post(f"/api/voices/{clone['id']}/duplicate", json={"name": "Clone copy"})
    assert r.status_code == 422 and "retune" in json.dumps(r.json()).lower()


@needs_ffmpeg
def test_duplicate_and_retune_write_audit_entries(client, ctx, make_worker):
    d = _design(client)
    c = client.post(f"/api/voices/{d['id']}/duplicate", json={"name": "Copy A"}).json()
    clone = _promote(client, make_worker, d)
    t = client.post(f"/api/voices/{clone['id']}/retune", json={"name": "Gruff captain (retune 2)"}).json()
    with ctx.db.session() as conn:
        dup = row(conn, "SELECT * FROM audit_log WHERE entity='voice' AND entity_id=? AND action='duplicate'", (str(c["id"]),))
        tun = row(conn, "SELECT * FROM audit_log WHERE entity='voice' AND entity_id=? AND action='retune'", (str(t["id"]),))
    assert dup and json.loads(dup["detail_json"])["derived_from"]["voice_id"] == d["id"]
    assert tun and json.loads(tun["detail_json"])["derived_from"]["clone_voice_id"] == clone["id"]


# ------------------------------------------------------------------ retune


@needs_ffmpeg
def test_retune_from_a_promoted_clone_creates_a_design_copy_of_the_source(client, ctx, make_worker):
    d = _design(client)
    w = make_worker()
    clone = _promote(client, w, d)
    assert clone["source_voice"] == {"id": d["id"], "name": "Gruff captain", "archived": False}
    r = client.post(f"/api/voices/{clone['id']}/retune", json={"name": "Gruff captain (retune 2)", "instruct": NEW_DESC})
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["kind"] == "design" and t["builtin"] is False and t["version"]["instruct"] == NEW_DESC and t["description"] == NEW_DESC
    assert t["source"] == {"id": d["id"], "name": "Gruff captain", "archived": False}
    df = t["derived_from"]
    assert df["relation"] == "retune" and df["clone_voice_id"] == clone["id"] and df["clone_name"] == "Gruff captain (stable)" and df["source_voice_id"] == d["id"]
    # without instruct the source's current description is copied
    t2 = client.post(f"/api/voices/{clone['id']}/retune", json={"name": "Gruff captain (retune 3)"}).json()
    assert t2["version"]["instruct"] == DESC
    # neither the source nor the clone changed
    assert len(_versions(ctx, d["id"])) == 1 and len(_versions(ctx, clone["id"])) == 1
    assert client.get(f"/api/voices/{d['id']}").json()["version"]["instruct"] == DESC
    # the new design voice can be previewed immediately and the preview completes (mock engine)
    pj = client.post(f"/api/voices/{t['id']}/preview", json={"text": "A brand new retuned sample sentence for testing.", "verify": False}).json()
    w.run_until_idle()
    done = client.get(f"/api/jobs/{pj['id']}").json()
    assert done["status"] == "completed", done


@needs_ffmpeg
def test_retune_finds_an_archived_source_and_a_legacy_clone_by_holder_name(client, ctx, make_worker):
    d = _design(client)
    clone = _promote(client, make_worker, d)
    # legacy clone (promoted before the source id was recorded): only rights_holder names the source
    with ctx.db.tx() as conn:
        c = json.loads(row(conn, "SELECT consent_json FROM voice_versions WHERE id=?", (clone["version"]["id"],))["consent_json"])
        c.pop("source_voice_id"); c.pop("source_voice_name")
        conn.execute("UPDATE voice_versions SET consent_json=? WHERE id=?", (json.dumps(c), clone["version"]["id"]))
    assert client.get(f"/api/voices/{clone['id']}").json()["source_voice"]["id"] == d["id"]
    assert client.post(f"/api/voices/{d['id']}/archive").status_code == 200   # the source may be archived
    listed = _voices(client)["Gruff captain (stable)"]
    assert listed["source_voice"] == {"id": d["id"], "name": "Gruff captain", "archived": True}
    t = client.post(f"/api/voices/{clone['id']}/retune", json={"name": "Gruff captain v2"})
    assert t.status_code == 200, t.text
    assert t.json()["source"]["archived"] is True and t.json()["version"]["instruct"] == DESC


@needs_ffmpeg
def test_retune_errors(client, ctx, make_worker):
    d = _design(client)
    ryan = _voices(client)["Ryan"]
    assert client.post(f"/api/voices/{ryan['id']}/retune", json={"name": "x"}).status_code == 422       # not a clone
    assert client.post(f"/api/voices/{d['id']}/retune", json={"name": "x"}).status_code == 422          # not a clone
    assert client.post("/api/voices/99999/retune", json={"name": "x"}).status_code == 404
    # a human-recorded clone has no source design voice
    from conftest import make_wav_bytes
    human = client.post("/api/voices/clone", data={"name": "Human", "ref_text": "This is a short sample of my own speaking voice.", "rights_holder": "Jane Author",
                                                   "owner_attest": "true"}, files={"file": ("me.wav", make_wav_bytes(6.0), "audio/wav")}).json()
    assert human["source_voice"] is None
    r = client.post(f"/api/voices/{human['id']}/retune", json={"name": "Human retune"})
    assert r.status_code == 422 and "no source design voice" in json.dumps(r.json()).lower()
    # source design voice deleted from the DB entirely -> also 422
    clone = _promote(client, make_worker, d)
    with ctx.db.tx() as conn:
        conn.execute("UPDATE voices SET kind='preset' WHERE id=?", (d["id"],))   # simulate "no design voice found"
    assert client.post(f"/api/voices/{clone['id']}/retune", json={"name": "Orphan"}).status_code == 422
    # duplicate name
    with ctx.db.tx() as conn:
        conn.execute("UPDATE voices SET kind='design' WHERE id=?", (d["id"],))
    assert client.post(f"/api/voices/{clone['id']}/retune", json={"name": "gruff captain"}).status_code == 409


# ------------------------------------------------------------------ GET /api/voices shape and cost


@needs_ffmpeg
def test_voice_list_exposes_derived_from_and_source_voice(client, make_worker):
    d = _design(client)
    c = client.post(f"/api/voices/{d['id']}/duplicate", json={"name": "Copy of captain"}).json()
    listed = _voices(client)
    assert listed["Copy of captain"]["derived_from"]["voice_id"] == d["id"]
    assert listed["Gruff captain"]["derived_from"] is None and "source_voice" not in listed["Gruff captain"]
    assert listed["Ryan"]["derived_from"] is None
    clone = _promote(client, make_worker, d)
    assert c["id"] != clone["id"] and _voices(client)[clone["name"]]["source_voice"]["id"] == d["id"]


def test_voice_list_query_count_does_not_grow_with_the_number_of_voices(client, ctx):
    def count_queries():
        n = []
        orig = ctx.db.connect

        def traced():
            conn = orig()
            conn.set_trace_callback(lambda sql: n.append(sql))
            return conn

        ctx.db.connect = traced
        try:
            out = S.list_voices(ctx)
        finally:
            ctx.db.connect = orig
        return len(n), len(out)

    q1, n1 = count_queries()
    for i in range(12):
        client.post("/api/voices/design", json={"name": f"Designed {i}", "description": f"A designed test voice number {i}, calm and clear."})
    q2, n2 = count_queries()
    assert n2 >= n1 + 12 and q2 == q1, (q1, n1, q2, n2)


# ------------------------------------------------------------------ roles


def test_operators_may_duplicate_and_retune_and_the_audit_names_them(ctx):
    from fastapi.testclient import TestClient

    from studio.web.app import create_app
    from studio.web.auth import is_admin_only

    assert not is_admin_only("POST", "/api/voices/3/duplicate") and not is_admin_only("POST", "/api/voices/3/retune")
    store = AuthStore(ctx.config_dir / "auth.json")
    store.set_user("boss", "correct-horse-battery", "admin")
    store.set_user("clerk", "correct-horse-battery2", "operator")
    app = create_app(ctx, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:7861", headers={"X-Studio-Request": "1"}) as c:
        assert c.post("/api/voices/1/duplicate", json={"name": "Anon copy"}).status_code == 401
        assert c.post("/api/auth/login", json={"username": "clerk", "password": "correct-horse-battery2"}).status_code == 200
        ryan = next(v for v in c.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
        r = c.post(f"/api/voices/{ryan['id']}/duplicate", json={"name": "Clerk copy", "instruct": "Warm and slow."})
        assert r.status_code == 200, r.text
        assert c.post(f"/api/voices/{ryan['id']}/retune", json={"name": "x"}).status_code == 422   # allowed, but Ryan is not a clone
        assert c.post(f"/api/voices/{r.json()['id']}/archive").status_code == 403                   # archiving stays admin-only
    with ctx.db.session() as conn:
        a = row(conn, "SELECT actor FROM audit_log WHERE entity='voice' AND action='duplicate'")
    assert a["actor"] == "clerk"
