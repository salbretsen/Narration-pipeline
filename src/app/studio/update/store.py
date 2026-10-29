"""Immutable release directories and the atomic active-release pointer."""
from __future__ import annotations

import json
import os
import secrets
import shutil
from pathlib import Path

from ..util import atomic_write_text, now
from .layout import VERSION_DIR_RE, Layout
from .manifest import parse_version


class ReleaseStore:
    def __init__(self, layout: Layout):
        self.layout = layout

    def installed(self) -> list[str]:
        if not self.layout.releases.exists():
            return []
        out = [p.name for p in self.layout.releases.iterdir() if p.is_dir() and VERSION_DIR_RE.match(p.name) and (p / "studio" / "__init__.py").exists()]
        return sorted(out, key=parse_version)

    def has(self, version: str) -> bool:
        return version in self.installed()

    def current(self) -> str | None:
        return self.layout.current_version()

    def previous(self) -> str | None:
        v = self.layout.read_pointer().get("previous")
        return v if isinstance(v, str) and VERSION_DIR_RE.match(v) else None

    def activate(self, version: str) -> str | None:
        """Atomically make ``version`` the active release. Returns the previously active version.
        The pointer file is authoritative; the ``current`` symlink (used by launchd paths) is replaced with rename(2) so it is never missing."""
        target = self.layout.release_dir(version)
        if not (target / "studio" / "__init__.py").exists():
            raise FileNotFoundError(f"release {version} is not installed")
        old = self.current()
        data = dict(self.layout.read_pointer())
        data.update({"current": version, "previous": old if old != version else data.get("previous"), "switched_at": now()})
        self._swap_symlink(target)
        atomic_write_text(self.layout.pointer_file, json.dumps(data, indent=2) + "\n")
        return old

    def _swap_symlink(self, target: Path, link: Path | None = None, directory: bool = True) -> None:
        link = link or self.layout.current_link
        tmp = link.with_name(f".{link.name}.{secrets.token_hex(4)}")
        try:
            os.symlink(target, tmp, target_is_directory=directory)
        except (OSError, NotImplementedError):
            return  # e.g. Windows without symlink rights: the pointer file still decides
        try:
            os.replace(tmp, link)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass

    def link_python(self, python: Path) -> None:
        """Keep ``app/venv-python`` (the interpreter launchd and the CLI run) pointing at the active release's private environment."""
        self._swap_symlink(Path(python), self.layout.app / "venv-python", directory=False)

    def remove(self, version: str) -> None:
        if version in (self.current(), self.previous()):
            raise ValueError("refusing to remove the active or previous release")
        shutil.rmtree(self.layout.release_dir(version), ignore_errors=True)

    def prune(self, keep: int = 3) -> list[str]:
        """Delete the oldest releases, always keeping the active and previous ones."""
        keep_set = {self.current(), self.previous()} - {None}
        removable = [v for v in self.installed() if v not in keep_set]
        extra = len(self.installed()) - max(keep, len(keep_set))
        gone = []
        for v in removable[: max(0, extra)]:
            self.remove(v)
            gone.append(v)
        return gone
