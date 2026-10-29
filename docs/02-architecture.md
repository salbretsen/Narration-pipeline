# 2. Architecture

## Process model

```
 browser (SPA, same origin) ──HTTP :8080 / HTTPS :8443──▶ uvicorn  `python -m studio serve`
                                                         │  FastAPI app (studio/web/app.py)
                                                         │  - API, SSE, range-capable media, static SPA
                                                         │  - WorkerSupervisor thread (spawns/restarts the worker)
                                                         ▼
                                              SQLite (WAL)  <home>/data/studio.db   ◀──▶  worker process
                                                                                         `python -m studio worker`
                                                                                         - owns the GPU (one at a time)
                                                                                         - MLX Qwen3-TTS + mlx-whisper
                                                                                         - FFmpeg assembly/export
```

- **Inference never runs in a web request.** The HTTP server only reads/writes SQLite and enqueues jobs. The worker is a
  separate OS process started by the server's supervisor (`WorkerSupervisor`: spawn, restart with backoff 2s doubling to 60s,
  resets backoff after 120 s healthy, skips spawning if another live worker holds the lease).
- **HTTPS** is a second `uvicorn.Server` in a daemon thread on `server.https_port` (lifespan "off" so the supervisor is not
  started twice), sharing the same ASGI app. It is only started when `server.https_enabled` is true; a TLS failure logs a warning
  and the studio carries on with HTTP.
- **Service:** on the Mac the server is a LaunchAgent (`com.audiobookstudio.claude`) with KeepAlive; see doc 6.
- **The SSE stream** (`GET /api/events`) polls a cheap `state_digest` once a second and pushes a `state` event when it changes
  (plus keep-alive comments). The browser then refetches the data it shows. There are no WebSockets.

## Repository and runtime layout

```
repo/
  src/app/                         application (what gets installed as <home>/app)
    studio/                        Python package (see module map)
      web/                         FastAPI app, auth, security, media, static SPA
    tests/                         pytest (mock engine)
    bin/                           launchers: start-studio stop-studio studio studio-service run-tests smoke-test _env.sh
    requirements.txt  requirements-mlx.txt  README.md  MODEL_LICENSES.md  CHANGELOG.md
  src/installer/installer.sh.in    installer template (placeholders @@VERSION@@ @@BUILD_ID@@ @@EMBEDDED_FILES@@)
  tools/build_installer.py         generates ../install_audiobook_studio_claude.sh
  tools/e2e/                       browser/LAN/real-engine verification scripts
  docs/                            this documentation + screenshots/
  install_audiobook_studio_claude.sh   GENERATED deliverable (committed)

<home> (default ~/audiobook-studio-claude on the Mac)
  app/ venv/ config/{settings.json,auth.json,tls/} data/{studio.db,previews/,imports/,recordings/}
  library/{manuscripts,segments,alternates,masters,exports,reports,references}/  (default storage root, configurable)
  cache/huggingface/ (HF_HOME)  logs/  run/{server.pid,worker.pid}  backups/{app-<ts>/,db/}
  start-studio stop-studio studio studio-service run-tests smoke-test _env.sh README.md MODEL_LICENSES.md
```

The database always lives on the internal disk (`<home>/data`) so queue state survives an external drive being unplugged.
Per-book data lives under the book's own recorded directories (the **per-project path registry**, `books.storage_json`).

## Module map (`src/app/studio/`)

