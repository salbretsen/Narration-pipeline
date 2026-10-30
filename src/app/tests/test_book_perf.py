"""get_book performance and equivalence (P2-A): the query count must not grow with the number of segments, the batched readiness must
agree with the old per-segment implementation, and ?view=review is a light subset. MOCK data only (synthetic rows and tiny placeholder files)."""
from __future__ import annotations

import time

import pytest
from book_synth import QueryCounter, build, synth_segments

from studio import services as S
from studio.assembly import chapter_readiness, takes_digest
from studio.db import row, rows
from studio.pipeline import resolve_take
from studio.services import release_check_conn


# ---- the pre-batching implementation, kept verbatim as the reference ------------------------------------------------------------
def _legacy_chapter_takes(conn, book, chapter_id):
    segs = rows(conn, "SELECT * FROM segments WHERE chapter_id=? ORDER BY idx", (chapter_id,))
    out, problems = [], []
    for s in segs:
        if not s["selected_attempt_id"]:
            problems.append({"sid": s["sid"], "problem": "no audio" if s["status"] == "pending" else f"no usable take ({s['status']})"})
            continue
        att = row(conn, "SELECT * FROM attempts WHERE id=?", (s["selected_attempt_id"],))
        path = resolve_take(book, att) if att else None
        if not att or path is None:
            problems.append({"sid": s["sid"], "problem": "selected take file is missing"})
            continue
        if att["fingerprint"] != book["synth_fingerprint"]:
            problems.append({"sid": s["sid"], "problem": "take was rendered with different narrator/model settings"})
            continue
        out.append((s, att, path))
    return segs, out, problems


def _legacy_readiness(conn, book, ctx):
    from pathlib import Path

    out = []
    for ch in rows(conn, "SELECT * FROM chapters WHERE book_id=? ORDER BY idx", (book["id"],)):
        segs, takes, problems = _legacy_chapter_takes(conn, book, ch["id"])
        renders = {r["kind"]: r for r in rows(conn, "SELECT * FROM chapter_renders WHERE chapter_id=?", (ch["id"],))}
        digest = takes_digest(conn, book, ch["id"], ctx)
        current = bool(renders.get("wav") and renders.get("mp3") and renders["wav"]["takes_digest"] == digest
                       and Path(renders["wav"]["path"]).exists() and Path(renders["mp3"]["path"]).exists())
        out.append({"chapter": ch, "segments": len(segs), "ready_takes": len(takes), "problems": problems,
                    "renders": renders, "render_current": current, "digest": digest})
    return out


def _book_row(ctx, book_id):
    with ctx.db.session() as conn:
        return row(conn, "SELECT * FROM books WHERE id=?", (book_id,))


def _messy(ctx, book_id):
    """Add the awkward cases the batched code must classify like the old one: a retitled chapter, a take rendered with other settings,
    a selected attempt row that no longer exists, a take that only exists in the other folder (crash between a move and its DB update)."""
    with ctx.db.tx() as conn:
        chs = rows(conn, "SELECT id FROM chapters WHERE book_id=? ORDER BY idx", (book_id,))
        conn.execute("UPDATE chapters SET original_title=title, title='Renamed' WHERE id=?", (chs[1]["id"],))
        sel = rows(conn, "SELECT selected_attempt_id FROM segments WHERE book_id=? AND selected_attempt_id IS NOT NULL ORDER BY id", (book_id,))
        conn.execute("UPDATE attempts SET fingerprint='other' WHERE id=?", (sel[3]["selected_attempt_id"],))
        conn.execute("DELETE FROM attempts WHERE id=?", (sel[7]["selected_attempt_id"],))
        moved = row(conn, "SELECT id, wav_path FROM attempts WHERE id=?", (sel[11]["selected_attempt_id"],))
        from pathlib import Path

        src = Path(moved["wav_path"])
        alt = src.parent.parent / "alternates" / src.name
        alt.parent.mkdir(parents=True, exist_ok=True)
        if src.exists():
            src.replace(alt)


def test_readiness_matches_the_old_per_segment_implementation(ctx):
    bid = build(ctx, 120, 6)
    _messy(ctx, bid)
    b = _book_row(ctx, bid)
    with ctx.db.session() as conn:
        old = _legacy_readiness(conn, b, ctx)
        new = chapter_readiness(conn, b, ctx)
        counts = {r["status"]: r["n"] for r in rows(conn, "SELECT status, COUNT(*) n FROM segments WHERE book_id=? GROUP BY status", (bid,))}
        chk_old = release_check_conn(conn, b, ctx, ready=old)
        chk_new = release_check_conn(conn, b, ctx)
        chk_fast = release_check_conn(conn, b, ctx, ready=new, counts=counts)
    assert len(old) == len(new) == 6
    for o, n in zip(old, new):
        assert o["chapter"]["id"] == n["chapter"]["id"]
        for k in ("segments", "ready_takes", "problems", "render_current", "digest"):
            assert o[k] == n[k], k
        assert {k: (v["id"], v["takes_digest"], v["path"]) for k, v in o["renders"].items()} == {k: (v["id"], v["takes_digest"], v["path"]) for k, v in n["renders"].items()}
    assert sum(len(r["problems"]) for r in new) > 3            # the messy cases really produce problems
    assert any(p["problem"] == "take was rendered with different narrator/model settings" for r in new for p in r["problems"])
    assert any(p["problem"] == "selected take file is missing" for r in new for p in r["problems"])
    assert chk_old == chk_new == chk_fast
    assert any(r["render_current"] for r in new) and not all(r["render_current"] for r in new)


