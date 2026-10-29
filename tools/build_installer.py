#!/usr/bin/env python3
"""Generate the installers from src/ by embedding every application file as a quoted bash heredoc. Outputs are fully
self-contained (no repo access at install time).

    python tools/build_installer.py              # both
    python tools/build_installer.py --only mac   # install_audiobook_studio_mac.sh (supported: no prerequisites)
    python tools/build_installer.py --only legacy  # install_audiobook_studio_claude.sh (Homebrew-era; kept for existing installs)

The same file list, release.json and MANIFEST.sha256 are used by tools/build_release.py for the update archive.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_APP = ROOT / "src" / "app"
TEMPLATES = {"legacy": (ROOT / "src" / "installer" / "installer.sh.in", ROOT / "install_audiobook_studio_claude.sh"),
             "mac": (ROOT / "src" / "installer" / "installer_mac.sh.in", ROOT / "install_audiobook_studio_mac.sh")}
RUNTIME_MANIFEST = ROOT / "tools" / "runtime-manifest.json"
EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}
EXECUTABLE = {"bin/start-studio", "bin/stop-studio", "bin/studio", "bin/studio-service", "bin/run-tests", "bin/smoke-test", "bin/audiobook-studio"}


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


def version() -> str:
    init = (SRC_APP / "studio" / "__init__.py").read_text("utf-8")
    return re.search(r'__version__ = "([^"]+)"', init).group(1)


def schema_version() -> int:
    db = (SRC_APP / "studio" / "db.py").read_text("utf-8")
    nums = [int(m) for m in re.findall(r"^\s*\((\d+), SCHEMA_V\d+\),\s*$", db, flags=re.M)]
    if not nums:
        sys.exit("cannot determine the latest database schema version from db.py")
    return max(nums)


def build_files() -> tuple[list[tuple[str, str]], str]:
    """All files of a release, with BUILD, release.json and MANIFEST.sha256 appended. Returns (files, build_id)."""
    files = collect()
    manifest = "".join(f"{hashlib.sha256(t.encode('utf-8')).hexdigest()}  {rel}\n" for rel, t in files)
    build_id = time.strftime("%Y%m%d%H%M%S") + "-" + hashlib.sha256(manifest.encode()).hexdigest()[:8]
    lock = SRC_APP / "requirements.lock"
    runtime_id = hashlib.sha256(lock.read_bytes()).hexdigest()[:16] if lock.exists() else ""
    files.append(("BUILD", build_id + "\n"))
    files.append(("release.json", json.dumps({"version": version(), "schema_version": schema_version(), "runtime_id": runtime_id,
                                              "build": build_id, "platform": "macos", "arch": "arm64"}, indent=2) + "\n"))
    manifest = "".join(f"{hashlib.sha256(t.encode('utf-8')).hexdigest()}  {rel}\n" for rel, t in files)
    files.append(("MANIFEST.sha256", manifest))
    return files, build_id


def runtime_pins() -> str:
    m = json.loads(RUNTIME_MANIFEST.read_text("utf-8"))
    out = []

    def put(name: str, value, pattern: str) -> None:
        v = str(value)
        if not re.fullmatch(pattern, v):
            sys.exit(f"runtime-manifest.json: unsafe or malformed value for {name}: {v!r}")
        out.append(f'{name}="{v}"')

    url = r"https://[A-Za-z0-9._/-]+"
    for key, prefix in (("uv", "UV"), ("ffmpeg", "FFMPEG"), ("ffprobe", "FFPROBE")):
        e = m[key]
        put(f"{prefix}_VERSION", e["version"], r"[0-9][0-9A-Za-z.]*")
        put(f"{prefix}_URL", e["url"], url)
        put(f"{prefix}_SHA256", e["sha256"], r"[0-9a-f]{64}")
        put(f"{prefix}_SIZE", e["size"], r"[0-9]{5,10}")
        put(f"{prefix}_MEMBER", e["member"], r"[A-Za-z0-9._/-]+")
    put("PY_REQUEST", m["python"]["request"], r"3\.[0-9]+")
    put("MIN_MACOS", m["min_macos"], r"[0-9]+(\.[0-9]+)?")
    return "\n".join(out)


def render(kind: str, files: list[tuple[str, str]], build_id: str) -> str:
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
    template = TEMPLATES[kind][0].read_text("utf-8")
    out = template.replace("@@VERSION@@", "v" + version()).replace("@@BUILD_ID@@", build_id).replace("@@EMBEDDED_FILES@@", "\n".join(parts))
    if "@@RUNTIME_PINS@@" in out:
        out = out.replace("@@RUNTIME_PINS@@", runtime_pins())
    if "@@" in out.replace("@@VERSION@@", ""):
        left = re.findall(r"@@[A-Z_]+@@", out)
        if left:
            sys.exit(f"unreplaced placeholders: {left}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["mac", "legacy"])
    a = ap.parse_args()
    files, build_id = build_files()
    for kind in ([a.only] if a.only else ["legacy", "mac"]):
        out_path = TEMPLATES[kind][1]
        text = render(kind, files, build_id)
        out_path.write_bytes(text.encode("utf-8"))
        try:
            out_path.chmod(0o755)
        except OSError:
            pass
        print(f"wrote {out_path.name}: {len(files)} files, {len(text.splitlines())} lines, {len(text.encode()) / 1024:.0f} KB (build {build_id})")


if __name__ == "__main__":
    main()
