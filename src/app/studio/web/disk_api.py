"""HTTP API for disk management (roadmap C3). Every route here is administrator-only (auth.ADMIN_RULES).
Destructive routes need ``"confirm": true`` in the body; without it they answer 409 with a preview of what would be removed."""
from __future__ import annotations

from typing import Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from .. import diskmgmt as D


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfirmReq(_Req):
    confirm: StrictBool = False


class PurgeReq(_Req):
    scope: Literal["takes", "exports", "masters"]
    confirm: StrictBool = False


class NameReq(_Req):
    name: str = Field(min_length=5, max_length=160)


class NameConfirmReq(NameReq):
    confirm: StrictBool = False


def register(app, ctx) -> None:
    def actor(request: Request) -> str:
        return (getattr(request.state, "user", None) or {}).get("name", "operator")

    @app.get("/api/disk/usage")
    def api_disk_usage(refresh: int = 0):
        # sync route (runs in the threadpool): walking the library is slow. Served from a 60 s cache unless ?refresh=1; the body carries
        # measured_at / age_s / cached. Disk-changing actions drop the cache themselves.
        return D.usage(ctx, max_age=D.USAGE_TTL_S, refresh=bool(refresh))

    @app.get("/api/disk/backups")
    def api_disk_backups():
        return D.list_backups(ctx)

    @app.post("/api/disk/books/{book_id}/delete")
    def api_disk_delete_book(book_id: int, req: ConfirmReq, request: Request):
        return D.delete_book(ctx, book_id, req.confirm, actor(request))

    @app.post("/api/disk/books/{book_id}/delete-all-versions")
    def api_disk_delete_work(book_id: int, req: ConfirmReq, request: Request):
        return D.delete_work(ctx, book_id, req.confirm, actor(request))

    @app.post("/api/disk/books/{book_id}/purge")
    def api_disk_purge(book_id: int, req: PurgeReq, request: Request):
        return D.purge(ctx, book_id, req.scope, req.confirm, actor(request))

    @app.post("/api/disk/books/{book_id}/backup")
    def api_disk_backup_book(book_id: int, request: Request):
        return D.backup_book(ctx, book_id, actor(request))

    @app.post("/api/disk/restore")
    def api_disk_restore(req: NameReq, request: Request):
        return D.restore_book(ctx, req.name, actor(request))

    @app.post("/api/disk/backups/delete")
    def api_disk_backup_delete(req: NameConfirmReq, request: Request):
        return D.delete_backup(ctx, req.name, req.confirm, actor(request))

    @app.post("/api/disk/db-backup")
    def api_disk_db_backup(request: Request):
        return D.backup_database(ctx, actor(request))
