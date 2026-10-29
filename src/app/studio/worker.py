"""The single GPU worker: a durable, serial scheduler over the SQLite job queue.

* Exactly one worker owns the GPU at a time (SQLite lease + heartbeat; stale leases are taken over).
* Priority: preview jobs, then short utility jobs (assemble/export), then book jobs in FIFO
  ``position`` order. Previews run *between* book segments, so they never wait for a book.
* Book jobs advance one segment per step; pause/cancel are honoured between segments
  (a single MLX call cannot be interrupted safely).
* A book whose storage disappears is paused (not failed) and auto-resumed when it returns.
* An irrecoverable error fails only that book; the queue continues with the next one.
"""
from __future__ import annotations

import logging
import os
import platform
import signal
import subprocess
import sys
import threading
import time
import traceback
import uuid

from . import assembly
from .db import row, rows
from .engines import EngineUnavailable, VoiceSpec, SynthParams, create_asr, create_tts, max_tokens_for
from .narrative import store as N_store
from .pipeline import StorageUnavailable, check_storage, process_segment, recover_interrupted
from . import audio as A
from .util import dumps, loads, now

log = logging.getLogger("studio.worker")
LEASE_STALE_S = 60.0
HEARTBEAT_S = 5.0
ACTIVE = ("queued", "running", "paused")


def refresh_book_state(conn, book_id: int) -> str:
    """Derive and store the lifecycle state shown in the UI."""
    book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
    if not book:
        return "missing"
    counts = {r["status"]: r["n"] for r in rows(conn, "SELECT status, COUNT(*) n FROM segments WHERE book_id=? GROUP BY status", (book_id,))}
    job = row(conn, "SELECT * FROM jobs WHERE book_id=? AND kind='book' ORDER BY id DESC LIMIT 1", (book_id,))
    release = row(conn, "SELECT id FROM releases WHERE book_id=? AND revoked_at IS NULL", (book_id,))
    total = sum(counts.values())
    pending = counts.get("pending", 0) + counts.get("synthesizing", 0)
    if job and job["status"] in ("queued", "running", "paused", "failed") and (pending or job["status"] == "failed"):
        state = {"queued": "queued", "running": "generating", "paused": "paused", "failed": "failed"}[job["status"]]
    elif release:
        state = "approved"
    elif total and pending == total:
        state = "imported"
    elif pending:
        state = "incomplete"
    elif counts.get("flagged", 0):
        state = "needs_review"
    else:
        state = "qa_passed"
    conn.execute("UPDATE books SET state=?, updated_at=? WHERE id=?", (state, now(), book_id))
    return state


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    if pid == os.getpid():
        return True
    if os.name != "posix":
        return True  # no cheap portable check: rely on the heartbeat timeout
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class _Caffeinate:
    """Prevent idle sleep on macOS while jobs are active (``caffeinate -i -w <pid>``)."""

    def __init__(self, enabled: bool):
        self.enabled = enabled and sys.platform == "darwin"
        self.proc: subprocess.Popen | None = None

    def hold(self, active: bool) -> None:
        if not self.enabled:
            return
        if active and (self.proc is None or self.proc.poll() is not None):
            try:
                self.proc = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())])
            except OSError:
                self.enabled = False
        elif not active and self.proc is not None:
            self.proc.terminate()
            self.proc = None


