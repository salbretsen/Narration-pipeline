"""Disk usage cache (P2-A): a second /api/disk/usage does not re-walk the library, ?refresh=1 does, disk-changing actions drop the cache,
and the numbers equal the previous implementation. MOCK engine only."""
from __future__ import annotations

import os
import time
import zipfile
from pathlib import Path

from test_disk_management import _ctx_book, _generated

from studio import diskmgmt as D
from studio.db import rows
from studio.pipeline import resolve_take


def _legacy_purgeable(conn, book):
    """The pre-cache implementation of D._purgeable, kept as the reference."""
    keep = {r["selected_attempt_id"] for r in rows(conn, "SELECT selected_attempt_id FROM segments WHERE book_id=? AND selected_attempt_id IS NOT NULL", (book["id"],))}
    n, size = 0, 0
    for a in rows(conn, "SELECT a.id, a.wav_path FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=? AND a.status!='in_progress'", (book["id"],)):
        if a["id"] in keep:
            continue
        n += 1
        if a["wav_path"]:
            size += D.file_size(resolve_take(book, a))
    return {"takes": n, "takes_bytes": size}


class _Walks:
    """Counts directory walks and per-file lstat calls made by D."""

    def __init__(self, monkeypatch):
        self.dirs = 0
        self.lstat = 0
        real_walk, real_lstat = D._walk_files, os.lstat
        real_size = D.dir_size

        def walk(path):
            self.dirs += 1
            return real_walk(path)

        def size(path):
            self.dirs += 1
            return real_size(path)

        def lstat(*a, **k):
            self.lstat += 1
            return real_lstat(*a, **k)

        monkeypatch.setattr(D, "_walk_files", walk)
        monkeypatch.setattr(D, "dir_size", size)
        monkeypatch.setattr(D.os, "lstat", lstat)


def test_numbers_equal_the_previous_implementation(client, ctx, make_worker):
    b = _generated(client, make_worker)
    u = D.usage(ctx)
    mine = next(x for x in u["books"] if x["id"] == b["id"])
    book = _ctx_book(ctx, b["id"])
    with ctx.db.session() as conn:
        old = _legacy_purgeable(conn, book)
    assert mine["purgeable"]["takes"] == old["takes"] > 0
    assert mine["purgeable"]["takes_bytes"] == old["takes_bytes"] > 0
    dirs = D.book_dirs(book)
    for cat, d in dirs.items():
        assert mine["categories"][cat] == D.dir_size(d), cat
    assert mine["busy"] is False and mine["narrator"] == "Ryan" and mine["display_title"] == "Alpha - Ryan"


def test_busy_and_narrator_come_from_the_batched_queries(client, ctx):
    from conftest import make_text

    v = next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
    prev = client.post("/api/imports", files={"file": ("q.txt", make_text(1, title="Queued").encode(), "text/plain")}).json()
    b = client.post(f"/api/imports/{prev['token']}/confirm", json={"title": "Queued", "voice_id": v["id"], "queue": True}).json()
    idle = client.post("/api/imports", files={"file": ("i.txt", make_text(1, title="Idle").encode(), "text/plain")}).json()
    b2 = client.post(f"/api/imports/{idle['token']}/confirm", json={"title": "Idle", "voice_id": v["id"], "queue": False}).json()
    u = D.usage(ctx)
    by = {x["id"]: x for x in u["books"]}
    assert by[b["id"]]["busy"] is True and by[b2["id"]]["busy"] is False


def test_second_usage_call_does_not_walk_again(client, ctx, make_worker, monkeypatch):
    _generated(client, make_worker)
    D.invalidate_usage(ctx)
    walks = _Walks(monkeypatch)
    first = client.get("/api/disk/usage").json()
    assert first["cached"] is False and walks.dirs > 0 and walks.lstat > 0
    d0, l0 = walks.dirs, walks.lstat
    second = client.get("/api/disk/usage").json()
    assert second["cached"] is True and second["measured_at"] == first["measured_at"]
    assert (walks.dirs, walks.lstat) == (d0, l0)          # nothing was walked or stat'ed
    assert {k: v for k, v in second.items() if k not in ("age_s", "cached")} == {k: v for k, v in first.items() if k not in ("age_s", "cached")}
    assert second["age_s"] >= 0


def test_refresh_forces_a_new_measurement(client, ctx, make_worker, monkeypatch):
    _generated(client, make_worker)
    D.invalidate_usage(ctx)
    walks = _Walks(monkeypatch)
    first = client.get("/api/disk/usage").json()
    d0 = walks.dirs
    again = client.get("/api/disk/usage?refresh=1").json()
    assert again["cached"] is False and walks.dirs > d0 and again["measured_at"] >= first["measured_at"]
    d1 = walks.dirs
    assert client.get("/api/disk/usage").json()["cached"] is True and walks.dirs == d1   # the refreshed result is what the cache now holds


def test_the_cache_expires_after_the_ttl(client, ctx):
    D.invalidate_usage(ctx)
    first = client.get("/api/disk/usage").json()
    key = D._usage_key(ctx)
    t, payload = D._USAGE_CACHE[key]
    D._USAGE_CACHE[key] = (t - D.USAGE_TTL_S - 5, payload)
    again = client.get("/api/disk/usage").json()
    assert again["cached"] is False and again["measured_at"] > first["measured_at"] - 1 and again["age_s"] < 5


def test_direct_calls_without_max_age_always_measure(ctx, tmp_path):
    before = D.usage(ctx)["other"]["recordings"]
    rec = ctx.data_dir / "recordings"
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "x.bin").write_bytes(b"x" * 1000)
    assert D.usage(ctx)["other"]["recordings"] == before + 1000
    assert D.usage(ctx)["cached"] is False


