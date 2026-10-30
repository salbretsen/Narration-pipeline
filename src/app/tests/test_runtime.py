"""Processing-time estimates (studio/runtime.py) and the grouped, paginated Queue API. MOCK engine and synthetic rows only."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import make_text
from studio import runtime as R
from studio import services as S
from studio.db import row, rows

NOW = 1_800_000_000.0


def _make_book(client, title, chapters=1, queue=False):
    r = client.post("/api/imports", files={"file": (f"{title}.txt", make_text(chapters, title=title).encode(), "text/plain")})
    assert r.status_code == 200, r.text
    voice = next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
    b = client.post(f"/api/imports/{r.json()['token']}/confirm", json={"title": title, "voice_id": voice["id"], "queue": queue})
    assert b.status_code == 200, b.text
    return b.json()


def _seg_ids(ctx, book_id):
    with ctx.db.session() as conn:
        return [r["id"] for r in rows(conn, "SELECT id FROM segments WHERE book_id=? ORDER BY id", (book_id,))]


def _attempts(ctx, seg_ids, secs, model="m-x", t0=NOW - 100000, status="done", vv=None):
    """One finished attempt per segment id, each taking `secs` wall seconds."""
    with ctx.db.tx() as conn:
        for i, sid in enumerate(seg_ids):
            c = t0 + i * 10
            conn.execute("""INSERT INTO attempts(segment_id, round, attempt_no, status, engine, model_id, voice_version_id, fingerprint, text_spoken, created_at, finished_at)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (sid, 1, 1, status, "mock", model, vv, "fp", "x", c, c + secs))


def _job(ctx, book_id, status, position=1.0, started_at=None, finished_at=None, active_seconds=0.0):
    with ctx.db.tx() as conn:
        return conn.execute("""INSERT INTO jobs(kind, book_id, status, position, created_at, started_at, finished_at, updated_at, active_seconds)
                               VALUES ('book',?,?,?,?,?,?,?,?)""", (book_id, status, position, (started_at or NOW) - 5, started_at, finished_at,
                                                                  finished_at or started_at or NOW, active_seconds)).lastrowid


def _set_model(ctx, book_id, model):
    """Point the book at its own copy of its voice version with the given TTS model (books of one voice otherwise share a version)."""
    with ctx.db.tx() as conn:
        bk = row(conn, "SELECT voice_id, voice_version_id FROM books WHERE id=?", (book_id,))
        vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (bk["voice_version_id"],))
        nxt = row(conn, "SELECT MAX(version) m FROM voice_versions WHERE voice_id=?", (bk["voice_id"],))["m"] + 1
        new = conn.execute("INSERT INTO voice_versions(voice_id, version, model_id, speaker, language, instruct, created_at) VALUES (?,?,?,?,?,?,?)",
                           (bk["voice_id"], nxt, model, vv["speaker"], vv["language"], vv["instruct"], NOW)).lastrowid
        conn.execute("UPDATE books SET voice_version_id=? WHERE id=?", (new, book_id))


def _rt(ctx, book_id, now=NOW):
    with ctx.db.session() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        return R.book_runtime(conn, b, R.runtime_context(conn, now))


def _pending(ctx, book_id):
    with ctx.db.session() as conn:
        return row(conn, "SELECT COUNT(*) n FROM segments WHERE book_id=? AND status IN ('pending','synthesizing')", (book_id,))["n"]


def _mark_done(ctx, book_id, n):
    """Mark the first n segments of a book rendered (passed)."""
    ids = _seg_ids(ctx, book_id)[:n]
    with ctx.db.tx() as conn:
        for sid in ids:
            conn.execute("UPDATE segments SET status='passed' WHERE id=?", (sid,))
    return ids


