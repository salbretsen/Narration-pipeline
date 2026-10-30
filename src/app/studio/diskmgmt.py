"""Disk management (roadmap C3): usage report, delete/purge, and book/database backup and restore.

Safety rules shared by every destructive function here:

* nothing is deleted unless the caller passes ``confirm=True`` (without it a :class:`NeedsConfirmation` carrying a preview is raised);
* a book with a queued, running or paused job is refused (HTTP 409);
* every file path read from the database is resolved and must lie inside one of the book's own registered storage directories
  (``pipeline.book_dirs``) before it is touched, so a tampered row can never reach unrelated files;
* every action is written to the audit log.

Backup format ("audiobook-studio-book-backup", version 1) is documented in docs/03-api-reference.md and docs/06-deployment-and-operations.md.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import zipfile
from pathlib import Path

from .config import STORAGE_CATEGORIES
from .db import LATEST_VERSION, row, rows
from .pipeline import book_dirs, resolve_take
from .services import NeedsConfirmation, ServiceError, _book_storage, display_title, siblings, work_key
from .storage import SYSTEM_PREFIXES
from .util import dumps, new_uid, now, safe_filename
from .worker import refresh_book_state

BACKUP_FORMAT = "audiobook-studio-book-backup"
BACKUP_VERSION = 1
BUSY_STATUSES = ("queued", "running", "paused")
PURGE_SCOPES = ("takes", "exports", "masters")
_BACKUP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,150}\.zip$")
_UID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_NO_COMPRESS = {".mp3", ".m4b", ".m4a", ".zip", ".png", ".jpg", ".jpeg", ".flac"}
MAX_RESTORE_BYTES = 200 * 1024**3

# Take (attempt) and per-book category labels shown in the usage report.
BOOK_CATEGORY_LABELS = {
    "manuscripts": "Manuscripts", "references": "Voices (book references)", "segments": "Segments (selected takes)",
    "alternates": "Takes / attempts (alternates)", "reports": "Reports", "masters": "Masters", "exports": "Exports",
}


# ------------------------------------------------------------------ helpers


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def file_size(path) -> int:
    try:
        return os.lstat(path).st_size if path and os.path.isfile(path) else 0
    except OSError:
        return 0


def _free(path: Path) -> dict | None:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        du = shutil.disk_usage(p)
        return {"path": str(path), "free_bytes": du.free, "total_bytes": du.total, "used_bytes": du.used}
    except OSError:
        return None


def _inside(path: Path, roots) -> Path | None:
    """The real path of ``path`` if it lies strictly inside one of ``roots`` (symlinks resolved), else None."""
    rp = Path(os.path.realpath(path))
    for r in roots:
        rr = Path(os.path.realpath(r))
        if rr in rp.parents:
            return rp
    return None


def _book(conn, book_id: int) -> dict:
    b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
    if not b:
        raise ServiceError("Book not found", 404)
    return b


def _assert_idle(conn, book_id: int, what: str) -> None:
    if row(conn, f"SELECT id FROM jobs WHERE book_id=? AND status IN ({','.join('?' * len(BUSY_STATUSES))})", (book_id, *BUSY_STATUSES)):
        raise ServiceError(f"Cannot {what} while the book has a queued, rendering or paused job. Cancel or finish it in the Queue first.", 409)


def _need_confirm(confirm: bool, message: str, preview: dict) -> None:
    if confirm is not True:
        raise NeedsConfirmation(message + " Send confirm=true to proceed.", preview)


def _safe_book_dirs(ctx, book: dict) -> dict[str, Path]:
    """The book's own storage directories, validated: must be named after the book uid, deep enough and not a system location."""
    out: dict[str, Path] = {}
    for cat, d in book_dirs(book).items():
        rd = Path(os.path.realpath(d))
        s = str(rd)
        if rd.name != book["uid"] or len(rd.parts) < 3 or any(s == p or s.startswith(p + "/") for p in SYSTEM_PREFIXES):
            raise ServiceError(f"Refusing to touch {d}: not a per-book storage folder.", 422)
        if rd == Path(os.path.realpath(ctx.category_dir(cat))):
            raise ServiceError(f"Refusing to touch {d}: it is a category root.", 422)
        out[cat] = rd
    return out


def _remove_file(path: Path, roots) -> int:
    """Delete one file (and its .json sidecar) if it lies inside ``roots``. Returns bytes freed."""
    freed = 0
    real = _inside(path, roots)
    if real is None:
        return 0
    for p in (real, Path(str(real) + ".json")):
        try:
            if p.is_file():
                freed += p.stat().st_size
                os.chmod(p, 0o600)
                p.unlink()
        except OSError:
            pass
    return freed


def _force_remove(func, path, _exc) -> None:
    """rmtree error hook: manuscripts are stored read-only (0444); lift that on the one file and retry."""
    os.chmod(path, 0o700)
    func(path)


def _prune_empty(root: Path) -> None:
    for d, _dirs, _files in os.walk(root, topdown=False):
        if Path(d) != root:
            try:
                os.rmdir(d)
            except OSError:
                pass


# ------------------------------------------------------------------ usage

USAGE_TTL_S = 60.0                      # how long GET /api/disk/usage may serve a measurement (refresh=1 forces a new one)
_MEASURE_LOCK = threading.Lock()        # one measurement at a time; concurrent callers share its result
_USAGE_CACHE: dict[str, tuple[float, dict]] = {}   # library key -> (measured_at, payload)
_USAGE_GEN: dict[str, int] = {}         # bumped by invalidate_usage so an in-flight measurement cannot store a stale result


