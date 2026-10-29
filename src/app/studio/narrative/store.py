"""Persistence and orchestration for semantic segmentation (Pass 1 + Pass 2).

Segments keep stable ids. Re-segmentation (after a voice assignment, a merge of characters, a
boundary edit or a re-analysis) *reconciles* the new segment list with the existing one: a
segment whose ``[src_start, src_end)`` range is unchanged keeps its row, id, takes, direction and
history; only segments whose range changed are retired (recorded in ``segment_history``) and
replaced. That is what limits the effect of an edit to the artifacts that really depend on it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import NarrativeSettings, Settings, effective_narrative
from ..db import row, rows, scalar
from ..engines import VoiceSpec
from ..normalize import to_speech
from ..util import dumps, loads, new_uid, now, sha256_bytes, short_hash
from . import cast as C
from . import delivery as DL
from . import segmenter as G
from . import structure as S
from . import windows as W


def _err(message: str, status: int = 400, details=None):
    from ..services import ServiceError

    return ServiceError(message, status, details)


# ------------------------------------------------------------------------------ settings


def book_ns(ctx, book: dict) -> NarrativeSettings:
    return effective_narrative(ctx.settings(), loads(book.get("narrative_json"), {}))


def is_semantic(ctx, book: dict) -> bool:
    return bool(loads(book.get("narrative_json"), {}).get("semantic_segmentation"))


def set_book_overrides(ctx, book_id: int, patch: dict, actor: str = "operator") -> dict:
    """Project-level narrative settings: merged over the global defaults. A ``null`` value removes an override."""
    from ..config import merge_patch

    with ctx.db.tx() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        if not b:
            raise _err("Book not found", 404)
        cur = loads(b["narrative_json"], {})
        merged = merge_patch(cur, patch or {})

        def prune(d):
            out = {}
            for k, v in d.items():
                v = prune(v) if isinstance(v, dict) else v
                if v is not None and v != {}:
                    out[k] = v
            return out

        merged = prune(merged)
        if merged.get("semantic_segmentation") is False and cur.get("semantic_segmentation") and scalar(conn, "SELECT COUNT(*) FROM segments WHERE book_id=? AND src_start IS NOT NULL", (book_id,), 0):
            raise _err("Semantic segmentation cannot be switched off for a book that was segmented with it (segment ids and offsets depend on it).", 409)
        try:
            effective_narrative(ctx.settings(), merged)
        except Exception as exc:
            raise _err(f"Invalid narrative settings: {exc}", 422)
        conn.execute("UPDATE books SET narrative_json=?, updated_at=? WHERE id=?", (dumps(merged), now(), book_id))
        ctx.audit(conn, "book", book_id, "narrative_overrides", patch, actor)
    return get_book_settings(ctx, book_id)


def get_book_settings(ctx, book_id: int) -> dict:
    with ctx.db.session() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
    if not b:
        raise _err("Book not found", 404)
    return {"overrides": loads(b["narrative_json"], {}), "effective": book_ns(ctx, b).model_dump(mode="json"),
            "defaults": ctx.settings().narrative.model_dump(mode="json")}


# --------------------------------------------------------------------------------- stages


def set_stage(conn, book_id: int, stage: str, status: str, stats: dict | None = None, error: str | None = None) -> None:
    t = now()
    cur = row(conn, "SELECT * FROM analysis_stages WHERE book_id=? AND stage=?", (book_id, stage))
    merged = {**loads(cur["stats_json"], {}), **(stats or {})} if cur else (stats or {})
    if cur:
        conn.execute("UPDATE analysis_stages SET status=?, started_at=COALESCE(started_at, ?), finished_at=?, error=?, stats_json=? WHERE book_id=? AND stage=?",
                     (status, t, t if status in ("done", "error", "skipped") else None, error, dumps(merged), book_id, stage))
    else:
        conn.execute("INSERT INTO analysis_stages(book_id, stage, status, started_at, finished_at, error, stats_json) VALUES (?,?,?,?,?,?,?)",
                     (book_id, stage, status, t, t if status in ("done", "error", "skipped") else None, error, dumps(merged)))


STAGES = ("structure", "attribution", "characters", "summaries", "segmentation", "director", "continuity")


# ---------------------------------------------------------------------- import (Pass 1/2)


@dataclass
class ChapterPlan:
    out_idx: int
    source_chapter: object
    title: str | None
    struct: S.ChapterStructure
    words: int


def prepare_import(res, include: list[int], settings: Settings, ns: NarrativeSettings, known=None) -> tuple[list[ChapterPlan], S.Cast, float]:
    """Pass 1 for every chapter to be narrated, with one cast carried across chapters. Pure computation."""
    import time

    t0 = time.time()
    cast = S.Cast(known)
    plans: list[ChapterPlan] = []
    ch_s = settings.chunking
    first_person = None
    for out_idx, ci in enumerate(include, start=1):
        ch = res.chapters[ci]
        auto_title = ch.title == "Opening" or bool(re.fullmatch(r"Section \d+", ch.title))
        title = ch.title if (ch_s.speak_chapter_titles and ch.title and not auto_title) else None
        st = S.analyze_chapter(ch.readable(), title, cast, ns.attribution_threshold, ns.auto_character_tracking, None, first_person)
        plans.append(ChapterPlan(out_idx, ch, title, st, ch.words()))
    return plans, cast, time.time() - t0


def _span_rows(conn, struct: S.ChapterStructure, book_id: int, uid: str, chapter_id: int, out_idx: int, ids: dict[str, int], narrator_id: int) -> int:
    for sp in struct.spans:
        sid = ids.get(sp.speaker) if sp.speaker else None
        guess = ids.get(sp.candidates[0]) if sp.candidates and not sid else None
        cues = dict(sp.cues)
        if sp.is_tag:
            cues["is_tag"], cues["tag_speaker"] = True, sp.tag_speaker
        conn.execute(
            """INSERT INTO nar_spans(uid, book_id, chapter_id, para_idx, idx, start, "end", kind, speaker_id, guess_id, attribution, confidence,
                   needs_review, evidence, candidates_json, scene_idx, cues_json, user_locked) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"{uid}-c{out_idx:03d}-t{sp.idx:05d}", book_id, chapter_id, sp.para_idx, sp.idx, sp.start, sp.end, sp.kind,
             sid if sp.kind == "dialogue" else narrator_id, guess, sp.attribution, round(sp.confidence, 3), 1 if sp.needs_review else 0,
             sp.evidence, dumps(sp.candidates), sp.scene_idx, dumps(cues), 1 if sp.user_locked else 0))
    return len(struct.spans)


