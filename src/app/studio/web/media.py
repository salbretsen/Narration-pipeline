"""Static file responses with RFC 7233 single-range support (audio seeking in browsers)."""
from __future__ import annotations

import os
import re
from email.utils import formatdate
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response, StreamingResponse

MIME = {
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4b": "audio/mp4", ".m4a": "audio/mp4", ".flac": "audio/flac",
    ".zip": "application/zip", ".json": "application/json", ".html": "text/html; charset=utf-8", ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8", ".epub": "application/epub+zip", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class RangeNotSatisfiable(Exception):
    pass


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Return (start, end) inclusive, None for 'whole file', raise RangeNotSatisfiable."""
    if not header:
        return None
    header = header.strip()
    if "," in header:
        header = header.split(",", 1)[0]  # multi-range: serve the first range only
    m = _RANGE.match(header)
    if not m:
        return None  # malformed: ignore per RFC 7233 and send the full representation
    a, b = m.group(1), m.group(2)
    if a == "" and b == "":
        return None
    if a == "":
        n = int(b)
        if n == 0:
            raise RangeNotSatisfiable()
        return max(0, size - n), size - 1
    start = int(a)
    end = int(b) if b else size - 1
    if start >= size or start > end:
        raise RangeNotSatisfiable()
    return start, min(end, size - 1)


def _iter_file(path: Path, start: int, length: int, chunk: int = 256 * 1024):
    with open(path, "rb") as fh:
        fh.seek(start)
        remaining = length
        while remaining > 0:
            data = fh.read(min(chunk, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def file_response(path: Path, request: Request, download_name: str | None = None, inline_html_sandbox: bool = True) -> Response:
    st = path.stat()
    size = st.st_size
    mime = MIME.get(path.suffix.lower(), "application/octet-stream")
    headers = {
        "Accept-Ranges": "bytes",
        "Last-Modified": formatdate(st.st_mtime, usegmt=True),
        "ETag": f'"{int(st.st_mtime)}-{size}"',
        "Cache-Control": "private, no-cache",
    }
    if download_name:
        headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(download_name)}"
    if mime.startswith("text/html") and inline_html_sandbox:
        headers["Content-Security-Policy"] = "sandbox; default-src 'none'; style-src 'unsafe-inline'"
    try:
        rng = parse_range(request.headers.get("range"), size)
    except RangeNotSatisfiable:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
    if_range = request.headers.get("if-range")
    if rng and if_range and if_range != headers["ETag"]:
        rng = None
    if request.method == "HEAD":
        headers["Content-Length"] = str(size if not rng else rng[1] - rng[0] + 1)
        if rng:
            headers["Content-Range"] = f"bytes {rng[0]}-{rng[1]}/{size}"
        return Response(status_code=206 if rng else 200, headers=headers, media_type=mime)
    if rng is None:
        headers["Content-Length"] = str(size)
        return StreamingResponse(_iter_file(path, 0, size), status_code=200, headers=headers, media_type=mime)
    start, end = rng
    length = end - start + 1
    headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    headers["Content-Length"] = str(length)
    return StreamingResponse(_iter_file(path, start, length), status_code=206, headers=headers, media_type=mime)


def ensure_inside(path: Path, roots: list[Path]) -> Path:
    """Resolve ``path`` and require it to lie inside one of ``roots`` (defence against
    path traversal or tampered database rows)."""
    rp = Path(os.path.realpath(path))
    for r in roots:
        rr = Path(os.path.realpath(r))
        if rp == rr or rr in rp.parents:
            return rp
    raise PermissionError("path outside registered storage")
