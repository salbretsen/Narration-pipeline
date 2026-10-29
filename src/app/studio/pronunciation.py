"""Pronunciation dictionary (global and per book) and per-voice pace (roadmap C2).

An entry maps a *written form* (word or phrase) to a *spoken rendition* (a plain respelling such as
``Hermione`` -> ``Her-my-oh-nee``). Entries are applied only when the text sent to the TTS model is
built (``narrative.store.plan_for_segment``): the manuscript text, ``narration_text`` and any operator
``override_text`` are never modified. Because the rendition is part of the generation key, editing the
dictionary changes the key of exactly the segments whose spoken text changes, and those (and only those)
are sent back for regeneration through the usual ``invalidate_changed`` mechanism.

Per-book entries override global entries with the same written form.
"""
from __future__ import annotations

import re
from functools import lru_cache

from .db import row, rows, scalar
from .util import now

MAX_ENTRIES = 2000
PACE_MIN, PACE_MAX = 0.8, 1.25


def _err(message: str, status: int = 422, details=None):
    from .services import ServiceError

    return ServiceError(message, status, details)


# ------------------------------------------------------------------ applying


@lru_cache(maxsize=64)
def _compile(entries: tuple):
    """entries: tuple of (written, spoken, case_sensitive, whole_word). One alternation, longest first,
    so a phrase beats its own words and a replacement is never re-scanned (no chained rewrites)."""
    ordered = sorted(entries, key=lambda e: -len(e[0]))
    parts, spoken = [], []
    for written, spk, cs, ww in ordered:
        body = re.escape(written).replace(r"\ ", r"\s+")
        if ww:
            body = r"(?<!\w)" + body + r"(?!\w)"
        parts.append("(" + ("" if cs else "(?i:") + body + ("" if cs else ")") + ")")
        spoken.append(spk)
    return re.compile("|".join(parts)), spoken


def apply(entries: list[dict], text: str) -> str:
    """Return ``text`` with every enabled entry applied. ``entries`` are dict rows (already merged)."""
    if not entries or not text:
        return text
    key = tuple((e["written"], e["spoken"], bool(e["case_sensitive"]), bool(e["whole_word"])) for e in entries)
    rx, spoken = _compile(key)
    return rx.sub(lambda m: spoken[m.lastindex - 1], text)


def effective_entries(conn, book_id: int | None) -> list[dict]:
    """Enabled global entries, overlaid by the book's own (same written form and case rule: the book wins)."""
    out: dict[tuple, dict] = {}
    for r in rows(conn, "SELECT * FROM pronunciations WHERE enabled=1 AND book_id IS NULL ORDER BY id"):
        out[(r["written"].lower() if not r["case_sensitive"] else r["written"], bool(r["case_sensitive"]))] = r
    if book_id:
        for r in rows(conn, "SELECT * FROM pronunciations WHERE enabled=1 AND book_id=? ORDER BY id", (book_id,)):
            out[(r["written"].lower() if not r["case_sensitive"] else r["written"], bool(r["case_sensitive"]))] = r
    return list(out.values())


def apply_for_book(conn, book_id: int, text: str) -> str:
    return apply(effective_entries(conn, book_id), text)


# ------------------------------------------------------------------ CRUD


def _clean(written: str, spoken: str) -> tuple[str, str]:
    written = " ".join((written or "").split())
    spoken = " ".join((spoken or "").split())
    if not 1 <= len(written) <= 80:
        raise _err("The written form must be 1-80 characters.")
    if not 1 <= len(spoken) <= 200:
        raise _err("The spoken form must be 1-200 characters.")
    return written, spoken


def _entry(r: dict) -> dict:
    return {"id": r["id"], "book_id": r["book_id"], "scope": "book" if r["book_id"] else "global", "written": r["written"], "spoken": r["spoken"],
            "case_sensitive": bool(r["case_sensitive"]), "whole_word": bool(r["whole_word"]), "enabled": bool(r["enabled"]), "note": r["note"],
            "created_at": r["created_at"], "updated_at": r["updated_at"]}


