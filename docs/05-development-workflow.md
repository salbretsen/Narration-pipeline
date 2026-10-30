# 5. Development workflow

## One-time setup on the Windows dev machine

Python 3.12 is the target; 3.13 works for tests (avoid 3.13-only features). MLX does not exist on Windows, so the studio runs with the
mock engine here.

```powershell
cd "D:\Code\Mac Narration pipeline"
python -m venv .venv                      # .venv/ is gitignored
.venv\Scripts\python -m pip install fastapi uvicorn python-multipart pydantic numpy pytest httpx cryptography
.venv\Scripts\python -m pip install playwright shellcheck-py     # for tools/e2e and shell linting
# needs on PATH: ffmpeg + ffprobe (tests that touch audio are skipped without them), node (JS syntax checks), bash (Git Bash)
```

Run the tests from `src/app`: `..\..\.venv\Scripts\python -m pytest -q -p no:cacheprovider tests` (about 2 min; the count is in doc 7).
Run a local mock studio: from `src/app`, with `AUDIOBOOK_STUDIO_HOME` pointing at a scratch folder,
`STUDIO_ENGINE=mock python -m studio serve --port 7891` (a worker subprocess is started automatically unless `STUDIO_NO_WORKER=1`).
Kill stray servers on Windows with `Get-NetTCPConnection -LocalPort 7891 -State Listen | % { Stop-Process -Id $_.OwningProcess -Force }`.

## Where to edit

- Application code: `src/app/studio/**`, SPA in `src/app/studio/web/static/**`, launchers in `src/app/bin/*`, tests in `src/app/tests/*`.
- Installer logic: `src/installer/installer_mac.sh.in` (the supported no-prerequisites installer, macOS `/bin/bash` 3.2; extra placeholder `@@RUNTIME_PINS@@`,
  rendered from `tools/runtime-manifest.json`) and `src/installer/installer.sh.in` (legacy Homebrew-era installer, kept for existing deployments).
