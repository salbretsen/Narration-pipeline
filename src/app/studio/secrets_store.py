"""API keys for outbound OpenAI-compatible endpoints, entered through the web UI.

Keys live in ``<home>/config/secrets.json`` (mode 0600), never in ``settings.json`` (which the API
returns), the database, the audit log or the event log. The API is write-only: it reports whether a
key is set, never the key. Every key has a named *slot*; a feature that talks to an endpoint reads
its own slot, so a new endpoint only needs an entry in ``SLOTS``.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from .util import now

# slot -> label shown in the UI
SLOTS: dict[str, str] = {
    "llm": "Narrative Director / LLM endpoint",
}
LLM_SLOT = "llm"

_KEY_RE = re.compile(r"^[\x21-\x7e]{1,400}$")  # printable ASCII, no spaces/newlines: it becomes an HTTP header value


class SecretError(ValueError):
    pass


class SecretStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text("utf-8") or "{}")
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2))
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @staticmethod
    def _check_slot(slot: str) -> None:
        if slot not in SLOTS:
            raise SecretError(f"Unknown key slot '{slot}'.")

    def get(self, slot: str) -> str:
        """The key, or '' if none is stored. Server-side use only."""
        entry = self._load().get(slot)
        return str(entry.get("value", "")) if isinstance(entry, dict) else ""

    def set(self, slot: str, value: str) -> None:
        self._check_slot(slot)
        value = (value or "").strip()
        if not value:
            raise SecretError("The API key is empty.")
        if not _KEY_RE.match(value):
            raise SecretError("An API key is 1-400 visible characters without spaces or line breaks.")
        with self._lock:
            data = self._load()
            data[slot] = {"value": value, "updated_at": now()}
            self._save(data)

    def delete(self, slot: str) -> bool:
        self._check_slot(slot)
        with self._lock:
            data = self._load()
            existed = data.pop(slot, None) is not None
            if existed:
                self._save(data)
            return existed

    def status(self) -> dict:
        """What the UI may know: which slots have a key and when it was saved."""
        data = self._load()
        out = {}
        for slot, label in SLOTS.items():
            entry = data.get(slot)
            has = isinstance(entry, dict) and bool(entry.get("value"))
            out[slot] = {"label": label, "configured": has, "updated_at": entry.get("updated_at") if has else None}
        return out