def test_purge_delete_backup_and_restore_invalidate(client, ctx, make_worker):
    b = _generated(client, make_worker)
    u1 = client.get("/api/disk/usage").json()
    assert client.get("/api/disk/usage").json()["cached"] is True
    mine = next(x for x in u1["books"] if x["id"] == b["id"])
    # a confirmation preview changes nothing: the cache stays
    r = client.post(f"/api/disk/books/{b['id']}/purge", json={"scope": "takes", "confirm": False})
    assert r.status_code == 409
    assert client.get("/api/disk/usage").json()["cached"] is True
    # purge
    r = client.post(f"/api/disk/books/{b['id']}/purge", json={"scope": "takes", "confirm": True})
    assert r.status_code == 200 and r.json()["freed_bytes"] > 0
    u2 = client.get("/api/disk/usage").json()
    assert u2["cached"] is False
    mine2 = next(x for x in u2["books"] if x["id"] == b["id"])
    assert mine2["categories"]["alternates"] < mine["categories"]["alternates"] and mine2["purgeable"]["takes"] == 0
    assert client.get("/api/disk/usage").json()["cached"] is True
    # backup, then delete the backup, then restore a second backup
    name = client.post(f"/api/disk/books/{b['id']}/backup").json()["name"]
    u3 = client.get("/api/disk/usage").json()
    assert u3["cached"] is False and any(x["name"] == name for x in u3["backups"]["books"]) and u3["other"]["backups"] > u2["other"]["backups"]
    assert client.get("/api/disk/usage").json()["cached"] is True
    restored = client.post("/api/disk/restore", json={"name": name}).json()
    u4 = client.get("/api/disk/usage").json()
    assert u4["cached"] is False and any(x["id"] == restored["book_id"] for x in u4["books"])
    assert client.get("/api/disk/usage").json()["cached"] is True
    assert client.post("/api/disk/backups/delete", json={"name": name, "confirm": True}).status_code == 200
    u5 = client.get("/api/disk/usage").json()
    assert u5["cached"] is False and not any(x["name"] == name for x in u5["backups"]["books"])
    assert client.get("/api/disk/usage").json()["cached"] is True
    assert client.post(f"/api/disk/books/{restored['book_id']}/delete", json={"confirm": True}).status_code == 200
    u6 = client.get("/api/disk/usage").json()
    assert u6["cached"] is False and not any(x["id"] == restored["book_id"] for x in u6["books"])
    assert client.get("/api/disk/usage").json()["cached"] is True
    assert client.post("/api/disk/db-backup").status_code == 200
    assert client.get("/api/disk/usage").json()["cached"] is False


def test_a_measurement_started_before_an_invalidation_is_not_stored(ctx, monkeypatch):
    real = D._measure

    def slow(c):
        out = real(c)
        D.invalidate_usage(c)        # an action finished while we were measuring
        return out

    monkeypatch.setattr(D, "_measure", slow)
    D.usage(ctx, max_age=60)
    assert D._usage_key(ctx) not in D._USAGE_CACHE


def test_list_backups_reads_each_manifest_once(client, ctx, make_worker, monkeypatch):
    b = _generated(client, make_worker)
    name = client.post(f"/api/disk/books/{b['id']}/backup").json()["name"]
    opened = []
    real = zipfile.ZipFile

    class Counting(real):
        def __init__(self, file, *a, **k):
            opened.append(str(file))
            super().__init__(file, *a, **k)

    monkeypatch.setattr(D.zipfile, "ZipFile", Counting)
    D._MANIFEST_CACHE.clear()
    first = D.list_backups(ctx)
    assert len(opened) == 1 and first["books"][0]["name"] == name and first["books"][0]["title"] == "Alpha"
    second = D.list_backups(ctx)
    assert len(opened) == 1 and second == first                       # served from the (path, mtime, size) cache
    p = D.backups_root(ctx) / name
    time.sleep(0.02)
    p.write_bytes(b"not a zip any more")                              # same path, new mtime/size: must be re-read
    third = D.list_backups(ctx)
    assert len(opened) == 2 and third["books"][0]["invalid"] is True and third["books"][0]["title"] is None
    p.unlink()
    assert D.list_backups(ctx)["books"] == [] and not [k for k in D._MANIFEST_CACHE if k[0] == str(p)]


def test_purgeable_uses_one_stat_per_take_file(client, ctx, make_worker, monkeypatch):
    b = _generated(client, make_worker)
    book = _ctx_book(ctx, b["id"])
    dirs = D.book_dirs(book)
    n_files = sum(1 for d in (dirs["segments"], dirs["alternates"]) for p in Path(d).rglob("*") if p.is_file())
    lstat = []
    real = os.lstat
    monkeypatch.setattr(D.os, "lstat", lambda *a, **k: (lstat.append(a[0]), real(*a, **k))[1])
    stats = []
    real_isfile = os.path.isfile
    monkeypatch.setattr(D.os.path, "isfile", lambda *a, **k: (stats.append(a[0]), real_isfile(*a, **k))[1])
    with ctx.db.session() as conn:
        _w, sizes = D._walk_files(dirs["segments"])
        _w2, sizes2 = D._walk_files(dirs["alternates"])
        lstat.clear(); stats.clear()
        out = D._purgeable(conn, book, dirs, {**sizes, **sizes2})
    assert out["takes"] > 0 and out["takes_bytes"] > 0
    assert lstat == [] and stats == []          # every size came from the walk: no second stat, no existence check
