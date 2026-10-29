#!/usr/bin/env python3
"""Fail if any pinned package in src/app/requirements.lock lacks a prebuilt wheel a clean Apple Silicon Mac (CPython 3.12) can use.

A package with only an sdist would make the installer compile it, which needs Xcode / Command Line Tools: that is a packaging defect
(see docs/10-installation-and-updates.md). Run in CI before every release; needs network access to pypi.org.

    python tools/check_lock_wheels.py [--min-macos 14]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;\\]+)(?:\s*;\s*(.+?))?\s*\\?$")


def pins(lock: Path):
    for line in lock.read_text("utf-8").splitlines():
        m = PIN.match(line)
        if m:
            yield m.group(1), m.group(2), (m.group(3) or "").strip()


def usable(filename: str, min_macos: int) -> bool:
    if not filename.endswith(".whl"):
        return False
    parts = filename[:-4].split("-")
    pytag, abitag, plat = parts[-3], parts[-2], parts[-1]
    py_ok = any(t in pytag.split(".") for t in ("py3", "py2.py3", "cp312", "cp3", "py312")) or pytag.startswith("py3") or "cp312" in pytag or "abi3" in abitag
    if not py_ok:
        return False
    for p in plat.split("."):
        if p == "any":
            return True
        m = re.match(r"macosx_(\d+)_(\d+)_(arm64|universal2)$", p)
        if m and int(m.group(1)) <= min_macos:
            return True
    return False


def check(item, min_macos):
    name, ver, marker = item
    if marker and "darwin" not in marker and "macos" not in marker.lower():
        if "sys_platform" in marker and "!=" not in marker and "darwin" not in marker:
            return name, ver, None   # marker excludes macOS (e.g. win32-only); not needed on a Mac
    url = f"https://pypi.org/pypi/{name}/{ver}/json"
    try:
        data = json.load(urllib.request.urlopen(url, timeout=30))
    except Exception as exc:
        return name, ver, f"lookup failed: {exc}"
    files = [f["filename"] for f in data.get("urls", [])]
    if any(usable(f, min_macos) for f in files):
        return name, ver, None
    return name, ver, "no prebuilt macOS arm64 / pure-Python wheel (sdist only?): " + ", ".join(files[:4])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lock", default=str(ROOT / "src" / "app" / "requirements.lock"))
    ap.add_argument("--min-macos", type=int, default=14, help="oldest supported macOS major version (wheel tags up to this are usable)")
    a = ap.parse_args()
    items = list(pins(Path(a.lock)))
    with ThreadPoolExecutor(8) as ex:
        results = list(ex.map(lambda i: check(i, a.min_macos), items))
    bad = [(n, v, why) for n, v, why in results if why]
    for n, v, why in bad:
        print(f"PROBLEM {n}=={v}: {why}")
    print(f"{len(items)} pinned packages checked; {len(bad)} problem(s).")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
