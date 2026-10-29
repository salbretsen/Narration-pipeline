"""Small shared helpers: hashing, atomic writes, JSON, filenames."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import unicodedata
from pathlib import Path
from typing import Any


def now() -> float:
    return time.time()


def new_uid(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(bufsize)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def short_hash(text: str, n: int = 12) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def _fsync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: str | Path, data: bytes) -> Path:
    """Write ``data`` to ``path`` via a temp file in the same directory and os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.part")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return path


def atomic_write_text(path: str | Path, text: str) -> Path:
    return atomic_write_bytes(path, text.encode("utf-8"))


def temp_sibling(path: str | Path, suffix: str = "") -> Path:
    """A unique temp path next to ``path`` (same filesystem, so os.replace is atomic)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.with_name(f".{path.stem}.{secrets.token_hex(4)}.part{suffix or path.suffix}")


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()\-]+")


def safe_filename(name: str, default: str = "file", maxlen: int = 120) -> str:
    """Reduce an untrusted filename to a safe basename (no separators, no leading dots)."""
    base = re.split(r"[\\/]", name or "")[-1]
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    base = _UNSAFE.sub("_", base).strip(" ._")
    base = re.sub(r"_+", "_", base)
    if not base:
        base = default
    if len(base) > maxlen:
        stem, dot, ext = base.rpartition(".")
        if dot and len(ext) <= 8:
            base = stem[: maxlen - len(ext) - 1] + "." + ext
        else:
            base = base[:maxlen]
    return base


def title_filename(*parts: str, maxlen: int = 150) -> str:
    joined = " - ".join(p for p in parts if p)
    return safe_filename(joined, default="audiobook", maxlen=maxlen)


def word_count(text: str) -> int:
    return len(re.findall(r"[\w'’-]+", text or ""))


def human_bytes(n: float | None) -> str:
    if n is None:
        return "unknown"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def is_within(path: str | Path, root: str | Path) -> bool:
    """True when the resolved ``path`` is ``root`` or inside it (symlinks resolved)."""
    try:
        p = Path(path).resolve()
        r = Path(root).resolve()
    except (OSError, RuntimeError):
        return False
    return p == r or r in p.parents
