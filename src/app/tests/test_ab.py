"""Blind A/B listening (named comparisons, rounds, votes, tally) and "apply new delivery settings to existing takes".
MOCK engine only (synthetic test tones, not speech): nothing here says anything about how real Qwen3-TTS voices sound or which setting listeners prefer."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import needs_ffmpeg
from studio import services as SV
from studio.db import row, rows
from studio.narrative import ab as AB
from studio.narrative import store as N_store
from studio.services import NeedsConfirmation, ServiceError
from studio.web.app import create_app
from studio.web.auth import AuthStore, is_admin_only
from test_delivery_audio import clone_book, delivery, direct, plan_of
from test_narrative import all_segments, import_semantic, worker_with
from test_narrator_versions import _add, _import


# ------------------------------------------------------------------------------------ helpers


def preset_book(client, ctx, **narr):
    """A semantic book narrated by the preset voice Ryan with the rules Director run (nothing rendered yet)."""
    bid = import_semantic(client, director_enabled=True, **narr)["id"]
    client.post(f"/api/books/{bid}/analyze", json={})
    w = worker_with(ctx)
    w.run_until_idle()
    return bid, w


def body_segment(ctx, bid):
    return next(s for s in all_segments(ctx, bid) if s["kind"] == "body")


def fear_segment(client, ctx, bid):
    """A body segment with a strong fear direction, so the Director's delivery visibly differs from a plain reading."""
    s = body_segment(ctx, bid)
    direct(client, s["id"], emotion="fear", intensity=4, pacing="slow")
    return s


def plans(ctx, segment_id, comparison):
    c = AB.AB_COMPARISONS[comparison]
    with ctx.db.session() as conn:
        seg = row(conn, "SELECT * FROM segments WHERE id=?", (segment_id,))
        book = row(conn, "SELECT * FROM books WHERE id=?", (seg["book_id"],))
        return N_store.plan_for_segment(conn, ctx, seg, book, ns_patch=c["a_patch"]), N_store.plan_for_segment(conn, ctx, seg, book, ns_patch=c["b_patch"])


def finish_round(ctx, w):
    w.run_until_idle()


# ------------------------------------------------------------------------------------ plan_for_segment(ns_patch)


def test_ns_patch_default_is_unchanged_and_only_whitelisted_keys_apply(client, ctx):
    bid, _w = preset_book(client, ctx)
    s = fear_segment(client, ctx, bid)
    with ctx.db.session() as conn:
        seg = row(conn, "SELECT * FROM segments WHERE id=?", (s["id"],))
        book = row(conn, "SELECT * FROM books WHERE id=?", (bid,))
        base = N_store.plan_for_segment(conn, ctx, seg, book)
        for same in (None, {}, {"semantic_segmentation": False, "max_segment_chars": 80}, {"llm": {"model": "x"}}, {"delivery": {"max_instruct_chars": 40, "custom_voice": "off"}},
                     {"director_enabled": "no"}, "garbage"):
            assert N_store.plan_for_segment(conn, ctx, seg, book, ns_patch=same).gen_key == base.gen_key, same
        off = N_store.plan_for_segment(conn, ctx, seg, book, ns_patch={"director_enabled": False})
    assert base.instruct and off.instruct is None and off.gen_key != base.gen_key
    base_ns = ctx.settings().narrative
    ns = N_store.effective_narrative_with(base_ns, {"director_enabled": True, "max_segment_chars": 900, "delivery": {"translator": "llm", "custom_voice": "off"}})
    assert ns.director_enabled is True and ns.delivery.translator == "llm"
    assert ns.max_segment_chars == base_ns.max_segment_chars and ns.delivery.custom_voice == base_ns.delivery.custom_voice   # not whitelisted: ignored
    with pytest.raises(ValidationError):                                                             # a whitelisted key still has to be valid
        N_store.effective_narrative_with(ctx.settings().narrative, {"delivery": {"bank_min_intensity": 9}})


def test_comparison_catalogue_is_explicit_and_labelled():
    assert set(AB.AB_COMPARISONS) == {"directed_vs_undirected", "llm_vs_template_translator", "bank_vs_base_clip", "pace_on_vs_off"}
    for k, c in AB.AB_COMPARISONS.items():
        assert c["a_label"] and c["b_label"] and c["a_label"] != c["b_label"] and c["title"] and c["applies"]
        assert c["a_patch"] != c["b_patch"] and set(c["kinds"]) <= {"preset", "design", "clone"}, k
    assert [x["id"] for x in AB.catalog()] == list(AB.AB_COMPARISONS)