def _usage_key(ctx) -> str:
    return str(ctx.data_dir)


def invalidate_usage(ctx=None) -> None:
    """Forget the cached usage measurement (all libraries when ``ctx`` is None). Called by every action that changes what is on disk."""
    keys = set(_USAGE_CACHE) | set(_USAGE_GEN) if ctx is None else {_usage_key(ctx)}
    for k in keys:
        _USAGE_GEN[k] = _USAGE_GEN.get(k, 0) + 1
        _USAGE_CACHE.pop(k, None)


def invalidates_usage(fn):
    """Decorator for actions that change disk contents (first argument is ctx): the usage cache is dropped once the action has run, whether
    it succeeded or failed half way. A confirmation preview (NeedsConfirmation) changed nothing and leaves the cache alone."""
    @functools.wraps(fn)
    def wrapper(ctx, *args, **kwargs):
        try:
            result = fn(ctx, *args, **kwargs)
        except NeedsConfirmation:
            raise
        except BaseException:
            invalidate_usage(ctx)
            raise
        invalidate_usage(ctx)
        return result
    return wrapper


def _walk_files(path: Path) -> tuple[int, dict[str, int]]:
    """(total bytes, {file path: bytes}) of every file under ``path``: one lstat per file."""
    total, sizes = 0, {}
    for root, _dirs, files in os.walk(path):
        for f in files:
            full = os.path.join(root, f)
            try:
                n = os.lstat(full).st_size
            except OSError:
                continue
            total += n
            sizes[full] = n
    return total, sizes


def _purgeable(conn, book: dict, dirs: dict[str, Path], sizes: dict[str, int] | None = None) -> dict:
    """Superseded takes of one book: count and bytes. ``sizes`` ({path: bytes}, from the segments/alternates walk) avoids a second stat
    per file; takes that are not in it fall back to the old existence-checked lookup."""
    keep = {r["selected_attempt_id"] for r in rows(conn, "SELECT selected_attempt_id FROM segments WHERE book_id=? AND selected_attempt_id IS NOT NULL", (book["id"],))}
    atts = rows(conn, "SELECT a.id, a.wav_path FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=? AND a.status!='in_progress'", (book["id"],))
    return _purgeable_from(book, atts, keep, sizes or {})


def _purgeable_from(book: dict, atts: list[dict], keep: set, sizes: dict[str, int]) -> dict:
    n, size = 0, 0
    for a in atts:
        if a["id"] in keep:
            continue
        n += 1
        if a["wav_path"]:
            hit = sizes.get(str(Path(a["wav_path"])))
            size += hit if hit is not None else file_size(resolve_take(book, a))
    return {"takes": n, "takes_bytes": size}


def usage(ctx, max_age: float = 0.0, refresh: bool = False) -> dict:
    """Per-book and global disk usage plus free space of the library volume and model cache.

    Walking every file is the slow part, so the result carries ``measured_at`` (epoch seconds), ``age_s`` and ``cached``. With the default
    ``max_age=0`` it always measures; the HTTP route passes ``max_age=USAGE_TTL_S`` so repeated page views reuse the last measurement, and
    ``refresh=True`` (``?refresh=1``) forces a new one. Disk-changing actions drop the cache (``invalidates_usage``)."""
    key = _usage_key(ctx)
    t_req = time.time()

    def usable(hit) -> bool:
        if hit is None:
            return False
        if hit[0] >= t_req:                    # measured after this call began (another caller's fresh result)
            return True
        return not refresh and max_age > 0 and (time.time() - hit[0]) <= max_age

    hit = _USAGE_CACHE.get(key)
    cached = usable(hit)
    if not cached:
        with _MEASURE_LOCK:
            hit = _USAGE_CACHE.get(key)
            cached = usable(hit)
            if not cached:
                gen = _USAGE_GEN.get(key, 0)
                started = time.time()
                payload = _measure(ctx)
                hit = (started, payload)
                if _USAGE_GEN.get(key, 0) == gen:
                    _USAGE_CACHE[key] = hit
    return {**hit[1], "measured_at": hit[0], "age_s": round(max(0.0, time.time() - hit[0]), 1), "cached": cached}


