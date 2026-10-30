"""Plumbing for the reference bank, A/B votes and the chapter / scene / translator LLM models (schema v5, settings, LLM client).
Nothing here changes how existing books sound: every new setting defaults to the old behaviour. MOCK engine only."""
from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from conftest import make_text
from studio import db as DB
from studio.config import LLMSettings, NarrativeSettings, Settings, book_snapshot, effective_narrative, synth_fingerprint
from studio.db import row
from studio.narrative.llm import LLMClient, LLMError, resolve_models


# ---------------------------------------------------------------- schema v5


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_migration_v4_to_v5_adds_bank_votes_and_attempt_columns(tmp_path):
    d = DB.Database(tmp_path / "t.db")
    with d.session() as c:
        for version, sql in DB.MIGRATIONS[:4]:
            for st in DB._split_sql(sql):
                c.execute(st)
            c.execute(f"PRAGMA user_version = {version}")
        assert "pace_used" not in _cols(c, "attempts")
    assert DB.MIGRATIONS[4][0] == 5 and DB.LATEST_VERSION >= 5
    assert d.migrate(tmp_path / "bk")[0] == 4
    with d.session() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == DB.LATEST_VERSION
        assert {"voice_id", "bucket", "ref_audio_path", "ref_text", "ref_sha256", "source", "consent_json", "metrics_json"} <= _cols(c, "voice_refs")
        assert {"book_id", "comparison", "a_label", "b_label", "winner", "other_narrator", "actor"} <= _cols(c, "ab_votes")
        assert {"pace_used", "ref_id_used"} <= _cols(c, "attempts")
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        c.execute("INSERT INTO books(uid,title,settings_json,synth_fingerprint,storage_json,created_at,updated_at) VALUES ('b','T','{}','f','{}',1,1)")
        with pytest.raises(Exception):  # winner is constrained
            c.execute("INSERT INTO ab_votes(book_id,comparison,a_label,b_label,winner,created_at) VALUES (1,'c','a','b','tie',1)")
        c.execute("INSERT INTO ab_votes(book_id,comparison,a_label,b_label,winner,created_at) VALUES (1,'c','a','b','same',1)")


def test_one_adopted_reference_per_bucket_per_voice(ctx):
    with ctx.db.tx() as conn:
        vid = conn.execute("SELECT id FROM voices LIMIT 1").fetchone()[0]
        ins = "INSERT INTO voice_refs(voice_id,bucket,ref_audio_path,ref_text,ref_sha256,source,consent_json,created_at) VALUES (?,?,?,?,?,?,?,1)"
        conn.execute(ins, (vid, "angry", "a.wav", "t", "x", "recording", "{}"))
        conn.execute(ins, (vid, "sad", "b.wav", "t", "x", "recording", "{}"))
        with pytest.raises(Exception):
            conn.execute(ins, (vid, "angry", "c.wav", "t", "x", "recording", "{}"))


def test_book_delete_still_works_with_ab_votes(client, ctx):
    r = client.post("/api/imports", files={"file": ("A.txt", make_text(2, title="A").encode(), "application/octet-stream")})
    voice = next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
    b = client.post(f"/api/imports/{r.json()['token']}/confirm", json={"title": "A", "voice_id": voice["id"], "queue": False}).json()
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO ab_votes(book_id,comparison,a_label,b_label,winner,created_at) VALUES (?,?,?,?,?,1)", (b["id"], "pace", "a", "b", "a"))
    r = client.post(f"/api/disk/books/{b['id']}/delete", json={"confirm": True})
    assert r.status_code == 200, r.text
    with ctx.db.session() as conn:
        assert row(conn, "SELECT id FROM books WHERE id=?", (b["id"],)) is None
        assert row(conn, "SELECT id FROM ab_votes WHERE book_id=?", (b["id"],)) is None


# ---------------------------------------------------------------- settings


def test_new_settings_defaults_and_bounds():
    s = Settings()
    llm, dl, nar = s.narrative.llm, s.narrative.delivery, s.narrative
    assert (llm.chapter_model, llm.scene_model, llm.translator_model) == ("", "", "")
    assert llm.chapter_max_tokens == 16000 and llm.chapter_timeout_s == 900.0
    assert (dl.clone, dl.pace_from_direction, dl.pauses_from_direction, dl.translator, dl.bank_min_intensity) == ("off", "off", False, "template", 3)
    assert nar.director_chapter_plan is True
    for bad in ({"chapter_max_tokens": 999}, {"chapter_max_tokens": 32001}, {"chapter_timeout_s": 29}, {"chapter_timeout_s": 3601}, {"scene_model": "x" * 201}):
        with pytest.raises(ValidationError):
            LLMSettings(**bad)
    for bad in ({"clone": "bank"}, {"pace_from_direction": "yes"}, {"translator": "gpt"}, {"bank_min_intensity": 0}, {"bank_min_intensity": 6}):
        with pytest.raises(ValidationError):
            NarrativeSettings(delivery=bad)
    assert LLMSettings(chapter_model="  big  ").chapter_model == "big"


def test_new_fields_do_not_change_the_fingerprint_or_snapshot():
    base = Settings()
    tweaked = Settings.model_validate({"narrative": {"director_chapter_plan": False, "llm": {"chapter_model": "big", "scene_model": "small", "translator_model": "tiny"},
                                                       "delivery": {"clone": "reference_bank", "pace_from_direction": "always", "pauses_from_direction": True,
                                                                    "translator": "llm", "bank_min_intensity": 1}}})
    assert book_snapshot(base) == book_snapshot(tweaked)
    vv = {"id": 1, "model_id": "m", "speaker": "s", "instruct": None, "ref_sha256": None}
    assert synth_fingerprint(book_snapshot(base), vv, "calm") == synth_fingerprint(book_snapshot(tweaked), vv, "calm")