def _history(client, ctx):
    """A book whose 30 segments carry 30 finished attempts of 4 s each (the global rate), tagged m-x. Returns that book's id."""
    h = _make_book(client, "History", chapters=6)
    ids = _seg_ids(ctx, h["id"])
    assert len(ids) >= 30
    _attempts(ctx, ids[:30], 4.0, model="m-x")
    _mark_done(ctx, h["id"], len(ids))
    return h["id"]


# ----------------------------------------------------------------------------------------------------------- rates


def test_no_rate_data_says_estimate_after_n_more_segments(client, ctx):
    b = _make_book(client, "Fresh")
    _job(ctx, b["id"], "queued")
    rt = _rt(ctx, b["id"])
    assert rt["state"] == "queued" and rt["total_est_s"] is None and rt["starts_at"] is None and rt["finish_at"] is None
    assert rt["basis"] == "estimate after 20 more segments" and rt["estimated"] is True
    ids = _seg_ids(ctx, b["id"])
    _attempts(ctx, ids[:5], 3.0)
    assert _rt(ctx, b["id"])["basis"] == "estimate after 15 more segments"
    assert set(rt) == {"state", "processing_s", "remaining_s", "finish_at", "starts_at", "total_est_s", "estimated", "basis"}


def test_idle_book_with_pending_segments(client, ctx):
    h = _history(client, ctx)
    b = _make_book(client, "Idle")
    _set_model(ctx, b["id"], "m-x")
    rt = _rt(ctx, b["id"])
    assert rt["state"] == "idle" and rt["starts_at"] is None and rt["finish_at"] is None and rt["remaining_s"] is None
    assert rt["total_est_s"] == _pending(ctx, b["id"]) * 4.0
    assert rt["basis"].startswith("typical pace on m-x")
    done = _rt(ctx, h)  # fully rendered, but no completed job: nothing to estimate and nothing to report
    assert done["state"] == "idle" and done["total_est_s"] is None and done["processing_s"] is None


def test_per_model_rate_beats_the_global_median(client, ctx):
    _history(client, ctx)  # global: 30 segments at 4 s on m-x
    fast = _make_book(client, "Fast", chapters=6)
    ids = _seg_ids(ctx, fast["id"])
    _set_model(ctx, fast["id"], "m-fast")
    _attempts(ctx, ids[:25], 1.0, model="m-fast", t0=NOW - 50000)  # newer than the history
    other = _make_book(client, "Other")  # m-other has no data: falls back to the global median
    _set_model(ctx, other["id"], "m-other")
    plain = _make_book(client, "Plain")
    _set_model(ctx, plain["id"], "m-x")
    for bk in (fast, other, plain):
        _job(ctx, bk["id"], "queued", position=float(bk["id"]))
    rf, ro, rp = (_rt(ctx, b["id"]) for b in (fast, other, plain))
    assert rf["total_est_s"] == _pending(ctx, fast["id"]) * 1.0 and "m-fast" in rf["basis"]
    assert ro["total_est_s"] == _pending(ctx, other["id"]) * 4.0 and "all voices" in ro["basis"]
    assert rp["total_est_s"] == _pending(ctx, plain["id"]) * 4.0 and "m-x" in rp["basis"]


def test_fewer_than_twenty_model_samples_fall_back_to_global(client, ctx):
    _history(client, ctx)
    b = _make_book(client, "Few", chapters=2)
    _set_model(ctx, b["id"], "m-few")
    _attempts(ctx, _seg_ids(ctx, b["id"])[:5], 9.0, model="m-few", t0=NOW - 10)
    _job(ctx, b["id"], "queued")
    rt = _rt(ctx, b["id"])
    assert rt["total_est_s"] == _pending(ctx, b["id"]) * 4.0 and "all voices" in rt["basis"]


# ----------------------------------------------------------------------------------------------------------- running


