# Audiobook Studio: documentation for the next session

**Audiobook Studio** (by Scott Albretsen, TSAL Solutions LLC) is a local, autonomous audiobook production
studio for Apple Silicon Macs: a web UI over a durable queue that narrates manuscripts with Qwen3-TTS
(mlx-audio), independently verifies every segment with Whisper (mlx-whisper), lets a human review and
correct flagged passages, and exports chapter-marked audiobooks. Current version: see
`src/app/studio/__init__.py` (and `src/app/CHANGELOG.md`).

If you are a new session (human or AI), read these in order. Each file is self-contained.

| # | File | Read it to learn |
|---|---|---|
| 1 | [01-project-overview.md](01-project-overview.md) | What the product is, requirements, current baseline, what is and is not verified |
| 2 | [02-architecture.md](02-architecture.md) | Process model, modules, data model, state machines, QA and assembly algorithms, security |
| 3 | [03-api-reference.md](03-api-reference.md) | Every HTTP endpoint, auth/role rules, settings schema, CLI |
| 4 | [04-frontend.md](04-frontend.md) | The SPA: files, patterns, views, the recorder lab, CSP constraints |
| 5 | [05-development-workflow.md](05-development-workflow.md) | Setting up, building the installer, tests, conventions, **versioning and commit policy** |
| 6 | [06-deployment-and-operations.md](06-deployment-and-operations.md) | The M3 Max deployment, redeploying, service, TLS, accounts, logs, troubleshooting |
| 7 | [07-testing-and-baseline.md](07-testing-and-baseline.md) | Test inventory and the checklist that **establishes a baseline before you change anything** |
| 8 | [08-decisions-gotchas-backlog.md](08-decisions-gotchas-backlog.md) | Why things are the way they are, traps already hit, known limits, backlog |

Also: `../CLAUDE.md` (short rules for AI sessions), `../src/app/README.md` (operator guide, also installed on
the Mac), `../src/app/MODEL_LICENSES.md`, `../tools/e2e/README.md` (end-to-end scripts), `screenshots/`.

## The 60-second brief

- **Repo layout:** `src/app/` is the application (Python package `studio/`, tests, launchers);
  `src/installer/installer.sh.in` is the installer template; `tools/build_installer.py` embeds everything into
  the single deliverable `install_audiobook_studio_claude.sh` at the repo root. **That file is generated. Never edit it by hand.**
- **Dev machine:** Windows 11, no MLX. All automated tests use a clearly labelled **mock engine** (synthetic tones, not speech).
  Real narration/ASR only runs on the Mac.
- **Deployment:** an M3 Max Mac on the LAN, reached over SSH. Served at `:8080` (HTTP) and `:8443` (HTTPS, needed for
  browser microphone access). Sign-in required. See doc 6.
- **Rules that always apply:** bump the version (MAJOR.MINOR.PATCH, doc 5) and add a changelog entry for any change;
  rebuild the installer; run the tests; commit on a version branch and tag; never touch the Mac's firewall or Homebrew;
  use temporary accounts for authenticated testing and remove them; never store or print real passwords in files.
- **Before adding features:** run the baseline checklist in doc 7 so you know what "working" looks like.