# ------------------------------------------------------------------------------------ the pair: refusals


def test_directed_vs_undirected_differs_on_a_preset_voice_with_a_direction(client, ctx):
    bid, _w = preset_book(client, ctx)
    s = fear_segment(client, ctx, bid)
    a, b = plans(ctx, s["id"], "directed_vs_undirected")
    assert a.voice_kind == "preset" and a.instruct and b.instruct is None and a.gen_key != b.gen_key
    assert a.text == b.text                                                                           # only the delivery differs
    # the patch is explicit: the pair is the same whether or not the book has the Director switched on
    client.put("/api/settings", json={"narrative": {"director_enabled": False}})
    a2, b2 = plans(ctx, s["id"], "directed_vs_undirected")
    assert (a2.gen_key, b2.gen_key) == (a.gen_key, b.gen_key)


def test_pace_comparison_differs_when_the_direction_asks_for_a_pace(client, ctx):
    bid, _w = preset_book(client, ctx)
    s = fear_segment(client, ctx, bid)
    a, b = plans(ctx, s["id"], "pace_on_vs_off")
    assert a.pace < 1.0 and b.pace == 1.0 and a.instruct == b.instruct and a.gen_key != b.gen_key


def test_identical_pair_is_refused(client, ctx):
    bid, _w = preset_book(client, ctx)
    s = body_segment(ctx, bid)
    with ctx.db.tx() as conn:                                                                          # no direction at all: nothing for the Director to add
        conn.execute("DELETE FROM directions WHERE segment_id=?", (s["id"],))
    a, b = plans(ctx, s["id"], "directed_vs_undirected")
    assert a.gen_key == b.gen_key
    with pytest.raises(ServiceError) as e:
        SV.enqueue_ab(ctx, s["id"], "directed_vs_undirected")
    assert e.value.status == 409 and "same audio" in str(e.value)
    with ctx.db.session() as conn:
        assert rows(conn, "SELECT id FROM jobs WHERE kind='preview'") == []                          # nothing was queued
    # pace comparison on a segment whose direction is normal pace: identical too
    n = fear_segment(client, ctx, bid)
    direct(client, n["id"], pacing="normal")
    with pytest.raises(ServiceError) as e:
        SV.enqueue_ab(ctx, n["id"], "pace_on_vs_off")
    assert e.value.status == 409


def test_inapplicable_comparison_is_refused(client, ctx):
    bid, _w = preset_book(client, ctx)
    s = fear_segment(client, ctx, bid)
    with pytest.raises(ServiceError) as e:
        SV.enqueue_ab(ctx, s["id"], "bank_vs_base_clip")
    assert e.value.status == 409 and "preset" in str(e.value)
    with pytest.raises(ServiceError) as e:
        SV.enqueue_ab(ctx, s["id"], "no_such_comparison")
    assert e.value.status == 422
    with pytest.raises(ServiceError) as e:
        SV.enqueue_ab(ctx, 99999, "pace_on_vs_off")
    assert e.value.status == 404


# ------------------------------------------------------------------------------------ rounds


def test_enqueue_creates_two_preview_jobs_with_one_seed_and_the_right_patches(client, ctx):
    bid, _w = preset_book(client, ctx)
    s = fear_segment(client, ctx, bid)
    r = SV.enqueue_ab(ctx, s["id"], "directed_vs_undirected", swap=False)
    assert set(r) >= {"round_id", "jobs"} and len(r["jobs"]) == 2
    jobs = [SV.get_job(ctx, j) for j in r["jobs"]]
    assert all(j["kind"] == "preview" and j["status"] == "queued" and j["book_id"] == bid for j in jobs)
    p0, p1 = jobs[0]["payload"], jobs[1]["payload"]
    assert p0["segment_id"] == p1["segment_id"] == s["id"] and p0["seed"] == p1["seed"]
    c = AB.AB_COMPARISONS["directed_vs_undirected"]
    assert p0["ns_patch"] == c["a_patch"] and p1["ns_patch"] == c["b_patch"]                           # swap=False: x is a, y is b
    assert [p["ab"]["slot"] for p in (p0, p1)] == ["x", "y"] and [p["ab"]["side"] for p in (p0, p1)] == ["a", "b"]
    assert p0["ab"]["round_id"] == p1["ab"]["round_id"] == r["round_id"]
    sw = SV.enqueue_ab(ctx, s["id"], "directed_vs_undirected", swap=True)
    q0, q1 = (SV.get_job(ctx, j)["payload"] for j in sw["jobs"])
    assert q0["ns_patch"] == c["b_patch"] and q1["ns_patch"] == c["a_patch"] and q0["ab"]["side"] == "b"
    assert sw["round_id"] != r["round_id"]


