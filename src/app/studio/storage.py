"""Storage location validation, disk usage and project relocation.

Behaviour chosen for path changes: **per-project path registry**. Every book records the
absolute directories it was created with. Changing the library location in Settings applies
to books imported afterwards; existing books stay where they are and remain findable. A book
can be moved explicitly ("Move project"), which copies, verifies every file by size and
SHA-256, then switches the database paths in one transaction. The old copy is kept until the
operator deletes it, so a failed or regretted move can always be rolled back.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import STORAGE_CATEGORIES
from .db import row, rows
from .util import dumps, loads, now, sha256_file

SYSTEM_PREFIXES = ("/System", "/usr", "/bin", "/sbin", "/etc", "/private/etc", "/private/var/db", "/dev", "/proc", "/Library", "/Applications", "/cores")


class PathRejected(ValueError):
    pass


def volume_of(p: Path) -> Path | None:
    parts = p.parts
    if len(parts) >= 3 and parts[0] == "/" and parts[1] == "Volumes":
        return Path("/Volumes") / parts[2]
    return None


def diskutil_info(vol: Path) -> dict:
    if sys.platform != "darwin":
        return {}
    try:
        out = subprocess.run(["/usr/sbin/diskutil", "info", "-plist", str(vol)], capture_output=True, timeout=10)
        if out.returncode != 0:
            return {}
        info = plistlib.loads(out.stdout)
        return {"removable": bool(info.get("RemovableMedia") or info.get("Ejectable")), "internal": bool(info.get("Internal")),
                "filesystem": info.get("FilesystemName") or info.get("FilesystemType"), "volume_name": info.get("VolumeName")}
    except Exception:
        return {}


def dir_usage(path: Path, budget_s: float = 2.0) -> tuple[int, bool]:
    """Total bytes under ``path``; stops after ``budget_s`` seconds (returns partial=True)."""
    total, t0 = 0, time.time()
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
        if time.time() - t0 > budget_s:
            return total, True
    return total, False


def validate_dir(raw: str, app_home: Path | None = None, create: bool = False, measure: bool = True) -> dict:
    """Validate an operator-supplied directory path. Never trusts the browser: the path is
    expanded, made absolute, resolved and checked on the server."""
    out = {"input": raw, "resolved": None, "ok": False, "exists": False, "writable": False, "free_bytes": None,
           "total_bytes": None, "usage_bytes": None, "usage_partial": False, "volume": None, "mounted": None,
           "removable": None, "errors": [], "warnings": []}
    raw = (raw or "").strip()
    if not raw:
        out["errors"].append("Path is empty.")
        return out
    if "\x00" in raw or any(ord(c) < 32 for c in raw):
        out["errors"].append("Path contains control characters.")
        return out
    p = Path(raw).expanduser()
    if not p.is_absolute():
        out["errors"].append("Use an absolute path such as ~/AudiobookLibrary or /Volumes/AudiobooksSSD/Library.")
        return out
    if ".." in Path(raw).parts:
        out["errors"].append("Path must not contain '..' components.")
        return out
    vol = volume_of(p)
    if vol is not None:
        out["volume"] = str(vol)
        out["mounted"] = os.path.ismount(vol)
        if not out["mounted"]:
            out["errors"].append(f"{vol} is not mounted. Connect the drive and try again.")
            return out
        out.update({k: v for k, v in diskutil_info(vol).items() if k == "removable"})
        if out.get("removable"):
            out["warnings"].append("Removable drive: if it is disconnected, jobs pause and resume automatically when it returns.")
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError) as exc:
        out["errors"].append(f"Cannot resolve path: {exc}")
        return out
    out["resolved"] = str(resolved)
    rs = str(resolved)
    if rs in ("/", str(Path.home())):
        out["errors"].append("Choose a dedicated folder, not the disk root or your home folder itself.")
        return out
    for pref in SYSTEM_PREFIXES:
        if rs == pref or rs.startswith(pref + "/"):
            out["errors"].append(f"System location {pref} is not allowed.")
            return out
    if app_home is not None:
        for sub in ("app", "venv", "backups"):
            forbidden = (app_home / sub).resolve()
            if resolved == forbidden or forbidden in resolved.parents:
                out["errors"].append("Do not store audiobook data inside the application code directory.")
                return out
    out["exists"] = resolved.exists()
    if out["exists"] and not resolved.is_dir():
        out["errors"].append("Path exists but is not a directory.")
        return out
    probe_dir = resolved
    if not out["exists"]:
        parent = next((q for q in resolved.parents if q.exists()), None)
        if parent is None:
            out["errors"].append("No existing parent directory.")
            return out
        if create:
            try:
                resolved.mkdir(parents=True, exist_ok=True)
                out["exists"] = True
            except OSError as exc:
                out["errors"].append(f"Cannot create directory: {exc}")
                return out
        else:
            out["warnings"].append("Directory does not exist yet; it will be created when saved.")
            probe_dir = parent
    if os.access(probe_dir, os.W_OK):
        if out["exists"]:
            test = resolved / f".studio-write-test-{os.getpid()}"
            try:
                test.write_bytes(b"ok")
                test.unlink()
                out["writable"] = True
            except OSError as exc:
                out["errors"].append(f"Directory is not writable: {exc}")
        else:
            out["writable"] = True
    else:
        out["errors"].append("You do not have write permission here.")
    try:
        du = shutil.disk_usage(probe_dir)
        out["free_bytes"], out["total_bytes"] = du.free, du.total
        if du.free < 5 * 1024**3:
            out["warnings"].append("Less than 5 GB free. A long audiobook needs several GB of intermediate audio.")
    except OSError:
        pass
    if measure and out["exists"]:
        out["usage_bytes"], out["usage_partial"] = dir_usage(resolved)
    out["ok"] = not out["errors"]
    return out


def storage_overview(ctx) -> dict:
    s = ctx.settings()
    cats = []
    for c in STORAGE_CATEGORIES:
        d = ctx.category_dir(c, s)
        info = validate_dir(str(d), ctx.home, create=False, measure=True)
        info["category"] = c
        info["override"] = getattr(s.storage, f"{c}_dir")
        cats.append(info)
    with ctx.db.session() as conn:
        books = rows(conn, "SELECT id, uid, title, storage_json FROM books WHERE archived=0 ORDER BY id")
    roots: dict[str, list] = {}
    for b in books:
        st = loads(b["storage_json"], {})
        seg_root = str(Path(st.get("segments", "")).parent) if st.get("segments") else "?"
        roots.setdefault(seg_root, []).append({"id": b["id"], "title": b["title"]})
    registry = []
    for root_path, bl in roots.items():
        p = Path(root_path)
        vol = volume_of(p)
        registry.append({"root": root_path, "books": bl, "available": p.exists() and (vol is None or os.path.ismount(vol)),
                         "volume": str(vol) if vol else None})
    return {
        "library_root": validate_dir(str(ctx.library_root(s)), ctx.home, measure=True),
        "categories": cats,
        "registry": registry,
        "app_home": str(ctx.home),
        "database": str(ctx.db.path),
        "model_cache": os.environ.get("HF_HOME") or str(ctx.cache_dir / "huggingface"),
        "native_chooser": sys.platform == "darwin",
        "policy": "per-project registry: new locations apply to books imported afterwards; existing books stay findable at their recorded paths and can be moved explicitly.",
    }


def choose_folder_native(prompt: str = "Choose a folder for audiobook data") -> str | None:
    """Open the real macOS folder chooser (only meaningful when the server runs on the same Mac)."""
    if sys.platform != "darwin":
        raise PathRejected("The native folder chooser is only available on macOS.")
    script = f'POSIX path of (choose folder with prompt "{prompt.replace(chr(34), "")}")'
    try:
        proc = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None  # cancelled
    return proc.stdout.strip().rstrip("/") or None


# ------------------------------------------------------------------ relocation


def move_book(ctx, book_id: int, new_root: str, progress=None) -> dict:
    """Copy a book's files under ``new_root``/<category>/<uid>, verify, then switch paths."""
    v = validate_dir(new_root, ctx.home, create=True, measure=False)
    if not v["ok"]:
        raise PathRejected("; ".join(v["errors"]))
    dest_root = Path(v["resolved"])
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        if not book:
            raise ValueError("book not found")
        busy = row(conn, "SELECT id FROM jobs WHERE book_id=? AND status IN ('running')", (book_id,))
        if busy:
            raise PathRejected("Pause the book's running job before moving it.")
    old = {k: Path(p) for k, p in loads(book["storage_json"], {}).items()}
    new = {k: dest_root / k / book["uid"] for k in old}
    for k in old:
        if old[k].resolve() == new[k].resolve():
            raise PathRejected("The book is already stored there.")
        if new[k].exists() and any(new[k].iterdir()):
            raise PathRejected(f"Destination {new[k]} already contains files.")
    copied: list[Path] = []
    try:
        for k, src in old.items():
            if not src.exists():
                new[k].mkdir(parents=True, exist_ok=True)
                continue
            for f in src.rglob("*"):
                if f.is_dir():
                    continue
                rel = f.relative_to(src)
                dst = new[k] / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(f, dst)
                copied.append(dst)
                if f.stat().st_size != dst.stat().st_size or sha256_file(f) != sha256_file(dst):
                    raise OSError(f"verification failed for {rel}")
            if progress:
                progress(f"copied {k}")
    except Exception:
        for f in copied:  # rollback the partial copy; originals were never touched
            f.unlink(missing_ok=True)
        raise

    def remap(p: str | None) -> str | None:
        if not p:
            return p
        for k in old:
            try:
                rel = Path(p).relative_to(old[k])
            except ValueError:
                continue
            return str(new[k] / rel)
        return p

    with ctx.db.tx() as conn:
        conn.execute("UPDATE books SET storage_json=?, cover_path=?, updated_at=? WHERE id=?",
                     (dumps({k: str(v_) for k, v_ in new.items()}), remap(book["cover_path"]), now(), book_id))
        for m in rows(conn, "SELECT id, stored_path FROM manuscripts WHERE book_id=?", (book_id,)):
            conn.execute("UPDATE manuscripts SET stored_path=? WHERE id=?", (remap(m["stored_path"]), m["id"]))
        for a in rows(conn, "SELECT a.id, a.wav_path FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=?", (book_id,)):
            conn.execute("UPDATE attempts SET wav_path=? WHERE id=?", (remap(a["wav_path"]), a["id"]))
        for r in rows(conn, "SELECT cr.id, cr.path FROM chapter_renders cr JOIN chapters c ON c.id=cr.chapter_id WHERE c.book_id=?", (book_id,)):
            conn.execute("UPDATE chapter_renders SET path=? WHERE id=?", (remap(r["path"]), r["id"]))
        for e in rows(conn, "SELECT id, path FROM exports WHERE book_id=?", (book_id,)):
            conn.execute("UPDATE exports SET path=? WHERE id=?", (remap(e["path"]), e["id"]))
        ctx.audit(conn, "book", book_id, "moved", {"from": {k: str(v_) for k, v_ in old.items()}, "to": {k: str(v_) for k, v_ in new.items()}, "files": len(copied)})
        ctx.event(conn, "storage", f"Moved '{book['title']}' to {dest_root} ({len(copied)} files verified). The old copy was kept.", book_id)
    return {"files": len(copied), "old": {k: str(v_) for k, v_ in old.items()}, "new": {k: str(v_) for k, v_ in new.items()}}
