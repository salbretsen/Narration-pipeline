#!/usr/bin/env python3
"""Build the update archive for the current version: dist/audiobook-studio-<version>-macos-arm64.tar.gz

The archive holds exactly the files the installer embeds (src/app plus BUILD, release.json and MANIFEST.sha256) under a single top-level
directory audiobook-studio-<version>/. No symlinks, no special files, fixed ownership and a fixed mtime, so the same sources give the same
bytes (reproducible apart from the BUILD id, which is derived from the manifest and a timestamp).

    python tools/build_release.py [--out dist]

Prints a JSON line with version, file name, size and sha256 for tools/sign_release.py.
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_installer as B  # noqa: E402

FIXED_MTIME = 1_700_000_000
EXEC = B.EXECUTABLE


def build(out_dir: Path) -> dict:
    files, build_id = B.build_files()
    ver = B.version()
    top = f"audiobook-studio-{ver}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{top}-macos-arm64.tar.gz"
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tf:
        d = tarfile.TarInfo(top)
        d.type, d.mode, d.mtime = tarfile.DIRTYPE, 0o755, FIXED_MTIME
        tf.addfile(d)
        made = set()
        for rel, text in files:
            parts = rel.split("/")
            for i in range(1, len(parts)):
                sub = "/".join(parts[:i])
                if sub not in made:
                    made.add(sub)
                    di = tarfile.TarInfo(f"{top}/{sub}")
                    di.type, di.mode, di.mtime = tarfile.DIRTYPE, 0o755, FIXED_MTIME
                    tf.addfile(di)
            data = text.encode("utf-8")
            ti = tarfile.TarInfo(f"{top}/{rel}")
            ti.size, ti.mtime, ti.mode = len(data), FIXED_MTIME, 0o755 if rel in EXEC else 0o644
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            tf.addfile(ti, io.BytesIO(data))
    with open(path, "wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    import hashlib

    data = path.read_bytes()
    meta = json.loads(dict(files)["release.json"])
    return {"version": ver, "file": path.name, "path": str(path), "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "schema_version": meta["schema_version"], "runtime_id": meta["runtime_id"], "build": build_id}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(B.ROOT / "dist"))
    a = ap.parse_args()
    print(json.dumps(build(Path(a.out))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