def persist_structure(conn, book_id: int, uid: str, chapter_id: int, out_idx: int, struct: S.ChapterStructure, ids: dict[str, int], narrator_id: int) -> None:
    scene_starts, last = [], None
    for sp in struct.spans:
        if sp.scene_idx != last:
            scene_starts.append(sp.start)
            last = sp.scene_idx
    meta = {"scene_starts": scene_starts, "perspective": struct.perspective, "warnings": struct.warnings, "scenes": struct.scenes,
            "tension": struct.tension, "tension_shifts": struct.tension_shifts}
    conn.execute("DELETE FROM chapter_text WHERE chapter_id=?", (chapter_id,))
    conn.execute("INSERT INTO chapter_text(chapter_id, book_id, text, sha256, meta_json) VALUES (?,?,?,?,?)",
                 (chapter_id, book_id, struct.text, sha256_bytes(struct.text.encode("utf-8")), dumps(meta)))
    conn.execute("DELETE FROM nar_spans WHERE chapter_id=?", (chapter_id,))
    _span_rows(conn, struct, book_id, uid, chapter_id, out_idx, ids, narrator_id)


def load_structure(conn, chapter_id: int) -> S.ChapterStructure:
    ct = row(conn, "SELECT * FROM chapter_text WHERE chapter_id=?", (chapter_id,))
    if not ct:
        raise _err("This chapter has no semantic structure (it was imported without semantic segmentation).", 409)
    names = {r["id"]: r["name"] for r in rows(conn, "SELECT id, name FROM characters WHERE book_id=?", (ct["book_id"],))}
    meta = loads(ct["meta_json"], {})
    spans = []
    for r in rows(conn, 'SELECT * FROM nar_spans WHERE chapter_id=? ORDER BY start', (chapter_id,)):
        cues = loads(r["cues_json"], {})
        sp = S.Span(r["para_idx"], r["idx"], r["start"], r["end"], r["kind"], ct["text"][r["start"]:r["end"]], r["scene_idx"],
                    speaker=names.get(r["speaker_id"]) if r["kind"] == "dialogue" or r["speaker_id"] else None, attribution=r["attribution"],
                    confidence=r["confidence"], needs_review=bool(r["needs_review"]), evidence=r["evidence"], candidates=loads(r["candidates_json"], []),
                    cues=cues, user_locked=bool(r["user_locked"]), is_tag=bool(cues.get("is_tag")), tag_speaker=cues.get("tag_speaker"))
        if r["kind"] == "dialogue" and not r["speaker_id"]:
            sp.speaker = None
        spans.append(sp)
    return S.ChapterStructure(ct["text"], [], spans, meta.get("scenes", 1), meta.get("perspective", "third"), meta.get("warnings", []),
                              meta.get("tension", []), meta.get("tension_shifts", []))


def voice_key_fn(conn, book_id: int):
    keys, ids = {}, {}
    for c in C.list_characters(conn, book_id):
        k = "narrator" if (c["kind"] == "narrator" or not c["voice_id"]) else f"v{c['voice_id']}"
        for nm in {c["name"], *c["aliases"]}:
            keys[nm], ids[nm] = k, c["id"]
    return (lambda sp: keys.get(sp, "narrator") if sp else "narrator"), ids


def _review_reason(struct: S.ChapterStructure, seg: G.Seg) -> str | None:
    for sp in struct.spans:
        if sp.kind == "dialogue" and sp.needs_review and sp.start < seg.end and sp.end > seg.start:
            cand = f" Candidates: {', '.join(sp.candidates[:4])}." if sp.candidates else ""
            return f"Speaker not established: {sp.evidence}.{cand}"[:500]
    return None


def _speech_text(text: str, settings: Settings) -> str:
    norm = " ".join(text.split())
    return (to_speech(norm) if settings.chunking.normalize_for_speech else norm) or norm


