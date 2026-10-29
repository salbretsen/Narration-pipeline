"""Disk management (C3): usage, purge/delete rules, backup and restore. MOCK engine only (synthetic tones)."""
from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_text
from studio import diskmgmt as D
from studio.db import row, rows
from studio.web.app import create_app
from studio.web.auth import AuthStore, is_admin_only


def _import(client, title, queue=False, marker=None):
    v = next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
    r = client.post("/api/imports", files={"file": (f"{title}.txt", make_text(2, marker=marker, title=title).encode(), "application/octet-stream")})
    assert r.status_code == 200, r.text
    prev = r.json()
    r = client.post(f"/api/imports/{prev['token']}/confirm", json={"title": title, "voice_id": v["id"], "queue": queue})
    assert r.status_code == 200, r.text
    return r.json()


def _generated(client, make_worker, title="Alpha", faults=None):
    b = _import(client, title, queue=True, marker="Zanzibar")
    make_worker(faults or {"text:Zanzibar": ["drop", "repeat", "drop"]}).run_until_idle()
    return b


def _tree(path: Path) -> dict[str, int]:
    return {p.relative_to(path).as_posix(): p.stat().st_size for p in path.rglob("*") if p.is_file()} if path.exists() else {}


def _ctx_book(ctx, book_id):
    with ctx.db.session() as conn:
        return row(conn, "SELECT * FROM books WHERE id=?", (book_id,))


# ---------------------------------------------------------------- usage


def test_usage_sizes_match_disk_and_report_free_space(client, ctx, make_worker):
    b = _generated(client, make_worker)
    u = client.get("/api/disk/usage").json()
    mine = next(x for x in u["books"] if x["id"] == b["id"])
    dirs = D.book_dirs(_ctx_book(ctx, b["id"]))
    for cat in ("manuscripts", "segments", "alternates", "reports", "masters", "exports"):
        assert mine["categories"][cat] == sum(_tree(dirs[cat]).values()), cat
    assert mine["categories"]["segments"] > 0 and mine["categories"]["alternates"] > 0
    assert mine["total_bytes"] == sum(mine["categories"].values())
    assert u["library_bytes"] == mine["total_bytes"]
    assert u["free"]["library"]["free_bytes"] > 0 and u["free"]["model_cache"] is not None
    assert mine["purgeable"]["takes"] > 0 and mine["purgeable"]["takes_bytes"] > 0
    assert "database" in u["other"] and "recordings" in u["other"]


# ---------------------------------------------------------------- purge


def test_purge_takes_keeps_selected_and_approved(client, ctx, make_worker):
    b = _generated(client, make_worker)
    with ctx.db.session() as conn:
        flagged = row(conn, "SELECT * FROM segments WHERE book_id=? AND status='flagged'", (b["id"],))
    assert flagged
    assert client.post(f"/api/segments/{flagged['id']}/approve", json={"note": "ok"}).status_code == 200
    with ctx.db.session() as conn:
        segs = rows(conn, "SELECT id, selected_attempt_id, status FROM segments WHERE book_id=?", (b["id"],))
        before = rows(conn, "SELECT a.id, a.wav_path FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=?", (b["id"],))
    selected = {s["selected_attempt_id"] for s in segs if s["selected_attempt_id"]}
    assert any(s["status"] == "approved" for s in segs) and len(before) > len(selected)
    # no confirm: refused with a preview, nothing changes
    r = client.post(f"/api/disk/books/{b['id']}/purge", json={"scope": "takes"})
    assert r.status_code == 409 and r.json()["details"]["items"] == len(before) - len(selected)
    with ctx.db.session() as conn:
        assert len(rows(conn, "SELECT id FROM attempts")) == len(before)
    r = client.post(f"/api/disk/books/{b['id']}/purge", json={"scope": "takes", "confirm": True})
    assert r.status_code == 200 and r.json()["items"] == len(before) - len(selected) and r.json()["freed_bytes"] > 0
    with ctx.db.session() as conn:
        left = rows(conn, "SELECT id, wav_path FROM attempts")
        audit = row(conn, "SELECT * FROM audit_log WHERE action='purge_takes'")
    assert {a["id"] for a in left} == selected and audit
    for a in left:
        assert Path(a["wav_path"]).exists()
    assert not _tree(D.book_dirs(_ctx_book(ctx, b["id"]))["alternates"])
    for s in segs:
        assert s["selected_attempt_id"] is None or any(a["id"] == s["selected_attempt_id"] for a in left)