def test_pause_hints_digest_is_unchanged_by_batching(ctx):
    bid = build(ctx, 60, 3)
    with ctx.db.tx() as conn:   # the book opts in to direction pauses and one segment carries a hint
        conn.execute("UPDATE books SET narrative_json=? WHERE id=?", ('{"delivery": {"pauses_from_direction": true}}', bid))
        sid = row(conn, "SELECT id FROM segments WHERE book_id=? AND pause_kind='sentence' ORDER BY id LIMIT 1", (bid,))["id"]
        conn.execute("INSERT INTO directions(segment_id, book_id, source, raw_json, final_json, updated_at) VALUES (?,?,?,?,?,?)",
                     (sid, bid, "test", "{}", '{"pause": "long"}', 1.0))
    b = _book_row(ctx, bid)
    with ctx.db.session() as conn:
        old, new = _legacy_readiness(conn, b, ctx), chapter_readiness(conn, b, ctx)
    assert [r["digest"] for r in old] == [r["digest"] for r in new]


def test_get_book_query_count_does_not_grow_with_segments(ctx):
    counts = {}
    for n in (50, 400):
        bid = build(ctx, n, 10, title=f"Count{n}")
        S.get_book(ctx, bid)                       # warm
        with QueryCounter(ctx) as q:
            out = S.get_book(ctx, bid)
        counts[n] = q.n
        assert out["segments"] == n
    assert counts[50] == counts[400], counts
    assert counts[400] <= 40, counts


def test_get_book_is_fast_on_400_segments(ctx):
    bid = build(ctx, 400, 10)
    S.get_book(ctx, bid)
    t = time.perf_counter()
    S.get_book(ctx, bid)
    assert time.perf_counter() - t < 0.5   # measured ~12 ms; the old implementation needed ~760 queries


def test_get_book_payload_unchanged_keys_and_values(ctx):
    bid = build(ctx, 100, 5)
    out = S.get_book(ctx, bid)
    for k in ("chapters", "release_check", "exports", "logs", "utility_jobs", "narrative", "manuscript", "versions", "voice_detail", "cover_version",
              "produced", "segments", "flagged", "approved", "storage", "release"):
        assert k in out, k
    ch = out["chapters"][0]
    assert set(ch) == {"id", "index", "title", "words", "segments", "title_overridden", "original_title", "est_duration_s", "status_counts",
                       "missing_audio", "render_current", "renders"}
    assert sum(sum(c["status_counts"].values()) for c in out["chapters"]) == 100
    merged: dict = {}
    for c in out["chapters"]:
        for k, v in c["status_counts"].items():
            merged[k] = merged.get(k, 0) + v
    assert out["release_check"]["counts"] == merged
    assert [v["narrator"] for v in out["versions"]] == ["Ryan"] and out["versions"][0]["current"] is True


def test_review_view_is_a_light_subset_with_identical_keys(ctx, client):
    bid = build(ctx, 100, 5)
    full = client.get(f"/api/books/{bid}").json()
    light = client.get(f"/api/books/{bid}?view=review").json()
    for heavy in ("release_check", "exports", "logs", "utility_jobs", "narrative", "manuscript", "storage", "settings", "voice_detail", "versions"):
        assert heavy in full and heavy not in light, heavy
    must = ("id", "uid", "title", "author", "state", "work_uid", "narrator", "display_title", "version_count", "voice", "segments", "produced",
            "passed", "flagged", "approved", "pending", "progress", "est_duration_s", "audio_duration_s", "job", "current_chapter", "released")
    for k in must:
        assert k in light and light[k] == full[k], k
    assert len(light["chapters"]) == 5
    for lc, fc in zip(light["chapters"], full["chapters"]):
        assert set(lc) == {"id", "index", "title", "words", "segments", "title_overridden", "original_title", "est_duration_s", "status_counts"}
        for k in lc:
            assert lc[k] == fc[k], k
    assert client.get("/api/books/99999?view=review").status_code == 404


def test_review_view_query_count_is_flat(ctx):
    counts = []
    for n in (50, 400):
        bid = build(ctx, n, 10, title=f"Rev{n}")
        with QueryCounter(ctx) as q:
            S.get_book_review(ctx, bid)
        counts.append(q.n)
    assert counts[0] == counts[1] and counts[1] <= 15


def test_book_stats_scopes_the_audio_duration_join(ctx):
    a, b = build(ctx, 30, 3, title="A"), build(ctx, 30, 3, title="B")
    with ctx.db.session() as conn:
        both = S._book_stats(conn)
        only_a = S._book_stats(conn, [a])
        nobody = S._book_stats(conn, [])
        sql = []
        conn.set_trace_callback(sql.append)
        S._book_stats(conn, [a])
    assert set(both) >= {a, b} and set(only_a) == {a} and nobody == {}
    assert only_a[a] == both[a]
    durations = [s for s in sql if "SUM(a.duration_s)" in s]
    assert durations and all("IN (" in s for s in durations)


def test_narrative_status_can_reuse_the_callers_session(ctx):
    from studio.narrative import store as N

    bid = build(ctx, 30, 3)
    alone = N.narrative_status(ctx, bid)
    with ctx.db.session() as conn:
        shared = N.narrative_status(ctx, bid, conn)
    assert alone == shared and alone["segments"] == 30
    with pytest.raises(Exception):
        N.narrative_status(ctx, 424242)


def test_synth_helper_roundtrip(ctx):
    bid = build(ctx, 20, 2)
    again = synth_segments(ctx, bid, 40, 4)
    assert len(again["chapter_ids"]) == 4
    assert S.get_book(ctx, bid)["segments"] == 40
