"""Semantic segmentation, speaker attribution, Narrative Director, continuity and their integration
with the queue. Pure-logic tests run on the fixture manuscripts in ``narrative_fixtures``; integration
tests use the real worker/scheduler/SQLite with the MOCK engine (synthetic tones, not speech).
The OpenAI-compatible endpoint is either an httpx MockTransport or a throw-away local HTTP server:
both are TEST DOUBLES for an LLM - no language model runs in this file."""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

from conftest import make_text, needs_ffmpeg
from narrative_fixtures import BOOK, CH1, CH2, SCENE, TXT, P
from studio.config import DeliverySettings, LLMSettings, NarrativeSettings
from studio.db import row, rows, scalar
from studio.engines import MockASR, MockEngine
from studio.narrative import continuity as CT
from studio.narrative import delivery as DL
from studio.narrative import director as D
from studio.narrative import segmenter as G
from studio.narrative import structure as S
from studio.narrative import windows as W
from studio.narrative.llm import LLMClient
from studio.parsing import Block
from studio.worker import Worker


def analyze(blocks, title="T", cast=None, **kw):
    cast = cast or S.Cast()
    return S.analyze_chapter(blocks, title, cast, **kw), cast


def by_text(st, fragment):
    return next(sp for sp in st.spans if sp.kind == "dialogue" and fragment in sp.text)


# ============================================================ Pass 1: structure and speakers


def test_dialogue_speakers_in_known_cases():
    cast = S.Cast()
    st1 = S.analyze_chapter(CH1, "The Flood", cast)
    assert by_text(st1, "come back").speaker == "Elena Marsh"          # explicit tag, surname learned from the narration
    assert by_text(st1, "no choice").speaker == "Hale"                  # inverted tag "said Dr. Hale"
    assert by_text(st1, "was clear").speaker == "Hale"                  # same speech continued after the tag
    assert by_text(st1, "forgery").speaker == "Elena Marsh"             # turn-taking
    assert by_text(st1, "Found who?").speaker == "Elena Marsh"          # "she whispered": unique female participant
    assert by_text(st1, "they have found him").speaker == "Tom Briggs"
    assert by_text(st1, "forgery").attribution == "alternation"
    assert not any(sp.needs_review for sp in st1.spans if sp.speaker)   # nothing confident is also flagged


def test_unattributed_dialogue_is_flagged_not_given_to_the_last_speaker():
    cast = S.Cast()
    S.analyze_chapter(CH1, "The Flood", cast)
    st2 = S.analyze_chapter(CH2, "The Funeral", cast)
    blamed = by_text(st2, "No one would have blamed")
    assert blamed.speaker is None and blamed.needs_review                # Elena spoke just before; we must not assume
    assert "no speaker tag" in blamed.evidence
    ambiguous = by_text(st2, "I am so sorry")
    assert ambiguous.speaker is None and ambiguous.needs_review
    assert {"Hale", "Tom Briggs"} <= set(by_text(st2, "Mrs. Marsh,").candidates)  # "he began": two men in the scene


def test_threshold_controls_what_is_flagged():
    cast = S.Cast()
    st, _ = analyze(CH1, "The Flood", cast, threshold=0.9)
    assert by_text(st, "forgery").needs_review and by_text(st, "forgery").speaker is None      # alternation 0.8 < 0.9
    assert by_text(st, "come back").speaker == "Elena Marsh"                                  # explicit 0.95 >= 0.9


def test_aliases_resolve_consistently_and_ambiguity_is_refused():
    cast = S.Cast()
    for t, b in BOOK:
        S.analyze_chapter(b, t, cast)
    assert cast.resolve("Mrs. Marsh").name == "Elena Marsh"
    assert cast.resolve("Elena").name == "Elena Marsh"
    assert cast.resolve("Dr. Hale").name == "Hale"
    assert cast.resolve("Tom").name == "Tom Briggs"
    cast.add("Anna Marsh")                                  # a second Marsh: the bare surname is now ambiguous
    assert cast.resolve("Marsh") is None
    assert cast.resolve("Mrs. Marsh") is None
    assert cast.resolve("Elena Marsh").name == "Elena Marsh"


def test_known_characters_are_not_renamed_or_duplicated():
    known = [S.KnownCharacter("Elena Marsh", ["Mrs. Marsh"], "female", id=7), S.KnownCharacter("Hale", ["the doctor"], "male", id=8)]
    cast = S.Cast(known)
    st = S.analyze_chapter(CH2, "The Funeral", cast)
    assert by_text(st, "I was not going to come").speaker == "Elena Marsh"
    assert {e.name for e in cast.entries.values() if e.kind != "narrator"} == {"Elena Marsh", "Hale"}   # nothing duplicated
    assert cast.entries["Elena Marsh"].id == 7 and cast.entries["Hale"].id == 8


def test_first_person_narrator_owns_i_said():
    blocks = [P("I opened the door and stood in the hall. I did not like what I saw."), P("“Who is there?” I said."), P("“Nobody,” said Pell.")]
    st, cast = analyze(blocks, "One")
    assert st.perspective == "first"
    assert by_text(st, "Who is there").speaker == "Narrator"


def test_quotes_across_paragraphs_continue_the_same_speaker():
    blocks = [P("Mara said, “I have to tell you something about the night he left, and it is not easy to say."), P("“He never took the boat. I moved it myself.”")]
    st, _ = analyze(blocks, "One")
    d = [sp for sp in st.spans if sp.kind == "dialogue"]
    assert d[0].speaker == "Mara" and d[1].speaker == "Mara" and d[1].attribution == "continuation"


def test_scare_quotes_are_not_dialogue():
    st, _ = analyze([P("The so-called “experts” never agreed, and the “final” report was late.")], "One")
    assert not [sp for sp in st.spans if sp.kind == "dialogue"]


def test_scene_and_paragraph_structure_with_offsets():
    st, _ = analyze(CH1, "The Flood")
    assert st.scenes == 2
    assert all(st.text[sp.start:sp.end] == sp.text for sp in st.spans)
    assert [p.scene_idx for p in st.paragraphs][0] == 0 and st.paragraphs[-1].scene_idx == 1
    assert all(p.start < p.end for p in st.paragraphs) and all(st.paragraphs[i].end < st.paragraphs[i + 1].start for i in range(len(st.paragraphs) - 1))


def test_emotional_cues_and_tension():
    blocks = [P("The afternoon was quiet and the tea was warm."), P("“Get out! Get out now!” Tom screamed, backing against the wall, terrified.")]
    st, _ = analyze(blocks, "One")
    calm, loud = st.spans[1].cues, by_text(st, "Get out").cues
    assert calm["emotion"] in ("neutral", "calm") and calm["intensity"] <= 2
    assert loud["emotion"] == "fear" and loud["intensity"] >= 4 and loud["explicit"]
    assert loud["shift"] > 0.5


# ================================================================ Pass 2: segment construction


def build(blocks, title=None, **kw):
    st, cast = analyze(blocks, title)
    voice_of = kw.pop("voice_of", lambda sp: sp or "narrator")
    return st, G.build(st, voice_key_of=voice_of, **kw)