def test_purge_ignores_database_paths_outside_the_book(client, ctx, make_worker, tmp_path):
    b = _generated(client, make_worker)
    victim = tmp_path / "precious.wav"
    victim.write_bytes(b"do not delete")
    with ctx.db.tx() as conn:
        a = row(conn, "SELECT a.id FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=? AND a.id NOT IN (SELECT selected_attempt_id FROM segments WHERE selected_attempt_id IS NOT NULL) LIMIT 1", (b["id"],))
        conn.execute("UPDATE attempts SET wav_path=? WHERE id=?", (str(victim), a["id"]))
        conn.execute("INSERT INTO exports(book_id, kind, label, path, created_at) VALUES (?, 'x', 'x', ?, 0)", (b["id"], str(victim)))
    assert client.post(f"/api/disk/books/{b['id']}/purge", json={"scope": "takes", "confirm": True}).status_code == 200
    assert client.post(f"/api/disk/books/{b['id']}/purge", json={"scope": "exports", "confirm": True}).status_code == 200
    assert victim.read_bytes() == b"do not delete"


def test_purge_exports_removes_files_and_rows(client, ctx, make_worker):
    b = _generated(client, make_worker)
    dirs = D.book_dirs(_ctx_book(ctx, b["id"]))
    f = dirs["exports"] / "book.m4b"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"x" * 1000)
    with ctx.db.tx() as conn:
        conn.execute("INSERT INTO exports(book_id, kind, label, path, size_bytes, created_at) VALUES (?, 'm4b', 'L', ?, 1000, 0)", (b["id"], str(f)))
    assert client.post(f"/api/disk/books/{b['id']}/purge", json={"scope": "exports"}).status_code == 409
    assert f.exists()
    r = client.post(f"/api/disk/books/{b['id']}/purge", json={"scope": "exports", "confirm": True})
    assert r.status_code == 200 and r.json()["freed_bytes"] == 1000 and not f.exists()
    with ctx.db.session() as conn:
        assert not rows(conn, "SELECT id FROM exports")
    assert client.post(f"/api/disk/books/{b['id']}/purge", json={"scope": "bogus", "confirm": True}).status_code == 422


# ---------------------------------------------------------------- refusal while queued/rendering


def test_refused_while_queued_or_running(client, ctx):
    b = _import(client, "Busy", queue=True)
    for path, body in ((f"/api/disk/books/{b['id']}/delete", {"confirm": True}), (f"/api/disk/books/{b['id']}/purge", {"scope": "takes", "confirm": True})):
        r = client.post(path, json=body)
        assert r.status_code == 409 and "queued" in r.json()["error"]
    with ctx.db.tx() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE book_id=?", (b["id"],))
    assert client.post(f"/api/disk/books/{b['id']}/delete", json={"confirm": True}).status_code == 409
    assert client.post(f"/api/disk/books/{b['id']}/backup").status_code == 409  # a running render is not backed up
    assert _ctx_book(ctx, b["id"]) is not None
    assert any(_tree(d) for d in D.book_dirs(_ctx_book(ctx, b["id"])).values())


# ---------------------------------------------------------------- delete


def test_delete_requires_confirm_and_leaves_other_books_alone(client, ctx, make_worker):
    a = _generated(client, make_worker, "Alpha")
    other = _import(client, "Bravo")
    dirs_a = D.book_dirs(_ctx_book(ctx, a["id"]))
    dirs_o = D.book_dirs(_ctx_book(ctx, other["id"]))
    other_before = {k: _tree(v) for k, v in dirs_o.items()}
    r = client.post(f"/api/disk/books/{a['id']}/delete", json={})
    assert r.status_code == 409 and r.json()["details"]["bytes"] > 0
    assert client.post(f"/api/disk/books/{a['id']}/delete", json={"confirm": "yes"}).status_code == 422
    assert _ctx_book(ctx, a["id"]) is not None and _tree(dirs_a["segments"])
    r = client.post(f"/api/disk/books/{a['id']}/delete", json={"confirm": True})
    assert r.status_code == 200 and r.json()["freed_bytes"] > 0 and not r.json()["errors"]
    assert _ctx_book(ctx, a["id"]) is None
    assert not any(d.exists() for d in dirs_a.values())
    with ctx.db.session() as conn:
        for t in ("segments", "chapters", "manuscripts", "nar_spans", "jobs", "chapter_text"):
            assert not rows(conn, f"SELECT 1 FROM {t} WHERE book_id=?", (a["id"],)), t
        assert not rows(conn, "SELECT 1 FROM attempts WHERE segment_id NOT IN (SELECT id FROM segments)")
        assert row(conn, "SELECT 1 FROM audit_log WHERE action='deleted' AND entity='book'")
    assert {k: _tree(v) for k, v in dirs_o.items()} == other_before and _ctx_book(ctx, other["id"])
    assert client.post(f"/api/disk/books/{a['id']}/delete", json={"confirm": True}).status_code == 404


