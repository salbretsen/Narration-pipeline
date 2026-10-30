"""Narrator versions (v0.7.0): one manuscript narrated by several narrators, each version kept intact; admin-only delete of a version or of the
whole book. MOCK engine only (synthetic tones)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_text
from studio import db as DB
from studio import diskmgmt as D
from studio.db import row, rows
from studio.web.app import create_app
from studio.web.auth import AuthStore, is_admin_only


def _voice(client, name):
    return next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == name)


def _import(client, title="Alpha", voice="Ryan", queue=False):
    r = client.post("/api/imports", files={"file": (f"{title}.txt", make_text(2, title=title).encode(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    r = client.post(f"/api/imports/{r.json()['token']}/confirm", json={"title": title, "voice_id": _voice(client, voice)["id"], "queue": queue})
    assert r.status_code == 200, r.text
    return r.json()


def _add(client, book_id, voice, **kw):
    return client.post(f"/api/books/{book_id}/versions", json={"voice_id": _voice(client, voice)["id"], **kw})


def _segs(ctx, book_id):
    with ctx.db.session() as conn:
        return rows(conn, "SELECT * FROM segments WHERE book_id=? ORDER BY id", (book_id,))


def _snapshot(ctx, book_id):
    with ctx.db.session() as conn:
        return {"segments": [(s["sid"], s["status"], s["selected_attempt_id"], s["attempts_used"], s["round"]) for s in rows(conn, "SELECT * FROM segments WHERE book_id=? ORDER BY id", (book_id,))],
                "attempts": rows(conn, "SELECT a.id, a.wav_path FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=? ORDER BY a.id", (book_id,)),
                "renders": rows(conn, "SELECT id, path FROM chapter_renders WHERE chapter_id IN (SELECT id FROM chapters WHERE book_id=?) ORDER BY id", (book_id,))}


# ---------------------------------------------------------------- schema


def test_schema_v4_makes_every_existing_book_its_own_work(client, ctx):
    assert DB.LATEST_VERSION >= 4
    b = _import(client)
    with ctx.db.tx() as conn:
        conn.execute("UPDATE books SET work_uid=NULL")                       # what a pre-0.7 row looks like right after ALTER TABLE
        for stmt in DB._split_sql(DB.SCHEMA_V4):
            if stmt.lstrip().upper().startswith("UPDATE"):
                conn.execute(stmt)
    with ctx.db.session() as conn:
        r = row(conn, "SELECT uid, work_uid FROM books WHERE id=?", (b["id"],))
    assert r["work_uid"] == r["uid"]
    assert b["work_uid"] == b["uid"] and b["version_count"] == 1 and b["display_title"] == "Alpha - Ryan"


# ---------------------------------------------------------------- adding a narrator keeps the earlier pass


def test_new_version_is_a_full_untouched_copy_and_leaves_the_first_pass_alone(client, ctx, make_worker):
    a = _import(client, queue=True)
    make_worker().run_until_idle()
    before = _snapshot(ctx, a["id"])
    assert any(s[1] in ("passed", "approved", "flagged") for s in before["segments"]) and before["attempts"]

    r = _add(client, a["id"], "Aiden", instructions="calm and slow")
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["id"] != a["id"] and v["uid"] != a["uid"] and v["work_uid"] == a["work_uid"] and v["title"] == "Alpha"
    assert v["narrator"] == "Aiden" and v["narrator_credit"] == "Aiden" and v["display_title"] == "Alpha - Aiden" and v["instructions"] == "calm and slow"
    assert v["version_count"] == 2 and v["synth_fingerprint"] != a["synth_fingerprint"]
    assert [x["narrator"] for x in v["versions"]] == ["Ryan", "Aiden"] and [x["current"] for x in v["versions"]] == [False, True]

    old_segs, new_segs = _segs(ctx, a["id"]), _segs(ctx, v["id"])
    assert len(new_segs) == len(old_segs) > 0
    assert [s["original_text"] for s in new_segs] == [s["original_text"] for s in old_segs]
    assert all(s["sid"].startswith(v["uid"]) for s in new_segs) and not {s["sid"] for s in new_segs} & {s["sid"] for s in old_segs}
    assert all(s["status"] == "pending" and s["selected_attempt_id"] is None and s["attempts_used"] == 0 and s["round"] == 1 for s in new_segs)
    with ctx.db.session() as conn:
        assert not rows(conn, "SELECT a.id FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=?", (v["id"],))
        assert not rows(conn, "SELECT id FROM chapter_renders WHERE chapter_id IN (SELECT id FROM chapters WHERE book_id=?)", (v["id"],))
        assert len(rows(conn, "SELECT id FROM chapters WHERE book_id=?", (v["id"],))) == len(rows(conn, "SELECT id FROM chapters WHERE book_id=?", (a["id"],)))
        m_old, m_new = (row(conn, "SELECT * FROM manuscripts WHERE book_id=?", (i,)) for i in (a["id"], v["id"]))
    assert m_new["sha256"] == m_old["sha256"] and m_new["stored_path"] != m_old["stored_path"] and Path(m_new["stored_path"]).read_bytes() == Path(m_old["stored_path"]).read_bytes()
    assert _snapshot(ctx, a["id"]) == before                                  # the first narration is byte-for-byte what it was

    # the new version generates on its own; the first one is still untouched afterwards
    assert client.post("/api/queue", json={"book_id": v["id"]}).status_code == 200
    make_worker(take_over=True).run_until_idle()
    done = _segs(ctx, v["id"])
    assert all(s["status"] != "pending" for s in done)
    assert {a_["wav_path"] for a_ in _snapshot(ctx, a["id"])["attempts"]} == {a_["wav_path"] for a_ in before["attempts"]}
    assert _snapshot(ctx, a["id"]) == before
    books = {b["id"]: b for b in client.get("/api/books").json()["books"]}
    assert books[a["id"]]["version_count"] == books[v["id"]]["version_count"] == 2


def test_version_copies_characters_attribution_and_director_guidance(client, ctx):
    from test_narrative import all_segments, chars, import_semantic

    a = import_semantic(client, director_enabled=False)
    v = _add(client, a["id"], "Aiden").json()
    assert set(chars(client, v["id"])) == set(chars(client, a["id"]))
    old, new = all_segments(ctx, a["id"]), all_segments(ctx, v["id"])
    assert [(s["src_start"], s["src_end"], s["span_kind"]) for s in new] == [(s["src_start"], s["src_end"], s["span_kind"]) for s in old]
    with ctx.db.session() as conn:
        assert row(conn, "SELECT COUNT(*) n FROM nar_spans WHERE book_id=?", (v["id"],))["n"] == row(conn, "SELECT COUNT(*) n FROM nar_spans WHERE book_id=?", (a["id"],))["n"] > 0
        assert row(conn, "SELECT COUNT(*) n FROM chapter_text WHERE book_id=?", (v["id"],))["n"] == row(conn, "SELECT COUNT(*) n FROM chapter_text WHERE book_id=?", (a["id"],))["n"] > 0
        # speakers point at the NEW book's characters, never the old one's
        new_chars = {r["id"] for r in rows(conn, "SELECT id FROM characters WHERE book_id=?", (v["id"],))}
        assert all(s["character_id"] in new_chars for s in new if s["character_id"])
        assert all(r["speaker_id"] in new_chars for r in rows(conn, "SELECT speaker_id FROM nar_spans WHERE book_id=? AND speaker_id IS NOT NULL", (v["id"],)))


def test_same_narrator_and_instructions_twice_is_refused_but_other_instructions_are_not(client, ctx):
    a = _import(client)
    assert _add(client, a["id"], "Ryan").status_code == 409                   # the book already has a Ryan version with these (empty) instructions
    assert _add(client, a["id"], "Aiden").status_code == 200
    assert _add(client, a["id"], "Aiden").status_code == 409
    assert _add(client, a["id"], "Aiden", instructions="whispered").status_code == 200
    assert client.get(f"/api/books/{a['id']}").json()["version_count"] == 3
    assert client.post(f"/api/books/{a['id']}/versions", json={"voice_id": 99999}).status_code == 422
    assert client.post("/api/books/99999/versions", json={"voice_id": _voice(client, "Ryan")["id"]}).status_code == 404


def test_cannot_add_a_narrator_while_the_book_is_rendering_and_failure_leaves_nothing_behind(client, ctx):
    a = _import(client, queue=True)
    with ctx.db.tx() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE book_id=?", (a["id"],))
    r = _add(client, a["id"], "Aiden")
    assert r.status_code == 409 and "rendering" in r.json()["error"]
    with ctx.db.session() as conn:
        assert len(rows(conn, "SELECT id FROM books")) == 1
    assert len(list(ctx.category_dir("segments").iterdir())) == 1


def test_queue_and_dashboard_titles_carry_the_narrator(client, ctx):
    a = _import(client, queue=True)
    v = _add(client, a["id"], "Aiden", queue=True).json()
    q = client.get("/api/queue").json()
    titles = {j["book_id"]: j["title"] for j in q["jobs"]}
    assert titles == {a["id"]: "Alpha - Ryan", v["id"]: "Alpha - Aiden"}


# ---------------------------------------------------------------- deleting


def test_delete_one_version_leaves_the_others_complete(client, ctx, make_worker):
    a = _import(client, queue=True)
    make_worker().run_until_idle()
    v = _add(client, a["id"], "Aiden").json()
    a_dirs = D.book_dirs(row_book(ctx, a["id"]))
    v_dirs = D.book_dirs(row_book(ctx, v["id"]))
    before = _snapshot(ctx, a["id"])
    r = client.post(f"/api/disk/books/{v['id']}/delete", json={"confirm": False})
    assert r.status_code == 409 and "Alpha - Aiden" in r.json()["error"] and "other 1 narrator version" in r.json()["error"]
    assert row_book(ctx, v["id"]) is not None                                # nothing happens without confirmation
    assert client.post(f"/api/disk/books/{v['id']}/delete", json={"confirm": True}).status_code == 200
    assert row_book(ctx, v["id"]) is None and not any(d.exists() for d in v_dirs.values())
    assert _snapshot(ctx, a["id"]) == before and all(d.exists() for d in a_dirs.values())
    assert all(Path(x["wav_path"]).is_file() for x in before["attempts"] if x["wav_path"])
    assert client.get(f"/api/books/{a['id']}").json()["version_count"] == 1


def test_remove_book_deletes_every_version_only_with_confirmation(client, ctx, make_worker):
    a = _import(client, queue=True)
    make_worker().run_until_idle()
    v = _add(client, a["id"], "Aiden").json()
    other = _import(client, title="Beta")                                     # an unrelated book must survive
    dirs = [D.book_dirs(row_book(ctx, i)) for i in (a["id"], v["id"])]
    r = client.post(f"/api/disk/books/{v['id']}/delete-all-versions", json={"confirm": False})
    assert r.status_code == 409 and "2 narrator version" in r.json()["error"] and len(r.json()["details"]["versions"]) == 2
    assert row_book(ctx, a["id"]) and row_book(ctx, v["id"])
    r = client.post(f"/api/disk/books/{v['id']}/delete-all-versions", json={"confirm": True})   # asking via ANY version removes the whole work
    assert r.status_code == 200 and r.json()["versions_deleted"] == 2
    assert row_book(ctx, a["id"]) is None and row_book(ctx, v["id"]) is None and not any(d.exists() for ds in dirs for d in ds.values())
    assert row_book(ctx, other["id"]) is not None
    with ctx.db.session() as conn:
        assert not rows(conn, "SELECT id FROM segments WHERE book_id IN (?,?)", (a["id"], v["id"]))


def test_remove_book_is_refused_while_any_version_has_an_active_job(client, ctx):
    a = _import(client)
    v = _add(client, a["id"], "Aiden", queue=True).json()
    r = client.post(f"/api/disk/books/{a['id']}/delete-all-versions", json={"confirm": True})
    assert r.status_code == 409 and "job" in r.json()["error"]
    assert row_book(ctx, a["id"]) and row_book(ctx, v["id"])                  # all-or-nothing


def test_deleting_a_book_with_pronunciation_entries_works(client, ctx):
    a = _import(client)
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO pronunciations(book_id, written, spoken, created_at, updated_at) VALUES (?, 'Xyzzy', 'zizzy', 1, 1)", (a["id"],))
    v = _add(client, a["id"], "Aiden").json()
    with ctx.db.session() as conn:
        assert row(conn, "SELECT COUNT(*) n FROM pronunciations WHERE book_id=?", (v["id"],))["n"] == 1      # copied to the new version
    assert client.post(f"/api/disk/books/{a['id']}/delete-all-versions", json={"confirm": True}).status_code == 200
    with ctx.db.session() as conn:
        assert row(conn, "SELECT COUNT(*) n FROM pronunciations")["n"] == 0


def test_deleting_is_admin_only_but_adding_a_narrator_is_not(ctx):
    for m, p in (("POST", "/api/disk/books/1/delete"), ("POST", "/api/disk/books/1/delete-all-versions")):
        assert is_admin_only(m, p)
    assert not is_admin_only("POST", "/api/books/1/versions")
    store = AuthStore(ctx.config_dir / "auth.json")
    store.set_user("boss", "correct-horse-battery", "admin")
    store.set_user("clerk", "correct-horse-battery2", "operator")
    app = create_app(ctx, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:7861", headers={"X-Studio-Request": "1"}) as c:
        assert c.post("/api/auth/login", json={"username": "clerk", "password": "correct-horse-battery2"}).status_code == 200
        assert c.post("/api/disk/books/1/delete-all-versions", json={"confirm": True}).status_code == 403
        assert c.post("/api/disk/books/1/delete", json={"confirm": True}).status_code == 403
        assert c.post("/api/books/1/versions", json={"voice_id": 1}).status_code != 403


def row_book(ctx, book_id):
    with ctx.db.session() as conn:
        return row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