def resegment_chapter(ctx, conn, book: dict, chapter: dict, ns: NarrativeSettings, settings: Settings) -> dict:
    """Pass 2 for one chapter, reconciled with the segments that already exist."""
    struct = load_structure(conn, chapter["id"])
    voice_of, ids = voice_key_fn(conn, book["id"])
    narrator_id = C.ensure_narrator(conn, book["id"])
    cuts = {r["offset"] for r in rows(conn, "SELECT offset FROM seg_edits WHERE chapter_id=? AND kind='cut'", (chapter["id"],))}
    joins = {r["offset"] for r in rows(conn, "SELECT offset FROM seg_edits WHERE chapter_id=? AND kind='join'", (chapter["id"],))}
    segs = G.build(struct, max_chars=ns.max_segment_chars, min_chars=ns.min_segment_chars, sensitivity=ns.emotion_sensitivity,
                   voice_key_of=voice_of, cuts=cuts, joins=joins, tags_with_speaker=ns.tags_with_speaker)
    problems = G.check_coverage(struct, segs)
    if problems:
        raise RuntimeError("segmentation invariant violated: " + "; ".join(problems))
    old = rows(conn, "SELECT * FROM segments WHERE chapter_id=? ORDER BY idx", (chapter["id"],))
    by_range = {(o["src_start"], o["src_end"]): o for o in old if o["src_start"] is not None}
    used: set[int] = set()
    uid_book = book["uid"]
    seq = max([int(m.group(1)) for o in old if (m := re.search(r"-s(\d+)$", o["sid"]))] +
              [int(m.group(1)) for r in rows(conn, "SELECT sid FROM segment_history WHERE chapter_id=?", (chapter["id"],)) if (m := re.search(r"-s(\d+)$", r["sid"]))] + [0])
    kept = created = 0
    new_ids: list[int] = []
    for n, sg in enumerate(segs, start=1):
        cid = ids.get(sg.speaker) if sg.speaker else (None if sg.span_kind in ("dialogue", "mixed") else narrator_id)
        reason = _review_reason(struct, sg) if sg.needs_review else None
        o = by_range.get((sg.start, sg.end))
        if o is not None and o["id"] not in used:
            used.add(o["id"])
            conn.execute("""UPDATE segments SET idx=?, para_idx=?, kind=?, pause_kind=?, character_id=?, span_kind=?, scene_idx=?, needs_review=?, review_reason=?,
                                boundary_manual=? WHERE id=?""",
                         (n, sg.para_idx, sg.kind, sg.pause_kind, cid, sg.span_kind, sg.scene_idx, 1 if sg.needs_review else 0, reason, 1 if sg.manual else 0, o["id"]))
            kept += 1
            new_ids.append(o["id"])
            continue
        seq += 1
        sid = f"{uid_book}-c{chapter['idx']:03d}-s{seq:04d}"
        cur = conn.execute(
            """INSERT INTO segments(sid, book_id, chapter_id, idx, para_idx, kind, pause_kind, original_text, narration_text, status, updated_at,
                   src_start, src_end, character_id, span_kind, scene_idx, needs_review, review_reason, boundary_manual)
               VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?, ?,?,?,?,?,?,?,?)""",
            (sid, book["id"], chapter["id"], n, sg.para_idx, sg.kind, sg.pause_kind, sg.text, _speech_text(sg.text, settings), now(),
             sg.start, sg.end, cid, sg.span_kind, sg.scene_idx, 1 if sg.needs_review else 0, reason, 1 if sg.manual else 0))
        created += 1
        new_ids.append(cur.lastrowid)
    retired = 0
    for o in old:
        if o["id"] in used:
            continue
        conn.execute("INSERT INTO segment_history(book_id, chapter_id, sid, src_start, src_end, text, reason, retired_at) VALUES (?,?,?,?,?,?,?,?)",
                     (book["id"], chapter["id"], o["sid"], o["src_start"], o["src_end"], o["original_text"], "boundaries changed", now()))
        for tbl, col in (("attempts", "segment_id"), ("review_actions", "segment_id"), ("directions", "segment_id"), ("direction_log", "segment_id")):
            conn.execute(f"DELETE FROM {tbl} WHERE {col}=?", (o["id"],))
        conn.execute("DELETE FROM segments WHERE id=?", (o["id"],))
        retired += 1
    conn.execute("UPDATE chapters SET segment_count=? WHERE id=?", (len(segs), chapter["id"]))
    if created or retired:
        conn.execute("DELETE FROM chapter_renders WHERE chapter_id=?", (chapter["id"],))
        from .. import services as SV

        SV._revoke(conn, ctx, book["id"], "segment boundaries changed")
    return {"chapter": chapter["idx"], "segments": len(segs), "kept": kept, "created": created, "retired": retired, "ids": new_ids}


def resegment_book(ctx, book_id: int, chapter_ids: list[int] | None = None) -> dict:
    settings = ctx.settings()
    with ctx.db.tx() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        ns = book_ns(ctx, book)
        out = []
        for ch in rows(conn, "SELECT * FROM chapters WHERE book_id=? ORDER BY idx", (book_id,)):
            if chapter_ids and ch["id"] not in chapter_ids:
                continue
            if row(conn, "SELECT 1 FROM chapter_text WHERE chapter_id=?", (ch["id"],)):
                out.append(resegment_chapter(ctx, conn, book, ch, ns, settings))
        set_stage(conn, book_id, "segmentation", "done", {"segments": sum(o["segments"] for o in out), "created": sum(o["created"] for o in out),
                                                           "retired": sum(o["retired"] for o in out), "kept": sum(o["kept"] for o in out)})
        from ..worker import refresh_book_state

        refresh_book_state(conn, book_id)
    return {"chapters": out, "created": sum(o["created"] for o in out), "retired": sum(o["retired"] for o in out), "kept": sum(o["kept"] for o in out),
            "ids": [i for o in out for i in o["ids"]]}


