"""Director v2: the chapter beat sheet (one thinking request per chapter) and scene-batched direction (one request per window of
segments). Everything here runs against stub replies (httpx.MockTransport or a local HTTP stub): no real model behaviour is tested."""
from __future__ import annotations

import json
import sqlite3

import httpx
import narrative_fixtures as NF
import pytest
from test_narrative import all_segments, import_semantic, llm_client, worker_with

from studio.config import LLMSettings, NarrativeSettings
from studio.db import row, rows, scalar
from studio.narrative import analysis, beats as B, director as D
from studio.narrative.llm import LLMClient
from studio.util import loads


# ------------------------------------------------------------------------------------------------ helpers


def ok(reply: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]})


class Recorder:
    """A stub LLM endpoint that answers the three Director prompts and records every request body."""

    def __init__(self, beats=True, scene_over=None, probe=None, emotions=("calm", "fear")):
        self.requests: list[dict] = []
        self.users: list[str] = []
        self.beats = beats
        self.emotions = emotions
        self.scene_over = scene_over or {}
        self.probe = probe

    def __call__(self, req):
        body = json.loads(req.content)
        user = body["messages"][1]["content"]
        body["_timeout"] = req.extensions.get("timeout")
        self.requests.append(body)
        self.users.append(user)
        if self.probe:
            self.probe(user)
        if user.startswith("Chapter beat sheet"):
            return ok(NF.beats_reply(user, self.emotions)) if self.beats else httpx.Response(500, text="no plan")
        if user.startswith("Direct this scene"):
            return ok(NF.scene_reply(user, **self.scene_over))
        return httpx.Response(500, text="unexpected")

    def of(self, header: str) -> list[dict]:
        return [b for b, u in zip(self.requests, self.users) if u.startswith(header)]


def client_for(handler, **cfg) -> LLMClient:
    return llm_client(handler, **cfg)


def prepared(client, ctx, **narr) -> int:
    """An imported, analysed book whose directions and Director cache are then cleared, so the next run asks the LLM afresh."""
    bid = import_semantic(client, director_enabled=True, **narr)["id"]
    client.post(f"/api/books/{bid}/analyze", json={})
    worker_with(ctx).run_until_idle()
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM directions WHERE book_id=?", (bid,))
        conn.execute("DELETE FROM director_cache WHERE 1=1")
    return bid


def clear_directions(ctx, bid):
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM directions WHERE book_id=?", (bid,))


def directions(ctx, bid) -> dict[int, dict]:
    with ctx.db.session() as conn:
        return {r["segment_id"]: {**r, "raw": loads(r["raw_json"], {})} for r in rows(conn, "SELECT * FROM directions WHERE book_id=?", (bid,))}


def stage_stats(client, bid) -> dict:
    return client.get(f"/api/books/{bid}/narrative").json()["stages"]["director"]["stats"]


# ------------------------------------------------------------------------------------------- validate_beats


def beat(a, b, emotion="calm", **kw):
    return {"paras": [a, b], "emotion": emotion, "intensity": 2, "pacing": "normal", "tension": 1, "purpose": "x", "pause": "none", **kw}


def covered(plan) -> list[int]:
    return [p for sc in plan["scenes"] for b in sc["beats"] for p in range(b["paras"][0], b["paras"][1] + 1)]


def test_validate_beats_covers_every_paragraph_once_in_order():
    plan = B.validate_beats({"arc": "up", "scenes": [{"summary": "s", "beats": [beat(1, 3), beat(4, 6, "fear")]}]}, 6)
    assert covered(plan) == [1, 2, 3, 4, 5, 6] and plan["n_paras"] == 6 and plan["arc"] == "up"


def test_validate_beats_resolves_overlaps_gaps_and_clips_ranges():
    raw = {"scenes": [{"summary": "a", "beats": [beat(3, 6, "fear"), beat(5, 20, "anger"), beat(0, 1, "calm")]},   # out of order, overlapping, out of range
                      {"summary": "b", "beats": [beat(8, 8, "grief")]}]}                                          # gap at 2 and 7, nothing after 8
    plan = B.validate_beats(raw, 10)
    assert covered(plan) == list(range(1, 11))
    flat = [b for sc in plan["scenes"] for b in sc["beats"]]
    assert [b["paras"][0] for b in flat] == sorted(b["paras"][0] for b in flat)
    by_start = {b["paras"][0]: b for b in flat}
    assert by_start[1]["paras"] == [1, 2] and by_start[1]["emotion"] == "calm"          # a gap is filled by extending the previous beat
    assert by_start[3]["paras"] == [3, 4] and by_start[3]["emotion"] == "fear"           # a later start wins over the overlapped tail
    assert by_start[5]["emotion"] == "anger" and by_start[8]["paras"] == [8, 10]


