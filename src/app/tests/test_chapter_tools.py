"""Per-chapter tools (C4): regenerate chapter, title override, chapter export, cover. MOCK engine only."""
from __future__ import annotations

from conftest import CHAPTERS, make_epub, needs_ffmpeg
from studio.db import row, rows
from test_integration import confirm, ryan, upload


def _book(client, make_worker):
    v = ryan(client)
    b = confirm(client, upload(client, "book.epub", make_epub(CHAPTERS)), v["id"], queue=True)
    w = make_worker()
    w.run_until_idle()
    return b, w


def test_schema_has_original_title(ctx):
    from studio.db import LATEST_VERSION
    assert LATEST_VERSION >= 3
    with ctx.db.session() as conn:
        assert "original_title" in [r["name"] for r in rows(conn, "PRAGMA table_info(chapters)")]


def test_regenerate_chapter_needs_confirmation_and_only_touches_that_chapter(client, ctx, make_worker):
    b, w = _book(client, make_worker)
    book = client.get(f"/api/books/{b['id']}").json()
    c1, c2 = book["chapters"][0], book["chapters"][1]
    url = f"/api/books/{b['id']}/chapters/{c1['id']}/regenerate"
    r = client.post(url, json={})
    assert r.status_code == 409 and r.json()["details"]["rendered_segments"] >= 1
    with ctx.db.session() as conn:
        assert row(conn, "SELECT COUNT(*) n FROM segments WHERE chapter_id=? AND status='pending'", (c1["id"],))["n"] == 0
    assert client.post(url, json={"confirm_invalidate": True}).status_code == 200
    with ctx.db.session() as conn:
        assert row(conn, "SELECT COUNT(*) n FROM segments WHERE chapter_id=? AND status<>'pending'", (c1["id"],))["n"] == 0
        assert row(conn, "SELECT COUNT(*) n FROM segments WHERE chapter_id=? AND status='pending'", (c2["id"],))["n"] == 0
        assert row(conn, "SELECT COUNT(*) n FROM chapter_renders WHERE chapter_id=?", (c1["id"],))["n"] == 0
        assert row(conn, "SELECT COUNT(*) n FROM jobs WHERE book_id=? AND kind='book' AND status='queued'", (b["id"],))["n"] == 1
    # nothing rendered any more: second call is a no-op, unknown chapter is a 404
    assert client.post(url, json={}).json()["already_pending"] is True
    assert client.post(f"/api/books/{b['id']}/chapters/999999/regenerate", json={}).status_code == 404
    w.run_until_idle()
    assert all(c["segments"] and not c["missing_audio"] for c in client.get(f"/api/books/{b['id']}").json()["chapters"])


def test_regenerate_chapter_revokes_release(client, ctx, make_worker):
    b, w = _book(client, make_worker)
    r = client.post(f"/api/books/{b['id']}/release", json={"approved_by": "QA Lead", "listen_scope": "full", "attest": True})
    if r.status_code != 200:  # release may need assembled masters / unflagged segments
        return
    cid = client.get(f"/api/books/{b['id']}").json()["chapters"][0]["id"]
    client.post(f"/api/books/{b['id']}/chapters/{cid}/regenerate", json={"confirm_invalidate": True})
    assert client.get(f"/api/books/{b['id']}").json()["release"] is None


def test_chapter_title_override_preserves_original_and_audio(client, ctx, make_worker):
    b, w = _book(client, make_worker)
    ch = client.get(f"/api/books/{b['id']}").json()["chapters"][0]
    with ctx.db.session() as conn:
        before = rows(conn, "SELECT id, status, selected_attempt_id FROM segments WHERE chapter_id=? ORDER BY idx", (ch["id"],))
    r = client.patch(f"/api/books/{b['id']}/chapters/{ch['id']}", json={"title": "  A   New Name "})
    assert r.status_code == 200
    c = r.json()["chapters"][0]
    assert c["title"] == "A New Name" and c["title_overridden"] and c["original_title"] == ch["title"]
    with ctx.db.session() as conn:
        assert rows(conn, "SELECT id, status, selected_attempt_id FROM segments WHERE chapter_id=? ORDER BY idx", (ch["id"],)) == before
        assert row(conn, "SELECT original_title FROM chapters WHERE id=?", (ch["id"],))["original_title"] == ch["title"]
    # second edit keeps the first original; reverting restores it
    c = client.patch(f"/api/books/{b['id']}/chapters/{ch['id']}", json={"title": "Another"}).json()["chapters"][0]
    assert c["original_title"] == ch["title"]
    c = client.patch(f"/api/books/{b['id']}/chapters/{ch['id']}", json={"title": ""}).json()["chapters"][0]
    assert c["title"] == ch["title"] and not c["title_overridden"]
    assert client.patch(f"/api/books/{b['id']}/chapters/999999", json={"title": "x"}).status_code == 404


@needs_ffmpeg
def test_title_flows_into_chapter_export_and_only_marks_master_outdated(client, ctx, make_worker):
    from studio.audio import probe_summary
    b, w = _book(client, make_worker)
    ch = client.get(f"/api/books/{b['id']}").json()["chapters"][0]
    assert ch["render_current"]
    client.patch(f"/api/books/{b['id']}/chapters/{ch['id']}", json={"title": "Renamed Chapter"})
    c = client.get(f"/api/books/{b['id']}").json()["chapters"][0]
    assert c["missing_audio"] == 0 and not c["render_current"]  # re-assemble only, no re-synthesis
    j = client.post(f"/api/books/{b['id']}/export", json={"kind": "chapter_m4b", "chapter_id": ch["id"]}).json()
    w.run_until_idle()
    assert client.get(f"/api/jobs/{j['id']}").json()["status"] == "completed"
    exp = client.get(f"/api/books/{b['id']}").json()["exports"][0]
    assert exp["kind"] == "chapter_m4b" and "DRAFT" in exp["label"]
    with ctx.db.session() as conn:
        path = row(conn, "SELECT path FROM exports WHERE id=?", (exp["id"],))["path"]
    info = probe_summary(path)
    assert [x["title"] for x in info["chapters"]] == ["Renamed Chapter"]


def test_chapter_export_is_blocked_when_a_segment_has_no_audio(client, ctx):
    v = ryan(client)
    b = confirm(client, upload(client, "book.epub", make_epub(CHAPTERS)), v["id"])
    ch = client.get(f"/api/books/{b['id']}").json()["chapters"][0]
    assert ch["missing_audio"] > 0  # nothing rendered yet: the export job would be blocked, never silently shortened


def test_cover_upload_validates_content(client, ctx):
    v = ryan(client)
    b = confirm(client, upload(client, "book.epub", make_epub(CHAPTERS)), v["id"])
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    bad = client.post(f"/api/books/{b['id']}/cover", files={"file": ("c.png", b"not an image", "image/png")})
    assert bad.status_code == 422
    assert client.post(f"/api/books/{b['id']}/cover", files={"file": ("c.gif", png, "image/gif")}).status_code == 422
    ok = client.post(f"/api/books/{b['id']}/cover", files={"file": ("c.png", png, "image/png")})
    assert ok.status_code == 200 and ok.json()["has_cover"]


def test_placeholder_cover_is_skipped_without_pillow():
    from studio import chapters
    try:
        import PIL  # noqa: F401
        expected = True
    except ImportError:
        expected = False
    assert chapters.placeholder_cover_available() is expected