def reparse_chapters(conn, book_id: int, include_from_manifest: bool = True):
    """Re-parse the immutable stored manuscript and pair its narrated chapters with the book's chapter rows."""
    from ..parsing import parse_manuscript

    m = row(conn, "SELECT * FROM manuscripts WHERE book_id=? ORDER BY id DESC LIMIT 1", (book_id,))
    if not m:
        raise _err("The book has no stored manuscript.", 409)
    from pathlib import Path

    data = Path(m["stored_path"]).read_bytes()
    res = parse_manuscript(m["original_name"], data)
    manifest = loads(m["parse_report_json"], {})
    narrated = [c["index"] for c in manifest.get("chapters", []) if c.get("narrated")]
    chapters = rows(conn, "SELECT * FROM chapters WHERE book_id=? ORDER BY idx", (book_id,))
    if len(narrated) != len(chapters):
        raise _err("The stored manuscript no longer matches the book's chapters.", 409)
    return res, list(zip(chapters, narrated))


def rebuild_structure(ctx, book_id: int) -> dict:
    """Pass 1 again over the stored manuscript, keeping characters, user-locked attributions and edits."""
    import time

    t0 = time.time()
    settings = ctx.settings()
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        ns = book_ns(ctx, book)
        res, pairs = reparse_chapters(conn, book_id)
        known = C.known_for_analysis(conn, book_id)
        locked = {r["chapter_id"]: {(x["start"], x["end"]): x["name"] for x in rows(
            conn, "SELECT s.start, s.\"end\" AS end, c.name FROM nar_spans s JOIN characters c ON c.id=s.speaker_id WHERE s.chapter_id=? AND s.user_locked=1", (r["chapter_id"],))}
            for r in rows(conn, "SELECT DISTINCT chapter_id FROM nar_spans WHERE book_id=? AND user_locked=1", (book_id,))}
    cast = S.Cast(known)
    plans = []
    ch_s = settings.chunking
    for chrow, ci in pairs:
        ch = res.chapters[ci]
        auto_title = ch.title == "Opening" or bool(re.fullmatch(r"Section \d+", ch.title))
        title = ch.title if (ch_s.speak_chapter_titles and ch.title and not auto_title) else None
        plans.append((chrow, S.analyze_chapter(ch.readable(), title, cast, ns.attribution_threshold, ns.auto_character_tracking, locked.get(chrow["id"]))))
    with ctx.db.tx() as conn:
        ids = C.sync_cast(conn, book_id, cast)
        narrator_id = C.ensure_narrator(conn, book_id)
        for chrow, st in plans:
            persist_structure(conn, book_id, book["uid"], chrow["id"], chrow["idx"], st, ids, narrator_id)
        flagged = scalar(conn, 'SELECT COUNT(*) FROM nar_spans WHERE book_id=? AND needs_review=1', (book_id,), 0)
        set_stage(conn, book_id, "structure", "done", {"chapters": len(plans), "seconds": round(time.time() - t0, 3), "spans": sum(len(p.spans) for _c, p in plans),
                                                        "characters": len([e for e in cast.entries.values() if e.kind != "narrator"]), "review": flagged})
        set_stage(conn, book_id, "characters", "done", {"characters": len(ids)})
    return {"seconds": round(time.time() - t0, 3), "review": flagged}


# ------------------------------------------------------------------- generation plan


@dataclass
class Plan:
    voice: VoiceSpec
    voice_id: int
    instruct: str | None
    delivery_applied: bool
    delivery_reason: str
    gen_key: str
    character_id: int | None
    direction: dict | None
    text: str
    voice_kind: str = "preset"
    pace: float = 1.0


def spoken_text(seg: dict) -> str:
    return (seg.get("override_text") or seg.get("narration_text") or seg.get("original_text") or "").strip()


def plan_for_segment(conn, ctx, seg: dict, book: dict) -> Plan:
    """Everything that determines how a segment sounds: voice, delivery instruction and the cache key."""
    ns = book_ns(ctx, book)
    voice_id = None
    vv = None
    if seg.get("voice_override_id"):
        v = row(conn, "SELECT * FROM voices WHERE id=?", (seg["voice_override_id"],))
        if v:
            vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (v["current_version_id"],))
            voice_id = v["id"]
    if vv is None and seg.get("character_id"):
        ch = row(conn, "SELECT voice_id, voice_version_id FROM characters WHERE id=? AND archived=0", (seg["character_id"],))
        if ch and ch["voice_id"]:
            v = row(conn, "SELECT * FROM voices WHERE id=? AND archived=0", (ch["voice_id"],))
            if v:
                vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (ch["voice_version_id"] or v["current_version_id"],))
                voice_id = v["id"]
    if vv is None:
        v = row(conn, "SELECT * FROM voices WHERE id=?", (book["voice_id"],))
        vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (book["voice_version_id"],))
        if not v or not vv:
            raise ValueError("book has no narrator voice assigned")
        voice_id = v["id"]
    spec = VoiceSpec.from_rows(v, vv)
    final = None
    info = {"applied": False, "reason": "delivery guidance is off for this book"}
    delivery = None
    if ns.director_enabled:
        d = row(conn, "SELECT final_json FROM directions WHERE segment_id=?", (seg["id"],))
        if d:
            final = loads(d["final_json"], None)
            delivery, info = DL.build_instruct(final, v["kind"], vv["model_id"], ns.delivery)
        else:
            info = {"applied": False, "reason": "no direction has been produced for this segment yet"}
    from ..pronunciation import apply_for_book

    text = apply_for_book(conn, book["id"], spoken_text(seg))  # dictionary respellings; the stored text is untouched
    pace = float(v.get("pace") or 1.0)
    instruct = " ".join(x for x in ((book.get("instructions") or "").strip(), delivery) if x) or None
    snap = loads(book["settings_json"], {}).get("models", {})
    key = short_hash(dumps({
        "text": " ".join(text.split()), "voice": [v["kind"], vv["model_id"], vv.get("speaker"), vv.get("instruct"), vv.get("ref_sha256"), vv.get("language")],
        "instruct": instruct, "engine": snap.get("engine"), "lang": snap.get("language"), "t": snap.get("temperature"), "p": snap.get("top_p"),
        "r": snap.get("repetition_penalty"), **({"pace": round(pace, 3)} if abs(pace - 1.0) > 1e-6 else {})}), 20)
    return Plan(spec, voice_id, instruct, bool(info["applied"]), info["reason"], key, seg.get("character_id"), final, text, v["kind"], pace)


