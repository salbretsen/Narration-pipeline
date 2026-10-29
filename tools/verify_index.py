#!/usr/bin/env python3
"""Verify a release index exactly the way an installed studio would: against the PUBLIC keys pinned in the repository.

    python tools/verify_index.py dist/release-index-stable.json [--archive dist/audiobook-studio-0.5.0-macos-arm64.tar.gz]

CI runs this after signing and before publishing. It catches a wrong or stale signing secret (a key that is not pinned would produce an
index no installation accepts), a corrupt file, and an archive whose size/SHA-256 differs from what the index promises.
"""
from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "app"))
from studio.update.manifest import Index, ManifestError  # noqa: E402
from studio.update.trust import TrustError, TrustStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("index")
    ap.add_argument("--archive", action="append", default=[])
    a = ap.parse_args()
    ts = TrustStore.load_pinned()
    if not ts.configured:
        print("No release-signing key is pinned in src/app/studio/update/pinned_keys.json; nothing can be verified.", file=sys.stderr)
        return 2
    try:
        idx = Index.from_signed(ts.verify_envelope(Path(a.index).read_bytes()))
    except (TrustError, ManifestError) as exc:
        print(f"INDEX REJECTED: {exc}", file=sys.stderr)
        return 1
    by_file = {r.url.rsplit("/", 1)[-1]: r for r in idx.releases}
    for arc in a.archive:
        p = Path(arc)
        r = by_file.get(p.name)
        if r is None:
            print(f"{p.name} is not listed in the index", file=sys.stderr)
            return 1
        data = p.read_bytes()
        if len(data) != r.size or hashlib.sha256(data).hexdigest() != r.sha256:
            print(f"{p.name} does not match the size/SHA-256 in the signed index", file=sys.stderr)
            return 1
    print(f"OK: index for channel {idx.channel}, sequence {idx.sequence}, {len(idx.releases)} release(s), signature valid for a pinned key")
    return 0


if __name__ == "__main__":
    sys.exit(main())
