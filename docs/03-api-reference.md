# 3. API, settings and CLI reference

All mutating requests (POST/PUT/PATCH/DELETE) need the header `X-Studio-Request: 1` and a same-origin `Origin`. When any user
exists every route except the public ones needs the `studio_session` cookie. Errors are JSON `{"error": "...", "details": ...}`
with the status in the table below. Request bodies are Pydantic models with `extra="forbid"` (unknown fields are `422`).
Handlers live in `src/app/studio/web/app.py`; logic in `services.py`.

Status codes used: `401` sign-in required, `403` forbidden (CSRF, role, path containment), `404`, `409` conflict (duplicate job,
needs confirmation, state), `413` too large, `421` bad Host, `422` validation, `429` login rate limit, `503` storage unavailable
or network bind without users.

**Admin-only** (operators get 403): `PUT /api/settings`, `GET /api/storage`, `GET /api/storage/browse`, `POST /api/storage/*`,
`POST /api/books/{id}/move`, `POST /api/books/{id}/archive`, `POST /api/worker/restart`, `POST /api/voices/{id}/archive`,
`GET|POST /api/auth/users`, `GET /api/doctor`.

## Auth and system

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/auth/status` | public: `{auth_required, user, engine, version, version_label, https_port, secure, cert_sha256}` |
| POST | `/api/auth/login` | `{username, password}` sets the session cookie; `429` after 6 failures/5 min |
| POST | `/api/auth/logout` | clears the cookie |
| POST | `/api/auth/password` | `{current, new, confirm}`; `new` ≥ 10 chars and must equal `confirm`; ends old sessions |
| GET/POST | `/api/auth/users` | admin: list / `{username, password, confirm, role}` |
| GET | `/tls/studio-certificate.pem` | public: the self-signed certificate (404 if HTTPS is off) |
| GET | `/api/about` | version, developer, company, copyright, build, engine mode, package versions, licences, QA limitations |
| GET | `/api/doctor` | admin: environment checks |
| GET | `/api/worker` | worker status (online, state, message, pid, heartbeat age, supervisor restarts) |
| POST | `/api/worker/restart` | admin: restart the worker process |
| GET | `/api/events` | SSE: `state` events carrying a digest `{events, segments, jobs, attempts, worker, active, progress}`; `?once=1` for tests |
| GET | `/api/overview` | dashboard cards, segment lifecycle counts, recent books, flags, events, failed jobs, worker |

## Imports, books, queue

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/imports` | multipart `file`; returns a preview (chapters, words, segments, read/not-read manifest, warnings, SHA-256, token) |
| GET | `/api/imports/{token}` | re-read a preview (kept 48 h) |
| POST | `/api/imports/{token}/confirm` | `{title, author, narrator_credit, publisher, year, description, genre, language, voice_id, instructions, include[], queue}`; idempotent |
| GET | `/api/books` | summaries (`?archived=true` to include archived) |
| GET/PATCH | `/api/books/{id}` | full detail (chapters, renders, exports, release check, logs) / edit metadata |
| POST | `/api/books/{id}/narrator` | `{voice_id, instructions, confirm_invalidate}`; `409` with `rendered_segments` until confirmed |
| POST | `/api/books/{id}/resnapshot` | apply current global settings; `{confirm_invalidate}` |
| POST | `/api/books/{id}/cover` | multipart JPEG/PNG |
| POST | `/api/books/{id}/assemble` | enqueue chapter-master assembly |
| POST | `/api/books/{id}/export` | `{kind: m4b | chapter_m4b | mp3_zip, chapter_id?}` enqueues an export job |
| POST | `/api/books/{id}/report` | write the QA report (HTML + JSON) |
| POST | `/api/books/{id}/release` | `{approved_by, listen_scope: full|partial, attest, notes}`; `409` with blockers |
| POST | `/api/books/{id}/release/revoke` | `{reason}` |
| POST | `/api/books/{id}/move` | admin: `{new_root}` copy-verify-switch |
| POST | `/api/books/{id}/archive` | admin: `{archived}`; `409` while a job is active |
| GET | `/api/books/{id}/segments` | `?chapter_id&filter&after&limit&q`; filters `all issues flagged generated passed approved pending overridden noted`; keyset paging (`next`) |
| GET | `/api/books/{id}/logs` | project log |
| GET | `/api/queue` | active/finished book jobs, utility jobs, throughput (ETA only after ≥ 20 measured segments) |
| POST | `/api/queue` | `{book_id, front}`; **idempotent** (`created:false` when already queued) |
| POST | `/api/queue/reorder` | `{job_ids[]}` |
| POST | `/api/jobs/{id}/move` | `{direction: up|down|top|bottom}` |
| POST | `/api/jobs/{id}/{pause|resume|retry|cancel}` | pause/cancel take effect between segments |
| GET | `/api/jobs/{id}`, `/api/jobs/{id}/log` | job and its log |