def _measure(ctx) -> dict:
    s = ctx.settings()
    with ctx.db.session() as conn:
        books_rows = rows(conn, "SELECT * FROM books ORDER BY id")
        voice_names = {r["id"]: r["name"] for r in rows(conn, "SELECT id, name FROM voices")}
        busy_ids = {r["book_id"] for r in rows(conn, "SELECT DISTINCT book_id FROM jobs WHERE status IN ('queued','running','paused') AND book_id IS NOT NULL")}
        keep_by: dict[int, set] = {}
        for r in rows(conn, "SELECT book_id, selected_attempt_id FROM segments WHERE selected_attempt_id IS NOT NULL"):
            keep_by.setdefault(r["book_id"], set()).add(r["selected_attempt_id"])
        atts_by: dict[int, list] = {}
        for r in rows(conn, "SELECT s.book_id, a.id, a.wav_path FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE a.status!='in_progress'"):
            atts_by.setdefault(r["book_id"], []).append(r)
        vfiles = set()
        for v in rows(conn, "SELECT ref_audio_path, ref_original_path FROM voice_versions"):
            for k in ("ref_audio_path", "ref_original_path"):
                if v[k]:
                    vfiles.add(v[k])
        for v in rows(conn, "SELECT ref_audio_path, ref_original_path FROM voice_refs"):  # emotion reference bank clips
            for k in ("ref_audio_path", "ref_original_path"):
                if v[k]:
                    vfiles.add(v[k])
    books = []
    totals = {c: 0 for c in STORAGE_CATEGORIES}
    for b in books_rows:
        dirs = book_dirs(b)
        cats, total = {}, 0
        take_sizes: dict[str, int] = {}
        for c in STORAGE_CATEGORIES:
            if c not in dirs:
                size = 0
            elif c in ("segments", "alternates"):
                size, files = _walk_files(dirs[c])   # the takes live here: keep the per-file sizes for the purgeable estimate
                take_sizes.update(files)
            else:
                size = dir_size(dirs[c])
            cats[c] = size
            totals[c] += size
            total += size
        purge = _purgeable_from(b, atts_by.get(b["id"], []), keep_by.get(b["id"], set()), take_sizes)
        narrator = voice_names.get(b["voice_id"])
        books.append({"id": b["id"], "uid": b["uid"], "title": b["title"], "narrator": narrator, "display_title": display_title(b["title"], narrator),
                      "work_uid": work_key(b), "archived": bool(b["archived"]), "state": b["state"],
                      "categories": cats, "total_bytes": total, "purgeable": {**purge, "exports_bytes": cats.get("exports", 0), "masters_bytes": cats.get("masters", 0)},
                      "busy": b["id"] in busy_ids})
    voices_bytes = sum(file_size(p) for p in vfiles)
    models = Path(s.models.cache_dir or os.environ.get("HF_HOME") or (ctx.cache_dir / "huggingface")).expanduser()
    db_bytes = sum(file_size(str(ctx.db.path) + suf) for suf in ("", "-wal", "-shm"))
    backups_dir = ctx.backups_dir
    other = {
        "voices": voices_bytes,
        "recordings": dir_size(ctx.data_dir / "recordings"),
        "previews": dir_size(ctx.data_dir / "previews"),
        "imports": dir_size(ctx.data_dir / "imports"),
        "database": db_bytes,
        "backups": dir_size(backups_dir),
        "logs": dir_size(ctx.logs_dir),
        "model_cache": dir_size(models),
    }
    return {
        "books": books,
        "category_labels": BOOK_CATEGORY_LABELS,
        "category_totals": totals,
        "other": other,
        "library_bytes": sum(totals.values()),
        "free": {"library": _free(ctx.library_root(s)), "model_cache": _free(models), "backups": _free(backups_dir)},
        "paths": {"library": str(ctx.library_root(s)), "model_cache": str(models), "backups": str(backups_dir)},
        "backups": list_backups(ctx),
    }


# ------------------------------------------------------------------ delete / purge

_BOOK_DELETE_ORDER = [
    ("ab_votes", "book_id=?"),
    ("direction_log", "segment_id IN (SELECT id FROM segments WHERE book_id=?)"),
    ("directions", "book_id=?"),
    ("review_actions", "segment_id IN (SELECT id FROM segments WHERE book_id=?)"),
    ("attempts", "segment_id IN (SELECT id FROM segments WHERE book_id=?)"),
    ("nar_spans", "book_id=?"),
    ("story_notes", "book_id=?"),
    ("characters", "book_id=?"),
    ("seg_edits", "book_id=?"),
    ("segment_history", "book_id=?"),
    ("chapter_text", "book_id=?"),
    ("chapter_renders", "chapter_id IN (SELECT id FROM chapters WHERE book_id=?)"),
    ("segments", "book_id=?"),
    ("chapters", "book_id=?"),
    ("analysis_stages", "book_id=?"),
    ("exports", "book_id=?"),
    ("releases", "book_id=?"),
    ("manuscripts", "book_id=?"),
    ("pronunciations", "book_id=?"),
    ("job_logs", "book_id=? OR job_id IN (SELECT id FROM jobs WHERE book_id=?)"),
    ("jobs", "book_id=?"),
    ("events", "book_id=?"),
]


@invalidates_usage
def delete_book(ctx, book_id: int, confirm: bool = False, actor: str = "operator") -> dict:
    with ctx.db.session() as conn:
        b = _book(conn, book_id)
        _assert_idle(conn, book_id, "delete it")
    dirs = _safe_book_dirs(ctx, b)
    sizes = {c: dir_size(d) if d.exists() else 0 for c, d in dirs.items()}
    with ctx.db.session() as conn:
        narrator = (row(conn, "SELECT name FROM voices WHERE id=?", (b["voice_id"],)) or {}).get("name")
        others = [x for x in siblings(conn, b) if x["id"] != book_id]
    shown = display_title(b["title"], narrator)
    _need_confirm(confirm, f"This permanently deletes '{shown}' with all its audio, exports and history."
                  + (f" The other {len(others)} narrator version(s) of this book are not affected." if others else ""),
                  {"book": b["title"], "narrator": narrator, "display_title": shown, "other_versions": len(others), "bytes": sum(sizes.values()), "categories": sizes})
    with ctx.db.tx() as conn:
        _assert_idle(conn, book_id, "delete it")
        imports = rows(conn, "SELECT id, stored_path FROM imports WHERE consumed_book_id=?", (book_id,))
        for table, where in _BOOK_DELETE_ORDER:
            n_params = where.count("?")
            conn.execute(f"DELETE FROM {table} WHERE {where}", (book_id,) * n_params)
        conn.execute("DELETE FROM imports WHERE consumed_book_id=?", (book_id,))
        conn.execute("DELETE FROM books WHERE id=?", (book_id,))
        ctx.audit(conn, "book", book_id, "deleted", {"title": b["title"], "uid": b["uid"], "bytes": sum(sizes.values())}, actor)
        ctx.event(conn, "disk", f"Deleted book '{b['title']}' ({sum(sizes.values())} bytes freed).", None, "warning")
    freed, errors = 0, []
    for cat, d in dirs.items():
        if d.exists():
            try:
                shutil.rmtree(d, onerror=_force_remove)
                freed += sizes[cat]
            except OSError as exc:
                errors.append(f"{d}: {exc}")
    imp_root = ctx.imports_dir()
    for imp in imports:
        if imp["stored_path"]:
            freed += _remove_file(Path(imp["stored_path"]), [imp_root])
    return {"deleted": True, "title": b["title"], "freed_bytes": freed, "errors": errors}


