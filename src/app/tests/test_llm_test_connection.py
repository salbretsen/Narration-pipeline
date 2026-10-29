"""Settings > Narrative > Local LLM: the Test connection button (endpoint and model name check, no book touched)."""
from __future__ import annotations

import httpx

from studio import services as S
from studio.config import LLMSettings
from studio.narrative.llm import LLMClient


def _client(handler, model="good-model"):
    cfg = LLMSettings(provider="openai_compatible", base_url="http://127.0.0.1:1234/v1", model=model)
    return LLMClient(cfg, transport=httpx.MockTransport(handler))


def _ok(request):
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "good-model"}, {"id": "other"}]})
    return httpx.Response(200, json={"choices": [{"message": {"content": "OK"}}]})


def test_connection_ok():
    r = _client(_ok).test_connection()
    assert r["ok"] and [c["name"] for c in r["checks"]] == ["Reach the endpoint", "Model is listed", "Ask the model"]


def test_wrong_model_is_named_with_alternatives():
    r = _client(_ok, model="typo").test_connection()
    listed = next(c for c in r["checks"] if c["name"] == "Model is listed")
    assert not r["ok"] and not listed["ok"] and "good-model" in listed["detail"]


def test_unreachable_and_bad_key_and_no_models_endpoint():
    def down(request):
        raise httpx.ConnectError("refused")

    r = _client(down).test_connection()
    assert not r["ok"] and "Could not connect" in r["checks"][0]["detail"]
    r = _client(lambda req: httpx.Response(401, json={})).test_connection()
    assert not r["ok"] and "key" in r["checks"][0]["detail"]

    def no_list(request):   # some servers have no /models
        return httpx.Response(404) if request.url.path.endswith("/models") else _ok(request)

    assert _client(no_list).test_connection()["ok"]
    assert not _client(_ok, model="").test_connection()["ok"]


def test_chat_failure_reported():
    def bad_chat(request):
        return _ok(request) if request.url.path.endswith("/models") else httpx.Response(500, text="boom")

    r = _client(bad_chat).test_connection()
    assert not r["ok"] and "HTTP 500" in r["checks"][-1]["detail"]


def test_service_validates_url_and_endpoint_is_admin_only(ctx):
    import pytest

    with pytest.raises(S.ServiceError):
        S.test_llm(ctx, "ftp://nope", "m")
    from studio.web.auth import is_admin_only

    assert is_admin_only("POST", "/api/narrative/llm/test")