def test_random_assignment_uses_both_orders(client, ctx):
    bid, _w = preset_book(client, ctx)
    s = fear_segment(client, ctx, bid)
    first_sides = set()
    for _ in range(24):
        r = SV.enqueue_ab(ctx, s["id"], "directed_vs_undirected")
        first_sides.add(SV.get_job(ctx, r["jobs"][0])["payload"]["ab"]["side"])
    assert first_sides == {"a", "b"}


def test_round_hides_labels_until_the_vote_and_the_order_is_stable(client, ctx):
    bid, w = preset_book(client, ctx)
    s = fear_segment(client, ctx, bid)
    r = SV.enqueue_ab(ctx, s["id"], "directed_vs_undirected", swap=True)
    c = AB.AB_COMPARISONS["directed_vs_undirected"]
    pending = SV.get_ab_round(ctx, r["round_id"])
    assert pending["status"] == "pending" and [x["side"] for x in pending["sides"]] == ["x", "y"] and all(x["media_url"] is None for x in pending["sides"])
    with pytest.raises(ServiceError) as e:
        SV.vote_ab(ctx, r["round_id"], "x")
    assert e.value.status == 409
    finish_round(ctx, w)
    rd = SV.get_ab_round(ctx, r["round_id"])
    assert rd["status"] == "ready" and rd["voted"] is False and "revealed" not in rd and "tally" not in rd
    assert rd["mock"] is True and rd["engine"] == "mock"                                              # mock engine is surfaced
    assert [x["job_id"] for x in rd["sides"]] == r["jobs"] and all(x["media_url"] == f"/media/preview/{x['job_id']}" for x in rd["sides"])
    blob = json.dumps(rd)
    assert c["a_label"] not in blob and c["b_label"] not in blob and "ns_patch" not in blob and "gen_key" not in blob and "director_enabled" not in blob
    assert SV.get_ab_round(ctx, r["round_id"]) == rd                                                  # stable
    for x in rd["sides"]:                                                                              # both clips are playable through the preview route
        assert client.get(x["media_url"]).status_code == 200
    # each preview was rendered with its own patch, and recorded it
    res = [SV.get_job(ctx, j)["result"] for j in r["jobs"]]
    assert res[0]["ns_patch"] == c["b_patch"] and res[1]["ns_patch"] == c["a_patch"]                  # swap=True: x is b
    assert res[0]["instruct"] is None and res[1]["instruct"]
    assert res[0]["gen_key"] != res[1]["gen_key"] and res[0]["ab"]["comparison"] == "directed_vs_undirected"


@pytest.mark.parametrize("swap", [False, True])
def test_vote_maps_x_and_y_back_to_a_and_b(client, ctx, swap):
    bid, w = preset_book(client, ctx)
    s = fear_segment(client, ctx, bid)
    c = AB.AB_COMPARISONS["directed_vs_undirected"]
    r = SV.enqueue_ab(ctx, s["id"], "directed_vs_undirected", swap=swap)
    finish_round(ctx, w)
    v = SV.vote_ab(ctx, r["round_id"], "x", other_narrator=True)
    expected = "b" if swap else "a"
    assert v["winner"] == expected and v["voted"] is True and v["other_narrator"] is True
    x_label, y_label = (c["b_label"], c["a_label"]) if swap else (c["a_label"], c["b_label"])
    assert v["revealed"] == {"x": x_label, "y": y_label}
    with ctx.db.session() as conn:
        vote = row(conn, "SELECT * FROM ab_votes WHERE book_id=?", (bid,))
    assert vote["winner"] == expected and vote["other_narrator"] == 1 and vote["segment_id"] == s["id"] and vote["comparison"] == "directed_vs_undirected"
    assert (vote["a_label"], vote["b_label"]) == (c["a_label"], c["b_label"]) and vote["a_gen_key"] and vote["b_gen_key"] and vote["a_gen_key"] != vote["b_gen_key"]
    with ctx.db.session() as conn:
        res = {SV.get_job(ctx, j)["payload"]["ab"]["side"]: SV.get_job(ctx, j)["result"]["gen_key"] for j in r["jobs"]}
    assert (vote["a_gen_key"], vote["b_gen_key"]) == (res["a"], res["b"])
    assert v["tally"]["votes"] == 1 and v["tally"][f"{expected}_wins"] == 1
    again = SV.get_ab_round(ctx, r["round_id"])
    assert again["voted"] is True and again["revealed"] == v["revealed"] and again["tally"] == v["tally"]
    with pytest.raises(ServiceError) as e:
        SV.vote_ab(ctx, r["round_id"], "y")
    assert e.value.status == 409
    with ctx.db.session() as conn:
        assert len(rows(conn, "SELECT id FROM ab_votes")) == 1


