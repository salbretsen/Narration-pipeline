"""Per-segment attempt lifecycle: synthesize -> transcribe -> QA -> score -> retry/flag.

Budget rules (crash-safe):
  * ``attempts_used`` is incremented and committed *before* inference starts, so an attempt
    interrupted by a crash, sleep or kill still counts against the budget;
  * once the budget is exhausted the best take is selected, the segment is flagged for a
    human and the book moves on - flagged segments are never retried automatically.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import statistics
import time
from pathlib import Path

from . import audio as A
from .align import evaluate
from .config import STORAGE_CATEGORIES
from .db import row, rows
from .engines import EngineUnavailable, SynthParams, VoiceSpec, max_tokens_for
from .narrative import store as N_store
from .util import atomic_write_text, dumps, loads, now, short_hash

log = logging.getLogger("studio.pipeline")


class StorageUnavailable(RuntimeError):
    """A book's storage location is missing (e.g. external drive unplugged)."""


# ------------------------------------------------------------------- book helpers


def book_dirs(book: dict) -> dict[str, Path]:
    data = loads(book["storage_json"], {})
    return {k: Path(v) for k, v in data.items() if k in STORAGE_CATEGORIES}


def check_storage(book: dict, categories=("segments", "alternates")) -> None:
    """Raise StorageUnavailable when a book's storage root cannot be used. Never creates a
    missing /Volumes/<disk> mount point (that would write onto the internal disk)."""
    for cat, d in book_dirs(book).items():
        if cat not in categories:
            continue
        parts = d.parts
        if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes":
            vol = Path("/Volumes") / parts[2]
            if not os.path.ismount(vol):
                raise StorageUnavailable(f"Volume {vol} is not mounted (needed for {cat})")
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageUnavailable(f"Cannot create {d}: {exc}") from exc
        if not os.access(d, os.W_OK):
            raise StorageUnavailable(f"{d} is not writable")


def voice_spec(conn, book: dict) -> VoiceSpec:
    vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (book["voice_version_id"],))
    v = row(conn, "SELECT * FROM voices WHERE id=?", (book["voice_id"],))
    if not vv or not v:
        raise ValueError("book has no narrator voice assigned")
    return VoiceSpec.from_rows(v, vv)


def spoken_text(seg: dict) -> str:
    return (seg.get("override_text") or seg.get("narration_text") or seg.get("original_text") or "").strip()


def take_paths(book: dict, seg: dict, chapter_idx: int, filename: str) -> tuple[Path, Path]:
    dirs = book_dirs(book)
    rel = Path(f"c{chapter_idx:03d}") / seg["sid"] / filename
    return dirs["segments"] / rel, dirs["alternates"] / rel


def resolve_take(book: dict, attempt: dict) -> Path | None:
    """Locate a take file, tolerating a crash between a file move and its DB update."""
    p = Path(attempt["wav_path"]) if attempt.get("wav_path") else None
    if p and p.exists():
        return p
    if not p:
        return None
    dirs = book_dirs(book)
    for a, b in (("segments", "alternates"), ("alternates", "segments")):
        try:
            rel = p.relative_to(dirs[a])
        except (ValueError, KeyError):
            continue
        alt = dirs[b] / rel
        if alt.exists():
            return alt
    return None


def _move(src: Path, dst: Path) -> None:
    if src == dst:
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(src, dst)
    except OSError:
        shutil.move(str(src), str(dst))
    side = Path(str(src) + ".json")
    if side.exists():
        try:
            os.replace(side, Path(str(dst) + ".json"))
        except OSError:
            shutil.move(str(side), str(dst) + ".json")