@invalidates_usage
def delete_work(ctx, book_id: int, confirm: bool = False, actor: str = "operator") -> dict:
    """Remove a book entirely: every narrator version of its manuscript, with all audio, exports and history."""
    with ctx.db.session() as conn:
        b = _book(conn, book_id)
        versions = siblings(conn, b)
        for v in versions:
            _assert_idle(conn, v["id"], f"remove the book (its '{v['title']}' version has a queued, rendering or paused job)")
        narrators = {v["id"]: (row(conn, "SELECT name FROM voices WHERE id=?", (v["voice_id"],)) or {}).get("name") for v in versions}
    sizes = {}
    for v in versions:
        sizes[v["id"]] = sum(dir_size(d) if d.exists() else 0 for d in _safe_book_dirs(ctx, v).values())
    _need_confirm(confirm, f"This permanently removes '{b['title']}' and all {len(versions)} narrator version(s) of it, with all their audio, exports and history.",
                  {"book": b["title"], "bytes": sum(sizes.values()),
                   "versions": [{"id": v["id"], "narrator": narrators[v["id"]], "bytes": sizes[v["id"]]} for v in versions]})
    freed, errors = 0, []
    for v in versions:
        r = delete_book(ctx, v["id"], True, actor)
        freed += r["freed_bytes"]
        errors += r["errors"]
    with ctx.db.tx() as conn:
        ctx.audit(conn, "book", book_id, "work_removed", {"title": b["title"], "versions": len(versions), "bytes": freed}, actor)
    return {"deleted": True, "title": b["title"], "versions_deleted": len(versions), "freed_bytes": freed, "errors": errors}


@invalidates_usage
def purge(ctx, book_id: int, scope: str, confirm: bool = False, actor: str = "operator") -> dict:
    if scope not in PURGE_SCOPES:
        raise ServiceError(f"scope must be one of {', '.join(PURGE_SCOPES)}", 422)
    with ctx.db.session() as conn:
        b = _book(conn, book_id)
        _assert_idle(conn, book_id, "purge it")
    dirs = _safe_book_dirs(ctx, b)
    if scope == "takes":
        plan = _plan_takes(ctx, b, dirs)
    else:
        plan = _plan_files(ctx, b, dirs, scope)
    preview = {"scope": scope, "items": len(plan["rows"]), "bytes": plan["bytes"]}
    if scope == "takes":
        preview["kept_selected"] = plan["kept"]
    _need_confirm(confirm, f"This permanently removes {len(plan['rows'])} {scope} ({plan['bytes']} bytes) from '{b['title']}'.", preview)
    freed = 0
    with ctx.db.tx() as conn:
        _assert_idle(conn, book_id, "purge it")
        table = {"takes": "attempts", "exports": "exports", "masters": "chapter_renders"}[scope]
        for r in plan["rows"]:
            conn.execute(f"DELETE FROM {table} WHERE id=?", (r["id"],))
        if scope == "masters":
            refresh_book_state(conn, book_id)
        ctx.audit(conn, "book", book_id, f"purge_{scope}", {"items": len(plan["rows"]), "bytes": plan["bytes"]}, actor)
        ctx.event(conn, "disk", f"Purged {len(plan['rows'])} {scope} from '{b['title']}'.", book_id)
    roots = [dirs["segments"], dirs["alternates"]] if scope == "takes" else [dirs[{"exports": "exports", "masters": "masters"}[scope]]]
    for f in plan["files"]:
        freed += _remove_file(f, roots)
    for r in roots:
        if r.exists():
            _prune_empty(r)
    return {"purged": True, "scope": scope, "items": len(plan["rows"]), "freed_bytes": freed}


def _plan_takes(ctx, book: dict, dirs: dict[str, Path]) -> dict:
    with ctx.db.session() as conn:
        keep = {r["selected_attempt_id"] for r in rows(conn, "SELECT selected_attempt_id FROM segments WHERE book_id=? AND selected_attempt_id IS NOT NULL", (book["id"],))}
        # approved segments always keep their selected take; a segment with no selection (never approved) keeps nothing special
        atts = rows(conn, "SELECT a.id, a.wav_path, a.status FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=?", (book["id"],))
    kept_paths = set()
    for a in atts:
        if a["id"] in keep and a["wav_path"]:
            t = resolve_take(book, a)
            kept_paths.add(os.path.realpath(t if t else a["wav_path"]))
            kept_paths.add(os.path.realpath(a["wav_path"]))
    victims, files, size = [], [], 0
    for a in atts:
        if a["id"] in keep or a["status"] == "in_progress":
            continue
        victims.append({"id": a["id"]})
        if a["wav_path"]:
            t = resolve_take(book, a) or Path(a["wav_path"])
            if os.path.realpath(t) in kept_paths:
                continue
            files.append(t)
            size += file_size(t)
    return {"rows": victims, "files": files, "bytes": size, "kept": len(keep)}


