"""Processing-time estimates and actuals for books and queue jobs (one place, so Overview, Library, Book and Queue agree).

Shape returned by :func:`book_runtime` (stable; the web views render it as-is)::

    {"state": "running" | "queued" | "finished" | "idle",
     "processing_s": float | None,   # finished: actual processing time (sum of jobs.active_seconds over all the book's jobs)
     "remaining_s": float | None,    # running: estimated time left
     "finish_at": float | None,      # running / queued: estimated epoch finish
     "starts_at": float | None,      # queued: estimated epoch start (None when paused or when something ahead has no estimate)
     "total_est_s": float | None,    # queued / idle: estimated processing time for the whole pending part
     "estimated": bool,              # False only for actuals
     "basis": str}                   # one plain sentence on where the number comes from (shown as a tooltip)

The rate is wall seconds per segment: this job's own observed pace once >= 10 segments are done, else the median for the book's TTS
model over the 200 most recent segments, else the global median, and none (``basis`` says "estimate after N more segments") with
fewer than 20 samples. Director (LLM) time is added per undirected pending segment when the Director is on for the book, except for
an observed job rate, which already includes it (``jobs.active_seconds`` counts the Director).

``runtime_context`` runs a fixed number of queries whatever the number of books; ``book_runtime`` then runs none.
"""
from __future__ import annotations

import statistics

from .db import row, rows
from .util import loads
from .util import now as _now

MIN_SAMPLES = 20      # fewer measured segments than this: no estimate
OBSERVED_MIN = 10     # a job's own pace is trusted after this many segments
WINDOW = 200          # most recent segments considered per rate
ACTIVE = ("queued", "running", "paused")
DONE = ("completed", "failed", "cancelled")


# ------------------------------------------------------------------------------------------------------------- rates


def global_rate(conn) -> dict:
    """Median wall seconds per segment over the 200 most recent segments, all books/voices/engines (the old ``_segment_seconds``)."""
    data = rows(conn, """SELECT a.segment_id, SUM(a.finished_at - a.created_at) t FROM attempts a
                         WHERE a.status IN ('done','error') AND a.finished_at IS NOT NULL GROUP BY a.segment_id ORDER BY MAX(a.id) DESC LIMIT ?""", (WINDOW,))
    vals = [d["t"] for d in data if d["t"] is not None and d["t"] >= 0]
    if not vals:
        return {"samples": 0, "median_s": None}
    return {"samples": len(vals), "median_s": round(statistics.median(vals), 2)}


def model_rates(conn) -> dict[str, dict]:
    """{model_id: {"samples", "median_s"}} from the most recent attempts (a bounded scan; one query)."""
    data = rows(conn, """SELECT model, SUM(finished_at - created_at) t FROM (
                           SELECT a.id, a.segment_id, COALESCE(a.model_id, vv.model_id) AS model, a.created_at, a.finished_at
                           FROM attempts a LEFT JOIN voice_versions vv ON vv.id = a.voice_version_id
                           WHERE a.status IN ('done','error') AND a.finished_at IS NOT NULL ORDER BY a.id DESC LIMIT ?)
                         WHERE model IS NOT NULL GROUP BY model, segment_id ORDER BY MAX(id) DESC""", (WINDOW * 20,))
    per: dict[str, list[float]] = {}
    for d in data:
        vals = per.setdefault(d["model"], [])
        if len(vals) < WINDOW and d["t"] is not None and d["t"] >= 0:
            vals.append(d["t"])
    return {m: {"samples": len(v), "median_s": round(statistics.median(v), 2)} for m, v in per.items() if v}


# ------------------------------------------------------------------------------------------------------------- context


