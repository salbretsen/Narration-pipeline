# Changelog

Versions are X.Y.Z: X = Major (a major release update, owner-triggered only); Y = Feature (any new feature);
Z = Minor/Patch (any bug fix, feature alteration, documentation update or housekeeping).

## v0.5.8
- First sign-in without leaving the web console: a fresh managed install creates `admin` with the starter password `admin`, flagged "must change". The sign-in
  leads straight to a "Choose a new password" page; every other API call is refused (403, `must_change_password`) until it is done. The random password file
  `initial-admin-password.txt` is no longer written. Existing accounts are unaffected. `audiobook-studio auth add-user NAME --temporary`.
- Note: until the password is changed, anyone who can reach the Mac on the network can sign in as `admin`; the installer says so.

## v0.5.7
- Added the MIT `LICENSE` (copyright notice must be kept: others may use it if properly credited).
- A fresh managed install now serves the LAN by default (HTTP and HTTPS, sign-in required); `--local-only` restricts it to this Mac. Existing
  installs and `--migrate-from` keep their current network setting. `audiobook-studio configure --lan | --local-only`.

## v0.5.6
- Documentation only: dropped the GitHub branch/tag protection setup (roadmap B3) as unnecessary for this tool. The automatic workflow guard stays; no owner-side configuration is required.

## v0.5.5
- Branch and channel policy adopted (docs/11-roadmap.md section 1, owner-approved): `main` = stable (`vX.Y.Z` tags), `dev` = beta (`vX.Y.Z-beta.N` tags),
  day-to-day work on `dev`, promotion to stable by fast-forwarding `main`.
- Release workflow guard: a plain `vX.Y.Z` tag is refused unless its commit is already on `origin/main` (beta tags: `origin/dev` or `origin/main`), and a
  release that already exists is never moved or re-created.
- Version scheme renamed (numbering unchanged): X = Major (owner only), Y = Feature, Z = Minor/Patch (bug fix, feature alteration, docs, housekeeping).
  Docs, CLAUDE.md and the module docstring updated.

## v0.5.4
- Documentation only: `docs/11-roadmap.md` (was a draft for owner review: branch/tag/channel policy, remaining roadmap, open owner decisions). Not yet tagged or published.

## v0.5.3
- First release delivered through the real update path (GitHub Releases, signed index, in-app Update Center, new private Python environment built by
  the updater). No functional change to narration, QA, review or export. Repository history was scrubbed of local network details before this release;
  the screenshots were removed and the lock tool no longer writes the developer's temp path into `requirements.lock`.

## v0.5.2
- Fixes found by the first real run on the M3 Max (macOS 27; results in doc 10):
  - Model cache: `HF_HOME` is now set by the launcher (and the LaunchAgent runs the launcher) before Python starts, instead of being changed in-process after
    `huggingface_hub` was imported (that produced "Tokenizer not loaded"). `Context.apply_model_cache_env` only acts if the library is not imported yet.
  - A supervised worker exits (after its current segment) when its server disappears, so `kill -9` of the server no longer leaves a stale worker holding the GPU lease.
  - `requirements.lock` pins `transformers 5.21.0`, `huggingface-hub 2.3.0`, `pydantic 2.14.1` (the combination verified on the Mac).
  - Re-running an installer build of the same version re-extracts the files (compares the build id).
- The stable interpreter path is now the directory link `app/venv` (a link to the interpreter file alone ran without the environment).
- Installer: the version carried a `v` prefix that the release store rejected; the bare version is used for release folders.

## v0.5.1
- **Production release-signing key pinned** (id `23bb536587702e29`, public half in `studio/update/pinned_keys.json`). The private key is held outside the
  repository by the owner's assistant session and must be set as the CI secret `RELEASE_SIGNING_KEY`; no release has been signed yet. The updater is
  now enabled and will accept only indexes signed by that key. Tests that assumed "no key" now use an empty trust store explicitly.

