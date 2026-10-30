"""The optional LLM translator: a small model turns the Director's structured direction into a natural performance instruction.
Every LLM here is a TEST DOUBLE (httpx.MockTransport or a local HTTP stub): no real model output is tested, and the engine is the MOCK
engine (synthetic tones). Plans are built inside write transactions and web requests, so the key property is that planning only READS a
stored translation, while analysis.translate_pending asks the model ahead of time with no write lock held."""
from __future__ import annotations

import json
import sqlite3
import threading
from http.server import HTTPServer

import httpx
import pytest
from test_narrative import RecordingEngine, _Stub, all_segments, import_semantic, put_settings, worker_with

from studio.config import DeliverySettings, LLMSettings
from studio.db import row, rows
from studio.narrative import analysis
from studio.narrative import delivery as DL
from studio.narrative import store as N_store
from studio.narrative.llm import LLMClient
from studio.util import loads

PRESET = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16"
DESIGN = "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16"
FINAL = {"emotion": "grief", "intensity": 4, "pacing": "slow", "tension": 3, "delivery": "voice breaking", "pause": "beat"}
LLM = {"provider": "openai_compatible", "base_url": "http://127.0.0.1:1/v1", "model": "stub-model", "translator_model": "stub-tr"}
HEADER = "Translate this direction"
QUOTE_FREE = "voice breaking, barely above a whisper, pausing between phrases"


def ok(reply: dict) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(reply)}, "finish_reason": "stop"}]})


def payload(user: str) -> dict:
    return json.JSONDecoder().raw_decode(user.split("\n", 1)[1])[0]


class Translator:
    """A stub translator endpoint: answers 'Translate this direction.' and records every request body."""

    def __init__(self, status=200, replies=None, probe=None):
        self.bodies: list[dict] = []
        self.users: list[str] = []
        self.status = status
        self.replies = list(replies or [])
        self.probe = probe

    def __call__(self, req):
        body = json.loads(req.content)
        user = body["messages"][1]["content"]
        self.bodies.append(body)
        self.users.append(user)
        if self.probe:
            self.probe()
        if self.status != 200:
            return httpx.Response(self.status, text="down")
        if self.replies:
            return ok(self.replies.pop(0))
        d = payload(user)["direction"]
        return ok({"instruction": f"voice carrying {d['emotion']} at level {d['intensity']}, pausing between phrases"})


def tr_client(handler, **over) -> LLMClient:
    return LLMClient(LLMSettings(**{**LLM, **over}), transport=httpx.MockTransport(handler))


def plan_of(ctx, segment_id: int, **patch):
    with ctx.db.session() as conn:
        seg = row(conn, "SELECT * FROM segments WHERE id=?", (segment_id,))
        book = row(conn, "SELECT * FROM books WHERE id=?", (seg["book_id"],))
        return N_store.plan_for_segment(conn, ctx, seg, book)


def analysed(client, ctx, translator="llm") -> int:
    """A semantic book analysed by the rules Director (no LLM yet); the translator is then switched on in the global settings."""
    bid = import_semantic(client, director_enabled=True)["id"]
    client.post(f"/api/books/{bid}/analyze", json={})
    worker_with(ctx).run_until_idle()
    put_settings(client, {"llm": LLM, "delivery": {"translator": translator}})
    return bid


def narrators(ctx, bid) -> list[dict]:
    with ctx.db.session() as conn:
        nid = row(conn, "SELECT id FROM characters WHERE book_id=? AND kind='narrator'", (bid,))["id"]
    return [s for s in all_segments(ctx, bid) if s["character_id"] == nid]


def direct(client, seg_id, **over):
    fields = {"emotion": "sadness", "intensity": 4, "pacing": "slow", "tension": 3, "delivery": "voice low", **over}
    r = client.patch(f"/api/segments/{seg_id}/narrative", json=fields)
    assert r.status_code == 200, r.text


