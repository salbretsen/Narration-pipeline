"""ReleaseUpdateManager: check, download, verify and stage a release. Runs in the web process (or the CLI) and only ever
*adds* a new directory under ``releases/``; it never touches the running release. Switching and restarting is the helper's job."""
from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .. import __version__
from ..interfaces import PlatformService, ReleaseRepository, UpdateManager
from ..util import atomic_write_text, now
from .archive import ArchiveError, safe_extract, verify_tree_manifest
from .layout import Layout
from .manifest import Index, ManifestError, Release, parse_version, select_update
from .net import NetError, validate_repository
from .repository import make_repository
from .runtime import RuntimeError_, ensure_runtime
from .status import StatusFile
from .store import ReleaseStore
from .trust import TrustError, TrustStore

FREE_SPACE_FACTOR = 3          # archive + unpacked tree + headroom
MIN_FREE_BYTES = 200 << 20


class UpdateError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def machine_platform() -> tuple[str, str]:
    return ("macos" if os.uname().sysname == "Darwin" else os.uname().sysname.lower(), _platform.machine().lower()) if hasattr(os, "uname") \
        else ("windows", _platform.machine().lower())


class ReleaseUpdateManager(UpdateManager):
    def __init__(self, ctx, layout: Layout, platform_service: PlatformService | None = None, repository: ReleaseRepository | None = None,
                 trust: TrustStore | None = None, installed_version: str | None = None, runner=None,
                 platform_tuple: tuple[str, str] | None = None, os_version: str | None = None):
        self.ctx = ctx
        self.layout = layout
        self.platform_service = platform_service
        self._repo = repository
        self.trust = trust or TrustStore.load_pinned()
        self.installed = installed_version or __version__
        self.store = ReleaseStore(layout)
        self.status_file = StatusFile(layout.update_dir)
        self.runner = runner
        self.plat, self.arch = platform_tuple or machine_platform()
        self._os_version = os_version
        self._lock = threading.Lock()
        self._busy: str | None = None
        self._cancel = threading.Event()
        self.launch_helper: Callable[[str, str], None] | None = None   # (version, mode) -> start the separate updater process

    # ------------------------------------------------------------------ helpers
    def settings(self):
        return self.ctx.settings().updates

    @property
    def repo(self) -> ReleaseRepository:
        return self._repo or make_repository(self.settings().repository)

    def os_version(self) -> str:
        if self._os_version:
            return self._os_version
        return self.platform_service.os_version() if self.platform_service else (_platform.mac_ver()[0] or "0")

    def _set(self, **kw) -> dict:
        return self.status_file.update(**kw)

    def _phase(self, name: str, state: str, pct: float | None = None, detail: str = "") -> None:
        self.status_file.update(phase=(name, state, pct, detail))

    def _last_sequence(self, channel: str) -> int:
        return int((self.status_file.read().get("sequences") or {}).get(channel, 0))

    def _store_sequence(self, channel: str, seq: int) -> None:
        seqs = dict(self.status_file.read().get("sequences") or {})
        seqs[channel] = max(seq, int(seqs.get(channel, 0)))
        self._set(sequences=seqs)

    # ------------------------------------------------------------------- status
    def status(self) -> dict:
        d = self.status_file.read()
        s = self.settings()
        managed = self.layout.managed
        pointer = self.layout.read_pointer()
        hold = None
        try:
            from ..scheduler import FileQueueScheduler

            h = FileQueueScheduler(self.ctx.run_dir).current_hold()
            hold = {"mode": h.mode, "reason": h.reason, "since": h.since} if h else None
        except Exception:
            pass
        return {
            "installed_version": self.installed, "build": self._build(), "channel": s.channel, "managed": managed,
            "repository": s.repository, "signing_configured": self.trust.configured or self.trust.allow_unsigned,
            "auto_check": s.auto_check, "check_interval_hours": s.check_interval_hours,
            "previous_version": pointer.get("previous"), "installed_releases": self.store.installed(),
            "state": d.get("state", "idle"), "message": d.get("message", ""), "error": d.get("error"), "error_kind": d.get("error_kind"),
            "latest": d.get("latest"), "selection": d.get("selection"), "staged_version": d.get("staged_version"),
            "target_version": d.get("target_version"), "mode": d.get("mode"), "phases": d.get("phases") or {},
            "last_check": d.get("last_check"), "last_success_check": d.get("last_success_check"), "busy": self._busy,
            "queue_hold": hold, "updated_at": d.get("updated_at"),
            "platform": f"{self.plat}/{self.arch}",
            "notes": (d.get("latest") or {}).get("notes", ""),
            "not_managed_reason": None if managed else "This copy was not installed by the managed installer, so it cannot update itself. "
                                                      "Run install_audiobook_studio_mac.sh once to migrate (your books and settings are kept).",
        }

    def _build(self) -> str:
        try:
            return (Path(__file__).resolve().parents[2] / "BUILD").read_text("utf-8").strip()
        except OSError:
            return ""

    # -------------------------------------------------------------------- check
    def check(self) -> dict:
        if not self._claim("check"):
            raise UpdateError("Another update operation is in progress.")
        try:
            return self._check()
        finally:
            self._busy = None

    def _claim(self, what: str) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = what
            return True

    def _check(self) -> dict:
        s = self.settings()
        if not (self.trust.configured or self.trust.allow_unsigned):
            msg = ("Updates are switched off: no release-signing key is pinned in this installation, so a download could not be authenticated. "
                   "The owner publishes a signing key in an application release (see docs/10-installation-and-updates.md).")
            self._set(state="disabled", message=msg, error=None, error_kind="no_key", last_check=now())
            return self.status()
        self._set(state="checking", message="Contacting the update server", error=None, error_kind=None)
        self._phase("check", "running", None, "Fetching signed release index")
        t = now()
        try:
            raw = self.repo.fetch_index(s.channel)
            signed = self.trust.verify_envelope(raw)
            index = Index.from_signed(signed)
            if index.channel != s.channel and not (self.trust.allow_unsigned and s.channel == "dev"):
                raise ManifestError(f"The index is for channel '{index.channel}', not '{s.channel}'.")
            if index.sequence < self._last_sequence(index.channel):
                raise ManifestError("The release index is older than one already seen (possible rollback/replay attack); ignored.")
            sel = select_update(index, self.installed, s.channel, self.plat, self.arch, self.os_version(), self.ctx.db.schema_version())
        except NetError as exc:
            kind = "offline" if exc.offline else "failed"
            self._set(state="offline" if exc.offline else "failed", message=str(exc), error=str(exc), error_kind=kind, last_check=t)
            self._phase("check", "failed", None, str(exc))
            self.status_file.add_history("check", None, False, str(exc))
            return self.status()
        except (TrustError, ManifestError, ValueError) as exc:
            self._set(state="failed", message=str(exc), error=str(exc), error_kind="untrusted", last_check=t)
            self._phase("check", "failed", None, str(exc))
            self.status_file.add_history("check", None, False, str(exc))
            return self.status()
        self._store_sequence(index.channel, index.sequence)
        self._phase("check", "done", 100, "Release index verified")
        base = dict(last_check=t, last_success_check=t, error=None, error_kind=None)
        self._set(selection={"state": sel.state, "reason": sel.reason, "newer_exists": sel.newer_exists}, **base)
        if sel.state == "available":
            self._set(state="available", latest={**sel.release.public(), "signed": self.trust.configured}, message=f"Version {sel.release.version} is available.")
            if self.status_file.read().get("staged_version") not in (None, sel.release.version):
                self._set(staged_version=None)
        elif sel.state == "incompatible":
            self._set(state="incompatible", latest={**sel.release.public(), "signed": self.trust.configured}, message=sel.reason)
        else:
            self._set(state="up_to_date", latest=None, staged_version=None, message=sel.reason)
        return self.status()

    def auto_check_due(self) -> bool:
        s = self.settings()
        if not s.auto_check or self._busy:
            return False
        last = self.status_file.read().get("last_check") or 0
        return now() - last >= s.check_interval_hours * 3600

    # ----------------------------------------------------------------- download
    def download(self, background: bool = True) -> dict:
        st = self.status_file.read()
        latest = st.get("latest")
        if st.get("state") not in ("available", "failed", "offline", "staged") or not latest:
            raise UpdateError("There is no update to download. Check for updates first.")
        if st.get("state") == "incompatible":
            raise UpdateError("This update is not compatible with this installation.")
        if not self.layout.managed:
            raise UpdateError(self.status()["not_managed_reason"])
        release = self._release_from_status(latest)
        if not self._claim("download"):
            raise UpdateError("Another update operation is in progress.")
        self._cancel.clear()
        if background:
            threading.Thread(target=self._download_guarded, args=(release,), daemon=True, name="update-download").start()
            return self.status()
        self._download_guarded(release)
        return self.status()

    def _release_from_status(self, latest: dict) -> Release:
        """The artifact URL is never stored in client-visible status. Re-fetch and re-verify the signed index to obtain it."""
        s = self.settings()
        raw = self.repo.fetch_index(s.channel)
        signed = self.trust.verify_envelope(raw)
        index = Index.from_signed(signed)
        for r in index.releases:
            if r.version == latest["version"] and r.sha256 == latest["sha256"] and r.platform == self.plat and r.arch == self.arch \
                    and r.version not in index.revoked_versions:
                return r
        raise UpdateError("The release is no longer listed in the signed index (it may have been withdrawn). Check for updates again.")

    def _download_guarded(self, release: Release) -> None:
        try:
            self._download(release)
        except (NetError, ArchiveError, RuntimeError_, UpdateError, TrustError, ManifestError, OSError) as exc:
            offline = isinstance(exc, NetError) and exc.offline
            self._set(state="offline" if offline else "failed", message=str(exc), error=str(exc), error_kind="offline" if offline else "failed")
            self.status_file.add_history("download", release.version, False, str(exc))
        except Exception as exc:  # never leave the UI stuck on "downloading"
            self._set(state="failed", message=f"{type(exc).__name__}: {exc}", error=str(exc), error_kind="failed")
            self.status_file.add_history("download", release.version, False, f"{type(exc).__name__}: {exc}")
        finally:
            self._busy = None

    def _download(self, release: Release) -> None:
        lay = self.layout
        lay.ensure()
        existing = lay.release_dir(release.version)
        if existing.exists() and self.store.has(release.version):
            self._set(state="staged", staged_version=release.version, message=f"Version {release.version} is already staged.", error=None, error_kind=None)
            return
        # 1. space
        need = max(MIN_FREE_BYTES, release.size * FREE_SPACE_FACTOR)
        free = shutil.disk_usage(lay.root if lay.root.exists() else lay.root.parent).free
        if free < need:
            raise UpdateError(f"Not enough free disk space for this update: {need // (1 << 20)} MB needed, {free // (1 << 20)} MB available. Free some space and try again.")
        stage = lay.staging / release.version
        stage.mkdir(parents=True, exist_ok=True)
        artifact = stage / f"audiobook-studio-{release.version}.tar.gz"
        # 2. download (resumable)
        self._set(state="downloading", message=f"Downloading {release.version}", target_version=release.version, error=None, error_kind=None)
        self._phase("download", "running", 0, "Starting")
        last = {"t": 0.0}

        def prog(done: int, total: int) -> None:
            if now() - last["t"] > 0.4:
                last["t"] = now()
                self._phase("download", "running", round(100 * done / total, 1), f"{done // (1 << 20)} of {total // (1 << 20)} MB")

        self.repo.download(release.url, artifact, release.size, progress=prog, cancelled=self._cancel.is_set)
        self._phase("download", "done", 100, "Downloaded")
        # 3. integrity
        self._set(state="verifying", message="Verifying the download")
        self._phase("verify", "running", 50, "Checking SHA-256 and size against the signed release index")
        digest = hashlib.sha256()
        with open(artifact, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        if artifact.stat().st_size != release.size or digest.hexdigest() != release.sha256:
            artifact.unlink(missing_ok=True)
            self._phase("verify", "failed", None, "Checksum mismatch")
            raise UpdateError("The downloaded file does not match the signed release (checksum/size mismatch). It was deleted and nothing was installed.")
        self._phase("verify", "done", 100, "Matches the signed release index")
        # 4. stage
        self._set(state="staging", message="Unpacking the update next to the running version")
        self._phase("stage", "running", 10, "Extracting")
        tmp = lay.releases / f".stage-{release.version}-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            safe_extract(artifact, tmp, expected_top=f"audiobook-studio-{release.version}")
            self._phase("stage", "running", 50, "Verifying every file against the release manifest")
            verify_tree_manifest(tmp)
            self._check_release_identity(tmp, release)
            if release.runtime_id:
                self._phase("stage", "running", 70, "Preparing the Python environment")
                ensure_runtime(lay, tmp, release.runtime_id, runner=self.runner,
                               progress=lambda m: self._phase("stage", "running", 80, m))
            final = lay.release_dir(release.version)
            if final.exists():
                shutil.rmtree(final)
            os.replace(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
        shutil.rmtree(stage, ignore_errors=True)
        self._phase("stage", "done", 100, "Ready to install")
        self._set(state="staged", staged_version=release.version, message=f"Version {release.version} is downloaded, verified and ready to install.")
        self.status_file.add_history("stage", release.version, True, "Downloaded, verified and staged")

    def _check_release_identity(self, tree: Path, release: Release) -> None:
        """The unpacked code must be the version the signed index promised (a swapped archive with a valid hash cannot get here,
        but a mislabelled release can)."""
        init = (tree / "studio" / "__init__.py").read_text("utf-8")
        import re

        m = re.search(r'__version__\s*=\s*"([^"]+)"', init)
        if not m or m.group(1) != release.version:
            raise UpdateError(f"The package claims version {m.group(1) if m else 'unknown'} but the signed release says {release.version}; refusing it.")
        if parse_version(release.version) <= parse_version(self.installed):
            raise UpdateError("Refusing to stage a release that is not newer than the installed one.")

    def cancel(self) -> dict:
        """Cancel a running download, or withdraw a pending install request (releases the queue hold if the updater set it)."""
        self._cancel.set()
        req = self.layout.update_dir / "request.json"
        if req.exists():
            req.unlink(missing_ok=True)
            from ..scheduler import FileQueueScheduler

            FileQueueScheduler(self.ctx.run_dir).release()
            self._set(state="staged" if self.status_file.read().get("staged_version") else "available", message="Installation cancelled.", mode=None)
            self.status_file.add_history("cancel", None, True, "Pending installation cancelled")
        return self.status()

    # ------------------------------------------------------------------ install
    def request_install(self, mode: str, confirm_version: str) -> dict:
        """Record the user's explicit intent and hand off to the separate updater process."""
        if mode not in ("now", "after_job"):
            raise UpdateError("mode must be 'now' or 'after_job'", 422)
        st = self.status_file.read()
        staged = st.get("staged_version")
        if not self.layout.managed:
            raise UpdateError(self.status()["not_managed_reason"])
        if (self.layout.update_dir / "request.json").exists():
            raise UpdateError("An installation is already pending.")
        if not staged or st.get("state") not in ("staged",):
            raise UpdateError("No update is staged. Download and verify it first.")
        if confirm_version != staged:
            raise UpdateError(f"Confirmation does not match the staged version ({staged}).", 422)
        if not self.store.has(staged):
            raise UpdateError("The staged release is missing from disk. Download it again.")
        req = {"version": staged, "mode": mode, "requested_at": now(), "from_version": self.installed}
        atomic_write_text(self.layout.update_dir / "request.json", json.dumps(req))
        self._set(state="waiting", mode=mode, target_version=staged,
                  message="Installing after the current job finishes." if mode == "after_job" else "Installing at the next safe segment boundary.")
        self.status_file.add_history("install_requested", staged, True, f"mode={mode}")
        if self.launch_helper:
            try:
                self.launch_helper(staged, mode)
            except Exception as exc:
                (self.layout.update_dir / "request.json").unlink(missing_ok=True)
                self._set(state="failed", message=f"Could not start the updater: {exc}", error=str(exc), error_kind="failed")
                raise UpdateError(f"Could not start the updater process: {exc}") from exc
        return self.status()
