# 12. Container image (non-Mac, mock/CPU only)

**Status: written, NOT built, NOT run, NOT tested.** `docker --version` works on the dev machine (28.4.0) but the Docker daemon was not
running (`error during connect ... dockerDesktopLinuxEngine`), so no image was built. Treat the files as an unverified starting point.

## What it is, and what it is not

- `Dockerfile` (repo root), `.dockerignore`, `docker/entrypoint.sh`. Base `python:3.12-slim` plus `ffmpeg`; installs `src/app/requirements.txt`
  (portable packages only) and copies `src/app/studio`.
- **It runs the mock engine only** (`STUDIO_ENGINE=mock`): deterministic placeholder audio, not speech. The real engine needs `mlx-audio` /
  `mlx-whisper`, which exist only for Apple Silicon and are deliberately not installed. Until a CUDA/CPU/remote `TTSProvider` exists (doc 10 section 8,
  "designed, not implemented"), the container is useful for trying the UI, API, parsing/chunking/narrative pipeline and CI, not for producing a real audiobook.
- `requirements.lock` is **not** used: it is hash-pinned and generated from both requirements files (it includes the MLX packages), and the lock tool does
  not yet emit a non-MLX lock. The image therefore resolves `requirements.txt` ranges at build time (less reproducible than the Mac install). A future
  `tools/lock_requirements.py` target for a portable lock would fix that.

## Run

```text
docker build -t audiobook-studio:0.5.x .
docker run -d --name studio -p 8080:8080 -v studio-data:/data audiobook-studio:0.5.x
docker logs studio        # first run prints the starter admin password once (user "admin"); you must change it at first sign-in
```

- Runs as non-root user `studio` (uid 10001). All state (database, settings, library, logs) is under `/data` (`AUDIOBOOK_STUDIO_HOME`); use a volume.
- Serves plain HTTP on 8080 bound to all interfaces with sign-in required (the entrypoint passes `--allow-remote`). Reach it by a host name or IP not
  on the default Host allowlist by adding it in Settings > Server (`allowed_hosts`), or put a TLS reverse proxy in front. The microphone needs HTTPS or
  localhost (CLAUDE.md rule 7), so the recorder lab works only through `localhost` port-forwarding or an HTTPS proxy.
- A `HEALTHCHECK` polls `/api/auth/status`.

## Upgrades

By **image tag only**: build or pull `audiobook-studio:<new version>`, stop and remove the container, start the new one on the same `/data` volume
(migrations run at startup, as on the Mac). There is no in-container self-replacement. Back up the volume before upgrading across a minor version.

## Update client: unmanaged in the container

`Layout.managed` is true only when `<root>/app/current.json` exists (written by the Mac installer). The container never creates it
(`default_root()` resolves to `~/.local/share/audiobook-studio` for the `studio` user and nothing is installed there), so
`UpdateManager.status()` reports `managed: false` with `not_managed_reason`, and the check/apply paths raise `UpdateError`. The existing test
`test_unmanaged_install_explains_itself` covers the logic; the in-container behaviour itself was not exercised. Note the reason text names the Mac
installer; a container-specific message is a small follow-up.
