"""Hardened extraction of a release archive (``.tar.gz`` only). Never uses ``extractall``.

Rejected: absolute paths, ``..`` or backslash components, drive letters, NUL bytes, symlinks, hardlinks, devices, FIFOs,
members outside the single top-level ``audiobook-studio-<version>/`` directory, duplicate names, too many files, too many
bytes (decompression bomb) and anything that is not a gzip stream. Permission bits are normalised (no setuid/setgid/world-write)."""
from __future__ import annotations

import hashlib
import os
import posixpath
import shutil
import tarfile
from pathlib import Path

MAX_FILES = 20000
MAX_UNPACKED = 1 << 30      # 1 GiB of unpacked code
MAX_MEMBER = 256 << 20


class ArchiveError(Exception):
    pass


def check_gzip(path: Path) -> None:
    with open(path, "rb") as fh:
        if fh.read(2) != b"\x1f\x8b":
            raise ArchiveError("The download is not a gzip-compressed tar archive.")


def _safe_member_path(name: str) -> str:
    if not name or "\x00" in name or "\\" in name or name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ArchiveError(f"Unsafe path in archive: {name!r}")
    norm = posixpath.normpath(name)
    if norm == ".." or norm.startswith("../") or norm.startswith("/") or any(p == ".." for p in name.split("/")):
        raise ArchiveError(f"Path traversal in archive: {name!r}")
    return norm


def safe_extract(archive: Path, dest: Path, expected_top: str | None = None) -> str:
    """Extract ``archive`` into the (new, empty) directory ``dest``, stripping the single top-level directory.
    Returns the top-level directory name. On any problem ``dest`` is removed and ArchiveError is raised."""
    check_gzip(archive)
    dest = Path(dest)
    if dest.exists():
        raise ArchiveError(f"{dest} already exists")
    dest.mkdir(parents=True)
    root = dest.resolve()
    total = 0
    seen: set[str] = set()
    top: str | None = None
    try:
        with tarfile.open(archive, "r:gz") as tf:
            count = 0
            for m in tf:
                count += 1
                if count > MAX_FILES:
                    raise ArchiveError("The archive contains too many files.")
                name = _safe_member_path(m.name)
                if m.issym() or m.islnk():
                    raise ArchiveError(f"Links are not allowed in a release archive: {m.name!r}")
                if not (m.isfile() or m.isdir()):
                    raise ArchiveError(f"Unsupported entry type in archive: {m.name!r}")
                parts = name.split("/")
                if top is None:
                    top = parts[0]
                    if expected_top and top != expected_top:
                        raise ArchiveError(f"Unexpected top-level directory {top!r} (expected {expected_top!r}).")
                elif parts[0] != top:
                    raise ArchiveError("The archive has more than one top-level entry.")
                if len(parts) == 1:
                    if not m.isdir():
                        raise ArchiveError("A file sits next to the top-level directory.")
                    continue
                rel = "/".join(parts[1:])
                if rel in seen:
                    raise ArchiveError(f"Duplicate entry in archive: {rel!r}")
                seen.add(rel)
                target = (root / rel).resolve()
                if root != target and root not in target.parents:
                    raise ArchiveError(f"Path escapes the extraction directory: {m.name!r}")
                if m.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if m.size > MAX_MEMBER:
                    raise ArchiveError(f"A file in the archive is implausibly large: {m.name!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tf.extractfile(m)
                if src is None:
                    raise ArchiveError(f"Cannot read {m.name!r}")
                written = 0
                with open(target, "wb") as out:
                    while True:
                        chunk = src.read(1 << 17)
                        if not chunk:
                            break
                        written += len(chunk)
                        total += len(chunk)
                        if written > m.size or total > MAX_UNPACKED:
                            raise ArchiveError("The archive expands beyond the allowed size (possible decompression bomb).")
                        out.write(chunk)
                os.chmod(target, 0o755 if (m.mode & 0o111) else 0o644)
        if top is None or not seen:
            raise ArchiveError("The archive is empty.")
    except (tarfile.TarError, EOFError, OSError, ValueError) as exc:
        shutil.rmtree(dest, ignore_errors=True)
        if isinstance(exc, ArchiveError):
            raise
        raise ArchiveError(f"The archive is damaged or unreadable ({type(exc).__name__}: {exc}).") from exc
    except ArchiveError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return top


def verify_tree_manifest(tree: Path) -> int:
    """Check ``MANIFEST.sha256`` (``<sha256>  <relative path>`` lines) against the extracted files; returns the file count.
    Every file in the tree except the manifest itself must be listed, so nothing can be added unnoticed."""
    mf = Path(tree) / "MANIFEST.sha256"
    if not mf.exists():
        raise ArchiveError("The release has no MANIFEST.sha256.")
    listed: dict[str, str] = {}
    for line in mf.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, rel = line.partition("  ")
        rel = rel.strip()
        if not digest or not rel or rel.startswith("*"):
            rel = rel.lstrip("*")
        _safe_member_path(rel)
        listed[rel] = digest
    root = Path(tree).resolve()
    for rel, digest in listed.items():
        p = (root / rel).resolve()
        if root not in p.parents or not p.is_file():
            raise ArchiveError(f"The release manifest lists a missing file: {rel}")
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        if h.hexdigest() != digest:
            raise ArchiveError(f"File does not match the release manifest: {rel}")
    actual = {str(p.relative_to(root)).replace(os.sep, "/") for p in root.rglob("*") if p.is_file()} - {"MANIFEST.sha256"}
    extra = actual - set(listed)
    if extra:
        raise ArchiveError(f"The release contains files not listed in its manifest: {sorted(extra)[:3]}")
    return len(listed)
