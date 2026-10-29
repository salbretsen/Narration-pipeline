# 7. Testing and establishing a baseline

**Before you change anything, run the baseline below and record the results.** After your change, run it again. A change is not
done until the same checks pass (plus new tests for the new behaviour).

## Automated suite (`src/app/tests`, mock engine, real FFmpeg, FastAPI TestClient)

Run from `src/app`: `pytest -q -p no:cacheprovider tests` → expected **162 passed** (about 75 s; it was 96 at v0.2.x). Tests needing ffmpeg are skipped
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

What these cannot show: real speech quality, the GPU engine, a real browser's microphone, the LAN, macOS behaviour, launchd.

### Where new tests go

New parsing/text logic → `test_text.py`; audio/DSP → `test_audio.py`; end-to-end HTTP behaviour → `test_integration.py`; auth/roles/storage
admin → `test_auth.py`; recording lab/TLS → `test_lab.py`. Inject faults with `make_worker({"text:substring": ["drop","repeat","silence","babble","clip","quiet","error"]})`.
Remember fault keys match the **spoken (normalised)** text, not the manuscript text ("3rd" becomes "third").

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