def template_instruct(ctx, seg_id) -> str:
    """What the template route says for a segment, whatever the translator setting is."""
    p = plan_of(ctx, seg_id)
    book = N_store.book_ns(ctx, {"narrative_json": "{}"})
    cfg = book.delivery.model_copy(update={"translator": "template"})
    return DL.build_instruct(p.direction, p.voice_kind, p.voice.model_id, cfg)[0]


# ------------------------------------------------------------------------------------------------ validation


def test_validation_strips_quotes_and_line_breaks_and_ends_the_sentence():
    out = DL.validate_translation({"instruction": '  Voice "breaking",\n barely\tabove a “whisper”  '}, 160)
    assert out == "Voice breaking, barely above a whisper."
    assert DL.validate_translation("quiet and slow!", 160) == "quiet and slow!"          # a bare string is accepted as well


def test_validation_caps_the_length_at_a_clause_boundary():
    long = "voice breaking, barely above a whisper, pausing between phrases, then trailing off into silence at the end"
    out = DL.validate_translation({"instruction": long}, 60)
    assert len(out) <= 61 and out.endswith(".") and out.startswith("voice breaking, barely above a whisper")
    assert "silence" not in out and not out.endswith(",.")
    assert DL.validate_translation({"instruction": long}, 400) == long + "."


def test_validation_leak_guard_removes_runs_copied_from_the_manuscript():
    line = "I do not know where she went after the funeral bell rang"
    out = DL.validate_translation({"instruction": "voice low, I do not know where she went after, then a pause"}, 160, [line, "context"])
    assert "where she went after" not in out and "voice low" in out
    with pytest.raises(ValueError):
        DL.validate_translation({"instruction": "I do not know where she went after"}, 160, [line])


@pytest.mark.parametrize("raw", [{}, {"instruction": ""}, {"instruction": "   \n "}, {"instruction": '""'}, {"instruction": 5}, {"instruction": None},
                                 {"instruction": ["voice low"]}, None, 7, {"other": "voice low"}])
def test_validation_rejects_empty_or_non_string(raw):
    with pytest.raises(ValueError):
        DL.validate_translation(raw, 160)


# ------------------------------------------------------------------------------------------------ prompt and key


def test_prompt_has_the_header_and_carries_the_direction_and_context():
    user = DL.build_translator_prompt(FINAL, "Elena Marsh", "design", "A stub scene.", "sadness, slow", "I do not know.", hints="soft spoken", max_chars=120)
    assert user.startswith(HEADER)
    p = payload(user)
    assert p["direction"] == {"emotion": "grief", "intensity": 4, "pacing": "slow", "tension": 3, "pause_after": "beat", "delivery_note": "voice breaking"}
    assert p["speaker"] == "Elena Marsh" and p["voice_kind"] == "design" and p["max_characters"] == 120 and p["user_instructions"] == "soft spoken"
    assert p["context_for_understanding_only"]["line_excerpt"] == "I do not know."
    assert "JSON" in DL.TRANSLATOR_SYSTEM_PROMPT and "quote" in DL.TRANSLATOR_SYSTEM_PROMPT


def test_cache_key_depends_on_the_direction_and_voice_only():
    base = DL.translate_cache_key(FINAL, "preset", "openai_compatible:m", 160, "Narrator", "")
    assert base.startswith("tr:") and base == DL.translate_cache_key(dict(FINAL), "preset", "openai_compatible:m", 160, "Narrator", "")
    noise = {**FINAL, "continuity": "held", "confidence": {"overall": 0.2}, "facts": ["x"], "segment_id": "c1-s9", "needs_review": True, "source": "llm"}
    assert DL.translate_cache_key(noise, "preset", "openai_compatible:m", 160, "Narrator", "") == base         # nothing about the text or the neighbours
    # every character speaks the same way to the key: only narrator vs character counts
    assert DL.translate_cache_key(FINAL, "preset", "openai_compatible:m", 160, "Hale", "") == DL.translate_cache_key(FINAL, "preset", "openai_compatible:m", 160, "Elena", "")
    assert DL.translate_cache_key(FINAL, "preset", "openai_compatible:m", 160, "Hale", "") != base
    changed = [dict(FINAL, emotion="fear"), dict(FINAL, intensity=2), dict(FINAL, pacing="fast"), dict(FINAL, tension=1), dict(FINAL, delivery="whispering"), dict(FINAL, pause="long")]
    keys = {DL.translate_cache_key(f, "preset", "openai_compatible:m", 160, "Narrator", "") for f in changed}
    keys |= {DL.translate_cache_key(FINAL, "design", "openai_compatible:m", 160, "Narrator", ""), DL.translate_cache_key(FINAL, "preset", "openai_compatible:other", 160, "Narrator", ""),
             DL.translate_cache_key(FINAL, "preset", "openai_compatible:m", 100, "Narrator", ""), DL.translate_cache_key(FINAL, "preset", "openai_compatible:m", 160, "Narrator", "soft")}
    assert len(keys) == len(changed) + 4 and base not in keys


