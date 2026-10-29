"""HTTP API for the Update Center. State-changing routes are administrator-only (auth.ADMIN_RULES) and pass the same-origin /
CSRF checks as every other mutating route. No endpoint accepts a URL or a command: the repository comes from validated
settings and artifact URLs come from the signature-verified index."""
from __future__ import annotations

import threading
import time
from typing import Literal, Optional

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field

from .. import services as S
from ..update.manager import ReleaseUpdateManager, UpdateError


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstallReq(_Req):
    version: str = Field(min_length=5, max_length=40)
    mode: Literal["now", "after_job"] = "after_job"
    confirm: bool = False


class PrefsReq(_Req):
    channel: Optional[Literal["stable", "beta"]] = None
    auto_check: Optional[bool] = None
    check_interval_hours: Optional[int] = Field(None, ge=1, le=168)


def _wrap(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except UpdateError as exc:
        raise S.ServiceError(str(exc), exc.status) from exc


def register(app, ctx, manager: ReleaseUpdateManager) -> None:
    app.state.update_manager = manager

    @app.get("/healthz", include_in_schema=False)
    def healthz(request: Request):
        from .. import __version__
        from ..update.launch import health_payload

        loopback = (request.client.host if request.client else "") in ("127.0.0.1", "::1", "testclient")
        return health_payload(ctx, __version__, loopback)

    @app.get("/api/version")
    def api_version():
        from .. import VERSION_LABEL, __version__

        return {"version": __version__, "version_label": VERSION_LABEL, "build": manager._build(), "channel": ctx.settings().updates.channel,
                "schema": ctx.db.schema_version(), "managed": manager.layout.managed}

    @app.get("/api/update/summary")
    def api_update_summary():
        """Small payload for the dashboard indicator."""
        st = manager.status()
        return {"state": st["state"], "installed_version": st["installed_version"], "latest_version": (st["latest"] or {}).get("version"),
                "staged_version": st["staged_version"], "message": st["message"], "error": st["error"], "managed": st["managed"]}

    @app.get("/api/update/status")
    def api_update_status():
        return manager.status()

    @app.get("/api/update/history")
    def api_update_history():
        return {"history": manager.status_file.read_history(50)}

    @app.post("/api/update/check")
    def api_update_check():
        return _wrap(manager.check)

    @app.post("/api/update/download")
    def api_update_download():
        return _wrap(manager.download)

    @app.post("/api/update/install")
    def api_update_install(req: InstallReq):
        if not req.confirm:
            raise S.ServiceError("Installing an update needs explicit confirmation.", 422)
        return _wrap(manager.request_install, req.mode, req.version)

    @app.post("/api/update/cancel")
    def api_update_cancel():
        return _wrap(manager.cancel)

    @app.post("/api/update/restart")
    def api_update_restart():
        """Restart the service through the platform, from a short-lived thread so this response is delivered first."""
        ps = manager.platform_service
        if ps is None or not manager.layout.managed:
            raise S.ServiceError("The service is not managed by the launchd installer; restart it from Terminal (audiobook-studio restart).", 409)
        label = manager.layout.service_info().get("label") or "com.audiobookstudio.app"
        threading.Thread(target=lambda: (time.sleep(0.8), ps.restart(label)), daemon=True).start()
        return {"ok": True, "message": "Restarting. This page reconnects automatically."}

    @app.put("/api/update/preferences")
    def api_update_prefs(req: PrefsReq):
        s = ctx.settings().model_copy(deep=True)
        for k, v in req.model_dump(exclude_none=True).items():
            setattr(s.updates, k, v)
        ctx.save_settings(s)
        return manager.status()