- Release tooling: `tools/build_release.py` (update archive), `tools/sign_release.py` (signed index, refuses without a key), `tools/verify_index.py`,
  `tools/release_keys.py` (key generation, owner's machine only), `tools/lock_requirements.py` (regenerate `requirements.lock` with uv),
  `tools/check_lock_wheels.py` (every pin has a prebuilt macOS arm64 wheel), `tools/scrub_check.py` (public-repo scrub: fails on private IPs, `.lan`/`.local`
  host names, `/Users/<name>` or `C:\Users\<name>` paths and PEM private keys in tracked files; allowlists with reasons live in the script; also a CI step). CI: `.github/workflows/ci.yml`, `release.yml`.
- **Never edit `install_audiobook_studio_mac.sh` or `install_audiobook_studio_claude.sh`.** Regenerate both: `python tools/build_installer.py`
  (`--only mac|legacy`); then `bash -n` and `shellcheck -S style` on the mac one.
- **Backslash escapes in heredocs sent through the Bash tool get mangled** (seen again in v0.5: a Python newline escape and a hex-zero escape inside
  source text became a real newline and a real NUL byte, which makes a `.py` file unimportable). Write or edit files with the Write/Edit tools, not
  `python - <<EOF` snippets that contain backslashes, and check a changed module with `python -c "import studio.<module>"`.
- Changing `requirements*.txt`: run `python tools/lock_requirements.py` and `python tools/check_lock_wheels.py`; the lock's digest is the release's `runtime_id`. The builder embeds every file
  under `src/app` as a quoted heredoc with a content-hash delimiter, appends `BUILD` and a `MANIFEST.sha256`, and the installer
  verifies the manifest after unpacking. It rejects CR characters and unsafe file names.
- Line endings must be **LF** (`.gitattributes`: `* text=auto eol=lf`). CRLF inside heredocs breaks the Bash installer on macOS.
- New files under `src/app` are embedded automatically. New executables must be added to `EXECUTABLE` in `build_installer.py`
  and to the launcher copy loop in `installer.sh.in` (`install_launchers`).
- Schema changes: append a migration to `MIGRATIONS` in `db.py` (never edit an old one); migrations run in `studio migrate`, which
  the installer calls after the code swap and which takes a DB backup first. Prefer reusing existing tables/payloads when sensible.
- Dependencies: `requirements.txt` (any platform) and `requirements-mlx.txt` (Apple Silicon; `mlx-audio==0.6.3` pinned, `mlx-whisper`).
  The installer writes the resolved set to `app/VERSIONS.lock`.

## Conventions

- **Honesty about the mock:** mock output is labelled `engine="mock"` everywhere (UI badge, preview notes, attempts). Never describe a
  mock result as model inference or claim a GPU test ran when it did not.
- Business logic in `services.py`; endpoints thin; raise `ServiceError` with a message a user can act on.
- Every state change in one SQLite transaction (`db.tx()`); audit trail via `ctx.audit`, user-visible events via `ctx.event`,
  per-job log via `ctx.job_log`.
- Write files atomically (`util.atomic_write_*`, `audio.write_wav`); never overwrite originals; segments' original text is immutable.
- Validate at the edge: Pydantic models with `extra="forbid"`, server-side path validation, extension+content sniffing, size limits.
- Match the surrounding code's style and comment density. Comments explain *why* (constraints, traps), not *what*.
- Keep every UI control wired to a real operation; no placeholders.

## Versioning (policy from the owner)

`X.Y.Z` (Major.Feature.Minor/Patch), defined once in `src/app/studio/__init__.py` (`__version__`; `VERSION_LABEL` is `v` + version; there is no
"prerelease" label any more).

| Part | Meaning | Who decides |
|---|---|---|
| X = Major | a major release update | **owner-triggered only. Never bump it yourself** |
| Y = Feature | any time a new feature is added | you may increment it |
| Z = Minor/Patch | any bug fix, feature alteration, documentation update or housekeeping | you may increment it |

Reset lower parts when a higher part increments. The version appears in the About page, the sidebar footer, `/api/auth/status`,
`/api/about`, the installer banner, the README header and `CHANGELOG.md`.

## Release checklist (do this for every change set; the owner has given standing permission to commit and bump)

1. Make the change. Add or update tests (see doc 7 for where things go).
2. Bump `__version__` and update the test assertions that pin the version (`tests/test_integration.py::test_about_page_data_and_version`).
3. Add a `CHANGELOG.md` entry (it becomes the GitHub release notes and the notes shown in the Update Center; the release is refused without one;
   a release's notes also include every changelog section since the previous *published* release, so an untagged version is never lost) (and update the README header line `**vX.Y.Z**`).
4. Run `node --input-type=module --check < file.js` on every changed JS file.
5. Run the full tests. Rebuild: `python tools/build_installer.py`; then `bash -n install_audiobook_studio_claude.sh` and
   `shellcheck -S style install_audiobook_studio_claude.sh` (also the `src/app/bin/*` launchers with `-x -S style`).
6. Deploy to the Mac and verify (doc 6, doc 7). Remove any temporary accounts/test data you created there.
7. Commit on a **version branch** (e.g. `v0.5.5`) or on `dev`, never directly on `main` (`main` only ever receives fast-forwards of
   verified commits, see "Branches and channels" below). Message: imperative summary line, a short body, then
   `Co-Authored-By: Claude <model> <noreply@anthropic.com>` using the attribution given in the session. Tag `vX.Y.Z`. Tell the owner how to
   promote (`git checkout main && git merge --ff-only vX.Y.Z`, only after the doc 10 checklist passed). Do not push unless asked. Never commit secrets (scan with
   `grep -rIl "BEGIN .*PRIVATE KEY" .`), passwords, TLS keys, or `.venv/`.

## Branches and channels (adopted in v0.5.5)

| | Branch | Tags | Channel | 
|---|---|---|---|
| Stable | `main` | `vX.Y.Z` | `stable` |
| Beta | `dev` | `vX.Y.Z-beta.N` | `beta` (beta users also see stable) |

Day-to-day work happens on `dev` (or topic/version branches merged into it). Promotion to stable is a fast-forward of `main` to the verified
commit, then the plain tag. `main` therefore always equals what stable users run. The Release workflow refuses a plain `vX.Y.Z` tag whose
commit is not already on `origin/main` (beta tags: `origin/dev` or `origin/main`) and refuses to re-create an existing release.
No GitHub branch or tag protection is used; the workflow guard is the only enforcement. This is a deliberately simple tool.

## Working-style notes for AI sessions

- **Large or tricky edits:** write a small patch script to a file (Write tool) and run it, or use the Edit tool. Inline Bash heredocs
  that contain quotes, backslashes or backticks have repeatedly been mangled by the shell tool and corrupted files.
- Do not poll or `sleep` to wait for background work; run long tasks in the background and wait for the notification.
- Plan Mode, when active, forbids edits; present the plan and exit via the proper tool.
- Prefer verifying in a real browser (`tools/e2e`) for UI work and on the real Mac for anything engine-related.
- Report exactly what ran and what did not; never claim success from an HTTP 200 alone.