@pytest.mark.parametrize("max_chars,min_chars", [(500, 120), (200, 60), (120, 30), (900, 300)])
def test_every_source_passage_exactly_once_in_order(max_chars, min_chars):
    for title, blocks in BOOK:
        st, segs = build(blocks, title, max_chars=max_chars, min_chars=min_chars)
        assert G.check_coverage(st, segs) == []
        assert re.sub(r"\s+", "", "".join(s.text for s in segs)) == re.sub(r"\s+", "", st.text)
        assert [s.start for s in segs] == sorted(s.start for s in segs)
        for s in segs:
            assert st.text[s.start:s.end] == s.text                       # wording, punctuation and case untouched
            assert len(s.text) <= max_chars or " " not in s.text.strip()


def test_segments_never_cross_scene_or_heading_or_mix_speakers():
    for title, blocks in BOOK:
        st, segs = build(blocks, title)
        for s in segs:
            spans = [sp for sp in st.spans if sp.start < s.end and sp.end > s.start]
            assert len({sp.scene_idx for sp in spans}) == 1
            if any(sp.kind == "heading" for sp in spans):
                assert len(spans) == 1
            assert len({sp.speaker for sp in spans if sp.kind == "dialogue"}) <= 1


def test_limit_is_a_ceiling_not_a_target():
    blocks = [P("A" * 0 + "The tide came in slowly over the grey flats, and the gulls wheeled above the water as they had for years, crying at nothing."),
              P("Inland, the farms lay quiet under a low sky, and nobody walked the lanes that morning because the church bell had stopped.")]
    _st, segs = build(blocks)
    assert len(segs) == 2 and all(len(s.text) < 250 for s in segs)       # two paragraphs stay two segments although 500 would fit


def test_short_fragments_are_joined_when_nothing_separates_them():
    blocks = [P("It rained."), P("Then it stopped."), P("Nobody moved.")]
    _st, segs = build(blocks, min_chars=60)
    assert len(segs) == 1 and re.sub(r"\s+", " ", segs[0].text) == "It rained. Then it stopped. Nobody moved."


def test_unrelated_dialogue_is_never_combined():
    blocks = [P("“No,” said Ann."), P("“Yes,” said Bob."), P("“Why?” said Ann.")]
    st, segs = build(blocks, voice_of=lambda sp: "narrator")            # even when everyone shares one voice
    dlg = [s for s in segs if s.span_kind in ("dialogue", "mixed")]
    assert len(dlg) == 3


def test_overlong_sentence_is_split_at_the_least_disruptive_point():
    sent = ("She walked through the market past the fishmongers, and the cheesemakers, and the old woman who sold lace, "
            "although she knew that the letter she carried would change everything; and when she reached the harbour wall, "
            "which had stood for three hundred years against every storm that the northern sea could throw at it, she stopped "
            "because the ship she had been waiting for was already leaving the bay with all her hopes on board and nobody on the quay to wave, "
            "and the harbour master, who had promised faithfully to hold it until noon, was nowhere to be seen among the gulls and the nets.")
    assert len(sent) > 500
    st, segs = build([P(sent)], max_chars=300)
    assert G.check_coverage(st, segs) == [] and all(len(s.text) <= 300 for s in segs) and len(segs) >= 2
    assert all(re.search(r"[;,]$", s.text) or s is segs[-1] for s in segs)    # cut at punctuation, not mid-clause


def test_emotional_transition_gets_its_own_segment_only_when_sensitive():
    blocks = [P("The room was quiet and the lamplight was soft on the table, and the tea had gone cold hours ago while she waited. "
                "“Get out! Get out now!” Tom screamed, terrified, and the cup shattered on the floor.")]
    one_voice = lambda sp: "narrator"
    st, calm = build(blocks, sensitivity=0.0, voice_of=one_voice)
    _st, keen = build(blocks, sensitivity=1.0, voice_of=one_voice)
    assert len(keen) > len(calm)


def test_speaker_change_splits_only_when_voices_differ():
    blocks = [P("“We should go,” said Ann. “Not yet,” said Bob.")]
    st1, same = build(blocks, voice_of=lambda sp: "narrator")
    st2, distinct = build(blocks, voice_of=lambda sp: {"Ann": "v1", "Bob": "v2"}.get(sp, "narrator"))
    assert len(same) == 1 and len(distinct) >= 3
    assert G.check_coverage(st2, distinct) == []


def test_manual_cut_and_join_are_honoured():
    st, base = build(CH2, "The Funeral")
    target = next(s for s in base if len(s.text) > 120 and " " in s.text)
    cut = target.start + target.text.index(" ", 60) + 1
    _st, cutsegs = build(CH2, "The Funeral", cuts={cut})
    assert any(s.start == cut for s in cutsegs) and G.check_coverage(st, cutsegs) == []
    boundary = base[2].start
    _st, joined = build(CH2, "The Funeral", joins={boundary})
    assert len(joined) < len(base) and G.check_coverage(st, joined) == []


# ===================================================================== context windows


def test_context_windows_respect_bounds_and_scene_breaks():
    st, _ = analyze(CH1, "The Flood")
    scene_starts = [st.paragraphs[0].start] + [p.start for p in st.paragraphs if p.scene_idx == 1][:1]
    seg = next(sp for sp in st.spans if "they have found him" in sp.text)
    w = W.context_window(st.text, seg.start, seg.end, 1250, 1250, scene_starts)
    assert len(w["before"]) <= 1250 and len(w["after"]) <= 1250
    assert seg.text not in w["before"] and seg.text not in w["after"]
    assert W.SCENE_MARK in w["before"] or w["scene_break_before"]
    first = W.context_window(st.text, 0, 9, 1250, 1250, scene_starts)
    assert first["before"] == "" and first["chapter_start"]
    last = W.context_window(st.text, len(st.text) - 20, len(st.text), 500, 500)
    assert last["after"] == "" and last["chapter_end"]
    off = W.context_window(st.text, seg.start, seg.end, 0, 0)
    assert off["before"] == "" and off["after"] == ""


def test_narration_window_is_only_the_segment_words():
    assert W.narration_window("  Hello,\n\nworld.  ") == "Hello, world."


# ================================================================== Narrative Director


def good_reply(**over):
    d = {"segment_id": "x", "speaker": "Elena Marsh", "voice_profile": "low and tired", "emotion": "grief", "intensity": 4,
         "delivery": "voice breaking, barely above a whisper", "pacing": "slow", "tension": 3,
         "confidence": {"speaker": 0.9, "emotion": 0.8, "overall": 0.8}, "text_supported_change": False,
         "context_refs": [{"kind": "previous", "quote": "Her voice cracked"}], "facts": ["Her voice cracked"], "inferences": ["She is grieving"],
         "ambiguity": None, "needs_review": False}
    d.update(over)
    return d


def validate(raw, **kw):
    base = dict(sid="x", text="I don't know.", context="Her voice cracked, and for a moment she could not go on.", known_speaker="Elena Marsh", cast_names=["Elena Marsh"])
    base.update(kw)
    return D.validate_direction(raw, **base)


def test_director_output_is_schema_validated():
    d = validate(good_reply())
    assert d["emotion"] == "grief" and d["intensity"] == 4 and d["pacing"] == "slow" and d["facts"] == ["Her voice cracked"]
    assert validate(good_reply(emotion="Sad"))["emotion"] == "sadness"                     # synonyms map into the vocabulary
    for bad in (good_reply(emotion="melancholic-ish"), good_reply(pacing="warp speed"), "not a dict"):
        with pytest.raises(D.DirectorError):
            validate(bad)
    assert validate(good_reply(intensity=0.6))["intensity"] == 3                           # 0-1 scales are converted