| Module | Responsibility |
|---|---|
| `__init__.py` | Version (single source of truth), app name, developer, company, copyright |
| `config.py` | Pydantic `Settings` (storage, chunking, qa, pauses, models, export, server, worker), `book_snapshot`, `synth_fingerprint`, atomic load/save, loopback check |
| `pronunciation.py` | Pronunciation dictionary (apply/CRUD/preview) and per-voice pace; reconciles stale segments through `narrative.store.invalidate_changed` |
| `context.py` | `Context`: home dirs, settings cache (by mtime), `Database`, `event()/audit()/job_log()` helpers, logging setup |
| `db.py` | SQLite connection (WAL, FK, busy timeout), `tx()` (BEGIN IMMEDIATE), ordered migrations tracked by `PRAGMA user_version` with a pre-migration backup. Schema v1 (see below) |
| `parsing.py` | `.txt/.md/.docx/.epub` parsers. Chapters of blocks, each block `read` or skipped with a reason (manifest). EPUB: container→OPF→**spine order**, nav/non-linear excluded, notes/images/tables skipped, DRM rejected |
| `chunker.py` | 150-400 char segments at sentence/clause boundaries, never mid-word; merges short paragraphs; pause kinds |
| `normalize.py` | `to_speech` (pronounceable rendition: numbers, years, currency, ordinals, abbreviations) and `compare_tokens` (ASR comparison: contractions, spelled numbers to digits, spelling variants) |
| `align.py` | Word-level Levenshtein; WER with soft substitutions; classified issues (dropped, truncated, repetition, hallucination, garbled, substitution) with times from Whisper word stamps |
| `audio.py` | WAV I/O (atomic), acoustic QA `analyze` (clipping, silence, dead air, level, duration ratio, noise floor), ffmpeg/ffprobe wrappers, reference conversion (24 kHz mono, 3-30 s) |
| `engines.py` | `MlxQwenEngine` (warm LRU of models, per-attempt `mx.random.seed`, RTF/peak memory), `MlxWhisperASR`, `MockEngine` (deterministic tones with fault injection), `MockASR` (reads a `<wav>.json` sidecar transcript) |
| `pipeline.py` | `process_segment` attempt lifecycle, crash recovery `recover_interrupted`, take placement, scoring |
| `worker.py` | `Worker`: lease/heartbeat, scheduling `step()`, job kinds, storage pause/auto-resume, `caffeinate`, `refresh_book_state` |
| `assembly.py` | Chapter masters (pauses, room tone, two-pass loudnorm, WAV/MP3), `export_book` (M4B with chapter metadata, MP3 ZIP), blocking rules |
| `reports.py` | QA report (JSON + escaped HTML), QA limitations text |
| `storage.py` | Path validation, disk usage, mount detection, `browse`/`make_folder` (folder browser), native Finder chooser, verified `move_book` |
| `services.py` | **All business logic**, used by the web API, CLI and smoke test: voices, imports, books, queue, segments/review, release, overview/digest, settings, the recording lab |
| `reference.py` | Sample scripts for the lab, `analyze_recording`, `quality_report` |
| `tls.py` | Self-signed certificate generation/reuse for the LAN names and IPs |
| `doctor.py` | Environment checks, model licence table |
| `smoke.py` | Real-hardware smoke test (refuses to run with the mock engine) |
| `cli.py` | `serve worker migrate auth doctor test import list run resume issues approve report export smoke` |
| `web/app.py` | FastAPI app, request models, endpoints, `WorkerSupervisor` |
| `web/auth.py` | PBKDF2 users, signed session tokens, roles, `ADMIN_RULES`, login limiter |
| `web/security.py` | Host allowlist, CSRF checks, auth enforcement, security headers |
| `web/media.py` | RFC 7233 range responder, MIME map, path containment `ensure_inside` |
| `web/narrative_api.py` | endpoints for the narrative layer (characters, inspector, boundary edits, analysis, project overrides, metadata) |
| `narrative/` | **v0.3**, see doc 9: `structure` (Pass 1), `segmenter` (Pass 2), `windows`, `llm`, `director`, `continuity`, `delivery`, `cast`, `store` (persistence, reconciliation, plan/gen key, inspector), `analysis` (stages + `analyze` job), `voicecheck`, `vocab` |

**Rule:** business logic belongs in `services.py` (or the domain modules it calls), raising `ServiceError(message, status, details)`.
Endpoints stay thin. The CLI and smoke test call the same functions.

## Data model (SQLite schema v1 + v2 + v3, `db.py`)

`voices`, `voice_versions` (immutable versions: model_id, speaker, language, instruct, reference paths/sha, consent JSON);
`books` (metadata, `settings_json` snapshot, `synth_fingerprint`, `storage_json`, derived `state`); `manuscripts` (original file
path + SHA-256 + parse manifest); `chapters`; `segments` (`sid` stable id `bk_xxxx-cNNN-sNNNN`, original text, narration text,
`override_text`, status, `round`, `attempts_used`, `selected_attempt_id`, flag reason, note); `attempts` (take path, engine,
model, seed, temperature, spoken text, ASR text/JSON, WER, issues, acoustics, score, passed, RTF, peak memory, fingerprint,
versions); `jobs`; `job_logs`; `events`; `review_actions`; `audit_log`; `worker_state` (single row lease); `chapter_renders`;
`exports`; `releases`; `imports` (uploaded manuscripts awaiting confirmation).

**Schema v3 (C2)** adds `pronunciations` (`book_id` NULL = global; `written`, `spoken`, `case_sensitive`, `whole_word`, `enabled`, `note`) and `voices.pace` (REAL, default 1.0).
Pronunciation entries are applied in `narrative.store.plan_for_segment` (the text handed to the TTS is the dictionary-rewritten `override_text`/`narration_text`/`original_text`; the stored
texts are never modified; per-book entries override global ones; one regex pass, longest written form first, no chained rewrites). The rewritten text and the voice `pace` (only when not 1.0, so
existing keys are unchanged) are part of the `gen_key`, so an edit goes through `invalidate_changed` and resets exactly the segments whose key changed (`pronunciation._commit`, rolled back
and reported as `409` until confirmed). Pace: the Qwen3-TTS API has no pace/speed control, so `pipeline.process_segment` time-stretches the finished take with `audio.time_stretch`
(NumPy WSOLA, pitch-preserving, bounded 0.8-1.25) before QA, and scales the expected-duration check by the pace. Known limits: ASR is compared with the respelled text, so a
respelling Whisper writes differently (for example "Her-my-oh-nee" heard as "Hermione") can raise a WER flag, prefer plain-word respellings; the time stretch is not audited on real speech;
previews ignore pace.