def runtime_context(conn, now: float | None = None, settings=None) -> dict:
    """Everything the estimates need, gathered once per request with a fixed number of queries (independent of the number of books).

    Keys: now, rates {model_id: median_s}, rate_samples {model_id: n}, global (median_s), samples (n), queue_order [book_id],
    running_remaining {book_id: seconds}, active {book_id: runtime dict}, pending/total {book_id: n}, models {voice_version_id: model_id},
    director {book_id: {"s_per_segment", "undirected"}}, finished {book_id: {...}}.
    """
    t = _now() if now is None else now
    g = global_rate(conn)
    mr = model_rates(conn)
    ctx: dict = {"now": t, "global": g["median_s"], "samples": g["samples"], "rates": {m: v["median_s"] for m, v in mr.items()},
                 "rate_samples": {m: v["samples"] for m, v in mr.items()}, "queue_order": [], "running_remaining": {}, "active": {},
                 "settings_director": bool(getattr(getattr(settings, "narrative", None), "director_enabled", False))}
    pending: dict[int, int] = {}
    total: dict[int, int] = {}
    for r in rows(conn, "SELECT book_id, COUNT(*) n, SUM(CASE WHEN status IN ('pending','synthesizing') THEN 1 ELSE 0 END) p FROM segments GROUP BY book_id"):
        total[r["book_id"]], pending[r["book_id"]] = r["n"], r["p"] or 0
    ctx["pending"], ctx["total"] = pending, total
    ctx["models"] = {r["id"]: r["model_id"] for r in rows(conn, "SELECT id, model_id FROM voice_versions")}
    # Director seconds per directed segment, and how many pending segments still need directing (only when it has run anywhere)
    director: dict[int, dict] = {}
    stage = rows(conn, "SELECT book_id, stats_json FROM analysis_stages WHERE stage='director'")
    per_seg = {}
    for s in stage:
        st = loads(s["stats_json"], {}) or {}
        directed, secs = st.get("directed") or 0, st.get("seconds") or 0
        if directed > 0 and secs > 0:
            per_seg[s["book_id"]] = secs / directed
    if per_seg:
        for r in rows(conn, """SELECT s.book_id, COUNT(*) n FROM segments s WHERE s.status IN ('pending','synthesizing')
                               AND NOT EXISTS (SELECT 1 FROM directions d WHERE d.segment_id = s.id) GROUP BY s.book_id"""):
            if r["book_id"] in per_seg:
                director[r["book_id"]] = {"s_per_segment": per_seg[r["book_id"]], "undirected": r["n"]}
    ctx["director"] = director
    # finished books: actual processing time per book across all of its book jobs; the latest job decides "completed"
    fin: dict[int, dict] = {}
    for r in rows(conn, """SELECT book_id, COUNT(*) runs, MAX(id) last_id,
                                  SUM(CASE WHEN active_seconds > 0 THEN active_seconds
                                           WHEN started_at IS NOT NULL AND finished_at IS NOT NULL AND finished_at >= started_at THEN finished_at - started_at
                                           ELSE 0 END) secs,
                                  SUM(CASE WHEN active_seconds <= 0 AND started_at IS NOT NULL AND finished_at IS NOT NULL AND finished_at >= started_at
                                           THEN 1 ELSE 0 END) legacy
                           FROM jobs WHERE kind='book' AND book_id IS NOT NULL AND status IN ('completed','failed','cancelled') GROUP BY book_id"""):
        fin[r["book_id"]] = {"runs": r["runs"], "secs": float(r["secs"] or 0), "legacy": r["legacy"] or 0, "last_id": r["last_id"], "completed": 0}
    for r in rows(conn, "SELECT book_id, COUNT(*) n FROM jobs WHERE kind='book' AND status='completed' GROUP BY book_id"):
        if r["book_id"] in fin:
            fin[r["book_id"]]["completed"] = r["n"]
    ctx["finished"] = fin
    # active jobs, in the order the worker will serve them: running, then queued by position, then paused (which do not advance)
    jobs = rows(conn, """SELECT j.id, j.book_id, j.status, j.position, j.started_at, j.active_seconds, b.voice_version_id, b.narrative_json
                         FROM jobs j JOIN books b ON b.id = j.book_id WHERE j.kind='book' AND j.status IN ('queued','running','paused')
                         ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, j.position, j.id""")
    done_by_job = {r["jid"]: r["n"] for r in rows(conn, """
        SELECT j.id jid, COUNT(DISTINCT a.segment_id) n FROM jobs j JOIN segments s ON s.book_id = j.book_id JOIN attempts a ON a.segment_id = s.id
        WHERE j.kind='book' AND j.status IN ('running','paused','queued') AND j.started_at IS NOT NULL AND a.status='done' AND a.finished_at >= j.started_at
        GROUP BY j.id""")} if jobs else {}
    offset: float | None = 0.0  # seconds of work ahead of the next queued job; None once something ahead has no estimate
    for j in jobs:
        bid = j["book_id"]
        ctx["queue_order"].append(bid)
        observed = None
        n_done = done_by_job.get(j["id"], 0)
        if j["status"] == "running" and n_done >= OBSERVED_MIN and (j["active_seconds"] or 0) > 0:
            observed = ((j["active_seconds"] or 0) / n_done, n_done)
        rate, basis, work = _work(ctx, j, pending.get(bid, 0), observed)
        rt = _blank("queued" if j["status"] != "running" else "running", basis)
        if j["status"] == "running":
            if work is not None:
                rt.update(remaining_s=work, finish_at=t + work, estimated=True)
                ctx["running_remaining"][bid] = work
            offset = None if (work is None or offset is None) else offset + work
        elif j["status"] == "queued":
            if work is not None:
                rt["total_est_s"] = work
                if offset is not None:
                    rt.update(starts_at=t + offset, finish_at=t + offset + work)
            offset = None if (work is None or offset is None) else offset + work
        else:  # paused: waits for a manual resume, so it neither starts on its own nor delays what is behind it
            rt["basis"] = "paused, so it is not advancing; " + basis
            if work is not None:
                rt["total_est_s"] = work
        ctx["active"][bid] = rt
    return ctx


