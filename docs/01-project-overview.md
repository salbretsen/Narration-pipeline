# 1. Project overview and baseline

## Purpose

The operator publishes books and wants to turn 50,000-word-plus manuscripts into finished audiobooks without
anyone narrating in person. They upload several manuscripts from a browser, pick an approved narrator for each,
queue them, let the Mac process them autonomously, listen and correct in the same interface, export files, and
record a human release approval. **Zero manual intervention during generation; targeted human review before release.**

## Original requirements (condensed; these still bind)

- **Fully local** inference, transcription and storage. No hosted TTS/ASR/LLM, no telemetry, no manuscript or voice uploads
  to third parties after the initial dependency and weight downloads. Hugging Face telemetry is disabled.
- **Target:** MacBook Pro M3 Max, 48 GB, native arm64, Python 3.12, a venv, Homebrew FFmpeg, mlx-audio, mlx-whisper, SQLite.
  A second deployment (M4 Max, 36 GB) must work through settings, not hardcoding.
- **One GPU worker** by default (no MLX contention); many queued books, not simultaneous GPU saturation.
- **Installer:** one self-contained Bash script (`install_audiobook_studio_claude.sh`), default location
  `~/audiobook-studio-claude` (override `AUDIOBOOK_STUDIO_HOME`), safe to re-run: never wipe manuscripts, audio, DB,
  voices, settings, queue state or exports; back up replaced code; migrate the schema.
- **A full web UI is mandatory** (every button performs a real backend operation): dashboard, library/queue,
  narrator studio, review workspace, playback/export, settings/storage.
- **Qwen3-TTS rules:** presets use `generate_custom_voice` (English speakers Ryan and Aiden); cloning uses the Base model with
  `ref_audio`/`ref_text` and **never a speaker argument**; designed voices use VoiceDesign.
- **QA:** independent local Whisper on every take, robust normalisation/alignment, acoustic checks, capped retries
  (default 3 attempts), keep the best take, flag for a human, never retry flagged segments on restart.
- **Output:** chapter WAV masters, MP3, chapter-marked AAC M4B built with FFmpeg and validated with ffprobe. Missing audio
  must **block** an export, never be silently omitted. Drafts are labelled DRAFT until a human records release approval.
  The system never implies anything was uploaded to Audible/ACX/podcast hosts.
- **Honesty:** never present mock output as MLX inference; report exactly which tests ran and which need the Mac.

## Requirements added after the original brief

| Requirement | Where |
|---|---|
| LAN access with authentication (bind `0.0.0.0`, port 8080, sign-in required, admin/operator roles) | doc 2 "Security", doc 6 |
| Background service that survives SSH and crashes (macOS LaunchAgent) | doc 6 |
| Library root must be changeable over the network with a way to browse the hosting Mac's folders | `GET /api/storage/browse`, Settings "Browse…" |
| Changing a password requires typing it twice | web dialog, API `confirm`, CLI |
| About page, with the version, developer and company credit | `studio/__init__.py`, `views/about.js` |
| **Reference voice lab:** record a reference voice in the browser with full guidance and sample scripts | doc 4, `views/voicelab.js`, `studio/reference.py` |
| HTTPS on `:8443` with a self-signed certificate (browsers need a secure context for the microphone) | `studio/tls.py` |
| Versioning MAJOR.MINOR.PATCH; permission to commit and bump versions | doc 5 |

## What exists (feature inventory)

Manuscript import (EPUB spine order, DOCX, TXT, MD) with preview and manifest of read/not-read text; per-book settings
snapshot and voice fingerprint; durable FIFO queue with reorder, pause, resume, retry, cancel; one serial GPU worker in a
separate supervised process with crash recovery; per-segment synth, Whisper check, acoustic QA, scoring, retries; review
workspace (side-by-side text, ASR diff, takes, approve/regenerate/override/select/note); chapter assembly with two-pass
loudness normalisation; WAV/MP3/M4B/ZIP exports; QA report (HTML+JSON); release signoff; voice studio (presets, designed,
cloned by upload, cloned by in-browser recording); storage settings with server-side folder browser and verified project
moves; authentication, TLS, service management; CLI; automated tests; end-to-end scripts.

## Current baseline (verified at v0.2.1; v0.2.2 changed only docs and tooling)

Verified, with evidence in doc 7:

- 96 automated tests passed at v0.2.x; v0.3.0 has 158 (see doc 7). All use the **mock** engine and real FFmpeg.
- On the M3 Max with the **real** engine: the smoke test passes (CustomVoice, Base cloning, VoiceDesign, queue of two
  manuscripts, chapter M4B) with 20/20 segments passing QA. Measured real-time factor 0.33 to 0.64 for presets/designed,
  about 0.4 for warm cloning; peak memory 8 to 15 GB.
- The browser end-to-end suite over the LAN (about 55 checks) passes, including crash recovery mid-generation, library-root
  change via the folder browser, operator-role restrictions, and the recorder flow over HTTPS.
- The shell installer passes `bash -n` and `shellcheck -S style`; re-running it on the Mac preserves data and accounts.

**Not verified (do not claim otherwise):**

- Recording with a **real human voice** through the microphone (the recorder was tested with Edge's fake microphone only).
- Safari and Firefox (only Edge was used).
- Subjective voice quality over a whole book, and long-book (50,000-word) throughput. Only about 500-word manuscripts were run on the GPU.
- The Whisper false-positive rate across real books (one false positive was found and fixed: very short segments).
- Boot-time hosting before login (a LaunchDaemon needs an administrator password; not set up).
- Distributor (ACX/Audible) compliance of the exported files.

## Identity and credit

Developed by Scott Albretsen, TSAL Solutions LLC. Copyright line: "© 2026 TSAL Solutions LLC" (no licence terms have been
chosen; that is the owner's decision).