def pick_segment(conn, book: dict, first: dict, warm: list[str], window: int = 150) -> dict:
    """Which pending segment to synthesise next. Output order does not depend on synthesis order (chapters
    are assembled by segment index), so when a book mixes voices that need different Qwen3-TTS models, prefer
    a segment whose model is already loaded instead of reloading a model (about 30 s) for every switch."""
    if not warm:
        return first
    vmodel = {r["id"]: r["model_id"] for r in rows(conn, "SELECT v.id, vv.model_id FROM voices v JOIN voice_versions vv ON vv.id=v.current_version_id")}
    chmodel = {r["id"]: r["model_id"] for r in rows(
        conn, """SELECT c.id, vv.model_id FROM characters c JOIN voice_versions vv ON vv.id=COALESCE(c.voice_version_id,
                 (SELECT current_version_id FROM voices WHERE id=c.voice_id)) WHERE c.book_id=? AND c.voice_id IS NOT NULL AND c.archived=0""", (book["id"],))}
    narr = row(conn, "SELECT model_id FROM voice_versions WHERE id=?", (book["voice_version_id"],))["model_id"]

    def model_of(s: dict) -> str:
        return vmodel.get(s.get("voice_override_id")) or chmodel.get(s.get("character_id")) or narr

    if model_of(first) in warm:
        return first
    for s in rows(conn, """SELECT s.* FROM segments s JOIN chapters c ON c.id=s.chapter_id WHERE s.book_id=? AND s.status IN ('pending','synthesizing')
                           ORDER BY c.idx, s.idx LIMIT ?""", (book["id"], window)):
        if model_of(s) in warm:
            return s
    return first


def invalidate_changed(conn, ctx, book_id: int, only_ids: list[int] | None = None, include_approved: bool = False) -> list[int]:
    """Segments whose rendered take no longer matches the plan (voice or delivery changed) go back to
    pending; unchanged ones are untouched. -> ids of segments reset."""
    book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
    reset = []
    sql = """SELECT s.*, a.gen_key AS sel_key FROM segments s JOIN attempts a ON a.id = s.selected_attempt_id
             WHERE s.book_id=? AND s.status IN ('passed','flagged'""" + (",'approved'" if include_approved else "") + ")"
    for s in rows(conn, sql, (book_id,)):
        if only_ids is not None and s["id"] not in only_ids:
            continue
        if not s["sel_key"]:
            continue
        if plan_for_segment(conn, ctx, s, book).gen_key != s["sel_key"]:
            conn.execute("UPDATE segments SET round=round+1, attempts_used=0, status='pending', selected_attempt_id=NULL, flag_reason=NULL, updated_at=? WHERE id=?",
                         (now(), s["id"]))
            conn.execute("DELETE FROM chapter_renders WHERE chapter_id=?", (s["chapter_id"],))
            conn.execute("INSERT INTO review_actions(segment_id, action, detail_json, actor, created_at) VALUES (?,?,?,?,?)",
                         (s["id"], "regenerate", dumps({"note": "voice or delivery changed"}), "system", now()))
            reset.append(s["id"])
    if reset:
        from .. import services as SV

        SV._revoke(conn, ctx, book_id, f"{len(reset)} segment(s) changed voice or delivery")
    return reset


# ------------------------------------------------------------------------ manual edits


def _seg_chapter(conn, segment_id: int):
    s = row(conn, "SELECT * FROM segments WHERE id=?", (segment_id,))
    if not s:
        raise _err("Segment not found", 404)
    if s["src_start"] is None:
        raise _err("This segment was not produced by semantic segmentation, so its boundaries cannot be edited.", 409)
    return s, row(conn, "SELECT * FROM chapters WHERE id=?", (s["chapter_id"],)), row(conn, "SELECT * FROM books WHERE id=?", (s["book_id"],))