## Segments (review)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/segments/{id}`, `/api/segments/by-sid/{sid}` | detail: attempts (ASR diff, issues, acoustics, versions), reviews, prev/next, chapter timing |
| POST | `/api/segments/{id}/approve` | `{note}`; needs a selected take with the book's fingerprint |
| POST | `/api/segments/{id}/regenerate` | `{note}`; resets the budget, enqueues the book at the front, revokes release |
| POST | `/api/segments/{id}/override` | `{text, regenerate}`; the manuscript text is never changed |
| POST | `/api/segments/{id}/select-take` | `{attempt_id}`; only takes with the current fingerprint |
| POST | `/api/segments/{id}/note` | `{note}` |

## Voices and the recording lab

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/voices` | voices, supported speakers (`Ryan Aiden` English; others are Chinese-native) |
| GET/PATCH | `/api/voices/{id}` | detail with versions / edit (sound-affecting edits create a new version) |
| POST | `/api/voices/preset` and `/api/voices/design` | create preset `{name, speaker, instruct}` / designed `{name, description}` |
| POST | `/api/voices/clone` | multipart upload: `file, name, ref_text, rights_holder, owner_attest, statement`; consent is mandatory |
| POST | `/api/voices/{id}/preview` | `{text, instruct, verify, seed}` enqueues a preview job (real synthesis) |
| POST | `/api/voices/promote` | `{job_id, name}` turn a designed-voice preview into a stable clone narrator |
| POST | `/api/voices/{id}/archive` | admin |
| GET | `/api/reference-scripts` | the lab's sample scripts (id, tier core/range, category, title, purpose, text, words, est_seconds) and the audition text |
| POST | `/api/recordings` | multipart `file, script_id, ref_text, label` → draft recording with quality report; `422` (with `details.quality`) if too short/silent/undecodable |
| GET | `/api/recordings` | drafts (newest first; drafts older than 14 days are deleted on listing) |
| PATCH / DELETE | `/api/recordings/{rid}` | edit the transcript `{ref_text}` / delete |
| POST | `/api/recordings/{rid}/job` | `{mode: check|audition, text?}` → a preview-kind job. `check` = Whisper vs transcript; `audition` = Base-model clone of `text` |
| POST | `/api/voices/from-recording` | `{recording_id, name, rights_holder, owner_attest, statement}`; `422` if not attested or rated poor |

## Media (all support GET and HEAD, single-range requests, correct MIME)

`/media/attempt/{id}` (a take; `?download=1`) · `/media/chapter/{chapter_id}.{wav|mp3}` · `/media/export/{id}` ·
`/media/preview/{job_id}` · `/media/recording/{rid}` · `/media/voice/{voice_id}/reference` · `/media/cover/{book_id}` ·
`/media/manuscript/{book_id}`. Every path is checked to lie inside the book's registered storage (or the data dir); otherwise `403`.

## Settings and storage

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/settings` | `{settings, categories, speakers, engine_mode, effective_paths, library_root}` |
| PUT | `/api/settings` | admin: deep-merge patch, validated; storage paths are validated server-side (created if needed) |
| GET | `/api/storage` | admin: per-category status, project path registry, policy text |
| POST | `/api/storage/validate` | admin: `{path}` → resolved, writable, free/used space, mount/removable state, errors/warnings |
| GET | `/api/storage/browse` | admin: `?path&hidden` lists sub-folders of a directory **on the hosting Mac** (folders only), shortcuts, mounted volumes |
| POST | `/api/storage/mkdir` | admin: `{parent, name}` create a folder (audited) |
| POST | `/api/storage/choose-folder` | admin: native Finder picker; **only works from the Mac itself** (403 for remote clients) |