def test_validate_beats_same_start_later_wins_and_first_beat_extends_back():
    plan = B.validate_beats({"scenes": [{"summary": "", "beats": [beat(2, 4, "calm"), beat(2, 3, "fear")]}]}, 5)
    (only,) = [b for sc in plan["scenes"] for b in sc["beats"]]
    assert only["paras"] == [1, 5] and only["emotion"] == "fear"


def test_validate_beats_normalises_vocabulary_and_drops_unknown_emotions():
    raw = {"scenes": [{"summary": "s", "beats": [beat(1, 2, "Sad", intensity=9, pacing="slowly", pause="Long", tension="0.8"),
                                                  beat(3, 4, "melancholic-ish"), beat(5, 6, "angry", pause="weird")]}]}
    plan = B.validate_beats(raw, 6)
    flat = [b for sc in plan["scenes"] for b in sc["beats"]]
    assert flat[0]["emotion"] == "sadness" and flat[0]["intensity"] == 5 and flat[0]["pacing"] == "slow" and flat[0]["pause"] == "long" and flat[0]["tension"] == 4
    assert [b["emotion"] for b in flat] == ["sadness", "anger"] and flat[0]["paras"] == [1, 4] and flat[1]["pause"] == "none"
    assert covered(plan) == [1, 2, 3, 4, 5, 6]


def test_validate_beats_accepts_loose_range_spellings():
    plan = B.validate_beats({"scenes": [{"summary": "", "beats": [{**beat(0, 0), "paras": "P1-P2"}, {**beat(0, 0, "fear"), "paras": [3]}]}]}, 4)
    assert covered(plan) == [1, 2, 3, 4]


@pytest.mark.parametrize("raw", [None, "text", {}, {"scenes": []}, {"scenes": [{"summary": "no beats"}]},
                                 {"scenes": [{"beats": [beat(1, 2, "nonsense")]}]}, {"scenes": [{"beats": [{"emotion": "calm"}]}]},
                                 {"scenes": [{"beats": [beat(40, 50)]}]}])
def test_validate_beats_unusable_replies_raise(raw):
    with pytest.raises(D.DirectorError):
        B.validate_beats(raw, 6)


def test_beat_for_and_paragraph_numbers():
    plan = B.validate_beats({"scenes": [{"summary": "one", "beats": [beat(1, 2)]}, {"summary": "two", "beats": [beat(3, 5, "fear")]}]}, 5)
    assert B.beat_for(plan, 2)["emotion"] == "calm" and B.beat_for(plan, 2)["scene_summary"] == "one" and B.beat_for(plan, 2)["index"] == 0
    assert B.beat_for(plan, 4)["emotion"] == "fear" and B.beat_for(plan, 4)["index"] == 1 and B.beat_for(plan, 4)["scene_no"] == 1
    assert B.beat_for(plan, 9) is None and B.beat_for(None, 1) is None
    text = "Heading\n\nFirst para.\n\nSecond para."
    starts = B.paragraph_starts(text)
    assert [B.para_number(starts, o) for o in (0, 9, 25, len(text) - 1)] == [1, 2, 3, 3]
    marked, n = B.marked_text(text)
    assert n == 3 and marked.startswith("[P1] Heading") and "[P3] Second para." in marked


# ------------------------------------------------------------------------------------- the chapter plan pass