class Worker:
    def __init__(self, ctx, tts=None, asr=None, owner: str | None = None):
        self.ctx = ctx
        self._tts, self._asr = tts, asr
        self.owner = owner or f"{platform.node()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.stop_event = threading.Event()
        self._hb_thread: threading.Thread | None = None
        self._state = {"state": "idle", "job": None, "sid": None, "message": ""}
        self.caffeinate = _Caffeinate(ctx.settings().worker.prevent_sleep)

    # -------------------------------------------------------------- engines
    @property
    def tts(self):
        if self._tts is None:
            self._tts = create_tts(self.ctx)
        return self._tts

    @property
    def asr(self):
        if self._asr is None:
            self._asr = create_asr(self.ctx)
        return self._asr

    # ---------------------------------------------------------------- lease
    def acquire_lease(self) -> bool:
        t = now()
        with self.ctx.db.tx() as conn:
            cur_owner = row(conn, "SELECT owner, pid FROM worker_state WHERE id=1")
            if cur_owner and cur_owner["owner"] and cur_owner["owner"] != self.owner:
                host = cur_owner["owner"].split(":", 1)[0]
                if host == platform.node() and not _pid_alive(cur_owner["pid"]):
                    # the previous worker on this machine is gone (crash/kill): take over now
                    conn.execute("UPDATE worker_state SET heartbeat_at=0 WHERE id=1")
            cur = conn.execute(
                """UPDATE worker_state SET owner=?, pid=?, heartbeat_at=?, state='starting', started_at=?, engine=?
                   WHERE id=1 AND (owner IS NULL OR owner=? OR heartbeat_at IS NULL OR heartbeat_at < ?)""",
                (self.owner, os.getpid(), t, t, self.ctx.engine_mode, self.owner, t - LEASE_STALE_S),
            )
            return cur.rowcount == 1

    def release_lease(self) -> None:
        with self.ctx.db.tx() as conn:
            conn.execute("UPDATE worker_state SET owner=NULL, state='offline', current_job_id=NULL, current_segment_sid=NULL, message=? WHERE id=1 AND owner=?",
                         ("stopped", self.owner))

    def _set(self, **kw) -> None:
        self._state.update(kw)
        self._write_state()

    def _write_state(self) -> bool:
        st = self._state
        with self.ctx.db.tx() as conn:
            cur = conn.execute(
                "UPDATE worker_state SET heartbeat_at=?, state=?, current_job_id=?, current_segment_sid=?, message=?, pid=? WHERE id=1 AND owner=?",
                (now(), st["state"], st["job"], st["sid"], st["message"][:500], os.getpid(), self.owner),
            )
            return cur.rowcount == 1

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.wait(HEARTBEAT_S):
            try:
                if not self._write_state():
                    log.error("worker lease lost; stopping")
                    self.stop_event.set()
            except Exception:
                log.exception("heartbeat failed")

    # ------------------------------------------------------------- recovery
    def recover(self) -> dict:
        with self.ctx.db.tx() as conn:
            info = recover_interrupted(conn)
            # interrupted running jobs go back to the queue, keeping their position (front)
            n = conn.execute("UPDATE jobs SET status='queued', updated_at=? WHERE status='running'", (now(),)).rowcount
            info["requeued_jobs"] = n
            for b in rows(conn, "SELECT DISTINCT book_id FROM jobs WHERE book_id IS NOT NULL"):
                refresh_book_state(conn, b["book_id"])
            if any(info.values()):
                self.ctx.event(conn, "recovery", f"Recovered after restart: {info}")
        return info

    # ------------------------------------------------------------- scheduling
    def _next_job(self, conn) -> dict | None:
        j = row(conn, "SELECT * FROM jobs WHERE kind='preview' AND status='queued' ORDER BY id LIMIT 1")
        if j:
            return j
        j = row(conn, "SELECT * FROM jobs WHERE kind IN ('assemble','export') AND status IN ('queued','running') ORDER BY id LIMIT 1")
        if j:
            return j
        j = row(conn, "SELECT * FROM jobs WHERE kind='analyze' AND status IN ('queued','running') ORDER BY id LIMIT 1")
        if j:
            return j
        j = row(conn, "SELECT * FROM jobs WHERE kind='book' AND status='running' ORDER BY position LIMIT 1")
        if j:
            return j
        return row(conn, "SELECT * FROM jobs WHERE kind='book' AND status='queued' ORDER BY position, id LIMIT 1")

    def _resume_storage_paused(self) -> None:
        with self.ctx.db.session() as conn:
            paused = rows(conn, "SELECT j.id, j.book_id FROM jobs j WHERE j.status='paused' AND j.paused_reason LIKE 'storage:%'")
        for p in paused:
            with self.ctx.db.session() as conn:
                book = row(conn, "SELECT * FROM books WHERE id=?", (p["book_id"],))
            try:
                check_storage(book)
            except StorageUnavailable:
                continue
            with self.ctx.db.tx() as conn:
                conn.execute("UPDATE jobs SET status='queued', paused_reason=NULL, updated_at=? WHERE id=? AND status='paused'", (now(), p["id"]))
                self.ctx.job_log(conn, p["id"], p["book_id"], "Storage is available again; job resumed automatically.")
                refresh_book_state(conn, p["book_id"])

    def step(self) -> bool:
        """Do one unit of work. Returns False when there is nothing to do."""
        with self.ctx.db.session() as conn:
            job = self._next_job(conn)
        if not job:
            self._resume_storage_paused()
            with self.ctx.db.session() as conn:
                job = self._next_job(conn)
            if not job:
                return False
        try:
            if job["kind"] == "preview":
                self._run_preview(job)
            elif job["kind"] in ("assemble", "export"):
                self._run_utility(job)
            elif job["kind"] == "analyze":
                self._run_analyze(job)
            else:
                self._run_book_step(job)
        except Exception as exc:
            log.exception("job %s failed", job["id"])
            self._fail(job, f"{type(exc).__name__}: {exc}", traceback.format_exc())
        return True

    def run_until_idle(self, max_steps: int = 100000) -> int:
        steps = 0
        while steps < max_steps and self.step():
            steps += 1
        return steps

    # --------------------------------------------------------------- job kinds
    def _fail(self, job: dict, message: str, tb: str = "") -> None:
        with self.ctx.db.tx() as conn:
            conn.execute("UPDATE jobs SET status='failed', error=?, finished_at=?, updated_at=? WHERE id=?", (message[:2000], now(), now(), job["id"]))
            self.ctx.job_log(conn, job["id"], job["book_id"], f"Job failed: {message}", "error")
            if tb:
                self.ctx.job_log(conn, job["id"], job["book_id"], tb[-3000:], "debug")
            if job["book_id"]:
                conn.execute("UPDATE books SET last_error=? WHERE id=?", (message[:2000], job["book_id"]))
                if job["kind"] == "book":
                    refresh_book_state(conn, job["book_id"])
            self.ctx.event(conn, "job_failed", f"{job['kind']} job #{job['id']} failed: {message[:300]}", job["book_id"], "error")

    def _run_recording_job(self, job: dict, p: dict) -> None:
        from . import reference as R
        from .align import evaluate

        with self.ctx.db.tx() as conn:
            conn.execute("UPDATE jobs SET status='running', started_at=?, updated_at=? WHERE id=?", (now(), now(), job["id"]))
        rdir = self.ctx.data_dir / "recordings" / p["recording_id"]
        meta = loads((rdir / "meta.json").read_text("utf-8"), {})
        ref = rdir / "reference-24k.wav"
        s = self.ctx.settings()
        if p["mode"] == "check":
            self._set(state="verifying", job=job["id"], sid=None, message="Checking a reference recording with Whisper")
            tr = self.asr.transcribe(ref)
            ev = evaluate(meta["ref_text"], tr["text"], tr.get("words"), meta.get("duration_s"), 1, 1)
            errors = [i for i in ev["issues"] if i["severity"] == "error"]
            result = {"mode": "check", "asr_text": tr["text"], "expected": meta["ref_text"], "wer": ev["wer"], "diff": ev["diff"], "issues": ev["issues"],
                      "matches": ev["wer"] <= 0.08 and not errors, "engine": tr.get("engine")}
        else:
            self._set(state="previewing", job=job["id"], sid=None, message="Audition: cloning from a recording")
            spec = VoiceSpec(kind="clone", model_id=s.models.base_model, language=s.models.language, ref_audio_path=str(ref), ref_text=meta["ref_text"])
            text = p["text"]
            res = self.tts.synthesize(spec, text, SynthParams(seed=1234, max_tokens=max_tokens_for(text)))
            out = self.ctx.previews_dir() / f"preview-{job['id']}.wav"
            A.write_wav(out, res.audio, res.sample_rate)
            dur = round(res.audio.size / res.sample_rate, 2)
            tr = self.asr.transcribe(out)
            ev = evaluate(text, tr["text"], tr.get("words"), dur)
            result = {"mode": "audition", "path": str(out), "duration_s": dur, "engine": res.engine, "model_id": res.model_id, "rtf": res.rtf,
                      "peak_mem_gb": res.peak_mem_gb, "sample_rate": res.sample_rate, "asr_text": tr["text"], "wer": ev["wer"], "issues": ev["issues"]}
            if res.engine == "mock":
                result["note"] = "Mock engine: synthetic test tones, not speech."
        with self.ctx.db.tx() as conn:
            conn.execute("UPDATE jobs SET status='completed', result_json=?, finished_at=?, updated_at=? WHERE id=?", (dumps(result), now(), now(), job["id"]))

    def _run_preview(self, job: dict) -> None:
        p = loads(job["payload_json"], {})
        if p.get("recording_id"):
            return self._run_recording_job(job, p)
        if p.get("segment_id"):
            return self._run_segment_preview(job, p)
        with self.ctx.db.tx() as conn:
            conn.execute("UPDATE jobs SET status='running', started_at=?, updated_at=? WHERE id=?", (now(), now(), job["id"]))
            vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (p["voice_version_id"],))
            v = row(conn, "SELECT * FROM voices WHERE id=?", (vv["voice_id"],)) if vv else None
        if not vv or not v:
            raise ValueError("voice not found for preview")
        self._set(state="previewing", job=job["id"], sid=None, message=f"Preview: {v['name']}")
        spec = VoiceSpec.from_rows(v, vv)
        text = p["text"]
        params = SynthParams(seed=int(p.get("seed", 1234)), temperature=p.get("temperature"), max_tokens=max_tokens_for(text), instruct=p.get("instruct"))
        res = self.tts.synthesize(spec, text, params)
        out = self.ctx.previews_dir() / f"preview-{job['id']}.wav"
        A.write_wav(out, res.audio, res.sample_rate)
        result = {"path": str(out), "duration_s": round(res.audio.size / res.sample_rate, 2), "engine": res.engine, "model_id": res.model_id,
                  "rtf": res.rtf, "peak_mem_gb": res.peak_mem_gb, "seconds": round(res.seconds, 2), "sample_rate": res.sample_rate}
        if p.get("verify"):
            tr = self.asr.transcribe(out)
            from .align import evaluate
            ev = evaluate(text, tr["text"], tr.get("words"), result["duration_s"])
            result.update({"asr_text": tr["text"], "wer": ev["wer"], "issues": ev["issues"]})
        if res.engine == "mock":
            result["note"] = "Mock engine: synthetic test tones, not speech."
        with self.ctx.db.tx() as conn:
            conn.execute("UPDATE jobs SET status='completed', result_json=?, finished_at=?, updated_at=? WHERE id=?", (dumps(result), now(), now(), job["id"]))
        self._cleanup_previews()

    def _run_segment_preview(self, job: dict, p: dict) -> None:
        """Preview one segment exactly as production would render it: its character voice, its delivery
        instruction, the same spoken text. Nothing is written to the segment's takes."""
        with self.ctx.db.tx() as conn:
            conn.execute("UPDATE jobs SET status='running', started_at=?, updated_at=? WHERE id=?", (now(), now(), job["id"]))
            seg = row(conn, "SELECT * FROM segments WHERE id=?", (p["segment_id"],))
            book = row(conn, "SELECT * FROM books WHERE id=?", (seg["book_id"],)) if seg else None
            plan = N_store.plan_for_segment(conn, self.ctx, seg, book) if seg else None
        if not plan:
            raise ValueError("segment not found for preview")
        self._set(state="previewing", job=job["id"], sid=seg["sid"], message=f"Preview of {seg['sid']}")
        text = plan.text
        params = SynthParams(seed=int(p.get("seed", 1234)), max_tokens=max_tokens_for(text), instruct=plan.instruct)
        res = self.tts.synthesize(plan.voice, text, params, sid=None)
        out = self.ctx.previews_dir() / f"preview-{job['id']}.wav"
        A.write_wav(out, res.audio, res.sample_rate)
        result = {"path": str(out), "duration_s": round(res.audio.size / res.sample_rate, 2), "engine": res.engine, "model_id": res.model_id,
                  "rtf": res.rtf, "peak_mem_gb": res.peak_mem_gb, "seconds": round(res.seconds, 2), "sample_rate": res.sample_rate,
                  "segment_id": seg["id"], "instruct": plan.instruct, "delivery_applied": plan.delivery_applied, "delivery_reason": plan.delivery_reason,
                  "text_sent_to_tts": text, "gen_key": plan.gen_key}
        if res.engine == "mock":
            result["note"] = "Mock engine: synthetic test tones, not speech."
        with self.ctx.db.tx() as conn:
            conn.execute("UPDATE jobs SET status='completed', result_json=?, finished_at=?, updated_at=? WHERE id=?", (dumps(result), now(), now(), job["id"]))
        self._cleanup_previews()

    def _cleanup_previews(self, keep: int = 60) -> None:
        files = sorted(self.ctx.previews_dir().glob("preview-*.wav"), key=lambda p: p.stat().st_mtime, reverse=True)
        with self.ctx.db.session() as conn:
            pinned = {r["path"] for r in rows(conn, "SELECT ref_original_path AS path FROM voice_versions WHERE ref_original_path IS NOT NULL")}
        for f in files[keep:]:
            if str(f) not in pinned:
                f.unlink(missing_ok=True)

    def _run_analyze(self, job: dict) -> None:
        """One bounded unit of narrative analysis (see narrative.analysis); pause/cancel apply between units."""
        from .narrative import analysis

        jid, bid = job["id"], job["book_id"]
        with self.ctx.db.tx() as conn:
            cur = row(conn, "SELECT * FROM jobs WHERE id=?", (jid,))
            if cur["cancel_requested"]:
                conn.execute("UPDATE jobs SET status='cancelled', finished_at=?, updated_at=? WHERE id=?", (now(), now(), jid))
                self.ctx.job_log(conn, jid, bid, "Analysis cancelled; completed stages are kept.")
                return
            if cur["pause_requested"]:
                conn.execute("UPDATE jobs SET status='paused', pause_requested=0, paused_reason='operator', updated_at=? WHERE id=?", (now(), jid))
                self.ctx.job_log(conn, jid, bid, "Analysis paused.")
                return
            title = row(conn, "SELECT title FROM books WHERE id=?", (bid,))["title"]
        self._set(state="analyzing", job=jid, sid=None, message=f"Narrative analysis: {title}")
        analysis.analysis_step(self.ctx, cur, getattr(self, "llm_transport", None))

    def _run_utility(self, job: dict) -> None:
        p = loads(job["payload_json"], {})
        with self.ctx.db.tx() as conn:
            conn.execute("UPDATE jobs SET status='running', started_at=COALESCE(started_at, ?), updated_at=? WHERE id=?", (now(), now(), job["id"]))
        self._set(state="assembling", job=job["id"], sid=None, message=f"{job['kind']} for book {job['book_id']}")

        def progress(msg):
            with self.ctx.db.tx() as conn:
                self.ctx.job_log(conn, job["id"], job["book_id"], msg)

        try:
            if job["kind"] == "assemble":
                result = assembly.assemble_book(self.ctx, job["book_id"], only_ready=True, progress=progress)
            else:
                result = assembly.export_book(self.ctx, job["book_id"], p.get("kind", "m4b"), p.get("chapter_id"), progress=progress)
        except assembly.AssemblyBlocked as exc:
            with self.ctx.db.tx() as conn:
                conn.execute("UPDATE jobs SET status='failed', error=?, result_json=?, finished_at=?, updated_at=? WHERE id=?",
                             (str(exc), dumps({"blocked": exc.details[:200]}), now(), now(), job["id"]))
                self.ctx.job_log(conn, job["id"], job["book_id"], f"Blocked: {exc}", "warning")
                self.ctx.event(conn, "blocked", str(exc), job["book_id"], "warning")
            return
        except StorageUnavailable as exc:
            with self.ctx.db.tx() as conn:
                conn.execute("UPDATE jobs SET status='failed', error=?, finished_at=?, updated_at=? WHERE id=?", (f"Storage unavailable: {exc}", now(), now(), job["id"]))
            return
        with self.ctx.db.tx() as conn:
            conn.execute("UPDATE jobs SET status='completed', result_json=?, finished_at=?, updated_at=? WHERE id=?", (dumps(result), now(), now(), job["id"]))
            self.ctx.job_log(conn, job["id"], job["book_id"], f"{job['kind']} completed")

    def _run_book_step(self, job: dict) -> None:
        jid, bid = job["id"], job["book_id"]
        with self.ctx.db.tx() as conn:
            cur = row(conn, "SELECT * FROM jobs WHERE id=?", (jid,))
            if cur["cancel_requested"]:
                conn.execute("UPDATE jobs SET status='cancelled', finished_at=?, updated_at=? WHERE id=?", (now(), now(), jid))
                self.ctx.job_log(conn, jid, bid, "Cancelled between segments; completed segments are kept.")
                refresh_book_state(conn, bid)
                return
            if cur["pause_requested"]:
                conn.execute("UPDATE jobs SET status='paused', pause_requested=0, paused_reason='operator', updated_at=? WHERE id=?", (now(), jid))
                self.ctx.job_log(conn, jid, bid, "Paused between segments.")
                refresh_book_state(conn, bid)
                return
            if cur["status"] == "queued":
                conn.execute("UPDATE jobs SET status='running', started_at=COALESCE(started_at, ?), updated_at=? WHERE id=?", (now(), now(), jid))
                self.ctx.job_log(conn, jid, bid, "Generation started" if not cur["started_at"] else "Generation resumed")
                refresh_book_state(conn, bid)
            book = row(conn, "SELECT * FROM books WHERE id=?", (bid,))
            seg = row(conn, """SELECT s.* FROM segments s JOIN chapters c ON c.id = s.chapter_id
                               WHERE s.book_id=? AND s.status IN ('pending','synthesizing')
                               ORDER BY c.idx, s.idx LIMIT 1""", (bid,))
        try:
            check_storage(book)
        except StorageUnavailable as exc:
            with self.ctx.db.tx() as conn:
                conn.execute("UPDATE jobs SET status='paused', paused_reason=?, updated_at=? WHERE id=?", (f"storage: {exc}", now(), jid))
                self.ctx.job_log(conn, jid, bid, f"Paused: {exc}. It resumes automatically when the storage is available.", "warning")
                self.ctx.event(conn, "storage", f"'{book['title']}' paused: {exc}", bid, "warning")
                refresh_book_state(conn, bid)
            return
        if seg is None:
            self._finish_book(job, book)
            return
        warm = getattr(self.tts, "warm_models", None)
        if warm is not None:
            with self.ctx.db.session() as conn:
                seg = N_store.pick_segment(conn, book, seg, warm())
        ns = N_store.book_ns(self.ctx, book)
        if ns.director_enabled:
            with self.ctx.db.session() as conn:
                has_dir = row(conn, "SELECT 1 FROM directions WHERE segment_id=?", (seg["id"],))
            if not has_dir:
                from .narrative import analysis

                self._set(state="analyzing", job=jid, sid=seg["sid"], message=f"{book['title']}: directing the next {ns.director_lookahead} segments")
                try:
                    analysis.director_batch(self.ctx, bid, ns.director_lookahead, analysis.make_llm(ns, getattr(self, "llm_transport", None)))
                except Exception as exc:  # a Director failure must never stop narration; the plan then has no delivery guidance
                    log.exception("director batch failed")
                    with self.ctx.db.tx() as conn:
                        self.ctx.job_log(conn, jid, bid, f"Narrative Director failed ({type(exc).__name__}: {exc}); narrating without delivery guidance.", "warning")
        self._set(state="generating", job=jid, sid=seg["sid"], message=f"{book['title']}: {seg['sid']}")

        def on_attempt(s, n):
            self._set(message=f"{book['title']}: {s['sid']} attempt {n}")

        try:
            status = process_segment(self.ctx, self.tts, self.asr, seg["id"], jid, on_attempt)
        except EngineUnavailable as exc:
            self._fail(job, f"Engine unavailable: {exc}")
            return
        except StorageUnavailable:
            return  # handled on the next step
        with self.ctx.db.tx() as conn:
            j = row(conn, "SELECT consecutive_errors FROM jobs WHERE id=?", (jid,))
            limit = self.ctx.settings().worker.max_consecutive_errors
            if j and j["consecutive_errors"] >= limit:
                conn.execute("UPDATE jobs SET status='failed', error=?, finished_at=?, updated_at=? WHERE id=?",
                             (f"{limit} consecutive segments produced no audio - the engine looks broken. Fix and press Retry.", now(), now(), jid))
                self.ctx.event(conn, "job_failed", f"'{book['title']}' stopped after {limit} consecutive synthesis failures", bid, "error")
            if status == "flagged":
                self.ctx.job_log(conn, jid, bid, f"{seg['sid']} flagged for human review after exhausting retries", "warning")
            conn.execute("UPDATE jobs SET updated_at=? WHERE id=?", (now(), jid))
            refresh_book_state(conn, bid)

    def _finish_book(self, job: dict, book: dict) -> None:
        with self.ctx.db.tx() as conn:
            counts = {r["status"]: r["n"] for r in rows(conn, "SELECT status, COUNT(*) n FROM segments WHERE book_id=? GROUP BY status", (book["id"],))}
            conn.execute("UPDATE jobs SET status='completed', current_segment_sid=NULL, finished_at=?, updated_at=?, result_json=? WHERE id=?",
                         (now(), now(), dumps(counts), job["id"]))
            msg = f"Generation finished: {counts.get('passed', 0)} passed QA, {counts.get('flagged', 0)} need review, {counts.get('approved', 0)} approved."
            self.ctx.job_log(conn, job["id"], book["id"], msg)
            self.ctx.event(conn, "book_done", f"'{book['title']}' {msg}", book["id"])
            refresh_book_state(conn, book["id"])
            snap = loads(book["settings_json"], {})
            if snap.get("export", {}).get("auto_assemble_drafts", True):
                exists = row(conn, "SELECT id FROM jobs WHERE book_id=? AND kind='assemble' AND status IN ('queued','running')", (book["id"],))
                if not exists:
                    conn.execute("INSERT INTO jobs(kind, book_id, payload_json, status, position, created_at, updated_at) VALUES ('assemble',?,?, 'queued', 0, ?, ?)",
                                 (book["id"], dumps({"auto": True}), now(), now()))

    # ------------------------------------------------------------------- loop
    def run_forever(self, idle_sleep: float = 2.0) -> None:
        if not self.acquire_lease():
            raise SystemExit("Another worker holds the GPU lease (see Overview). Not starting a second one.")
        info = self.recover()
        log.info("worker %s started (engine=%s) recovery=%s", self.owner, self.ctx.engine_mode, info)
        self._set(state="idle", job=None, sid=None, message="ready")
        self._hb_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._hb_thread.start()
        try:
            while not self.stop_event.is_set():
                worked = self.step()
                with self.ctx.db.session() as conn:
                    active = row(conn, "SELECT COUNT(*) n FROM jobs WHERE status IN ('queued','running')")["n"] > 0
                self.caffeinate.hold(active)
                if not worked:
                    self._set(state="idle", job=None, sid=None, message="waiting for work")
                    self.stop_event.wait(idle_sleep)
        finally:
            self.caffeinate.hold(False)
            self.release_lease()


def main(ctx) -> None:
    w = Worker(ctx)
    pidfile = ctx.run_dir / "worker.pid"
    pidfile.write_text(str(os.getpid()))

    def _stop(signum, _frame):
        log.info("signal %s: finishing current segment then stopping", signum)
        w.stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        w.run_forever()
    finally:
        if pidfile.exists() and pidfile.read_text().strip() == str(os.getpid()):
            pidfile.unlink(missing_ok=True)