def test_per_book_overrides_accept_the_new_nested_fields(client, ctx):
    r = client.post("/api/imports", files={"file": ("A.txt", make_text(2, title="A").encode(), "application/octet-stream")})
    voice = next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
    b = client.post(f"/api/imports/{r.json()['token']}/confirm", json={"title": "A", "voice_id": voice["id"], "queue": False}).json()
    patch = {"director_chapter_plan": False, "delivery": {"clone": "reference_bank", "bank_min_intensity": 2}, "llm": {"scene_model": "small"}}
    r = client.put(f"/api/books/{b['id']}/narrative/settings", json={"overrides": patch})
    assert r.status_code == 200, r.text
    assert client.put(f"/api/books/{b['id']}/narrative/settings", json={"overrides": {"delivery": {"bank_min_intensity": 9}}}).status_code == 422
    eff = effective_narrative(ctx.settings(), patch)
    assert eff.delivery.clone == "reference_bank" and eff.delivery.bank_min_intensity == 2 and eff.llm.scene_model == "small" and eff.director_chapter_plan is False
    with pytest.raises(Exception):
        effective_narrative(ctx.settings(), {"delivery": {"bank_min_intensity": 9}})


# ---------------------------------------------------------------- LLM client


def _client(handler, **kw):
    cfg = LLMSettings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1", model="main", max_tokens=100, **kw)
    return LLMClient(cfg, transport=httpx.MockTransport(handler))


def _reply(content="{\"a\": 1}", finish="stop", **msg):
    return httpx.Response(200, json={"choices": [{"message": {"content": content, **msg}, "finish_reason": finish}]})


def test_derive_shares_state_and_overrides_only_what_is_given():
    parent = _client(lambda r: _reply())
    child = parent.derive(model="big", thinking=True, max_tokens=5000, timeout_s=600)
    assert child._rejected is parent._rejected and child._transport is parent._transport
    assert child._env is parent._env and child._secrets is parent._secrets and child._slot == parent._slot
    assert child.cfg.model == "big" and child.cfg.max_tokens == 5000 and child.cfg.timeout_s == 600.0 and child.thinking is True
    assert child.model_label == "openai_compatible:big" and parent.model_label == "openai_compatible:main"
    assert parent.cfg.model == "main" and parent.cfg.max_tokens == 100 and parent.thinking is False
    same = parent.derive(model="  ", max_tokens=None)
    assert same.cfg.model == "main" and same.cfg.max_tokens == 100 and same.cfg.timeout_s == parent.cfg.timeout_s and same.thinking is False


def test_thinking_request_enables_thinking_without_json_mode_and_keeps_reasoning():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return _reply("plan text", reasoning_content="step 1, step 2")

    c = _client(handler).derive(thinking=True)
    assert c.chat("s", "u", json_mode=True) == "plan text"
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": True} and "response_format" not in seen[0]
    assert c.last_reasoning == "step 1, step 2"
    c2 = _client(lambda r: _reply("x", reasoning="alt field")).derive(thinking=True)
    c2.chat("s", "u")
    assert c2.last_reasoning == "alt field"
    c3 = _client(lambda r: _reply("x")).derive(thinking=True)
    c3.chat("s", "u")
    assert c3.last_reasoning == ""


def test_thinking_reply_cut_off_at_max_tokens_is_an_error():
    c = _client(lambda r: _reply("half a pla", finish="length", reasoning_content="long")).derive(thinking=True)
    with pytest.raises(LLMError, match="cut off at max_tokens"):
        c.chat("s", "u")
    plain = _client(lambda r: _reply("half", finish="length"))
    assert plain.chat("s", "u") == "half"          # non-thinking: only a log warning, as before


def test_non_thinking_request_is_unchanged():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return _reply()

    c = _client(handler)
    c.chat("s", "u")
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False} and seen[0]["response_format"] == {"type": "json_object"}
    assert seen[0]["model"] == "main" and seen[0]["max_tokens"] == 100


def test_resolve_models_blank_handling():
    assert resolve_models(LLMSettings(model="m")) == {"chapter": "m", "scene": "m", "translator": None}
    assert resolve_models(LLMSettings(model="m", chapter_model="big", scene_model=" small ", translator_model="tiny")) == {"chapter": "big", "scene": "small", "translator": "tiny"}


def test_connection_checks_extra_models_after_the_first_three():
    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "main"}, {"id": "big"}]})
        return _reply("OK")

    r = _client(handler, chapter_model="big", scene_model="missing", translator_model="main").test_connection()
    names = [c["name"] for c in r["checks"]]
    assert names == ["Reach the endpoint", "Model is listed", "Ask the model", "Chapter model is listed", "Scene model is listed"]
    by = {c["name"]: c for c in r["checks"]}
    assert by["Chapter model is listed"]["ok"] and not by["Scene model is listed"]["ok"] and "missing" in by["Scene model is listed"]["detail"]
    assert not r["ok"] and all(by[n]["ok"] for n in names[:3])
    plain = _client(handler).test_connection()
    assert plain["ok"] and len(plain["checks"]) == 3