def test_delete_refuses_tampered_storage_paths(client, ctx, tmp_path):
    b = _import(client, "Tamper")
    outside = tmp_path / "important"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    with ctx.db.tx() as conn:
        conn.execute("UPDATE books SET storage_json=? WHERE id=?", (json.dumps({"segments": str(outside)}), b["id"]))
    r = client.post(f"/api/disk/books/{b['id']}/delete", json={"confirm": True})
    assert r.status_code == 422 and (outside / "keep.txt").exists()


# ---------------------------------------------------------------- backup / restore


def test_backup_restore_round_trip(client, ctx, make_worker):
    b = _generated(client, make_worker)
    orig = _ctx_book(ctx, b["id"])
    orig_dirs = D.book_dirs(orig)
    orig_files = {k: _tree(v) for k, v in orig_dirs.items()}
    with ctx.db.session() as conn:
        n_seg = len(rows(conn, "SELECT id FROM segments WHERE book_id=?", (b["id"],)))
        n_att = len(rows(conn, "SELECT a.id FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=?", (b["id"],)))
        n_span = len(rows(conn, "SELECT id FROM nar_spans WHERE book_id=?", (b["id"],)))
        n_sel = len(rows(conn, "SELECT id FROM segments WHERE book_id=? AND selected_attempt_id IS NOT NULL", (b["id"],)))
    r = client.post(f"/api/disk/books/{b['id']}/backup")
    assert r.status_code == 200, r.text
    bk = r.json()
    zpath = Path(bk["path"])
    assert zpath.parent == ctx.backups_dir / "books" and zpath.exists()
    with zipfile.ZipFile(zpath) as zf:
        m = json.loads(zf.read("manifest.json"))
    assert m["format"] == D.BACKUP_FORMAT and m["book_uid"] == orig["uid"] and len(m["files"]) == sum(len(v) for v in orig_files.values())
    assert any(x["name"] == bk["name"] for x in client.get("/api/disk/backups").json()["books"])

    rr = client.post("/api/disk/restore", json={"name": bk["name"]})
    assert rr.status_code == 200, rr.text
    new = rr.json()
    assert new["book_id"] != b["id"] and new["uid"] != orig["uid"] and new["title"].endswith("(restored)")
    nb = _ctx_book(ctx, new["book_id"])
    ndirs = D.book_dirs(nb)
    for k, files in orig_files.items():
        assert _tree(ndirs[k]) == files, k
    with ctx.db.session() as conn:
        segs = rows(conn, "SELECT * FROM segments WHERE book_id=?", (new["book_id"],))
        atts = rows(conn, "SELECT a.* FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=?", (new["book_id"],))
        assert len(segs) == n_seg and len(atts) == n_att
        assert len(rows(conn, "SELECT id FROM nar_spans WHERE book_id=?", (new["book_id"],))) == n_span
        assert sum(1 for s in segs if s["selected_attempt_id"]) == n_sel
        assert all(s["sid"].startswith(nb["uid"]) for s in segs)
        attempt_ids = {a["id"] for a in atts}
        assert all(s["selected_attempt_id"] in attempt_ids for s in segs if s["selected_attempt_id"])
        for a in atts:
            if a["wav_path"]:
                assert Path(a["wav_path"]).exists() and nb["uid"] in a["wav_path"]
        man = row(conn, "SELECT * FROM manuscripts WHERE book_id=?", (new["book_id"],))
        assert Path(man["stored_path"]).exists() and nb["uid"] in man["stored_path"]
        assert row(conn, "SELECT 1 FROM audit_log WHERE action='restored'")
    assert nb["voice_id"] == orig["voice_id"]
    # the original is untouched, and the restored copy works after the original is deleted
    assert {k: _tree(v) for k, v in orig_dirs.items()} == orig_files
    assert client.post(f"/api/disk/books/{b['id']}/delete", json={"confirm": True}).status_code == 200
    assert client.get(f"/api/books/{new['book_id']}").status_code == 200
    assert client.get(f"/api/books/{new['book_id']}/segments").status_code == 200
    # restoring twice yields two independent books
    again = client.post("/api/disk/restore", json={"name": bk["name"]}).json()
    assert again["uid"] not in (new["uid"], orig["uid"])


def test_database_backup_is_a_valid_sqlite_copy(client, ctx):
    import sqlite3

    _import(client, "DbBook")
    r = client.post("/api/disk/db-backup")
    assert r.status_code == 200 and r.json()["integrity"] == "ok"
    p = ctx.backups_dir / "db" / r.json()["name"]
    con = sqlite3.connect(str(p))
    try:
        assert con.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 1
    finally:
        con.close()
    assert any(x["name"] == r.json()["name"] for x in client.get("/api/disk/backups").json()["databases"])


def test_delete_backup_needs_confirm(client, ctx):
    b = _import(client, "Tiny")
    bk = client.post(f"/api/disk/books/{b['id']}/backup").json()
    assert client.post("/api/disk/backups/delete", json={"name": bk["name"]}).status_code == 409
    assert Path(bk["path"]).exists()
    assert client.post("/api/disk/backups/delete", json={"name": bk["name"], "confirm": True}).status_code == 200
    assert not Path(bk["path"]).exists()


