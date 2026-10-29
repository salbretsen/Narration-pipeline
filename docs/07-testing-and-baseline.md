# 7. Testing and establishing a baseline

**Before you change anything, run the baseline below and record the results.** After your change, run it again. A change is not
done until the same checks pass (plus new tests for the new behaviour).

## Automated suite (`src/app/tests`, mock engine, real FFmpeg, FastAPI TestClient)

Run from `src/app`: `pytest -q -p no:cacheprovider tests` → expected **96 passed** (about 30 s). Tests needing ffmpeg are skipped
without it. `conftest.py` provides isolated homes (`AUDIOBOOK_STUDIO_HOME` per test), the mock engine, a worker you drive in-process
(`make_worker(faults).run_until_idle()`), and builders for TXT, DOCX, EPUB and WAV fixtures.

| File | Covers |
|---|---|
| `test_text.py` (19 test functions) | parsers (TXT/MD/DOCX/EPUB incl. spine order, non-linear, footnotes, DRM/corrupt), chunker invariants (random property tests), normalisation, alignment (drops, repeats, hallucination, truncation, soft name substitutions, word times), the short-segment WER rule |
| `test_audio.py` (7) | acoustic QA on synthetic signals, WAV round trip and trim, audio sniffing, Range parsing, path validation, settings validation |
| `test_integration.py` (13) | HTTP flows: upload two books, queue/reorder/duplicate protection, retries and flagging, review actions, assembly/exports/streaming/release gating, blocked exports, settings paths, voice upload and consent, security (CSRF, Host, traversal, tampered rows), crash recovery without endless retry, missing storage pause, failure isolation, narrator change confirmation, UI shell, About/version |
| `test_auth.py` (11) | login/session cookies, forged tokens, roles, password change and confirmation, rate limiting, Host/LAN names, CSRF while signed in, network-bind refusal without users, auth store rules (including `remove_user`), folder browser (admin only, folders only, mkdir rules) |
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

## Pitfalls when testing

- Print with `PYTHONIOENCODING=utf-8` on Windows (✓ characters crash the default console encoding).
- Don't wait on `.toast-success` to decide an action finished (an earlier toast satisfies it); poll the API.
- A user changing their password signs out any running script session (sessions are bound to the password hash).
- Edge's default fake microphone plays a full-scale beep (correctly rated "poor", clipping). Use `--use-file-for-fake-audio-capture` with
  `tools/e2e/make_fixtures.py`'s `fake-mic.wav`.
- Over plain HTTP on the LAN the microphone API does not exist; the recorder must be tested at `https://` or `http://localhost`.
- Do not leave test books/narrators/accounts on the Mac: clean up (doc 6).