## v0.5.0
Installation, launch and updates. Nothing about narration, QA, review or export changed; the data model is unchanged (schema v2).
**Verified on the production M3 Max in v0.5.2** (`docs/10-installation-and-updates.md`, "Results of the first hardware run"); no release has been published or signed on GitHub yet.
- **No-prerequisites installer** `install_audiobook_studio_mac.sh`: needs no Homebrew, Git, Python, pip, FFmpeg, Node, Xcode or Command Line Tools,
  no sudo, and edits no shell profile or system directory. Installs a private Python 3.12 with the pinned `uv`, FFmpeg/FFprobe 9.0.2 (GPLv3 build,
  downloaded not bundled, AAC and MP3 encoders verified) into the app folder, and hash-locked prebuilt wheels (`requirements.lock`, 88 packages,
  `--require-hashes --only-binary :all:`; a package without a wheel stops the install instead of reaching for a compiler). Every download is size- and
  SHA-256-checked against values pinned in the script before use and fails closed. Pre-flight (macOS 14+, native arm64, RAM, disk per location, mounted
  external disks, writable paths, reachability, free port), `--check`, prompts or flags for library/model-cache location and the optional model
  download, idempotent and resumable, `--uninstall` (data only with a typed confirmation). The old Homebrew installer remains for existing installs.
- **Managed layout**: replaceable releases under `app/releases/<ver>` with an atomic `current` pointer, private runtimes, user data in `state/`, logs in
  `~/Library/Logs/Audiobook Studio`, model cache and library anywhere you choose (`models.cache_dir` setting; applied as `HF_HOME`).
- **Per-user launchd service** (`gui/<uid>`, no root): starts at login, restarts after failures only, 10 s throttle, 127.0.0.1 by default, port
  collision and double-run detection, `GET /healthz` and `GET /api/version`. CLI: `doctor start stop restart status logs version update service migrate-legacy configure`.
- **Update Center** (sidebar Updates, nav dot, Overview notice): check, release notes (plain text), download and verify, stage, **Install now** (next
  segment boundary) or **Install after current book**, cancel, restart service, automatic checks (daily by default; never auto-download or install),
  eight progress phases, history, clear states for offline / incompatible / failed / rolled back / switched off. Admin-only, CSRF/Host/same-origin protected;
  no endpoint accepts a URL or command.
- **Signed releases**: Ed25519-signed per-channel release index (version, channel, platform, arch, URL, size, SHA-256, published, notes URL, min compatible
  version/OS, schema, runtime) with sequence number (anti-rollback), expiry, revoked versions and keys; public key pinned in the app
  (`pinned_keys.json`, **empty until the owner generates a key**, which keeps the updater off). HTTPS only, host allowlist re-checked on every redirect,
  size limits, resumable download, hardened archive extraction (traversal, links, bombs, per-file manifest). GitHub Releases first; `HttpsRepository` implements the
  same contract for a future first-party host.
- **Separate updater process** (one-shot launchd job or `update --install`): drain the queue at a segment/job boundary, consistent database backup, atomic
  switch, `studio migrate` from the new release, restart, health check, automatic rollback (database restored only if the schema moved), repair note as a
  last resort; user data is never deleted.
- **Migration** from the Bash/Homebrew install (`--migrate-from` / `studio migrate-legacy`): backup, copy, library and model cache kept in place, new backend
  tested on a spare port first, only then the old LaunchAgent is replaced (plist saved); the old folder is never modified; network binding off unless `--keep-network`.
- **Boundaries**: `studio/interfaces.py` (PlatformService, InferenceProvider, TTSProvider, ASRProvider, ModelRegistry, ReleaseRepository, UpdateManager,
  StorageManager, QueueScheduler) with launchd, process, MLX/mock, settings and GitHub/HTTPS implementations. Windows/Linux/containers/remote providers are designed for, not built.
- **Tooling and CI**: `tools/build_release.py`, `sign_release.py` (refuses without a key), `verify_index.py`, `release_keys.py`, `lock_requirements.py`,
  `check_lock_wheels.py`, `tools/e2e/updates_ui.py`; GitHub Actions `ci.yml` and `release.yml` (signs only if `RELEASE_SIGNING_KEY` exists, verifies the index
  against the pinned key, otherwise publishes an UNSIGNED prerelease and no index).
- FFmpeg lookup now prefers the app-owned copy; error messages no longer suggest Homebrew. 80 new tests (242 total).

## v0.4.0
- **API keys can be entered in the web UI.** Settings > Narrative > Local LLM has an API key field with Save key / Remove key, so an
  OpenAI-compatible endpoint no longer needs an environment variable on the Mac. The key is write-only: it is stored in
  `config/secrets.json` (mode 0600), never in `settings.json`, the database, the audit or event logs, and no API response contains it;
  the UI only shows whether a key is saved and when. Saving or removing a key is admin-only, takes effect immediately for the worker,
  and is audited without the value. A saved key wins over the old `api_key_env` environment variable, which still works as a fallback.
  Keys are kept in named slots (`studio/secrets_store.py`), so any further OpenAI-compatible endpoint adds a slot, not a new mechanism.
  Endpoints: `PUT /api/secrets/{slot}` `{"value": ...}`, `DELETE /api/secrets/{slot}`; `GET /api/settings` reports `secrets` status.

