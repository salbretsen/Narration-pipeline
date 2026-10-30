#!/usr/bin/env python3
"""Create the SIGNED release index (release-index-<channel>.json) for one or more built archives.

The private key is read ONLY from the environment (RELEASE_SIGNING_KEY: base64 of the 32-byte Ed25519 seed) or a file outside the
repository given with --key-file. It is never printed, logged, written to the index or committed. Without a key this tool refuses:
there is no "unsigned" production output. (For local development a clearly labelled unsigned dev index needs --dev-unsigned and the
studio must be run with the development channel enabled.)

    python tools/sign_release.py --channel stable --base-url https://github.com/OWNER/REPO/releases/download \
        --release build.json [--release build2.json] [--merge-from previous-index.json] [--notes-from-changelog | --notes-file NOTES.md] [--out dist]

``--release`` takes the JSON line printed by tools/build_release.py. ``--merge-from`` keeps releases from the previous index (verified with
the same key first) so older versions stay installable as stepping stones; the sequence number always increases. ``--notes-from-changelog``
(re)writes the notes of every release in the index from CHANGELOG.md: each release lists everything since the release before it in the
index, so versions that were never published separately are still described.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "app"))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from studio.update.manifest import MAX_NOTES, Index, ManifestError  # noqa: E402
from studio.update.trust import TrustError, TrustStore, key_id, sign_payload  # noqa: E402

KEEP_RELEASES = 8


def load_key(key_file: str | None) -> Ed25519PrivateKey | None:
    raw = None
    if key_file:
        raw = Path(key_file).read_text().strip()
    elif os.environ.get("RELEASE_SIGNING_KEY"):
        raw = os.environ["RELEASE_SIGNING_KEY"].strip()
    if not raw:
        return None
    try:
        seed = base64.b64decode(raw, validate=True)
        return Ed25519PrivateKey.from_private_bytes(seed)
    except Exception:
        sys.exit("The signing key is not a base64-encoded 32-byte Ed25519 seed. (The key value is not shown.)")


def fill_notes(ordered: list[dict]) -> None:
    """Notes for every release (newest first) from CHANGELOG.md, covering everything since the previous release in the index on the
    same channel (a stable release follows stable releases; a beta follows betas and stable releases). A release without a changelog
    section keeps the notes it had."""
    sys.path.insert(0, str(ROOT / "tools"))
    import release_notes as RN

    text = RN.CHANGELOG.read_text("utf-8")
    for i, r in enumerate(ordered):
        beta = "-" in r["version"]
        prev = next((o["version"] for o in ordered[i + 1:] if beta or "-" not in o["version"]), None)
        body = RN.notes(text, r["version"], prev)
        if body:
            r["notes"] = body[:MAX_NOTES]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", choices=["stable", "beta"], default="stable")
    ap.add_argument("--base-url", required=True, help="https://github.com/OWNER/REPO/releases/download")
    ap.add_argument("--release", action="append", required=True, help="JSON line from build_release.py (path to a file or the JSON itself)")
    ap.add_argument("--merge-from")
    ap.add_argument("--notes-file")
    ap.add_argument("--notes-from-changelog", action="store_true")
    ap.add_argument("--min-compatible", default="0.0.0")
    ap.add_argument("--min-os", default="14.0")
    ap.add_argument("--key-file")
    ap.add_argument("--expires-days", type=int, default=180)
    ap.add_argument("--revoke-version", action="append", default=[])
    ap.add_argument("--revoke-keyid", action="append", default=[])
    ap.add_argument("--dev-unsigned", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "dist"))
    a = ap.parse_args()
    if not a.base_url.startswith("https://"):
        sys.exit("--base-url must be https")
    key = load_key(a.key_file)
    if key is None and not a.dev_unsigned:
        sys.exit("No signing key configured (set RELEASE_SIGNING_KEY or --key-file). Refusing to produce a release index: "
                 "nothing is published as signed unless a key is present.")
    notes = Path(a.notes_file).read_text("utf-8")[:MAX_NOTES] if a.notes_file else ""
    now = datetime.now(timezone.utc)
    new = []
    for spec in a.release:
        meta = json.loads(Path(spec).read_text() if Path(spec).exists() else spec)
        v = meta["version"]
        new.append({"version": v, "channel": "beta" if "-" in v else a.channel, "platform": "macos", "arch": "arm64",
                    "url": f"{a.base_url.rstrip('/')}/v{v}/{meta['file']}", "size": meta["size"], "sha256": meta["sha256"],
                    "published": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "notes_url": f"{a.base_url.rstrip('/').rsplit('/releases/download', 1)[0]}/releases/tag/v{v}",
                    "notes": notes, "min_compatible_version": a.min_compatible, "min_os": a.min_os,
                    "schema_version": meta["schema_version"], "runtime_id": meta["runtime_id"]})
    releases, sequence, revoked_v = {r["version"]: r for r in new}, int(time.time()), set(a.revoke_version)
    revoked_k = set(a.revoke_keyid)
    if a.merge_from and Path(a.merge_from).exists():
        raw = Path(a.merge_from).read_bytes()
        if key is not None:
            pub = base64.b64encode(key.public_key().public_bytes_raw()).decode()
            try:
                prev = Index.from_signed(TrustStore.from_public_b64(pub).verify_envelope(raw))
            except (TrustError, ManifestError) as exc:
                sys.exit(f"The previous index does not verify with this key ({exc}); refusing to merge. Use a fresh index deliberately.")
        else:
            prev = Index.from_signed(json.loads(raw)["signed"])
        for r in prev.raw.get("releases", []):
            releases.setdefault(r["version"], r)
        sequence = max(sequence, prev.sequence + 1)
        revoked_v |= prev.revoked_versions
        revoked_k |= set(prev.raw.get("revoked_keyids", []))
    from studio.update.manifest import parse_version

    ordered = [dict(r) for r in sorted(releases.values(), key=lambda r: parse_version(r["version"]), reverse=True)[:KEEP_RELEASES]]
    if a.notes_from_changelog:
        fill_notes(ordered)
    signed = {"schema": 1, "channel": a.channel, "sequence": sequence, "generated": now.isoformat(),
              "expires": (now + timedelta(days=a.expires_days)).isoformat(), "revoked_versions": sorted(revoked_v),
              "revoked_keyids": sorted(revoked_k), "releases": ordered}
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"release-index-{a.channel}.json"
    if key is None:
        path.write_bytes(json.dumps({"signed": signed, "signatures": []}, indent=2, sort_keys=True).encode())
        print(f"WROTE UNSIGNED DEVELOPMENT INDEX {path} (not accepted by production installations)", file=sys.stderr)
    else:
        path.write_bytes(sign_payload(signed, key))
        print(f"signed {path} with key id {key_id(key.public_key().public_bytes_raw())} (sequence {sequence}, {len(ordered)} release(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
