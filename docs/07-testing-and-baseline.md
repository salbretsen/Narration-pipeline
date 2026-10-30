# 7. Testing and establishing a baseline

**Before you change anything, run the baseline below and record the results.** After your change, run it again. A change is not
done until the same checks pass (plus new tests for the new behaviour).

## Automated suite (`src/app/tests`, mock engine, real FFmpeg, FastAPI TestClient)

Run from `src/app`: `pytest -q -p no:cacheprovider tests` → expected **531 passed** (about 2 min; 451 at v0.7.0, 324 at v0.6.x, 162 at v0.4.0, 96 at v0.2.x). Tests needing ffmpeg are skipped
without it. `conftest.py` provides isolated homes (`AUDIOBOOK_STUDIO_HOME` per test), the mock engine, a worker you drive in-process
(`make_worker(faults).run_until_idle()`), and builders for TXT, DOCX, EPUB and WAV fixtures.

| File | Covers |
|---|---|
| `test_text.py` (19 test functions) | parsers (TXT/MD/DOCX/EPUB incl. spine order, non-linear, footnotes, DRM/corrupt), chunker invariants (random property tests), normalisation, alignment (drops, repeats, hallucination, truncation, soft name substitutions, word times), the short-segment WER rule |
| `test_audio.py` (7) | acoustic QA on synthetic signals, WAV round trip and trim, audio sniffing, Range parsing, path validation, settings validation |
| `test_integration.py` (13) | HTTP flows: upload two books, queue/reorder/duplicate protection, retries and flagging, review actions, assembly/exports/streaming/release gating, blocked exports, settings paths, voice upload and consent, security (CSRF, Host, traversal, tampered rows), crash recovery without endless retry, missing storage pause, failure isolation, narrator change confirmation, UI shell, About/version |
| `test_auth.py` (11) | login/session cookies, forged tokens, roles, password change and confirmation, rate limiting, Host/LAN names, CSRF while signed in, network-bind refusal without users, auth store rules (including `remove_user`), folder browser (admin only, folders only, mkdir rules) |
| `test_narrative.py` (59 functions) | v0.3: speaker attribution on fixture manuscripts (known cases, ambiguity flagged, aliases, threshold), segmentation invariants (exact once, in order, ceiling, scenes/headings, unrelated dialogue, long sentences, emotional splits, manual cut/join), context windows, Director schema/grounding/leak guard/cache/fallback/repair, continuity (grief, intentional change, escalation, resets), per-model delivery, migration v1 to v2, imports, voice assignment and targeted regeneration, audio-cache reuse, character merge and confirmed-fact precedence, boundary edits, Whisper modes, assembly order, project overrides, inspector, segment preview, pause/cancel/restart recovery, model-aware ordering, redirect, an HTTP stub LLM end to end. Fixtures in `narrative_fixtures.py`. The LLM tests use an httpx MockTransport or a local HTTP stub: **no language model runs in the suite**. |
| `test_lab.py` (9) | sample-script lengths/variety, quality verdicts, the recording flow (upload → analysis → Whisper check → audition → create narrator), bad/clipped recordings, noise-floor estimation, certificate generation/reuse, HTTPS settings validation |
| `test_update_core.py` (33) | v0.5: Ed25519 signature/tamper/revoked-key/no-key behaviour, semver and update selection (up to date, available, incompatible, stepping stones, revoked, channels, expired index), HTTPS-only fetcher (host allowlist, redirect re-checks, size limits, offline, resumable Range download), archive hardening (traversal, links, bombs, manifest), release store (atomic pointer, prune), runtime build rules (wheels only, bound to the signed lock), launchd plist and command sequences (fake `launchctl`), real process service, interface contracts, provider/storage managers |
| `test_update_flow.py` (30) | v0.5: manager check/download/stage (replayed index, tampered/mislabelled/malicious archives, untrusted origin, low disk, interrupted download resumes, background download), the updater `Applier` (success, failed migration + DB restore, unhealthy release rollback, double failure repair mode, post-staging tamper, drain waits for the segment/for the running book, single-updater lock), worker drain with the real worker (`now` and `after_job`), the HTTP API (CSRF, roles, no URL/command accepted, preferences), and a **real-process E2E** (two real releases, real server and migrate, good update then a failing one that rolls back) |
| `test_update_install.py` (18) | v0.5: installer policy scan (no brew/sudo/xattr/spctl/curl-pipe/profile edits), pins sane, `--extract-only` and `--check` in real bash, **fail-closed pinned download in real bash**, hash-locked `requirements.lock`, wheel-checker logic, build->sign->check->download->stage pipeline with a throw-away key, `sign_release` refusal without a key/merge, key tool, no secrets in repo, legacy migration (old tree hash-identical, test-before-switch, failure leaves old service), CLI, busy-port refusal |

