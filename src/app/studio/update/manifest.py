"""The signed release-index contract and update selection.

Envelope (see trust.py)  ->  ``signed`` payload:

  schema            1
  channel           "stable" | "beta" | "dev"
  sequence          monotonically increasing integer; clients refuse a lower value than the last one seen (rollback/replay)
  generated, expires  ISO-8601 UTC; clients refuse an expired index (freeze attack)
  revoked_versions  versions that must never be installed or offered
  revoked_keyids    signing keys to distrust from now on
  releases[]        version, channel, platform ("macos"), arch ("arm64"), url, size, sha256, published,
                    notes_url, notes (short plain text), min_compatible_version (oldest installed version that may
                    update directly to this one), min_os, schema_version (database schema this release needs),
                    runtime_id (private Python environment the release needs)

The signature covers the digest and size of every archive, so the archive is authenticated *through* the index."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
CHANNELS = ("stable", "beta", "dev")
MAX_NOTES = 8000


class ManifestError(Exception):
    pass


def parse_version(v: str) -> tuple:
    m = SEMVER_RE.match(v or "")
    if not m:
        raise ManifestError(f"not a semantic version: {v!r}")
    major, minor, patch, pre = int(m[1]), int(m[2]), int(m[3]), m[4]
    # a release outranks its pre-releases; pre-release identifiers compare numerically when numeric
    pre_key = (1,) if pre is None else (0, tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")))
    return (major, minor, patch, pre_key)


def parse_time(s: str) -> datetime:
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise ManifestError(f"bad timestamp {s!r}") from exc
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _https_or_empty(u) -> str:
    """Links shown in the UI must be https (never javascript:, data:, http:)."""
    u = str(u or "")
    return u if u.startswith("https://") and " " not in u and len(u) < 400 else ""


@dataclass(frozen=True)
class Release:
    version: str
    channel: str
    platform: str
    arch: str
    url: str
    size: int
    sha256: str
    published: str
    notes_url: str = ""
    notes: str = ""
    min_compatible_version: str = "0.0.0"
    min_os: str = "14.0"
    schema_version: int = 0
    runtime_id: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Release":
        if not isinstance(d, dict):
            raise ManifestError("release entry is not an object")
        try:
            r = cls(version=str(d["version"]), channel=str(d["channel"]), platform=str(d["platform"]), arch=str(d["arch"]),
                    url=str(d["url"]), size=int(d["size"]), sha256=str(d["sha256"]).lower(), published=str(d["published"]),
                    notes_url=_https_or_empty(d.get("notes_url", "")), notes=str(d.get("notes", ""))[:MAX_NOTES],
                    min_compatible_version=str(d.get("min_compatible_version", "0.0.0")), min_os=str(d.get("min_os", "14.0")),
                    schema_version=int(d.get("schema_version", 0)), runtime_id=str(d.get("runtime_id", "")))
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError(f"release entry is missing or has a bad field: {exc}") from exc
        parse_version(r.version)
        parse_version(r.min_compatible_version)
        if r.channel not in CHANNELS:
            raise ManifestError(f"unknown channel {r.channel!r}")
        if not SHA_RE.match(r.sha256):
            raise ManifestError(f"release {r.version}: sha256 must be 64 hex characters")
        if r.size <= 0:
            raise ManifestError(f"release {r.version}: size must be positive")
        if not r.url.startswith("https://"):
            raise ManifestError(f"release {r.version}: artifact URL must be https")
        return r

    def public(self) -> dict:
        return {k: getattr(self, k) for k in ("version", "channel", "platform", "arch", "size", "sha256", "published", "notes_url",
                                              "notes", "min_compatible_version", "min_os", "schema_version")}


@dataclass
class Index:
    channel: str
    sequence: int
    generated: str
    expires: str
    releases: list[Release]
    revoked_versions: set[str] = field(default_factory=set)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_signed(cls, signed: dict[str, Any], now: datetime | None = None) -> "Index":
        if signed.get("schema") != 1:
            raise ManifestError(f"unsupported release-index schema {signed.get('schema')!r}")
        channel = signed.get("channel")
        if channel not in CHANNELS:
            raise ManifestError(f"unknown channel {channel!r}")
        try:
            seq = int(signed["sequence"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ManifestError("release index has no sequence number") from exc
        expires = parse_time(str(signed.get("expires", "")))
        if expires < (now or datetime.now(timezone.utc)):
            raise ManifestError("The release index has expired (possible stale or replayed metadata); not trusting it.")
        rels = [Release.from_dict(r) for r in signed.get("releases", [])]
        revoked = {str(v) for v in signed.get("revoked_versions", [])}
        return cls(channel, seq, str(signed.get("generated", "")), str(signed["expires"]), rels, revoked, signed)


@dataclass
class Selection:
    state: str  # up_to_date | available | incompatible | none_for_platform
    release: Release | None = None
    reason: str = ""
    newer_exists: bool = False  # a newer release exists but needs an intermediate step first


def _os_tuple(v: str) -> tuple:
    return tuple(int(p) for p in re.findall(r"\d+", v)[:3]) or (0,)


def select_update(index: Index, installed: str, channel: str, platform: str, arch: str, os_version: str, db_schema: int) -> Selection:
    """Pick the best release for this machine. A release is a candidate when it matches platform/arch/channel, is newer,
    is not revoked, runs on this macOS, and the installed version is at or above its ``min_compatible_version``.
    When the newest release needs an intermediate step, the highest directly-installable release is offered instead."""
    cur = parse_version(installed)
    same = [r for r in index.releases if r.platform == platform and r.arch == arch and r.version not in index.revoked_versions
            and (r.channel == channel or (channel == "beta" and r.channel == "stable") or channel == "dev")]
    if not same:
        return Selection("none_for_platform", reason=f"No release is published for {platform}/{arch} on the {channel} channel.")
    newer = sorted((r for r in same if parse_version(r.version) > cur), key=lambda r: parse_version(r.version), reverse=True)
    if not newer:
        return Selection("up_to_date", reason=f"Version {installed} is the newest on the {channel} channel.")
    top = newer[0]
    blocked: list[str] = []
    for r in newer:
        if _os_tuple(os_version) < _os_tuple(r.min_os):
            blocked.append(f"{r.version} needs macOS {r.min_os} or later (this Mac runs {os_version})")
            continue
        if r.schema_version < db_schema:
            blocked.append(f"{r.version} uses an older database schema than your data (schema {db_schema}); installing it would be a downgrade")
            continue
        if cur < parse_version(r.min_compatible_version):
            blocked.append(f"{r.version} requires {r.min_compatible_version} or newer to be installed first")
            continue
        return Selection("available", r, newer_exists=r.version != top.version,
                         reason="" if r.version == top.version else f"{top.version} becomes available after this step.")
    return Selection("incompatible", top, reason="; ".join(blocked) or "No compatible release.")
