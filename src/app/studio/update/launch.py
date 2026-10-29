"""Starting the separate updater process, and the health payload both the server and the updater agree on."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from ..interfaces import PlatformService
from .layout import Layout

UPDATER_LABEL = "com.audiobookstudio.updater"


def make_launcher(layout: Layout, platform_service: PlatformService, python: str | None = None):
    """Returns ``launch(version, mode)``. The helper runs from the CURRENTLY ACTIVE release directory (resolved, not through the
    ``current`` symlink), outside the service's process group, with absolute paths only. No shell is involved."""

    def launch(version: str, mode: str) -> None:
        cur = layout.current_version()
        if not cur:
            raise RuntimeError("no active release is recorded; cannot start the updater")
        cwd = str(layout.release_dir(cur).resolve())
        py = python or layout.service_info().get("python") or sys.executable
        env = {"AUDIOBOOK_STUDIO_HOME": str(layout.state), "AUDIOBOOK_STUDIO_ROOT": str(layout.root), "AUDIOBOOK_STUDIO_LOGS": str(layout.logs),
               "PYTHONPATH": cwd, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
               "PATH": os.pathsep.join([str(layout.tools_bin), "/usr/bin", "/bin", "/usr/sbin", "/sbin"] if os.name != "nt" else [os.environ.get("PATH", "")])}
        argv = [str(py), "-m", "studio.update.helper", "--root", str(layout.root), "--version", version, "--mode", mode]
        platform_service.launch_detached(UPDATER_LABEL, argv, cwd, env, str(layout.logs / "updater.log"), layout.update_dir / "launchd")

    return launch


def health_payload(ctx, version: str, detailed: bool) -> dict:
    """/healthz body. Counts and storage detail are only returned to loopback callers."""
    out: dict = {"ok": True, "version": version, "db": False, "schema": None}
    try:
        out["schema"] = ctx.db.schema_version()
        with ctx.db.session() as conn:
            conn.execute("SELECT 1").fetchone()
            out["db"] = True
            if detailed:
                out["queue"] = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM jobs WHERE kind='book' GROUP BY status")}
    except Exception as exc:
        out["ok"] = False
        out["error"] = type(exc).__name__
    if detailed:
        try:
            root = Path(ctx.library_root())
            ok = root.exists() and os.access(root, os.W_OK)
            out["storage"] = {"ok": ok, "detail": "" if ok else f"{root} is not available"}
        except Exception as exc:
            out["storage"] = {"ok": False, "detail": type(exc).__name__}
    return out