def test_vote_choices_and_unknown_rounds(client, ctx):
    bid, w = preset_book(client, ctx)
    s = fear_segment(client, ctx, bid)
    r = SV.enqueue_ab(ctx, s["id"], "directed_vs_undirected", swap=False)
    finish_round(ctx, w)
    with pytest.raises(ServiceError) as e:
        SV.vote_ab(ctx, r["round_id"], "a")
    assert e.value.status == 422
    for bad in ("0" * 32, "zz", "../../x"):
        with pytest.raises(ServiceError) as e:
            SV.get_ab_round(ctx, bad)
        assert e.value.status == 404
    v = SV.vote_ab(ctx, r["round_id"], "same")
    assert v["winner"] == "same" and v["tally"]["same"] == 1 and v["tally"]["non_tie"] == 0 and v["tally"]["a_share"] is None


def test_a_failed_preview_fails_the_round(client, ctx):
    bid, _w = preset_book(client, ctx)
    s = fear_segment(client, ctx, bid)
    r = SV.enqueue_ab(ctx, s["id"], "directed_vs_undirected", swap=False)
    with ctx.db.tx() as conn:
        conn.execute("UPDATE jobs SET status='failed', error='boom' WHERE id=?", (r["jobs"][1],))
    rd = SV.get_ab_round(ctx, r["round_id"])
    assert rd["status"] == "failed" and rd["error"] == "boom"


# ------------------------------------------------------------------------------------ tally


def test_tally_math_and_status(client, ctx):
    bid, _w = preset_book(client, ctx)
    t0 = SV.ab_tally(ctx, bid)
    assert set(t0) == set(AB.AB_COMPARISONS) and all(v["votes"] == 0 and v["a_share"] is None for v in t0.values())
    ins = "INSERT INTO ab_votes(book_id, comparison, a_label, b_label, winner, other_narrator, created_at) VALUES (?,?,?,?,?,?,1)"
    with ctx.db.tx() as conn:
        for winner, flag in (("a", 0), ("a", 0), ("a", 1), ("b", 0), ("same", 0), ("same", 1)):
            conn.execute(ins, (bid, "pace_on_vs_off", "x", "y", winner, flag))
        conn.execute(ins, (bid, "directed_vs_undirected", "x", "y", "b", 0))
    t = SV.ab_tally(ctx, bid)["pace_on_vs_off"]
    assert (t["a_wins"], t["b_wins"], t["same"], t["votes"], t["non_tie"], t["other_narrator_flags"]) == (3, 1, 2, 6, 4, 2)
    assert t["a_share"] == 0.75 and t["other_narrator_share"] == pytest.approx(2 / 6, abs=1e-3)
    assert t["a_label"] == AB.AB_COMPARISONS["pace_on_vs_off"]["a_label"]
    assert SV.ab_tally(ctx, bid)["directed_vs_undirected"]["a_share"] == 0.0
    assert client.get(f"/api/books/{bid}/narrative").json()["ab"]["pace_on_vs_off"]["a_share"] == 0.75   # also part of the Narrative status
    with pytest.raises(ServiceError):
        SV.ab_tally(ctx, 99999)