def edit_boundary(ctx, segment_id: int, op: str, offset: int | None = None, actor: str = "operator") -> dict:
    """op = split (cut inside the segment at ``offset``), merge_next (join with the following segment),
    move_end (move the boundary after this segment to ``offset``)."""
    settings = ctx.settings()
    with ctx.db.tx() as conn:
        s, ch, book = _seg_chapter(conn, segment_id)
        if row(conn, "SELECT id FROM jobs WHERE book_id=? AND kind='book' AND status='running'", (book["id"],)):
            raise _err("Pause the running job before editing segment boundaries.", 409)
        ct = row(conn, "SELECT text FROM chapter_text WHERE chapter_id=?", (ch["id"],))["text"]

        def snap(pos: int, lo: int, hi: int) -> int:
            """Nearest word start strictly inside (lo, hi)."""
            pos = max(lo + 1, min(hi - 1, pos))

            def is_start(p: int) -> bool:
                return lo < p < hi and ct[p - 1].isspace() and not ct[p].isspace()

            for d in range(0, hi - lo):
                for p in (pos - d, pos + d):
                    if is_start(p):
                        return p
            return pos

        if op == "split" and s["kind"] == "heading":
            raise _err("A heading is spoken as one piece and cannot be split.", 409)
        if op == "split":
            if offset is None:
                raise _err("offset is required", 422)
            at = snap(int(offset), s["src_start"], s["src_end"])
            if not s["src_start"] < at < s["src_end"] or not ct[s["src_start"]:at].strip() or not ct[at:s["src_end"]].strip():
                raise _err("Choose a split point inside the segment text.", 422)
            conn.execute("INSERT OR IGNORE INTO seg_edits(book_id, chapter_id, kind, offset, actor, created_at) VALUES (?,?,?,?,?,?)", (book["id"], ch["id"], "cut", at, actor, now()))
            conn.execute("DELETE FROM seg_edits WHERE chapter_id=? AND kind='join' AND offset=?", (ch["id"], at))
        elif op in ("merge_next", "move_end"):
            nxt = row(conn, "SELECT * FROM segments WHERE chapter_id=? AND idx>? ORDER BY idx LIMIT 1", (ch["id"], s["idx"]))
            if not nxt:
                raise _err("There is no following segment in this chapter.", 409)
            boundary = nxt["src_start"]
            if op == "merge_next":
                if nxt["kind"] == "heading" or s["kind"] == "heading" or nxt["scene_idx"] != s["scene_idx"]:
                    raise _err("A heading or a scene break cannot be merged across.", 409)
                if nxt["src_end"] - s["src_start"] > 1200:
                    raise _err("The merged segment would exceed 1200 characters.", 422)
                conn.execute("DELETE FROM seg_edits WHERE chapter_id=? AND kind='cut' AND offset=?", (ch["id"], boundary))
                conn.execute("INSERT OR IGNORE INTO seg_edits(book_id, chapter_id, kind, offset, actor, created_at) VALUES (?,?,?,?,?,?)", (book["id"], ch["id"], "join", boundary, actor, now()))
            else:
                if offset is None:
                    raise _err("offset is required", 422)
                at = snap(int(offset), s["src_start"], nxt["src_end"])
                if not s["src_start"] < at < nxt["src_end"]:
                    raise _err("The new boundary must stay between the two segments' text.", 422)
                # a new cut at the chosen word, and a join across the old boundary so the text on either side re-attaches
                conn.execute("DELETE FROM seg_edits WHERE chapter_id=? AND offset IN (?, ?)", (ch["id"], boundary, at))
                conn.execute("INSERT INTO seg_edits(book_id, chapter_id, kind, offset, actor, created_at) VALUES (?,?,?,?,?,?)", (book["id"], ch["id"], "cut", at, actor, now()))
                if at != boundary:
                    conn.execute("INSERT INTO seg_edits(book_id, chapter_id, kind, offset, actor, created_at) VALUES (?,?,?,?,?,?)", (book["id"], ch["id"], "join", boundary, actor, now()))
        else:
            raise _err("op must be split, merge_next or move_end", 422)
        ctx.audit(conn, "segment", s["sid"], f"boundary_{op}", {"offset": offset}, actor)
        res = resegment_chapter(ctx, conn, book, ch, book_ns(ctx, book), settings)
        from ..worker import refresh_book_state

        refresh_book_state(conn, book["id"])
    return {k: v for k, v in res.items()} | {"book_id": book["id"]}


# --------------------------------------------------------------------------- inspection


def _nbr_direction(conn, seg: dict | None) -> dict | None:
    if not seg:
        return None
    d = row(conn, "SELECT final_json FROM directions WHERE segment_id=?", (seg["id"],))
    return {"sid": seg["sid"], "id": seg["id"], "text": seg["original_text"][:120], "final": loads(d["final_json"], None) if d else None,
            "speaker_id": seg["character_id"], "status": seg["status"]}