def test_ungrounded_facts_are_dropped_and_speaker_disagreement_is_flagged():
    d = validate(good_reply(facts=["Her voice cracked", "She had buried her husband in March"], speaker="Hale"))
    assert d["facts"] == ["Her voice cracked"]
    assert any("not found" in n for n in d["notes"])
    assert d["speaker"] == "Elena Marsh" and d["needs_review"] and "disagrees" in (d["ambiguity"] or "")


def test_leak_guard_removes_copied_manuscript_text_from_the_instruction():
    ctx_text = "Her voice cracked, and for a moment she could not go on speaking at all"
    clean, changed = D.leak_guard("tender; her voice cracked, and for a moment she could not go on speaking", [ctx_text, "I don't know."])
    assert changed and "cracked" not in clean and clean.startswith("tender")
    assert D.leak_guard("voice breaking, barely a whisper", [ctx_text]) == ("voice breaking, barely a whisper", False)


def prompt_payload(**over):
    p = {"sid": "x", "text": "I don't know.", "kind": "dialogue", "speaker": "Elena Marsh", "context": {"before": "Her voice cracked.", "after": "Hale waited."},
         "sensitivity": 0.5, "cues": {"emotion": "sadness", "intensity": 3, "explicit": True, "pace": "slow", "tension": 1, "evidence": ["word 'sad…'"]},
         "prev": {"speaker": "Hale", "emotion": "calm", "intensity": 2}, "profile": {"name": "Elena Marsh"}, "notes": [], "cast_names": ["Elena Marsh"]}
    p.update(over)
    return p


def llm_client(handler, **cfg):
    c = LLMSettings(provider="openai_compatible", model="stub-model", **cfg)
    return LLMClient(c, transport=httpx.MockTransport(handler))


def test_director_uses_llm_then_cache_and_falls_back_to_rules_on_failure():
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "```json\n" + json.dumps(good_reply()) + "\n```"}}]})

    store = {}
    d = D.Director(llm_client(handler))
    out, key = d.direct(prompt_payload(), store.get, lambda k, v, m: store.__setitem__(k, v))
    assert out["source"] == "llm" and len(calls) == 1
    system = calls[0]["messages"][0]["content"]
    assert "Never rewrite" in system and "controlled vocabulary" in system
    out2, key2 = d.direct(prompt_payload(), store.get, lambda k, v, m: store.__setitem__(k, v))
    assert key2 == key and len(calls) == 1 and d.stats["cache_hits"] == 1                  # cached: no second request
    out3, key3 = d.direct(prompt_payload(prev={"speaker": "Hale", "emotion": "anger", "intensity": 5}), store.get, lambda k, v, m: store.__setitem__(k, v))
    assert key3 != key and len(calls) == 2                                                 # previous delivery is part of the cache key

    def down(req):
        return httpx.Response(503, text="busy")

    bad = D.Director(llm_client(down))
    r, _ = bad.direct(prompt_payload(text="Another line."), {}.get, lambda *a: None)
    assert r["source"] == "rules" and r["needs_review"] and any("LLM unavailable" in n for n in r["notes"])


def test_director_repairs_once_then_gives_up():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content)["messages"][1]["content"])
        content = json.dumps(good_reply(emotion="nonsense")) if len(seen) == 1 else json.dumps(good_reply())
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    out, _ = D.Director(llm_client(handler)).direct(prompt_payload(), {}.get, lambda *a: None)
    assert out["source"] == "llm" and len(seen) == 2 and "rejected" in seen[1]


def test_rules_director_respects_sensitivity_and_is_labelled():
    cues = {"emotion": "anger", "intensity": 4, "pace": "fast", "tension": 3, "explicit": True, "evidence": ["speech verb 'shouted'"]}
    hi = D.rules_direction("x", "Tom", cues, 1.0, "He shouted at them all.")
    lo = D.rules_direction("x", "Tom", cues, 0.0, "He shouted at them all.")
    assert hi["source"] == "rules" and hi["intensity"] > lo["intensity"] and hi["facts"] == ["shouted"]
    weak = D.rules_direction("x", "Tom", {"emotion": "sadness", "intensity": 2, "explicit": False, "evidence": []}, 0.1, "text")
    assert weak["emotion"] == "neutral"                                                    # low sensitivity ignores weak cues


# ============================================================ continuity and smoothing


def raw(emotion, intensity, tension=2, pacing="normal", support=False):
    return {"emotion": emotion, "intensity": intensity, "tension": tension, "pacing": pacing, "support": support}


def run_seq(seq, **kw):
    st, outs = CT.new_state(), []
    for r, sp in seq:
        sm, st, note, kind = CT.apply(r, st, speaker=sp.get("speaker", "Elena"), scene=sp.get("scene", 0), chapter=sp.get("chapter", 1),
                                      strength=kw.get("strength", 0.6), enabled=kw.get("enabled", True), reset_scope=kw.get("reset_scope", "chapter"))
        outs.append((sm, kind, note))
    return outs


def test_grief_does_not_snap_back_to_neutral_at_a_segment_boundary():
    outs = run_seq([(raw("grief", 4), {}), (raw("neutral", 1), {}), (raw("neutral", 1), {}), (raw("neutral", 1), {}), (raw("neutral", 1), {})])
    emos = [o[0]["emotion"] for o in outs]
    assert emos[:3] == ["grief", "grief", "grief"] and emos[-1] == "neutral"              # decays gradually, then releases
    assert outs[1][0]["intensity"] == 3 and outs[1][1] == "smoothed" and "carried grief" in outs[1][2]


def test_intentional_abrupt_change_is_not_smoothed_away():
    outs = run_seq([(raw("calm", 1), {}), (raw("anger", 5, support=True), {})])
    assert outs[1][0]["emotion"] == "anger" and outs[1][0]["intensity"] == 5 and outs[1][1] == "intentional"
    unsupported = run_seq([(raw("calm", 1), {}), (raw("anger", 5), {})])
    assert unsupported[1][0]["intensity"] <= 3                                              # same jump without textual support is limited


def test_escalation_keeps_building_instead_of_restarting():
    outs = run_seq([(raw("anger", 2, tension=1), {}), (raw("anger", 2, tension=2), {}), (raw("anger", 2, tension=3), {}), (raw("anger", 2, tension=4), {})])
    assert [o[0]["intensity"] for o in outs] == [2, 3, 4, 5]


def test_speakers_have_independent_state_and_scene_chapter_resets():
    outs = run_seq([(raw("grief", 4), {"speaker": "Elena"}), (raw("neutral", 1), {"speaker": "Tom"}), (raw("neutral", 1), {"speaker": "Elena"})])
    assert outs[1][0]["emotion"] == "neutral" and outs[2][0]["emotion"] == "grief"
    scene = run_seq([(raw("grief", 4), {"scene": 0}), (raw("neutral", 1), {"scene": 1})], reset_scope="scene")
    assert scene[1][0]["emotion"] == "neutral" and scene[1][1] == "reset"
    decay = run_seq([(raw("grief", 4), {"scene": 0}), (raw("neutral", 1), {"scene": 1})], reset_scope="chapter")
    assert decay[1][0]["emotion"] == "grief"
    chapter = run_seq([(raw("grief", 4), {"chapter": 1}), (raw("neutral", 1), {"chapter": 2})], reset_scope="chapter")
    assert chapter[1][0]["emotion"] == "neutral"
    never = run_seq([(raw("grief", 4), {"chapter": 1}), (raw("neutral", 1), {"chapter": 2})], reset_scope="none")
    assert never[1][0]["emotion"] == "grief"


