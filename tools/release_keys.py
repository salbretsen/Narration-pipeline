#!/usr/bin/env python3
"""Release-signing key management (run on the OWNER's trusted machine, never in CI and never inside the repository).

    python tools/release_keys.py generate --private-out ~/secure/audiobook-studio-release.key
        creates a new Ed25519 key pair. The private seed is written to the file (mode 600) and is NEVER printed; the PUBLIC key and its id
        are printed and can be added to src/app/studio/update/pinned_keys.json with  --pin  (this is the only file you commit).
    python tools/release_keys.py show-public --private ~/secure/audiobook-studio-release.key

Then store the contents of the private key file as the GitHub Actions secret RELEASE_SIGNING_KEY (Settings > Secrets and variables >
Actions) and keep an offline backup. Rotation and compromise handling: docs/10-installation-and-updates.md.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "app"))
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from studio.update.trust import PINNED_FILE, key_id  # noqa: E402


def public_b64(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes_raw()).decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--private-out", required=True)
    g.add_argument("--pin", action="store_true", help="add the new PUBLIC key to pinned_keys.json")
    g.add_argument("--name", default="release-" + __import__("time").strftime("%Y"))
    s = sub.add_parser("show-public")
    s.add_argument("--private", required=True)
    a = ap.parse_args()
    if a.cmd == "generate":
        out = Path(a.private_out).expanduser()
        if out.exists():
            sys.exit(f"{out} already exists; refusing to overwrite a key.")
        try:
            out.resolve().relative_to(ROOT)
            sys.exit("Refusing to write a private key inside the repository.")
        except ValueError:
            pass
        key = Ed25519PrivateKey.generate()
        out.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(base64.b64encode(key.private_bytes_raw()).decode() + "\n")
        pub = public_b64(key)
        print(f"private key written to {out} (not shown). Keep it secret; back it up offline.")
        print(f"public key  {pub}\nkey id      {key_id(key.public_key().public_bytes_raw())}")
        if a.pin:
            data = json.loads(PINNED_FILE.read_text("utf-8"))
            data["keys"].append({"id": key_id(key.public_key().public_bytes_raw()), "name": a.name, "public_key": pub})
            PINNED_FILE.write_text(json.dumps(data, indent=2) + "\n", "utf-8")
            print(f"pinned in {PINNED_FILE}: review and commit that file (public key only).")
    else:
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(Path(a.private).expanduser().read_text().strip()))
        print(f"public key  {public_b64(key)}\nkey id      {key_id(key.public_key().public_bytes_raw())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