def test_ab_votes_are_not_copied_to_a_new_narrator_version_and_leave_with_the_book(client, ctx):
    b = _import(client)
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO ab_votes(book_id, comparison, a_label, b_label, winner, created_at) VALUES (?,?,?,?,?,1)", (b["id"], "pace_on_vs_off", "a", "b", "a"))
    r = _add(client, b["id"], "Aiden")
    assert r.status_code == 200, r.text
    new_id = r.json()["id"]
    with ctx.db.session() as conn:
        assert rows(conn, "SELECT id FROM ab_votes WHERE book_id=?", (new_id,)) == []
        assert len(rows(conn, "SELECT id FROM ab_votes WHERE book_id=?", (b["id"],))) == 1
    assert client.post(f"/api/disk/books/{b['id']}/delete", json={"confirm": True}).status_code == 200
    with ctx.db.session() as conn:
        assert rows(conn, "SELECT id FROM ab_votes WHERE book_id=?", (b["id"],)) == []


# ------------------------------------------------------------------------------------ random differing segment


def test_random_differing_segment_finds_and_excludes(client, ctx):
    bid, _w = preset_book(client, ctx)
    s = fear_segment(client, ctx, bid)
    found = SV.random_differing_segment(ctx, bid, "directed_vs_undirected")
    assert found["segment_id"] and found["sid"] and found["excerpt"]
    a, b = plans(ctx, found["segment_id"], "directed_vs_undirected")
    assert a.gen_key != b.gen_key                                                                       # whatever it returns really differs
    with ctx.db.session() as conn:
        everything = [r["segment_id"] for r in rows(conn, "SELECT segment_id FROM directions WHERE book_id=?", (bid,))]
    with pytest.raises(ServiceError) as e:
        SV.random_differing_segment(ctx, bid, "directed_vs_undirected", exclude=everything)
    assert e.value.status == 404 and "No segment differs" in str(e.value)
    assert s["id"] in everything
    # bank needs a cloned voice: this preset book has none
    with pytest.raises(ServiceError) as e:
        SV.random_differing_segment(ctx, bid, "bank_vs_base_clip")
    assert e.value.status == 404
    with pytest.raises(ServiceError) as e:
        SV.random_differing_segment(ctx, bid, "nope")
    assert e.value.status == 422


def test_random_differing_segment_falls_back_to_all_segments_without_directions(client, ctx):
    bid = import_semantic(client)["id"]                                                                 # director off, never analysed: no directions
    with pytest.raises(ServiceError) as e:
        SV.random_differing_segment(ctx, bid, "directed_vs_undirected")
    assert e.value.status == 404


# ------------------------------------------------------------------------------------ routes and roles


