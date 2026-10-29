"""Authenticated, user-level application updates.

The pieces are deliberately small and separately testable:

  layout      where releases, runtimes, state and logs live (outside every release)
  trust       pinned Ed25519 public keys; verifies signed release metadata
  manifest    the signed release-index contract and update selection (semver, channel, compatibility)
  net         HTTPS-only fetcher: host allowlist, redirect checks, size limits, resumable downloads
  archive     hardened tar.gz extraction
  repository  ReleaseRepository implementations (GitHub Releases first, generic HTTPS, local dev)
  store       immutable release directories and the atomic active-release pointer
  status      persisted update state, progress and history shown by the Update Center
  manager     check / download / verify / stage (runs inside the web process; never touches the running release)
  helper      the separate process that drains the queue, switches the pointer, restarts and rolls back
"""