def test_running_uses_observed_rate_after_ten_segments(client, ctx):
    _history(client, ctx)
    b = _make_book(client, "Running", chapters=3)
    _set_model(ctx, b["id"], "m-x")
    total = len(_seg_ids(ctx, b["id"]))
    done = _mark_done(ctx, b["id"], 12)
    _attempts(ctx, done, 20.0, t0=NOW - 900)  # finished after the job started (NOW-1000)
    _job(ctx, b["id"], "running", started_at=NOW - 1000, active_seconds=300.0)  # 300 s over 12 segments = 25 s each
    rt = _rt(ctx, b["id"])
    assert rt["state"] == "running" and rt["estimated"] is True and rt["starts_at"] is None
    assert rt["remaining_s"] == (total - 12) * 25.0 and rt["finish_at"] == NOW + rt["remaining_s"]
    assert "this job's own pace" in rt["basis"] and "12 segments" in rt["basis"]


def test_running_with_few_done_uses_the_model_median(client, ctx):
    _history(client, ctx)
    b = _make_book(client, "Starting", chapters=3)
    _set_model(ctx, b["id"], "m-x")
    done = _mark_done(ctx, b["id"], 4)
    _attempts(ctx, done, 20.0, t0=NOW - 90)
    _job(ctx, b["id"], "running", started_at=NOW - 100, active_seconds=80.0)
    rt = _rt(ctx, b["id"])
    assert rt["remaining_s"] == _pending(ctx, b["id"]) * 4.0 and "typical pace on m-x" in rt["basis"]


def test_running_with_no_data_has_no_remaining_time(client, ctx):
    b = _make_book(client, "Blind")
    _job(ctx, b["id"], "running", started_at=NOW - 10, active_seconds=5.0)
    rt = _rt(ctx, b["id"])
    assert rt["state"] == "running" and rt["remaining_s"] is None and rt["finish_at"] is None and rt["basis"].startswith("estimate after")


# ----------------------------------------------------------------------------------------------------------- queue order


def test_queued_start_times_accumulate_over_the_queue_and_paused_does_not_advance(client, ctx):
    _history(client, ctx)
    books = [_make_book(client, f"Q{i}", chapters=2) for i in range(4)]
    for b in books:
        _set_model(ctx, b["id"], "m-x")
    run, q1, q2, paused = books
    _mark_done(ctx, run["id"], 2)
    _job(ctx, run["id"], "running", position=1, started_at=NOW - 50, active_seconds=10.0)
    _job(ctx, paused["id"], "paused", position=2)
    _job(ctx, q1["id"], "queued", position=3)
    _job(ctx, q2["id"], "queued", position=4)
    r_run, r_q1, r_q2, r_p = (_rt(ctx, b["id"]) for b in (run, q1, q2, paused))
    left = _pending(ctx, run["id"]) * 4.0
    assert r_run["remaining_s"] == left
    assert r_q1["starts_at"] == NOW + left and r_q1["total_est_s"] == _pending(ctx, q1["id"]) * 4.0
    assert r_q1["finish_at"] == r_q1["starts_at"] + r_q1["total_est_s"]
    assert r_q2["starts_at"] == r_q1["finish_at"]  # cumulative: the paused book did not delay it
    assert r_p["state"] == "queued" and "paused" in r_p["basis"] and r_p["starts_at"] is None and r_p["finish_at"] is None
    assert r_p["total_est_s"] == _pending(ctx, paused["id"]) * 4.0
    with ctx.db.session() as conn:
        assert R.runtime_context(conn, NOW)["queue_order"] == [run["id"], q1["id"], q2["id"], paused["id"]]
        assert R.runtime_context(conn, NOW)["running_remaining"] == {run["id"]: left}


def test_unknown_work_ahead_leaves_start_time_unknown(client, ctx):
    a, b = _make_book(client, "A"), _make_book(client, "B")
    _job(ctx, a["id"], "running", position=1, started_at=NOW - 5)
    _job(ctx, b["id"], "queued", position=2)
    assert _rt(ctx, b["id"])["starts_at"] is None


# ----------------------------------------------------------------------------------------------------------- finished