def test_chapter_plan_is_one_thinking_request_per_chapter_and_cached(client, ctx):
    bid = prepared(client, ctx)
    rec = Recorder()
    c = client_for(rec, chapter_model="big-model", scene_model="small-model", chapter_max_tokens=12000, chapter_timeout_s=777)
    r = analysis.director_batch(ctx, bid, 100, c)
    assert r["directed"] == len(all_segments(ctx, bid)) and r["remaining"] == 0
    plans = rec.of("Chapter beat sheet")
    assert len(plans) == 2                                                                    # one per chapter, however many windows
    p = plans[0]
    assert p["model"] == "big-model" and p["chat_template_kwargs"] == {"enable_thinking": True} and "response_format" not in p
    assert p["max_tokens"] == 12000 and p["_timeout"]["read"] == 777
    assert "[P1]" in rec.users[0] and "[P2]" in rec.users[0]
    scenes = rec.of("Direct this scene")
    assert scenes and all(s["model"] == "small-model" and s["chat_template_kwargs"] == {"enable_thinking": False} and s["response_format"] == {"type": "json_object"} for s in scenes)
    assert {r_["source"] for r_ in directions(ctx, bid).values()} == {"llm"}
    st = stage_stats(client, bid)
    assert st["chapter_plans"] == 2 and st["chapter_plan_failures"] == 0 and st["scene_calls"] == len(scenes) and st["llm_calls"] == len(scenes)
    # a rerun (directions gone, cache kept) needs no request at all
    clear_directions(ctx, bid)
    before = len(rec.requests)
    r2 = analysis.director_batch(ctx, bid, 100, c)
    assert len(rec.requests) == before and r2["directed"] == r["directed"]
    assert r2["cache_hits"] >= r["directed"]                                                  # window hits count per segment
    assert stage_stats(client, bid)["chapter_plans"] == 4                                    # plans came from the cache both times


def test_beats_prompt_has_markers_and_only_confirmed_facts_and_profiles(client, ctx):
    bid = prepared(client, ctx)
    with ctx.db.tx() as conn:
        for text, confirmed in (("Elena lost her son in the flood", 1), ("Hale secretly stole the letter", 0)):
            conn.execute("""INSERT INTO story_notes(book_id, chapter_idx, scene_idx, kind, text, source, confidence, confirmed, refs_json, created_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?)""", (bid, None, None, "fact", text, "user", 1.0, confirmed, "[]", 0))
    with ctx.db.session() as conn:
        elena = row(conn, "SELECT * FROM characters WHERE book_id=? AND name LIKE 'Elena%'", (bid,))
    rec = Recorder()
    analysis.director_batch(ctx, bid, 100, client_for(rec))
    user = rec.of("Chapter beat sheet") and next(u for u in rec.users if u.startswith("Chapter beat sheet"))
    head = json.JSONDecoder().raw_decode(user.split("\n", 1)[1])[0]
    assert head["confirmed_facts"] == ["Elena lost her son in the flood"] and "stole the letter" not in user
    assert "[P1]" in user and "[P2]" in user and "Chapter text" in user
    assert {c["name"] for c in head["cast"]} >= {elena["name"]} and all(set(c) == {"name", "aliases", "confirmed"} for c in head["cast"])
    assert "inferred" not in user
    # the system prompt asks for restraint and the vocabulary, and forbids inventing events
    system = rec.requests[0]["messages"][0]["content"]
    assert "restraint" in system.lower() and "controlled vocabulary" in system and "never invent" in system.lower()


def test_ensure_chapter_plan_returns_none_when_off_or_without_llm(client, ctx):
    bid = prepared(client, ctx)
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (bid,))
        ch = row(conn, "SELECT c.id, c.idx, t.text, t.sha256 FROM chapters c JOIN chapter_text t ON t.chapter_id=c.id WHERE c.book_id=? ORDER BY c.idx", (bid,))
    rec = Recorder()
    live = client_for(rec)
    on = NarrativeSettings(director_chapter_plan=True)
    get, put = analysis._cache_fns(ctx.db)
    assert B.ensure_chapter_plan(ctx, book, ch, live, NarrativeSettings(director_chapter_plan=False), get, put) is None
    assert B.ensure_chapter_plan(ctx, book, ch, None, on, get, put) is None
    assert B.ensure_chapter_plan(ctx, book, ch, LLMClient(LLMSettings()), on, get, put) is None
    assert rec.requests == []
    plan = B.ensure_chapter_plan(ctx, book, ch, live, on, get, put)
    assert plan and plan["scenes"] and len(rec.requests) == 1


def test_failed_chapter_plan_is_not_retried_and_scenes_still_run(client, ctx):
    bid = prepared(client, ctx)
    rec = Recorder(beats=False)
    c = client_for(rec)
    r = analysis.director_batch(ctx, bid, 100, c)
    assert r["directed"] == len(all_segments(ctx, bid))
    assert len(rec.of("Chapter beat sheet")) == 2                                             # no repair re-call, no retry per window
    assert {d["raw"]["source"] for d in directions(ctx, bid).values()} == {"llm"}
    assert all("beat" not in d["raw"] for d in directions(ctx, bid).values())
    st = stage_stats(client, bid)
    assert st["chapter_plans"] == 0 and st["chapter_plan_failures"] == 2 and st["scene_calls"] >= 2
    with ctx.db.session() as conn:
        assert scalar(conn, "SELECT COUNT(*) FROM director_cache WHERE key LIKE 'beats:%'", (), 0) == 0   # only valid plans are cached as plans
    clear_directions(ctx, bid)
    analysis.director_batch(ctx, bid, 100, c)
    assert len(rec.of("Chapter beat sheet")) == 2                                             # a recent failure is left alone for a while


