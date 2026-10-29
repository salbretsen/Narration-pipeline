"""HTTPS-only fetching for the updater. The web process never fetches a URL it was handed by a client:
the repository comes from trusted configuration and artifact URLs come from a *signature-verified* index.

Rules enforced here: https only (plain http / file only for the explicit dev repository), host allowlist
checked on the first request and on every redirect, at most 5 redirects, hard size caps while streaming,
and resumable downloads (HTTP Range) into a ``.part`` file that is only renamed once complete."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

# Hosts a release may legitimately be served from. A first-party release host is added here when one exists;
# it is deliberately code, not user configuration.
TRUSTED_HOSTS = {"github.com", "api.github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com",
                 "github-releases.githubusercontent.com"}
MAX_REDIRECTS = 5
MAX_INDEX_BYTES = 1 << 20          # 1 MiB of release metadata is generous
MAX_ARTIFACT_BYTES = 2 << 30       # 2 GiB
GITHUB_REPO_RE = re.compile(r"^github:([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$")


class NetError(Exception):
    """Network/policy failure. ``offline`` is True when the cause looks like missing connectivity."""

    def __init__(self, message: str, offline: bool = False):
        super().__init__(message)
        self.offline = offline


def validate_repository(spec: str, extra_hosts: set[str] | None = None) -> str:
    """Accept ``github:owner/repo`` or an https URL whose host is trusted. Anything else is rejected."""
    spec = (spec or "").strip()
    if GITHUB_REPO_RE.match(spec):
        return spec
    u = urlsplit(spec)
    hosts = TRUSTED_HOSTS | (extra_hosts or set())
    if u.scheme == "https" and u.hostname and u.hostname.lower() in hosts and not u.username and not u.password:
        return spec
    raise ValueError("The update repository must be github:OWNER/REPO or an https URL on a trusted release host.")


def _chunks(resp: httpx.Response):
    """Raw chunks exactly as received (no re-chunking, so a dropped connection keeps what already arrived).
    In-memory responses (test doubles) are already read; serve their content in slices."""
    if resp.is_stream_consumed:
        data = resp.content
        for i in range(0, len(data), 1 << 16):
            yield data[i:i + (1 << 16)]
    else:
        yield from resp.iter_raw()


class Fetcher:
    def __init__(self, allowed_hosts: set[str] | None = None, transport: httpx.BaseTransport | None = None, allow_insecure: bool = False,
                 timeout: float = 30.0):
        self.allowed = {h.lower() for h in (allowed_hosts if allowed_hosts is not None else TRUSTED_HOSTS)}
        self.allow_insecure = allow_insecure  # only the explicit local dev repository sets this
        self._transport = transport
        self._timeout = timeout

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport, follow_redirects=False, timeout=self._timeout,
                            headers={"User-Agent": "AudiobookStudio-Updater", "Accept-Encoding": "identity"})

    def _check_url(self, url: str) -> None:
        u = urlsplit(url)
        host = (u.hostname or "").lower()
        if u.username or u.password:
            raise NetError("Credentials in update URLs are not allowed.")
        if u.scheme != "https" and not (self.allow_insecure and u.scheme == "http" and host in ("127.0.0.1", "localhost")):
            raise NetError(f"Refusing a non-HTTPS update URL ({u.scheme}).")
        if host not in self.allowed:
            raise NetError(f"Refusing update host '{host}': it is not a trusted release host.")

    def _open(self, client: httpx.Client, url: str, headers: dict | None = None):
        """GET with manual redirect handling so every hop is re-validated. Returns a streaming response context."""
        hops = 0
        while True:
            self._check_url(url)
            try:
                req = client.build_request("GET", url, headers=headers or {})
                resp = client.send(req, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.NetworkError) as exc:
                raise NetError(f"Cannot reach the update server ({type(exc).__name__}). Check the internet connection.", offline=True) from exc
            except httpx.HTTPError as exc:
                raise NetError(f"Update request failed ({type(exc).__name__}: {exc}).") from exc
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location", "")
                resp.close()
                hops += 1
                if hops > MAX_REDIRECTS or not loc:
                    raise NetError("Too many redirects from the update server.")
                url = str(httpx.URL(url).join(loc))
                continue
            return resp

    def get_bytes(self, url: str, max_bytes: int = MAX_INDEX_BYTES) -> bytes:
        with self._client() as c:
            resp = self._open(c, url)
            try:
                if resp.status_code != 200:
                    raise NetError(f"The update server answered HTTP {resp.status_code}.", offline=resp.status_code >= 500)
                clen = resp.headers.get("content-length")
                if clen and clen.isdigit() and int(clen) > max_bytes:
                    raise NetError("The response is larger than the allowed limit.")
                buf = bytearray()
                try:
                    for chunk in _chunks(resp):
                        buf += chunk
                        if len(buf) > max_bytes:
                            raise NetError("The response is larger than the allowed limit.")
                except httpx.HTTPError as exc:
                    raise NetError(f"The connection dropped ({type(exc).__name__}).", offline=True) from exc
                return bytes(buf)
            finally:
                resp.close()

    def download(self, url: str, dest: Path, expected_size: int, max_bytes: int = MAX_ARTIFACT_BYTES,
                 progress: Callable[[int, int], None] | None = None, cancelled: Callable[[], bool] | None = None) -> Path:
        """Download to ``dest`` (resuming ``dest.part`` when possible). The size must equal ``expected_size`` exactly."""
        if expected_size <= 0 or expected_size > max_bytes:
            raise NetError("The release declares an unacceptable download size.")
        dest = Path(dest)
        part = dest.with_name(dest.name + ".part")
        dest.parent.mkdir(parents=True, exist_ok=True)
        have = part.stat().st_size if part.exists() else 0
        if have > expected_size:
            part.unlink()
            have = 0
        with self._client() as c:
            while True:
                headers = {"Range": f"bytes={have}-"} if have else {}
                resp = self._open(c, url, headers)
                try:
                    if resp.status_code == 416 and have == expected_size:
                        break
                    if resp.status_code == 200 and have:
                        have = 0  # server ignored Range: start over
                        part.unlink(missing_ok=True)
                    elif resp.status_code not in (200, 206):
                        raise NetError(f"The update server answered HTTP {resp.status_code}.", offline=resp.status_code >= 500)
                    if resp.status_code == 206:
                        m = re.match(r"bytes (\d+)-", resp.headers.get("content-range", ""))
                        if not m or int(m.group(1)) != have:
                            have = 0
                            part.unlink(missing_ok=True)
                            continue
                    clen = resp.headers.get("content-length")
                    if clen and clen.isdigit() and have + int(clen) > expected_size:
                        raise NetError("The download is larger than the size the signed release declares.")
                    with open(part, "ab" if have else "wb") as fh:
                        for chunk in _chunks(resp):
                            have += len(chunk)
                            if have > expected_size:
                                raise NetError("The download exceeded the size the signed release declares; aborted.")
                            fh.write(chunk)
                            if progress:
                                progress(have, expected_size)
                            if cancelled and cancelled():
                                raise NetError("Download cancelled; progress is kept so it can resume.")
                        fh.flush()
                        os.fsync(fh.fileno())
                except NetError:
                    raise
                except httpx.HTTPError as exc:
                    raise NetError(f"The connection dropped mid-download ({type(exc).__name__}); partial data was kept and the download can resume.", offline=True) from exc
                finally:
                    resp.close()
                break
        if part.stat().st_size != expected_size:
            raise NetError(f"Incomplete download ({part.stat().st_size} of {expected_size} bytes); it can be resumed.", offline=True)
        os.replace(part, dest)
        return dest