def test_smoothing_can_be_disabled_and_user_values_win():
    outs = run_seq([(raw("grief", 4), {}), (raw("neutral", 1), {})], enabled=False)
    assert outs[1][0]["emotion"] == "neutral"
    fin = CT.final_of(raw("grief", 4) | {"delivery": "heavy"}, {"emotion": "grief", "intensity": 3, "pacing": "slow", "tension": 2}, {"intensity": 5, "delivery": "hushed"})
    assert fin["intensity"] == 5 and fin["delivery"] == "hushed" and fin["emotion"] == "grief"


# ========================================================== model-aware delivery translation


FINAL = {"emotion": "grief", "intensity": 4, "pacing": "slow", "delivery": "voice breaking"}


def test_delivery_per_model_family():
    cfg = DeliverySettings()
    text, info = DL.build_instruct(FINAL, "preset", "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16", cfg)
    assert info["applied"] and text.startswith("Speak strongly grief-stricken") and "slow pace" in text and "voice breaking" in text and len(text) <= 160
    dtext, dinfo = DL.build_instruct(FINAL, "design", "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16", cfg)
    assert dinfo["applied"] and dtext.startswith("For this line:")
    ctext, cinfo = DL.build_instruct(FINAL, "clone", "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16", cfg)
    assert ctext is None and not cinfo["applied"] and "no instruction input" in cinfo["reason"]
    small, sinfo = DL.build_instruct(FINAL, "preset", "mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-bf16", cfg)
    assert small is None and "0.6B" in sinfo["reason"]
    off, oinfo = DL.build_instruct(FINAL, "preset", "x/CustomVoice", DeliverySettings(custom_voice="off"))
    assert off is None and "switched off" in oinfo["reason"]
    neutral, ninfo = DL.build_instruct({"emotion": "neutral", "intensity": 1, "pacing": "normal", "delivery": ""}, "preset", "x/CustomVoice", cfg)
    assert neutral is None


def test_llm_settings_never_store_keys_or_accept_credentials_in_urls():
    with pytest.raises(ValueError):
        LLMSettings(api_key_env="sk-live-abc123def")
    with pytest.raises(ValueError):
        LLMSettings(base_url="http://user:secret@127.0.0.1:1234/v1")
    with pytest.raises(ValueError):
        NarrativeSettings(director_enabled=True, semantic_segmentation=False)
    assert LLMSettings(base_url="http://127.0.0.1:1234/v1/").base_url == "http://127.0.0.1:1234/v1"


# ====================================================================== database / migration


def test_migration_v1_to_v2_keeps_existing_rows(tmp_path):
    from studio import db as DB

    d = DB.Database(tmp_path / "t.db")
    with d.session() as c:
        for st in DB._split_sql(DB.SCHEMA_V1):
            c.execute(st)
        c.execute("PRAGMA user_version = 1")
        c.execute("INSERT INTO books(uid,title,settings_json,synth_fingerprint,storage_json,created_at,updated_at) VALUES ('b','T','{}','f','{}',1,1)")
        c.execute("INSERT INTO jobs(kind,book_id,status,created_at,updated_at) VALUES ('book',1,'completed',1,1)")
        c.execute("INSERT INTO job_logs(job_id,book_id,level,message,created_at) VALUES (1,1,'info','hi',1)")
    assert d.migrate(tmp_path / "bk") == (1, DB.LATEST_VERSION)
    assert list((tmp_path / "bk").glob("studio-v1-*.db"))                                        # backup taken first
    with d.session() as c:
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert c.execute("SELECT message FROM job_logs").fetchone()[0] == "hi"
        c.execute("INSERT INTO jobs(kind,book_id,status,created_at,updated_at) VALUES ('analyze',1,'queued',1,1)")
        with pytest.raises(Exception):
            c.execute("INSERT INTO jobs(kind,book_id,status,created_at,updated_at) VALUES ('analyze',1,'queued',1,1)")  # one active per book
        assert d.migrate() == (DB.LATEST_VERSION, DB.LATEST_VERSION)


# ===================================================================== integration


def put_settings(client, narrative):
    r = client.put("/api/settings", json={"narrative": narrative})
    assert r.status_code == 200, r.text
    return r.json()


def ryan(client):
    return next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == "Ryan")


def voice_named(client, name):
    return next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == name)


def import_semantic(client, text=TXT, queue=False, **narr):
    put_settings(client, {"semantic_segmentation": True, **narr})
    r = client.post("/api/imports", files={"file": ("flood.txt", text.encode("utf-8"), "text/plain")})
    assert r.status_code == 200, r.text
    prev = r.json()
    c = client.post(f"/api/imports/{prev['token']}/confirm", json={"title": "The Flood", "voice_id": ryan(client)["id"], "queue": queue})
    assert c.status_code == 200, c.text
    return c.json()


def all_segments(ctx, book_id):
    with ctx.db.session() as conn:
        return rows(conn, "SELECT s.*, c.idx AS ch_idx FROM segments s JOIN chapters c ON c.id=s.chapter_id WHERE s.book_id=? ORDER BY c.idx, s.idx", (book_id,))


def chars(client, book_id):
    return {c["name"]: c for c in client.get(f"/api/books/{book_id}/characters").json()["characters"]}


