"""The separate updater process. It is started by the web process through the platform (a one-shot launchd job on macOS) or
by ``studio update --install``; it is never a thread of the server, so stopping the server cannot interrupt it.

Sequence (each step is journalled so an interrupted run can be repaired):
  verify   re-hash the staged tree against its manifest (catches tampering after staging)
  drain    hold the queue; the segment being synthesised finishes and is on disk before we continue
  backup   consistent SQLite backup (sqlite3 online-backup API) + a copy of config/
  stop     stop the service through the platform (launchd bootout)
  switch   atomically repoint ``current`` (rename of a symlink + pointer file)
  migrate  run ``studio migrate`` from the NEW release (takes its own DB backup first)
  start    start the service
  health   /healthz must report the target version with a working database
  finish   release the queue hold, record history, prune old releases
On any failure after the stop: stop, repoint to the previous release, restore the database backup if the schema moved, start,
confirm health. If even that fails, write a repair note and leave every user file untouched ("repair mode")."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from ..scheduler import FileQueueScheduler
from ..util import atomic_write_text, now
from .archive import ArchiveError, verify_tree_manifest
from .layout import Layout
from .runtime import runtime_python, runtime_ready
from .status import StatusFile
from .store import ReleaseStore

DEFAULT_LABEL = "com.audiobookstudio.app"


class ApplyError(Exception):
    pass


def http_health(port: int, timeout: float = 5.0) -> dict | None:
    import httpx

    try:
        r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def pid_alive(pid: int) -> bool:
    from ..platform_services import _alive

    return _alive(pid)


class Applier:
    def __init__(self, layout: Layout, ctx, platform_service, label: str | None = None, scheduler: FileQueueScheduler | None = None,
                 health: Callable[[int], dict | None] | None = None, migrate: Callable[[Path, Path, dict], tuple[int, str]] | None = None,
                 health_timeout: float = 120.0, drain_timeout: float = 15 * 60, after_job_timeout: float = 7 * 86400, poll: float = 2.0,
                 python_for: Callable[[Path], Path] | None = None):
        self.layout, self.ctx, self.platform = layout, ctx, platform_service
        info = layout.service_info()
        self.label = label or info.get("label") or DEFAULT_LABEL
        self.scheduler = scheduler or FileQueueScheduler(ctx.run_dir, ctx.db)
        self.health = health or http_health
        self.migrate_fn = migrate or self._migrate_subprocess
        self.health_timeout, self.drain_timeout, self.after_job_timeout, self.poll = health_timeout, drain_timeout, after_job_timeout, poll
        self.python_for = python_for or self._python_for
        self.store = ReleaseStore(layout)
        self.status = StatusFile(layout.update_dir)
        self.journal = layout.update_dir / "journal.json"
        self.request = layout.update_dir / "request.json"
        self._had_request = False

    # ---------------------------------------------------------------- utilities
    def _python_for(self, release_dir: Path) -> Path:
        try:
            meta = json.loads((release_dir / "release.json").read_text("utf-8"))
            rid = meta.get("runtime_id")
            if rid:
                if runtime_ready(self.layout, rid):
                    return runtime_python(self.layout, rid)
        except (OSError, ValueError):
            pass
        info = self.layout.service_info()
        return Path(info.get("python") or sys.executable)

    def _activate(self, version: str, release_dir: Path) -> None:
        self.store.activate(version)
        try:
            self.store.link_python(self.python_for(release_dir))
        except OSError:
            pass

    def _port(self) -> int:
        return self.ctx.settings().server.port

    def _step(self, phase: str, state: str, pct: float | None, detail: str, **top) -> None:
        self.status.update(phase=(phase, state, pct, detail), **top)

    def _journal(self, **kw) -> None:
        atomic_write_text(self.journal, json.dumps({**kw, "at": now()}))

    def _cancelled(self) -> bool:
        """The Update Center cancels by deleting request.json; a run started without one (CLI) cannot be cancelled that way."""
        return self._had_request and not self.request.exists()

    def _migrate_subprocess(self, release_dir: Path, python: Path, env: dict) -> tuple[int, str]:
        r = subprocess.run([str(python), "-m", "studio", "migrate"], cwd=str(release_dir), env={**os.environ, **env},
                           capture_output=True, text=True, timeout=1800)
        return r.returncode, (r.stdout + r.stderr)[-2000:]

    def _service_env(self, release_dir: Path) -> dict:
        info = self.layout.service_info()
        env = dict(info.get("env") or {})
        env.setdefault("AUDIOBOOK_STUDIO_HOME", str(self.layout.state))
        env.setdefault("AUDIOBOOK_STUDIO_ROOT", str(self.layout.root))
        env["PYTHONPATH"] = str(release_dir)
        return env

    def backup_database(self, tag: str) -> tuple[Path, Path | None]:
        d = self.ctx.backups_dir / "update"
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dst = d / f"studio-{tag}-{stamp}.db"
        src = sqlite3.connect(str(self.ctx.db.path))
        try:
            out = sqlite3.connect(str(dst))
            try:
                src.backup(out)
            finally:
                out.close()
        finally:
            src.close()
        cfg = None
        if self.ctx.config_dir.exists():
            cfg = d / f"config-{tag}-{stamp}"
            shutil.copytree(self.ctx.config_dir, cfg, ignore=shutil.ignore_patterns("tls"))
        return dst, cfg

    def _restore_database(self, backup: Path) -> None:
        db = Path(self.ctx.db.path)
        keep = db.with_name(f"studio.failed-{time.strftime('%Y%m%d-%H%M%S')}.db")
        if db.exists():
            shutil.copy2(db, keep)   # the post-failure database is kept, never silently discarded
        for ext in ("-wal", "-shm"):
            Path(str(db) + ext).unlink(missing_ok=True)
        tmp = db.with_name(db.name + ".restore")
        shutil.copy2(backup, tmp)
        os.replace(tmp, db)

    def _wait_health(self, version: str, timeout: float) -> dict | None:
        deadline = time.time() + timeout
        while True:
            h = self.health(self._port())
            if h and h.get("ok") and h.get("db") and h.get("version") == version:
                return h
            if time.time() >= deadline:
                return None
            time.sleep(min(self.poll, 1.0))

    # ----------------------------------------------------------------- preflight
    def preflight(self, version: str) -> list[str]:
        problems: list[str] = []
        rel = self.layout.release_dir(version)
        if not self.store.has(version):
            problems.append(f"Release {version} is not staged.")
        for d, what in ((self.layout.app, "the application folder"), (self.layout.state, "the data folder")):
            if not d.exists() or not os.access(d, os.W_OK):
                problems.append(f"{what.capitalize()} ({d}) is missing or not writable. If it is on an external disk, reconnect it.")
        try:
            if rel.exists():
                verify_tree_manifest(rel)
        except ArchiveError as exc:
            problems.append(f"The staged release failed re-verification: {exc}")
        free = shutil.disk_usage(self.layout.state).free
        db_size = Path(self.ctx.db.path).stat().st_size if Path(self.ctx.db.path).exists() else 0
        if free < db_size * 2 + (50 << 20):
            problems.append("Not enough free space to back up the database.")
        return problems

    # ---------------------------------------------------------------------- run
    def run(self, version: str, mode: str) -> bool:
        lock = self.layout.update_dir / "apply.lock"
        self.layout.update_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
        except FileExistsError:
            try:
                other = int(lock.read_text() or 0)
            except (OSError, ValueError):
                other = 0
            if other and other != os.getpid() and pid_alive(other):
                raise ApplyError("Another update is already running.")
            lock.write_text(str(os.getpid()))
        try:
            return self._run(version, mode)
        finally:
            lock.unlink(missing_ok=True)
            self.request.unlink(missing_ok=True)

    def _fail(self, version: str, message: str, kind: str = "failed", state: str = "failed") -> bool:
        self.scheduler.release()
        self.status.update(state=state, message=message, error=message, error_kind=kind, mode=None)
        self.status.add_history("install", version, False, message)
        return False

    def _run(self, version: str, mode: str) -> bool:
        old = self.store.current()
        self._had_request = self.request.exists()
        self._journal(step="start", version=version, old=old, mode=mode)
        self.status.update(state="waiting", target_version=version, mode=mode, error=None, error_kind=None, phases={})
        problems = self.preflight(version)
        if problems:
            return self._fail(version, " ".join(problems), "preflight")
        if old == version:
            return self._fail(version, "That version is already active.", "preflight")
        # --- drain
        self.scheduler.hold(mode, reason=f"update to {version}")
        timeout = self.after_job_timeout if mode == "after_job" else self.drain_timeout
        deadline = time.time() + timeout
        self._step("drain", "running", None, "Waiting for the current work to reach a safe point")
        while not self.scheduler.drained():
            if self._cancelled():
                self.scheduler.release()
                self.status.update(state="staged", message="Installation cancelled.", mode=None)
                return False
            title = self.scheduler.running_summary()
            self.status.update(state="waiting" if mode == "after_job" else "draining",
                               message=f"Waiting for “{title}” to finish." if title else "Waiting for the current segment to finish.")
            if time.time() > deadline:
                return self._fail(version, "Timed out waiting for the queue to drain. Nothing was changed; try again when the Mac is idle.", "drain")
            time.sleep(self.poll)
        self._step("drain", "done", 100, "Queue is idle; completed audio is on disk")
        # --- backup
        self.status.update(state="installing", message="Backing up the database")
        self._step("install", "running", 10, "Backing up the database")
        try:
            backup, cfg = self.backup_database(f"{old or 'x'}-to-{version}")
        except Exception as exc:
            return self._fail(version, f"Could not back up the database ({exc}). Nothing was changed.", "backup")
        schema_before = self.ctx.db.schema_version()
        self._journal(step="backed_up", version=version, old=old, backup=str(backup), schema_before=schema_before)
        # --- stop, switch, migrate, start, health
        new_dir = self.layout.release_dir(version)
        switched = False
        try:
            self._step("install", "running", 30, "Stopping the service")
            self.status.update(state="installing", message="Stopping the service")
            self.platform.stop(self.label)
            self._journal(step="stopped", version=version, old=old, backup=str(backup), schema_before=schema_before)
            self._step("install", "running", 50, "Switching to the new version")
            self._activate(version, new_dir)
            switched = True
            self._journal(step="switched", version=version, old=old, backup=str(backup), schema_before=schema_before)
            self._step("install", "running", 70, "Applying database migrations")
            rc, out = self.migrate_fn(new_dir, self.python_for(new_dir), self._service_env(new_dir))
            if rc != 0:
                raise ApplyError(f"Database migration failed: {out.strip()[-400:]}")
            self._step("install", "done", 100, "Installed")
            self.status.update(state="restarting", message="Starting the new version")
            self._step("restart", "running", None, "Starting the service")
            self.platform.start(self.label)
            self._step("restart", "done", 100, "Service started")
            self.status.update(state="health_check", message="Checking the new version")
            self._step("health", "running", None, "Waiting for /healthz")
            h = self._wait_health(version, self.health_timeout)
            if not h:
                raise ApplyError(f"The new version did not become healthy within {int(self.health_timeout)} s.")
            self._step("health", "done", 100, "Healthy")
        except Exception as exc:
            return self._rollback(version, old, backup, schema_before, str(exc), switched)
        warnings = []
        storage = h.get("storage") or {}
        if storage.get("ok") is False:
            warnings.append("Storage is unavailable (" + str(storage.get("detail", "disconnected")) + "); affected jobs stay paused until it returns.")
        self.scheduler.release()
        pruned = self.store.prune(self.ctx.settings().updates.keep_releases)
        msg = f"Updated to {version}." + (" " + " ".join(warnings) if warnings else "")
        self.status.update(state="succeeded", message=msg, error=None, error_kind=None, mode=None, staged_version=None, latest=None,
                           last_installed=version, last_installed_at=now(), warnings=warnings)
        self.status.add_history("install", version, True, msg, previous=old, backup=str(backup), pruned=pruned)
        self._journal(step="done", version=version, old=old)
        return True

    def _rollback(self, version: str, old: str | None, backup: Path, schema_before: int, reason: str, switched: bool) -> bool:
        self.status.update(state="installing", message="The update failed; restoring the previous version", error=reason)
        self._step("install", "failed", None, reason)
        try:
            self.platform.stop(self.label)
            if old and switched:
                self._activate(old, self.layout.release_dir(old))
            if self.ctx.db.schema_version() != schema_before:
                self._restore_database(backup)
            if old:
                self.platform.start(self.label)
                h = self._wait_health(old, self.health_timeout)
            else:
                h = None
        except Exception as exc:
            h = None
            reason += f" (rollback error: {exc})"
        if h:
            self.scheduler.release()
            msg = f"Update to {version} failed and was rolled back to {old}: {reason}"
            self.status.update(state="rolled_back", message=msg, error=reason, error_kind="rolled_back", mode=None)
            self.status.add_history("install", version, False, msg, rolled_back_to=old)
            return False
        note = self.layout.update_dir / "REPAIR.txt"
        atomic_write_text(note, f"Update to {version} failed and the previous version did not restart automatically.\nReason: {reason}\n"
                                f"Your books, voices, settings and the database backup are untouched: {backup}\n"
                                f"Run:  audiobook-studio doctor   then   audiobook-studio start\n")
        self.scheduler.release()
        msg = f"Update to {version} failed and automatic recovery could not confirm the previous version is running. See {note}. No project data was deleted."
        self.status.update(state="failed", message=msg, error=reason, error_kind="repair", mode=None)
        self.status.add_history("install", version, False, msg, repair=True)
        return False


def preload() -> None:
    """Import everything the helper may need BEFORE the active release changes, so it never mixes old and new modules."""
    import importlib

    for m in ("studio.context", "studio.db", "studio.config", "studio.scheduler", "studio.platform_services", "studio.update.archive",
              "studio.update.store", "studio.update.runtime", "studio.update.status", "studio.util", "httpx", "sqlite3"):
        importlib.import_module(m)


def main(argv: list[str] | None = None) -> int:
    from ..context import Context
    from ..platform_services import default_platform

    preload()
    ap = argparse.ArgumentParser(prog="studio.update.helper")
    ap.add_argument("--root")
    ap.add_argument("--version")
    ap.add_argument("--mode", choices=["now", "after_job"])
    a = ap.parse_args(argv)
    layout = Layout.discover(a.root)
    req = {}
    try:
        req = json.loads((layout.update_dir / "request.json").read_text("utf-8"))
    except (OSError, ValueError):
        pass
    version = a.version or req.get("version")
    mode = a.mode or req.get("mode") or "after_job"
    if not version:
        print("No update request found.", file=sys.stderr)
        return 2
    os.environ.setdefault("AUDIOBOOK_STUDIO_HOME", str(layout.state))
    ctx = Context(layout.state, migrate=False)
    ap_ = Applier(layout, ctx, default_platform(layout.state))
    try:
        return 0 if ap_.run(version, mode) else 1
    except ApplyError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