v0.7.0 added `test_plumbing_v5.py` (schema v5, settings defaults, `LLMClient.derive`, `resolve_models`, thinking mode, Test connection with extra models), `test_delivery_audio.py` (pace from direction, pause hints and digests, reference bank selection/adoption/consent/candidates, `calibrated_wpm`, preview stretch), `test_director_v2.py` (beat sheet validation and cache, scene windows, per-item validation and re-ask, rules fallback, `direct_scope`, no write lock across an LLM request), `test_translator.py` (translation validation and cache keys, `plan_for_segment` stays read-only, `translate_pending`, worker back-off) and `test_ab.py` (comparisons, blind rounds, vote and reveal, tally, `random_differing_segment`, apply-delivery with dry run and confirmation), plus the narrator-version, delete and import tests added earlier in the release. They all use the mock engine and HTTP stub LLM servers.

v0.8.0 added 80 tests in eight files (451 to 531): `test_live_foundation.py` (11: stable SSE digest, schema v6 indexes and `jobs.active_seconds`, active-time accounting with a fake worker clock, `cover_version`, and the node suite `tests/js/ui_helpers.test.mjs` for `patchHTML`, `patchRegions`, `pager`, `onState` de-duplication), `test_book_perf.py` (10: `get_book` query count independent of segment count with synthetic books, batched readiness equal to the old per-segment result, `?view=review` is a subset), `test_disk_cache.py` (10: second `usage` call does not re-walk, `refresh`, invalidation by disk-changing actions, numbers equal the old implementation), `test_runtime.py` (22: rates and fallbacks, queue ordering, paused jobs, finished/legacy timing, the paginated grouped Queue API, and the node suite `tests/js/runtime_fmt.test.mjs`), `test_overview_runtime.py` (1: `runtime` reaches `/api/books` and `/api/overview`), `test_regen_direction.py` (13: a user direction reaches the voice with the Director off and on classic books, the delivery summary per voice kind, the read-only preview, regenerate with `direction`, the Director-on cascade queues its resets), `test_voice_retune.py` (12: duplicate, retune, lineage, description sync, constant query count of `GET /api/voices`) and `test_settings_ui.py` (1: the node suite `tests/js/settings_model.test.mjs`). The three node suites run through pytest wrappers and are **skipped when `node` is not installed**, so a machine without Node reports fewer than 531 passed. `test_background_download_endpoint_flow_and_busy_guard` now waits up to 20 s and reports the real state and error (it failed on a loaded machine). All use the mock engine, synthetic rows and tiny placeholder files.

Also (not in pytest): browser scripts, each starting its own local MOCK-engine server (Edge via Playwright, headless): `python tools/e2e/foundation_ui.py OUT` (16 checks), `polish_book.py` (29), `polish_overview.py` (27), `polish_queue.py` (27), `polish_review.py` (20), `polish_settings.py` (85), `polish_voices.py` (24), plus the earlier `versions_ui.py` (29) and `delivery_ui.py` (13); purposes in `tools/e2e/README.md`. `python tools/e2e/updates_ui.py OUT chromium|webkit` (Update Center in a real browser, offline and phone-width included),
`python tools/check_lock_wheels.py` (network), shellcheck on the generated Mac installer. Real Mac checks: doc 10 (M1-M10).

What these cannot show: real speech quality, the GPU engine, a real browser's microphone, the LAN, macOS behaviour, launchd.

### Where new tests go