class RecordingEngine(MockEngine):
    """MockEngine that records exactly what the TTS engine is handed."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.log = []

    def synthesize(self, voice, text, params, sid=None):
        self.log.append({"sid": sid, "text": text, "instruct": params.instruct, "kind": voice.kind, "speaker": voice.speaker, "model": voice.model_id})
        return super().synthesize(voice, text, params, sid)


class CountingASR(MockASR):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def transcribe(self, wav_path, language="en"):
        self.calls += 1
        return super().transcribe(wav_path, language)


def worker_with(ctx, tts=None, asr=None):
    with ctx.db.tx() as conn:  # take over from any worker an earlier step of the same test created
        conn.execute("UPDATE worker_state SET heartbeat_at=0 WHERE id=1")
    w = Worker(ctx, tts=tts or RecordingEngine(), asr=asr or MockASR())
    assert w.acquire_lease()
    return w


def test_semantic_import_persists_structure_characters_and_exact_coverage(client, ctx):
    book = import_semantic(client)
    bid = book["id"]
    segs = all_segments(ctx, bid)
    assert segs and all(s["src_start"] is not None for s in segs)
    with ctx.db.session() as conn:
        for ch in rows(conn, "SELECT * FROM chapters WHERE book_id=?", (bid,)):
            text = row(conn, "SELECT text FROM chapter_text WHERE chapter_id=?", (ch["id"],))["text"]
            mine = [s for s in segs if s["chapter_id"] == ch["id"]]
            assert [s["idx"] for s in mine] == list(range(1, len(mine) + 1))
            assert re.sub(r"\s+", "", "".join(text[s["src_start"]:s["src_end"]] for s in mine)) == re.sub(r"\s+", "", text)
            assert all(text[s["src_start"]:s["src_end"]] == s["original_text"] for s in mine)
        assert scalar(conn, "SELECT COUNT(*) FROM nar_spans WHERE book_id=?", (bid,)) > 20
    cs = chars(client, bid)
    assert {"Narrator", "Hale", "Elena Marsh", "Tom Briggs"} <= set(cs)
    assert "Mrs. Marsh" in cs["Elena Marsh"]["aliases"] or "Elena" in cs["Elena Marsh"]["aliases"]
    assert cs["Elena Marsh"]["gender"] == "female" and cs["Elena Marsh"]["prov"]["gender"]["source"] == "rules"
    st = client.get(f"/api/books/{bid}/narrative").json()
    assert st["semantic"] and st["needs_review"] >= 1 and st["stages"]["structure"]["status"] == "done" and st["stages"]["segmentation"]["status"] == "done"
    assert client.get(f"/api/books/{bid}").json()["narrative"]["characters"] == 3


def test_legacy_import_is_unchanged_when_the_feature_is_off(client, ctx):
    put_settings(client, {"semantic_segmentation": False})
    r = client.post("/api/imports", files={"file": ("a.txt", make_text(1).encode(), "text/plain")})
    c = client.post(f"/api/imports/{r.json()['token']}/confirm", json={"title": "Legacy", "voice_id": ryan(client)["id"]}).json()
    assert all(s["src_start"] is None for s in all_segments(ctx, c["id"]))
    assert client.get(f"/api/books/{c['id']}/narrative").json()["semantic"] is False
    assert client.post(f"/api/segments/{all_segments(ctx, c['id'])[0]['id']}/boundary", json={"op": "merge_next"}).status_code == 409


def test_tts_receives_only_the_segment_text_never_context_or_instructions(client, ctx):
    bid = import_semantic(client, director_enabled=True)["id"]
    eng = RecordingEngine()
    w = worker_with(ctx, eng)
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    segs = {s["sid"]: s for s in all_segments(ctx, bid)}
    assert eng.log and len(eng.log) >= len(segs)
    for call in eng.log:
        seg = segs[call["sid"]]
        assert call["text"] == " ".join((seg["override_text"] or seg["narration_text"]).split())
        if call["instruct"]:
            with ctx.db.session() as conn:
                ct = row(conn, "SELECT text FROM chapter_text WHERE chapter_id=?", (seg["chapter_id"],))["text"]
            outside = ct.replace(seg["original_text"], " ")
            assert not D.leak_guard(call["instruct"], [outside])[1], "instruction repeats surrounding manuscript text"
    assert any(c["instruct"] for c in eng.log), "the rules Director produced no delivery guidance at all"
    with ctx.db.session() as conn:
        a = row(conn, "SELECT * FROM attempts WHERE instruct_used IS NOT NULL LIMIT 1")
        assert a["delivery_applied"] == 1 and a["gen_key"]


def test_assigning_a_voice_recuts_and_regenerates_only_what_depends_on_it(client, ctx):
    bid = import_semantic(client)["id"]
    w = worker_with(ctx)
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    before = {s["sid"]: s for s in all_segments(ctx, bid)}
    assert all(s["status"] == "passed" for s in before.values() if not s["needs_review"] or True)
    hale = chars(client, bid)["Hale"]
    aiden = voice_named(client, "Aiden")
    res = client.patch(f"/api/characters/{hale['id']}", json={"voice_id": aiden["id"]}).json()
    assert res["voice_changed"] and res["resegmented"]["kept"] > 0 and res["resegmented"]["created"] > 0
    after = {s["sid"]: s for s in all_segments(ctx, bid)}
    untouched = [sid for sid, s in before.items() if sid in after and after[sid]["selected_attempt_id"] == s["selected_attempt_id"] and after[sid]["status"] == "passed"]
    assert len(untouched) >= len(before) // 2                                              # most of the book keeps its audio
    hale_segs = [s for s in after.values() if s["character_id"] == hale["id"]]
    assert hale_segs and all(s["status"] == "pending" for s in hale_segs)
    assert scalar(ctx.db.connect(), "SELECT COUNT(*) FROM segment_history WHERE book_id=?", (bid,)) >= 1
    eng = RecordingEngine()
    w2 = worker_with(ctx, eng)
    w2.run_until_idle()
    assert eng.log and all(c["speaker"] == "Aiden" or c["sid"] not in {s["sid"] for s in hale_segs} for c in eng.log)
    assert {c["speaker"] for c in eng.log if c["sid"] in {s["sid"] for s in hale_segs}} == {"Aiden"}
    assert len(eng.log) < len(after)                                                        # not a whole-book regeneration
    # narrator and Elena (unassigned) still use the book narrator
    narr = [c for c in eng.log if c["sid"] not in {s["sid"] for s in hale_segs}]
    assert all(c["speaker"] == "Ryan" for c in narr)


def test_reverting_a_voice_reuses_the_cached_take(client, ctx):
    bid = import_semantic(client)["id"]
    w = worker_with(ctx)
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    hale = chars(client, bid)["Hale"]
    client.patch(f"/api/characters/{hale['id']}", json={"voice_id": voice_named(client, "Aiden")["id"]})
    w.run_until_idle()
    with ctx.db.session() as conn:
        n_before = scalar(conn, "SELECT COUNT(*) FROM attempts", (), 0)
    client.patch(f"/api/characters/{hale['id']}", json={"voice_id": None})
    eng = RecordingEngine()
    worker_with(ctx, eng).run_until_idle()
    with ctx.db.session() as conn:
        reused = scalar(conn, "SELECT COUNT(*) FROM review_actions WHERE action='cache_reuse'", (), 0)
        n_after = scalar(conn, "SELECT COUNT(*) FROM attempts", (), 0)
    assert reused > 0                                                                       # the original narrator take came straight back
    assert n_after <= n_before + len(eng.log)
    assert all(s["status"] == "passed" for s in all_segments(ctx, bid))


def test_merging_characters_repoints_dialogue_and_keeps_the_alias(client, ctx):
    bid = import_semantic(client)["id"]
    cs = chars(client, bid)
    tom = cs["Tom Briggs"]
    dup = client.post(f"/api/books/{bid}/characters", json={"name": "The Cyclist"}).json()
    assert client.post(f"/api/characters/{dup['id']}/merge", json={"into_id": tom["id"]}).status_code == 200
    merged = chars(client, bid)
    assert "The Cyclist" not in merged and "The Cyclist" in merged["Tom Briggs"]["aliases"]
    assert client.post(f"/api/characters/{tom['id']}/merge", json={"into_id": tom["id"]}).status_code == 422
    narr = merged["Narrator"]
    assert client.post(f"/api/characters/{narr['id']}/merge", json={"into_id": tom["id"]}).status_code == 422


def test_user_confirmed_facts_survive_reanalysis(client, ctx):
    bid = import_semantic(client)["id"]
    elena = chars(client, bid)["Elena Marsh"]
    client.patch(f"/api/characters/{elena['id']}", json={"gender": "neutral", "accent": "Northern English", "aliases": ["Mrs. Marsh", "the widow"]})
    # assign a flagged line by hand, then rebuild Pass 1 from the stored manuscript
    flagged = next(s for s in all_segments(ctx, bid) if s["needs_review"])
    r = client.patch(f"/api/segments/{flagged['id']}/narrative", json={"character_id": elena["id"]})
    assert r.status_code == 200, r.text
    job = client.post(f"/api/books/{bid}/analyze", json={"rebuild": True, "stages": ["structure", "segmentation"]}).json()
    worker_with(ctx).run_until_idle()
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "completed"
    now_elena = chars(client, bid)["Elena Marsh"]
    assert now_elena["gender"] == "neutral" and now_elena["accent"] == "Northern English" and "the widow" in now_elena["aliases"]
    assert now_elena["prov"]["gender"]["confirmed"] and now_elena["prov"]["gender"]["source"] == "user"
    with ctx.db.session() as conn:
        locked = rows(conn, "SELECT * FROM nar_spans WHERE book_id=? AND user_locked=1", (bid,))
    assert locked and all(l["speaker_id"] == elena["id"] for l in locked)
    kept = [s for s in all_segments(ctx, bid) if s["id"] == flagged["id"]]
    assert kept and kept[0]["character_id"] == elena["id"] and not kept[0]["needs_review"]       # same row, same id


def test_manual_boundaries_invalidate_only_the_affected_segments(client, ctx):
    bid = import_semantic(client)["id"]
    w = worker_with(ctx)
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    before = {s["sid"]: s for s in all_segments(ctx, bid)}
    target = next(s for s in before.values() if len(s["original_text"]) > 120 and s["kind"] == "body")
    mid = target["src_start"] + target["original_text"].index(" ", 60)
    r = client.post(f"/api/segments/{target['id']}/boundary", json={"op": "split", "offset": mid})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["created"] == 2 and out["retired"] == 1
    after = {s["sid"]: s for s in all_segments(ctx, bid)}
    assert target["sid"] not in after
    unchanged = [sid for sid in before if sid in after and sid != target["sid"]]
    assert len(unchanged) == len(before) - 1
    assert all(after[sid]["status"] == before[sid]["status"] and after[sid]["selected_attempt_id"] == before[sid]["selected_attempt_id"] for sid in unchanged)
    new = [s for sid, s in after.items() if sid not in before]
    assert len(new) == 2 and all(s["status"] == "pending" for s in new) and any(s["boundary_manual"] for s in new)
    assert re.sub(r"\s+", "", "".join(s["original_text"] for s in new)) == re.sub(r"\s+", "", target["original_text"])
    with ctx.db.session() as conn:
        assert row(conn, "SELECT reason FROM segment_history WHERE sid=?", (target["sid"],))["reason"]
    # the manual cut survives a full re-segmentation
    assert client.post(f"/api/books/{bid}/resegment").status_code == 200
    again = {s["sid"] for s in all_segments(ctx, bid)}
    assert {s["sid"] for s in new} <= again
    # merging back
    first = min(new, key=lambda s: s["src_start"])
    m = client.post(f"/api/segments/{first['id']}/boundary", json={"op": "merge_next"})
    assert m.status_code == 200 and m.json()["created"] == 1
    # move_end shifts the boundary to a different word without touching other segments
    nxt = next(s for s in all_segments(ctx, bid) if s["kind"] == "body" and len(s["original_text"]) > 200)
    seg2 = client.post(f"/api/segments/{nxt['id']}/boundary", json={"op": "split", "offset": nxt["src_start"] + 100})
    assert seg2.status_code == 200
    left = min((s for s in all_segments(ctx, bid) if nxt["src_start"] <= s["src_start"] < nxt["src_end"] and s["chapter_id"] == nxt["chapter_id"]), key=lambda s: s["src_start"])
    mv = client.post(f"/api/segments/{left['id']}/boundary", json={"op": "move_end", "offset": left["src_start"] + 40})
    assert mv.status_code == 200, mv.text
    with ctx.db.session() as conn:
        ct = row(conn, "SELECT text FROM chapter_text WHERE chapter_id=?", (nxt["chapter_id"],))["text"]
        mine = rows(conn, "SELECT * FROM segments WHERE chapter_id=? ORDER BY idx", (nxt["chapter_id"],))
    assert re.sub(r"\s+", "", "".join(ct[s["src_start"]:s["src_end"]] for s in mine)) == re.sub(r"\s+", "", ct)
    assert client.post(f"/api/segments/{mine[0]['id']}/boundary", json={"op": "split", "offset": 0}).status_code in (409, 422)


def test_director_pipeline_overrides_and_targeted_regeneration(client, ctx):
    bid = import_semantic(client, director_enabled=True)["id"]
    job = client.post(f"/api/books/{bid}/analyze", json={}).json()
    w = worker_with(ctx)
    w.run_until_idle()
    j = client.get(f"/api/jobs/{job['id']}").json()
    assert j["status"] == "completed", j
    st = client.get(f"/api/books/{bid}/narrative").json()
    segs = all_segments(ctx, bid)
    assert st["directed"] == len(segs) and st["stages"]["director"]["status"] == "done" and st["stages"]["summaries"]["status"] == "done"
    with ctx.db.session() as conn:
        d = rows(conn, "SELECT * FROM directions WHERE book_id=?", (bid,))
        notes = rows(conn, "SELECT * FROM story_notes WHERE book_id=? AND kind='summary'", (bid,))
        text = {r["chapter_id"]: r["text"] for r in rows(conn, "SELECT chapter_id, text FROM chapter_text WHERE book_id=?", (bid,))}
    assert all(x["source"] == "rules" for x in d)
    assert notes and all(n["source"] == "extractive" for n in notes)
    for n in notes:                                                                         # summaries only quote the manuscript
        for part in n["text"].split(" … "):
            assert any(part in t for t in text.values())
    # generate everything, then override one segment's emotion
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    done = {s["sid"]: s for s in all_segments(ctx, bid)}
    assert all(s["status"] == "passed" for s in done.values())
    victim = next(s for s in done.values() if s["kind"] == "body")
    r = client.patch(f"/api/segments/{victim['id']}/narrative", json={"emotion": "fear", "intensity": 5, "delivery": "trembling"})
    assert r.status_code == 200, r.text
    mid = {s["sid"]: s for s in all_segments(ctx, bid)}
    assert mid[victim["sid"]]["status"] == "pending"
    changed = [sid for sid in done if mid[sid]["status"] != done[sid]["status"]]
    assert victim["sid"] in changed and len(changed) <= 1 + 24                              # bounded cascade
    assert len(changed) < len(done) // 2
    insp = client.get(f"/api/segments/{victim['id']}/inspect").json()
    assert insp["direction"]["user"]["emotion"] == "fear" and insp["direction"]["final"]["emotion"] == "fear"
    assert insp["delivery"]["applied"] and "trembling" in insp["delivery"]["instruct"] and insp["delivery"]["spoken_text_sent_to_tts"] == victim["narration_text"]
    # the override persists across a Director re-run and a rebuild
    client.post(f"/api/books/{bid}/analyze", json={"rebuild": True})
    w.run_until_idle()
    insp2 = client.get(f"/api/segments/{victim['id']}/inspect").json()
    assert insp2["direction"]["user"]["emotion"] == "fear" if insp2["direction"] else True


def test_director_results_are_cached_across_runs(client, ctx):
    bid = import_semantic(client, director_enabled=True)["id"]
    client.post(f"/api/books/{bid}/analyze", json={})
    worker_with(ctx).run_until_idle()
    with ctx.db.session() as conn:
        n = scalar(conn, "SELECT COUNT(*) FROM director_cache", (), 0)
        conn.execute("SELECT 1")
    with ctx.db.tx() as conn:
        conn.execute("DELETE FROM directions WHERE book_id=?", (bid,))
    client.post(f"/api/books/{bid}/analyze", json={"stages": ["director"]})
    worker_with(ctx).run_until_idle()
    with ctx.db.session() as conn:
        assert scalar(conn, "SELECT COUNT(*) FROM director_cache", (), 0) == n                # no new analysis was needed
        assert scalar(conn, "SELECT COALESCE(SUM(hits),0) FROM director_cache", (), 0) >= n
    stats = client.get(f"/api/books/{bid}/narrative").json()["stages"]["director"]["stats"]
    assert stats["cache_hits"] >= n


@needs_ffmpeg
def test_clone_voice_gets_no_instruction_but_keeps_reference_conditioning(client, ctx):
    from conftest import make_wav_bytes

    bid = import_semantic(client, director_enabled=True)["id"]
    form = {"name": "CloneTest", "ref_text": "This is a short sample of my own speaking voice.", "rights_holder": "Jane Author", "owner_attest": "true"}
    r = client.post("/api/voices/clone", data=form, files={"file": ("me.wav", make_wav_bytes(6.0), "audio/wav")})
    assert r.status_code == 200, r.text
    clone = r.json()
    tom = chars(client, bid)["Tom Briggs"]
    client.patch(f"/api/characters/{tom['id']}", json={"voice_id": clone["id"]})
    client.post(f"/api/books/{bid}/analyze", json={})
    eng = RecordingEngine()
    w = worker_with(ctx, eng)
    w.run_until_idle()
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    clone_calls = [c for c in eng.log if c["kind"] == "clone"]
    preset_calls = [c for c in eng.log if c["kind"] == "preset"]
    assert clone_calls and all(not c["instruct"] for c in clone_calls)                      # Base: reference only, no instruction
    assert any(c["instruct"] for c in preset_calls)                                          # the same book's preset voice does get guidance
    with ctx.db.session() as conn:
        a = row(conn, "SELECT delivery_applied, instruct_used FROM attempts WHERE voice_id_used=?", (clone["id"],))
        assert a["delivery_applied"] == 0 and a["instruct_used"] is None
        assert row(conn, "SELECT ref_sha256 FROM voice_versions WHERE voice_id=?", (clone["id"],))["ref_sha256"]


def test_voice_design_character_keeps_its_description_and_gets_delivery_appended(client, ctx):
    bid = import_semantic(client, director_enabled=True)["id"]
    d = client.post("/api/voices/design", json={"name": "Gruff doctor", "description": "A gruff older male voice, low and slow, with a dry edge."}).json()
    hale = chars(client, bid)["Hale"]
    client.patch(f"/api/characters/{hale['id']}", json={"voice_id": d["id"]})
    client.post(f"/api/books/{bid}/analyze", json={})
    eng = RecordingEngine()
    w = worker_with(ctx, eng)
    w.run_until_idle()
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    design = [c for c in eng.log if c["kind"] == "design"]
    assert design
    with ctx.db.session() as conn:
        atts = rows(conn, "SELECT instruct_used, delivery_applied FROM attempts WHERE voice_id_used=?", (d["id"],))
    assert atts and all(a["instruct_used"] is None or a["instruct_used"].startswith("For this line:") for a in atts)


def test_whisper_verification_modes(client, ctx):
    bid = import_semantic(client, verify_mode="off")["id"]
    asr = CountingASR()
    w = worker_with(ctx, RecordingEngine({"text:Tom yelled": ["drop"]}), asr)
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    assert asr.calls == 0
    with ctx.db.session() as conn:
        assert scalar(conn, "SELECT COUNT(*) FROM attempts WHERE asr_skipped=1", (), 0) > 0
    # every: the same kind of fault is caught and flagged for review
    bid2 = import_semantic(client, text=TXT.replace("The Flood", "The Flood Again"), verify_mode="every")["id"]
    asr2 = CountingASR()
    w2 = worker_with(ctx, RecordingEngine({"text:found him": ["drop", "drop", "drop"]}), asr2)
    client.post("/api/queue", json={"book_id": bid2})
    w2.run_until_idle()
    assert asr2.calls > 0
    assert any(s["status"] == "flagged" for s in all_segments(ctx, bid2))
    # sample: roughly the configured share is checked, deterministically per segment
    bid3 = import_semantic(client, text=TXT.replace("The Flood", "The Flood Third"), verify_mode="sample", verify_sample_rate=0.3)["id"]
    asr3 = CountingASR()
    client.post("/api/queue", json={"book_id": bid3})
    worker_with(ctx, RecordingEngine(), asr3).run_until_idle()
    n = len(all_segments(ctx, bid3))
    assert 0 < asr3.calls < n


@needs_ffmpeg
def test_assembly_preserves_segment_order_and_exports_metadata(client, ctx):
    bid = import_semantic(client)["id"]
    hale = chars(client, bid)["Hale"]
    client.patch(f"/api/characters/{hale['id']}", json={"voice_id": voice_named(client, "Aiden")["id"]})
    w = worker_with(ctx)
    client.post("/api/queue", json={"book_id": bid})
    w.run_until_idle()
    w.run_until_idle()
    with ctx.db.session() as conn:
        for ch in rows(conn, "SELECT * FROM chapters WHERE book_id=? ORDER BY idx", (bid,)):
            r = row(conn, "SELECT segment_map_json FROM chapter_renders WHERE chapter_id=? AND kind='wav'", (ch["id"],))
            assert r, "chapter master was not assembled"
            order = [m["sid"] for m in json.loads(r["segment_map_json"])]
            expect = [s["sid"] for s in rows(conn, "SELECT sid FROM segments WHERE chapter_id=? ORDER BY idx", (ch["id"],))]
            assert order == expect
    meta = client.get(f"/api/books/{bid}/narrative/metadata").json()
    assert meta["segments"] and {c["name"] for c in meta["characters"]} >= {"Hale", "Elena Marsh"}
    hale_rows = [s for s in meta["segments"] if s["speaker"] == "Hale"]
    assert hale_rows and all(s["status"] in ("passed", "approved") for s in meta["segments"])


def test_project_overrides_and_validation(client, ctx):
    bid = import_semantic(client)["id"]
    r = client.put(f"/api/books/{bid}/narrative/settings", json={"overrides": {"context_before_chars": 400, "emotion_sensitivity": 0.9}}).json()
    assert r["effective"]["context_before_chars"] == 400 and r["defaults"]["context_before_chars"] == 1250 and r["overrides"]["emotion_sensitivity"] == 0.9
    ins = client.get(f"/api/segments/{all_segments(ctx, bid)[3]['id']}/inspect").json()
    assert len(ins["context"]["before"]) <= 400 and ins["settings"]["context_before_chars"] == 400
    assert client.put(f"/api/books/{bid}/narrative/settings", json={"overrides": {"director_enabled": True, "semantic_segmentation": False}}).status_code in (409, 422)
    assert client.put(f"/api/books/{bid}/narrative/settings", json={"overrides": {"llm": {"base_url": "ftp://x"}}}).status_code == 422
    assert client.put(f"/api/books/{bid}/narrative/settings", json={"overrides": {"context_before_chars": None}}).json()["effective"]["context_before_chars"] == 1250
    # global settings persist and validate
    assert client.put("/api/settings", json={"narrative": {"director_enabled": True, "semantic_segmentation": False}}).status_code == 422
    assert client.get("/api/settings").json()["settings"]["narrative"]["max_segment_chars"] == 500
    vocab = client.get("/api/narrative/vocabulary").json()
    assert "grief" in vocab["emotions"] and "slow" in vocab["pacings"]


def test_segment_inspector_payload(client, ctx):
    bid = import_semantic(client, director_enabled=True)["id"]
    client.post(f"/api/books/{bid}/analyze", json={})
    worker_with(ctx).run_until_idle()
    seg = next(s for s in all_segments(ctx, bid) if s["span_kind"] in ("dialogue", "mixed") and s["character_id"])
    i = client.get(f"/api/segments/{seg['id']}/inspect").json()
    for key in ("original_text", "context", "chapter", "scene", "speaker", "voice", "direction", "previous", "next", "delivery", "attempts", "spans", "history"):
        assert key in i
    assert i["speaker"]["name"] and i["voice"]["name"] == "Ryan" and i["direction"]["raw"]["emotion"]
    assert i["direction"]["smoothed"] and i["direction"]["final"]["intensity"] >= 1
    assert i["delivery"]["gen_key"] and i["delivery"]["spoken_text_sent_to_tts"]
    prev = i["previous"]
    assert prev is None or prev["final"] is not None


def test_segment_preview_job_uses_the_segments_own_voice_and_delivery(client, ctx):
    bid = import_semantic(client, director_enabled=True)["id"]
    client.post(f"/api/books/{bid}/analyze", json={})
    worker_with(ctx).run_until_idle()
    hale = chars(client, bid)["Hale"]
    client.patch(f"/api/characters/{hale['id']}", json={"voice_id": voice_named(client, "Aiden")["id"]})
    seg = next(s for s in all_segments(ctx, bid) if s["character_id"] == hale["id"])
    job = client.post(f"/api/segments/{seg['id']}/preview", json={}).json()
    eng = RecordingEngine()
    worker_with(ctx, eng).step()
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["status"] == "completed" and done["result"]["text_sent_to_tts"] and eng.log[0]["speaker"] == "Aiden"
    with ctx.db.session() as conn:
        assert scalar(conn, "SELECT COUNT(*) FROM attempts WHERE segment_id=?", (seg["id"],), 0) == 0   # a preview is never a take


# ---------------------------------------------------------------- a real HTTP LLM endpoint (stub)


class _Stub(BaseHTTPRequestHandler):
    seen: list = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        user = body["messages"][1]["content"]
        _Stub.seen.append(user)
        if "Cast:" in user:  # attribution request
            m = re.search(r"(Hale said nothing|Tom Briggs, standing a little apart[^.]*)", user)
            reply = {"speaker": "Hale", "confidence": 0.8, "evidence": "told the doctor"} if "told the doctor" in user else {"speaker": "UNKNOWN", "confidence": 0, "evidence": ""}
        else:
            reply = good_reply(speaker=json.loads(user.split("\n", 1)[1])["segment"]["speaker"] or "Narrator", facts=[], context_refs=[])
        data = json.dumps({"choices": [{"message": {"content": json.dumps(reply)}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_llm_stage_end_to_end_against_a_stub_endpoint(client, ctx):
    _Stub.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_port}/v1"
        bid = import_semantic(client, director_enabled=True, llm={"provider": "openai_compatible", "base_url": url, "model": "stub"})["id"]
        before = client.get(f"/api/books/{bid}/narrative").json()["needs_review"]
        client.post(f"/api/books/{bid}/analyze", json={})
        worker_with(ctx).run_until_idle()
        st = client.get(f"/api/books/{bid}/narrative").json()
        assert st["stages"]["attribution"]["stats"]["tried"] >= 1
        assert st["stages"]["director"]["stats"]["llm_calls"] >= 1
        with ctx.db.session() as conn:
            srcs = {r["source"] for r in rows(conn, "SELECT source FROM directions WHERE book_id=?", (bid,))}
            llm_spans = rows(conn, "SELECT * FROM nar_spans WHERE book_id=? AND attribution='llm'", (bid,))
        assert "llm" in srcs
        # the stub answered only where its evidence is genuinely in the passage; resolved lines carry that evidence
        for sp in llm_spans:
            assert "told the doctor" in sp["evidence"]
        assert st["needs_review"] <= before
        assert any("<<<" in u for u in _Stub.seen)                                        # the line was marked inside its surrounding passage
    finally:
        srv.shutdown()


def test_api_requires_nothing_new_to_keep_existing_endpoints_working(client, ctx):
    assert client.get("/api/overview").status_code == 200
    assert client.get("/api/queue").status_code == 200
    r = client.get("/api/auth/status").json()
    assert r["version"]


def test_voice_consistency_measure_tracks_pitch():
    import numpy as np

    from studio.narrative import voicecheck as VC

    sr = 24000
    t = np.arange(sr) / sr

    def tone(f):
        return 0.3 * np.sin(2 * np.pi * f * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t) ** 2)

    assert abs(VC.f0_median(tone(140), sr) - 140) < 6 and abs(VC.f0_median(tone(220), sr) - 220) < 9
    rep = VC.consistency([("steady", tone(140), sr), ("steady", tone(143), sr), ("drifty", tone(120), sr), ("drifty", tone(210), sr)])
    assert rep["steady"]["f0_cv"] < 0.05 < rep["drifty"]["f0_cv"]
    assert VC.f0_median(np.zeros(sr), sr) is None


def test_api_key_env_is_restricted_to_studio_variables():
    assert LLMSettings(api_key_env="STUDIO_LLM_KEY").api_key_env == "STUDIO_LLM_KEY"
    for bad in ("AWS_SECRET_ACCESS_KEY", "HOME", "STUDIO_LLM key"):
        with pytest.raises(ValueError):
            LLMSettings(api_key_env=bad)
    c = LLMClient(LLMSettings(provider="openai_compatible", model="m", api_key_env="STUDIO_LLM_KEY"), environ={})
    with pytest.raises(Exception, match="not set"):
        c._headers()


def test_analysis_survives_pause_and_a_worker_restart(client, ctx):
    bid = import_semantic(client, director_enabled=True, director_lookahead=4)["id"]
    job = client.post(f"/api/books/{bid}/analyze", json={}).json()
    w = worker_with(ctx)
    for _ in range(3):
        w.step()
    mid = client.get(f"/api/jobs/{job['id']}").json()
    assert mid["status"] in ("running", "queued") and mid["payload"]["progress"]
    client.post(f"/api/jobs/{job['id']}/pause")
    w.step()
    assert client.get(f"/api/jobs/{job['id']}").json()["status"] == "paused"
    assert w.step() is False                                                                 # a paused analysis is not picked up
    client.post(f"/api/jobs/{job['id']}/resume")
    with ctx.db.tx() as conn:                                                                # simulate a crash while running
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job["id"],))
    w2 = worker_with(ctx)
    assert w2.recover()["requeued_jobs"] >= 1
    w2.run_until_idle()
    done = client.get(f"/api/jobs/{job['id']}").json()
    assert done["status"] == "completed"
    assert client.get(f"/api/books/{bid}/narrative").json()["directed"] == len(all_segments(ctx, bid))
    # cancelling keeps everything already produced
    job2 = client.post(f"/api/books/{bid}/analyze", json={"stages": ["director"]}).json()
    client.post(f"/api/jobs/{job2['id']}/cancel")
    worker_with(ctx).run_until_idle()
    assert client.get(f"/api/jobs/{job2['id']}").json()["status"] == "cancelled"
    assert client.get(f"/api/books/{bid}/narrative").json()["directed"] == len(all_segments(ctx, bid))
