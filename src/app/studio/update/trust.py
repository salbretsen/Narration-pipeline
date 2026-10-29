"""Release authenticity. Public keys are pinned in ``pinned_keys.json`` inside the *installed* application,
so a compromised download site cannot substitute its own key. A SHA-256 published next to an archive
proves nothing about who built it; the Ed25519 signature over the release index does.

Key rotation: ship the new public key in an application release signed by the old key, keep both keys
for one release cycle, then drop the old one. Compromise: remove the key from ``pinned_keys.json`` in the
next release signed by a still-trusted key and list its id in the index's ``revoked_keyids``; if every key
is compromised, users must reinstall from a freshly downloaded installer (there is no remote recovery
and there must not be one)."""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PINNED_FILE = Path(__file__).with_name("pinned_keys.json")


class TrustError(Exception):
    pass


def key_id(raw_public: bytes) -> str:
    return hashlib.sha256(raw_public).hexdigest()[:16]


def canonical(obj) -> bytes:
    """Deterministic JSON (sorted keys, no whitespace, UTF-8). Signed payloads use only strings, ints, bools, lists, dicts."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass
class TrustStore:
    keys: dict[str, Ed25519PublicKey] = field(default_factory=dict)
    revoked: set[str] = field(default_factory=set)
    allow_unsigned: bool = False  # development channel only; never the production default

    @classmethod
    def from_public_b64(cls, *keys_b64: str, **kw) -> "TrustStore":
        ts = cls(**kw)
        for k in keys_b64:
            raw = base64.b64decode(k)
            ts.keys[key_id(raw)] = Ed25519PublicKey.from_public_bytes(raw)
        return ts

    @classmethod
    def load_pinned(cls, path: Path | None = None) -> "TrustStore":
        p = path or PINNED_FILE
        try:
            data = json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            data = {}
        ts = cls()
        for entry in data.get("keys", []):
            raw = base64.b64decode(entry["public_key"])
            kid = key_id(raw)
            if entry.get("id") and entry["id"] != kid:
                raise TrustError(f"pinned key id mismatch for {entry['id']}")
            ts.keys[kid] = Ed25519PublicKey.from_public_bytes(raw)
        ts.revoked = set(data.get("revoked", []))
        return ts

    @property
    def configured(self) -> bool:
        return bool(self.keys)

    def verify_envelope(self, raw: bytes) -> dict:
        """Parse and authenticate a signed index ``{"signed": {...}, "signatures": [{"keyid", "sig"}]}``.
        Returns the signed payload. Raises TrustError on any failure."""
        try:
            env = json.loads(raw.decode("utf-8"))
            signed, sigs = env["signed"], env.get("signatures", [])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            raise TrustError("The release index is not valid JSON in the expected envelope.") from exc
        if not isinstance(signed, dict) or not isinstance(sigs, list):
            raise TrustError("The release index envelope is malformed.")
        if not self.keys:
            if self.allow_unsigned:
                return signed
            raise TrustError("No release-signing key is pinned in this installation, so updates cannot be authenticated. "
                             "Updates stay disabled until the owner publishes a signing key (docs/10-installation-and-updates.md).")
        body = canonical(signed)
        extra = signed.get("revoked_keyids")
        revoked = self.revoked | set(extra if isinstance(extra, list) else [])
        for s in sigs:
            kid = s.get("keyid") if isinstance(s, dict) else None
            if kid in revoked or kid not in self.keys:
                continue
            try:
                self.keys[kid].verify(base64.b64decode(s["sig"], validate=True), body)
                return signed
            except (InvalidSignature, ValueError, KeyError, TypeError):
                continue
        raise TrustError("The release index signature is invalid or was made by a key this installation does not trust. Nothing was installed.")


def sign_payload(signed: dict, private_key) -> bytes:
    """Used by tools/sign_release.py and tests: returns the envelope bytes."""
    from cryptography.hazmat.primitives import serialization

    raw_pub = private_key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    sig = private_key.sign(canonical(signed))
    env = {"signed": signed, "signatures": [{"keyid": key_id(raw_pub), "sig": base64.b64encode(sig).decode()}]}
    return json.dumps(env, indent=2, sort_keys=True).encode("utf-8")
