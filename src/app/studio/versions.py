"""Narrator versions (v0.7.0): one manuscript narrated by several narrators.

Every version is a complete book of its own (own segments, takes, chapter masters, exports, review and release state), so narrating again with
another voice never touches an earlier pass. Versions of one manuscript share ``books.work_uid``. Adding a version copies the manuscript, chapters,
characters, speaker attribution, Director guidance and the operator's text edits, and starts with every segment pending."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from .db import row, rows
from .diskmgmt import _insert
from .config import STORAGE_CATEGORIES
from .services import (ServiceError, _book_storage, enqueue_book, get_book, siblings, synth_fingerprint, work_key)
from .util import dumps, loads, new_uid, now
from .worker import refresh_book_state

# Per-book tables copied to a new version, and the ones deliberately left behind (audio, jobs, history of the old narration).
COPIED = ("manuscripts", "chapters", "characters", "segments", "nar_spans", "story_notes", "directions", "seg_edits", "segment_history",
          "chapter_text", "analysis_stages", "pronunciations")
NOT_COPIED = ("attempts", "chapter_renders", "exports", "releases", "jobs", "job_logs", "review_actions", "direction_log", "events", "ab_votes")


def add_version(ctx, book_id: int, voice_id: int, instructions: str | None = None, queue: bool = False, actor: str = "operator") -> dict:
    """Start another narration of the same manuscript with ``voice_id``. Returns the NEW book; the source book is not modified."""
    with ctx.db.session() as conn:
        src = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        if not src:
            raise ServiceError("Book not found", 404)
        voice = row(conn, "SELECT * FROM voices WHERE id=? AND archived=0", (voice_id,))
        if not voice:
            raise ServiceError("Choose an approved narrator voice.", 422)
        vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (voice["current_version_id"],))
        instr = src["instructions"] if instructions is None else str(instructions).strip()[:1000]
        for sib in siblings(conn, src):
            if sib["voice_id"] == voice["id"] and (sib["instructions"] or "") == instr:
                raise ServiceError(f"This book already has a version narrated by {voice['name']}"
                                   f"{' with these delivery instructions' if instr else ''}. Open it from the narrator list, or choose another voice.", 409)
        if row(conn, "SELECT id FROM jobs WHERE book_id=? AND status='running'", (book_id,)):
            raise ServiceError("The book is rendering right now. Pause or finish its job before adding another narrator.", 409)
        tables = {t: rows(conn, f"SELECT * FROM {t} WHERE book_id=?", (book_id,)) for t in COPIED if t not in ("segments", "chapter_text", "directions")}
        tables["segments"] = rows(conn, "SELECT * FROM segments WHERE book_id=? ORDER BY id", (book_id,))
        tables["chapter_text"] = rows(conn, "SELECT * FROM chapter_text WHERE book_id=?", (book_id,))
        tables["directions"] = rows(conn, "SELECT * FROM directions WHERE book_id=?", (book_id,))
        # a take-free copy of the settings snapshot: the new narrator gets the same rendering settings as the book it was copied from
        snap = loads(src["settings_json"], {})
    fp = synth_fingerprint(snap, vv, instr)
    old_uid, uid = src["uid"], new_uid("bk")
    storage = _book_storage(ctx, uid)
    created: list[Path] = []
    warnings: list[str] = []
    try:
        for d in storage.values():
            Path(d).mkdir(parents=True, exist_ok=True)
            created.append(Path(d))
    except OSError as exc:
        raise ServiceError(f"Storage location is unavailable: {exc}. Check Settings & Storage.", 503)
    try:
        # the immutable original manuscript, and the cover, are copied: a version must stay complete if its sibling is deleted
        man_paths: dict[int, str] = {}
        for m in tables["manuscripts"]:
            dest = Path(storage["manuscripts"]) / Path(m["stored_path"]).name
            if Path(m["stored_path"]).is_file():
                shutil.copy2(m["stored_path"], dest)
                try:
                    os.chmod(dest, 0o444)
                except OSError:
                    pass
            else:
                warnings.append(f"The original manuscript file {m['original_name']} was not found, so it was not copied.")
            man_paths[m["id"]] = str(dest)
        cover = None
        if src["cover_path"] and Path(src["cover_path"]).is_file():
            cover = Path(storage["manuscripts"]) / Path(src["cover_path"]).name
            shutil.copy2(src["cover_path"], cover)

        def reuid(s: str) -> str:
            return uid + s[len(old_uid):] if s.startswith(old_uid) else f"{uid}-{s}"

        with ctx.db.tx() as conn:
            conn.execute("UPDATE books SET work_uid=uid WHERE id=? AND work_uid IS NULL", (book_id,))
            bid = _insert(conn, "books", src, uid=uid, work_uid=work_key(src), voice_id=voice["id"], voice_version_id=vv["id"], instructions=instr,
                          narrator_credit=voice["name"][:200], synth_fingerprint=fp, storage_json=dumps(storage), cover_path=str(cover) if cover else None,
                          state="imported", last_error=None, archived=0, created_at=now(), updated_at=now())
            chmap: dict[int, int] = {}
            for c in tables["chapters"]:
                chmap[c["id"]] = _insert(conn, "chapters", c, book_id=bid)
            charmap: dict[int, int] = {}
            for c in tables["characters"]:
                charmap[c["id"]] = _insert(conn, "characters", c, book_id=bid, uid=new_uid("ch"), merged_into=None)
            for c in tables["characters"]:
                if c.get("merged_into") in charmap:
                    conn.execute("UPDATE characters SET merged_into=? WHERE id=?", (charmap[c["merged_into"]], charmap[c["id"]]))
            for m in tables["manuscripts"]:
                _insert(conn, "manuscripts", m, book_id=bid, stored_path=man_paths[m["id"]])
            segmap: dict[int, int] = {}
            for s in tables["segments"]:
                segmap[s["id"]] = _insert(conn, "segments", s, book_id=bid, chapter_id=chmap[s["chapter_id"]], sid=reuid(s["sid"]),
                                          character_id=charmap.get(s.get("character_id")), status="pending", round=1, attempts_used=0,
                                          selected_attempt_id=None, flag_reason=None, note=None, updated_at=now())
            for sp in tables["nar_spans"]:
                _insert(conn, "nar_spans", sp, book_id=bid, uid=reuid(sp["uid"]), chapter_id=chmap[sp["chapter_id"]],
                        speaker_id=charmap.get(sp.get("speaker_id")), guess_id=charmap.get(sp.get("guess_id")))
            for n in tables["story_notes"]:
                _insert(conn, "story_notes", n, book_id=bid, character_id=charmap.get(n.get("character_id")))
            for d in tables["directions"]:
                if d["segment_id"] in segmap:
                    _insert(conn, "directions", d, book_id=bid, segment_id=segmap[d["segment_id"]])
            for e in tables["seg_edits"]:
                _insert(conn, "seg_edits", e, book_id=bid, chapter_id=chmap[e["chapter_id"]])
            for h in tables["segment_history"]:
                _insert(conn, "segment_history", h, book_id=bid, chapter_id=chmap.get(h["chapter_id"], 0), sid=reuid(h["sid"]))
            for t in tables["chapter_text"]:
                _insert(conn, "chapter_text", t, drop=(), chapter_id=chmap[t["chapter_id"]], book_id=bid)
            for st in tables["analysis_stages"]:
                _insert(conn, "analysis_stages", st, drop=(), book_id=bid)
            for p in tables["pronunciations"]:
                _insert(conn, "pronunciations", p, book_id=bid)
            refresh_book_state(conn, bid)
            ctx.audit(conn, "book", bid, "narrator_version_added", {"from_book": book_id, "from_uid": old_uid, "uid": uid, "voice_id": voice["id"],
                                                                   "voice_version_id": vv["id"], "instructions": instr, "segments": len(segmap)}, actor)
            ctx.event(conn, "narrator", f"Added a '{voice['name']}' narration of '{src['title']}' ({len(segmap)} segments, none rendered yet).", bid)
    except BaseException:
        for d in created:
            shutil.rmtree(d, ignore_errors=True)
        raise
    if queue:
        enqueue_book(ctx, bid)
    out = get_book(ctx, bid)
    out["warnings"] = warnings
    return out
