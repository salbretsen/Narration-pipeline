#!/usr/bin/env python3
"""Print the CHANGELOG.md notes for a version, for use as the GitHub release notes and in the signed release index.

    python tools/release_notes.py v0.6.1                         # that version's section on stdout
    python tools/release_notes.py v0.7.0-beta.2                  # falls back to the v0.7.0 section if there is no beta-specific one
    python tools/release_notes.py v0.8.2 --since v0.6.2          # every section after v0.6.2 up to v0.8.2 (versions never published separately)
    python tools/release_notes.py --previous v0.8.2 --releases releases.json   # the published release before v0.8.2 (or nothing)

``--releases`` is the output of ``gh release list --json tagName,isPrerelease``. Exit status 1 (and a message on stderr) when no
section exists, so the Release workflow refuses to publish a release without a changelog entry."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "src" / "app" / "CHANGELOG.md"
_TAG = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")


def _key(version: str) -> tuple | None:
    """Sort key for vX.Y.Z[-pre]; a release outranks its pre-releases. None for anything else (index-stable, ...)."""
    m = _TAG.match(version.strip())
    if not m:
        return None
    pre = m[4]
    pre_key = (1,) if pre is None else (0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")))
    return (int(m[1]), int(m[2]), int(m[3]), pre_key)


def section(text: str, version: str) -> str | None:
    """Body of the ``## vX.Y.Z`` section (without its heading), or None."""
    v = version.lstrip("v")
    candidates = [v] + ([v.split("-", 1)[0]] if "-" in v else [])
    for cand in candidates:
        m = re.search(rf"^## v{re.escape(cand)}[ \t]*\n(.*?)(?=^## v|\Z)", text, re.S | re.M)
        if m and m.group(1).strip():
            return m.group(1).strip() + "\n"
    return None


def changelog_versions(text: str) -> list[str]:
    """Every ``## vX.Y.Z`` heading in the changelog, as written (newest first in practice)."""
    return re.findall(r"^## (v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?)[ \t]*$", text, re.M)


def notes(text: str, version: str, since: str | None = None) -> str | None:
    """The notes for ``version``. With ``since`` (the previous *published* release), every changelog section after it up to
    ``version`` is included, newest first, so versions that were never published separately are not lost. None when
    ``version`` itself has no section."""
    own = section(text, version)
    if own is None:
        return None
    target, low = _key(version.lstrip("v").split("-", 1)[0]), _key(since) if since else None
    if low is None:
        return own
    between = [v for v in changelog_versions(text) if _key(v) is not None and low < _key(v) < target and section(text, v)]
    if not between:
        return own
    between.sort(key=_key, reverse=True)
    base = "v" + version.lstrip("v").split("-", 1)[0]
    one = len(between) == 1
    joined = between[0] if one else ", ".join(between[:-1]) + " and " + between[-1]
    lead = f"This release also contains {joined}, which {'was' if one else 'were'} not published separately; {'its' if one else 'their'} notes follow.\n"
    parts = [lead, f"## {base}\n{own}"] + [f"## {v}\n{section(text, v)}" for v in between]
    return "\n".join(parts)


def previous_published(version: str, releases: list[dict]) -> str | None:
    """The highest published release below ``version`` on the same channel. ``releases`` is ``gh release list --json
    tagName,isPrerelease``. A stable version only follows stable releases (an unsigned build is published as a prerelease and
    does not count); a beta version follows betas and stable releases."""
    me = _key(version)
    if me is None:
        return None
    beta = "-" in version
    best = None
    for r in releases or []:
        tag = str(r.get("tagName") or "")
        k = _key(tag)
        if k is None or k >= me or tag.lstrip("v") == version.lstrip("v"):
            continue
        is_beta_tag = "-" in tag
        if r.get("isPrerelease") and not is_beta_tag:
            continue  # unsigned build of a stable version
        if is_beta_tag and not beta:
            continue
        if best is None or k > _key(best):
            best = tag
    return best


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version", nargs="?")
    ap.add_argument("--since", help="previous published release; include every section after it")
    ap.add_argument("--previous", metavar="VERSION", help="print the published release before VERSION (needs --releases)")
    ap.add_argument("--releases", help="JSON from: gh release list --json tagName,isPrerelease")
    a = ap.parse_args(argv[1:])
    if a.previous:
        if not a.releases:
            ap.error("--previous needs --releases")
        prev = previous_published(a.previous, json.loads(Path(a.releases).read_text("utf-8")))
        if prev:
            print(prev)
        return 0
    if not a.version:
        ap.error("a version is required")
    body = notes(CHANGELOG.read_text("utf-8"), a.version, a.since)
    if body is None:
        print(f"release_notes: no CHANGELOG.md section for {a.version}", file=sys.stderr)
        return 1
    sys.stdout.write(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
