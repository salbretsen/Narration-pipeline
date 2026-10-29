"""On-disk layout of a managed installation. Everything replaceable lives under ``app/``; everything the
user owns lives under ``state/`` (and the storage root / model cache the user chose), so an update
can never overwrite books, voices, settings or model weights.

    <root>/app/releases/<version>/   immutable application code (one directory per release)
    <root>/app/current               symlink to the active release (what launchd runs)
    <root>/app/runtimes/<id>/        private Python virtual environments, shared by releases with the same lock
    <root>/app/tools/bin/            app-owned uv, ffmpeg, ffprobe
    <root>/state/                    AUDIOBOOK_STUDIO_HOME: config, data (SQLite), run, backups, update/
    <logs>                           ~/Library/Logs/Audiobook Studio on macOS
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

VERSION_DIR_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")


def default_root() -> Path:
    env = os.environ.get("AUDIOBOOK_STUDIO_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Audiobook Studio"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "audiobook-studio"


def default_logs(root: Path) -> Path:
    env = os.environ.get("AUDIOBOOK_STUDIO_LOGS")
    if env:
        return Path(env).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "Audiobook Studio"
    return root / "logs"


@dataclass(frozen=True)
class Layout:
    root: Path
    logs: Path

    @classmethod
    def discover(cls, root: str | Path | None = None) -> "Layout":
        r = Path(root).expanduser().resolve() if root else default_root()
        return cls(r, default_logs(r))

    # --- application side
    @property
    def app(self) -> Path:
        return self.root / "app"

    @property
    def releases(self) -> Path:
        return self.app / "releases"

    @property
    def current_link(self) -> Path:
        return self.app / "current"

    @property
    def pointer_file(self) -> Path:
        """Authoritative active-release pointer (the symlink is a convenience for launchd paths)."""
        return self.app / "current.json"

    @property
    def runtimes(self) -> Path:
        return self.app / "runtimes"

    @property
    def tools_bin(self) -> Path:
        return self.app / "tools" / "bin"

    @property
    def service_manifest(self) -> Path:
        return self.app / "service.json"

    # --- state side
    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def update_dir(self) -> Path:
        return self.state / "update"

    @property
    def staging(self) -> Path:
        return self.update_dir / "staging"

    @property
    def models(self) -> Path:
        return self.root / "models"

    def ensure(self) -> None:
        for d in (self.releases, self.runtimes, self.tools_bin, self.state, self.update_dir, self.staging, self.logs):
            d.mkdir(parents=True, exist_ok=True)

    @property
    def managed(self) -> bool:
        """True when this machine was set up by the managed installer (a release pointer exists)."""
        return self.pointer_file.exists()

    def release_dir(self, version: str) -> Path:
        if not VERSION_DIR_RE.match(version):
            raise ValueError(f"invalid version string {version!r}")
        return self.releases / version

    def read_pointer(self) -> dict:
        try:
            data = json.loads(self.pointer_file.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def current_version(self) -> str | None:
        v = self.read_pointer().get("current")
        return v if isinstance(v, str) and VERSION_DIR_RE.match(v) else None

    def service_info(self) -> dict:
        try:
            data = json.loads(self.service_manifest.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}