def place_takes(conn, book: dict, segment_id: int) -> None:
    """Keep the selected take in the segments directory and every other take in alternates."""
    seg = row(conn, "SELECT * FROM segments WHERE id=?", (segment_id,))
    if not seg:
        return
    dirs = book_dirs(book)
    for att in rows(conn, "SELECT * FROM attempts WHERE segment_id=? AND wav_path IS NOT NULL", (segment_id,)):
        cur = resolve_take(book, att)
        if cur is None:
            continue
        target_root = dirs["segments"] if att["id"] == seg["selected_attempt_id"] else dirs["alternates"]
        rel = None
        for root in (dirs["segments"], dirs["alternates"]):
            try:
                rel = cur.relative_to(root)
                break
            except ValueError:
                continue
        if rel is None:
            continue
        dst = target_root / rel
        if dst != cur:
            try:
                _move(cur, dst)
            except OSError as exc:
                log.warning("could not move take %s: %s", cur, exc)
                continue
        if str(dst) != att["wav_path"]:
            conn.execute("UPDATE attempts SET wav_path=? WHERE id=?", (str(dst), att["id"]))


def calibrated_wpm(conn, book_id: int, default: float) -> float:
    recent = rows(
        conn,
        """SELECT a.duration_s, a.text_spoken, a.pace_used FROM attempts a JOIN segments s ON s.id = a.segment_id
           WHERE s.book_id=? AND a.passed=1 AND a.duration_s > 1 ORDER BY a.id DESC LIMIT 40""",
        (book_id,),
    )
    if len(recent) < 10:
        return default
    # Stored durations are already time-stretched by that take's pace (faster = shorter). Undo it so the rate is the
    # model's natural one: process_segment multiplies by the new take's pace again when it sets the expected duration.
    rates = [len(r["text_spoken"].split()) * 60.0 / (r["duration_s"] * float(r["pace_used"] or 1.0)) for r in recent if r["duration_s"]]
    rates = [r for r in rates if 60 <= r <= 300]
    return statistics.median(rates) if len(rates) >= 10 else default


def attempt_params(snapshot: dict, sid: str, rnd: int, attempt_no: int, text: str, book_instructions: str) -> SynthParams:
    models = snapshot.get("models", {})
    seed = int(short_hash(f"{sid}:{rnd}:{attempt_no}", 8), 16)
    temp = models.get("temperature")
    if attempt_no > 1:
        retries = models.get("retry_temperatures") or []
        if retries:
            temp = retries[min(attempt_no - 2, len(retries) - 1)]
    return SynthParams(
        seed=seed,
        temperature=temp,
        top_p=models.get("top_p"),
        repetition_penalty=models.get("repetition_penalty"),
        max_tokens=max_tokens_for(text, snapshot.get("qa", {}).get("words_per_minute", 155.0)),
        instruct=book_instructions or None,
    )


def reusable_take(conn, book: dict, seg: dict, gen_key: str) -> dict | None:
    """An earlier passing take of this very segment made with identical text, voice, delivery and model
    parameters (e.g. after a voice change is reverted). A user-requested regeneration never reuses a take."""
    last = row(conn, "SELECT action, actor FROM review_actions WHERE segment_id=? ORDER BY id DESC LIMIT 1", (seg["id"],))
    if last and last["action"] == "regenerate" and last["actor"] != "system":
        return None
    for a in rows(conn, """SELECT * FROM attempts WHERE segment_id=? AND status='done' AND passed=1 AND gen_key=? AND wav_path IS NOT NULL
                          AND fingerprint=? ORDER BY id DESC""", (seg["id"], gen_key, book["synth_fingerprint"])):
        if resolve_take(book, a) is not None:
            return a
    return None


def verify_this(qa: dict, ns, seg: dict) -> bool:
    """Whether Whisper checks this segment: the book's ASR switch, then the narrative verification mode."""
    if not qa.get("enable_asr", True) or ns.verify_mode == "off":
        return False
    if ns.verify_mode == "sample":
        return int(short_hash(f"{seg['sid']}:{seg['round']}", 6), 16) % 1000 < ns.verify_sample_rate * 1000
    return True


def score_attempt(wer: float | None, issues: list[dict]) -> float:
    s = (wer or 0.0) * 100.0
    for i in issues:
        s += {"error": 25.0, "warning": 2.0}.get(i.get("severity"), 0.0)
    return round(s, 3)


def summarize_issues(issues: list[dict], limit: int = 3) -> str:
    errs = [i["message"] for i in issues if i.get("severity") == "error"]
    if not errs:
        errs = [i["message"] for i in issues if i.get("severity") == "warning"]
    text = "; ".join(errs[:limit])
    if len(errs) > limit:
        text += f" (+{len(errs) - limit} more)"
    return text