def list_entries(ctx, book_id: int | None = None) -> dict:
    """book_id None -> the global dictionary. Otherwise the book's own entries plus the global ones."""
    with ctx.db.session() as conn:
        if book_id is not None and not row(conn, "SELECT 1 FROM books WHERE id=?", (book_id,)):
            raise _err("Book not found", 404)
        glob = [_entry(r) for r in rows(conn, "SELECT * FROM pronunciations WHERE book_id IS NULL ORDER BY lower(written), id")]
        out = {"global": glob}
        if book_id is not None:
            out["book"] = [_entry(r) for r in rows(conn, "SELECT * FROM pronunciations WHERE book_id=? ORDER BY lower(written), id", (book_id,))]
        return out


def preview(ctx, text: str, book_id: int | None = None) -> dict:
    with ctx.db.session() as conn:
        res = apply_for_book(conn, book_id, text)
    return {"input": text, "output": res, "changed": res != text}


def _affected_books(conn, book_id: int | None) -> list[int]:
    if book_id is not None:
        return [book_id]
    return [r["id"] for r in rows(conn, "SELECT id FROM books WHERE archived=0")]


def reconcile(conn, ctx, book_ids: list[int]) -> dict[int, int]:
    """Send segments whose generation key changed back for regeneration (others untouched).
    -> {book_id: segments reset}. Old takes stay as history, never mixed into the book."""
    from . import services as S
    from .narrative import store as ST

    out = {}
    for bid in book_ids:
        if not scalar(conn, "SELECT COUNT(*) FROM segments WHERE book_id=? AND status IN ('passed','flagged')", (bid,), 0):
            continue
        reset = ST.invalidate_changed(conn, ctx, bid)
        if reset:
            out[bid] = len(reset)
            S.refresh_book_state(conn, bid)
    return out


def _commit(ctx, book_id: int | None, confirm: bool, mutate, what: str) -> dict:
    """Run ``mutate(conn)`` and then reconcile affected segments in one transaction. Without ``confirm`` and
    with segments affected, the transaction is rolled back and a 409 reports how many would be regenerated."""
    from .services import NeedsConfirmation

    with ctx.db.tx() as conn:
        result = mutate(conn)
        reset = reconcile(conn, ctx, _affected_books(conn, book_id))
        n = sum(reset.values())
        if n and not confirm:
            raise NeedsConfirmation(f"This pronunciation change alters how {n} already-rendered segment(s) are spoken; they would be marked for regeneration "
                                    "(earlier takes are kept as history).", {"stale_segments": n, "books": len(reset)})
        ctx.audit(conn, "pronunciation", book_id or 0, what, {"stale_segments": n})
        if n:
            ctx.event(conn, "pronunciation", f"Pronunciation change: {n} segment(s) marked for regeneration.", book_id, "warning")
    return {**result, "stale_segments": n, "books_affected": len(reset)}


def _find_dupe(conn, book_id, written, cs, exclude=None):
    sql = "SELECT id FROM pronunciations WHERE " + ("book_id IS NULL" if book_id is None else "book_id=?") + \
          " AND case_sensitive=? AND " + ("written=?" if cs else "lower(written)=lower(?)")
    params = ([] if book_id is None else [book_id]) + [1 if cs else 0, written]
    r = row(conn, sql + (" AND id<>?" if exclude else ""), params + ([exclude] if exclude else []))
    return r


