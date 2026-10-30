# End-to-end verification scripts

These drive a **running** studio through the real web UI (Playwright + Microsoft Edge) or its API. They are
the scripts used to establish the v0.2.x baseline. Unit/integration tests live in `src/app/tests/`; these
cover what those cannot: a real browser, the real Mac, the real GPU engine and the LAN.

Prerequisites on the dev machine: a Python venv with `playwright httpx numpy cryptography` plus the studio's
own requirements, Microsoft Edge installed (`channel="msedge"`), and `ffprobe` on PATH (lan_e2e checks the M4B).

| Script | Purpose | Typical run |
|---|---|---|
| `make_fixtures.py` | writes `smoke_text.txt` (500-word two-chapter manuscript) and `fake-mic.wav` (speech-like signal) | `python make_fixtures.py ./fx` |
| `lan_e2e.py` | about 55 checks against the Mac over the LAN: sign-in, 401s, upload, queue, live progress, real TTS preview, clone upload and consent, review, MP3/M4B download, settings, folder browser, operator 403, service restart, crash recovery | `python lan_e2e.py PASSWORD ./out ./fx/smoke_text.txt` |
| `lab_local.py` | the Reference voice lab through a fake microphone: mic, meter, room check, record, waveform, upload and analysis, Whisper check, audition, create narrator | `python lab_local.py https://HOST:8443 ./out USER PW ./fx/fake-mic.wav` (for a local server: `python lab_local.py http://127.0.0.1:PORT ./out "" "" ./fx/fake-mic.wav`) |
| `ref_experiment.py` | the "does a clone inherit the reference's emotion?" experiment on the real Base model (needs an admin account; archive the `zz-exp-*` narrators afterwards) | `python ref_experiment.py USER PW out.json` |
| `narrative_real.py` | v0.3 narrative pipeline on the real engine (19 checks, about 6 minutes): semantic import, characters, voices of all three model families, analysis job, synthesis + Whisper, targeted regeneration, audio and Director caches, boundary edit, voice-consistency measure, timings and memory; writes `narrative_real.json`; needs an admin account and cleans up what it created | `python narrative_real.py PASSWORD ./out` |
| `versions_ui.py` | v0.7.0 UI in a real browser against a local MOCK server (no Mac): import-form narrator placeholder and draft survival, narrate with another voice, grouped library, review list scroll while a job renders, typed-name delete of a version and of a whole book | `python versions_ui.py ./out` |
| `delivery_ui.py` | v0.7.0 delivery UI in a real browser against a local MOCK server (no Mac): reference-bank panel on a clone card, the inspector's blind A/B card (pair, two unlabelled clips, vote, reveal, tally), the Narrative page A/B results and the apply-delivery confirmation. Mock tones only; says nothing about audio quality | `python delivery_ui.py ./out` |
| `foundation_ui.py` | v0.8.0 router and live-update foundation in a real browser against a local MOCK server it starts itself (16 checks): the "Loading..." state and dimmed old page on a slow navigation, a rapid double navigation ends on the last page, Overview refetch rate stays low on an idle studio | `python foundation_ui.py ./out` |
| `polish_book.py` | v0.8.0 Book page and Disk & Backups (29 checks, mock server, a worker rendering): typed metadata / release / delivery fields survive live updates, cover upload with an unsaved edit pending, a "playing" chapter player is not re-created, slow-API loading state, Disk placeholders then numbers and Refresh | `python polish_book.py ./out` |
| `polish_overview.py` | v0.8.0 Overview and Library (27 checks): activity feed keeps its scroll and shows "N new events", attention list capped at 5 with Show all, Library narrator select keeps selection and focus, processing-time text for running / queued / finished / idle (through a test-only shim) | `python polish_overview.py ./out` |
| `polish_queue.py` | v0.8.0 Queue (27 checks): one recently-finished row per book with "+N earlier runs", 10-per-page pagers, runtime lines, an open job log keeps its scroll while live updates repaint. Drag-to-reorder is not covered | `python polish_queue.py ./out` |
| `polish_review.py` | v0.8.0 Review & QA (20 checks): typed text and the direction form survive live refreshes, Regenerate with direction (preview explanation, queued, re-rendered, user direction stored), the clone explanation, the loading state when the book endpoint is slow | `python polish_review.py ./out` |
| `polish_settings.py` | v0.8.0 tabbed Settings and page widths (85 checks): every tab by URL, click and keyboard, edits kept across tabs with unsaved dots, one Save, leave warning, PUT body unchanged, dependent fields, content width at 2560 px and no horizontal scroll at 360 / 768 px; screenshots at 1280 / 1920 / 2560 px. Folder browser, Finder picker and API key clicks are not covered | `python polish_settings.py ./out` |
| `polish_voices.py` | v0.8.0 Narrator Studio (24 checks): built-in voice delivery dialog and version 2, Duplicate & retune on a designed voice (lineage, pre-filled preview), Retune from the source on a promoted clone, preview text survives a trip to the Record tab | `python polish_voices.py ./out` |
| `seed.py`, `shots.py` | seed a local mock-engine server with demo books (start it with `STUDIO_MOCK_FAULTS` pointing at a faults JSON) and take the docs screenshots | see the header of each file |

The `foundation_ui.py` and `polish_*.py` scripts need no running studio and no Mac: each starts its own uvicorn server with `STUDIO_ENGINE=mock` in a temporary home, drives it in headless Edge and prints PASS/FAIL per check. They poll from Python because the app's CSP forbids the `eval` that Playwright's `wait_for_function` needs.

Rules:
- Use a **temporary account** for authenticated runs and remove it afterwards (`./studio auth remove-user NAME` on the Mac).
- Archive any books, narrators and recordings a run created.
- Environment overrides are documented at the top of each script (`STUDIO_BASE`, `STUDIO_USER`, `STUDIO_SSH`, ...).
- The real-engine steps take several minutes (model load plus about 40 segments of synthesis).
- Known harness gotchas: print with `PYTHONIOENCODING=utf-8` on Windows; wait on real data (API polls), not on toasts, because earlier toasts satisfy a `.toast-success` wait; a password change by a user signs out any running script session.