# ------------------------------------------------------------------------------------------------ build_instruct


def test_build_instruct_uses_the_translation_for_preset_and_wraps_it_for_design():
    cfg = DeliverySettings(translator="llm")
    text, info = DL.build_instruct(FINAL, "preset", PRESET, cfg, QUOTE_FREE)
    assert text == QUOTE_FREE + "." and info["applied"] and info["reason"] == "LLM-translated instruction"
    dtext, dinfo = DL.build_instruct(FINAL, "design", DESIGN, cfg, QUOTE_FREE)
    assert dtext == f"For this line: {QUOTE_FREE}." and dinfo["reason"] == "LLM-translated instruction"


def test_build_instruct_falls_back_to_the_template_and_says_so():
    cfg = DeliverySettings(translator="llm")
    tmpl, _ = DL.build_instruct(FINAL, "preset", PRESET, DeliverySettings())
    text, info = DL.build_instruct(FINAL, "preset", PRESET, cfg)
    assert text == tmpl and info["applied"] and "not available yet" in info["reason"] and "template" in info["reason"]
    bad, binfo = DL.build_instruct(FINAL, "preset", PRESET, cfg, '""')                                  # an unusable stored value is not used
    assert bad == tmpl and "template" in binfo["reason"]


def test_build_instruct_ignores_a_translation_when_the_template_route_is_selected():
    tmpl, info = DL.build_instruct(FINAL, "preset", PRESET, DeliverySettings())
    text, info2 = DL.build_instruct(FINAL, "preset", PRESET, DeliverySettings(translator="template"), QUOTE_FREE)
    assert text == tmpl and info2 == info and info["reason"] == "passed as the model's instruct input"


def test_build_instruct_translation_never_reaches_voices_without_an_instruction_input():
    cfg = DeliverySettings(translator="llm")
    assert DL.build_instruct(FINAL, "clone", "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16", cfg, QUOTE_FREE)[0] is None
    assert DL.build_instruct(FINAL, "preset", "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-bf16", cfg, QUOTE_FREE)[0] is None
    assert DL.build_instruct(FINAL, "preset", PRESET, DeliverySettings(translator="llm", custom_voice="off"), QUOTE_FREE)[0] is None
    neutral = {"emotion": "neutral", "intensity": 1, "pacing": "normal", "delivery": ""}
    assert DL.build_instruct(neutral, "preset", PRESET, cfg, QUOTE_FREE)[0] is None                    # nothing to say, nothing to translate


def test_build_instruct_caps_a_long_translation():
    cfg = DeliverySettings(translator="llm", max_instruct_chars=60)
    text, _ = DL.build_instruct(FINAL, "preset", PRESET, cfg, "voice breaking, barely above a whisper, pausing between phrases, then trailing off")
    assert len(text) <= 61


# ------------------------------------------------------------------------------------------------ translate_llm


def run_translate(client, cache=None, **kw):
    cache = {} if cache is None else cache
    stats: dict = {}
    out = DL.translate_llm(client, FINAL, "preset", cache_get=cache.get, cache_put=lambda k, v, m: cache.__setitem__(k, v), stats=stats, **kw)
    return out, cache, stats


