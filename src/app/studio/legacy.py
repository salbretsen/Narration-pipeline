"""Adopt a Bash/Homebrew-era installation (``~/audiobook-studio-claude``) into the managed layout.

Rules this honours:
  * The old directory is never modified or deleted. Everything is copied (the database with SQLite's online-backup API, so a
    running old service is fine) and the old application stays as a working fallback.
  * Storage paths are preserved: books keep their recorded paths, the library root and the Hugging Face cache are pinned to
    the old absolute locations instead of being copied (no 14 GB duplicate, no re-download).
  * Metadata is backed up first, the NEW backend is started on a spare port against the copy and must answer /healthz,
    and only then is the old LaunchAgent unloaded and the new one installed.
  * Homebrew and every other package are never touched.
  * The new service listens on 127.0.0.1 unless the old configuration deliberately served the LAN and ``keep_network`` is passed."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .update.layout import Layout

LEGACY_LABEL = "com.audiobookstudio.claude"


def _say(msg: str) -> None:
    print(msg, flush=True)


def plan_settings(old_settings: dict, old_home: Path, keep_network: bool) -> tuple[dict, list[str]]:
    """Return the settings for the new installation and a list of human-readable notes about what was preserved or changed."""
    s = json.loads(json.dumps(old_settings))
    notes: list[str] = []
    storage = s.setdefault("storage", {})
    if not storage.get("library_root"):
        storage["library_root"] = str(old_home / "library")
        notes.append(f"Library stays at {storage['library_root']} (not copied).")
    else:
        notes.append(f"Library root kept: {storage['library_root']}.")
    models = s.setdefault("models", {})
    if not models.get("cache_dir"):
        cache = old_home / "cache" / "huggingface"
        if cache.exists():
            models["cache_dir"] = str(cache)
            notes.append(f"Model cache reused in place: {cache}.")
    srv = s.setdefault("server", {})
    if srv.get("allow_remote") or srv.get("host") not in (None, "127.0.0.1", "localhost", "::1"):
        if keep_network:
            notes.append(f"Network access kept as before (host {srv.get('host')}); sign-in remains required.")
        else:
            notes.append(f"Network access (host {srv.get('host')}) was turned OFF; the studio now listens on 127.0.0.1 only. "
                         "Re-run with --keep-network, or change it in Settings, to serve the LAN again.")
            srv["host"], srv["allow_remote"], srv["https_enabled"] = "127.0.0.1", False, False
    s.setdefault("updates", {})
    return s, notes


def backup_database(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    a = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        b = sqlite3.connect(str(dst))
        try:
            a.backup(b)
        finally:
            b.close()
    finally:
        a.close()


def test_backend(layout: Layout, python: str, release_dir: Path, port: int, timeout: float = 90.0) -> tuple[bool, str]:
    """Run the NEW backend against the migrated copy on a spare port (no GPU worker) and require a healthy /healthz."""
    import httpx

    env = {**os.environ, "AUDIOBOOK_STUDIO_HOME": str(layout.state), "AUDIOBOOK_STUDIO_ROOT": str(layout.root), "AUDIOBOOK_STUDIO_LOGS": str(layout.logs),
           "STUDIO_NO_WORKER": "1", "STUDIO_NO_UPDATE_CHECK": "1", "PYTHONPATH": str(release_dir), "PYTHONDONTWRITEBYTECODE": "1"}
    log = layout.logs / "migration-test.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "ab") as lf:
        p = subprocess.Popen([python, "-m", "studio", "serve", "--port", str(port)], cwd=str(release_dir), env=env, stdout=lf, stderr=lf,
                             stdin=subprocess.DEVNULL)
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if p.poll() is not None:
                    return False, f"the new backend exited with code {p.returncode} (see {log})"
                try:
                    r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=3)
                    if r.status_code == 200 and r.json().get("ok") and r.json().get("db"):
                        return True, f"healthy on port {port}"
                except Exception:
                    pass
                time.sleep(1)
            return False, f"the new backend did not answer within {int(timeout)} s (see {log})"
        finally:
            p.terminate()
            try:
                p.wait(15)
            except subprocess.TimeoutExpired:
                p.kill()


def migrate_legacy(old_home: Path, layout: Layout, dry_run: bool = False, keep_network: bool = False, platform_service=None,
                   backend_test: Callable[[Layout, str, Path, int], tuple[bool, str]] | None = None, agents_dir: Path | None = None,
                   install_service: Callable[[], None] | None = None) -> int:
    old_home = Path(old_home).expanduser().resolve()
    cfg, db = old_home / "config" / "settings.json", old_home / "data" / "studio.db"
    if not cfg.exists() and not db.exists():
        _say(f"{old_home} does not look like an Audiobook Studio installation (no config/settings.json or data/studio.db).")
        return 2
    if layout.state.exists() and (layout.state / "data" / "studio.db").exists():
        _say(f"{layout.state} already holds a database; refusing to overwrite it. Move it aside first if you really want to re-migrate.")
        return 2
    cur = layout.current_version()
    if not cur:
        _say("The managed application is not installed yet (no active release). Run install_audiobook_studio_mac.sh first.")
        return 2
    release_dir = layout.release_dir(cur)
    old_settings = json.loads(cfg.read_text("utf-8")) if cfg.exists() else {}
    new_settings, notes = plan_settings(old_settings, old_home, keep_network)
    _say(f"Migrating {old_home}  ->  {layout.state}")
    for n in notes:
        _say(f"  - {n}")
    if dry_run:
        _say("Dry run: nothing was changed.")
        return 0
    stamp = time.strftime("%Y%m%d-%H%M%S")
    bk = layout.state / "backups" / f"legacy-{stamp}"
    bk.mkdir(parents=True, exist_ok=True)
    # 1. back up metadata (old tree stays untouched)
    _say("Backing up the old configuration and database ...")
    if (old_home / "config").exists():
        shutil.copytree(old_home / "config", bk / "config")
    if db.exists():
        backup_database(db, bk / "studio.db")
    # 2. build the new state from the copies
    _say("Creating the new data folder ...")
    (layout.state / "config").mkdir(parents=True, exist_ok=True)
    (layout.state / "data").mkdir(parents=True, exist_ok=True)
    if (old_home / "config").exists():
        shutil.copytree(old_home / "config", layout.state / "config", dirs_exist_ok=True)
    (layout.state / "config" / "settings.json").write_text(json.dumps(new_settings, indent=2) + "\n", "utf-8")
    if db.exists():
        backup_database(db, layout.state / "data" / "studio.db")
    # 3. run migrations and prove the new backend works on the copy
    py = layout.service_info().get("python") or sys.executable
    env = {**os.environ, "AUDIOBOOK_STUDIO_HOME": str(layout.state), "PYTHONPATH": str(release_dir)}
    r = subprocess.run([py, "-m", "studio", "migrate"], cwd=str(release_dir), env=env, capture_output=True, text=True)
    if r.returncode != 0:
        _say(f"Database migration of the COPY failed; the old installation is untouched.\n{r.stdout[-400:]}{r.stderr[-400:]}")
        return 1
    from .lifecycle_cli import find_free_port

    port = find_free_port(int(new_settings.get("server", {}).get("port", 7861)) + 1000)
    ok, detail = (backend_test or test_backend)(layout, py, release_dir, port)
    if not ok:
        _say(f"The new backend failed its check: {detail}\nThe old installation is untouched and still the active service.")
        return 1
    _say(f"New backend verified: {detail}.")
    # 4. switch the service: unload the old LaunchAgent (keep its plist in the backup so it can be put back), install the new one
    if platform_service is not None:
        old_plist = (agents_dir or Path.home() / "Library" / "LaunchAgents") / f"{LEGACY_LABEL}.plist"
        try:
            platform_service.stop(LEGACY_LABEL)
        except Exception as exc:
            _say(f"  (could not stop the old service cleanly: {exc})")
        if old_plist.exists():
            shutil.move(str(old_plist), str(bk / old_plist.name))
            _say(f"Old LaunchAgent removed from login items; its plist is saved at {bk / old_plist.name}.")
        if install_service:
            install_service()
    _say("Migration complete. The old folder was not modified; remove it yourself once you are happy. "
         f"To go back: restore {bk / (LEGACY_LABEL + '.plist')} to ~/Library/LaunchAgents and stop the new service.")
    return 0
