# 10. Installation, launch and updates (v0.5)

Audiobook Studio stays a **locally installed web application** with a Python inference backend. It is *not* a native `.app`, `.dmg` or `.pkg`;
no Apple Developer ID, signing or notarization is involved, and nothing here asks anyone to disable or bypass a macOS security control.
This document is the reference for the managed install, the per-user service, the signed update system and how each is tested.

**What was and was not verified.** Tests marked *tested* ran on the Windows dev machine (mock engine, fake network, real processes where noted).
On **2026-10-29 the installer, service, updater and migration were also run on the production M3 Max (macOS 27.0)**: see
[Results of the first hardware run](#results-of-the-first-hardware-run) for exactly what passed and what has **not** been verified (notably: a truly
clean Mac, real Safari, the real GitHub download path). The production signing key (id `23bb536587702e29`) is generated and its public half is pinned
(v0.5.1); **no release has been published or signed on GitHub yet**, and the CI secret `RELEASE_SIGNING_KEY` still has to be set (the private key
file is on the dev machine, outside the repository).

## 1. The user flow

1. Download `install_audiobook_studio_mac.sh` (a plain shell script, about 1.4 MB, everything it installs besides the three pinned downloads is embedded).
2. **Inspect it** (recommended): `less install_audiobook_studio_mac.sh`, or `bash install_audiobook_studio_mac.sh --extract-only ~/inspect` to unpack
   the embedded application files without installing anything. Do not pipe it from the network into a shell.
3. `bash install_audiobook_studio_mac.sh --check` runs the pre-flight checks and changes nothing.
4. `bash install_audiobook_studio_mac.sh` installs. It asks where to keep the library and the model cache (or use `--library`, `--model-cache`) and
   whether to download the models now (`--download-models` about 6 GB, `--prefetch-all` about 14 GB) **before** anything large is fetched.
5. Open `http://127.0.0.1:7861` (the first free port from 7861 is chosen). Everything afterwards, including updates, happens in the web console.

The Terminal step is unavoidable and one-time. Needs network access to: `github.com` (+ its CDNs), `ffmpeg.martin-riedl.de`, `pypi.org` +
`files.pythonhosted.org`, and (only when models are downloaded) `huggingface.co`.

**Disk cost** (disclosed again by the pre-flight): about 5 GB for the application (private Python, about 1-2 GB of wheels including MLX, FFmpeg/FFprobe
about 130 MB unpacked, uv, releases); models 6-7 GB by default, up to 15 GB with all four; library space is yours (several GB of working audio per book).

**Prerequisites: none.** No Homebrew, Git, Python, pip, Node, Rust, Docker, Xcode or Command Line Tools. It uses only tools every Mac has:
`/bin/bash` (3.2), `/usr/bin/curl`, `tar`, `unzip`, `shasum`, `nc`, `/bin/launchctl`, `sw_vers`, `sysctl`. Homebrew is never invoked, in any code path,
including failures (enforced by `test_mac_installer_never_uses_forbidden_mechanisms`).

### What the installer does, in order

