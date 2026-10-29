#!/usr/bin/env python3
"""Public-repo scrub check: fail if tracked files contain private network details, personal paths or private keys.

Run from anywhere inside the repository:  python tools/scrub_check.py

Detects
  * private IPv4 addresses (10.x, 172.16-31.x, 192.168.x), except documentation placeholders (ALLOWED_IPS);
  * real-looking `.lan` / `.local` host names, except generic examples (ALLOWED_HOSTS);
  * personal home paths: C:\\Users\\<name>, /Users/<name>, /home/<name>, except placeholders (ALLOWED_USERS);
  * PEM private key blocks.

Loopback (127.x) and 0.0.0.0 are never private-network leaks and are not matched at all.
Exit code 0 = clean, 1 = findings (printed as path:line: kind: text).
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Documentation placeholders that are fine to keep (RFC 5737-style examples, obvious dummies).
ALLOWED_IPS = {"192.168.1.1", "192.168.0.1", "192.168.1.10", "192.168.1.20", "10.0.0.1", "10.0.0.2", "10.0.0.5"}
ALLOWED_HOSTS = {"example.lan", "example.local", "your-mac.local", "my-mac.local", "mac.local", "studio.local",
                 "hostname.local", "host.local", "your-mac.lan", "mac.lan", "studio.lan", "localhost.localdomain",
                 # false positives: attribute access in code (`args.lan`, `c.local`) and dummy names in tests
                 "a.lan", "c.local", "another-name.lan"}
ALLOWED_USERS = {"user", "username", "you", "yourname", "your-name", "name", "me", "owner", "scott-placeholder",
                 "shared", "runner", "public", "default", "all", "admin", "example", "test", "tester", "x", "foo",
                 "u"}  # "u": dummy user in updater path tests
# This script and its test spell out the patterns on purpose.
# The generated installers embed the exempt files above verbatim; their sources are scanned directly, so the copies are skipped.
SELF_EXEMPT = {"tools/scrub_check.py", "src/app/tests/test_scrub_check.py", "install_audiobook_studio_mac.sh", "install_audiobook_studio_claude.sh"}

IP_RE = re.compile(r"(?<![\d.])(10(?:\.\d{1,3}){3}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2}|192\.168(?:\.\d{1,3}){2})(?![\d])")
HOST_RE = re.compile(r"(?<![\w.-])([A-Za-z0-9][A-Za-z0-9-]*(?:\.[A-Za-z0-9-]+)*\.(?:lan|local))(?![\w-])", re.I)
# Windows (either slash style, optionally JSON-escaped) and mac/linux home directories.
WIN_RE = re.compile(r"[A-Za-z]:(?:\\{1,2}|/)Users(?:\\{1,2}|/)([^\\/\s\"'<>*{}$%]+)", re.I)
UNIX_RE = re.compile(r"(?<![\w.~:-])/(?:Users|home)/([^/\s\"'<>*{}$%`)]+)")
PEM_RE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----")

BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".wav", ".mp3", ".m4b", ".zip", ".gz", ".tar", ".whl", ".pdf"}


def scan_text(text: str, name: str = "<text>") -> list[tuple[str, int, str, str]]:
    """Return findings as (name, line number, kind, matched text)."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for m in IP_RE.finditer(line):
            if m.group(1) not in ALLOWED_IPS:
                out.append((name, n, "private IPv4", m.group(1)))
        for m in HOST_RE.finditer(line):
            host = m.group(1).lower()
            if host not in ALLOWED_HOSTS and not host.endswith((".example.lan", ".example.local")):
                out.append((name, n, "private hostname", m.group(1)))
        for rx in (WIN_RE, UNIX_RE):
            for m in rx.finditer(line):
                if m.group(1).lower() not in ALLOWED_USERS:
                    out.append((name, n, "home path", m.group(0)))
        if PEM_RE.search(line):
            out.append((name, n, "private key block", line.strip()[:40]))
    return out


def tracked_files() -> list[str]:
    res = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return [p for p in res.stdout.decode("utf-8", "replace").split("\0") if p]


def main() -> int:
    findings = []
    for rel in tracked_files():
        if rel in SELF_EXEMPT or Path(rel).suffix.lower() in BINARY_SUFFIXES:
            continue
        p = ROOT / rel
        try:
            data = p.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        findings += scan_text(data.decode("utf-8", "replace"), rel)
    for name, n, kind, text in findings:
        print(f"{name}:{n}: {kind}: {text}")
    if findings:
        print(f"\nscrub_check: {len(findings)} finding(s). Replace with placeholders (192.168.x.x -> <lan-ip>, /Users/<user>/..., host.example.lan) "
              "or, for a genuine false positive, extend the allowlist in tools/scrub_check.py with a comment.", file=sys.stderr)
        return 1
    print("scrub_check: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
