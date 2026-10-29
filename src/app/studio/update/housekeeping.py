"""Conservative clean-up after a successful install or rollback. It only ever removes things that can be re-created:
tool archives the installer downloaded (``app/downloads``), private Python environments (``app/runtimes/<id>``) that no kept release
names, and empty staging folders. It never touches ``releases/`` (that is ``ReleaseStore.prune``), ``state/``, books, voices or models."""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from .layout import Layout
from .store import ReleaseStore

RUNTIME_ID_RE = re.compile(r"^[0-9a-f]{16}$")
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".zip", ".part", ".tmp")
RECENT_SECONDS = 3600      # something touched in the last hour may be a build in progress elsewhere; leave it


def referenced_runtimes(layout: Layout) -> set[str]:
    """Runtime ids named by every release still on disk (current, previous and any staged one), plus the one ``app/venv`` points at."""
    keep: set[str] = set()
    for v in ReleaseStore(layout).installed():
        try:
            rid = json.loads((layout.release_dir(v) / "release.json").read_text("utf-8")).get("runtime_id")
        except (OSError, ValueError):
            rid = None
        if rid:
            keep.add(str(rid))
    try:
        target = (layout.app / "venv").resolve()
        for part in target.parts:
            if RUNTIME_ID_RE.match(part):
                keep.add(part)
    except OSError:
        pass
    return keep


def prune_runtimes(layout: Layout, now: float | None = None) -> list[str]:
    if not layout.runtimes.exists():
        return []
    store = ReleaseStore(layout)
    if not store.current():
        return []                    # without a known active release we cannot tell what is in use: remove nothing
    keep = referenced_runtimes(layout)
    t = now if now is not None else time.time()
    gone = []
    for p in sorted(layout.runtimes.iterdir()):
        if not p.is_dir() or p.is_symlink() or not RUNTIME_ID_RE.match(p.name) or p.name in keep:
            continue
        try:
            if t - p.stat().st_mtime < RECENT_SECONDS:
                continue
        except OSError:
            continue
        shutil.rmtree(p, ignore_errors=True)
        gone.append(p.name)
    return gone


def tidy_downloads(layout: Layout) -> list[str]:
    d = layout.app / "downloads"
    if not d.is_dir() or d.is_symlink():
        return []
    gone = []
    for p in sorted(d.iterdir()):
        if p.is_file() and not p.is_symlink() and p.name.lower().endswith(ARCHIVE_SUFFIXES):
            try:
                p.unlink()
                gone.append(p.name)
            except OSError:
                pass
    try:
        d.rmdir()                    # only succeeds when empty
    except OSError:
        pass
    return gone


def tidy_staging(layout: Layout) -> int:
    n = 0
    if layout.staging.is_dir():
        for p in layout.staging.iterdir():
            if p.is_dir() and not any(p.iterdir()):
                try:
                    p.rmdir()
                    n += 1
                except OSError:
                    pass
    return n


def run(layout: Layout) -> dict:
    """Never raises: housekeeping must not turn a successful install into a failure."""
    out: dict = {"downloads": [], "runtimes": [], "staging": 0}
    for key, fn in (("downloads", tidy_downloads), ("runtimes", prune_runtimes), ("staging", tidy_staging)):
        try:
            out[key] = fn(layout)
        except Exception:
            pass
    return out