def _plan_files(ctx, book: dict, dirs: dict[str, Path], scope: str) -> dict:
    with ctx.db.session() as conn:
        if scope == "exports":
            rs = rows(conn, "SELECT id, path FROM exports WHERE book_id=?", (book["id"],))
        else:
            rs = rows(conn, "SELECT cr.id, cr.path FROM chapter_renders cr JOIN chapters c ON c.id=cr.chapter_id WHERE c.book_id=?", (book["id"],))
    files, size = [], 0
    for r in rs:
        p = Path(r["path"])
        files.append(p)
        size += file_size(p)
    return {"rows": [{"id": r["id"]} for r in rs], "files": files, "bytes": size}


# ------------------------------------------------------------------ backup / restore

# (table, how it hangs off the book). Order is the restore order.
_BACKUP_TABLES = [
    ("manuscripts", "book_id=?"), ("chapters", "book_id=?"), ("characters", "book_id=?"), ("segments", "book_id=?"),
    ("attempts", "segment_id IN (SELECT id FROM segments WHERE book_id=?)"), ("nar_spans", "book_id=?"), ("story_notes", "book_id=?"),
    ("directions", "book_id=?"), ("direction_log", "segment_id IN (SELECT id FROM segments WHERE book_id=?)"), ("seg_edits", "book_id=?"),
    ("segment_history", "book_id=?"), ("chapter_text", "book_id=?"), ("analysis_stages", "book_id=?"),
    ("review_actions", "segment_id IN (SELECT id FROM segments WHERE book_id=?)"),
    ("chapter_renders", "chapter_id IN (SELECT id FROM chapters WHERE book_id=?)"), ("exports", "book_id=?"),
]
_PATH_COLS = {"books": ["cover_path"], "manuscripts": ["stored_path"], "attempts": ["wav_path"], "chapter_renders": ["path"], "exports": ["path"]}
_PATH_REQUIRED = {"manuscripts"}  # rows kept even if their file is missing; other tables drop the row (or null the path for attempts)


def backups_root(ctx) -> Path:
    d = ctx.backups_dir / "books"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _backup_path(ctx, name: str) -> Path:
    if not isinstance(name, str) or not _BACKUP_NAME.match(name) or ".." in name:
        raise ServiceError("Invalid backup file name.", 422)
    root = backups_root(ctx)
    p = root / name
    if _inside(p, [root]) is None:
        raise ServiceError("Backup must be inside the backups folder.", 422)
    if not p.is_file():
        raise ServiceError("Backup not found", 404)
    return p


_MANIFEST_CACHE: dict[tuple, dict] = {}   # (path, mtime_ns, size) -> manifest summary: a backup zip is opened once, not on every page view


def _backup_meta(p: Path, key: tuple) -> dict:
    meta = _MANIFEST_CACHE.get(key)
    if meta is None:
        try:
            with zipfile.ZipFile(p) as zf:
                m = json.loads(zf.read("manifest.json"))
                meta = {"title": m.get("title"), "book_uid": m.get("book_uid"), "created_at": m.get("created_at"), "app_version": m.get("app_version")}
        except Exception:
            meta = {"title": None, "invalid": True}
        _MANIFEST_CACHE[key] = meta
    return dict(meta)


def list_backups(ctx) -> dict:
    out = {"books": [], "databases": []}
    root = backups_root(ctx)
    entries = []
    for p in root.glob("*.zip"):
        try:
            entries.append((p, p.stat()))
        except OSError:
            continue    # vanished while listing
    entries.sort(key=lambda e: e[1].st_mtime, reverse=True)
    seen = set()
    for p, st in entries:
        key = (str(p), st.st_mtime_ns, st.st_size)
        seen.add(key)
        out["books"].append({"name": p.name, "size_bytes": st.st_size, "modified": st.st_mtime, **_backup_meta(p, key)})
    prefix = str(root)
    for k in [k for k in _MANIFEST_CACHE if k[0].startswith(prefix) and k not in seen]:
        _MANIFEST_CACHE.pop(k, None)
    dbdir = ctx.backups_dir / "db"
    if dbdir.is_dir():
        for p in sorted(dbdir.glob("*.db"), key=lambda x: x.stat().st_mtime, reverse=True):
            out["databases"].append({"name": p.name, "size_bytes": p.stat().st_size, "modified": p.stat().st_mtime})
    return out