def inspect_segment(ctx, segment_id: int) -> dict:
    with ctx.db.session() as conn:
        s = row(conn, "SELECT * FROM segments WHERE id=?", (segment_id,))
        if not s:
            raise _err("Segment not found", 404)
        book = row(conn, "SELECT * FROM books WHERE id=?", (s["book_id"],))
        ns = book_ns(ctx, book)
        ch = row(conn, "SELECT id, idx, title FROM chapters WHERE id=?", (s["chapter_id"],))
        out: dict = {"segment_id": s["id"], "sid": s["sid"], "semantic": s["src_start"] is not None, "chapter": ch, "scene": s["scene_idx"],
                     "status": s["status"], "needs_review": bool(s["needs_review"]), "review_reason": s["review_reason"], "span_kind": s["span_kind"],
                     "boundary_manual": bool(s["boundary_manual"]), "src_start": s["src_start"], "src_end": s["src_end"],
                     "original_text": s["original_text"], "spoken_text": spoken_text(s), "override_text": s["override_text"]}
        if s["src_start"] is not None:
            ct = row(conn, "SELECT text, meta_json FROM chapter_text WHERE chapter_id=?", (s["chapter_id"],))
            meta = loads(ct["meta_json"], {})
            out["context"] = W.context_window(ct["text"], s["src_start"], s["src_end"], ns.context_before_chars, ns.context_after_chars, meta.get("scene_starts"))
            out["perspective"] = meta.get("perspective")
            out["spans"] = [{"id": r["id"], "start": r["start"], "end": r["end"], "kind": r["kind"], "speaker_id": r["speaker_id"], "attribution": r["attribution"],
                             "confidence": r["confidence"], "needs_review": bool(r["needs_review"]), "evidence": r["evidence"], "candidates": loads(r["candidates_json"], []),
                             "locked": bool(r["user_locked"]), "text": ct["text"][r["start"]:r["end"]]}
                            for r in rows(conn, 'SELECT * FROM nar_spans WHERE chapter_id=? AND "end">? AND start<? ORDER BY start', (s["chapter_id"], s["src_start"], s["src_end"]))]
        plan = plan_for_segment(conn, ctx, s, book)
        char = C.get_character(conn, s["character_id"]) if s["character_id"] else None
        v = row(conn, "SELECT id, name, kind FROM voices WHERE id=?", (plan.voice_id,))
        out["speaker"] = {"character_id": s["character_id"], "name": char["name"] if char else None, "kind": char["kind"] if char else None}
        out["voice"] = {"id": v["id"], "name": v["name"], "kind": v["kind"], "model_id": plan.voice.model_id, "override": bool(s["voice_override_id"])}
        d = row(conn, "SELECT * FROM directions WHERE segment_id=?", (segment_id,))
        out["direction"] = None if not d else {
            "source": d["source"], "raw": loads(d["raw_json"], {}), "smoothed": loads(d["smoothed_json"], {}), "user": loads(d["user_json"], {}),
            "final": loads(d["final_json"], {}), "smoothing_note": d["smoothing_note"], "needs_review": bool(d["needs_review"]), "stale": bool(d["stale"]),
            "updated_at": d["updated_at"]}
        out["delivery"] = {"instruct": plan.instruct, "applied": plan.delivery_applied, "reason": plan.delivery_reason, "gen_key": plan.gen_key,
                           "spoken_text_sent_to_tts": plan.text}
        prev = row(conn, "SELECT * FROM segments WHERE chapter_id=? AND idx<? ORDER BY idx DESC LIMIT 1", (s["chapter_id"], s["idx"]))
        nxt = row(conn, "SELECT * FROM segments WHERE chapter_id=? AND idx>? ORDER BY idx LIMIT 1", (s["chapter_id"], s["idx"]))
        out["previous"], out["next"] = _nbr_direction(conn, prev), _nbr_direction(conn, nxt)
        out["history"] = rows(conn, "SELECT source, payload_json, actor, created_at FROM direction_log WHERE segment_id=? ORDER BY id DESC LIMIT 20", (segment_id,))
        for h in out["history"]:
            h["payload"] = loads(h.pop("payload_json"), {})
        out["attempts"] = [{"id": a["id"], "round": a["round"], "attempt_no": a["attempt_no"], "status": a["status"], "passed": a["passed"], "wer": a["wer"],
                            "gen_key": a["gen_key"], "instruct_used": a["instruct_used"], "delivery_applied": a["delivery_applied"],
                            "asr_skipped": bool(a["asr_skipped"]), "current": a["gen_key"] == plan.gen_key, "selected": a["id"] == s["selected_attempt_id"],
                            "created_at": a["created_at"], "duration_s": a["duration_s"]}
                           for a in rows(conn, "SELECT * FROM attempts WHERE segment_id=? ORDER BY id DESC", (segment_id,))]
        out["settings"] = {"context_before_chars": ns.context_before_chars, "context_after_chars": ns.context_after_chars, "max_segment_chars": ns.max_segment_chars}
    return out


def set_segment_narrative(ctx, segment_id: int, patch: dict, actor: str = "operator") -> dict:
    """Manual overrides on one segment: speaker, voice profile, emotion/intensity/pacing/delivery.
    Overrides are kept across re-analysis. Only this segment is regenerated; downstream continuity is
    recomputed from stored directions without new LLM calls."""
    from . import analysis as A

    changed_dir = False
    with ctx.db.tx() as conn:
        s = row(conn, "SELECT * FROM segments WHERE id=?", (segment_id,))
        if not s:
            raise _err("Segment not found", 404)
        book = row(conn, "SELECT * FROM books WHERE id=?", (s["book_id"],))
        sets, details = {}, {}
        if "character_id" in patch:
            cid = patch["character_id"]
            if cid:
                ch = row(conn, "SELECT * FROM characters WHERE id=? AND book_id=? AND archived=0", (cid, s["book_id"]))
                if not ch:
                    raise _err("Character not found in this book", 404)
                if s["src_start"] is not None:  # lock the underlying dialogue spans to the confirmed speaker
                    conn.execute("""UPDATE nar_spans SET speaker_id=?, attribution='user', confidence=1, needs_review=0, user_locked=1, evidence='confirmed by the user'
                                    WHERE chapter_id=? AND kind='dialogue' AND start>=? AND "end"<=?""", (cid, s["chapter_id"], s["src_start"], s["src_end"]))
                sets.update({"character_id": cid, "needs_review": 0, "review_reason": None})
            else:
                sets.update({"character_id": None})
            details["character_id"] = cid
        if "voice_id" in patch:
            vid = patch["voice_id"] or None
            if vid and not row(conn, "SELECT id FROM voices WHERE id=? AND archived=0", (vid,)):
                raise _err("Voice not found", 404)
            sets["voice_override_id"] = vid
            details["voice_id"] = vid
        user = {}
        d = row(conn, "SELECT * FROM directions WHERE segment_id=?", (segment_id,))
        if d:
            user = loads(d["user_json"], {})
        for k in ("emotion", "intensity", "pacing", "delivery", "tension"):
            if k in patch:
                v = patch[k]
                if v in (None, ""):
                    user.pop(k, None)
                else:
                    from . import vocab as V

                    if k == "emotion" and v not in V.EMOTIONS:
                        raise _err(f"emotion must be one of: {', '.join(V.EMOTIONS)}", 422)
                    if k == "pacing" and v not in V.PACINGS:
                        raise _err(f"pacing must be one of: {', '.join(V.PACINGS)}", 422)
                    if k in ("intensity", "tension"):
                        v = V.clamp_intensity(int(v))
                    if k == "delivery":
                        v = " ".join(str(v).split())[:240]
                    user[k] = v
                details[k] = v
                changed_dir = True
        if sets:
            conn.execute(f"UPDATE segments SET {', '.join(k + '=?' for k in sets)}, updated_at=? WHERE id=?", (*sets.values(), now(), segment_id))
        if changed_dir:
            A.store_user_direction(conn, ctx, s, book, user, actor)
        ctx.audit(conn, "segment", s["sid"], "narrative_edit", details, actor)
    # a voice or speaker change can alter how neighbouring text is cut; re-reconcile the chapter
    if "character_id" in patch and s["src_start"] is not None:
        resegment_book(ctx, s["book_id"], [s["chapter_id"]])
    if changed_dir:
        A.recompute_from(ctx, s["book_id"], segment_id)
    with ctx.db.tx() as conn:
        reset = invalidate_changed(conn, ctx, s["book_id"], [segment_id], include_approved=True)
        seg_now = row(conn, "SELECT status FROM segments WHERE id=?", (segment_id,))
    if reset:
        from ..services import enqueue_book

        try:
            enqueue_book(ctx, s["book_id"], front=True)
        except Exception:
            pass
    return {"ok": True, "segment_id": segment_id, "regenerating": bool(reset), "status": seg_now["status"] if seg_now else None}