def test_translate_llm_asks_once_with_thinking_off_json_mode_and_the_translator_model():
    rec = Translator(replies=[{"instruction": QUOTE_FREE}])
    out, cache, stats = run_translate(tr_client(rec), speaker="Narrator", scene_summary="A scene.", text_excerpt="Her voice cracked.")
    assert out == QUOTE_FREE + "." and stats == {"translations": 1}
    body = rec.bodies[0]
    assert body["model"] == "stub-tr" and body["chat_template_kwargs"] == {"enable_thinking": False} and body["response_format"] == {"type": "json_object"}
    assert body["messages"][0]["content"] == DL.TRANSLATOR_SYSTEM_PROMPT and rec.users[0].startswith(HEADER)
    assert list(cache) and next(iter(cache)).startswith("tr:") and next(iter(cache.values()))["instruction"] == out
    again, _c, stats2 = run_translate(tr_client(rec), cache)                                           # cached: no second request
    assert again == out and len(rec.bodies) == 1 and stats2 == {"translation_cache_hits": 1}


def test_translate_llm_repairs_once_then_gives_up_without_caching_the_failure():
    rec = Translator(replies=[{"instruction": ""}, {"instruction": QUOTE_FREE}])
    out, cache, _s = run_translate(tr_client(rec))
    assert out == QUOTE_FREE + "." and len(rec.bodies) == 2 and "was rejected" in rec.users[1]
    bad = Translator(replies=[{"instruction": ""}, {"instruction": ""}, {"instruction": QUOTE_FREE}])
    out2, cache2, stats = run_translate(tr_client(bad))
    assert out2 is None and len(bad.bodies) == 2 and cache2 == {} and stats == {"translation_failures": 1}
    down = Translator(status=503)
    out3, cache3, stats3 = run_translate(tr_client(down))
    assert out3 is None and cache3 == {} and stats3 == {"translation_failures": 1}


def test_translate_llm_without_a_translator_model_makes_no_request():
    rec = Translator()
    out, cache, stats = run_translate(tr_client(rec, translator_model=""))
    assert out is None and not rec.bodies and cache == {} and stats == {}


# ------------------------------------------------------------------------------------------------ plan_for_segment


def test_plan_uses_a_stored_translation_and_falls_back_with_a_reason(client, ctx, monkeypatch):
    bid = analysed(client, ctx)
    seg = narrators(ctx, bid)[0]
    direct(client, seg["id"])
    tmpl = template_instruct(ctx, seg["id"])
    assert tmpl
    boom = lambda *a, **k: pytest.fail("planning made an HTTP request")  # noqa: E731
    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(LLMClient, "chat", boom)
    missing = plan_of(ctx, seg["id"])
    assert missing.instruct == tmpl and missing.delivery_applied and "not available yet" in missing.delivery_reason and "template" in missing.delivery_reason
    monkeypatch.undo()
    rec = Translator()
    r = analysis.translate_pending(ctx, bid, [seg["id"]], tr_client(rec))
    assert r == {"translations": 1, "translation_cache_hits": 0, "translation_failures": 0} and len(rec.bodies) == 1
    monkeypatch.setattr(httpx.Client, "send", boom)
    monkeypatch.setattr(LLMClient, "chat", boom)
    got = plan_of(ctx, seg["id"])
    assert got.instruct == "voice carrying sadness at level 4, pausing between phrases." and got.delivery_reason == "LLM-translated instruction"
    assert got.gen_key != missing.gen_key                                                               # the instruction is part of the take's identity
    monkeypatch.undo()
    # the template route (what an A/B comparison passes) ignores the stored translation, and gets its original key back
    put_settings(client, {"delivery": {"translator": "template"}})
    back = plan_of(ctx, seg["id"])
    assert back.instruct == tmpl and back.gen_key == missing.gen_key and back.delivery_reason == "passed as the model's instruct input"


