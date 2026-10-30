"""v0.8 live-update foundation: a stable SSE digest, schema v6 (indexes + jobs.active_seconds), active-time accounting in the worker,
the cover cache key, and the node unit tests for the shared UI helpers. MOCK engine only."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from conftest import make_text
from studio import db as DB
from studio import services as S
from studio.db import row, rows


def _import(client, queue=True, title="Live Book"):
    r = client.post("/api/imports", files={"file": ("A.txt", make_text(2, title=title).encode(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    voice = next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
    b = client.post(f"/api/imports/{r.json()['token']}/confirm", json={"title": title, "voice_id": voice["id"], "queue": queue})
    assert b.status_code == 200, b.text
    return b.json()


# ------------------------------------------------------------------------------ digest


def _heartbeat(ctx, at: float, state="idle", message="Idle"):
    with ctx.db.tx() as conn:
        conn.execute("UPDATE worker_state SET owner='w1', pid=4242, state=?, message=?, heartbeat_at=? WHERE id=1", (state, message, at))


def test_digest_is_stable_while_the_heartbeat_ages(ctx, monkeypatch):
    t = [time.time()]
    monkeypatch.setattr(S, "now", lambda: t[0])
    _heartbeat(ctx, t[0] - 0.5)
    d1 = S.state_digest(ctx)
    assert d1["worker"]["online"] is True and "heartbeat_age_s" not in d1["worker"] and "heartbeat_at" not in d1["worker"]
    for dt in (1.0, 2.0, 7.3):  # the same heartbeat, seconds later: nothing real changed
        t[0] += dt
        assert json.dumps(S.state_digest(ctx), sort_keys=True) == json.dumps(d1, sort_keys=True)
    # a fresh beat (different timestamp, same state) is also not a change
    _heartbeat(ctx, t[0])
    assert S.state_digest(ctx) == d1
    # going stale IS a change: the worker is no longer online
    t[0] += 120
    d2 = S.state_digest(ctx)
    assert d2["worker"]["online"] is False and d2 != d1


def test_digest_changes_when_a_segment_or_job_changes(ctx, client, monkeypatch):
    _heartbeat(ctx, time.time())
    b = _import(client, queue=True)
    d0 = S.state_digest(ctx)
    time.sleep(0.01)
    with ctx.db.tx() as conn:
        conn.execute("UPDATE segments SET updated_at = updated_at + 5 WHERE book_id=?", (b["id"],))
    d1 = S.state_digest(ctx)
    assert d1 != d0 and d1["segments"] != d0["segments"]
    with ctx.db.tx() as conn:
        conn.execute("UPDATE jobs SET updated_at = updated_at + 5 WHERE book_id=?", (b["id"],))
    d2 = S.state_digest(ctx)
    assert d2["jobs"] != d1["jobs"]


def test_worker_endpoint_still_reports_the_exact_age(client, ctx):
    _heartbeat(ctx, time.time() - 3)
    w = client.get("/api/worker").json()
    assert w["online"] is True and 2.5 <= w["heartbeat_age_s"] <= 10 and isinstance(w["heartbeat_at"], float)


def test_sse_event_payload_has_no_clock_values(client, ctx):
    _heartbeat(ctx, time.time())
    r = client.get("/api/events?once=1")
    assert r.status_code == 200
    data = next(line[6:] for line in r.text.splitlines() if line.startswith("data: "))
    w = json.loads(data)["worker"]
    assert "heartbeat_age_s" not in w and "heartbeat_at" not in w and w["online"] is True


# ----------------------------------------------------------------------------- schema v6


def _cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _indexes(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()}


def test_migration_v5_to_v6_adds_indexes_and_active_seconds(tmp_path):
    d = DB.Database(tmp_path / "t.db")
    with d.session() as c:
        for version, sql in DB.MIGRATIONS[:5]:
            for st in DB._split_sql(sql):
                c.execute(st)
            c.execute(f"PRAGMA user_version = {version}")
        assert "active_seconds" not in _cols(c, "jobs") and "ix_jobs_book" not in _indexes(c)
        c.execute("INSERT INTO books(uid,title,settings_json,synth_fingerprint,storage_json,created_at,updated_at) VALUES ('b','T','{}','f','{}',1,1)")
        c.execute("INSERT INTO jobs(kind,book_id,payload_json,status,position,created_at,updated_at) VALUES ('book',1,'{}','queued',1,1,1)")
    assert DB.MIGRATIONS[5][0] == 6 and DB.LATEST_VERSION >= 6
    assert d.migrate(tmp_path / "bk")[0] == 5
    with d.session() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == DB.LATEST_VERSION
        assert {"ix_segments_updated", "ix_jobs_updated", "ix_jobs_book", "ux_one_active_book_job"} <= _indexes(c)
        assert "active_seconds" in _cols(c, "jobs")
        assert c.execute("SELECT active_seconds FROM jobs WHERE id=1").fetchone()[0] == 0  # existing rows get the default
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        # the partial unique index still guards "one active book job per book"
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO jobs(kind,book_id,payload_json,status,position,created_at,updated_at) VALUES ('book',1,'{}','queued',2,1,1)")
    assert d.migrate() == (DB.LATEST_VERSION, DB.LATEST_VERSION)  # idempotent


def test_fresh_database_has_the_v6_schema(ctx):
    with ctx.db.session() as c:
        assert {"ix_segments_updated", "ix_jobs_updated", "ix_jobs_book"} <= _indexes(c)
        assert "active_seconds" in _cols(c, "jobs")
        plan = " ".join(str(tuple(r)) for r in c.execute("EXPLAIN QUERY PLAN SELECT MAX(updated_at) FROM segments"))
        assert "ix_segments_updated" in plan


# ------------------------------------------------------------------------ active_seconds


def test_active_seconds_accumulates_per_step_with_a_fake_clock(client, ctx, make_worker):
    _import(client, queue=True)
    w = make_worker()
    ticks = [0.0]

    def clock():  # every call advances 2 s, and a step reads it exactly twice (start, end)
        ticks[0] += 2.0
        return ticks[0]

    w.clock = clock
    steps = w.run_until_idle()
    assert steps > 1
    with ctx.db.session() as conn:
        total = conn.execute("SELECT SUM(active_seconds) FROM jobs").fetchone()[0]
        bookjob = row(conn, "SELECT * FROM jobs WHERE kind='book'")
    assert total == pytest.approx(2.0 * steps)
    assert bookjob["status"] == "completed" and bookjob["active_seconds"] >= 2.0 * 2  # several segments, each a step


def test_active_seconds_is_real_wall_time_and_idle_waits_add_nothing(client, ctx, make_worker):
    b = _import(client, queue=True)
    w = make_worker()
    t0 = time.monotonic()
    w.step()
    time.sleep(0.3)  # an idle gap between steps: must not be counted
    w.step()
    wall = time.monotonic() - t0
    with ctx.db.session() as conn:
        j = row(conn, "SELECT * FROM jobs WHERE book_id=? AND kind='book'", (b["id"],))
    assert 0 < j["active_seconds"] <= wall - 0.25
    # a job that has not run (queued behind another) has none
    b2 = _import(client, queue=True, title="Second")
    with ctx.db.session() as conn:
        assert row(conn, "SELECT active_seconds FROM jobs WHERE book_id=?", (b2["id"],))["active_seconds"] == 0


def test_active_seconds_is_recorded_on_the_failure_path_too(client, ctx, make_worker, monkeypatch):
    b = _import(client, queue=True)
    w = make_worker()

    def boom(job):
        time.sleep(0.05)
        raise RuntimeError("engine fell over")

    monkeypatch.setattr(w, "_run_book_step", boom)
    assert w.step() is True
    with ctx.db.session() as conn:
        j = row(conn, "SELECT * FROM jobs WHERE book_id=? AND kind='book'", (b["id"],))
    assert j["status"] == "failed" and j["active_seconds"] >= 0.05


# ------------------------------------------------------------------------------ cover


def test_cover_version_is_the_cover_file_mtime(client, ctx):
    b = _import(client, queue=False)
    assert client.get(f"/api/books/{b['id']}").json()["cover_version"] == 0
    png = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
    r = client.post(f"/api/books/{b['id']}/cover", files={"file": ("c.png", png, "image/png")})
    assert r.status_code == 200, r.text
    d = client.get(f"/api/books/{b['id']}").json()
    assert d["has_cover"] is True and d["cover_version"] > 1_000_000_000
    assert client.get(f"/api/books/{b['id']}").json()["cover_version"] == d["cover_version"]  # stable until the file changes


# ------------------------------------------------------------------- node UI helpers

JS_TEST = Path(__file__).resolve().parent / "js" / "ui_helpers.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_ui_helpers_node_suite():
    r = subprocess.run(["node", str(JS_TEST)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "0 failed" in r.stdout