## v0.3.0
Context-aware multi-character narration. Everything below is opt-in (Settings > Narrative); with it off, books import and
render exactly as in v0.2.2.
- **Two-pass semantic segmentation.** Pass 1 parses each chapter into paragraphs, scenes and dialogue/narration spans with exact
  character offsets, attributes quoted speech from tags, beats, pronouns plus gender, quote continuation and turn-taking (it never
  assumes "the last speaker"; uncertain lines are flagged for review), tracks characters and aliases across chapters, and records
  emotional cues and tension. Pass 2 builds the narration segments (default ceiling 500 characters, never filled for its own sake),
  splitting at speaker changes (when voices differ), scene breaks and meaningful emotional turns, joining tiny fragments, and
  splitting over-long sentences at the least disruptive punctuation. Segments are exact slices of the chapter text.
- **Dual context windows.** The spoken text is only the segment's words. A separate narrative context window (default 1,250
  characters before and after, set independently) feeds analysis only and cannot reach the TTS engine.
- **Narrative Director** with a controlled emotion vocabulary, schema-validated and auditable output (facts are verified against the
  text and dropped if ungrounded; interpretations are kept separate), a configurable OpenAI-compatible local LLM, and a deterministic
  rules engine that is used when no LLM is configured or when it fails. Results are cached by text, context, profile, previous
  delivery, model and prompt version.
- **Cross-segment continuity.** Per-speaker delivery state with bounded intensity steps, decay instead of snapping back to neutral,
  escalation, scene/chapter resets, and no smoothing of changes the manuscript supports. Edits recompute downstream directions
  only until they stabilise (bounded cascade), without new LLM calls.
- **Characters and story notes** per manuscript: aliases, merge, voice assignment, accents and personality with provenance and
  confidence; user-confirmed values always win. Extractive chapter/scene summaries (verbatim sentences) so summaries cannot invent events.
- **Model-aware delivery**: CustomVoice gets an `instruct` style instruction; VoiceDesign gets the delivery appended to the voice
  description; the Base cloning model receives no instruction (the library has no such input) and keeps its reference conditioning.
- **Segment Inspector, character directory, boundary editing, per-segment preview** in the web UI; project-level overrides of every
  narrative setting; a Narrative analysis job in the queue with pause, cancel and restart recovery.
- **Targeted regeneration and audio cache.** Every take records a generation key (text, voice, delivery, model parameters). A voice or
  delivery change resets only the affected segments, and reverting it reuses the earlier take.
- **Whisper verification modes**: every segment (default), a deterministic sample, or off.
- Database migration v2 (a backup is taken first); installer rebuilt. New tests: `tests/test_narrative.py`.

## v0.2.2
- Documentation set for new sessions: `docs/` (overview, architecture, API, frontend, development workflow, deployment and
  operations, testing and baseline, decisions/gotchas/backlog) and a root `CLAUDE.md`.
- The end-to-end verification scripts moved into the repo as `tools/e2e/` (parameterised, with a fixtures generator).
- `.venv/` ignored by git. No application behaviour changed.

## v0.2.1
- The Reference voice lab moved into the Narrator Studio "Add a narrator" card as the **Record a reference voice** tab.
- Leaving that tab now turns the microphone off, and the lab screen shows it as off when you return.
- Versioning changed to MAJOR.MINOR.PATCH; the "prerelease" label was dropped.

## v0.2
- Reference voice lab: record a reference voice in the browser (guided setup, room check, sample scripts,
  quality analysis, Whisper transcript check, audition, create narrator). It now lives in the
  Narrator Studio "Add a narrator" card, as the **Record a reference voice** tab.
- HTTPS (self-signed) on port 8443 with `--lan`, required for microphone access from other computers.
- Folder browser for storage locations (admin), `studio auth remove-user`, password confirmation,
  About page, short-segment WER false-positive fix.

## v0.1
- First prerelease: manuscript import, durable queue, Qwen3-TTS narration with Whisper QA, review workspace,
  chapter-marked M4B export, release signoff, LAN access with authentication, background service.