def update_character(ctx, character_id: int, patch: dict, actor: str = "operator") -> dict:
    with ctx.db.tx() as conn:
        c0 = C.get_character(conn, character_id)
        book_id = c0["book_id"]
        c, voice_changed = C.apply_patch(ctx, conn, character_id, patch, actor)
        affected = scalar(conn, "SELECT COUNT(*) FROM segments WHERE character_id=?", (character_id,), 0)
    renamed = ("name" in patch or "aliases" in patch)
    out = {"character": c, "voice_changed": voice_changed, "segments": affected, "reset": []}
    if voice_changed and affected:
        r = resegment_book(ctx, book_id)
        with ctx.db.tx() as conn:
            out["reset"] = invalidate_changed(conn, ctx, book_id)
        out["resegmented"] = {"created": r["created"], "retired": r["retired"], "kept": r["kept"]}
        if out["reset"]:
            from ..services import enqueue_book

            try:
                enqueue_book(ctx, book_id, front=True)
            except Exception:
                pass
    if renamed and not voice_changed:
        pass
    return out


def merge_characters(ctx, src_id: int, dst_id: int, actor: str = "operator") -> dict:
    with ctx.db.tx() as conn:
        book_id = C.get_character(conn, src_id)["book_id"]
        dst = C.merge_into(ctx, conn, src_id, dst_id, actor)
    r = resegment_book(ctx, book_id)
    with ctx.db.tx() as conn:
        reset = invalidate_changed(conn, ctx, book_id)
    return {"character": dst, "reset": reset, "resegmented": {"created": r["created"], "retired": r["retired"], "kept": r["kept"]}}


def accept_guess(ctx, span_or_segment_id: int, character_id: int, actor: str = "operator") -> dict:
    return set_segment_narrative(ctx, span_or_segment_id, {"character_id": character_id}, actor)


def narrative_status(ctx, book_id: int) -> dict:
    with ctx.db.session() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        if not b:
            raise _err("Book not found", 404)
        ns = book_ns(ctx, b)
        stages = {r["stage"]: {"status": r["status"], "started_at": r["started_at"], "finished_at": r["finished_at"], "error": r["error"],
                               "stats": loads(r["stats_json"], {})} for r in rows(conn, "SELECT * FROM analysis_stages WHERE book_id=?", (book_id,))}
        total = scalar(conn, "SELECT COUNT(*) FROM segments WHERE book_id=?", (book_id,), 0)
        semantic = scalar(conn, "SELECT COUNT(*) FROM segments WHERE book_id=? AND src_start IS NOT NULL", (book_id,), 0)
        directed = scalar(conn, "SELECT COUNT(*) FROM directions WHERE book_id=?", (book_id,), 0)
        stale = scalar(conn, "SELECT COUNT(*) FROM directions WHERE book_id=? AND stale=1", (book_id,), 0)
        review = scalar(conn, "SELECT COUNT(*) FROM segments WHERE book_id=? AND needs_review=1", (book_id,), 0)
        dreview = scalar(conn, "SELECT COUNT(*) FROM directions WHERE book_id=? AND needs_review=1", (book_id,), 0)
        job = row(conn, "SELECT id, status, error, result_json, updated_at FROM jobs WHERE book_id=? AND kind='analyze' ORDER BY id DESC LIMIT 1", (book_id,))
        cache = row(conn, "SELECT COUNT(*) n, COALESCE(SUM(hits),0) hits FROM director_cache")
        chars = scalar(conn, "SELECT COUNT(*) FROM characters WHERE book_id=? AND archived=0 AND kind='character'", (book_id,), 0)
    if job:
        job["result"] = loads(job.pop("result_json"), None)
    return {"semantic": semantic > 0, "segments": total, "semantic_segments": semantic, "directed": directed, "stale": stale,
            "needs_review": review, "direction_review": dreview, "characters": chars, "stages": stages, "job": job, "director_cache": cache,
            "settings": {"director_enabled": ns.director_enabled, "llm": ns.llm.provider, "model": ns.llm.model or None,
                         "semantic_segmentation": ns.semantic_segmentation}}