def test_finished_is_the_sum_of_active_seconds_over_all_runs(client, ctx):
    b = _make_book(client, "Done", chapters=1)
    _mark_done(ctx, b["id"], len(_seg_ids(ctx, b["id"])))
    _job(ctx, b["id"], "completed", started_at=NOW - 9000, finished_at=NOW - 8000, active_seconds=900.0)   # full pass
    _job(ctx, b["id"], "completed", started_at=NOW - 500, finished_at=NOW - 400, active_seconds=40.0)      # touch-up
    _job(ctx, b["id"], "completed", started_at=NOW - 300, finished_at=NOW - 200, active_seconds=12.5)      # touch-up
    rt = _rt(ctx, b["id"])
    assert rt["state"] == "finished" and rt["estimated"] is False and rt["processing_s"] == 952.5
    assert rt["remaining_s"] is None and rt["starts_at"] is None and rt["finish_at"] is None and rt["total_est_s"] is None
    assert "3 runs" in rt["basis"]


def test_legacy_jobs_without_active_seconds_fall_back_to_wall_time(client, ctx):
    b = _make_book(client, "Old")
    _mark_done(ctx, b["id"], len(_seg_ids(ctx, b["id"])))
    _job(ctx, b["id"], "completed", started_at=NOW - 2000, finished_at=NOW - 1500, active_seconds=0.0)
    _job(ctx, b["id"], "completed", started_at=NOW - 100, finished_at=NOW - 50, active_seconds=20.0)
    rt = _rt(ctx, b["id"])
    assert rt["processing_s"] == 520.0 and rt["estimated"] is False and "older run was timed" in rt["basis"]


def test_book_with_pending_segments_is_never_finished(client, ctx):
    b = _make_book(client, "Partial")
    _job(ctx, b["id"], "completed", started_at=NOW - 100, finished_at=NOW - 50, active_seconds=20.0)
    assert _rt(ctx, b["id"])["state"] == "idle"


# ----------------------------------------------------------------------------------------------------------- director


def test_director_time_is_added_per_undirected_segment(client, ctx):
    _history(client, ctx)
    b = _make_book(client, "Directed", chapters=2)
    _set_model(ctx, b["id"], "m-x")
    _job(ctx, b["id"], "queued")
    plain = _rt(ctx, b["id"])["total_est_s"]
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO analysis_stages(book_id, stage, status, stats_json) VALUES (?, 'director', 'running', ?)",
                     (b["id"], '{"directed": 10, "seconds": 50.0}'))  # 5 s of Director per segment
        conn.execute("UPDATE books SET narrative_json=? WHERE id=?", ('{"director_enabled": true}', b["id"]))
    pending = _pending(ctx, b["id"])
    rt = _rt(ctx, b["id"])
    assert rt["total_est_s"] == plain + pending * 5.0 and "Director" in rt["basis"]
    with ctx.db.tx() as conn:  # Director off for this book: no addition
        conn.execute("UPDATE books SET narrative_json=? WHERE id=?", ('{"director_enabled": false}', b["id"]))
    assert _rt(ctx, b["id"])["total_est_s"] == plain


# ----------------------------------------------------------------------------------------------------------- cost


def test_query_count_does_not_grow_with_the_number_of_books(client, ctx):
    def count() -> int:
        n = [0]
        with ctx.db.session() as conn:
            conn.set_trace_callback(lambda _sql: n.__setitem__(0, n[0] + 1))
            rc = R.runtime_context(conn, NOW)
            for b in rows(conn, "SELECT * FROM books"):
                R.book_runtime(conn, b, rc)
        return n[0]

    first = [_make_book(client, f"B{i}") for i in range(2)]
    for b in first:
        _job(ctx, b["id"], "queued", position=float(b["id"]))
    few = count()
    more = [_make_book(client, f"C{i}") for i in range(10)]
    for b in more:
        _job(ctx, b["id"], "queued", position=float(b["id"]))
    assert count() == few