def test_plan_reads_the_translation_with_the_ns_it_is_given(client, ctx, monkeypatch):
    """The A/B tool passes a settings patch; the lookup must follow ns.delivery.translator, not a global."""
    bid = analysed(client, ctx)
    seg = narrators(ctx, bid)[0]
    direct(client, seg["id"])
    analysis.translate_pending(ctx, bid, [seg["id"]], tr_client(Translator()))
    on, off = plan_of(ctx, seg["id"]), None
    put_settings(client, {"delivery": {"translator": "template"}})
    off = plan_of(ctx, seg["id"])
    assert on.instruct != off.instruct and "voice carrying" in on.instruct and "voice carrying" not in off.instruct


def test_a_voice_without_an_instruction_input_is_never_translated(client, ctx):
    bid = analysed(client, ctx)
    seg = narrators(ctx, bid)[0]
    direct(client, seg["id"])
    with ctx.db.tx() as conn:
        conn.execute("UPDATE voice_versions SET model_id=? WHERE id=(SELECT voice_version_id FROM books WHERE id=?)", ("mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-bf16", bid))
    rec = Translator()
    r = analysis.translate_pending(ctx, bid, [seg["id"]], tr_client(rec))
    assert r == {"translations": 0, "translation_cache_hits": 0, "translation_failures": 0} and not rec.bodies
    assert plan_of(ctx, seg["id"]).instruct is None


# ------------------------------------------------------------------------------------------------ translate_pending


def test_pending_asks_only_for_misses_and_identical_directions_share_one_request(client, ctx):
    bid = analysed(client, ctx)
    a, b, c = narrators(ctx, bid)[:3]
    for s in (a, b):
        direct(client, s["id"])                                                                        # identical directions
    direct(client, c["id"], intensity=2)
    rec = Translator()
    r = analysis.translate_pending(ctx, bid, [a["id"], b["id"], c["id"]], tr_client(rec))
    assert len(rec.bodies) == 2 and r["translations"] == 2 and r["translation_cache_hits"] == 0 and r["translation_failures"] == 0
    assert plan_of(ctx, a["id"]).instruct == plan_of(ctx, b["id"]).instruct != plan_of(ctx, c["id"]).instruct
    again = analysis.translate_pending(ctx, bid, [a["id"], b["id"], c["id"]], tr_client(rec))
    assert len(rec.bodies) == 2 and again == {"translations": 0, "translation_cache_hits": 2, "translation_failures": 0}
    # one more segment with a direction nobody has translated: only that one costs a request
    d = narrators(ctx, bid)[3]
    direct(client, d["id"], emotion="fear")
    assert analysis.translate_pending(ctx, bid, [a["id"], d["id"]], tr_client(rec))["translations"] == 1 and len(rec.bodies) == 3


def test_pending_does_nothing_unless_the_llm_translator_is_selected_and_configured(client, ctx):
    bid = analysed(client, ctx, translator="template")
    seg = narrators(ctx, bid)[0]
    direct(client, seg["id"])
    rec = Translator()
    assert analysis.translate_pending(ctx, bid, [seg["id"]], tr_client(rec))["translations"] == 0 and not rec.bodies
    put_settings(client, {"delivery": {"translator": "llm"}, "llm": {"translator_model": ""}})
    assert analysis.translate_pending(ctx, bid, [seg["id"]], tr_client(rec, translator_model=""))["translations"] == 0 and not rec.bodies
    put_settings(client, {"director_enabled": False, "llm": {"translator_model": "stub-tr"}})
    assert analysis.translate_pending(ctx, bid, [seg["id"]], tr_client(rec))["translations"] == 0 and not rec.bodies


