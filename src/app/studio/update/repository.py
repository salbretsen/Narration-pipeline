"""ReleaseRepository implementations. A repository only says *where* to fetch; the signature decides *whether to trust*."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from ..interfaces import ReleaseRepository
from .net import GITHUB_REPO_RE, Fetcher, NetError, validate_repository


class GitHubReleasesRepository(ReleaseRepository):
    """Index published as a release asset on a rolling tag per channel:
    https://github.com/OWNER/REPO/releases/download/index-<channel>/release-index-<channel>.json
    Artifacts are ordinary release assets (github.com redirects to its CDN; every hop is host-checked)."""

    name = "github"

    def __init__(self, owner: str, repo: str, fetcher: Fetcher | None = None):
        self.owner, self.repo = owner, repo
        self.fetcher = fetcher or Fetcher()

    def index_url(self, channel: str) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/releases/download/index-{channel}/release-index-{channel}.json"

    def fetch_index(self, channel: str) -> bytes:
        return self.fetcher.get_bytes(self.index_url(channel))

    def download(self, url, dest, size, progress=None, cancelled=None) -> Path:
        prefix = f"https://github.com/{self.owner}/{self.repo}/releases/download/"
        if not url.startswith(prefix):
            raise NetError("The release artifact is not hosted on this project's GitHub Releases; refusing it.")
        return self.fetcher.download(url, dest, size, progress=progress, cancelled=cancelled)


class HttpsRepository(ReleaseRepository):
    """A first-party website exposing the same contract: ``<base>/<channel>/release-index.json``. Artifacts must be on the same host."""

    name = "https"

    def __init__(self, base_url: str, fetcher: Fetcher | None = None):
        self.base = base_url.rstrip("/")
        self.host = (urlsplit(self.base).hostname or "").lower()
        self.fetcher = fetcher or Fetcher(allowed_hosts={self.host})

    def index_url(self, channel: str) -> str:
        return f"{self.base}/{channel}/release-index.json"

    def fetch_index(self, channel: str) -> bytes:
        return self.fetcher.get_bytes(self.index_url(channel))

    def download(self, url, dest, size, progress=None, cancelled=None) -> Path:
        if (urlsplit(url).hostname or "").lower() != self.host:
            raise NetError("The release artifact is not on the update host; refusing it.")
        return self.fetcher.download(url, dest, size, progress=progress, cancelled=cancelled)


def make_repository(spec: str, fetcher: Fetcher | None = None) -> ReleaseRepository:
    spec = validate_repository(spec)
    m = GITHUB_REPO_RE.match(spec)
    if m:
        return GitHubReleasesRepository(m.group(1), m.group(2), fetcher)
    return HttpsRepository(spec, fetcher)