| Step | Detail |
|---|---|
| Pre-flight | macOS 14+, native arm64 (not Rosetta), RAM (16 GB minimum, 32 GB advised), disk per location, writable paths, **mounted** external volumes, reachability of the three download hosts (HEAD only), free port. Each failure says what to do. Stops before changing anything. |
| Tools | `uv`, `ffmpeg`, `ffprobe` into `<root>/app/tools/bin/`. Each download: HTTPS only (`--proto =https --proto-redir =https --tlsv1.2`), resumable `.part`, exact **size** check, then **SHA-256 against a value pinned inside the installer**; mismatch deletes the file and aborts (fail closed). Then FFmpeg is asked whether it can encode AAC (M4B) and MP3. |
| Release | Embedded application files are unpacked to `<root>/app/releases/<version>/` and verified with `MANIFEST.sha256`. |
| Python | `uv python install 3.12 --no-bin` into `<root>/app/python` (never the system Python, never `~/.local/bin`), then a venv at `<root>/app/runtimes/<runtime_id>/venv`, `uv pip install --require-hashes --only-binary :all: -r requirements.lock`. If any package has no prebuilt wheel the install **stops** with "packaging defect"; it never offers or triggers Xcode/CLT. The environment is marked `.complete` only at the very end, so a half-built one is never used. |
| MLX check | imports `mlx.core` and requires Metal to be available. |
| Activate | `ReleaseStore.activate()` (the application's own tested code): atomic pointer switch. Refuses to install an *older* version over a newer one (`--force-downgrade`). |
| State | `studio configure`: data under `<root>/state`, library and model cache as chosen, port. Existing settings, accounts and data are kept on re-runs. First run creates an administrator with a random password (printed once, saved mode 600). |
| Service | `audiobook-studio service install`: per-user LaunchAgent in the `gui/<uid>` domain. Waits for `/healthz`. |
| Done | Prints addresses, the Dock steps, locations and the command-line tool. |

It is **idempotent**: every step checks for a finished result (hash-verified tool, complete environment, verified release) and skips it; interrupted
downloads resume; a second install never removes books, settings, accounts or models. A lock directory prevents two installers running at once.

### Honest security statement

A shell installer avoids shipping an unsigned `.app`, but it is **not** a promise of zero macOS dialogs. Files fetched with `curl` carry no
`com.apple.quarantine` flag, and the FFmpeg builds are published by their author as code-signed, but we cannot test a fresh Mac here. If macOS
shows a prompt for one of the downloaded binaries, follow it. The project **never** runs `spctl --master-disable`, `xattr -d com.apple.quarantine`,
or tells anyone to. If a pinned binary cannot run under default Gatekeeper policy on a fresh Mac, the right fix is to change the dependency or
distribution mechanism and report the limitation (manual check M3 below tests exactly this).

The pinned artifacts (`tools/runtime-manifest.json`, hashes computed from the downloaded bytes **and** cross-checked against each publisher's own
checksum on 2026-10-29):

| Artifact | Version | Source | Licence |
|---|---|---|---|
| uv | 0.13.6 | github.com/astral-sh/uv release asset `uv-aarch64-apple-darwin.tar.gz` | MIT OR Apache-2.0 |
| ffmpeg / ffprobe | 9.0.2 | ffmpeg.martin-riedl.de versioned URL (not a "latest" redirect) | **GPL-3.0-or-later build** (`--enable-gpl --enable-version3`). Downloaded from its publisher onto the user's Mac and run as a separate program; this project does not bundle or redistribute it. Verified encoders: `aac`, `aac_at`, `libmp3lame`. |
| Python | 3.12 | python-build-standalone via the pinned uv, which verifies the digest embedded in its own metadata | PSF |
| wheels | `requirements.lock` | PyPI, hash-pinned, 88 packages | per package |

If the FFmpeg publisher ever removes the pinned build, the installer fails closed (404 or checksum error). Fix: re-pin a new build after repeating both
checks and ship a new installer; do not loosen the check. libsndfile and PortAudio are not installed separately: the macOS arm64 wheels of the Python
audio packages bundle them (`tools/check_lock_wheels.py` proves a usable wheel exists for every pinned package).

## 2. Layout (user data never lives in a release)

```
~/Library/Application Support/Audiobook Studio/
  audiobook-studio                 command-line tool (absolute paths only)
  app/releases/<version>/          immutable, replaceable application code (one directory per release, 3 kept)
  app/current -> releases/<ver>    symlink launchd uses; app/current.json is the authoritative pointer (+ previous)
  app/venv -> runtimes/<id>/venv     (directory link: the service runs app/venv/bin/python; a link to the interpreter file alone loses the venv)
  app/runtimes/<runtime_id>/       private Python environments (shared by releases with the same requirements.lock)
  app/python/  app/uv-cache/       uv-managed Python and cache (inside app/, never ~/.local)
  app/tools/bin/                   uv, ffmpeg, ffprobe (pinned, verified)
  app/service.json                 app-controlled manifest the service definition is built from
  state/                           AUDIOBOOK_STUDIO_HOME: config/ (settings, accounts, TLS), data/ (SQLite), run/, backups/, update/
  models/huggingface/              default model cache (changeable, may be an external disk)
~/Library/Logs/Audiobook Studio/   launchd.out.log, server.log, worker.log, updater.log, install-*.log
<library>                          the storage root you chose: manuscripts, voices, segments, masters, exports (any disk, e.g. /Volumes/AudioSSD/...)
```

Paths with spaces are used throughout (the default root has one) and are tested. The library root, per-category folders and the model cache
(`settings.models.cache_dir`, exported as `HF_HOME` to the server and worker) are independent of the release directory; the database stays on the
internal disk so queue state survives disk removal. An unmounted `/Volumes/X` is reported precisely ("The volume 'X' is not mounted") by the
pre-flight, `doctor`, and the existing storage layer pauses affected books and resumes them when the disk returns.

Reproducibility is unaffected by updates: narrator choice, source-text hashes, per-segment settings and model versions are stored per book/segment
(v0.2/v0.3 data model). An application update does **not** invalidate completed segments; only changed text, narrator, model or settings do.

## 3. The per-user service

`com.audiobookstudio.app`, `~/Library/LaunchAgents/com.audiobookstudio.app.plist`, bootstrapped into `gui/<uid>` (no `sudo`, no `/Library`, no
LaunchDaemon, no `UserName`). Built by `platform_services.build_plist` from `app/service.json` and fixed paths:

- `RunAtLoad` (starts at login, no Terminal); `KeepAlive {SuccessfulExit: false}` (restarts after a crash or failure, **not** after a clean stop);
  `ThrottleInterval 10`; `ExitTimeOut 45` so an in-flight segment can finish; one instance per label (launchd), plus a pid-file guard and a port
  check in `studio serve` ("Port N is already in use" / "already running (pid N)").
- Binds `127.0.0.1` by default (`server.host`); the installer picks the first free port from 7861. Nothing is exposed to the LAN unless
  `server.allow_remote` is set deliberately (sign-in is then mandatory, as before).
- `/healthz` (public): `{ok, version, db, schema}`; from loopback it also reports queue counts and whether the library root is writable.
  `/api/version`: version, build, channel, schema, managed.
- Stop/restart: the worker's existing recovery returns an interrupted job to the queue at its position; completed WAVs are never touched.
- CLI: `audiobook-studio doctor | start | stop | restart | status | logs [-f] | version | update --check | update --install [--when now|after_job] [--yes] | service install|uninstall | migrate-legacy DIR`.
- Uninstall: `install_audiobook_studio_mac.sh --uninstall` (service only), `--purge-app` (also application files), `--purge-data` (additionally
  books/settings/models, only after typing a confirmation phrase). Without those flags **no data is deleted**.

### Dock icon (Safari, macOS Sonoma 14 or later)

1. Open `http://127.0.0.1:<port>` in Safari (the installer prints the port; default 7861).
2. Menu **File > Add to Dock...**, name it, **Add**.

The Dock icon is a Safari *web app*, not an executable of ours. The studio itself keeps running under launchd whether or not the Dock icon is open;
closing the web app does not stop it. (Microphone recording needs HTTPS or localhost; 127.0.0.1 counts as localhost.)

## 4. Release and trust design

**Roles.** The pinned **public key** lives in the installed application (`src/app/studio/update/pinned_keys.json`, empty today). The **private key**
is held by the owner and as a protected CI secret; it is never in Git, in an archive, or reachable through a web API.

**Signed index.** One JSON envelope per channel, `release-index-<channel>.json`: `{"signed": {...}, "signatures": [{"keyid", "sig"}]}`, Ed25519 over
the canonical JSON (sorted keys, no whitespace) of `signed`. A SHA-256 published next to an archive proves nothing about who built it, so the
archive is authenticated *through* the signed index (which carries its size and SHA-256), never by a hash from the same site.

```json
{ "schema": 1, "channel": "stable", "sequence": 1793294400, "generated": "...", "expires": "...",
  "revoked_versions": [], "revoked_keyids": [],
  "releases": [ { "version": "0.5.1", "channel": "stable", "platform": "macos", "arch": "arm64",
                  "url": "https://github.com/OWNER/REPO/releases/download/v0.5.1/audiobook-studio-0.5.1-macos-arm64.tar.gz",
                  "size": 422406, "sha256": "<64 hex>", "published": "2026-10-29T00:00:00Z",
                  "notes_url": "https://github.com/OWNER/REPO/releases/tag/v0.5.1", "notes": "plain text, max 8000 chars",
                  "min_compatible_version": "0.4.0", "min_os": "14.0", "schema_version": 2, "runtime_id": "<16 hex>" } ] }
```

Client rules (`update/manifest.py`, `trust.py`, `net.py`, `archive.py`, `manager.py`), each covered by a test:

| Threat | Defence |
|---|---|
| Forged / edited index | Ed25519 verification against pinned keys; unknown or revoked key id rejected |
| Replay of an older, genuine index (rollback) | `sequence` must not decrease; `expires` enforced (freeze attack) |
| Swapped archive | size + SHA-256 from the signed index; mismatch deletes the file; the unpacked `__version__` must equal the signed version and be newer |
| Unsafe archive | gzip magic check, no symlinks/hardlinks/devices, no absolute/`..`/backslash/drive paths, one top-level `audiobook-studio-<v>/`, no duplicates, file-count and unpacked-size limits (bomb), per-file hashes against `MANIFEST.sha256`, and **no extra files** |
| Redirect / origin games | HTTPS only; host allowlist (GitHub + its CDNs) re-checked on **every** redirect hop; max 5 hops; artifact URL must be under the configured repository's own `releases/download/`; credentials in URLs rejected |
| Client-chosen URL | none: no endpoint accepts a URL or a command. The repository comes from validated settings (`github:owner/repo` or an https URL on a trusted host); the install endpoint takes only a version string that must equal the *staged* version, plus `confirm: true` |
| Oversized / endless downloads | index 1 MiB, artifact 2 GiB, and never more bytes than the signed size; resumable via HTTP Range into a `.part` file |
| No key configured | the updater is **disabled** (state "Switched off"); it never falls back to "unsigned" |
| Development convenience | `TrustStore(allow_unsigned=True)` exists for tests/dev only; the Settings channel list contains only `stable` and `beta`, and the production default pins keys |

**Key rotation.** Generate the new key; ship its public key in `pinned_keys.json` in a release signed by the *old* key (both pinned for one cycle);
switch CI to the new secret; drop the old public key in the following release. **Compromised key.** Publish a release signed by a still-trusted key
that removes the compromised public key and lists its id in `revoked_keyids` (also list any bad versions in `revoked_versions`); installed studios
that accept that release stop trusting the key. If every key is compromised there is deliberately no remote recovery: users must obtain a new
installer out of band. Never reuse a leaked key.

**A second repository.** `HttpsRepository(base)` serves the same contract at `<base>/<channel>/release-index.json` with artifacts on the same host;
`make_repository()` selects GitHub or HTTPS from the validated setting. The host must be added to `TRUSTED_HOSTS` in code (not configuration).

### Signing secrets (GitHub Actions)

1. On the owner's trusted machine: `python tools/release_keys.py generate --private-out ~/secure/audiobook-studio-release.key --pin`
   (refuses to write inside the repository, never prints the private key, file mode 600). Review and commit **only** the changed
   `pinned_keys.json` (the public key).
2. Repository **Settings > Environments > New environment `release`**: add *required reviewers*; add the secret `RELEASE_SIGNING_KEY` there (paste the
   key file's single line). Keep an offline backup of the file.
3. Tag `vX.Y.Z` matching `__version__`. `release.yml` tests, builds, then (in the protected environment) signs, **verifies the produced index against the
   pinned key** (`tools/verify_index.py`, which also checks archive size/hash), and only then publishes the release and replaces the rolling
   `index-<channel>` asset. With no secret it publishes a clearly labelled **UNSIGNED prerelease and no index**; nothing is ever reported as signed
   unless a key was present. Hardening to consider: pin the actions to commit SHAs, restrict who can push `v*` tags (tag protection rules).

## 5. The Update Center

Sidebar **Updates** (plus a dot on the nav item and a notice on the Overview when something needs attention; Settings also offers the model cache
folder). The page shows installed version/build, channel, latest compatible version, last check and last *successful* check, status and error, release
notes (plain text, never HTML; the link is only shown for `https://`), eight labelled progress bars (check, download, verify, stage, drain,
install, restart, health) with `aria-live`, **Check for updates**, **Download & verify**, **Install now**, **Install after current book**,
**Cancel installation**, **Restart service**, automatic-check preferences (default daily; checks only, never downloads or installs by itself), and the
history table. States: up to date, available, downloading, verifying, staging, staged, waiting, draining, installing, restarting, health check,
succeeded, failed, rolled back, incompatible, offline, switched off. Administrators act; operators can look. Every state-changing route is
admin-only, requires the `X-Studio-Request` header, same-origin and Host checks, like the rest of the API.

## 6. Safe update execution

The web process only **checks, downloads, verifies and stages** (adds a new directory under `releases/`; it never touches the running one). Switching
and restarting is done by a separate process:

| # | Step | Where |
|---|---|---|
| 1-2 | fetch the signed index, compare platform/arch/channel/version/OS/schema, download to staging (resumable) | web process |
| 3 | verify signature (pinned key) and SHA-256 | web process |
| 4 | free-space check (3x the archive), safe extract, per-file manifest check, runtime check/creation (`uv`, wheels only, `--require-hashes`) | web process |
| 5 | preflight again in the updater (tree re-hashed, folders writable) and consistent SQLite backup (online-backup API) + copy of `config/` | updater |
| 6 | **drain**: the queue hold (`run/dispatch.hold`) stops new work; the segment in flight finishes and is on disk. `now` = next segment boundary; `after_job` = running job continues to its end, nothing else starts | updater + worker |
| 7 | stop service, atomic pointer switch (+ interpreter link), `studio migrate` from the **new** release (takes its own DB backup first), start | updater |
| 8 | `/healthz` must report the target version with a working database; storage problems are reported as warnings, not blamed on the release | updater |
| 9 | on any failure: stop, repoint to the previous release, restore the database backup **only if the schema moved** (the failed DB is kept as `studio.failed-*.db`), start, confirm health, status `rolled_back`. If even that fails: `state/update/REPAIR.txt`, status "failed/repair", **no user data deleted** | updater |

The updater is a separate process: a one-shot launchd job (`com.audiobookstudio.updater`, outside the service's process group so restarting the service
cannot kill it) or `audiobook-studio update --install` from Terminal. It imports everything it needs before the switch so it never mixes old and new
modules. It runs only authenticated, versioned artifacts of this application; there is no `curl | bash`, no arbitrary command from an API, no
`brew upgrade`, no `git pull`, no `sudo`.

## 7. Migration from the Bash/Homebrew installation

`bash install_audiobook_studio_mac.sh --migrate-from ~/audiobook-studio-claude [--keep-network]` (it also offers when it finds that folder).
`studio migrate-legacy DIR [--dry-run]`:
1. Backs up `config/` and the database (SQLite online backup, safe while the old service runs) into `state/backups/legacy-<stamp>/`.
2. Builds the new state from **copies**; pins the library root and model cache to the old absolute locations (nothing is copied or re-downloaded, books keep their paths).
3. Turns network binding off (new default is this Mac only) unless `--keep-network`; says so.
4. Migrates the copy and starts the **new** backend on a spare port with no worker; it must answer `/healthz`.
5. Only then stops the old service, moves its LaunchAgent plist into the backup (restorable), installs the new service.
The old folder is never modified (a test compares file hashes before and after) and Homebrew and every other package are untouched. If the test of the
new backend fails, the old service is never stopped. Rollback: restore the saved plist, `audiobook-studio stop`.
The legacy `install_audiobook_studio_claude.sh` is still generated for existing deployments but **needs Homebrew and is not the supported path**.

## 8. Platform and provider boundaries

`studio/interfaces.py` defines `PlatformService`, `InferenceProvider`, `TTSProvider`, `ASRProvider`, `ModelRegistry`, `ReleaseRepository`,
`UpdateManager`, `StorageManager`, `QueueScheduler` (abstract; instantiating one raises). Implemented: `MacPlatformService` (launchd) and
`ProcessPlatformService` (plain processes; used in tests and as the seed for Linux), `FileQueueScheduler`, `SettingsModelRegistry` + MLX/mock providers,
`StudioStorageManager`, `GitHubReleasesRepository` / `HttpsRepository`, `ReleaseUpdateManager`. The web layer never names launchd or GitHub (tested).
**Designed, not implemented:** CUDA/CPU providers, remote HTTP TTS/ASR endpoints, optional quality-scoring/planning models, Windows/Linux services, and
containers (image-tag upgrades; no in-container self-replacement). No model/provider selector is shown in the UI until more than one usable provider exists.

## 9. Tests

`pytest` from `src/app` (see doc 7 for the baseline and counts). Update-related files: `test_update_core.py` (signatures, selection, net, archive,
store, runtime, launchd/process services, interfaces), `test_update_flow.py` (manager, helper with fakes, queue drain with the real worker, API/CSRF/roles,
and a **real-process** end-to-end: two real releases of this application, a real server, real `studio migrate`, real health checks, a good update
then a bad one that rolls back), `test_update_install.py` (installer policy and `--extract-only`, fail-closed pinned download in real bash, lock,
release tooling, migration, CLI). Browser: `python tools/e2e/updates_ui.py OUT chromium|webkit` drives the Update Center against a fake GitHub
(WebKit is Safari's engine, **not Safari**).

| Required scenario | Covered by | Real hardware needed? |
|---|---|---|
| clean-machine install, no Homebrew/Git/Python/FFmpeg/Xcode, zero sudo, idempotent | policy scan + fail-closed helper + extract + `--check` (**partial**) | **yes: M1-M4** |
| migration without touching Homebrew/books | `test_migration_*` (old tree hash-identical) | M5 for launchd steps |
| launchd start/stop/restart/recovery/no duplicates | plist + `launchctl` command sequences (fake runner); real process service start/stop/restart/idempotent start | **yes: M6** |
| Safari-compatible UI, responsive | Chromium + WebKit flow, 390 px no overflow | **yes: M7 (real Safari)** |
| discovery, no-op, available, offline, incompatible | `test_check_states`, `test_incompatible_*`, selection tests | no |
| tampered archive, bad signature, traversal, untrusted origin | `test_tampered_*`, `test_invalid_signature_*`, `test_archive_*`, `test_artifact_from_untrusted_origin_*`, `test_replayed_*` | no |
| active-queue update, segment boundary, pause/resume, remaining manuscripts | `test_hold_now_*`, `test_after_job_*` (real worker, mock engine) | M8 with real MLX |
| interrupted download, unavailable external storage | `test_interrupted_download_*`, `check_available` / preflight tests | M9 (real unplug) |
| bad release rollback, originals/voices/model cache/DB preserved | `test_failed_migration_*`, `test_unhealthy_*`, `test_double_failure_*`, real-process E2E | no |
| upload, preview, TTS/ASR, review, streaming, download, M4B | unchanged v0.4 suite (mock engine) | **M8: real MLX** |

## Manual macOS checks

Run on the M3 Max (or any Apple Silicon Mac, ideally a fresh user account or machine with no developer tools). Record results; anything that
fails is a defect to report, not to work around.

- **M1 Clean preconditions:** `which brew git python3 ffmpeg; xcode-select -p` (all absent or not used; note any macOS stub prompts). `bash install_audiobook_studio_mac.sh --check` passes; `ls /opt/homebrew /usr/local/bin` unchanged afterwards.
- **M2 Install:** `bash install_audiobook_studio_mac.sh --library "/Volumes/AudioSSD/Audiobooks" --model-cache "/Volumes/AudioSSD/models" --no-models`; confirm no password prompt, no Xcode/CLT dialog, `~/.zshrc` and `PATH` untouched, nothing written outside `~/Library/Application Support/Audiobook Studio`, `~/Library/Logs/Audiobook Studio`, `~/Library/LaunchAgents/com.audiobookstudio.app.plist`, your chosen folders. Re-run: finished steps skip.
- **M3 Gatekeeper:** `codesign -dv --verbose=2 "$HOME/Library/Application Support/Audiobook Studio/app/tools/bin/ffmpeg"` and `.../uv`; `spctl --assess --type execute -vv` on both (informational); run `ffmpeg -version`. Report any dialog. Do **not** clear quarantine; if one appears, the pin must change.
- **M4 Audio stack:** `~/Library/Application\ Support/Audiobook\ Studio/audiobook-studio doctor` (MLX device, FFmpeg AAC/MP3 encoders); `python -c "import soundfile, sounddevice"` inside the private venv (bundled libsndfile/PortAudio).
- **M5 Migration:** `--migrate-from ~/audiobook-studio-claude --dry-run` first; then for real; compare `shasum -a 256` of the old folder before/after; books open; old LaunchAgent plist saved in `state/backups/legacy-*/`.
- **M6 launchd:** log out/in (service up without Terminal); `kill -9 $(pgrep -f "studio serve")` (restarts after about 10 s, one process); `audiobook-studio stop` (stays stopped); `launchctl print gui/$(id -u)/com.audiobookstudio.app`.
- **M7 Safari:** Safari at the printed address; File > Add to Dock; Updates page layout at narrow width; mic recording works on 127.0.0.1.
- **M8 Real narration:** the v0.4 baseline (doc 7) plus: queue two books, press "Install after current book" with a fake newer release from a test channel (build one with a throw-away key pinned in a scratch install), confirm the first book finishes, the second resumes after restart, completed WAVs are untouched, M4B export works.
- **M9 External disk:** unplug the library disk mid-job: affected book pauses with `storage:`; Updates still works; replug resumes. `--check` with the disk unplugged names the volume.
- **M10 Update end-to-end:** with a real signed release (after the owner pins a key): Check, Download, Install; confirm rollback by publishing a deliberately broken test release to a scratch repo/channel.

## Results of the first hardware run

M3 Max, 48 GB, macOS 27.0, 2026-10-29, driven over SSH (no GUI session). The Mac was **not** a pristine machine: Command Line Tools and system git were
installed and Homebrew exists (it is not on the SSH PATH). The installer used none of them (nothing Homebrew-related was touched, no password prompt, no
profile edited), but "works on a Mac with none of them" is **not proven** by this run.

| Check | Result |
|---|---|
| M1/M2 pre-flight and install (scratch root, then production root) | **PASS** after fixes. Real bugs found and fixed in 0.5.2: a `v` prefix in the release folder name; a re-run reused stale files of the same version; the `app/venv-python` file link ran Python without its venv (now the directory link `app/venv`) |
| Idempotent re-run | **PASS**: tools, release (same build) and environment skipped, accounts kept |
| M3 Gatekeeper | FFmpeg/FFprobe are signed by Developer ID *Martin Riedl (KU3N25YGLU)*, uv by *OpenAI OpCo, LLC (2DC432GLL2)* (Astral), hardened runtime, **no quarantine flags anywhere in the tree**, all ran with no dialog. `spctl --assess` says "rejected (the code is valid but does not seem to be an app)", the normal answer for command-line tools. A *browser-downloaded* installer was not tested |
| M4 audio/MLX | **PASS**: MLX 0.32.4 with Metal, FFmpeg 9.0.2 with AAC and MP3 encoders, `doctor` all OK |
| M6 launchd | **PASS**: starts; `kill -9` of the server restarts it in seconds as one process; a supervised worker exits on its own when its server is killed (fixed in 0.5.2); `stop` leaves nothing running and stays stopped; a second `start` is idempotent; a second `serve` refuses ("port in use") |
| M8 real narration (smoke test: real Qwen3-TTS + Whisper) | **PASS**: CustomVoice RTF 0.33, clone RTF 0.42, 20/20 queued segments passed QA, AAC M4B export. It first failed ("Tokenizer not loaded") because the model cache was applied inside Python after `huggingface_hub` was imported; fixed by having the launcher set `HF_HOME` before Python starts |
| M10 signed update through the real launchd handoff (signed with the production key, served from a local directory) | **PASS** 21/21: tampered archive refused, index signed by another key refused, good update installed (pointer switched, database backup, data intact, hold released), deliberately broken release rolled back automatically |
| Drain during real narration (`tools/e2e/mac_drain_test.py`) | **PASS**: update requested mid-job, queue held, service switched, job resumed by itself, 19/19 segments passed, **0 segments regenerated** |
| M5 migration from the Bash/Homebrew install (production) | **PASS**: 19 books / 386 segments / 24 voices, accounts and TLS certificate kept (same fingerprint), HTTP 8080 and HTTPS 8443 reachable from the Windows dev machine, unauthenticated API still 401, the old LaunchAgent plist saved in `state/backups/legacy-*/`, the old folder left in place |
| M9 external disk | **PASS** (pre-flight): an unmounted `/Volumes/AudioSSD` is named and stops the install; `/Volumes/<share>` (an SMB share) is accepted. A mid-job unplug was **not** tested |
| M7 Safari / Add to Dock | **NOT VERIFIED** (no GUI session). Chromium and WebKit-in-Playwright only |
| Real GitHub download path | **NOT VERIFIED**: nothing is published; `update --check` returns a clean "HTTP 404". The update tests used a local directory repository with the real signature checks |
| Clean-Mac claim (no CLT, Homebrew or git) | **NOT VERIFIED** (see above) |

Operational notes: the legacy worker (pid 57449) had outlived the legacy server and kept the GPU lease after the cutover, so it was stopped by hand.
Killing a server with `kill -9` used to leave its worker running for good; supervised workers now exit by themselves.

## Known limits and decisions

- The production public key is pinned (v0.5.1) but nothing is published: until a signed index exists on GitHub, `update --check` reports HTTP 404 (verified). The private key lives only on the owner-side dev machine (`~/.audiobook-studio-release/release.key`, mode 600) and must be added as the Actions secret `RELEASE_SIGNING_KEY` (no `gh` CLI is installed on the dev machine; use the GitHub web UI) and backed up offline.
- `requirements.lock` is a **universal** lock resolved on a Windows machine with `uv pip compile --universal`; the macOS target resolver assumes macOS 13 and rejects MLX (14+). `tools/check_lock_wheels.py` verifies wheels exist; actually installing them on a Mac is manual check M2.
- The lock pins `transformers==5.21.0`, `huggingface-hub==2.3.0`, `pydantic==2.14.1` exactly (the combination verified on the production Mac); the other pins resolved on 2026-10-29 were exercised by the hardware run (install, smoke test, drain test), but a newer set must be re-verified the same way before it ships.
- The FFmpeg build is GPLv3 and from a single third-party site; an outage or removal blocks fresh installs (fail closed) until re-pinned.
- Updates install a new Python environment only when `requirements.lock` changes (`runtime_id`); that path uses the app-owned `uv` and has been tested with a fake runner only.
- `after_job` waits at most 7 days; `now` waits at most 15 minutes for the segment in flight, then gives up without changing anything.
- Windows, Linux and container support are designed for (interfaces, `ProcessPlatformService`) and not implemented or claimed.
