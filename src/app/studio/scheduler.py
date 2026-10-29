"""QueueScheduler implementation used to drain the queue for an update.

A small JSON file (``run/dispatch.hold``) is the hold: the worker consults it before every unit of work, so the segment
being synthesised always finishes (and is written to disk) and nothing new starts. Running jobs keep their ``running`` status
and are re-queued at the same position by the worker's normal crash recovery after the restart; completed WAVs are never touched."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .interfaces import HoldRequest, QueueScheduler
from .util import atomic_write_text

BUSY_STATES = ("generating", "analyzing", "assembling", "previewing", "verifying")
HEARTBEAT_FRESH_S = 30.0


class FileQueueScheduler(QueueScheduler):
    def __init__(self, run_dir: Path, db=None):
        self.path = Path(run_dir) / "dispatch.hold"
        self.db = db

    def hold(self, mode: str, reason: str = "update") -> None:
        if mode not in ("now", "after_job"):
            raise ValueError("mode must be 'now' or 'after_job'")
        atomic_write_text(self.path, json.dumps({"mode": mode, "reason": reason, "since": time.time()}))

    def release(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def current_hold(self) -> HoldRequest | None:
        try:
            d = json.loads(self.path.read_text("utf-8"))
            return HoldRequest(d["mode"], d.get("reason", ""), float(d.get("since", 0)))
        except (OSError, ValueError, KeyError):
            return None

    def allows(self, job: dict) -> bool:
        h = self.current_hold()
        if h is None:
            return True
        return h.mode == "after_job" and job.get("status") == "running"

    def drained(self) -> bool:
        if self.db is None:
            return True
        h = self.current_hold()
        with self.db.session() as conn:
            w = conn.execute("SELECT state, heartbeat_at FROM worker_state WHERE id=1").fetchone()
            if w and w["state"] in BUSY_STATES and (time.time() - (w["heartbeat_at"] or 0)) < HEARTBEAT_FRESH_S:
                return False
            if h is not None and h.mode == "after_job":
                n = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='running' AND kind IN ('book','analyze','assemble','export')").fetchone()[0]
                return n == 0
        return True

    def running_summary(self) -> str:
        """Human text for the Update Center while waiting."""
        if self.db is None:
            return ""
        with self.db.session() as conn:
            r = conn.execute("""SELECT b.title FROM jobs j LEFT JOIN books b ON b.id=j.book_id
                                WHERE j.status='running' ORDER BY j.id LIMIT 1""").fetchone()
        return r["title"] if r and r["title"] else ""
