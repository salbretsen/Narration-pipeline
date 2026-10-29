#!/usr/bin/env python3
"""Print the CHANGELOG.md section for a version, for use as the GitHub release notes.

    python tools/release_notes.py v0.6.1            # notes on stdout
    python tools/release_notes.py v0.7.0-beta.2     # falls back to the v0.7.0 section if there is no beta-specific one

Exit status 1 (and a message on stderr) when no section exists, so the Release workflow refuses to publish a release without a changelog entry."""
from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "src" / "app" / "CHANGELOG.md"


def section(text: str, version: str) -> str | None:
    """Body of the ``## vX.Y.Z`` section (without its heading), or None."""
    v = version.lstrip("v")
    candidates = [v] + ([v.split("-", 1)[0]] if "-" in v else [])
    for cand in candidates:
        m = re.search(rf"^## v{re.escape(cand)}[ \t]*\n(.*?)(?=^## v|\Z)", text, re.S | re.M)
        if m and m.group(1).strip():
            return m.group(1).strip() + "\n"
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    body = section(CHANGELOG.read_text("utf-8"), argv[1])
    if body is None:
        print(f"release_notes: no CHANGELOG.md section for {argv[1]}", file=sys.stderr)
        return 1
    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
