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

log = logging.getLogger("studio.llm")
T = TypeVar("T")


class LLMError(RuntimeError):
    """The LLM endpoint is unreachable, misconfigured, or returned something unusable."""


class LLMClient:
    def __init__(self, cfg: LLMSettings, transport=None, environ=None):
        self.cfg = cfg
        self._transport = transport  # httpx transport, injected by tests
        self._env = environ if environ is not None else os.environ
        self.calls = 0
        self.seconds = 0.0

    @property
    def enabled(self) -> bool:
        return self.cfg.provider == "openai_compatible" and bool(self.cfg.model.strip())

    @property
    def model_label(self) -> str:
        return f"{self.cfg.provider}:{self.cfg.model}" if self.enabled else "rules"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.cfg.api_key_env:
            key = self._env.get(self.cfg.api_key_env, "")
            if not key:
                raise LLMError(f"Environment variable {self.cfg.api_key_env} (the API key) is not set for the worker process")
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
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        t0 = time.time()
        try:
            with httpx.Client(timeout=self.cfg.timeout_s, transport=self._transport) as c:
                r = c.post(f"{self.cfg.base_url}/chat/completions", headers=self._headers(), json=body)
                if r.status_code == 400 and json_mode:  # server without response_format support
                    body.pop("response_format")
                    r = c.post(f"{self.cfg.base_url}/chat/completions", headers=self._headers(), json=body)
                r.raise_for_status()
                data = r.json()
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(f"LLM request to {self.cfg.base_url} failed: {type(exc).__name__}: {exc}") from exc
        finally:
            self.calls += 1
            self.seconds += time.time() - t0
        try:
            return str(data["choices"][0]["message"]["content"] or "")
        except Exception as exc:
            raise LLMError(f"Unexpected LLM response shape: {str(data)[:200]}") from exc

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