def test_an_unparseable_or_empty_plan_reply_counts_as_a_failure(client, ctx):
    bid = prepared(client, ctx)

    def handler(req):
        user = json.loads(req.content)["messages"][1]["content"]
        if user.startswith("Chapter beat sheet"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "I think the chapter is sad.", "reasoning_content": "hmm"}, "finish_reason": "stop"}]})
        return ok(NF.scene_reply(user))

    analysis.director_batch(ctx, bid, 100, client_for(handler))
    assert stage_stats(client, bid)["chapter_plan_failures"] == 2


def test_chapter_plan_setting_off_skips_the_pass(client, ctx):
    bid = prepared(client, ctx, director_chapter_plan=False)
    rec = Recorder()
    r = analysis.director_batch(ctx, bid, 100, client_for(rec))
    assert rec.of("Chapter beat sheet") == [] and rec.of("Direct this scene")
    assert r["directed"] == len(all_segments(ctx, bid)) and all("beat" not in d["raw"] for d in directions(ctx, bid).values())


# ------------------------------------------------------------------------------------------ scene direction


def item(sid, text="Some text.", speaker="Hale", **kw):
    return {"sid": sid, "text": text, "kind": "dialogue", "speaker": speaker, "cues": {"emotion": "neutral", "intensity": 1}, "sensitivity": 0.5,
            "profile": {"name": speaker}, "hints": "", "beat": None, "context": {"before": "", "after": ""}, **kw}


def scene_ctx(**kw):
    return {"chapter": 1, "scene": 0, "summary": "Hale visits Elena.", "arc": "", "facts": [], "previous": [], "before": "", "after": "", **kw}


def test_validate_scene_items_is_per_item():
    ctx_by_id = {str(i): {"sid": f"s{i}", "text": "I don't know. Her voice cracked.", "speaker": "Elena", "context": "ctx"} for i in (1, 2, 3, 4)}
    raw = {"items": [
        {"id": "1", "emotion": "Sad", "intensity": 4, "pacing": "slowly", "tension": 3, "delivery": "voice breaking, barely a whisper", "change": True, "pause": "long",
         "confidence": 0.9, "review": False},
        {"id": "2", "emotion": "bogus", "intensity": 3, "pacing": "normal"},                       # outside the vocabulary
        {"id": "3", "emotion": "calm", "pacing": "warp speed"},                                    # pacing outside the vocabulary
        {"id": "9", "emotion": "calm", "pacing": "normal"},                                        # not asked for
        {"id": "1", "emotion": "anger", "pacing": "fast"},                                         # duplicate: the first valid one wins
    ]}
    ok_, failed = D.validate_scene_items(raw, ["1", "2", "3", "4"], ctx_by_id)
    assert list(ok_) == ["1"] and failed == ["2", "3", "4"]
    d = ok_["1"]
    assert d["emotion"] == "sadness" and d["pacing"] == "slow" and d["intensity"] == 4 and d["tension"] == 3 and d["pause"] == "long"
    assert d["segment_id"] == "s1" and d["speaker"] == "Elena" and d["source"] == "llm"
    assert d["support"] is True and d["needs_review"] is False and d["confidence"] == {"speaker": 0.9, "emotion": 0.9, "overall": 0.9}
    assert d["facts"] == [] and d["inferences"] == [] and d["context_refs"] == [] and d["notes"] == []
    for k in ("segment_id", "speaker", "voice_profile", "emotion", "intensity", "delivery", "pacing", "tension", "confidence", "support", "context_refs",
              "facts", "inferences", "ambiguity", "needs_review", "notes", "source"):
        assert k in d