# ---------------------------------------------------------------- path traversal


@pytest.mark.parametrize("name", ["../evil.zip", "..\\evil.zip", "/etc/passwd.zip", "sub/dir.zip", "C:\\x.zip", "no-extension", "a..b/../c.zip", ".hidden.zip"])
def test_restore_rejects_bad_names(client, name):
    r = client.post("/api/disk/restore", json={"name": name})
    assert r.status_code in (404, 422), (name, r.status_code)
    r = client.post("/api/disk/backups/delete", json={"name": name, "confirm": True})
    assert r.status_code in (404, 422)


def test_restore_rejects_malicious_and_damaged_archives(client, ctx, tmp_path):
    b = _import(client, "Victim")
    good = Path(client.post(f"/api/disk/books/{b['id']}/backup").json()["path"])
    root = ctx.backups_dir / "books"
    lib_before = sorted(p.name for p in ctx.category_dir("segments").iterdir())

    def forged(name, mutate):
        out = root / name
        with zipfile.ZipFile(good) as zin, zipfile.ZipFile(out, "w") as zout:
            items = {i.filename: zin.read(i.filename) for i in zin.infolist()}
            items = mutate(items)
            for k, v in items.items():
                zout.writestr(k, v)
        return out

    evil = {"traverse.zip": lambda it: {**it, "media/segments/../../../escape.txt": b"x"},
            "abs.zip": lambda it: {**it, "/abs/escape.txt": b"x"},
            "cat.zip": lambda it: {**it, "media/bogus/x.wav": b"x"},
            "nomanifest.zip": lambda it: {k: v for k, v in it.items() if k != "manifest.json"}}
    for name, fn in evil.items():
        forged(name, fn)
        r = client.post("/api/disk/restore", json={"name": name})
        assert r.status_code == 422, (name, r.text)
    from studio.services import ServiceError

    for bad in ("media" + chr(92) + "segments" + chr(92) + "x.wav", "C:/x.wav", "media/segments/./x.wav", "media//x.wav"):
        with pytest.raises(ServiceError):
            D._clean_member(bad)
    # damaged media: declared checksum no longer matches
    def corrupt(it):
        m = json.loads(it["manifest.json"])
        if m["files"]:
            m["files"][0]["sha256"] = "0" * 64
        else:
            it["media/manuscripts/extra.txt"] = b"hello"
            m["files"].append({"path": "media/manuscripts/extra.txt", "size": 5, "sha256": "0" * 64})
        it["manifest.json"] = json.dumps(m).encode()
        return it
    forged("bad-sha.zip", corrupt)
    r = client.post("/api/disk/restore", json={"name": "bad-sha.zip"})
    assert r.status_code == 422 and "Checksum" in r.json()["error"]
    # nothing leaked: no new book, no leftover folders, nothing written outside the library
    with ctx.db.session() as conn:
        assert len(rows(conn, "SELECT id FROM books")) == 1
    assert sorted(p.name for p in ctx.category_dir("segments").iterdir()) == lib_before
    assert not (ctx.home / "escape.txt").exists() and not (ctx.library_root() / "escape.txt").exists()


# ---------------------------------------------------------------- admin only


def test_disk_routes_are_admin_only(ctx):
    for m, p in (("GET", "/api/disk/usage"), ("GET", "/api/disk/backups"), ("POST", "/api/disk/books/1/delete"), ("POST", "/api/disk/books/1/purge"),
                 ("POST", "/api/disk/books/1/backup"), ("POST", "/api/disk/restore"), ("POST", "/api/disk/backups/delete"), ("POST", "/api/disk/db-backup")):
        assert is_admin_only(m, p), (m, p)
    store = AuthStore(ctx.config_dir / "auth.json")
    store.set_user("boss", "correct-horse-battery", "admin")
    store.set_user("clerk", "correct-horse-battery2", "operator")
    app = create_app(ctx, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:7861", headers={"X-Studio-Request": "1"}) as c:
        assert c.post("/api/auth/login", json={"username": "clerk", "password": "correct-horse-battery2"}).status_code == 200
        assert c.get("/api/disk/usage").status_code == 403
        assert c.post("/api/disk/books/1/delete", json={"confirm": True}).status_code == 403
        assert c.post("/api/disk/books/1/purge", json={"scope": "takes", "confirm": True}).status_code == 403
        assert c.post("/api/disk/restore", json={"name": "x.zip"}).status_code == 403
        c.cookies.clear()
        assert c.post("/api/auth/login", json={"username": "boss", "password": "correct-horse-battery"}).status_code == 200
        assert c.get("/api/disk/usage").status_code == 200