New parsing/text logic → `test_text.py`; audio/DSP → `test_audio.py`; end-to-end HTTP behaviour → `test_integration.py`; auth/roles/storage
admin → `test_auth.py`; recording lab/TLS → `test_lab.py`. Inject faults with `make_worker({"text:substring": ["drop","repeat","silence","babble","clip","quiet","error"]})`.
Remember fault keys match the **spoken (normalised)** text, not the manuscript text ("3rd" becomes "third").

## Verification status of v0.8.0

Honest list of what ran where. **Nothing in this release has run on the Mac, with the real engine, or in real Safari.** Everything below ran on Windows with the mock engine (tones), HTTP stub servers and headless Edge.

| Area | Ran | Where / how | Not run |
|---|---|---|---|
| Automated suite (531) | yes | Windows dev, mock engine, real FFmpeg, node for the three JS suites | the GPU engine, real Whisper, a real LLM |
| SSE digest stability, `onState` de-duplication, router loading state and stale-render guard, Overview refetch rate | yes | `test_live_foundation.py`, `ui_helpers.test.mjs`, `foundation_ui.py` (16 checks) | real Safari, a real worker heartbeat on the Mac |
| Book page live regions (typed fields survive, cover upload, mini-player) and Disk & Backups placeholders / Refresh | yes | `polish_book.py` (29 checks) with a worker rendering and a slow stub API | the Book page at 360 px wide (horizontal overflow from a nowrap table in `book.js`, known and unfixed); real playback |
| `get_book` speed (6,523 to 30 queries, about 333 to about 104 ms at 4,000 segments), batched readiness, light review payload | yes | `test_book_perf.py` on synthetic rows and placeholder files | the Mac's real library (19+ books); real file-system timings |
| Disk usage cache | yes | `test_disk_cache.py`, `polish_book.py` | how long a real measurement takes on the Mac |
| Overview feed, attention list, Library narrator select | yes | `polish_overview.py` (27) | real Safari |
| Queue grouping, pagination, log scroll | yes | `test_runtime.py`, `polish_queue.py` (27) | drag-to-reorder in a browser |
| **Processing-time estimates** | formatting, fallbacks, ordering, paused handling | unit tests with synthetic rows and a fake clock; `polish_overview.py` injects a test-only runtime shim to check the rendered text | **never compared with real timings**; accuracy of the rates, the Director term and "starts about" on the M3 Max is unknown |
| Regenerate with direction | the plumbing and the explanation | `test_regen_direction.py`, `polish_review.py` (20) | whether a real model sounds different for an instruction; a bank clip on a real clone |
| Settings tabs, one Save, unsaved dots, leave warning, widths | yes | `settings_model.test.mjs`, `polish_settings.py` (85) | the folder browser and Finder picker; API key *Save* and *Test connection* clicks |
| Narrator Studio edit dialog, duplicate, retune | yes | `test_voice_retune.py`, `polish_voices.py` (24) | audio quality of a retuned voice |
| Schema v6 migration | mock databases | `migrate()` in tests | the Mac's real database (a pre-migration backup is written automatically) |
| Installer, updater, signing | not touched | no code change (generated installers not rebuilt by this documentation change) | M1-M10 (doc 10) for the release itself |

## Verification status of v0.7.0

Honest list of what ran where. Nothing in this release has run on the Mac.

