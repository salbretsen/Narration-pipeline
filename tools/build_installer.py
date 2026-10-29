#!/usr/bin/env python3
"""Generate install_audiobook_studio_claude.sh from src/ by embedding every application file
as a quoted bash heredoc. The output is fully self-contained (no repo access at install time).

    python tools/build_installer.py
"""
from __future__ import annotations

import hashlib
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_APP = ROOT / "src" / "app"
TEMPLATE = ROOT / "src" / "installer" / "installer.sh.in"
OUT = ROOT / "install_audiobook_studio_claude.sh"
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXECUTABLE = {"bin/start-studio", "bin/stop-studio", "bin/studio", "bin/run-tests", "bin/smoke-test"}


def collect() -> list[tuple[str, str]]:
    files = []
    for p in sorted(SRC_APP.rglob("*")):
        if p.is_dir() or any(part in EXCLUDE_DIRS for part in p.parts) or p.suffix in EXCLUDE_SUFFIXES:
            continue
        rel = p.relative_to(SRC_APP).as_posix()
        if not re.fullmatch(r"[A-Za-z0-9_./-]+", rel):
            sys.exit(f"unsafe file name for embedding: {rel}")
        raw = p.read_bytes()
        if b"\r" in raw:
            sys.exit(f"{rel} contains CR characters; use LF line endings")
        text = raw.decode("utf-8")
        if text and not text.endswith("\n"):
            text += "\n"
        files.append((rel, text))
    return files


def main() -> None:
    from_version = re.search(r'__version__ = "([^"]+)"', (SRC_APP / "studio" / "__init__.py").read_text("utf-8")).group(1)
    files = collect()
    manifest = "".join(f"{hashlib.sha256(t.encode('utf-8')).hexdigest()}  {rel}\n" for rel, t in files)
    files.append(("MANIFEST.sha256", manifest))
    parts: list[str] = []
    made_dirs: set[str] = set()
    for rel, text in files:
        d = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if d and d not in made_dirs:
            parts.append(f'  mkdir -p "$DEST/{d}"')
            made_dirs.add(d)
        delim = "__STUDIO_EOF_" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12] + "__"
        if delim in text:
            sys.exit(f"heredoc delimiter collision in {rel}")
        if text:
            parts.append(f"cat > \"$DEST/{rel}\" <<'{delim}'\n{text}{delim}")
        else:
            parts.append(f': > "$DEST/{rel}"')
        if rel in EXECUTABLE:
            parts.append(f'  chmod 755 "$DEST/{rel}"')
    template = TEMPLATE.read_text("utf-8")
    build_id = time.strftime("%Y%m%d%H%M%S") + "-" + hashlib.sha256(manifest.encode()).hexdigest()[:8]
    out = (template.replace("@@VERSION@@", from_version).replace("@@BUILD_ID@@", build_id)
           .replace("@@EMBEDDED_FILES@@", "\n".join(parts)))
    OUT.write_bytes(out.encode("utf-8"))
    try:
        OUT.chmod(0o755)
    except OSError:
        pass
    print(f"wrote {OUT.name}: {len(files)} files, {len(out.splitlines())} lines, {len(out.encode()) / 1024:.0f} KB (build {build_id})")


if __name__ == "__main__":
    main()
