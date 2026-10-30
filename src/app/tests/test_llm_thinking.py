"""Thinking models (Gemma 4 on the Mac): the client must ask for no reasoning, survive servers that reject the field, and explain empty answers.

Found on the Mac in v0.6.2: with JSON mode on and thinking on, gemma-4-26B burned the whole max_tokens budget (reply `{"segment_`), and without JSON
mode its reasoning went to a separate `reasoning_content` field, leaving `content` empty."""
from __future__ import annotations

import json

import httpx
import pytest

from studio.config import LLMSettings
from studio.narrative.llm import LLMClient, LLMError


def _client(handler):
    cfg = LLMSettings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1", model="m", max_tokens=100)
    return LLMClient(cfg, transport=httpx.MockTransport(handler))


def _reply(content="{\"a\": 1}", **msg):
    return httpx.Response(200, json={"choices": [{"message": {"content": content, **msg}, "finish_reason": "stop"}]})


def test_requests_ask_for_no_thinking_and_json_mode():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return _reply()

    assert _client(handler).chat_json("s", "u", lambda d: d) == {"a": 1}
    assert seen[0]["chat_template_kwargs"] == {"enable_thinking": False} and seen[0]["response_format"] == {"type": "json_object"}


def test_server_rejecting_optional_fields_gets_them_dropped_one_at_a_time_and_remembered():
    seen = []

    def handler(req):
        body = json.loads(req.content)
        seen.append(sorted(k for k in ("response_format", "chat_template_kwargs") if k in body))
        return httpx.Response(400, text="unknown field") if "chat_template_kwargs" in body else _reply()

    c = _client(handler)
    assert c.chat("s", "u") == "{\"a\": 1}"
    assert seen == [["chat_template_kwargs", "response_format"], ["chat_template_kwargs"], []]
    seen.clear()
    c.chat("s", "u")
    assert seen == [[]]                                                       # remembered: no further 400s on this client


def test_a_genuine_400_does_not_switch_the_options_off_for_good():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400, text="context too long")

    c = _client(handler)
    with pytest.raises(LLMError):
        c.chat("s", "u")
    assert not c._rejected


def test_reasoning_only_answer_is_reported_as_such():
    with pytest.raises(LLMError, match="reasoning"):
        _client(lambda req: _reply(None, reasoning_content="Let me think about this...")).chat("s", "u")
    with pytest.raises(LLMError, match="empty"):
        _client(lambda req: _reply("")).chat("s", "u")


def test_inline_think_block_is_not_mistaken_for_the_answer():
    text = "<think>maybe {\"x\": 0} is right</think>{\"a\": 1}"
    assert _client(lambda req: _reply(text)).chat_json("s", "u", lambda d: d) == {"a": 1}
    with pytest.raises(LLMError):                                              # never closed: nothing usable
        _client(lambda req: _reply("<think>{\"a\": 1}")).chat("s", "u")


def test_connection_test_flags_a_model_that_only_thinks():
    def handler(req):
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        return _reply(None, reasoning_content="hmm")

    r = _client(handler).test_connection()
    assert not r["ok"] and "reasoning" in r["checks"][-1]["detail"]
