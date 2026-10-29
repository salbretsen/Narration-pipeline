# Changelog

Versions are MAJOR.MINOR.PATCH: MAJOR = a major release update (needs human acceptance); MINOR = feature
additions or overhauls inside the current major release; PATCH = minor adjustments (visual tweaks, bug fixes).

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