**Storage policy (decided):** a per-project path registry. A new library location applies to books imported afterwards; existing
books stay findable at their recorded paths and can be moved explicitly (copy, verify size + SHA-256, switch the DB in one
transaction, keep the old copy).

### Settings schema (`config.Settings`, defaults)

- `storage`: `library_root` and per-category dirs (`manuscripts references segments alternates reports masters exports`); empty = default under `<home>/library`.
- `chunking`: `min_chars 150`, `max_chars 400`, `merge_short_paragraphs`, `speak_chapter_titles`, `normalize_for_speech`.
- `qa`: `enable_asr`, `wer_threshold 0.12`, `max_attempts 3`, `max_dropped_words 2`, `max_inserted_words 2`, `dead_air_seconds 1.6`,
  `clip_ratio_limit 0.001`, `min_rms_dbfs -42`, `max_rms_dbfs -8`, `duration_ratio_min 0.5`, `duration_ratio_max 2.0`, `words_per_minute 155`.
- `pauses`: `sentence_ms 300`, `paragraph_ms 800`, `scene_break_ms 1800`, `after_heading_ms 1500`, `chapter_head_ms 800`, `chapter_tail_ms 2500`, `room_tone_dbfs -72`, `trim_silence`.
- `models`: `engine` (mlx on Apple Silicon else mock), `custom_voice_model`, `base_model`, `voice_design_model`
  (`mlx-community/Qwen3-TTS-12Hz-1.7B-{CustomVoice,Base,VoiceDesign}-bf16`), `asr_model` (`mlx-community/whisper-large-v3-turbo`),
  `language`, `temperature/top_p/repetition_penalty` (null = model default), `retry_temperatures [0.7, 0.55]`, `max_warm_models 2`, `allow_downloads`.
- `export`: `sample_rate 44100`, `mp3_bitrate_kbps 192`, `m4b_bitrate_kbps 96`, `loudness_normalize`, `target_lufs -19`, `true_peak_db -3`, `auto_assemble_drafts`.
- `server`: `host 127.0.0.1`, `port 7861`, `allow_remote false`, `https_enabled false`, `https_port 8443`, `allowed_hosts []`,
  `max_manuscript_mb 100`, `max_reference_mb 25`. A non-loopback host requires `allow_remote`. **The Mac's deployment uses host `0.0.0.0`, port 8080, https on 8443.**
- `worker`: `gpu_workers 1` (fixed), `autostart`, `prevent_sleep`, `max_consecutive_errors 5`.

Each book freezes `chunking, qa, pauses, models (minus max_warm_models/allow_downloads), export` at import. `STUDIO_ENGINE=mlx|mock`
overrides `models.engine`; `STUDIO_NO_WORKER=1` stops the server spawning a worker; `STUDIO_MOCK_FAULTS=<json>` injects mock faults.

## CLI (`./studio ...` on the Mac, `python -m studio ...` from `src/app`)

`serve [--host --port --allow-remote]` · `worker` · `migrate` · `doctor [--json]` · `test` · `import FILE [--voice --queue ...]` · `list` ·
`run [--book]` (foreground queue; refuses if the web worker holds the lease) · `resume BOOK` · `issues BOOK` ·
`approve SID | BOOK --release --by NAME` · `report BOOK` · `export BOOK --kind m4b|chapter_m4b|mp3_zip` · `smoke [--ref-audio --ref-text --keep]` ·
`auth list | add-user NAME --role admin|operator [--password P | --generate] | set-password NAME | remove-user NAME` (prompts twice for a typed password).
The CLI works on the files directly (no web login), so shell access as the install user is full control.