def test_scene_item_support_review_and_leak_guard():
    c = {"1": {"sid": "a", "text": "Her voice cracked and she could not go on speaking at all", "speaker": None, "context": ""}}
    low = D.validate_scene_items({"items": [{"id": "1", "emotion": "calm", "change": True, "confidence": 0.5, "review": "false"}]}, ["1"], c)[0]["1"]
    assert low["support"] is False and low["needs_review"] is False                               # support needs confidence >= 0.6; "false" is false
    unsure = D.validate_scene_items({"items": [{"id": "1", "emotion": "calm", "confidence": 0.3}]}, ["1"], c)[0]["1"]
    assert unsure["needs_review"] is True and unsure["speaker"] == ""
    quoted = D.validate_scene_items({"items": [{"id": "1", "emotion": "calm", "confidence": 0.8,
                                                "delivery": "tender, her voice cracked and she could not go on speaking"}]}, ["1"], c)[0]["1"]
    assert "cracked" not in quoted["delivery"] and quoted["notes"]
    for bad in (None, [], "x", {"items": "no"}, {}):
        with pytest.raises(D.DirectorError):
            D.validate_scene_items(bad, ["1"], c)


def test_scene_prompt_carries_beats_summary_previous_delivery_and_hints():
    beat1 = {"index": 3, "emotion": "fear", "intensity": 4, "pacing": "brisk", "tension": 4, "purpose": "the threat arrives", "pause": "beat", "paras": [3, 4]}
    items = [item("a", "He ran.", beat=beat1, hints="keep it dry"), item("b", "She waited.", speaker="Elena", beat=beat1)]
    msg = D.build_scene_prompt(items, scene_ctx(previous=[{"speaker": "Hale", "emotion": "calm", "intensity": 2, "pacing": "slow", "delivery": "low"}], arc="dread builds"))
    assert msg.startswith("Direct this scene.\n")
    payload = NF.scene_payload(msg)
    assert payload["scene"]["summary"] == "Hale visits Elena." and payload["scene"]["chapter_arc"] == "dread builds"
    assert payload["beats"] == {"B1": {"emotion": "fear", "intensity": 4, "pacing": "brisk", "tension": 4, "purpose": "the threat arrives", "pause": "beat"}}
    assert [s["id"] for s in payload["segments"]] == ["1", "2"] and all(s["beat"] == "B1" for s in payload["segments"])
    assert payload["previous_delivery"][0]["emotion"] == "calm" and payload["user_instructions"] == {"Hale": "keep it dry"}
    again = D.build_scene_prompt(items, scene_ctx(), only=["2"], feedback="bad ids")
    assert [s["id"] for s in NF.scene_payload(again)["segments"]] == ["2"] and "rejected" in again
    for word in ("controlled vocabulary", "at most 12 words", "must not quote", "restraint"):
        assert word in D.SCENE_SYSTEM_PROMPT


def window_client(replies, seen):
    """A client whose scene replies come from ``replies`` (callables of the user message), one per request."""
    def handler(req):
        user = json.loads(req.content)["messages"][1]["content"]
        seen.append(user)
        return ok(replies[min(len(seen), len(replies)) - 1](user))
    return llm_client(handler)


def test_partial_failure_reasks_only_the_failing_ids_once_then_falls_back_to_rules():
    seen = []

    def first(user):
        r = NF.scene_reply(user)
        r["items"][1]["emotion"] = "bogus"                      # item 2 is invalid
        del r["items"][2]                                       # item 3 is missing
        return r

    def second(user):
        r = NF.scene_reply(user)
        r["items"] = [i for i in r["items"] if i["id"] == "3"]  # item 3 is fixed; item 2 is simply not answered
        return r

    d = D.Director(window_client([first, second], seen))
    items = [item(f"s{i}", f"Line {i} is here.") for i in (1, 2, 3)]
    store = {}
    out = d.direct_scene(items, scene_ctx(), store.get, lambda k, v, m: store.__setitem__(k, v))
    assert len(seen) == 2 and [s["id"] for s in NF.scene_payload(seen[1])["segments"]] == ["2", "3"] and "rejected" in seen[1]
    assert out["s1"][0]["source"] == "llm" and out["s3"][0]["source"] == "llm"
    assert out["s2"][0]["source"] == "rules" and out["s2"][0]["needs_review"] and any("rules engine used" in n for n in out["s2"][0]["notes"])
    assert d.stats["llm_calls"] == 2 and d.stats["fallbacks"] == 1 and d.stats["scene_calls"] == 1
    assert store == {}                                          # a window with a fallback is not cached, so a later run asks again
    assert [out[f"s{i}"][0]["segment_id"] for i in (1, 2, 3)] == ["s1", "s2", "s3"]