# ------------------------------------------------------------------ main entry


def process_segment(ctx, tts, asr, segment_id: int, job_id: int | None = None, on_attempt=None) -> str:
    """Run attempts for one segment until it passes QA or the budget is exhausted.
    Returns the final segment status."""
    db = ctx.db
    with db.session() as conn:
        seg = row(conn, "SELECT * FROM segments WHERE id=?", (segment_id,))
        if not seg:
            raise ValueError(f"segment {segment_id} not found")
        if seg["status"] not in ("pending", "synthesizing"):
            return seg["status"]
        book = row(conn, "SELECT * FROM books WHERE id=?", (seg["book_id"],))
        chapter = row(conn, "SELECT * FROM chapters WHERE id=?", (seg["chapter_id"],))
        snapshot = loads(book["settings_json"], {})
        qa = snapshot.get("qa", {})
        plan = N_store.plan_for_segment(conn, ctx, seg, book)
        voice = plan.voice
        ns = N_store.book_ns(ctx, book)
        wpm = calibrated_wpm(conn, book["id"], qa.get("words_per_minute", 155.0))
        reusable = reusable_take(conn, book, seg, plan.gen_key)
    check_storage(book)
    max_attempts = int(qa.get("max_attempts", 3))
    text = plan.text  # manuscript/override text after pronunciation-dictionary respellings
    pace = plan.pace
    if reusable is not None:
        with db.tx() as conn:
            conn.execute("UPDATE segments SET status='passed', selected_attempt_id=?, flag_reason=NULL, attempts_used=0, updated_at=? WHERE id=?",
                         (reusable["id"], now(), segment_id))
            place_takes(conn, book, segment_id)
            conn.execute("INSERT INTO review_actions(segment_id, action, detail_json, actor, created_at) VALUES (?,?,?,?,?)",
                         (segment_id, "cache_reuse", dumps({"attempt_id": reusable["id"], "gen_key": plan.gen_key}), "system", now()))
        return "passed"
    verify = verify_this(qa, ns, seg)

    while True:
        with db.tx() as conn:
            seg = row(conn, "SELECT * FROM segments WHERE id=?", (segment_id,))
            if seg["attempts_used"] >= max_attempts:
                break
            attempt_no = seg["attempts_used"] + 1
            params = attempt_params(snapshot, seg["sid"], seg["round"], attempt_no, text, plan.instruct or "")
            cur = conn.execute(
                """INSERT INTO attempts(segment_id, round, attempt_no, status, engine, model_id, voice_version_id,
                       fingerprint, seed, temperature, text_spoken, created_at, gen_key, instruct_used, delivery_applied, voice_id_used, asr_skipped,
                       pace_used, ref_id_used)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (segment_id, seg["round"], attempt_no, "in_progress", tts.name, voice.model_id, voice.version_id,
                 book["synth_fingerprint"], params.seed, params.temperature, text, now(), plan.gen_key, plan.instruct,
                 1 if plan.delivery_applied else 0, plan.voice_id, 0 if verify else 1,
                 round(pace, 4) if abs(pace - 1.0) > 1e-6 else None, plan.ref_id),
            )
            attempt_id = cur.lastrowid
            conn.execute("UPDATE segments SET status='synthesizing', attempts_used=?, updated_at=? WHERE id=?",
                         (attempt_no, now(), segment_id))
            if job_id:
                conn.execute("UPDATE jobs SET current_segment_sid=?, updated_at=? WHERE id=?", (seg["sid"], now(), job_id))
        if on_attempt:
            on_attempt(seg, attempt_no)

        try:
            res = tts.synthesize(voice, text, params, sid=seg["sid"])
        except EngineUnavailable as exc:
            with db.tx() as conn:
                conn.execute("UPDATE attempts SET status='aborted', error=?, finished_at=? WHERE id=?", (str(exc), now(), attempt_id))
                conn.execute("UPDATE segments SET status='pending', attempts_used=MAX(0, attempts_used-1), updated_at=? WHERE id=?", (now(), segment_id))
            raise
        except Exception as exc:  # a failed generation consumes an attempt
            log.exception("synthesis failed for %s", seg["sid"])
            with db.tx() as conn:
                conn.execute("UPDATE attempts SET status='error', error=?, score=?, passed=0, finished_at=? WHERE id=?",
                             (f"{type(exc).__name__}: {exc}", 1e6, now(), attempt_id))
            continue

        audio_arr = A.to_mono_float(res.audio)
        sr = res.sample_rate
        if abs(pace - 1.0) > 1e-6:  # per-voice pace: pitch-preserving time stretch (the model has no pace control)
            audio_arr = A.time_stretch(audio_arr, sr, pace)
        seg_path, _alt = take_paths(book, seg, chapter["idx"], f"r{seg['round']}a{attempt_no}.wav")
        seg_path.parent.mkdir(parents=True, exist_ok=True)
        A.write_wav(seg_path, audio_arr, sr)
        versions = tts.versions() if hasattr(tts, "versions") else {}
        provenance = {
            "sid": seg["sid"], "attempt": attempt_no, "round": seg["round"], "engine": res.engine,
            "model_id": res.model_id, "voice_version_id": voice.version_id, "seed": params.seed,
            "temperature": params.temperature, "text": text, "sample_rate": sr, "versions": versions,
            "meta": res.meta, "created_at": now(),
        }
        atomic_write_text(str(seg_path) + ".json", json.dumps(provenance, ensure_ascii=False, indent=1))

        acoustic = A.analyze(audio_arr, sr, A.expected_duration(text, wpm * pace), qa)
        issues = list(acoustic["issues"])
        asr_out, ev = None, None
        if verify:
            try:
                asr_out = asr.transcribe(seg_path)
            except EngineUnavailable as exc:
                # QA must never be skipped silently: give the attempt back and fail the job.
                seg_path.unlink(missing_ok=True)
                Path(str(seg_path) + ".json").unlink(missing_ok=True)
                with db.tx() as conn:
                    conn.execute("UPDATE attempts SET status='aborted', error=?, finished_at=? WHERE id=?",
                                 (f"ASR unavailable: {exc}", now(), attempt_id))
                    conn.execute("UPDATE segments SET status='pending', attempts_used=MAX(0, attempts_used-1), updated_at=? WHERE id=?", (now(), segment_id))
                raise
            ev = evaluate(text, asr_out["text"], asr_out.get("words"), acoustic["metrics"].get("duration_s"),
                          qa.get("max_dropped_words", 2), qa.get("max_inserted_words", 2))
            issues.extend(ev["issues"])
            # A percentage is meaningless on very short segments (one word in a 5-word heading is
            # 20%): below 10 words require at least two real word errors before flagging on WER.
            errors = ev["counts"]["sub"] + ev["counts"]["del"] + ev["counts"]["ins"]
            if ev["wer"] > qa.get("wer_threshold", 0.12) and (ev["ref_tokens"] >= 10 or errors >= 2):
                issues.append({"severity": "error", "kind": "wer", "message": f"Word error rate {ev['wer'] * 100:.1f}% exceeds {qa.get('wer_threshold', 0.12) * 100:.0f}%", "at_s": None, "source": "asr"})
        else:
            why = "ASR verification disabled for this book" if not qa.get("enable_asr", True) else "Whisper verification skipped by the verification setting (sampling/off)"
            issues.append({"severity": "info", "kind": "asr_disabled", "message": f"{why} - listen manually.", "at_s": None, "source": "settings"})
        wer = ev["wer"] if ev else None
        passed = not any(i["severity"] == "error" for i in issues)
        score = score_attempt(wer, issues)
        asr_json = {"words": (asr_out or {}).get("words", []), "engine": (asr_out or {}).get("engine"), "model": (asr_out or {}).get("model"),
                    "diff": (ev or {}).get("diff", []), "counts": (ev or {}).get("counts"), "wer_raw": (ev or {}).get("wer_raw")}
        with db.tx() as conn:
            conn.execute(
                """UPDATE attempts SET status='done', wav_path=?, duration_s=?, sample_rate=?, asr_text=?, asr_json=?, wer=?,
                       issues_json=?, acoustics_json=?, score=?, passed=?, rtf=?, peak_mem_gb=?, synth_seconds=?,
                       versions_json=?, finished_at=? WHERE id=?""",
                (str(seg_path), acoustic["metrics"].get("duration_s"), sr, (asr_out or {}).get("text"), dumps(asr_json), wer,
                 dumps(issues), dumps(acoustic["metrics"]), score, 1 if passed else 0, res.rtf, res.peak_mem_gb, res.seconds,
                 dumps({**versions, "model_revision": res.meta.get("model_revision"), "asr_model": (asr_out or {}).get("model")}), now(), attempt_id),
            )
            if passed:
                conn.execute("UPDATE segments SET status='passed', selected_attempt_id=?, flag_reason=NULL, updated_at=? WHERE id=?",
                             (attempt_id, now(), segment_id))
                place_takes(conn, book, segment_id)
                if job_id:
                    conn.execute("UPDATE jobs SET consecutive_errors=0 WHERE id=?", (job_id,))
                return "passed"

    # budget exhausted -> keep the best take, flag for a human, move on
    with db.tx() as conn:
        seg = row(conn, "SELECT * FROM segments WHERE id=?", (segment_id,))
        best = row(conn, """SELECT * FROM attempts WHERE segment_id=? AND round=? AND status='done' AND wav_path IS NOT NULL
                            ORDER BY score ASC, id ASC LIMIT 1""", (segment_id, seg["round"]))
        if best:
            reason = summarize_issues(loads(best["issues_json"], [])) or "Automatic QA did not pass"
            conn.execute("UPDATE segments SET status='flagged', selected_attempt_id=?, flag_reason=?, updated_at=? WHERE id=?",
                         (best["id"], f"{max_attempts} attempts failed QA; best take kept. {reason}", now(), segment_id))
            place_takes(conn, book, segment_id)
        else:
            last_err = row(conn, "SELECT error FROM attempts WHERE segment_id=? AND round=? ORDER BY id DESC LIMIT 1", (segment_id, seg["round"]))
            conn.execute("UPDATE segments SET status='flagged', selected_attempt_id=NULL, flag_reason=?, updated_at=? WHERE id=?",
                         (f"All {max_attempts} attempts failed to produce audio: {(last_err or {}).get('error') or 'unknown error'}", now(), segment_id))
            if job_id:
                conn.execute("UPDATE jobs SET consecutive_errors=consecutive_errors+1 WHERE id=?", (job_id,))
        ctx.event(conn, "flag", f"{seg['sid']} flagged for human review", seg["book_id"], "warning")
    return "flagged"


def recover_interrupted(conn) -> dict:
    """Crash/sleep recovery. In-progress attempts become 'aborted' (they still count against
    the budget); segments stuck in 'synthesizing' return to 'pending', or are flagged when
    their budget is already spent."""
    t = now()
    aborted = conn.execute("UPDATE attempts SET status='aborted', error='interrupted (process stopped)', finished_at=? WHERE status='in_progress'", (t,)).rowcount
    stuck = rows(conn, "SELECT s.*, b.settings_json FROM segments s JOIN books b ON b.id=s.book_id WHERE s.status='synthesizing'")
    requeued = flagged = 0
    for s in stuck:
        max_attempts = int(loads(s["settings_json"], {}).get("qa", {}).get("max_attempts", 3))
        if s["attempts_used"] >= max_attempts:
            best = row(conn, "SELECT id, issues_json FROM attempts WHERE segment_id=? AND round=? AND status='done' ORDER BY score ASC LIMIT 1", (s["id"], s["round"]))
            conn.execute("UPDATE segments SET status='flagged', selected_attempt_id=?, flag_reason=?, updated_at=? WHERE id=?",
                         (best["id"] if best else None, "Interrupted repeatedly; attempt budget exhausted. Review manually.", t, s["id"]))
            flagged += 1
        else:
            conn.execute("UPDATE segments SET status='pending', updated_at=? WHERE id=?", (t, s["id"]))
            requeued += 1
    return {"aborted_attempts": aborted, "requeued_segments": requeued, "flagged_segments": flagged}


def wait_ms(t0: float) -> int:
    return int((time.time() - t0) * 1000)
