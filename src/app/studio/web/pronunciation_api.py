"""HTTP API for the pronunciation dictionary and per-voice pace (roadmap C2). Logic: ``studio.pronunciation``."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .. import pronunciation as PR


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EntryReq(_Req):
    written: str = Field(min_length=1, max_length=80)
    spoken: str = Field(min_length=1, max_length=200)
    book_id: Optional[int] = None
    case_sensitive: bool = False
    whole_word: bool = True
    note: str = Field("", max_length=200)
    confirm_invalidate: bool = False


class EntryPatchReq(_Req):
    written: Optional[str] = Field(None, min_length=1, max_length=80)
    spoken: Optional[str] = Field(None, min_length=1, max_length=200)
    case_sensitive: Optional[bool] = None
    whole_word: Optional[bool] = None
    enabled: Optional[bool] = None
    note: Optional[str] = Field(None, max_length=200)
    confirm_invalidate: bool = False


class TryReq(_Req):
    text: str = Field(min_length=1, max_length=2000)
    book_id: Optional[int] = None


class PaceReq(_Req):
    pace: float = Field(ge=PR.PACE_MIN, le=PR.PACE_MAX)
    confirm_invalidate: bool = False


def register(app, ctx) -> None:
    @app.get("/api/pronunciations")
    def api_pron_list(book_id: Optional[int] = None):
        return {**PR.list_entries(ctx, book_id), "pace_range": [PR.PACE_MIN, PR.PACE_MAX]}

    @app.post("/api/pronunciations")
    def api_pron_create(req: EntryReq):
        return PR.create_entry(ctx, req.written, req.spoken, req.book_id, req.case_sensitive, req.whole_word, req.note, req.confirm_invalidate)

    @app.patch("/api/pronunciations/{entry_id}")
    def api_pron_patch(entry_id: int, req: EntryPatchReq):
        fields = {k: v for k, v in req.model_dump().items() if v is not None and k != "confirm_invalidate"}
        return PR.update_entry(ctx, entry_id, fields, req.confirm_invalidate)

    @app.delete("/api/pronunciations/{entry_id}")
    def api_pron_delete(entry_id: int, confirm_invalidate: bool = False):
        return PR.delete_entry(ctx, entry_id, confirm_invalidate)

    @app.post("/api/pronunciations/try")
    def api_pron_try(req: TryReq):
        return PR.preview(ctx, req.text, req.book_id)

    @app.put("/api/voices/{voice_id}/pace")
    def api_voice_pace(voice_id: int, req: PaceReq):
        return PR.set_voice_pace(ctx, voice_id, req.pace, req.confirm_invalidate)