def _blank(state: str, basis: str = "") -> dict:
    return {"state": state, "processing_s": None, "remaining_s": None, "finish_at": None, "starts_at": None, "total_est_s": None, "estimated": True, "basis": basis}


def _pick_rate(ctx: dict, model: str | None) -> tuple[float | None, str]:
    """(seconds per segment, basis) from the model's median, else the global median, else (None, 'estimate after N more segments')."""
    if model and ctx["rate_samples"].get(model, 0) >= MIN_SAMPLES:
        r = ctx["rates"][model]
        return r, f"typical pace on {model}: {r:g} s per segment (median of the last {ctx['rate_samples'][model]} segments)"
    if ctx["samples"] >= MIN_SAMPLES and ctx["global"] is not None:
        return ctx["global"], f"typical pace across all voices: {ctx['global']:g} s per segment (median of the last {ctx['samples']} segments)"
    return None, f"estimate after {max(1, MIN_SAMPLES - ctx['samples'])} more segments"


def _director_enabled(ctx: dict, narrative_json) -> bool:
    ov = loads(narrative_json, {}) if isinstance(narrative_json, str) or narrative_json is None else (narrative_json or {})
    if isinstance(ov, dict) and isinstance(ov.get("director_enabled"), bool):
        return ov["director_enabled"]
    return ctx.get("settings_director", False)


def _work(ctx: dict, book: dict, pending: int, observed: tuple[float, int] | None = None) -> tuple[float | None, str, float | None]:
    """(rate, basis, seconds of work for ``pending`` segments) for one book. ``book`` needs voice_version_id and narrative_json."""
    if observed:
        rate, n = observed
        return rate, f"this job's own pace so far: {rate:.1f} s per segment over {n} segments (includes any Director time)", pending * rate
    model = ctx["models"].get(book.get("voice_version_id"))
    rate, basis = _pick_rate(ctx, model)
    if rate is None:
        return None, basis, None
    work = pending * rate
    d = ctx["director"].get(book.get("book_id", book.get("id")))
    if d and d["undirected"] and _director_enabled(ctx, book.get("narrative_json")):
        extra = d["undirected"] * d["s_per_segment"]
        work += extra
        basis += f"; plus {d['s_per_segment']:.1f} s of Director time for each of {d['undirected']} undirected segments"
    return rate, basis, work


def book_runtime(conn, book: dict, ctx_info: dict | None = None) -> dict:
    """The runtime dict for one book (see the module docstring). ``ctx_info`` from :func:`runtime_context`, built lazily when omitted."""
    ctx = ctx_info if ctx_info is not None else runtime_context(conn)
    bid = book["id"]
    active = ctx["active"].get(bid)
    if active is not None:
        return dict(active)
    pend = ctx["pending"].get(bid, 0)
    if pend == 0:
        f = ctx["finished"].get(bid)
        if f and f["completed"] and f["secs"] >= 0:
            return _finished(f)
        return {**_blank("idle", "nothing left to render"), "estimated": False}
    rate_work = _work(ctx, book, pend)
    rt = _blank("idle", rate_work[1])
    if rate_work[2] is not None:
        rt["total_est_s"] = rate_work[2]
    return rt


def _finished(f: dict) -> dict:
    basis = f"actual processing time across {f['runs']} run{'s' if f['runs'] != 1 else ''} (waiting and paused time excluded)"
    if f["legacy"]:
        basis += f"; {f['legacy']} older run{'s were' if f['legacy'] != 1 else ' was'} timed from start to finish"
    return {**_blank("finished", basis), "processing_s": f["secs"], "estimated": False}


def job_runtime(conn, job: dict, ctx_info: dict) -> dict:
    """Runtime of one queue job. Active jobs get their book's live estimate; a finished job gets its actual processing time (the book's
    whole processing time when this is its latest completed run and the book is fully rendered)."""
    ctx = ctx_info
    if job["status"] in ACTIVE:
        return book_runtime(conn, {"id": job["book_id"]}, ctx)
    secs = float(job.get("active_seconds") or 0)
    legacy = False
    if secs <= 0 and job.get("started_at") and job.get("finished_at") and job["finished_at"] >= job["started_at"]:
        secs, legacy = float(job["finished_at"] - job["started_at"]), True
    f = ctx["finished"].get(job["book_id"])
    if job["status"] == "completed" and f and ctx["pending"].get(job["book_id"], 0) == 0:
        return _finished(f)
    verb = {"completed": "processing time of this run", "failed": "time spent before it failed", "cancelled": "time spent before it was cancelled"}[job["status"]]
    return {**_blank("finished", f"actual {verb}" + ("; timed from start to finish because it pre-dates exact timing" if legacy else "")),
            "processing_s": secs, "estimated": False}
