"""Synthetic books for performance and equivalence tests: segments, takes and chapter masters are written straight to SQLite and to
tiny placeholder files (no worker, no audio), so a 4,000-segment book builds in about a second. MOCK data only."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from conftest import make_text

from studio import services as S
from studio.assembly import takes_digest
from studio.db import row
from studio.pipeline import book_dirs
from studio.util import now


def import_empty(ctx, title: str = "Synthetic", voice_name: str = "Ryan") -> dict:
    """A tiny imported book (real chapters/segments from a short text); callers usually replace its segments with synth_segments."""
    voice = next(v for v in S.list_voices(ctx) if v["name"] == voice_name)
    prev = S.create_import(ctx, f"{title}.txt", make_text(1, title=title).encode())
    return S.confirm_import(ctx, prev["token"], {"title": title, "voice_id": voice["id"], "queue": False})


def synth_segments(ctx, book_id: int, n_segments: int, n_chapters: int = 10, renders: int = 3, missing_every: int = 97) -> dict:
    """Replace the book's segments/chapters with ``n_segments`` spread over ``n_chapters``. Mix: ~55% passed, ~20% approved, ~5% flagged, the rest
    pending; ~20% of rendered segments also carry an older unselected attempt; every ``missing_every``-th selected take has no file on disk;
    the first ``renders`` chapters get current WAV+MP3 masters. Returns {"book_id", "chapter_ids"}."""
    with ctx.db.session() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
    dirs = book_dirs(b)
    seg_dir, alt_dir, mast_dir = dirs["segments"], dirs["alternates"], dirs["masters"]
    for d in (seg_dir, alt_dir, mast_dir):
        d.mkdir(parents=True, exist_ok=True)
    per = max(1, n_segments // n_chapters)
    t = now()
    conn = sqlite3.connect(str(ctx.db.path))
    conn.row_factory = sqlite3.Row
    chapter_ids = []
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM attempts WHERE segment_id IN (SELECT id FROM segments WHERE book_id=?)", (book_id,))
        conn.execute("DELETE FROM chapter_renders WHERE chapter_id IN (SELECT id FROM chapters WHERE book_id=?)", (book_id,))
        conn.execute("DELETE FROM segments WHERE book_id=?", (book_id,))
        conn.execute("DELETE FROM chapters WHERE book_id=?", (book_id,))
        n = 0
        for ci in range(1, n_chapters + 1):
            count = per if ci < n_chapters else n_segments - per * (n_chapters - 1)
            chid = conn.execute("INSERT INTO chapters(book_id, idx, title, source_ref, word_count, segment_count, est_duration_s) VALUES (?,?,?,?,?,?,?)",
                                (book_id, ci, f"Chapter {ci}", "", count * 12, count, count * 4.0)).lastrowid
            chapter_ids.append(chid)
            for k in range(1, count + 1):
                n += 1
                mod = n % 20
                status = "passed" if mod < 11 else "approved" if mod < 15 else "flagged" if mod < 16 else "pending"
                sid = f"{b['uid']}-c{ci:03d}-s{k:04d}"
                sg = conn.execute("""INSERT INTO segments(sid, book_id, chapter_id, idx, para_idx, kind, pause_kind, original_text, narration_text, status, updated_at)
                                     VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (sid, book_id, chid, k, k // 3, "body", "sentence", f"Sentence {n}.", f"Sentence {n}.", status, t)).lastrowid
                if status == "pending":
                    continue
                if n % 5 == 0:   # an older, unselected take (alternates folder)
                    old = alt_dir / f"{sid}-a1.wav"
                    old.write_bytes(b"RIFFold")
                    conn.execute("""INSERT INTO attempts(segment_id, round, attempt_no, status, engine, fingerprint, text_spoken, wav_path, duration_s, sample_rate, created_at)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (sg, 1, 1, "done", "mock", b["synth_fingerprint"], "old", str(old), 3.0, 24000, t))
                wav = seg_dir / f"{sid}.wav"
                if missing_every and n % missing_every != 0:
                    wav.write_bytes(b"RIFFtone")
                aid = conn.execute("""INSERT INTO attempts(segment_id, round, attempt_no, status, engine, fingerprint, text_spoken, wav_path, duration_s, sample_rate, passed, created_at)
                                      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (sg, 1, 2, "done", "mock", b["synth_fingerprint"], "x", str(wav), 4.0, 24000, 1, t)).lastrowid
                conn.execute("UPDATE segments SET selected_attempt_id=? WHERE id=?", (aid, sg))
        conn.execute("UPDATE books SET updated_at=? WHERE id=?", (t, book_id))
        conn.execute("COMMIT")
    finally:
        conn.close()
    with ctx.db.tx() as c2:
        for chid in chapter_ids[:renders]:
            digest = takes_digest(c2, b, chid, ctx)
            for kind in ("wav", "mp3"):
                p = mast_dir / f"{chid}.{kind}"
                p.write_bytes(b"master")
                c2.execute("""INSERT INTO chapter_renders(chapter_id, kind, path, duration_s, segment_map_json, takes_digest, draft, created_at)
                              VALUES (?,?,?,?,?,?,?,?)""", (chid, kind, str(p), 60.0, "[]", digest, 1, t))
    return {"book_id": book_id, "chapter_ids": chapter_ids}


def build(ctx, n_segments: int, n_chapters: int = 10, title: str = "Synthetic") -> int:
    b = import_empty(ctx, title)
    synth_segments(ctx, b["id"], n_segments, n_chapters)
    return b["id"]


class QueryCounter:
    """Counts SQL statements run on connections opened through ctx.db while active (sqlite3 trace callback)."""

    def __init__(self, ctx):
        self.ctx, self.n, self.statements = ctx, 0, []

    def __enter__(self):
        orig = self.ctx.db.connect
        counter = self

        def connect(*a, **k):
            c = orig(*a, **k)
            c.set_trace_callback(lambda s: (setattr(counter, "n", counter.n + 1), counter.statements.append(s)))
            return c

        self._orig = orig
        self.ctx.db.connect = connect
        return self

    def __exit__(self, *exc):
        self.ctx.db.connect = self._orig