**Schema v2 (v0.3.0)** adds `chapter_text`, `characters`, `nar_spans`, `story_notes`, `directions`, `direction_log`, `director_cache`, `seg_edits`, `segment_history`,
`analysis_stages`, and columns on `books` (`narrative_json`), `segments` (offsets, speaker, scene, review flags) and `attempts` (`gen_key`, `instruct_used`, ...). Jobs gained the kind
`analyze` (the table was rebuilt; `migrate()` runs with foreign keys off and verifies them before commit). Details: doc 9.

Key constraints: a **partial unique index allows only one active (`queued/running/paused`) book job per book**; segments'
`status` is checked (`pending, synthesizing, passed, flagged, approved`); jobs' `kind` is checked
(`book, preview, assemble, export, analyze`) and `status` (`queued, running, paused, completed, failed, cancelled`).
**Adding a column or table requires a new migration appended to `MIGRATIONS` in `db.py` (never edit a released one).**
Recording-lab jobs deliberately reuse `kind='preview'` with `payload_json.recording_id` to avoid a migration.

## State machines

- **Segment:** `pending → synthesizing → passed | flagged`, then `approved` (human). Regenerate/override/narrator change bump
  `round`, reset `attempts_used`, return to `pending`. Flagged segments are never retried automatically.
- **Attempt:** `in_progress → done | aborted | error`. `attempts_used` is committed **before** inference, so an attempt lost to a
  crash/sleep still counts against the budget (no endless retry loops).
- **Job (book):** `queued → running → completed | failed | paused | cancelled`. `paused_reason` is `operator` or `storage: ...`
  (storage pauses auto-resume when the path is usable again). Pause/cancel are honoured **between segments**.
- **Book `state` (derived by `refresh_book_state`):** `imported, queued, generating, paused, failed, incomplete, needs_review,
  qa_passed, approved`. UI labels: "Human review required" = flagged segments remain; "Approved" = a release record exists.
- **Worker lease:** `worker_state.owner/pid/heartbeat_at`; stale after 60 s; a same-host dead PID allows immediate takeover.

## Scheduling (`Worker.step`)

Order: queued **preview** jobs (includes lab check/audition) → running/queued **assemble/export** jobs → the running book job →
the next queued book by `position`. A book job advances **one segment per step**, so previews and utility jobs interleave
between segments. A book whose storage is missing is paused (not failed). `max_consecutive_errors` synthesis failures fail
only that book. `caffeinate -i -w <pid>` holds off idle sleep while work is queued (macOS, setting `worker.prevent_sleep`).

## The QA pipeline (`pipeline.process_segment`)

1. Build the spoken text: `override_text` else `narration_text` (a pronounceable rendition of the immutable `original_text`), then the pronunciation dictionary (`plan.text`). A voice `pace` other than 1.0 time-stretches the take after synthesis.
2. Commit a new `attempts` row and `attempts_used += 1`; derive a deterministic seed from `sid:round:attempt` and, for retries,
   temperature from `models.retry_temperatures`.
3. Synthesize (preset → `generate_custom_voice`; design → `generate_voice_design`; clone → `generate(ref_audio, ref_text)` and
   **never** a speaker/voice argument). Output is concatenated, converted `mx.array → float32 numpy`, written as 16-bit WAV
   atomically with a provenance sidecar `*.wav.json`.
4. Acoustic analysis (`audio.analyze`), then Whisper (`mlx_whisper.transcribe`, word timestamps, English, temperature 0,
   no condition-on-previous-text). If ASR is unavailable the attempt is **given back** and the job fails: QA is never skipped silently.
5. `align.evaluate` against the spoken text: WER, issues. A WER flag requires `wer > qa.wer_threshold` **and** (reference tokens ≥ 10
   **or** ≥ 2 word errors). Near-identical spellings (similarity ≥ 0.8) are weighted 0.3 as likely name/spelling variants.
6. Score = WER×100 + 25 per error issue + 2 per warning. Pass = no error-severity issue. Otherwise retry until
   `qa.max_attempts`; then keep the lowest-score take, mark the segment `flagged`, move on.

