"""Local web security: Host allowlist (DNS rebinding), same-origin + custom header checks on
mutating requests (CSRF), session authentication with admin-only routes, strict headers.

No CORS headers are sent: the UI is served by the same origin as the API, so cross-origin
access is never needed and is blocked by the browser's same-origin policy."""
from __future__ import annotations

import socket
import subprocess
import sys
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.responses import JSONResponse

from ..config import is_loopback
from .auth import COOKIE, AuthStore, LoginLimiter, is_admin_only

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; "
       "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
PUBLIC_PREFIXES = ("/static/",)
PUBLIC_PATHS = {"/", "/api/auth/login", "/api/auth/status", "/api/auth/logout", "/favicon.ico"}


def local_names() -> set[str]:
    """Host names/addresses this machine legitimately answers to."""
    names = {"127.0.0.1", "localhost", "[::1]", "::1"}
    try:
        names.add(socket.gethostname().lower())
        names.add(socket.getfqdn().lower())
        names.add(socket.gethostname().lower().split(".")[0])
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            names.add(info[4][0])
    except OSError:
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["/sbin/ifconfig"], capture_output=True, text=True, timeout=5).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[0] == "inet":
                    names.add(parts[1])
        except Exception:
            pass
    return {n for n in names if n}


def install(app, ctx) -> None:
    auth = AuthStore(ctx.config_dir / "auth.json")
    limiter = LoginLimiter()
    app.state.auth = auth
    app.state.limiter = limiter
    cached = {"names": None}

    def allowed_hosts() -> set[str]:
        if cached["names"] is None:
            cached["names"] = local_names()
        extra = {h.lower() for h in ctx.settings().server.allowed_hosts}
        return cached["names"] | extra

    def host_ok(host: str, port: int) -> bool:
        if host.startswith("["):  # [::1]:port
            name, _, rest = host.partition("]")
            name, p = name + "]", rest.lstrip(":")
        else:
            name, _, p = host.partition(":")
        if p and p != str(port):
            return False
        return name in allowed_hosts()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        s = ctx.settings().server
        port = getattr(app.state, "port", None) or s.port
        host = (request.headers.get("host") or "").lower()
        if not host_ok(host, port):
            return JSONResponse({"error": "Invalid Host header. Add this name to server.allowed_hosts in settings.json if it is legitimate."}, status_code=421)
        path = request.url.path
        if request.method in MUTATING:
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc.lower() != host:
                return JSONResponse({"error": "Cross-origin request blocked."}, status_code=403)
            sfs = request.headers.get("sec-fetch-site")
            if sfs and sfs not in ("same-origin", "none"):
                return JSONResponse({"error": "Cross-site request blocked."}, status_code=403)
            if request.headers.get("x-studio-request") != "1":
                return JSONResponse({"error": "Missing X-Studio-Request header (use the studio UI or CLI)."}, status_code=403)
        request.state.user = None
        if auth.enabled():
            request.state.user = auth.parse_token(request.cookies.get(COOKIE))
            public = path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)
            if not request.state.user and not public:
                return JSONResponse({"error": "Sign in required.", "auth": "required"}, status_code=401)
            if request.state.user and request.state.user["role"] != "admin" and is_admin_only(request.method, path):
                return JSONResponse({"error": "This action needs an administrator account."}, status_code=403)
        elif not is_loopback(s.host):
            return JSONResponse({"error": "Refusing to serve: network binding without any user account."}, status_code=503)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if path.startswith("/api/") or path.startswith("/media/"):
            response.headers.setdefault("Cache-Control", "private, no-store" if path.startswith("/api/") else "private, no-cache")
        return response
