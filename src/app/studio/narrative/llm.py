"""Minimal OpenAI-compatible chat client for a local LLM (LM Studio, Ollama's /v1, mlx_lm.server,
llama.cpp server...). Uses httpx, which the application already depends on."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Callable, TypeVar

from ..config import LLMSettings
from ..secrets_store import LLM_SLOT

log = logging.getLogger("studio.llm")
T = TypeVar("T")


# Thinking models (Gemma 4, Qwen3...) otherwise spend max_tokens on hidden reasoning and answer with nothing or a stump. Servers that do not
# know the field ignore it; a server that answers 400 has it dropped (see LLMClient.chat).
NO_THINKING = {"enable_thinking": False}
THINKING = {"enable_thinking": True}
OPTIONAL_FIELDS = ("response_format", "chat_template_kwargs")


class LLMError(RuntimeError):
    """The LLM endpoint is unreachable, misconfigured, or returned something unusable."""


def _strip_think(text: str) -> str:
    """Remove a ``<think>...</think>`` block a server left inline in the answer (an unclosed one hides the rest)."""
    return re.sub(r"<think>.*?(?:</think>|$)", "", text, flags=re.S).strip()


class LLMClient:
    def __init__(self, cfg: LLMSettings, transport=None, environ=None, secrets=None, slot: str = LLM_SLOT):
        self.cfg = cfg
        self._transport = transport  # httpx transport, injected by tests
        self._env = environ if environ is not None else os.environ
        self._secrets = secrets  # SecretStore holding the key entered in the web UI
        self._slot = slot
        self.calls = 0
        self.seconds = 0.0
        self._rejected: set[str] = set()  # optional request fields this server answered 400 to
        self.thinking = False  # True: ask the model to reason first (no JSON-constrained decoding); see chat()
        self.last_reasoning = ""  # the reasoning text of the most recent chat() reply, when the server returned it

    def derive(self, *, model: str | None = None, thinking: bool = False, max_tokens: int | None = None, timeout_s: float | None = None) -> "LLMClient":
        """A client for another pass (other model, thinking on, bigger budget). Shares the transport, key sources and the set of request
        fields this server rejected; a blank or None argument keeps this client's value."""
        update: dict = {}
        if model and model.strip():
            update["model"] = model.strip()
        if max_tokens:
            update["max_tokens"] = int(max_tokens)
        if timeout_s:
            update["timeout_s"] = float(timeout_s)
        child = LLMClient(self.cfg.model_copy(update=update), transport=self._transport, environ=self._env, secrets=self._secrets, slot=self._slot)
        child._rejected = self._rejected  # the same set object: one server, one set of quirks
        child.thinking = bool(thinking)
        return child

    @property
    def enabled(self) -> bool:
        return self.cfg.provider == "openai_compatible" and bool(self.cfg.model.strip())

    @property
    def model_label(self) -> str:
        return f"{self.cfg.provider}:{self.cfg.model}" if self.enabled else "rules"

    def _extra_models(self) -> list[tuple[str, str]]:
        """(check name, model) for each pass model that differs from the main one."""
        out = []
        for label, name in (("Chapter", self.cfg.chapter_model), ("Scene", self.cfg.scene_model), ("Translator", self.cfg.translator_model)):
            name = (name or "").strip()
            if name and name != self.cfg.model:
                out.append((f"{label} model is listed", name))
        return out

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        # The key saved in the web UI wins; a named environment variable is the fallback.
        key = self._secrets.get(self._slot) if self._secrets is not None else ""
        if not key and self.cfg.api_key_env:
            key = self._env.get(self.cfg.api_key_env, "")
            if not key:
                raise LLMError(f"No API key: none is saved in Settings and environment variable {self.cfg.api_key_env} is not set for the worker process")
        if key:
            h["Authorization"] = f"Bearer {key}"
        return h

    def chat(self, system: str, user: str, json_mode: bool = True) -> str:
        if not self.enabled:
            raise LLMError("No LLM is configured (Settings > Narrative > Local LLM)")
        import httpx

        body = {
            "model": self.cfg.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.cfg.temperature,
            "max_tokens": self.cfg.max_tokens,
            "stream": False,
        }
        self.last_reasoning = ""
        if json_mode and not self.thinking:  # thinking + JSON-constrained decoding degenerates on the owner's server
            body["response_format"] = {"type": "json_object"}
        body["chat_template_kwargs"] = THINKING if self.thinking else NO_THINKING
        for field in self._rejected:
            body.pop(field, None)
        dropped: list[str] = []
        t0 = time.time()
        try:
            with httpx.Client(timeout=self.cfg.timeout_s, transport=self._transport) as c:
                r = c.post(f"{self.cfg.base_url}/chat/completions", headers=self._headers(), json=body)
                while r.status_code == 400:  # a server that rejects an optional field: drop them one at a time, JSON mode first
                    field = next((f for f in OPTIONAL_FIELDS if f in body), None)
                    if field is None:
                        break
                    body.pop(field)
                    dropped.append(field)
                    r = c.post(f"{self.cfg.base_url}/chat/completions", headers=self._headers(), json=body)
                r.raise_for_status()
                self._rejected.update(dropped)  # remembered only once the retry worked, so a one-off 400 does not turn the options off for good
                data = r.json()
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"LLM request to {self.cfg.base_url} failed: {type(exc).__name__}: {exc}") from exc
        finally:
            self.calls += 1
            self.seconds += time.time() - t0
        try:
            choice = data["choices"][0]
            msg = choice["message"]
            text = _strip_think(str(msg.get("content") or ""))
            finish = choice.get("finish_reason")
        except Exception as exc:
            raise LLMError(f"Unexpected LLM response shape: {str(data)[:200]}") from exc
        self.last_reasoning = str(msg.get("reasoning_content") or msg.get("reasoning") or "")
        if self.thinking and finish == "length":
            raise LLMError(f"The thinking model's reply was cut off at max_tokens ({self.cfg.max_tokens}); a truncated reply is unusable. "
                           "Raise the chapter pass token budget in Settings > Narrative > Local LLM")
        if not text.strip():
            if self.thinking and self.last_reasoning:
                raise LLMError(f"The thinking model gave reasoning but no answer within max_tokens ({self.cfg.max_tokens})")
            if msg.get("reasoning_content") or msg.get("reasoning"):
                raise LLMError(f"The model spent its whole max_tokens budget ({self.cfg.max_tokens}) on reasoning and gave no answer; "
                               "its server may ignore the request to skip thinking")
            raise LLMError(f"The model returned an empty answer (finish_reason={finish})")
        if finish == "length":
            log.warning("LLM reply was cut off at max_tokens=%d; raise Max reply tokens in Settings > Narrative > Local LLM", self.cfg.max_tokens)
        return text

    def test_connection(self) -> dict:
        """Check the endpoint and model name without touching any book: list the models (informational; not every server offers it),
        then ask one tiny question. Returns ``{ok, checks:[{name, ok, detail}], seconds}``; never raises."""
        import httpx

        checks: list[dict] = []
        t0 = time.time()

        def done(ok: bool) -> dict:
            return {"ok": ok, "checks": checks, "seconds": round(time.time() - t0, 2)}

        if not self.cfg.model.strip():
            checks.append({"name": "Model name", "ok": False, "detail": "Enter the model name first."})
            return done(False)
        try:
            headers = self._headers()
        except LLMError as exc:
            checks.append({"name": "API key", "ok": False, "detail": str(exc)})
            return done(False)
        base = self.cfg.base_url
        try:
            with httpx.Client(timeout=min(self.cfg.timeout_s, 30.0), transport=self._transport) as c:
                try:
                    r = c.get(f"{base}/models", headers=headers)
                except httpx.HTTPError as exc:
                    checks.append({"name": "Reach the endpoint", "ok": False,
                                   "detail": f"Could not connect to {base}: {type(exc).__name__}. Is the server running, and is the address and port right?"})
                    return done(False)
                if r.status_code in (401, 403):
                    checks.append({"name": "Reach the endpoint", "ok": False, "detail": f"The server answered HTTP {r.status_code}: the API key is missing or wrong."})
                    return done(False)
                checks.append({"name": "Reach the endpoint", "ok": True, "detail": f"{base} answered (HTTP {r.status_code})."})
                ids: list[str] = []
                if r.status_code == 200:
                    try:
                        ids = [str(m.get("id")) for m in r.json().get("data", []) if isinstance(m, dict)]
                    except ValueError:
                        ids = []
                if ids:
                    listed = self.cfg.model in ids
                    checks.append({"name": "Model is listed", "ok": listed,
                                   "detail": f"'{self.cfg.model}' is available." if listed else
                                   f"'{self.cfg.model}' is not in the server's list. Available: {', '.join(ids[:8])}{' ...' if len(ids) > 8 else ''}"})
                else:
                    checks.append({"name": "Model is listed", "ok": True, "detail": "The server does not list its models; trying the model directly."})
                body = {"model": self.cfg.model, "messages": [{"role": "user", "content": "Reply with the single word OK."}],
                        "temperature": 0, "max_tokens": 16, "stream": False, "chat_template_kwargs": NO_THINKING}
                try:
                    r = c.post(f"{base}/chat/completions", headers=headers, json=body)
                except httpx.HTTPError as exc:
                    checks.append({"name": "Ask the model", "ok": False, "detail": f"No answer: {type(exc).__name__}. A slow model may need a longer timeout."})
                    return done(False)
                if r.status_code == 400:  # same fallback as chat(): a server that rejects the no-thinking field
                    body.pop("chat_template_kwargs")
                    r = c.post(f"{base}/chat/completions", headers=headers, json=body)
                if r.status_code != 200:
                    checks.append({"name": "Ask the model", "ok": False, "detail": f"HTTP {r.status_code}: {r.text[:200]}"})
                    return done(False)
                try:
                    msg = r.json()["choices"][0]["message"]
                    reply = _strip_think(str(msg.get("content") or ""))
                except Exception:
                    checks.append({"name": "Ask the model", "ok": False, "detail": f"The reply was not a chat completion: {r.text[:160]}"})
                    return done(False)
                if not reply:
                    thinking = bool(msg.get("reasoning_content") or msg.get("reasoning"))
                    checks.append({"name": "Ask the model", "ok": False,
                                   "detail": "The model answered with nothing" + (": it used its tokens on hidden reasoning, so narration requests would come back empty."
                                                                                 if thinking else ".")})
                    return done(False)
                checks.append({"name": "Ask the model", "ok": True, "detail": f"Replied: {reply[:80]}"})
                if ids:  # extra pass models: only checkable when the server lists its models
                    for check_name, name in self._extra_models():
                        listed = name in ids
                        checks.append({"name": check_name, "ok": listed,
                                       "detail": f"'{name}' is available." if listed else
                                       f"'{name}' is not in the server's list. Available: {', '.join(ids[:8])}{' ...' if len(ids) > 8 else ''}"})
        except Exception as exc:  # defensive: a test must report, never crash
            checks.append({"name": "Unexpected error", "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
        return done(all(c["ok"] for c in checks))

    def chat_json(self, system: str, user: str, validate: Callable[[dict], T], retries: int = 1) -> T:
        """Ask for one JSON object and validate it. One repair round is attempted with the
        validation error fed back to the model."""
        prompt, last = user, ""
        for attempt in range(retries + 1):
            text = self.chat(system, prompt)
            try:
                return validate(extract_json(text))
            except (ValueError, KeyError, TypeError) as exc:
                last = str(exc)
                log.info("LLM output rejected (%s), attempt %d", last[:200], attempt + 1)
                prompt = user + f"\n\nYour previous reply was rejected: {last[:300]}\nReply again with ONLY the corrected JSON object."
        raise LLMError(f"LLM output failed validation: {last[:300]}")


def extract_json(text: str) -> dict:
    """First JSON object in a model reply (tolerates code fences and chatter around it)."""
    text = re.sub(r"```(?:json)?", "", text)
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSON: {exc}") from exc
                if not isinstance(obj, dict):
                    raise ValueError("JSON reply is not an object")
                return obj
    raise ValueError("unterminated JSON object")


def resolve_models(llm_cfg: LLMSettings) -> dict:
    """Which model each pass uses: chapter and scene fall back to ``model``; no translator model means no LLM translator (None)."""
    base = (llm_cfg.model or "").strip()
    return {
        "chapter": (llm_cfg.chapter_model or "").strip() or base,
        "scene": (llm_cfg.scene_model or "").strip() or base,
        "translator": (llm_cfg.translator_model or "").strip() or None,
    }