@invalidates_usage
def backup_database(ctx, actor: str = "operator") -> dict:
    """Consistent copy of studio.db (SQLite online backup API) into backups/db, verified with integrity_check."""
    dbdir = ctx.backups_dir / "db"
    dbdir.mkdir(parents=True, exist_ok=True)
    dest = dbdir / f"studio-v{ctx.db.schema_version()}-manual-{time.strftime('%Y%m%d-%H%M%S')}.db"
    with ctx.db.session() as src:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
    if ok != "ok":
        dest.unlink(missing_ok=True)
        raise ServiceError(f"The database copy failed its integrity check ({ok}); it was discarded.", 500)
    with ctx.db.tx() as conn:
        ctx.audit(conn, "database", None, "backup", {"file": dest.name, "bytes": dest.stat().st_size}, actor)
    return {"name": dest.name, "size_bytes": dest.stat().st_size, "path": str(dest), "integrity": ok}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@invalidates_usage
def backup_book(ctx, book_id: int, actor: str = "operator") -> dict:
    with ctx.db.session() as conn:
        b = _book(conn, book_id)
        if row(conn, "SELECT id FROM jobs WHERE book_id=? AND status='running'", (book_id,)):
            raise ServiceError("The book is rendering right now; back it up when the job has finished or is paused.", 409)
        conn.execute("BEGIN")  # one consistent read snapshot for every table
        tables = {name: rows(conn, f"SELECT * FROM {name} WHERE {where}", (book_id,)) for name, where in _BACKUP_TABLES}
        vref = []
        for v in rows(conn, "SELECT id, uid, name, builtin, current_version_id FROM voices"):
            vref.append(v)
        vvref = rows(conn, "SELECT id, voice_id FROM voice_versions")
        conn.execute("COMMIT")
    dirs = _safe_book_dirs(ctx, b)
    # path tokens: "@cat/<category>/<relative path>" so a restore can re-root every file
    roots = {c: d for c, d in dirs.items()}

    def tokenize(p: str | None) -> str | None:
        if not p:
            return None
        real = Path(os.path.realpath(p))
        for c, d in roots.items():
            if d in real.parents:
                return f"@cat/{c}/{real.relative_to(d).as_posix()}"
        return None

    def with_paths(table: str, rws: list[dict]) -> list[dict]:
        cols = _PATH_COLS.get(table)
        if not cols:
            return rws
        out = []
        for r in rws:
            r = dict(r)
            for c in cols:
                r[c] = tokenize(r.get(c))
            out.append(r)
        return out

    brow = dict(b)
    brow["cover_path"] = tokenize(b.get("cover_path"))
    # a take that resolve_take can find under the other folder is recorded where it really is
    for a in tables["attempts"]:
        if a.get("wav_path"):
            t = resolve_take(b, a)
            if t:
                a["wav_path"] = str(t)
    db_doc = {"book": brow, "tables": {k: with_paths(k, v) for k, v in tables.items()}, "voices": vref, "voice_versions": vvref}
    ts = time.strftime("%Y%m%d-%H%M%S")
    name = f"{safe_filename(b['title'], 'book', 60)}-{b['uid']}-{ts}.zip"
    dest = backups_root(ctx) / name
    part = dest.with_suffix(".zip.part")
    files_meta, total = [], 0
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as zf:
            for cat, d in dirs.items():
                if not d.exists():
                    continue
                for f in sorted(d.rglob("*")):
                    if f.is_symlink() or not f.is_file():
                        continue
                    rel = f.relative_to(d).as_posix()
                    arc = f"media/{cat}/{rel}"
                    ct = zipfile.ZIP_STORED if f.suffix.lower() in _NO_COMPRESS else zipfile.ZIP_DEFLATED
                    try:
                        size, digest = f.stat().st_size, _sha(f)
                        zf.write(f, arc, compress_type=ct)
                    except OSError:
                        continue  # vanished mid-backup
                    files_meta.append({"path": arc, "size": size, "sha256": digest})
                    total += size
            from . import __version__
            manifest = {"format": BACKUP_FORMAT, "version": BACKUP_VERSION, "app_version": __version__, "schema_version": LATEST_VERSION,
                        "created_at": now(), "book_uid": b["uid"], "title": b["title"], "created_by": actor, "files": files_meta,
                        "media_bytes": total, "table_rows": {k: len(v) for k, v in tables.items()}}
            zf.writestr("manifest.json", dumps(manifest))
            zf.writestr("db.json", dumps(db_doc))
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    with ctx.db.tx() as conn:
        ctx.audit(conn, "book", book_id, "backup", {"file": name, "bytes": dest.stat().st_size, "files": len(files_meta)}, actor)
    return {"name": name, "size_bytes": dest.stat().st_size, "files": len(files_meta), "media_bytes": total, "path": str(dest)}


@invalidates_usage
def delete_backup(ctx, name: str, confirm: bool = False, actor: str = "operator") -> dict:
    p = _backup_path(ctx, name)
    size = p.stat().st_size
    _need_confirm(confirm, f"This permanently deletes the backup file {name}.", {"name": name, "bytes": size})
    p.unlink()
    with ctx.db.tx() as conn:
        ctx.audit(conn, "backup", name, "deleted", {"bytes": size}, actor)
    return {"deleted": True, "freed_bytes": size}


def _clean_member(name: str) -> tuple[str, str] | None:
    """Validate a zip member name; returns (category, relative path) for media entries."""
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name) or "\x00" in name:
        raise ServiceError(f"Unsafe path in backup: {name!r}", 422)
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ServiceError(f"Unsafe path in backup: {name!r}", 422)
    if name in ("manifest.json", "db.json"):
        return None
    if len(parts) >= 3 and parts[0] == "media" and parts[1] in STORAGE_CATEGORIES:
        return parts[1], "/".join(parts[2:])
    raise ServiceError(f"Unexpected entry in backup: {name!r}", 422)