def test_unreachable_llm_falls_back_per_item_and_the_breaker_opens_after_three_windows():
    calls = []

    def down(req):
        calls.append(1)
        return httpx.Response(503, text="busy")

    d = D.Director(llm_client(down))
    for n in range(4):
        out = d.direct_scene([item(f"w{n}a", f"Alpha {n}."), item(f"w{n}b", f"Beta {n}.")], scene_ctx(), {}.get, lambda *a: None)
        assert all(v[0]["source"] == "rules" and v[0]["needs_review"] for v in out.values())
    assert len(calls) == 3 and d.stats["fallbacks"] == 8       # the fourth window did not call the LLM at all
    assert any("paused" in n for n in out["w3a"][0]["notes"])


def test_scene_cache_key_is_per_window_and_not_chained_to_the_previous_scene():
    a = [item("a1", "First scene line."), item("a2", "Another line.")]
    b = [item("b1", "Second scene line.")]
    key_a = D.scene_cache_key(a, scene_ctx(), "m")
    key_b = D.scene_cache_key(b, scene_ctx(summary="Different scene."), "m")
    # editing scene 2 changes scene 2's key only; the previous delivery (which scene 2 would inherit from scene 1) is not in any key
    b_edited = [item("b1", "Second scene line, edited.")]
    assert D.scene_cache_key(a, scene_ctx(), "m") == key_a and D.scene_cache_key(b_edited, scene_ctx(summary="Different scene."), "m") != key_b
    assert D.scene_cache_key(b, scene_ctx(summary="Different scene.", previous=[{"emotion": "fear"}]), "m") == key_b
    beat_x = {"index": 0, "emotion": "fear", "intensity": 3, "pacing": "normal", "tension": 3, "purpose": "p", "pause": "none"}
    assert D.scene_cache_key([item("a1", "First scene line.", beat=beat_x), a[1]], scene_ctx(), "m") != key_a
    assert D.scene_cache_key(a, scene_ctx(), "other-model") != key_a and D.scene_cache_key(a, scene_ctx(summary="changed"), "m") != key_a
    assert D.scene_cache_key([{**a[0], "sensitivity": 0.9}, a[1]], scene_ctx(), "m") != key_a
    assert D.scene_cache_key([{**a[0], "hints": "whisper"}, a[1]], scene_ctx(), "m") != key_a


def test_scene_window_is_cached_and_counts_hits_per_segment():
    seen = []
    d = D.Director(window_client([lambda u: NF.scene_reply(u)], seen))
    store, puts = {}, []
    put = lambda k, v, m: (store.__setitem__(k, v), puts.append(k))  # noqa: E731
    items = [item("x1", "One."), item("x2", "Two."), item("x3", "Three.")]
    first = d.direct_scene(items, scene_ctx(), store.get, put)
    assert len(seen) == 1 and len(puts) == 1 and d.stats["llm_calls"] == 1
    again = D.Director(window_client([lambda u: NF.scene_reply(u)], seen))
    second = again.direct_scene(items, scene_ctx(previous=[{"emotion": "grief"}]), store.get, put)
    assert len(seen) == 1 and again.stats["cache_hits"] == 3 and again.stats["llm_calls"] == 0
    assert {k: v[0] for k, v in second.items()} == {k: v[0] for k, v in first.items()}


def test_no_llm_directs_each_item_with_the_rules_engine():
    d = D.Director(None)
    store = {}
    out = d.direct_scene([item("r1", "Calm text."), item("r2", "More calm text.")], scene_ctx(), store.get, lambda k, v, m: store.__setitem__(k, v))
    assert all(v[0]["source"] == "rules" and v[0]["pause"] == "none" for v in out.values()) and d.stats["rules"] == 2 and d.stats["scene_calls"] == 0
    assert D.rules_direction("x", None, {}, 0.5, "t")["pause"] == "none"


def test_scene_request_budget_scales_with_the_window():
    seen_tokens = []

    def handler(req):
        body = json.loads(req.content)
        seen_tokens.append(body["max_tokens"])
        return ok(NF.scene_reply(body["messages"][1]["content"]))

    c = LLMClient(LLMSettings(provider="openai_compatible", model="m", max_tokens=1200), transport=httpx.MockTransport(handler))
    D.Director(c).direct_scene([item(f"p{i}", f"Text number {i}.") for i in range(16)], scene_ctx(), {}.get, lambda *a: None)
    D.Director(c).direct_scene([item("q1", "Short.")], scene_ctx(), {}.get, lambda *a: None)
    assert seen_tokens == [120 * 16 + 200, 1200]