# ----------------------------------------------------------------------------------------------------------- services wiring


def test_books_and_queue_carry_the_same_runtime(client, ctx):
    _history(client, ctx)
    b = _make_book(client, "Wired", chapters=2)
    _set_model(ctx, b["id"], "m-x")
    _job(ctx, b["id"], "queued")
    summary = next(x for x in S.list_books(ctx) if x["id"] == b["id"])
    q = next(j for j in client.get("/api/queue").json()["jobs"] if j["book_id"] == b["id"])
    assert summary["runtime"]["state"] == "queued" and q["runtime"]["state"] == "queued"
    assert abs(summary["runtime"]["total_est_s"] - q["runtime"]["total_est_s"]) < 1e-6
    assert q["eta_s"] == q["runtime"]["total_est_s"] and abs(q["eta_finish_s"] - (q["runtime"]["finish_at"] - S.now())) < 5
    assert client.get(f"/api/books/{b['id']}").json()["runtime"]["state"] == "queued"
    assert any(x["id"] == b["id"] and x["runtime"] for x in client.get("/api/overview").json()["recent"])


# ----------------------------------------------------------------------------------------------------------- queue grouping / pagination


def _finished_job(ctx, book_id, finished_at, active=10.0, status="completed"):
    return _job(ctx, book_id, status, started_at=finished_at - 100, finished_at=finished_at, active_seconds=active)


def test_one_finished_row_per_book_with_earlier_runs(client, ctx):
    b = _make_book(client, "Touchy", chapters=1)
    ids = _seg_ids(ctx, b["id"])
    _mark_done(ctx, b["id"], len(ids))
    t = S.now() - 5000
    full = _finished_job(ctx, b["id"], t, active=600.0)
    _attempts(ctx, ids, 5.0, t0=t - 90)  # rendered every segment during the first run
    runs = [full]
    for k in range(3):  # three touch-ups, each re-rendered one segment
        ft = t + 1000 * (k + 1)
        jid = _finished_job(ctx, b["id"], ft, active=7.0 + k)
        _attempts(ctx, [ids[k]], 3.0, t0=ft - 50)
        runs.append(jid)
    q = client.get("/api/queue").json()
    assert q["finished_total"] == 1 and len(q["finished"]) == 1
    f = q["finished"][0]
    assert f["id"] == runs[-1] and f["book_id"] == b["id"]
    assert [r["id"] for r in f["earlier_runs"]] == [runs[2], runs[1], runs[0]]  # newest first
    assert [r["kind_hint"] for r in f["earlier_runs"]] == ["touch-up", "touch-up", "full pass"]
    assert f["earlier_runs"][0]["segments_rendered"] == 1 and f["earlier_runs"][2]["segments_rendered"] == len(ids)
    assert f["earlier_runs"][0]["active_seconds"] == 8.0 and f["latest_run"]["kind_hint"] == "touch-up"
    assert f["runtime"]["state"] == "finished" and f["runtime"]["processing_s"] == 600.0 + 7.0 + 8.0 + 9.0
    assert f["eta_s"] is None and f["eta_finish_s"] is None
    assert [j["id"] for j in q["jobs"] if j["status"] == "completed"] == [runs[-1]]  # old consumers see a single entry too


def test_finished_pagination_is_stable_and_counts_books(client, ctx):
    t = S.now() - 100000
    ids = []
    for i in range(25):
        b = _make_book(client, f"Fin {i:02d}")
        _finished_job(ctx, b["id"], t + i * 60)
        ids.append(b["id"])
    pages = []
    for off in (0, 10, 20):
        q = client.get(f"/api/queue?finished_offset={off}&finished_limit=10").json()
        assert q["finished_total"] == 25 and q["finished_offset"] == off
        pages.append([j["book_id"] for j in q["finished"]])
    assert [len(p) for p in pages] == [10, 10, 5]
    assert sum(pages, []) == list(reversed(ids))  # newest finished first, no repeats across pages
    assert client.get("/api/queue").json()["finished_limit"] == 10
    assert len(client.get("/api/queue?finished_limit=50").json()["finished"]) == 25
    assert client.get("/api/queue?finished_limit=51").status_code == 422