def create_entry(ctx, written: str, spoken: str, book_id: int | None = None, case_sensitive: bool = False, whole_word: bool = True,
                 note: str = "", confirm: bool = False) -> dict:
    written, spoken = _clean(written, spoken)

    def mutate(conn):
        if book_id is not None and not row(conn, "SELECT 1 FROM books WHERE id=?", (book_id,)):
            raise _err("Book not found", 404)
        if scalar(conn, "SELECT COUNT(*) FROM pronunciations", (), 0) >= MAX_ENTRIES:
            raise _err(f"At most {MAX_ENTRIES} pronunciation entries are supported.")
        if _find_dupe(conn, book_id, written, case_sensitive):
            raise _err(f"'{written}' already has an entry in this dictionary; edit it instead.", 409)
        t = now()
        eid = conn.execute("""INSERT INTO pronunciations(book_id, written, spoken, case_sensitive, whole_word, enabled, note, created_at, updated_at)
                              VALUES (?,?,?,?,?,1,?,?,?)""", (book_id, written, spoken, 1 if case_sensitive else 0, 1 if whole_word else 0, (note or "")[:200], t, t)).lastrowid
        return {"id": eid}

    res = _commit(ctx, book_id, confirm, mutate, "entry_created")
    return {**res, "entry": get_entry(ctx, res["id"])}


def get_entry(ctx, entry_id: int) -> dict:
    with ctx.db.session() as conn:
        r = row(conn, "SELECT * FROM pronunciations WHERE id=?", (entry_id,))
        if not r:
            raise _err("Entry not found", 404)
        return _entry(r)


def update_entry(ctx, entry_id: int, fields: dict, confirm: bool = False) -> dict:
    with ctx.db.session() as conn:
        cur = row(conn, "SELECT * FROM pronunciations WHERE id=?", (entry_id,))
    if not cur:
        raise _err("Entry not found", 404)

    def mutate(conn):
        w, s = _clean(fields.get("written", cur["written"]), fields.get("spoken", cur["spoken"]))
        cs = bool(fields.get("case_sensitive", cur["case_sensitive"]))
        ww = bool(fields.get("whole_word", cur["whole_word"]))
        en = bool(fields.get("enabled", cur["enabled"]))
        note = (fields.get("note", cur["note"]) or "")[:200]
        if _find_dupe(conn, cur["book_id"], w, cs, exclude=entry_id):
            raise _err(f"'{w}' already has an entry in this dictionary.", 409)
        conn.execute("UPDATE pronunciations SET written=?, spoken=?, case_sensitive=?, whole_word=?, enabled=?, note=?, updated_at=? WHERE id=?",
                     (w, s, 1 if cs else 0, 1 if ww else 0, 1 if en else 0, note, now(), entry_id))
        return {"id": entry_id}

    res = _commit(ctx, cur["book_id"], confirm, mutate, "entry_updated")
    return {**res, "entry": get_entry(ctx, entry_id)}


def delete_entry(ctx, entry_id: int, confirm: bool = False) -> dict:
    with ctx.db.session() as conn:
        cur = row(conn, "SELECT * FROM pronunciations WHERE id=?", (entry_id,))
    if not cur:
        raise _err("Entry not found", 404)

    def mutate(conn):
        conn.execute("DELETE FROM pronunciations WHERE id=?", (entry_id,))
        return {"deleted": entry_id}

    return _commit(ctx, cur["book_id"], confirm, mutate, "entry_deleted")


# ------------------------------------------------------------------ per-voice pace


def clamp_pace(value: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise _err("Pace must be a number.")
    if not PACE_MIN <= v <= PACE_MAX:
        raise _err(f"Pace must be between {PACE_MIN} and {PACE_MAX} (1.0 is the model's natural pace).")
    return round(v, 3)


def set_voice_pace(ctx, voice_id: int, pace: float, confirm: bool = False) -> dict:
    """Change a voice's pace. Segments rendered with the voice become stale (generation key includes pace)."""
    pace = clamp_pace(pace)
    with ctx.db.session() as conn:
        v = row(conn, "SELECT * FROM voices WHERE id=?", (voice_id,))
    if not v:
        raise _err("Voice not found", 404)
    if abs(float(v.get("pace") or 1.0) - pace) < 1e-9:
        return {"voice_id": voice_id, "pace": pace, "stale_segments": 0, "books_affected": 0}

    def mutate(conn):
        conn.execute("UPDATE voices SET pace=?, updated_at=? WHERE id=?", (pace, now(), voice_id))
        return {"voice_id": voice_id, "pace": pace}

    return _commit(ctx, None, confirm, mutate, "voice_pace")