def test_routes_end_to_end_for_an_operator(client, ctx):
    bid, w = preset_book(client, ctx)
    s = fear_segment(client, ctx, bid)
    for m, p in (("POST", f"/api/segments/{s['id']}/ab"), ("GET", "/api/ab/" + "0" * 32), ("POST", "/api/ab/" + "0" * 32 + "/vote"),
                 ("GET", f"/api/books/{bid}/ab/random"), ("GET", f"/api/books/{bid}/ab"), ("POST", f"/api/books/{bid}/narrative/apply-delivery")):
        assert not is_admin_only(m, p), p
    store = AuthStore(ctx.config_dir / "auth.json")
    store.set_user("clerk", "correct-horse-battery2", "operator")
    app = create_app(ctx, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:7861", headers={"X-Studio-Request": "1"}) as c:
        assert c.get(f"/api/books/{bid}/ab").status_code == 401
        assert c.post("/api/auth/login", json={"username": "clerk", "password": "correct-horse-battery2"}).status_code == 200
        r = c.get(f"/api/books/{bid}/ab/random", params={"comparison": "directed_vs_undirected"})
        assert r.status_code == 200 and r.json()["segment_id"]
        assert c.get(f"/api/books/{bid}/ab/random", params={"comparison": "bank_vs_base_clip"}).status_code == 404
        assert c.post(f"/api/segments/{s['id']}/ab", json={"comparison": "bank_vs_base_clip"}).status_code == 409
        assert c.post(f"/api/segments/{s['id']}/ab", json={"comparison": "pace_on_vs_off", "extra": 1}).status_code == 422
        rd = c.post(f"/api/segments/{s['id']}/ab", json={"comparison": "directed_vs_undirected"})
        assert rd.status_code == 200, rd.text
        rid = rd.json()["round_id"]
        assert c.get(f"/api/ab/{rid}").json()["status"] == "pending"
        assert c.post(f"/api/ab/{rid}/vote", json={"choice": "x"}).status_code == 409                    # not rendered yet
        w.run_until_idle()
        got = c.get(f"/api/ab/{rid}").json()
        assert got["status"] == "ready" and got["mock"] is True
        assert c.post(f"/api/ab/{rid}/vote", json={"choice": "q"}).status_code == 422
        v = c.post(f"/api/ab/{rid}/vote", json={"choice": "y", "other_narrator": False})
        assert v.status_code == 200 and set(v.json()["revealed"]) == {"x", "y"}
        assert c.post(f"/api/ab/{rid}/vote", json={"choice": "y"}).status_code == 409
        t = c.get(f"/api/books/{bid}/ab").json()
        assert t["tally"]["directed_vs_undirected"]["votes"] == 1 and [x["id"] for x in t["comparisons"]] == list(AB.AB_COMPARISONS)
        with ctx.db.session() as conn:
            assert row(conn, "SELECT actor FROM ab_votes")["actor"] == "clerk"
        assert c.post(f"/api/books/{bid}/narrative/apply-delivery", json={}).status_code == 200


# ------------------------------------------------------------------------------------ apply to existing takes


def rendered_clone_book(client, ctx):
    """A book with a cloned character and a preset narrator, every segment rendered with the delivery settings OFF; one clone segment asks for a slow pace."""
    bid, clone, toms, eng, w = clone_book(client, ctx)
    direct(client, toms[0]["id"], pacing="slow")
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    assert all(s["status"] == "passed" for s in all_segments(ctx, bid))
    return bid, toms, w


def seg_status(ctx, bid):
    return {s["id"]: s["status"] for s in all_segments(ctx, bid)}


@needs_ffmpeg
def test_invalidate_changed_dry_run_matches_the_real_run_and_writes_nothing(client, ctx):
    bid, toms, _w = rendered_clone_book(client, ctx)
    with ctx.db.session() as conn:
        assert N_store.invalidate_changed(conn, ctx, bid, dry_run=True) == []                          # nothing changed yet
    delivery(client, pace_from_direction="always")
    before = seg_status(ctx, bid)
    with ctx.db.tx() as conn:
        dry = N_store.invalidate_changed(conn, ctx, bid, dry_run=True)
        n_actions = len(rows(conn, "SELECT id FROM review_actions"))
        rounds = {r["id"]: r["round"] for r in rows(conn, "SELECT id, round FROM segments WHERE book_id=?", (bid,))}
    assert dry and toms[0]["id"] in dry and len(dry) < len(all_segments(ctx, bid))
    assert seg_status(ctx, bid) == before
    with ctx.db.session() as conn:
        assert len(rows(conn, "SELECT id FROM review_actions")) == n_actions
        assert {r["id"]: r["round"] for r in rows(conn, "SELECT id, round FROM segments WHERE book_id=?", (bid,))} == rounds
    with ctx.db.tx() as conn:
        real = N_store.invalidate_changed(conn, ctx, bid)
    assert sorted(real) == sorted(dry)
    assert {i for i, st in seg_status(ctx, bid).items() if st == "pending"} == set(real)


@needs_ffmpeg
def test_apply_requires_confirmation_then_resets_only_changed_segments(client, ctx):
    bid, toms, w = rendered_clone_book(client, ctx)
    other = next(s for s in all_segments(ctx, bid) if s["id"] != toms[0]["id"] and s["status"] == "passed")
    # nothing differs yet: no confirmation needed, nothing happens
    r = SV.apply_delivery_to_takes(ctx, bid, confirm=False)
    assert r["reset"] == 0 and r["queued"] is False and "No rendered segment" in r["message"]
    delivery(client, pace_from_direction="always")
    with ctx.db.tx() as conn:
        would = N_store.invalidate_changed(conn, ctx, bid, dry_run=True)
    with pytest.raises(NeedsConfirmation) as e:
        SV.apply_delivery_to_takes(ctx, bid, confirm=False)
    d = e.value.details
    assert e.value.status == 409 and d["segments"] == len(would) and d["approved"] == 0 and d["chapters"] >= 1 and "regenerated" in str(e.value)
    assert all(st == "passed" for st in seg_status(ctx, bid).values())                                 # the dry run changed nothing
    # the same through HTTP
    h = client.post(f"/api/books/{bid}/narrative/apply-delivery", json={})
    assert h.status_code == 409 and h.json()["details"]["segments"] == len(would)
    with ctx.db.tx() as conn:                                                                           # a release exists
        conn.execute("INSERT INTO releases(book_id, approved_by, listen_scope, statement, checklist_json, approved_at) VALUES (?,?,?,?,?,1)", (bid, "QA Lead", "full", "s", "{}"))
    out = SV.apply_delivery_to_takes(ctx, bid, confirm=True)
    assert out["reset"] == len(would) and out["queued"] is True and out["job_id"]
    after = seg_status(ctx, bid)
    assert {i for i, st in after.items() if st == "pending"} == set(would) and after[toms[0]["id"]] == "pending"
    assert (after[other["id"]] == "pending") == (other["id"] in would)                                  # a segment is reset only if it changed
    with ctx.db.session() as conn:
        assert row(conn, "SELECT id FROM releases WHERE book_id=? AND revoked_at IS NULL", (bid,)) is None      # release revoked
        job = row(conn, "SELECT * FROM jobs WHERE id=?", (out["job_id"],))
        assert job["kind"] == "book" and job["status"] == "queued"
        assert job["position"] == min(r["position"] for r in rows(conn, "SELECT position FROM jobs WHERE kind='book'"))
        assert row(conn, "SELECT id FROM audit_log WHERE entity='book' AND action='apply_delivery'")
        old = rows(conn, "SELECT id FROM attempts WHERE segment_id=?", (toms[0]["id"],))
    assert old                                                                                          # old takes are kept as history
    w.run_until_idle()
    assert all(st == "passed" for st in seg_status(ctx, bid).values())
    with ctx.db.session() as conn:
        a = row(conn, "SELECT pace_used FROM attempts WHERE segment_id=? ORDER BY id DESC", (toms[0]["id"],))
    assert a["pace_used"] == pytest.approx(0.92)
    # applying again finds nothing to do
    assert SV.apply_delivery_to_takes(ctx, bid, confirm=True)["reset"] == 0


@needs_ffmpeg
def test_apply_leaves_approved_segments_unless_asked(client, ctx):
    bid, toms, w = rendered_clone_book(client, ctx)
    assert client.post(f"/api/segments/{toms[0]['id']}/approve", json={"note": None}).status_code == 200
    delivery(client, pace_from_direction="always")
    with ctx.db.tx() as conn:
        everything = N_store.invalidate_changed(conn, ctx, bid, include_approved=True, dry_run=True)
        plain = N_store.invalidate_changed(conn, ctx, bid, dry_run=True)
    assert toms[0]["id"] in everything and toms[0]["id"] not in plain and len(everything) == len(plain) + 1
    with pytest.raises(NeedsConfirmation) as e:
        SV.apply_delivery_to_takes(ctx, bid, confirm=False)
    assert e.value.details["segments"] == len(plain) and e.value.details["approved"] == 1
    with pytest.raises(NeedsConfirmation) as e:
        SV.apply_delivery_to_takes(ctx, bid, confirm=False, include_approved=True)
    assert e.value.details["segments"] == len(everything) and e.value.details["approved_included"] is True
    out = SV.apply_delivery_to_takes(ctx, bid, confirm=True)
    assert out["reset"] == len(plain)
    assert seg_status(ctx, bid)[toms[0]["id"]] == "approved"
    out = SV.apply_delivery_to_takes(ctx, bid, confirm=True, include_approved=True)
    assert out["reset"] == 1 and seg_status(ctx, bid)[toms[0]["id"]] == "pending"


@needs_ffmpeg
def test_apply_with_only_approved_changes_explains_itself(client, ctx):
    bid, toms, _w = rendered_clone_book(client, ctx)
    for s in all_segments(ctx, bid):
        client.post(f"/api/segments/{s['id']}/approve", json={"note": None})
    delivery(client, pace_from_direction="always")
    out = SV.apply_delivery_to_takes(ctx, bid, confirm=False)
    assert out["reset"] == 0 and out["approved"] >= 1 and "approved" in out["message"]
    with pytest.raises(ServiceError) as e:
        SV.apply_delivery_to_takes(ctx, 99999, confirm=True)
    assert e.value.status == 404
