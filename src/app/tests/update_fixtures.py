"""Helpers that build signed release indexes and release archives for the update tests. Everything here is local: no network.
(Real signing keys are never used; each test run generates a throw-away Ed25519 pair.)"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import shutil
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from studio.db import LATEST_VERSION
from studio.update.trust import TrustStore, sign_payload

APP_DIR = Path(__file__).resolve().parent.parent
REPO_URL = "github:salbretsen/Narration-pipeline"
BASE = "https://github.com/salbretsen/Narration-pipeline/releases/download"


def new_key():
    k = Ed25519PrivateKey.generate()
    pub = base64.b64encode(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    return k, pub


def trust_for(pub: str) -> TrustStore:
    return TrustStore.from_public_b64(pub)


def write_tree(root: Path, version: str, full: bool = False, break_serve: bool = False, schema: int = LATEST_VERSION, runtime_id: str = "",
               extra_files: dict[str, bytes] | None = None) -> Path:
    """Create ``root/audiobook-studio-<version>/`` with a MANIFEST.sha256 covering every file."""
    top = root / f"audiobook-studio-{version}"
    if top.exists():
        shutil.rmtree(top)
    if full:
        shutil.copytree(APP_DIR / "studio", top / "studio", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        init = top / "studio" / "__init__.py"
        init.write_text(re.sub(r'__version__ = "[^"]+"', f'__version__ = "{version}"', init.read_text("utf-8")), "utf-8", newline="\n")
        if break_serve:
            # `migrate` still works but `serve` dies at once: the "installs fine, service will not run" case
            body = "import sys\nif 'serve' in sys.argv:\n    raise SystemExit('this release is deliberately broken')\nfrom .cli import main\nmain()\n"
            (top / "studio" / "__main__.py").write_text(body, "utf-8", newline="\n")
    else:
        (top / "studio").mkdir(parents=True)
        (top / "studio" / "__init__.py").write_text(f'__version__ = "{version}"\n', "utf-8", newline="\n")
    (top / "release.json").write_text(json.dumps({"version": version, "schema_version": schema, "runtime_id": runtime_id}), "utf-8", newline="\n")
    for name, data in (extra_files or {}).items():
        p = top / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    lines = []
    for p in sorted(top.rglob("*")):
        if p.is_file():
            lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(top).as_posix()}")
    (top / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", "utf-8", newline="\n")
    return top


def pack(top: Path, out_dir: Path) -> tuple[Path, str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{top.name}-macos-arm64.tar.gz"
    with tarfile.open(path, "w:gz") as tf:
        tf.add(top, arcname=top.name)
    data = path.read_bytes()
    return path, hashlib.sha256(data).hexdigest(), len(data)


def entry(version: str, archive: Path, sha: str, size: int, **over) -> dict:
    d = {"version": version, "channel": "stable", "platform": "macos", "arch": "arm64",
         "url": f"{BASE}/v{version}/{archive.name}", "size": size, "sha256": sha, "published": "2026-10-01T00:00:00Z",
         "notes_url": f"https://github.com/salbretsen/Narration-pipeline/releases/tag/v{version}", "notes": f"Notes for {version}\n- fixed things",
         "min_compatible_version": "0.0.0", "min_os": "14.0", "schema_version": LATEST_VERSION, "runtime_id": ""}
    d.update(over)
    return d


def index_bytes(key, releases: list[dict], channel="stable", sequence=1, expires_in_days=30, **extra) -> bytes:
    now = datetime.now(timezone.utc)
    signed = {"schema": 1, "channel": channel, "sequence": sequence, "generated": now.isoformat(),
              "expires": (now + timedelta(days=expires_in_days)).isoformat(), "revoked_versions": [], "revoked_keyids": [], "releases": releases}
    signed.update(extra)
    return sign_payload(signed, key)


class FakeSite:
    """An in-memory GitHub: serves index and artifacts, honours Range, can redirect, drop connections or be offline."""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.redirects: dict[str, str] = {}
        self.offline = False
        self.drop_after: int | None = None   # truncate the next full download after N bytes (simulates a dropped connection)
        self.requests: list[tuple[str, str | None]] = []

    def put_index(self, channel: str, data: bytes) -> None:
        self.files[f"https://github.com/salbretsen/Narration-pipeline/releases/download/index-{channel}/release-index-{channel}.json"] = data

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((url, request.headers.get("range")))
        if self.offline:
            raise httpx.ConnectError("offline", request=request)
        if url in self.redirects:
            return httpx.Response(302, headers={"location": self.redirects[url]})
        data = self.files.get(url)
        if data is None:
            return httpx.Response(404)
        rng = request.headers.get("range")
        if rng and rng.startswith("bytes="):
            start = int(rng[6:].split("-")[0])
            body = data[start:]
            return httpx.Response(206, content=body, headers={"content-range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
        if self.drop_after is not None and url.endswith(".tar.gz"):
            n, self.drop_after = self.drop_after, None
            return httpx.Response(200, content=_Dropping(data, n), headers={"content-length": str(len(data))})
        return httpx.Response(200, content=data)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


class _Dropping(httpx.SyncByteStream):
    def __init__(self, data: bytes, n: int):
        self.data, self.n = data, n

    def __iter__(self):
        yield self.data[: self.n]
        raise httpx.ReadError("connection dropped")


def tar_with(members: list[tuple[tarfile.TarInfo, bytes | None]], path: Path) -> Path:
    with tarfile.open(path, "w:gz") as tf:
        for ti, data in members:
            tf.addfile(ti, io.BytesIO(data) if data is not None else None)
    return path


def file_member(name: str, data: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    ti = tarfile.TarInfo(name)
    ti.size = len(data)
    ti.mode = 0o644
    return ti, data