def test_failures_fall_back_to_the_template_are_counted_and_stop_after_a_streak(client, ctx):
    bid = analysed(client, ctx)
    segs = narrators(ctx, bid)[:5]
    for k, s in enumerate(segs):
        direct(client, s["id"], intensity=1 + k)                                                       # five distinct directions
    rec = Translator(status=503)
    r = analysis.translate_pending(ctx, bid, [s["id"] for s in segs], tr_client(rec))
    assert r["translations"] == 0 and r["translation_failures"] == analysis.TRANSLATE_FAIL_STREAK == len(rec.bodies)   # the rest is left for later
    for s in segs:
        p = plan_of(ctx, s["id"])
        assert p.instruct == template_instruct(ctx, s["id"]) and p.delivery_applied and "template" in p.delivery_reason
    with ctx.db.session() as conn:
        assert not rows(conn, "SELECT key FROM director_cache WHERE key LIKE 'tr:%'")                   # failures are not cached
    ok_rec = Translator()                                                                               # a later run retries and succeeds
    assert analysis.translate_pending(ctx, bid, [s["id"] for s in segs], tr_client(ok_rec))["translations"] == len(segs)


def test_no_write_lock_is_held_while_the_translator_is_asked(client, ctx):
    bid = analysed(client, ctx)
    segs = narrators(ctx, bid)[:2]
    for k, s in enumerate(segs):
        direct(client, s["id"], intensity=2 + k)
    probes = []

    def probe():
        other = sqlite3.connect(str(ctx.db.path), timeout=0, isolation_level=None)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            probes.append("free")
        except sqlite3.OperationalError:
            probes.append("locked")
        finally:
            other.close()

    analysis.translate_pending(ctx, bid, [s["id"] for s in segs], tr_client(Translator(probe=probe)))
    assert probes == ["free", "free"]


def test_pending_prompt_context_and_leak_guard_use_the_segment_and_its_surroundings(client, ctx):
    bid = analysed(client, ctx)
    seg = max(narrators(ctx, bid), key=lambda s: len(s["original_text"]))
    direct(client, seg["id"])
    spoken = plan_of(ctx, seg["id"]).text
    words = " ".join(seg["original_text"].split()[:7])
    rec = Translator(replies=[{"instruction": f"voice low, {words}, slow"}])
    analysis.translate_pending(ctx, bid, [seg["id"]], tr_client(rec))
    p = payload(rec.users[0])
    assert p["speaker"] == "Narrator" and p["voice_kind"] == "preset" and p["context_for_understanding_only"]["line_excerpt"] == spoken[:200]
    stored = plan_of(ctx, seg["id"]).instruct
    assert stored and words not in stored                                                               # quoted manuscript words were removed


# ------------------------------------------------------------------------------------------------ with the Director and the worker


def director_recorder():
    from test_director_v2 import Recorder

    class Rec(Recorder):
        def __call__(self, req):
            body = json.loads(req.content)
            user = body["messages"][1]["content"]
            if not user.startswith(HEADER):
                return super().__call__(req)
            self.requests.append(body)
            self.users.append(user)
            d = payload(user)["direction"]
            return ok({"instruction": f"voice carrying {d['emotion']} at level {d['intensity']}, pausing between phrases"})

    return Rec(scene_over={"emotion": "fear", "intensity": 3})


def test_director_batch_translates_what_it_directed_and_counts_it_in_the_stage_stats(client, ctx):
    from test_director_v2 import client_for, stage_stats

    bid = analysed(client, ctx)
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM directions WHERE book_id=?", (bid,))
        conn.execute("DELETE FROM director_cache WHERE 1=1")
    rec = director_recorder()
    r = analysis.director_batch(ctx, bid, 100, client_for(rec, translator_model="stub-tr"))
    n_tr = len(rec.of(HEADER))
    assert r["directed"] > 3 and 0 < n_tr <= r["directed"] and r["translations"] == n_tr and r["translation_failures"] == 0
    st = stage_stats(client, bid)
    assert st["translations"] == n_tr and st["translation_cache_hits"] == 0 and st["translation_failures"] == 0
    seg = narrators(ctx, bid)[0]
    assert plan_of(ctx, seg["id"]).instruct.startswith("voice carrying fear at level 3")
    # the translator is asked after the Director has stored its windows (never inside the window's write transaction)
    assert all(u.startswith(HEADER) for u in rec.users[-n_tr:])


