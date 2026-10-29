"""Per-chapter tools: regenerate a whole chapter, edit chapter titles (override), cover capability.

Single-chapter export already exists as export kind ``chapter_m4b`` (assembly.export_book); it is
gated by the same "no segment without usable audio" rule as the book export and is labelled DRAFT
until the book has an active release approval.

Titles: ``chapters.title`` always holds the *effective* title (so markers, exports, reports and
file names need no change). ``chapters.original_title`` holds the parsed title once overridden and
is NULL otherwise. A title change never touches segments or takes; it only makes the chapter
master outdated (its tags/markers change), which a re-assemble fixes without any re-synthesis.
"""
from __future__ import annotations

from .db import row, rows
from .services import NeedsConfirmation, ServiceError, _review, _revoke, enqueue_book, get_book
from .util import now
from .worker import refresh_book_state


def placeholder_cover_available() -> bool:
    """Generated placeholder covers need Pillow, which is not a dependency; so: unavailable."""
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


def regenerate_chapter(ctx, book_id: int, chapter_id: int, confirm: bool = False) -> dict:
    with ctx.db.tx() as conn:
        ch = row(conn, "SELECT * FROM chapters WHERE id=? AND book_id=?", (chapter_id, book_id))
        if not ch:
            raise ServiceError("Chapter not found", 404)
        if not row(conn, "SELECT id FROM books WHERE id=? AND archived=0", (book_id,)):
            raise ServiceError("Book not found", 404)
        segs = rows(conn, "SELECT * FROM segments WHERE chapter_id=? ORDER BY idx", (chapter_id,))
        if any(s["status"] == "synthesizing" for s in segs):
            raise ServiceError("A segment of this chapter is rendering right now. Pause the job first.", 409)
        rendered = [s for s in segs if s["status"] != "pending"]
        if not rendered:
            return {"ok": True, "regenerated": 0, "already_pending": True, "chapter_id": chapter_id}
        if not confirm:
            raise NeedsConfirmation(f"{len(rendered)} rendered segment(s) of chapter {ch['idx']} '{ch['title']}' will be regenerated. "
                                    "Old takes are kept as history. Any release approval will be revoked.", {"rendered_segments": len(rendered)})
        conn.execute("""UPDATE segments SET round=round+1, attempts_used=0, status='pending', selected_attempt_id=NULL, flag_reason=NULL, updated_at=?
                        WHERE chapter_id=? AND status<>'pending'""", (now(), chapter_id))
        conn.execute("DELETE FROM chapter_renders WHERE chapter_id=?", (chapter_id,))
        for s in rendered:
            _review(conn, ctx, s, "regenerate", {"previous_status": s["status"], "previous_attempt": s["selected_attempt_id"], "scope": "chapter"})
        _revoke(conn, ctx, book_id, f"chapter {ch['idx']} regenerated")
        refresh_book_state(conn, book_id)
    enqueue_book(ctx, book_id, front=True)
    return {"ok": True, "regenerated": len(rendered), "chapter_id": chapter_id}


def set_chapter_title(ctx, book_id: int, chapter_id: int, title: str | None) -> dict:
    """Set a title override (non-empty) or restore the parsed title (empty/None)."""
    new = " ".join((title or "").split())[:300]
    with ctx.db.tx() as conn:
        ch = row(conn, "SELECT * FROM chapters WHERE id=? AND book_id=?", (chapter_id, book_id))
        if not ch:
            raise ServiceError("Chapter not found", 404)
        original = ch["original_title"] if ch["original_title"] is not None else ch["title"]
        if not new or new == original:
            conn.execute("UPDATE chapters SET title=?, original_title=NULL WHERE id=?", (original, chapter_id))
            effective = original
        else:
            conn.execute("UPDATE chapters SET title=?, original_title=? WHERE id=?", (new, original, chapter_id))
            effective = new
        if effective != ch["title"]:
            ctx.audit(conn, "chapter", str(chapter_id), "title", {"from": ch["title"], "to": effective, "original": original})
    return get_book(ctx, book_id)