def test_window_is_thirty_days_by_finished_at(client, ctx):
    old, recent = _make_book(client, "Old"), _make_book(client, "Recent")
    _finished_job(ctx, old["id"], S.now() - 31 * 86400)
    _finished_job(ctx, recent["id"], S.now() - 29 * 86400)
    with ctx.db.tx() as conn:
        conn.execute("UPDATE jobs SET updated_at=?", (S.now(),))  # touching updated_at must not resurrect the old run
    q = client.get("/api/queue").json()
    assert [j["book_id"] for j in q["finished"]] == [recent["id"]] and q["finished_total"] == 1


def test_active_jobs_stay_unpaginated_and_keep_old_keys(client, ctx):
    _history(client, ctx)
    books = [_make_book(client, f"Act {i}") for i in range(12)]
    for b in books:
        client.post("/api/queue", json={"book_id": b["id"]})
    q = client.get("/api/queue").json()
    assert len(q["active"]) == 12 and [j["status"] for j in q["jobs"]] == ["queued"] * 12
    assert {"jobs", "utility", "throughput", "finished", "finished_total", "utility_total"} <= set(q)
    assert q["throughput"]["samples"] >= 20 and all("runtime" in j for j in q["jobs"])


def test_utility_pagination(client, ctx):
    b = _make_book(client, "Util")
    with ctx.db.tx() as conn:
        for i in range(23):
            conn.execute("INSERT INTO jobs(kind, book_id, status, created_at, finished_at, updated_at) VALUES ('assemble',?,'completed',?,?,?)",
                         (b["id"], NOW + i, NOW + i, NOW + i))
    p1 = client.get("/api/queue").json()
    assert p1["utility_total"] == 23 and len(p1["utility"]) == 10 and p1["utility"][0]["title"].startswith("Util")
    p3 = client.get("/api/queue?utility_offset=20&utility_limit=10").json()
    assert len(p3["utility"]) == 3
    ids = [u["id"] for u in p1["utility"]] + [u["id"] for u in client.get("/api/queue?utility_offset=10").json()["utility"]] + [u["id"] for u in p3["utility"]]
    assert ids == sorted(ids, reverse=True) and len(set(ids)) == 23


def test_touch_up_regenerate_shows_one_finished_entry(client, ctx, make_worker):
    b = _make_book(client, "Regen", chapters=1, queue=True)
    w = make_worker()
    w.run_until_idle()
    assert client.get(f"/api/books/{b['id']}").json()["pending"] == 0
    sid = _seg_ids(ctx, b["id"])[1]
    for _ in range(2):  # two touch-ups, each creating (and finishing) another book job
        assert client.post(f"/api/segments/{sid}/regenerate", json={}).status_code == 200
        w.run_until_idle()
    with ctx.db.session() as conn:
        assert row(conn, "SELECT COUNT(*) n FROM jobs WHERE book_id=? AND kind='book' AND status='completed'", (b["id"],))["n"] == 3
    q = client.get("/api/queue").json()
    assert q["finished_total"] == 1 and len(q["finished"]) == 1
    f = q["finished"][0]
    assert len(f["earlier_runs"]) == 2 and f["runtime"]["state"] == "finished" and f["runtime"]["estimated"] is False
    assert f["runtime"]["processing_s"] is not None and f["eta_s"] is None
    assert [j["status"] for j in q["jobs"]] == ["completed"]


# ----------------------------------------------------------------------------------------------------------- node formatting tests

JS_TEST = Path(__file__).parent / "js" / "runtime_fmt.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_runtime_formatting_node_suite():
    r = subprocess.run(["node", str(JS_TEST)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
