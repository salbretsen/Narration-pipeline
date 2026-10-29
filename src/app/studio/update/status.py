"""Persisted update state shown by the Update Center. One JSON file written atomically by whichever process is working
(web process during check/download/stage, the helper during install), so the UI sees progress from both and survives a restart."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from ..util import atomic_write_text, now

PHASES = ("check", "download", "verify", "stage", "drain", "install", "restart", "health")
STATES = ("idle", "checking", "up_to_date", "available", "downloading", "verifying", "staging", "staged", "waiting",
          "draining", "installing", "restarting", "health_check", "succeeded", "failed", "rolled_back", "incompatible", "offline", "disabled")


class StatusFile:
    def __init__(self, update_dir: Path):
        self.dir = Path(update_dir)
        self.path = self.dir / "status.json"
        self.history = self.dir / "history.jsonl"
        self._lock = threading.Lock()

    def read(self) -> dict:
        try:
            d = json.loads(self.path.read_text("utf-8"))
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def update(self, **fields) -> dict:
        with self._lock:
            d = self.read()
            phases = fields.pop("phases", None)
            if phases is None:
                phases = d.get("phases") or {}
            ph = fields.pop("phase", None)
            if ph:
                name, state, pct, detail = ph
                phases[name] = {"state": state, "pct": pct, "detail": detail, "at": now()}
            d.update(fields)
            d["phases"] = phases
            d["updated_at"] = now()
            self.dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.path, json.dumps(d, indent=2))
            return d

    def reset_phases(self) -> None:
        self.update(phases={})

    def add_history(self, event: str, version: str | None, ok: bool, message: str, **extra) -> None:
        rec = {"at": now(), "event": event, "version": version, "ok": ok, "message": message[:1000], **extra}
        self.dir.mkdir(parents=True, exist_ok=True)
        with self._lock, open(self.history, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def read_history(self, limit: int = 50) -> list[dict]:
        try:
            lines = self.history.read_text("utf-8").splitlines()
        except OSError:
            return []
        out = []
        for ln in lines[-limit:]:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return list(reversed(out))