Acoustic checks: empty/NaN, silence, clipping ratio, speech level out of range, dead air, suspicious duration vs words/pace
(pace calibrated from the book's own recent passed takes), trailing silence.

## Assembly and export (`assembly.py`)

- A chapter can be assembled only if **every** segment has a selected, existing take whose fingerprint equals the book's.
  Otherwise `AssemblyBlocked` lists the problems; nothing is skipped.
- Chapter master: head room tone, each take (silence-trimmed), pauses by `pause_kind` (sentence/paragraph/scene/heading/chapter
  end), room tone at `room_tone_dbfs`; two-pass `loudnorm` (single-pass fallback), resample to 44.1 kHz; 16-bit WAV master;
  MP3 CBR. Both are validated with ffprobe (codec, rate, duration within tolerance).
- M4B: concat of chapter WAV masters, ffmetadata `[CHAPTER]` blocks from measured durations, optional cover (attached pic), AAC;
  validated (codec, chapter count, duration). MP3 ZIP includes a playlist. Names carry `DRAFT` until a release exists.
- `chapter_renders.takes_digest` (selected attempt ids + pauses + export settings + title/author) detects stale masters.
- Release approval requires: no pending segments, no flagged segments, no missing/mismatched takes, all masters current. Changing
  any take/regeneration/narrator **revokes** the approval automatically.

## Voices and the recording lab

Voices are versioned and immutable per version (`voice_versions`); books pin `voice_version_id` and a fingerprint
(`config.synth_fingerprint`: engine, language, sampling params, voice version/model/speaker/instruct/reference SHA, delivery
instructions). Changing a book's narrator requires explicit confirmation and regenerates everything; old takes are kept as
history but can never be selected into the book. **Designed voices drift** (VoiceDesign recreates the voice every call); the UI
offers "promote a preview to a stable narrator" (becomes a clone reference).

The **recording lab** (`services.create_recording` etc.) stores *draft recordings* in `<home>/data/recordings/<rec_id>/`
(`original.<ext>`, `reference-24k.wav`, `meta.json`; auto-expire after 14 days). A draft gets an objective quality report
(`reference.quality_report`), can be checked with Whisper or auditioned with the Base model (as `preview` jobs), and becomes a
clone voice via `create_voice_from_recording`, which reuses `create_clone_voice` (consent recorded, SHA-256 stored). A take rated
`poor` cannot become a narrator. **One narrator uses one reference clip**; extra takes are audition candidates.

## Security model (`web/security.py`, `web/auth.py`, `tls.py`)

Request pipeline (every request):
1. **Host allowlist** (DNS-rebinding defence): host:port must be one of this machine's names/IPs (`local_names()` plus
   `server.allowed_hosts`) on the HTTP port or the active HTTPS port, else `421`.
2. **CSRF** for POST/PUT/PATCH/DELETE: `Origin` must match the Host, `Sec-Fetch-Site` must be same-origin/none, and the custom header
   `X-Studio-Request: 1` is required (the SPA and CLI add it). No CORS headers are ever sent (same-origin app).
3. **Authentication** when any user exists: cookie `studio_session` (HMAC-signed, 12 h, `HttpOnly`, `SameSite=Strict`, `Secure` on
   HTTPS), validated against the user's password-hash prefix, so a password change or user removal ends old sessions. Public paths:
   `/`, `/static/*`, `/api/auth/login|status|logout`, `/tls/studio-certificate.pem`. Others → `401`.
4. **Roles:** `admin` and `operator`. `ADMIN_RULES` in `web/auth.py` lists admin-only routes (settings write, storage and folder
   browser, move/archive book, worker restart, archive voice, user management, doctor). Operators get `403`.
5. If bound to a non-loopback address with **no** user, every request is `503` (the studio refuses to serve unauthenticated on a network).
6. Login: PBKDF2-SHA256 (240k iterations, per-user salt), constant-work on unknown users, 6 failures per 5 minutes per client IP.
7. Headers: strict CSP (`default-src 'self'` etc.; **no inline scripts/styles**), `nosniff`, `X-Frame-Options: DENY`,
   `Permissions-Policy: microphone=(self)`, `Referrer-Policy: no-referrer`, `no-store` on `/api/*`.

Other protections: uploads are extension-, content- and size-checked; filenames sanitised; media served only by database id with
`ensure_inside` containment (a tampered row pointing outside storage gets 403); manuscript text is escaped in all HTML (SPA
templating escapes by default; the QA report escapes server-side); the generated certificate's key is mode 0600.

TLS: `tls.ensure_certificate` writes `<home>/config/tls/studio-{certificate,key}.pem` (EC P-256, 825 days, SAN = this machine's
DNS names and IPs + `allowed_hosts`), regenerating if a name is missing or it expires within 30 days. It is **self-signed**:
browsers warn once; the certificate can be downloaded from `/tls/studio-certificate.pem` to trust it. Browsers expose the
microphone only on HTTPS or localhost, which is why the HTTPS listener exists.
