"""HTTP API for semantic segmentation, the Narrative Director and the character directory.

Thin endpoints only: the logic lives in ``studio.narrative``. Inference (LLM, TTS) never runs in a
request; anything slow is queued as an ``analyze`` or ``preview`` job for the worker.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import Query
from pydantic import BaseModel, ConfigDict, Field

from .. import services as S
from ..db import row, rows
from ..narrative import ab as AB
from ..narrative import analysis as AN
from ..narrative import cast as C
from ..narrative import store as ST
from ..narrative import vocab as V
from ..util import dumps, loads, now


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalyzeReq(_Req):
    stages: Optional[list[str]] = Field(None, max_length=10)
    rebuild: bool = False
    confirm: bool = False


class OverridesReq(_Req):
    overrides: dict = Field(default_factory=dict)


class SegNarrativeReq(_Req):
    character_id: Optional[int] = None
    voice_id: Optional[int] = None
    emotion: Optional[str] = Field(None, max_length=20)
    intensity: Optional[int] = Field(None, ge=1, le=5)
    tension: Optional[int] = Field(None, ge=1, le=5)
    pacing: Optional[str] = Field(None, max_length=20)
    delivery: Optional[str] = Field(None, max_length=240)


class BoundaryReq(_Req):
    op: Literal["split", "merge_next", "move_end"]
    offset: Optional[int] = Field(None, ge=0)


class SegPreviewReq(_Req):
    seed: Optional[int] = Field(None, ge=0, le=2**31)


class ABCreateReq(_Req):
    comparison: str = Field(min_length=1, max_length=60)


class ABVoteReq(_Req):
    choice: Literal["x", "y", "same"]
    other_narrator: bool = False


class ApplyDeliveryReq(_Req):
    confirm: bool = False
    include_approved: bool = False


class CharCreateReq(_Req):
    name: str = Field(min_length=1, max_length=80)


class CharPatchReq(_Req):
    name: Optional[str] = Field(None, max_length=80)
    aliases: Optional[list[str]] = Field(None, max_length=30)
    gender: Optional[str] = Field(None, max_length=10)
    voice_id: Optional[int] = None
    voice_description: Optional[str] = Field(None, max_length=600)
    accent: Optional[str] = Field(None, max_length=200)
    speech_pattern: Optional[str] = Field(None, max_length=600)
    personality: Optional[str] = Field(None, max_length=600)
    description: Optional[str] = Field(None, max_length=600)
    delivery_pref: Optional[str] = Field(None, max_length=300)
    relationships: Optional[list[dict]] = Field(None, max_length=30)


class MergeReq(_Req):
    into_id: int


class NoteReq(_Req):
    text: str = Field(min_length=3, max_length=600)
    character_id: Optional[int] = None
    chapter_idx: Optional[int] = None


def register(app, ctx) -> None:
    @app.get("/api/narrative/vocabulary")
    def api_vocab():
        return {"emotions": list(V.EMOTIONS), "pacings": list(V.PACINGS), "intensity": [V.INTENSITY_MIN, V.INTENSITY_MAX]}

    @app.get("/api/books/{book_id}/narrative")
    def api_narrative(book_id: int):
        return ST.narrative_status(ctx, book_id)

    @app.get("/api/books/{book_id}/narrative/settings")
    def api_narrative_settings(book_id: int):
        return ST.get_book_settings(ctx, book_id)

    @app.put("/api/books/{book_id}/narrative/settings")
    def api_narrative_settings_put(book_id: int, req: OverridesReq):
        return ST.set_book_overrides(ctx, book_id, req.overrides)

    @app.post("/api/books/{book_id}/analyze")
    def api_analyze(book_id: int, req: AnalyzeReq):
        return AN.enqueue(ctx, book_id, req.stages, req.rebuild, req.confirm)

    @app.post("/api/books/{book_id}/resegment")
    def api_resegment(book_id: int):
        with ctx.db.session() as conn:
            if not row(conn, "SELECT 1 FROM chapter_text WHERE book_id=? LIMIT 1", (book_id,)):
                raise S.ServiceError("This book was not imported with semantic segmentation.", 409)
        r = ST.resegment_book(ctx, book_id)
        with ctx.db.tx() as conn:
            reset = ST.invalidate_changed(conn, ctx, book_id)
        return {**{k: v for k, v in r.items() if k != "ids"}, "audio_reset": len(reset)}

    @app.post("/api/books/{book_id}/continuity/recompute")
    def api_recompute(book_id: int):
        with ctx.db.session() as conn:
            first = row(conn, """SELECT d.segment_id FROM directions d JOIN segments s ON s.id=d.segment_id JOIN chapters c ON c.id=s.chapter_id
                                 WHERE d.book_id=? ORDER BY c.idx, s.idx LIMIT 1""", (book_id,))
        if not first:
            raise S.ServiceError("No directions exist yet; run the analysis first.", 409)
        return AN.recompute_from(ctx, book_id, first["segment_id"])

    @app.get("/api/books/{book_id}/narrative/metadata")
    def api_narrative_metadata(book_id: int):
        return narrative_metadata(ctx, book_id)

    # --------------------------------------------------------------- characters
    @app.get("/api/books/{book_id}/characters")
    def api_characters(book_id: int):
        with ctx.db.session() as conn:
            if not row(conn, "SELECT 1 FROM books WHERE id=?", (book_id,)):
                raise S.ServiceError("Book not found", 404)
            chars = C.list_characters(conn, book_id)
            voices = rows(conn, "SELECT id, name, kind FROM voices WHERE archived=0 ORDER BY name")
        return {"characters": chars, "voices": voices}

    @app.post("/api/books/{book_id}/characters")
    def api_character_create(book_id: int, req: CharCreateReq):
        return C.create_character(ctx, book_id, req.name)

    @app.patch("/api/characters/{character_id}")
    def api_character_patch(character_id: int, req: CharPatchReq):
        return ST.update_character(ctx, character_id, req.model_dump(exclude_unset=True))

    @app.post("/api/characters/{character_id}/merge")
    def api_character_merge(character_id: int, req: MergeReq):
        return ST.merge_characters(ctx, character_id, req.into_id)

    @app.get("/api/books/{book_id}/notes")
    def api_notes(book_id: int, kind: Optional[str] = None):
        with ctx.db.session() as conn:
            q = "SELECT * FROM story_notes WHERE book_id=?" + (" AND kind=?" if kind else "") + " ORDER BY chapter_idx, scene_idx, id"
            out = rows(conn, q, (book_id, kind) if kind else (book_id,))
        for n in out:
            n["refs"] = loads(n.pop("refs_json"), [])
        return {"notes": out}

    @app.post("/api/books/{book_id}/notes")
    def api_note_add(book_id: int, req: NoteReq):
        with ctx.db.tx() as conn:
            if not row(conn, "SELECT 1 FROM books WHERE id=?", (book_id,)):
                raise S.ServiceError("Book not found", 404)
            nid = conn.execute("""INSERT INTO story_notes(book_id, chapter_idx, kind, character_id, text, source, confidence, confirmed, created_at)
                                  VALUES (?,?,?,?,?,?,?,?,?)""", (book_id, req.chapter_idx, "fact", req.character_id, " ".join(req.text.split()), "user", 1.0, 1, now())).lastrowid
            ctx.audit(conn, "story_note", nid, "create", {"text": req.text})
        return {"id": nid}

    @app.delete("/api/notes/{note_id}")
    def api_note_delete(note_id: int):
        with ctx.db.tx() as conn:
            if not conn.execute("DELETE FROM story_notes WHERE id=?", (note_id,)).rowcount:
                raise S.ServiceError("Note not found", 404)
            ctx.audit(conn, "story_note", note_id, "delete")
        return {"ok": True}

    # ----------------------------------------------------------------- segments
    @app.get("/api/segments/{segment_id}/inspect")
    def api_inspect(segment_id: int):
        return ST.inspect_segment(ctx, segment_id)

    @app.get("/api/segments/{segment_id}/delivery-preview")
    def api_delivery_preview(segment_id: int, emotion: Optional[str] = Query(None, max_length=20), intensity: Optional[str] = Query(None, max_length=2),
                             pacing: Optional[str] = Query(None, max_length=20), tension: Optional[str] = Query(None, max_length=2),
                             delivery: Optional[str] = Query(None, max_length=240)):
        """What would reach the voice for these inputs, without saving anything. A parameter that is absent keeps the stored value; an empty one clears it."""
        given = {k: v for k, v in (("emotion", emotion), ("intensity", intensity), ("pacing", pacing), ("tension", tension), ("delivery", delivery)) if v is not None}
        return ST.preview_delivery(ctx, segment_id, given or None)

    @app.patch("/api/segments/{segment_id}/narrative")
    def api_seg_narrative(segment_id: int, req: SegNarrativeReq):
        return ST.set_segment_narrative(ctx, segment_id, req.model_dump(exclude_unset=True))

    @app.post("/api/segments/{segment_id}/boundary")
    def api_seg_boundary(segment_id: int, req: BoundaryReq):
        res = ST.edit_boundary(ctx, segment_id, req.op, req.offset)
        out = {k: v for k, v in res.items() if k != "ids"}
        if res["created"] or res["retired"]:
            out["recompute"] = AN.recompute_from(ctx, res["book_id"], res["ids"][0]) if res["ids"] and _has_direction(ctx, res["book_id"]) else None
        return out

    @app.post("/api/segments/{segment_id}/preview")
    def api_seg_preview(segment_id: int, req: SegPreviewReq):
        return S.preview_segment(ctx, segment_id, req.seed)

    # ------------------------------------------------- blind A/B listening
    @app.post("/api/segments/{segment_id}/ab")
    def api_ab_create(segment_id: int, req: ABCreateReq):
        return S.enqueue_ab(ctx, segment_id, req.comparison)

    @app.get("/api/ab/{round_id}")
    def api_ab_round(round_id: str):
        return S.get_ab_round(ctx, round_id)

    @app.post("/api/ab/{round_id}/vote")
    def api_ab_vote(round_id: str, req: ABVoteReq):
        return S.vote_ab(ctx, round_id, req.choice, req.other_narrator)

    @app.get("/api/books/{book_id}/ab/random")
    def api_ab_random(book_id: int, comparison: str, exclude: str = ""):
        ids = [int(x) for x in exclude.split(",") if x.strip().isdigit()][:500]
        return S.random_differing_segment(ctx, book_id, comparison, ids)

    @app.get("/api/books/{book_id}/ab")
    def api_ab_tally(book_id: int):
        return {"comparisons": AB.catalog(), "tally": S.ab_tally(ctx, book_id)}

    @app.post("/api/books/{book_id}/narrative/apply-delivery")
    def api_apply_delivery(book_id: int, req: ApplyDeliveryReq):
        return S.apply_delivery_to_takes(ctx, book_id, req.confirm, req.include_approved)

    @app.post("/api/segments/{segment_id}/redirect")
    def api_seg_redirect(segment_id: int):
        with ctx.db.tx() as conn:
            s = row(conn, "SELECT id, book_id FROM segments WHERE id=?", (segment_id,))
            if not s:
                raise S.ServiceError("Segment not found", 404)
            conn.execute("UPDATE directions SET stale=1 WHERE segment_id=?", (segment_id,))
        return AN.enqueue(ctx, s["book_id"], ["director"])


def _has_direction(ctx, book_id: int) -> bool:
    with ctx.db.session() as conn:
        return bool(row(conn, "SELECT 1 FROM directions WHERE book_id=? LIMIT 1", (book_id,)))


def narrative_metadata(ctx, book_id: int) -> dict:
    """Machine-readable record of how every segment was cut, voiced and directed (for export/audit)."""
    with ctx.db.session() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        if not b:
            raise S.ServiceError("Book not found", 404)
        chars = {c["id"]: c for c in C.list_characters(conn, book_id, include_archived=True)}
        segs = rows(conn, """SELECT s.*, c.idx AS ch_idx, a.duration_s, a.gen_key AS take_key, a.instruct_used, a.wer
                             FROM segments s JOIN chapters c ON c.id=s.chapter_id LEFT JOIN attempts a ON a.id=s.selected_attempt_id
                             WHERE s.book_id=? ORDER BY c.idx, s.idx""", (book_id,))
        dirs = {d["segment_id"]: loads(d["final_json"], {}) for d in rows(conn, "SELECT segment_id, final_json FROM directions WHERE book_id=?", (book_id,))}
    return {
        "book": {"id": b["id"], "title": b["title"], "uid": b["uid"]}, "generated_at": now(),
        "characters": [{"id": c["id"], "name": c["name"], "kind": c["kind"], "aliases": c["aliases"], "voice": c["voice"], "gender": c["gender"]} for c in chars.values() if not c["archived"]],
        "segments": [{"sid": s["sid"], "chapter": s["ch_idx"], "scene": s["scene_idx"], "src_start": s["src_start"], "src_end": s["src_end"], "kind": s["span_kind"],
                      "speaker": chars[s["character_id"]]["name"] if s["character_id"] in chars else None, "needs_review": bool(s["needs_review"]),
                      "status": s["status"], "duration_s": s["duration_s"], "wer": s["wer"], "direction": dirs.get(s["id"]), "instruct": s["instruct_used"]} for s in segs],
    }
