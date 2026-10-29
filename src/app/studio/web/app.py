"""FastAPI application: JSON API, SSE progress stream, range-capable media and the SPA."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

from fastapi import FastAPI, File, Form, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .. import chapters as CH
from .. import APP_NAME, COMPANY, COPYRIGHT, DEVELOPER, VERSION_LABEL, __version__, services as S
from ..context import Context, setup_logging
from ..db import row
from ..doctor import MODEL_LICENSES, run_checks
from ..pipeline import book_dirs, resolve_take
from ..reports import QA_LIMITATIONS, write_report
from ..storage import PathRejected, browse, choose_folder_native, make_folder, move_book, storage_overview, validate_dir
from ..util import dumps, loads, title_filename
from . import security
from .auth import COOKIE, ROLES, SESSION_TTL
from .media import ensure_inside, file_response

log = logging.getLogger("studio.web")
STATIC = Path(__file__).resolve().parent / "static"


# ----------------------------------------------------------------- request models


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfirmImportReq(_Req):
    title: str = Field(min_length=1, max_length=300)
    author: str = Field("", max_length=300)
    narrator_credit: str = Field("", max_length=200)
    publisher: str = Field("", max_length=200)
    year: str = Field("", max_length=20)
    description: str = Field("", max_length=4000)
    genre: str = Field("Audiobook", max_length=60)
    language: str = Field("English", max_length=40)
    voice_id: int
    instructions: str = Field("", max_length=1000)
    include: Optional[list[int]] = Field(None, max_length=5000)
    queue: bool = False


class BookPatchReq(_Req):
    title: Optional[str] = Field(None, min_length=1, max_length=300)
    author: Optional[str] = Field(None, max_length=300)
    narrator_credit: Optional[str] = Field(None, max_length=200)
    publisher: Optional[str] = Field(None, max_length=200)
    year: Optional[str] = Field(None, max_length=20)
    description: Optional[str] = Field(None, max_length=4000)
    genre: Optional[str] = Field(None, max_length=60)
    language: Optional[str] = Field(None, max_length=40)


class NarratorReq(_Req):
    voice_id: int
    instructions: Optional[str] = Field(None, max_length=1000)
    confirm_invalidate: bool = False


class ConfirmReq(_Req):
    confirm_invalidate: bool = False


class EnqueueReq(_Req):
    book_id: int
    front: bool = False


class ReorderReq(_Req):
    job_ids: list[int] = Field(max_length=100000)


class MoveJobReq(_Req):
    direction: Literal["up", "down", "top", "bottom"]


class PresetVoiceReq(_Req):
    name: str = Field(min_length=1, max_length=80)
    speaker: str = Field(min_length=1, max_length=40)
    instruct: str = Field("", max_length=1000)
    language: Optional[str] = Field(None, max_length=40)


class DesignVoiceReq(_Req):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=10, max_length=1000)
    language: Optional[str] = Field(None, max_length=40)


class VoicePatchReq(_Req):
    name: Optional[str] = Field(None, max_length=80)
    instruct: Optional[str] = Field(None, max_length=1000)
    speaker: Optional[str] = Field(None, max_length=40)
    description: Optional[str] = Field(None, max_length=300)


class PreviewReq(_Req):
    text: str = Field(min_length=3, max_length=600)
    instruct: Optional[str] = Field(None, max_length=1000)
    verify: bool = False
    seed: Optional[int] = Field(None, ge=0, le=2**31)


class PromoteReq(_Req):
    job_id: int
    name: str = Field(min_length=1, max_length=80)


class NoteReq(_Req):
    note: Optional[str] = Field(None, max_length=2000)


class OverrideReq(_Req):
    text: Optional[str] = Field(None, max_length=2000)
    regenerate: bool = True


class SelectTakeReq(_Req):
    attempt_id: int


class ChapterTitleReq(_Req):
    title: Optional[str] = Field(None, max_length=300)


class ExportReq(_Req):
    kind: Literal["m4b", "chapter_m4b", "mp3_zip"] = "m4b"
    chapter_id: Optional[int] = None


class ReleaseReq(_Req):
    approved_by: str = Field(min_length=2, max_length=120)
    listen_scope: Literal["full", "partial"]
    attest: bool
    notes: str = Field("", max_length=2000)


class RevokeReq(_Req):
    reason: str = Field("", max_length=500)


class PathReq(_Req):
    path: str = Field(max_length=4096)


class MkdirReq(_Req):
    parent: str = Field(max_length=4096)
    name: str = Field(max_length=120)


class MoveBookReq(_Req):
    new_root: str = Field(max_length=4096)


class SecretReq(_Req):
    value: str  # length and characters are checked by the service so a rejected key is never echoed back


class LoginReq(_Req):
    username: str = Field(max_length=80)
    password: str = Field(max_length=200)


class PasswordReq(_Req):
    current: str = Field("", max_length=200)   # may be empty only while a temporary password is in force
    new: str = Field(min_length=10, max_length=200)
    confirm: str = Field(max_length=200)


class UserReq(_Req):
    username: str = Field(max_length=40)
    password: str = Field(min_length=10, max_length=200)
    confirm: str = Field(max_length=200)
    role: str = "operator"


class RecordingTextReq(_Req):
    ref_text: str = Field(min_length=5, max_length=1000)


class RecordingJobReq(_Req):
    mode: Literal["check", "audition"]
    text: Optional[str] = Field(None, max_length=600)


class FromRecordingReq(_Req):
    recording_id: str = Field(max_length=40)
    name: str = Field(min_length=1, max_length=80)
    rights_holder: str = Field(max_length=120)
    owner_attest: bool = False
    statement: str = Field("", max_length=1000)


class ArchiveReq(_Req):
    archived: bool = True


# -------------------------------------------------------------- worker supervisor


class WorkerSupervisor:
    """Runs the GPU worker as a separate process (inference never blocks HTTP) and restarts
    it with exponential backoff if it crashes."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.proc: subprocess.Popen | None = None
        self.stop = threading.Event()
        self.restarts = 0
        self.last_exit: int | None = None
        self.spawned_at = 0.0
        self.thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def _other_worker_alive(self) -> bool:
        with self.ctx.db.session() as conn:
            w = row(conn, "SELECT * FROM worker_state WHERE id=1")
        own_pid = self.proc.pid if self.proc and self.proc.poll() is None else None
        from ..worker import _pid_alive

        return bool(w and w["owner"] and w["heartbeat_at"] and time.time() - w["heartbeat_at"] < 30 and w["pid"] != own_pid
                    and _pid_alive(w["pid"]))

    def _spawn(self) -> None:
        logf = open(self.ctx.logs_dir / "worker.out.log", "ab")
        env = dict(os.environ)
        env["AUDIOBOOK_STUDIO_HOME"] = str(self.ctx.home)
        env["STUDIO_SUPERVISED_WORKER"] = "1"  # the worker exits (after its current segment) if this server disappears
        kw: dict[str, Any] = {}
        if os.name == "posix":
            kw["start_new_session"] = True
        self.spawned_at = time.time()
        self.proc = subprocess.Popen([sys.executable, "-m", "studio", "worker"], cwd=str(self.ctx.app_dir), env=env,
                                     stdout=logf, stderr=subprocess.STDOUT, **kw)
        log.info("worker process started pid=%s", self.proc.pid)

    def _loop(self) -> None:
        backoff = 2.0
        while not self.stop.is_set():
            with self._lock:
                running = self.proc is not None and self.proc.poll() is None
                if not running:
                    if self.proc is not None:
                        self.last_exit = self.proc.returncode
                        log.warning("worker exited with %s; restarting in %.0fs", self.last_exit, backoff)
                        self.proc = None
                        if self.stop.wait(backoff):
                            break
                        backoff = min(60.0, backoff * 2)
                        self.restarts += 1
                    if not self._other_worker_alive():
                        self._spawn()
                elif time.time() - self.spawned_at > 120:
                    backoff = 2.0  # healthy for a while: reset the crash backoff
            self.stop.wait(3.0)

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, daemon=True, name="worker-supervisor")
        self.thread.start()

    def restart(self) -> None:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                self._terminate(self.proc)
            self.proc = None

    @staticmethod
    def _terminate(p: subprocess.Popen, grace: float = 25.0) -> None:
        try:
            p.send_signal(signal.SIGTERM)
            p.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            p.kill()
        except Exception:
            pass

    def shutdown(self) -> None:
        self.stop.set()
        with self._lock:
            if self.proc and self.proc.poll() is None:
                self._terminate(self.proc)

    def status(self) -> dict:
        return {"managed": True, "pid": self.proc.pid if self.proc and self.proc.poll() is None else None,
                "restarts": self.restarts, "last_exit": self.last_exit}


