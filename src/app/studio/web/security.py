"""Local web security: Host allowlist (DNS rebinding), same-origin + custom header checks on
mutating requests (CSRF), and strict response headers."""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from ..config import is_loopback

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def _loopback_hosts(port: int) -> set[str]:
    return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def install(app, ctx) -> None:
    @app.middleware("http")
    async def guard(request: Request, call_next):
        s = ctx.settings().server
        port = getattr(app.state, "port", None) or s.port
        host = (request.headers.get("host") or "").lower()
        if not s.allow_remote or is_loopback(s.host):
            if host not in _loopback_hosts(port):
                return JSONResponse({"error": "Invalid Host header (this studio only answers on 127.0.0.1)."}, status_code=421)
        if request.method in MUTATING:
            origin = request.headers.get("origin")
            if origin:
                if urlsplit(origin).netloc.lower() != host:
                    return JSONResponse({"error": "Cross-origin request blocked."}, status_code=403)
            sfs = request.headers.get("sec-fetch-site")
            if sfs and sfs not in ("same-origin", "none"):
                return JSONResponse({"error": "Cross-site request blocked."}, status_code=403)
            if request.headers.get("x-studio-request") != "1":
                return JSONResponse({"error": "Missing X-Studio-Request header (use the studio UI or CLI)."}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        return response