| Area | Ran | Where / how | Not run |
|---|---|---|---|
| Automated suite (451) | yes | Windows dev, mock engine, stub LLM servers, real FFmpeg | the GPU engine, a real LLM, real Whisper |
| Narrator versions, library grouping, admin delete, import form, review list scroll, pronunciation delete | yes | pytest plus `tools/e2e/versions_ui.py` in Edge against a local mock server | real Safari, the Mac, the real library and its 19+ books |
| Director write-lock fix, thinking-model handling (400 fallback, empty/reasoning-only errors, Test connection) | stubs only | HTTP stub servers that answer like the oMLX server did; a test fails if a write lock is held during a request | a real thinking model; the original findings came from the earlier Mac session |
| Chapter beat sheet, scene batches, `direct_scope`, LLM translator | stubs only | scripted stub replies (valid, invalid, partial, truncated, failing) | quality or speed with a real model; chapter-pass time and token budget |
| Reference bank, pace, pause hints | mock engine | mock tones; adoption rules, consent, generation keys, digests, drift metrics are unit-tested | whether any of it sounds better; Whisper WER on real emotional speech; WSOLA at 0.92-1.08 on real speech |
| Blind A/B tool | mock engine | pair creation, randomised mapping, vote, reveal, tally, "same audio" refusal | listening on real output; the 60% / 10% rule has never been applied to real votes |
| Schema v5 migration | mock databases | `migrate()` from v4 in tests | the Mac's real database (a pre-migration backup is written automatically) |
| Voices reference-bank panel, Inspector A/B card, Narrative A/B results and apply-delivery dialog | `node --check` + `tools/e2e/delivery_ui.py` (13 checks, Edge, mock engine; the bank generate/adopt flow and the apply-delivery dialog not exercised) | the mock engine produces tones, not speech | real Safari, real engine |
| Installer, updater | not touched | no code change | M1-M10 (doc 10) for the release itself |

Before relying on any of the new delivery features on a book: run the Mac smoke test, enable one feature on a copy of a book, use *Apply new delivery settings*, and compare with the A/B tool by ear.

## Static checks

```text
node --input-type=module --check < <each changed .js>
python -m compileall -q src/app/studio src/app/tests
bash -n install_audiobook_studio_claude.sh
shellcheck -S style install_audiobook_studio_claude.sh          # expect no output
shellcheck -x -S style src/app/bin/{start-studio,stop-studio,studio,studio-service,run-tests,smoke-test,_env.sh}   # run in src/app/bin
python tools/build_installer.py && bash install_audiobook_studio_claude.sh --extract-only <scratch>   # integrity check passes
```

## Real-hardware checks (on the Mac)

1. **Smoke test:** `cd ~/audiobook-studio-claude && ./smoke-test` (run it with `--keep` to retain its books; archive them after). Expected:
   `PASS`, no defects, 20/20 segments passed, real RTF numbers. Report: `logs/smoke-<timestamp>/smoke_report.{md,json}`. It uses the
   real MLX models (downloads any missing weights), the worker via the service, and a chapter M4B export. A synthetic reference is used
   for cloning unless you pass `--ref-audio` and `--ref-text`.
2. **LAN browser suite:** `tools/e2e/lan_e2e.py` (about 55 checks, 8-10 minutes, real GPU). Needs a temporary admin account.
   See `tools/e2e/README.md`.
3. **Recorder:** `tools/e2e/lab_local.py` against `https://HOST:8443` with a fake microphone (14 checks).
4. **Manual (cannot be automated):** open the UI in a normal browser from another computer, sign in, record with a *real* microphone,
   listen to a review segment and an exported chapter in Apple Books/QuickTime, check chapter markers, record a release signoff.

## Baseline results recorded at v0.2.1

| Check | Result |
|---|---|
| Automated suite | 96 passed (Windows, Python 3.13, ffmpeg 9.0.1, mock engine) |
| shellcheck `-S style`, `bash -n`, node syntax checks | clean |
| Smoke test on M3 Max (2 runs) | PASS both. Run 1 flagged one false positive (5-word chapter heading at 20% WER), now fixed; run 2: 20/20 passed. RTF 0.33-0.64 (preset/design), clone 0.42 warm, peak 8.5-15.1 GB, wall 135 s for 2 books / 281 s audio |
| LAN browser suite | 54/55 on the first full pass (the one failure was a harmless console note about a security header on plain HTTP, since removed); the crash-recovery, folder-browser, operator-403 and persistence checks all passed |
| Recorder over HTTPS on the LAN, real engine | 13/13 (14 with the tab-position check) |
| Plain-HTTP LAN | `isSecureContext=false`, no `navigator.mediaDevices`; the lab shows the HTTPS notice (verified) |
| Reference-emotion experiment (real Base model, synthetic references) | pitch variation of the clone tracked the reference: neutral 1.97, tense 2.71, joyful 4.27, sad 1.43 (references 2.12, 2.69, 4.37, 1.05); a mixed neutral+joyful clip gave an in-between result (2.59). Data in `docs/reference_emotion_experiment.json`. One run, one seed, one voice |