# ------------------------------------------------------------------------------------ the batch (integration)


def test_windows_group_adjacent_segments_of_one_scene_and_respect_the_cap():
    segs = [{"chapter_id": 1, "scene_idx": 0}] * 40 + [{"chapter_id": 1, "scene_idx": 1}] * 3 + [{"chapter_id": 2, "scene_idx": 1}] * 2
    assert [len(w) for w in analysis._windows(segs, list(range(45)))] == [16, 16, 8, 3, 2]
    assert [len(w) for w in analysis._windows(segs, [0, 1, 5, 6, 7])] == [2, 3]            # not adjacent: separate windows
    assert [len(w) for w in analysis._windows(segs, list(range(3)))] == [3]


def test_batch_window_never_exceeds_the_limit(client, ctx):
    bid = prepared(client, ctx)
    rec = Recorder()
    r = analysis.director_batch(ctx, bid, 3, client_for(rec))
    assert r["directed"] == 3 and r["remaining"] == len(all_segments(ctx, bid)) - 3
    sizes = [len(NF.scene_payload(u)["segments"]) for u in rec.users if u.startswith("Direct this scene")]
    assert sum(sizes) == 3 and max(sizes) <= 3
    assert len(directions(ctx, bid)) == 3


def test_no_write_lock_is_held_during_the_chapter_or_scene_requests(client, ctx):
    bid = prepared(client, ctx)
    probes = []

    def probe(user):
        other = sqlite3.connect(str(ctx.db.path), timeout=0, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            probes.append((user.split("\n", 1)[0], "free"))
        except sqlite3.OperationalError:
            probes.append((user.split("\n", 1)[0], "locked"))
        finally:
            other.close()

    rec = Recorder(probe=probe)
    analysis.director_batch(ctx, bid, 100, client_for(rec))
    kinds = {k for k, _ in probes}
    assert any(k.startswith("Chapter beat sheet") for k in kinds) and any(k.startswith("Direct this scene") for k in kinds)
    assert {v for _, v in probes} == {"free"}, probes


def test_stats_count_plans_scene_calls_and_would_escalate(client, ctx):
    bid = prepared(client, ctx)
    rec = Recorder(scene_over={"confidence": 0.3, "review": True})
    r = analysis.director_batch(ctx, bid, 100, client_for(rec))
    n = r["directed"]
    assert r["would_escalate"] == n and r["chapter_plans"] == 2 and r["scene_calls"] == len(rec.of("Direct this scene"))
    st = stage_stats(client, bid)
    assert st["would_escalate"] == n and st["chapter_plans"] == 2 and st["scene_calls"] == r["scene_calls"] and st["directed"] == n
    assert all(d["needs_review"] for d in directions(ctx, bid).values())                  # low confidence marks them for review
    # confident replies that agree with the plan are not escalation candidates
    clear_directions(ctx, bid)
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM director_cache WHERE 1=1")
    r2 = analysis.director_batch(ctx, bid, 100, client_for(Recorder()))
    assert r2["would_escalate"] < n


def test_rules_and_llm_disagreement_counts_as_escalation(client, ctx):
    """Confident, unflagged replies still count when they contradict an explicit lexical cue from another emotion family."""
    bid = prepared(client, ctx)
    with ctx.db.session() as conn:
        explicit = [s for s in all_segments(ctx, bid) if analysis.segment_cues(conn, s).get("explicit")]
    assert explicit
    r = analysis.director_batch(ctx, bid, 100, client_for(Recorder(scene_over={"emotion": "joy", "confidence": 0.95})))
    assert 0 < r["would_escalate"] <= len(explicit)
    assert all(not d["needs_review"] for d in directions(ctx, bid).values())


def test_plan_beats_reach_the_stored_direction_and_turns_are_marked_as_supported(client, ctx):
    bid = prepared(client, ctx)
    rec = Recorder()
    analysis.director_batch(ctx, bid, 100, client_for(rec))
    segs = all_segments(ctx, bid)
    dirs = directions(ctx, bid)
    assert all("beat" in dirs[s["id"]]["raw"] for s in segs)
    turns = []
    for k, s in enumerate(segs):
        b = dirs[s["id"]]["raw"]["beat"]
        prev = dirs[segs[k - 1]["id"]]["raw"]["beat"] if k and segs[k - 1]["chapter_id"] == s["chapter_id"] else None
        first_of_beat = b["index"] > 0 and (prev is None or prev["index"] != b["index"])
        if first_of_beat:
            turns.append(s["id"])
        assert dirs[s["id"]]["raw"].get("support", False) == first_of_beat, (s["sid"], b, prev)
    assert turns                                                                              # calm -> fear is a change of family in the stub's plan
    beat_b = next(dirs[sid]["raw"]["beat"] for sid in turns)
    assert beat_b["emotion"] == "fear" and beat_b["purpose"] == "stub"


def test_same_family_beat_change_is_not_a_turn(client, ctx):
    bid = prepared(client, ctx)
    analysis.director_batch(ctx, bid, 100, client_for(Recorder(emotions=("calm", "neutral"))))     # both "flat": no planned turn to protect
    dirs = directions(ctx, bid)
    assert all("beat" in d["raw"] for d in dirs.values()) and not any(d["raw"].get("support") for d in dirs.values())


def test_direct_scope_directs_the_segment_the_worker_picked_with_its_plan(client, ctx):
    bid = prepared(client, ctx)
    segs = all_segments(ctx, bid)
    ch1 = [s for s in segs if s["ch_idx"] == min(x["ch_idx"] for x in segs)]
    later_scene = max(s["scene_idx"] for s in ch1)
    assert later_scene >= 1
    target = [s for s in ch1 if s["scene_idx"] == later_scene][-1]                          # a segment far from the earliest undirected one
    rec = Recorder()
    r = analysis.direct_scope(ctx, bid, target["id"], client_for(rec))
    dirs = directions(ctx, bid)
    assert target["id"] in dirs and dirs[target["id"]]["raw"]["source"] == "llm" and "beat" in dirs[target["id"]]["raw"]
    assert len(rec.of("Chapter beat sheet")) == 1 and len(rec.of("Direct this scene")) == 1  # the plan first, then its scene
    in_scene = {s["id"] for s in ch1 if s["scene_idx"] == later_scene}
    assert set(dirs) <= in_scene and len(dirs) <= 16 and r["directed"] == len(dirs)
    assert r["remaining"] == len(segs) - len(dirs)
    # the plan was requested before the first scene request
    assert rec.users[0].startswith("Chapter beat sheet") and rec.users[1].startswith("Direct this scene")
    # directing it again is a no-op
    again = analysis.direct_scope(ctx, bid, target["id"], client_for(Recorder()))
    assert again["directed"] == 0


def test_direct_scope_without_an_llm_uses_rules_and_ignores_directed_segments(client, ctx):
    bid = prepared(client, ctx)
    segs = all_segments(ctx, bid)
    r = analysis.direct_scope(ctx, bid, segs[3]["id"], LLMClient(LLMSettings()))
    dirs = directions(ctx, bid)
    assert segs[3]["id"] in dirs and {d["raw"]["source"] for d in dirs.values()} == {"rules"} and r["directed"] == len(dirs) >= 1
    assert r["scene_calls"] == 0 and r["chapter_plans"] == 0


def test_director_batch_without_an_llm_is_unchanged(client, ctx):
    bid = prepared(client, ctx)
    r = analysis.director_batch(ctx, bid, 5, LLMClient(LLMSettings()))
    assert r["directed"] == 5 and r["rules"] == 5 and r["llm_calls"] == 0 and r["scene_calls"] == 0 and r["chapter_plans"] == 0
    assert [d["raw"]["source"] for d in directions(ctx, bid).values()] == ["rules"] * 5


def test_worker_directs_the_scene_it_is_about_to_narrate(client, ctx):
    """Narrating a book whose analysis never ran: the worker directs on demand through direct_scope (plan, then scene, then render)."""
    rec = Recorder()
    llm = {"provider": "openai_compatible", "base_url": "http://127.0.0.1:1/v1", "model": "stub-model"}
    bid = import_semantic(client, director_enabled=True, llm=llm)["id"]
    w = worker_with(ctx)
    w.llm_transport = httpx.MockTransport(rec)
    assert client.post("/api/queue", json={"book_id": bid}).status_code == 200
    w.run_until_idle()
    segs = all_segments(ctx, bid)
    assert all(s["status"] == "passed" for s in segs)
    dirs = directions(ctx, bid)
    assert set(dirs) == {s["id"] for s in segs} and {d["raw"]["source"] for d in dirs.values()} == {"llm"}
    assert len(rec.of("Chapter beat sheet")) == 2 and rec.of("Direct this scene")
