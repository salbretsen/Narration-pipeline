# CLAUDE.md: Audiobook Studio

Local, autonomous audiobook production studio (web UI + queue + Qwen3-TTS/Whisper on Apple Silicon). Developed by Scott Albretsen,
TSAL Solutions LLC. **Start by reading `docs/README.md`** (reading order and a 60-second brief), then `docs/07-testing-and-baseline.md`
and run the baseline *before* changing anything.

## Non-negotiables

1. `install_audiobook_studio_claude.sh` is **generated**. Edit `src/`, then `python tools/build_installer.py`. Never hand-edit the generated file.
2. Mock-engine output is labelled as such. Never claim a GPU/MLX test ran unless it ran on the Mac. Report exactly what was and was not tested.
3. Versioning is MAJOR.MINOR.PATCH in `src/app/studio/__init__.py`. Never bump MAJOR yourself (needs human acceptance). You may bump MINOR
   (features) and PATCH (visual/bug fixes/docs). For every change set: bump, update `CHANGELOG.md` and version-pinned tests, rebuild, test, commit on a
   **version branch** (not `main`), tag `vX.Y.Z`. You have standing permission to commit; do not push unless asked.
4. Deployment target is the owner's M3 Max Mac over SSH (details in `docs/06-deployment-and-operations.md`). **Never touch its firewall, Homebrew or
   unrelated software.** Use temporary accounts for authenticated testing, tell the owner, remove them, and clean up test books/narrators/recordings.
5. Never write real passwords, API keys, TLS keys or the SSH key into the repo, docs, memory or commit messages.
6. Security model is fixed: same-origin SPA, CSP without inline scripts/styles, CSRF header, Host allowlist, sign-in required on the network. Do not weaken it.
7. The microphone only works over HTTPS or localhost (browser rule). The HTTPS listener (`:8443`) exists for that reason.
8. Write large edits as patch scripts via the file tools; inline shell heredocs with quotes have corrupted files before.

## Quick commands (from `src/app`, Windows dev)

```text
pytest -q -p no:cacheprovider tests              # expect 96 passed
node --input-type=module --check < path/to.js     # JS syntax
python ../../tools/build_installer.py             # regenerate the installer, then bash -n and shellcheck -S style
```

Everything else (architecture, API, frontend patterns, deploy/redeploy, troubleshooting, decisions, gotchas, backlog) is in `docs/`.