@invalidates_usage
def restore_book(ctx, name: str, actor: str = "operator") -> dict:
    """Restore a backup zip as a NEW book (new uid, new ids). The original book is never touched."""
    src = _backup_path(ctx, name)
    try:
        zf = zipfile.ZipFile(src)
    except zipfile.BadZipFile:
        raise ServiceError("The backup is not a valid zip file.", 422)
    with zf:
        infos = {i.filename: i for i in zf.infolist()}
        for n in infos:
            _clean_member(n)
        if "manifest.json" not in infos or "db.json" not in infos:
            raise ServiceError("The zip is not an Audiobook Studio book backup (manifest.json or db.json missing).", 422)
        try:
            manifest = json.loads(zf.read("manifest.json"))
            doc = json.loads(zf.read("db.json"))
        except ValueError:
            raise ServiceError("The backup metadata is unreadable.", 422)
        if manifest.get("format") != BACKUP_FORMAT or int(manifest.get("version", 0)) > BACKUP_VERSION:
            raise ServiceError("Unsupported backup format or version.", 422)
        if int(manifest.get("schema_version", 0)) > LATEST_VERSION:
            raise ServiceError("This backup was made by a newer version of Audiobook Studio; update first.", 422)
        old_uid = str(manifest.get("book_uid", ""))
        if not _UID_RE.match(old_uid):
            raise ServiceError("Backup has an invalid book id.", 422)
        declared = {f["path"]: f for f in manifest.get("files", [])}
        for p in declared:
            if p not in infos:
                raise ServiceError(f"Backup is incomplete: {p} is missing.", 422)
        need = sum(int(f["size"]) for f in declared.values())
        if need > MAX_RESTORE_BYTES:
            raise ServiceError("Backup is implausibly large.", 422)
        new_uid_ = new_uid("bk")
        storage = _book_storage(ctx, new_uid_)
        free = _free(Path(storage["segments"]))
        if free and free["free_bytes"] < need * 1.05 + 50 * 1024 * 1024:
            raise ServiceError("Not enough free disk space to restore this backup.", 507)
        created: list[Path] = []
        try:
            for d in storage.values():
                Path(d).mkdir(parents=True, exist_ok=True)
                created.append(Path(d))
            for arc, meta in declared.items():
                cm = _clean_member(arc)
                if cm is None:
                    continue
                cat, rel = cm
                if cat not in storage:
                    continue  # category not stored per book
                dest = Path(storage[cat]) / rel
                if _inside(dest, [Path(storage[cat])]) is None:
                    raise ServiceError(f"Unsafe path in backup: {arc!r}", 422)
                dest.parent.mkdir(parents=True, exist_ok=True)
                h, got = hashlib.sha256(), 0
                with zf.open(arc) as fin, open(dest, "wb") as fout:
                    for chunk in iter(lambda: fin.read(1024 * 1024), b""):
                        got += len(chunk)
                        if got > int(meta["size"]):
                            raise ServiceError(f"{arc} is larger than declared in the manifest.", 422)
                        h.update(chunk)
                        fout.write(chunk)
                if got != int(meta["size"]) or h.hexdigest() != meta["sha256"]:
                    raise ServiceError(f"Checksum mismatch for {arc}; the backup is damaged.", 422)
            result = _restore_rows(ctx, doc, old_uid, new_uid_, storage, actor, name)
        except BaseException:
            for d in created:
                shutil.rmtree(d, ignore_errors=True)
            raise
    return result