`docs/lan_e2e_results.json` holds the machine-readable output of the first full LAN pass (it ends at the point the old harness crashed on a
console encoding error; later full passes are summarised above).

## Baseline results recorded at v0.3.0 (real M3 Max, 2026-10-29)

`tools/e2e/narrative_real.py`: 19 of 19 checks passed on the second full run (the first run found a real bug, re-directing a segment did nothing, now fixed and covered by a unit test).
Real Qwen3-TTS (CustomVoice, VoiceDesign, Base) and Whisper. The Narrative Director ran its **rules** engine: no LLM is installed on the Mac, so LLM output quality is unmeasured.
Manuscript: 3 chapters, 511 words, 30 segments at import (46 once three characters had their own voices), 4 voices (Ryan narrator, Aiden preset, a designed voice, a clone made from real VoiceDesign speech).

| Measure | Result |
|---|---|
| Text fidelity | all 511 source words appear once, in order, in the segments; longest segment 370 characters (ceiling 500) |
| Pass 1 + Pass 2 (server side) | 0.025 s; the whole import request took 0.07 s |
| Analysis job (summaries, 46 directions, rules Director) | 2.2 s; the Director itself 0.01 s |
| TTS + Whisper QA, 46 segments | 106.9 s wall for 213 s of audio (overall RTF 0.50, median per-attempt RTF 0.35); 49 attempts, 3 retries; mean WER 1.3 %, max 33 % on a short segment |
| Memory | peak GPU memory 16.7 GB (MLX); worker RSS peak 10.5 GB (sampled every 12 s, so a lower bound) |
| QA outcome | 45 passed, 1 flagged: a 1.6 s line the model rendered in 0.7 s on all 4 attempts ("Suspiciously short"); very short segments are unstable on the real model and the flag worked as designed |
| Delivery plumbing | the Base clone voice received no instruction in any of its 4 segments; CustomVoice segments with non-neutral direction received `instruct`; the text sent to the engine equalled the segment text for all 46 |
| Targeted regeneration | giving one character another voice reset 9 of 46 segments (37 kept their take); 15.1 s to re-render |
| Audio cache | switching the voice back reused all 9 earlier takes: 3.1 s instead of 15.1 s |
| Director cache | re-directing 6 segments gave 6 cache hits and no new analysis (the second book's 46 directions were also all cache hits because the first run had cached identical text and context) |
| Boundary edit | splitting one segment created 2, retired 1, left all others untouched; 9-12 s to re-render |
| Smoke test (legacy path, same build) | PASS: 20/20 segments, M4B exported and validated |
| Voice consistency (acoustic; f0 coefficient of variation across independent segments) | Ryan 0.14 (22 segments, includes dialogue vs narration prosody), Aiden 0.07 (2), designed voice 0.13 (4), clone 0.11 (2). Three of the four samples are tiny and pitch is a crude proxy: it shows drift is measurable, not that any voice is consistent |

Not measured: LLM attribution/direction quality; audio quality or emotional accuracy by listening (nobody listened); books longer than about 500 words; `max_warm_models=1`; the
real-microphone/LAN browser suites (not re-run for v0.3.0; the UI was driven locally against the mock engine in Edge/Playwright).

## Pitfalls when testing

- Print with `PYTHONIOENCODING=utf-8` on Windows (✓ characters crash the default console encoding).
- Don't wait on `.toast-success` to decide an action finished (an earlier toast satisfies it); poll the API.
- A user changing their password signs out any running script session (sessions are bound to the password hash).
- Edge's default fake microphone plays a full-scale beep (correctly rated "poor", clipping). Use `--use-file-for-fake-audio-capture` with
  `tools/e2e/make_fixtures.py`'s `fake-mic.wav`.
- Over plain HTTP on the LAN the microphone API does not exist; the recorder must be tested at `https://` or `http://localhost`.
- Do not leave test books/narrators/accounts on the Mac: clean up (doc 6).