# ------------------------------------------------------------------------- app


def create_app(ctx: Context | None = None, start_worker: bool | None = None, update_manager=None, auto_update_check: bool | None = None) -> FastAPI:
    ctx = ctx or Context()
    setup_logging(ctx, "server")
    S.ensure_builtin_voices(ctx)
    if start_worker is None:
        start_worker = ctx.settings().worker.autostart and os.environ.get("STUDIO_NO_WORKER") != "1"
    supervisor = WorkerSupervisor(ctx) if start_worker else None
    if auto_update_check is None:
        auto_update_check = start_worker and os.environ.get("STUDIO_NO_UPDATE_CHECK") != "1"
    stop_checks = threading.Event()

    def _auto_check_loop():
        """Checks for (never downloads or installs) updates on the configured interval. Failures are recorded, not raised."""
        stop_checks.wait(45)
        while not stop_checks.is_set():
            mgr = app.state.update_manager
            try:
                if mgr.layout.managed and mgr.auto_check_due():
                    mgr.check()
            except Exception:
                log.exception("automatic update check failed")
            stop_checks.wait(1800)

    @asynccontextmanager
    async def lifespan(_app):
        if supervisor:
            supervisor.start()
        if auto_update_check:
            threading.Thread(target=_auto_check_loop, daemon=True, name="update-check").start()
        yield
        stop_checks.set()
        if supervisor:
            await run_in_threadpool(supervisor.shutdown)

    app = FastAPI(title="Audiobook Studio", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.state.ctx = ctx
    app.state.supervisor = supervisor
    security.install(app, ctx)

    @app.exception_handler(S.ServiceError)
    async def _svc_err(_req, exc: S.ServiceError):
        return JSONResponse({"error": str(exc), "details": exc.details}, status_code=exc.status)

    @app.exception_handler(PathRejected)
    async def _path_err(_req, exc: PathRejected):
        return JSONResponse({"error": str(exc)}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def _val_err(_req, exc: RequestValidationError):
        msgs = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e.get("loc", []) if x != "body")
            msgs.append(f"{loc}: {e.get('msg')}" if loc else e.get("msg"))
        return JSONResponse({"error": "; ".join(msgs) or "Invalid request", "details": exc.errors()}, status_code=422)

    async def read_upload(f: UploadFile, limit: int) -> bytes:
        chunks, total = [], 0
        while True:
            data = await f.read(1024 * 1024)
            if not data:
                break
            total += len(data)
            if total > limit:
                raise S.ServiceError(f"Upload exceeds the {limit // (1024 * 1024)} MB limit.", 413)
            chunks.append(data)
        return b"".join(chunks)

    # ------------------------------------------------------------------- auth
    def _client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _session(resp, user, request):
        resp.set_cookie(COOKIE, app.state.auth.make_token(user), max_age=SESSION_TTL, httponly=True, samesite="strict", path="/",
                        secure=request.url.scheme == "https")
        return resp

    def _cert_fp():
        if not getattr(app.state, "https_active", False):
            return None
        try:
            from ..tls import fingerprint_sha256
            return fingerprint_sha256(ctx.config_dir / "tls" / "studio-certificate.pem")
        except Exception:
            return None

    @app.get("/tls/studio-certificate.pem", include_in_schema=False)
    def tls_certificate():
        p = ctx.config_dir / "tls" / "studio-certificate.pem"
        if not p.exists():
            raise S.ServiceError("HTTPS is not enabled on this studio.", 404)
        return FileResponse(p, media_type="application/x-pem-file", filename="audiobook-studio-certificate.pem")

    @app.get("/api/auth/status")
    def api_auth_status(request: Request):
        return {"auth_required": app.state.auth.enabled(), "user": request.state.user, "engine": ctx.engine_mode,
                "version": __version__, "version_label": VERSION_LABEL,
                "https_port": app.state.https_port if getattr(app.state, "https_active", False) else None,
                "secure": request.url.scheme == "https", "cert_sha256": _cert_fp()}

    @app.post("/api/auth/login")
    def api_login(req: LoginReq, request: Request):
        a, lim = app.state.auth, app.state.limiter
        key = _client_key(request)
        wait = lim.blocked(key)
        if wait:
            return JSONResponse({"error": f"Too many failed sign-ins. Try again in {wait // 60 + 1} min."}, status_code=429)
        user = a.verify(req.username.strip(), req.password)
        if not user:
            lim.fail(key)
            with ctx.db.tx() as conn:
                ctx.audit(conn, "auth", req.username.strip()[:40], "login_failed", {"ip": key}, "anonymous")
            return JSONResponse({"error": "Wrong user name or password."}, status_code=401)
        lim.ok(key)
        with ctx.db.tx() as conn:
            ctx.audit(conn, "auth", user["name"], "login", {"ip": key}, user["name"])
        return _session(JSONResponse({"ok": True, "user": user}), user, request)

    @app.post("/api/auth/logout")
    def api_logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE, path="/")
        return resp

    @app.post("/api/auth/password")
    def api_password(req: PasswordReq, request: Request):
        u = request.state.user
        if not u:
            raise S.ServiceError("Sign in required.", 401)
        if req.new != req.confirm:
            raise S.ServiceError("The new password and its confirmation do not match. Nothing was changed.", 422)
        a = app.state.auth
        if not u.get("must_change") and not a.verify(u["name"], req.current):
            raise S.ServiceError("Current password is wrong.", 403)
        try:
            a.set_user(u["name"], req.new, u["role"])
            u = {"name": u["name"], "role": u["role"]}
        except ValueError as exc:
            raise S.ServiceError(str(exc), 422)
        with ctx.db.tx() as conn:
            ctx.audit(conn, "auth", u["name"], "password_changed", {}, u["name"])
        return _session(JSONResponse({"ok": True}), u, request)

    @app.get("/api/auth/users")
    def api_users():
        return {"users": app.state.auth.list_users()}

    @app.post("/api/auth/users")
    def api_user_add(req: UserReq, request: Request):
        if req.role not in ROLES:
            raise S.ServiceError("role must be admin or operator", 422)
        if req.password != req.confirm:
            raise S.ServiceError("The password and its confirmation do not match.", 422)
        try:
            app.state.auth.set_user(req.username, req.password, req.role)
        except ValueError as exc:
            raise S.ServiceError(str(exc), 422)
        with ctx.db.tx() as conn:
            ctx.audit(conn, "auth", req.username, "user_saved", {"role": req.role}, (request.state.user or {}).get("name", "operator"))
        return {"users": app.state.auth.list_users()}

    # ------------------------------------------------------------------ pages
    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    # --------------------------------------------------------------- overview
    @app.get("/api/overview")
    def api_overview():
        return S.overview(ctx)

    @app.get("/api/events")
    async def api_events(request: Request, once: int = 0):
        async def gen():
            last, seq, last_beat = None, 0, time.time()
            yield "retry: 3000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    d = await run_in_threadpool(S.state_digest, ctx)
                except Exception as exc:  # database busy etc.: keep the stream alive
                    d = {"error": str(exc)}
                key = dumps(d)
                if key != last:
                    seq += 1
                    last = key
                    yield f"id: {seq}\nevent: state\ndata: {key}\n\n"
                    if once:
                        break
                elif time.time() - last_beat > 15:
                    last_beat = time.time()
                    yield ": keep-alive\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/worker")
    def api_worker():
        with ctx.db.session() as conn:
            w = row(conn, "SELECT * FROM worker_state WHERE id=1")
        out = S.worker_status_from_row(ctx, w)
        out["supervisor"] = supervisor.status() if supervisor else {"managed": False}
        return out

    @app.post("/api/worker/restart")
    def api_worker_restart():
        if not supervisor:
            raise S.ServiceError("The worker is not managed by this server (started with STUDIO_NO_WORKER=1 or autostart off).", 409)
        supervisor.restart()
        return {"ok": True}

    # ---------------------------------------------------------------- imports
    @app.post("/api/imports")
    async def api_import(file: UploadFile = File(...)):
        limit = ctx.settings().server.max_manuscript_mb * 1024 * 1024
        data = await read_upload(file, limit)
        return await run_in_threadpool(S.create_import, ctx, file.filename or "manuscript.txt", data)

    @app.get("/api/imports/{token}")
    def api_import_get(token: str):
        imp = S.get_import(ctx, token)
        return loads(imp["preview_json"], {})

    @app.post("/api/imports/{token}/confirm")
    def api_import_confirm(token: str, req: ConfirmImportReq):
        return S.confirm_import(ctx, token, req.model_dump())

    # ------------------------------------------------------------------ books
    @app.get("/api/books")
    def api_books(archived: bool = False):
        return {"books": S.list_books(ctx, include_archived=archived)}

    @app.get("/api/books/{book_id}")
    def api_book(book_id: int):
        return S.get_book(ctx, book_id)

    @app.patch("/api/books/{book_id}")
    def api_book_patch(book_id: int, req: BookPatchReq):
        return S.update_book_meta(ctx, book_id, req.model_dump(exclude_none=True))

    @app.post("/api/books/{book_id}/narrator")
    def api_book_narrator(book_id: int, req: NarratorReq):
        return S.change_narrator(ctx, book_id, req.voice_id, req.instructions, req.confirm_invalidate)

    @app.post("/api/books/{book_id}/resnapshot")
    def api_book_resnapshot(book_id: int, req: ConfirmReq):
        return S.resnapshot_settings(ctx, book_id, req.confirm_invalidate)

    @app.post("/api/books/{book_id}/archive")
    def api_book_archive(book_id: int, req: ArchiveReq):
        return S.archive_book(ctx, book_id, req.archived)

    @app.post("/api/books/{book_id}/cover")
    async def api_book_cover(book_id: int, file: UploadFile = File(...)):
        data = await read_upload(file, 10 * 1024 * 1024)
        return await run_in_threadpool(S.set_cover, ctx, book_id, file.filename or "cover.jpg", data)

    @app.post("/api/books/{book_id}/chapters/{chapter_id}/regenerate")
    def api_chapter_regenerate(book_id: int, chapter_id: int, req: ConfirmReq):
        return CH.regenerate_chapter(ctx, book_id, chapter_id, req.confirm_invalidate)

    @app.patch("/api/books/{book_id}/chapters/{chapter_id}")
    def api_chapter_title(book_id: int, chapter_id: int, req: ChapterTitleReq):
        return CH.set_chapter_title(ctx, book_id, chapter_id, req.title)

    @app.post("/api/books/{book_id}/assemble")
    def api_book_assemble(book_id: int):
        return S.enqueue_utility(ctx, book_id, "assemble", {})

    @app.post("/api/books/{book_id}/export")
    def api_book_export(book_id: int, req: ExportReq):
        if req.kind == "chapter_m4b" and not req.chapter_id:
            raise S.ServiceError("chapter_id is required for a chapter export", 422)
        return S.enqueue_utility(ctx, book_id, "export", {"kind": req.kind, "chapter_id": req.chapter_id})

    @app.post("/api/books/{book_id}/report")
    def api_book_report(book_id: int):
        return write_report(ctx, book_id)

    @app.post("/api/books/{book_id}/release")
    def api_book_release(book_id: int, req: ReleaseReq):
        return S.approve_release(ctx, book_id, req.approved_by, req.listen_scope, req.attest, req.notes)

    @app.post("/api/books/{book_id}/release/revoke")
    def api_book_revoke(book_id: int, req: RevokeReq):
        return S.revoke_release(ctx, book_id, req.reason)

    @app.post("/api/books/{book_id}/move")
    def api_book_move(book_id: int, req: MoveBookReq):
        return move_book(ctx, book_id, req.new_root)

    @app.get("/api/books/{book_id}/segments")
    def api_segments(book_id: int, chapter_id: Optional[int] = None, filter: str = "all", after: Optional[int] = None,
                     limit: int = Query(50, ge=1, le=200), q: Optional[str] = Query(None, max_length=200)):
        return S.list_segments(ctx, book_id, chapter_id, filter, after, limit, q)

    @app.get("/api/books/{book_id}/logs")
    def api_book_logs(book_id: int, limit: int = Query(200, ge=1, le=2000)):
        with ctx.db.session() as conn:
            from ..db import rows
            return {"logs": rows(conn, "SELECT id, job_id, level, message, created_at FROM job_logs WHERE book_id=? ORDER BY id DESC LIMIT ?", (book_id, limit))}

    # ------------------------------------------------------------------ queue
    @app.get("/api/queue")
    def api_queue():
        return S.list_queue(ctx)

    @app.post("/api/queue")
    def api_enqueue(req: EnqueueReq):
        return S.enqueue_book(ctx, req.book_id, req.front)

    @app.post("/api/queue/reorder")
    def api_reorder(req: ReorderReq):
        return S.reorder_queue(ctx, req.job_ids)

    @app.post("/api/jobs/{job_id}/move")
    def api_job_move(job_id: int, req: MoveJobReq):
        return S.move_job(ctx, job_id, req.direction)

    @app.post("/api/jobs/{job_id}/{action}")
    def api_job_action(job_id: int, action: Literal["pause", "resume", "retry", "cancel"]):
        return S.job_action(ctx, job_id, action)

    @app.get("/api/jobs/{job_id}")
    def api_job(job_id: int):
        return S.get_job(ctx, job_id)

    @app.get("/api/jobs/{job_id}/log")
    def api_job_log(job_id: int):
        return {"logs": S.job_logs(ctx, job_id)}

    # ----------------------------------------------------------------- voices
    @app.get("/api/voices")
    def api_voices(archived: bool = False):
        return {"voices": S.list_voices(ctx, archived), "speakers": S.PRESET_SPEAKERS, "english_speakers": list(S.ENGLISH_SPEAKERS)}

    @app.get("/api/voices/{voice_id}")
    def api_voice(voice_id: int):
        return S.get_voice(ctx, voice_id)

    @app.post("/api/voices/preset")
    def api_voice_preset(req: PresetVoiceReq):
        return S.create_preset_voice(ctx, req.name, req.speaker, req.instruct, req.language)

    @app.post("/api/voices/design")
    def api_voice_design(req: DesignVoiceReq):
        return S.create_design_voice(ctx, req.name, req.description, req.language)

    @app.post("/api/voices/clone")
    async def api_voice_clone(file: UploadFile = File(...), name: str = Form(..., max_length=80), ref_text: str = Form(..., max_length=1000),
                              owner_attest: bool = Form(False), rights_holder: str = Form("", max_length=120), statement: str = Form("", max_length=1000)):
        limit = ctx.settings().server.max_reference_mb * 1024 * 1024
        data = await read_upload(file, limit)
        return await run_in_threadpool(S.create_clone_voice, ctx, name, file.filename or "reference.wav", data, ref_text,
                                       {"owner_attest": owner_attest, "rights_holder": rights_holder, "statement": statement})

    @app.patch("/api/voices/{voice_id}")
    def api_voice_patch(voice_id: int, req: VoicePatchReq):
        return S.update_voice(ctx, voice_id, req.instruct, req.speaker, req.name, req.description)

    @app.post("/api/voices/{voice_id}/archive")
    def api_voice_archive(voice_id: int):
        S.archive_voice(ctx, voice_id)
        return {"ok": True}

    @app.post("/api/voices/{voice_id}/preview")
    def api_voice_preview(voice_id: int, req: PreviewReq):
        return S.enqueue_preview(ctx, voice_id, req.text, req.instruct, req.verify, req.seed)

    # ------------------------------------------------- reference-voice lab (recording)
    @app.get("/api/reference-scripts")
    def api_reference_scripts():
        from .. import reference as R
        return {"scripts": R.public_scripts(), "audition_text": R.AUDITION_TEXT}

    @app.post("/api/recordings")
    async def api_recording_create(file: UploadFile = File(...), script_id: str = Form("", max_length=60), ref_text: str = Form(..., max_length=1000),
                                   label: str = Form("", max_length=80)):
        data = await read_upload(file, ctx.settings().server.max_reference_mb * 1024 * 1024)
        return await run_in_threadpool(S.create_recording, ctx, file.filename or "recording.wav", data, script_id, ref_text, label)

    @app.get("/api/recordings")
    def api_recordings():
        return {"recordings": S.list_recordings(ctx)}

    @app.patch("/api/recordings/{rid}")
    def api_recording_text(rid: str, req: RecordingTextReq):
        return S.update_recording_text(ctx, rid, req.ref_text)

    @app.delete("/api/recordings/{rid}")
    def api_recording_delete(rid: str):
        S.delete_recording(ctx, rid)
        return {"ok": True}

    @app.post("/api/recordings/{rid}/job")
    def api_recording_job(rid: str, req: RecordingJobReq):
        return S.enqueue_recording_job(ctx, rid, req.mode, req.text)

    @app.post("/api/voices/from-recording")
    def api_voice_from_recording(req: FromRecordingReq):
        return S.create_voice_from_recording(ctx, req.recording_id, req.name,
                                             {"owner_attest": req.owner_attest, "rights_holder": req.rights_holder, "statement": req.statement})

    @app.api_route("/media/recording/{rid}", methods=["GET", "HEAD"])
    def media_recording(rid: str, request: Request):
        S.get_recording(ctx, rid)
        d = S.recordings_dir(ctx) / rid
        return _serve(d / "reference-24k.wav", [d], request, None)

    @app.post("/api/voices/promote")
    def api_voice_promote(req: PromoteReq):
        return S.promote_preview_to_clone(ctx, req.job_id, req.name)

    # --------------------------------------------------------------- segments
    @app.get("/api/segments/{segment_id}")
    def api_segment(segment_id: int):
        return S.segment_detail(ctx, segment_id)

    @app.get("/api/segments/by-sid/{sid}")
    def api_segment_sid(sid: str):
        with ctx.db.session() as conn:
            s = row(conn, "SELECT id FROM segments WHERE sid=?", (sid,))
        if not s:
            raise S.ServiceError("Segment not found", 404)
        return S.segment_detail(ctx, s["id"])

    @app.post("/api/segments/{segment_id}/approve")
    def api_seg_approve(segment_id: int, req: NoteReq):
        return S.approve_segment(ctx, segment_id, req.note)

    @app.post("/api/segments/{segment_id}/regenerate")
    def api_seg_regen(segment_id: int, req: NoteReq):
        return S.regenerate_segment(ctx, segment_id, req.note)

    @app.post("/api/segments/{segment_id}/override")
    def api_seg_override(segment_id: int, req: OverrideReq):
        return S.set_override(ctx, segment_id, req.text, req.regenerate)

    @app.post("/api/segments/{segment_id}/select-take")
    def api_seg_select(segment_id: int, req: SelectTakeReq):
        return S.select_take(ctx, segment_id, req.attempt_id)

    @app.post("/api/segments/{segment_id}/note")
    def api_seg_note(segment_id: int, req: NoteReq):
        return S.set_note(ctx, segment_id, req.note)

    from .narrative_api import register as register_narrative

    register_narrative(app, ctx)

    from ..platform_services import default_platform
    from ..update.launch import make_launcher
    from ..update.layout import Layout
    from ..update.manager import ReleaseUpdateManager
    from .update_api import register as register_updates

    if update_manager is None:
        layout = Layout.discover()
        pf = default_platform(layout.state)
        update_manager = ReleaseUpdateManager(ctx, layout, pf)
        update_manager.launch_helper = make_launcher(layout, pf)
    register_updates(app, ctx, update_manager)

    # ------------------------------------------------------------------ media
    def _book(conn, book_id):
        b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        if not b:
            raise S.ServiceError("Not found", 404)
        return b

    def _serve(path: Path | None, roots: list[Path], request: Request, download: str | None):
        if path is None or not Path(path).exists():
            raise S.ServiceError("Audio file not found on disk (is the storage drive connected?)", 404)
        try:
            safe = ensure_inside(Path(path), roots)
        except PermissionError:
            raise S.ServiceError("Access denied", 403)
        return file_response(safe, request, download)

    @app.api_route("/media/attempt/{attempt_id}", methods=["GET", "HEAD"])
    def media_attempt(attempt_id: int, request: Request, download: int = 0):
        with ctx.db.session() as conn:
            a = row(conn, "SELECT a.*, s.book_id, s.sid FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE a.id=?", (attempt_id,))
            if not a:
                raise S.ServiceError("Not found", 404)
            b = _book(conn, a["book_id"])
        dirs = book_dirs(b)
        p = resolve_take(b, a)
        return _serve(p, [dirs["segments"], dirs["alternates"]], request, f"{a['sid']}-take{a['round']}.{a['attempt_no']}.wav" if download else None)

    @app.api_route("/media/chapter/{chapter_id}.{ext}", methods=["GET", "HEAD"])
    def media_chapter(chapter_id: int, ext: Literal["wav", "mp3"], request: Request, download: int = 0):
        with ctx.db.session() as conn:
            r = row(conn, "SELECT cr.*, c.book_id, c.idx, c.title FROM chapter_renders cr JOIN chapters c ON c.id=cr.chapter_id WHERE cr.chapter_id=? AND cr.kind=?", (chapter_id, ext))
            if not r:
                raise S.ServiceError("Chapter has not been assembled yet", 404)
            b = _book(conn, r["book_id"])
        name = title_filename(b["title"], f"{r['idx']:03d} {r['title']}", "DRAFT" if r["draft"] else "") + f".{ext}"
        return _serve(Path(r["path"]), [book_dirs(b)["masters"]], request, name if download else None)

    @app.api_route("/media/export/{export_id}", methods=["GET", "HEAD"])
    def media_export(export_id: int, request: Request, download: int = 1):
        with ctx.db.session() as conn:
            e = row(conn, "SELECT * FROM exports WHERE id=?", (export_id,))
            if not e:
                raise S.ServiceError("Not found", 404)
            b = _book(conn, e["book_id"])
        dirs = book_dirs(b)
        p = Path(e["path"])
        return _serve(p, [dirs["exports"], dirs["reports"]], request, p.name if download else None)

    @app.api_route("/media/preview/{job_id}", methods=["GET", "HEAD"])
    def media_preview(job_id: int, request: Request, download: int = 0):
        j = S.get_job(ctx, job_id)
        if j["kind"] != "preview" or j["status"] != "completed":
            raise S.ServiceError("Preview not ready", 404)
        p = Path((j["result"] or {}).get("path", ""))
        return _serve(p, [ctx.previews_dir()], request, f"voice-preview-{job_id}.wav" if download else None)

    @app.api_route("/media/voice/{voice_id}/reference", methods=["GET", "HEAD"])
    def media_voice_ref(voice_id: int, request: Request):
        with ctx.db.session() as conn:
            v = row(conn, "SELECT * FROM voices WHERE id=?", (voice_id,))
            vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (v["current_version_id"],)) if v else None
        if not vv or not vv["ref_audio_path"]:
            raise S.ServiceError("No reference audio", 404)
        p = Path(vv["ref_audio_path"])
        return _serve(p, [p.parent], request, None)

    @app.api_route("/media/cover/{book_id}", methods=["GET", "HEAD"])
    def media_cover(book_id: int, request: Request):
        with ctx.db.session() as conn:
            b = _book(conn, book_id)
        if not b["cover_path"]:
            raise S.ServiceError("No cover", 404)
        return _serve(Path(b["cover_path"]), [book_dirs(b)["manuscripts"]], request, None)

    @app.api_route("/media/manuscript/{book_id}", methods=["GET", "HEAD"])
    def media_manuscript(book_id: int, request: Request):
        with ctx.db.session() as conn:
            b = _book(conn, book_id)
            m = row(conn, "SELECT * FROM manuscripts WHERE book_id=? ORDER BY id DESC LIMIT 1", (book_id,))
        if not m:
            raise S.ServiceError("No manuscript", 404)
        return _serve(Path(m["stored_path"]), [book_dirs(b)["manuscripts"]], request, m["original_name"])

    # --------------------------------------------------------------- settings
    @app.get("/api/settings")
    def api_settings():
        return S.get_settings(ctx)

    @app.put("/api/settings")
    def api_settings_put(patch: dict[str, Any]):
        return S.update_settings(ctx, patch)

    @app.put("/api/secrets/{slot}")
    def api_secret_put(slot: str, req: SecretReq, request: Request):
        return S.set_secret(ctx, slot, req.value, (request.state.user or {}).get("name", "operator"))

    @app.delete("/api/secrets/{slot}")
    def api_secret_delete(slot: str, request: Request):
        return S.set_secret(ctx, slot, None, (request.state.user or {}).get("name", "operator"))

    @app.get("/api/storage")
    def api_storage():
        return storage_overview(ctx)

    @app.post("/api/storage/validate")
    def api_storage_validate(req: PathReq):
        return validate_dir(req.path, ctx.home, create=False, measure=True)

    @app.get("/api/storage/browse")
    def api_storage_browse(path: Optional[str] = Query(None, max_length=4096), hidden: bool = False):
        """Server-side folder browser (admin only): lists folders on the hosting Mac."""
        return browse(path, hidden)

    @app.post("/api/storage/mkdir")
    def api_storage_mkdir(req: MkdirReq, request: Request):
        created = make_folder(req.parent, req.name)
        with ctx.db.tx() as conn:
            ctx.audit(conn, "storage", created, "mkdir", {"parent": req.parent}, (request.state.user or {}).get("name", "operator"))
        return {"path": created}

    @app.post("/api/storage/choose-folder")
    def api_choose_folder(request: Request):
        from ..config import is_loopback
        client = request.client.host if request.client else ""
        if not is_loopback(client):
            raise S.ServiceError("The Finder picker opens on the Mac's own screen, so it only works from the Mac itself. Use Browse instead.", 403)
        path = choose_folder_native()
        return {"path": path, "cancelled": path is None}

    # ------------------------------------------------------------------ about
    @app.get("/api/doctor")
    def api_doctor():
        return {"checks": run_checks(ctx)}

    @app.get("/api/about")
    def api_about():
        from ..engines import package_version
        build = ""
        try:
            build = (ctx.app_dir / "BUILD").read_text("utf-8").strip()
        except OSError:
            pass
        return {"name": APP_NAME, "version": __version__, "version_label": VERSION_LABEL, "developer": DEVELOPER,
                "company": COMPANY, "copyright": COPYRIGHT, "build": build, "engine_mode": ctx.engine_mode, "licenses": MODEL_LICENSES,
                "qa_limitations": QA_LIMITATIONS, "home": str(ctx.home), "python": sys.version.split()[0],
                "components": {k: package_version(k) for k in ("mlx", "mlx-audio", "mlx-whisper", "fastapi", "uvicorn", "numpy")}}

    return app