def _table_cols(conn, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _insert(conn, table: str, rec: dict, drop=("id",), **over) -> int:
    cols = [c for c in _table_cols(conn, table) if c in rec and c not in drop]
    data = {c: rec[c] for c in cols}
    data.update(over)
    cs = list(data)
    cur = conn.execute(f"INSERT INTO {table}({','.join(cs)}) VALUES ({','.join('?' * len(cs))})", [data[c] for c in cs])
    return cur.lastrowid


def _restore_rows(ctx, doc: dict, old_uid: str, uid: str, storage: dict, actor: str, backup_name: str) -> dict:
    tables = doc.get("tables", {})
    old_book = doc["book"]
    warnings: list[str] = []

    def path_of(tok: str | None) -> str | None:
        if not tok or not tok.startswith("@cat/"):
            return None
        _, cat, rel = tok.split("/", 2)
        if cat not in storage:
            return None
        p = Path(storage[cat]) / rel
        return str(p) if _inside(p, [Path(storage[cat])]) is not None and p.exists() else None

    def reuid(s: str) -> str:
        return uid + s[len(old_uid):] if s.startswith(old_uid) else f"{uid}-{s}"

    with ctx.db.tx() as conn:
        # voices: map by uid, then by name for built-ins; otherwise unlinked (the operator picks a narrator again)
        vmap: dict[int, int | None] = {}
        for v in doc.get("voices", []):
            hit = row(conn, "SELECT id FROM voices WHERE uid=?", (v["uid"],))
            if not hit and v.get("builtin"):
                hit = row(conn, "SELECT id FROM voices WHERE builtin=1 AND name=?", (v["name"],))
            vmap[v["id"]] = hit["id"] if hit else None
        vv_voice = {v["id"]: v["voice_id"] for v in doc.get("voice_versions", [])}

        def voice(old):
            return vmap.get(old) if old is not None else None

        def version(old_vv, new_voice):
            if new_voice is None:
                return None
            if old_vv and row(conn, "SELECT id FROM voice_versions WHERE id=? AND voice_id=?", (old_vv, new_voice)):
                return old_vv
            r = row(conn, "SELECT current_version_id FROM voices WHERE id=?", (new_voice,))
            return r["current_version_id"] if r else None

        nv = voice(old_book.get("voice_id"))
        if old_book.get("voice_id") is not None and nv is None:
            warnings.append("The narrator voice of this book does not exist here; choose a narrator before queueing it.")
        bid = _insert(conn, "books", old_book, uid=uid, work_uid=uid, title=f"{old_book['title']} (restored)", voice_id=nv,
                      voice_version_id=version(old_book.get("voice_version_id"), nv), storage_json=dumps(storage),
                      cover_path=path_of(old_book.get("cover_path")), state="imported", archived=0, created_at=now(), updated_at=now())
        chmap: dict[int, int] = {}
        for c in tables.get("chapters", []):
            chmap[c["id"]] = _insert(conn, "chapters", c, book_id=bid)
        charmap: dict[int, int] = {}
        for c in tables.get("characters", []):
            cv = voice(c.get("voice_id"))
            charmap[c["id"]] = _insert(conn, "characters", c, book_id=bid, uid=new_uid("ch"), merged_into=None, voice_id=cv,
                                       voice_version_id=version(c.get("voice_version_id"), cv))
        for c in tables.get("characters", []):
            if c.get("merged_into") in charmap:
                conn.execute("UPDATE characters SET merged_into=? WHERE id=?", (charmap[c["merged_into"]], charmap[c["id"]]))
        for m in tables.get("manuscripts", []):
            p = path_of(m.get("stored_path"))
            if p is None:
                warnings.append(f"Manuscript file {m.get('original_name')} was not in the backup.")
            _insert(conn, "manuscripts", m, book_id=bid, stored_path=p or str(Path(storage["manuscripts"]) / safe_filename(m.get("original_name") or "manuscript")))
        segmap: dict[int, int] = {}
        for s in tables.get("segments", []):
            segmap[s["id"]] = _insert(conn, "segments", s, book_id=bid, chapter_id=chmap[s["chapter_id"]], sid=reuid(s["sid"]),
                                      selected_attempt_id=None, character_id=charmap.get(s.get("character_id")), voice_override_id=voice(s.get("voice_override_id")))
        attmap: dict[int, int] = {}
        for a in tables.get("attempts", []):
            vo = a.get("voice_id_used")
            attmap[a["id"]] = _insert(conn, "attempts", a, segment_id=segmap[a["segment_id"]], wav_path=path_of(a.get("wav_path")),
                                      voice_version_id=None, voice_id_used=voice(vo))
        for s in tables.get("segments", []):
            if s.get("selected_attempt_id") in attmap:
                conn.execute("UPDATE segments SET selected_attempt_id=? WHERE id=?", (attmap[s["selected_attempt_id"]], segmap[s["id"]]))
        for sp in tables.get("nar_spans", []):
            _insert(conn, "nar_spans", sp, book_id=bid, uid=reuid(sp["uid"]), chapter_id=chmap[sp["chapter_id"]],
                    speaker_id=charmap.get(sp.get("speaker_id")), guess_id=charmap.get(sp.get("guess_id")))
        for n in tables.get("story_notes", []):
            _insert(conn, "story_notes", n, book_id=bid, character_id=charmap.get(n.get("character_id")))
        for d in tables.get("directions", []):
            if d["segment_id"] in segmap:
                _insert(conn, "directions", d, book_id=bid, segment_id=segmap[d["segment_id"]])
        for d in tables.get("direction_log", []):
            if d["segment_id"] in segmap:
                _insert(conn, "direction_log", d, segment_id=segmap[d["segment_id"]])
        for e in tables.get("seg_edits", []):
            _insert(conn, "seg_edits", e, book_id=bid, chapter_id=chmap[e["chapter_id"]])
        for h in tables.get("segment_history", []):
            _insert(conn, "segment_history", h, book_id=bid, chapter_id=chmap.get(h["chapter_id"], 0), sid=reuid(h["sid"]))
        for t in tables.get("chapter_text", []):
            _insert(conn, "chapter_text", t, drop=(), chapter_id=chmap[t["chapter_id"]], book_id=bid)
        for st in tables.get("analysis_stages", []):
            _insert(conn, "analysis_stages", st, drop=(), book_id=bid)
        for r in tables.get("review_actions", []):
            if r["segment_id"] in segmap:
                _insert(conn, "review_actions", r, segment_id=segmap[r["segment_id"]])
        for cr in tables.get("chapter_renders", []):
            p = path_of(cr.get("path"))
            if p:
                _insert(conn, "chapter_renders", cr, chapter_id=chmap[cr["chapter_id"]], path=p)
        for ex in tables.get("exports", []):
            p = path_of(ex.get("path"))
            if p:
                _insert(conn, "exports", ex, book_id=bid, path=p)
        refresh_book_state(conn, bid)
        ctx.audit(conn, "book", bid, "restored", {"from_backup": backup_name, "old_uid": old_uid, "uid": uid, "warnings": warnings}, actor)
        ctx.event(conn, "disk", f"Restored '{old_book['title']}' from {backup_name} as a new book.", bid)
    return {"restored": True, "book_id": bid, "uid": uid, "title": f"{old_book['title']} (restored)", "warnings": warnings}