def test_director_batch_leaves_the_translator_alone_by_default(client, ctx):
    from test_director_v2 import client_for

    bid = analysed(client, ctx, translator="template")
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM directions WHERE book_id=?", (bid,))
        conn.execute("DELETE FROM director_cache WHERE 1=1")
    rec = director_recorder()
    r = analysis.director_batch(ctx, bid, 100, client_for(rec, translator_model="stub-tr"))
    assert not rec.of(HEADER) and r["translations"] == 0 and r["translation_cache_hits"] == 0 and r["translation_failures"] == 0


def test_direct_scope_translates_only_the_segment_about_to_be_narrated(client, ctx):
    from test_director_v2 import client_for

    bid = analysed(client, ctx)
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM directions WHERE book_id=?", (bid,))
        conn.execute("DELETE FROM director_cache WHERE 1=1")
    target = narrators(ctx, bid)[2]
    rec = director_recorder()
    r = analysis.direct_scope(ctx, bid, target["id"], client_for(rec, translator_model="stub-tr"))
    assert r["directed"] >= 1 and len(rec.of(HEADER)) == 1 and r["translations"] == 1
    assert plan_of(ctx, target["id"]).delivery_reason == "LLM-translated instruction"


def test_worker_translates_the_picked_segment_before_it_is_planned(client, ctx):
    bid = analysed(client, ctx)                                                                          # analysed, so directions exist and only the translator is missing
    rec = director_recorder()
    eng = RecordingEngine()
    w = worker_with(ctx, eng)
    w.llm_transport = httpx.MockTransport(rec)
    assert client.post("/api/queue", json={"book_id": bid}).status_code == 200
    w.run_until_idle()
    segs = all_segments(ctx, bid)
    assert all(s["status"] == "passed" for s in segs) and rec.of(HEADER) and not rec.of("Direct this scene")
    sent = [c["instruct"] for c in eng.log if c["instruct"]]
    assert sent and all(i.startswith("voice carrying") for i in sent)
    with ctx.db.session() as conn:
        used = [r["instruct_used"] for r in rows(conn, "SELECT instruct_used FROM attempts WHERE instruct_used IS NOT NULL")]
    assert used and all(u.startswith("voice carrying") for u in used)


def test_worker_narrates_with_the_template_when_the_translator_is_down(client, ctx):
    bid = analysed(client, ctx)
    rec = Translator(status=503)
    eng = RecordingEngine()
    w = worker_with(ctx, eng)
    w.llm_transport = httpx.MockTransport(rec)
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    assert all(s["status"] == "passed" for s in all_segments(ctx, bid))
    sent = [c["instruct"] for c in eng.log if c["instruct"]]
    assert sent and not any("voice carrying" in i for i in sent)                                        # the template narrated every line
    assert 0 < len(rec.bodies) <= analysis.TRANSLATE_FAIL_STREAK                                        # then the worker stops asking for a while


def test_end_to_end_against_a_stub_http_endpoint(client, ctx):
    _Stub.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        llm = {"provider": "openai_compatible", "base_url": f"http://127.0.0.1:{srv.server_port}/v1", "model": "stub", "translator_model": "stub-tr"}
        bid = import_semantic(client, director_enabled=True, llm=llm, delivery={"translator": "llm"})["id"]
        client.post(f"/api/books/{bid}/analyze", json={})
        worker_with(ctx).run_until_idle()
        st = client.get(f"/api/books/{bid}/narrative").json()["stages"]["director"]["stats"]
        assert st["translations"] >= 1 and st["translation_failures"] == 0
        assert any(u.startswith(HEADER) for u in _Stub.seen)
        eng = RecordingEngine()
        assert client.post("/api/queue", json={"book_id": bid}).status_code == 200
        worker_with(ctx, eng).run_until_idle()
        sent = [c["instruct"] for c in eng.log if c["instruct"]]
        assert sent and "voice low and unsteady, pausing between phrases." in sent
        with ctx.db.session() as conn:
            assert any(loads(r["response_json"], {}).get("instruction") for r in rows(conn, "SELECT response_json FROM director_cache WHERE key LIKE 'tr:%'"))
    finally:
        srv.shutdown()
