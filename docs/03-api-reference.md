# 3. API, settings and CLI reference

All mutating requests (POST/PUT/PATCH/DELETE) need the header `X-Studio-Request: 1` and a same-origin `Origin`. When any user
exists every route except the public ones needs the `studio_session` cookie. Errors are JSON `{"error": "...", "details": ...}`
with the status in the table below. Request bodies are Pydantic models with `extra="forbid"` (unknown fields are `422`).
Handlers live in `src/app/studio/web/app.py`; logic in `services.py`.

Status codes used: `401` sign-in required, `403` forbidden (CSRF, role, path containment), `404`, `409` conflict (duplicate job,
needs confirmation, state), `413` too large, `421` bad Host, `422` validation, `429` login rate limit, `503` storage unavailable
or network bind without users.

**Attribution:** every `audit_log` and `review_actions` row records the signed-in user's name as `actor` (set per request by the guard
middleware through `studio.context.current_actor`; `ctx.audit(...)` and review actions resolve it automatically). `operator` appears only when no one is
signed in (loopback without accounts); CLI uses `cli`, the worker `system`. Release approval keeps the typed `approved_by` name in `releases`, and the
`release_approved` audit row's `actor` is the signed-in account (`signed_in_user` in its detail); the QA report shows it as `release.approved_by_account`
and each `review_actions[].actor`. No schema change.

**Admin-only** (operators get 403): `PUT /api/settings`, `GET /api/storage`, `GET /api/storage/browse`, `POST /api/storage/*`,
`POST /api/books/{id}/move`, `POST /api/books/{id}/archive`, `POST /api/worker/restart`, `POST /api/voices/{id}/archive`,
`GET|POST /api/auth/users`, `GET /api/doctor`, and everything under `/api/disk/` (disk management, below).

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

## Narrative (v0.3; all in `web/narrative_api.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/narrative/vocabulary` | emotions, pacings, intensity range |
| GET | `/api/books/{id}/narrative` | stage status/stats/errors, counts (segments, directed, speaker/direction review, stale), latest analysis job, Director cache |
| GET, PUT | `/api/books/{id}/narrative/settings` | effective settings + overrides + defaults; PUT `{overrides}` merges (null removes a key); **admin only** |
| POST | `/api/books/{id}/analyze` | `{stages?, rebuild?, confirm?}` queues the `analyze` job (409 NeedsConfirmation when converting a rendered legacy book) |
| POST | `/api/books/{id}/resegment` | Pass 2 again (reconciles; resets only segments whose voice/delivery changed) |
| POST | `/api/books/{id}/continuity/recompute` | re-smooth all stored directions (no LLM calls) |
| GET | `/api/books/{id}/narrative/metadata` | JSON of characters and every segment (offsets, speaker, status, direction, instruction) |
| GET, POST | `/api/books/{id}/characters` | directory (+ voices list); create |
| PATCH | `/api/characters/{id}` | name, aliases, gender, `voice_id`, accent, speech pattern, personality, description, delivery preference, relationships; a voice change re-cuts and resets only affected segments |
| POST | `/api/characters/{id}/merge` | `{into_id}` |
| GET, POST | `/api/books/{id}/notes`; DELETE `/api/notes/{id}` | story notes/summaries; user facts are confirmed |
| GET | `/api/segments/{id}/inspect` | context windows, spans with evidence, speaker, voice, direction (raw/smoothed/user/final), neighbours, instruction + spoken text sent to the engine, history, attempts with `gen_key` match |
| PATCH | `/api/segments/{id}/narrative` | `character_id, voice_id, emotion, intensity, tension, pacing, delivery`; regenerates only this segment |
| POST | `/api/segments/{id}/boundary` | `{op: split|merge_next|move_end, offset}` |
| POST | `/api/segments/{id}/preview` | queues a preview with the segment's own voice and delivery (not saved as a take) |
| POST | `/api/segments/{id}/redirect` | marks the direction stale and queues a Director run |

List filters added: `speaker_review`, `direction_review`. Attempts in `GET /api/segments/{id}` now include `gen_key, instruct_used, delivery_applied, asr_skipped, peak_mem_gb, voice_id_used`.

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
| GET | `/api/recordings/compare?ids=rec_a,rec_b[,...]` | 2-8 saved takes side by side → `{rows:[{id, label, script_title, verdict, duration_s, peak_dbfs, rms_dbfs, noise_floor_dbfs, snr_db, clip_ratio, warnings, fails, ref_text, rank, recommended}], recommended_id}`. Advisory rank: verdict, then fewer warnings, then voice-to-noise; poor takes are never recommended (`recommended_id` null if all poor). `422` outside 2-8 ids, `404` unknown id. Whisper transcript match comes from the existing `check` jobs (the UI merges them). Picking a take = the existing `POST /api/voices/from-recording` |
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

## Disk management (`web/disk_api.py`, logic in `diskmgmt.py`; all admin-only)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/disk/usage` | per book and per category sizes (manuscripts, segments = selected takes, alternates = other takes/attempts, reports, masters, exports; plus a `purgeable` estimate), global items (voices, recordings, previews, import copies, database, backups, logs, model cache), free space of the library volume, model cache and backups folder, and the backup list |
| GET | `/api/disk/backups` | `{books: [...zip files in backups/books], databases: [...files in backups/db]}` |
| POST | `/api/disk/books/{id}/delete` | `{confirm: true}`: delete the book (database rows, its storage folders, its import copy). Audited. Moved-away old copies are not touched |
| POST | `/api/disk/books/{id}/purge` | `{scope: takes\|exports\|masters, confirm: true}`. `takes` removes attempts that are not any segment's selected take (selected and therefore approved takes always survive; in-progress attempts are skipped); `exports` removes export rows and files; `masters` removes chapter renders (re-run Assemble). Audited |
| POST | `/api/disk/books/{id}/backup` | write a book backup zip to `backups/books/` (refused with `409` while the book's job is running) |
| POST | `/api/disk/restore` | `{name}`: restore a zip from `backups/books/` as a NEW book (new uid and ids, title gets " (restored)") |
| POST | `/api/disk/backups/delete` | `{name, confirm: true}` delete one book backup file |
| POST | `/api/disk/db-backup` | consistent copy of `studio.db` (SQLite online backup) to `backups/db/studio-v<schema>-manual-<time>.db`, verified with `integrity_check` |

Destructive routes need `confirm` to be the JSON boolean `true` (strict: `"yes"` is rejected with `422`). Without it they answer
`409` with `details` describing what would be removed (item count, bytes), so the UI can show a precise confirmation. Delete and
purge are refused with `409` while the book has a queued, running or paused job. Backup and restore names must be plain
`*.zip` file names inside `backups/books/` (no separators, no `..`); anything else is `422`.

**Book backup format** (`audiobook-studio-book-backup`, version 1): a zip with `manifest.json` (format, version, app and schema
version, book uid and title, created time, and every media file with size and SHA-256), `db.json` (the book row and its rows from
manuscripts, chapters, characters, segments, attempts, nar_spans, story_notes, directions, direction_log, seg_edits,
segment_history, chapter_text, analysis_stages, review_actions, chapter_renders, exports; file paths are stored as
`@cat/<category>/<relative path>` tokens; plus a voice reference list), and `media/<category>/<relative path>` for every file in
the book's storage folders. Jobs, logs, events and release sign-offs are not included. Restore validates every entry name (no
absolute paths, `..`, backslashes or unknown categories), streams each file while checking size and SHA-256, requires free space,
re-roots paths under a fresh uid, maps voices by uid (built-in voices by name; an unknown narrator leaves the book without one
and a warning is returned), and removes everything it created if any step fails. Archives from a newer schema are refused.

### Settings schema (`config.Settings`, defaults)

- `storage`: `library_root` and per-category dirs (`manuscripts references segments alternates reports masters exports`); empty = default under `<home>/library`.
- `chunking`: `min_chars 150`, `max_chars 400`, `merge_short_paragraphs`, `speak_chapter_titles`, `normalize_for_speech`.
- `qa`: `enable_asr`, `wer_threshold 0.12`, `max_attempts 3`, `max_dropped_words 2`, `max_inserted_words 2`, `dead_air_seconds 1.6`,
  `clip_ratio_limit 0.001`, `min_rms_dbfs -42`, `max_rms_dbfs -8`, `duration_ratio_min 0.5`, `duration_ratio_max 2.0`, `words_per_minute 155`.
- `pauses`: `sentence_ms 300`, `paragraph_ms 800`, `scene_break_ms 1800`, `after_heading_ms 1500`, `chapter_head_ms 800`, `chapter_tail_ms 2500`, `room_tone_dbfs -72`, `trim_silence`.
- `models`: `engine` (mlx on Apple Silicon else mock), `custom_voice_model`, `base_model`, `voice_design_model`
  (`mlx-community/Qwen3-TTS-12Hz-1.7B-{CustomVoice,Base,VoiceDesign}-bf16`), `asr_model` (`mlx-community/whisper-large-v3-turbo`),
  `language`, `temperature/top_p/repetition_penalty` (null = model default), `retry_temperatures [0.7, 0.55]`, `max_warm_models 2`, `allow_downloads`.
- `narrative` (v0.3, doc 9): `semantic_segmentation` (default off), `max_segment_chars 500`, `min_segment_chars 120`, `context_before_chars 1250`, `context_after_chars 1250`, `attribution_threshold 0.6`, `emotion_sensitivity 0.5`, `director_enabled`, `director_lookahead 12`, `continuity_smoothing`, `smoothing_strength 0.6`, `reset_scope`, `cascade_limit 24`, `verify_mode every|sample|off`, `delivery{...}`, `llm{...}` (the API key is not a setting: save it with `PUT /api/secrets/llm`; `api_key_env` is a fallback environment-variable name `STUDIO_LLM_*`).
- `PUT /api/secrets/{slot}` `{"value": "..."}` and `DELETE /api/secrets/{slot}` (admin only; slot `llm` today): save or remove an API key for an outbound OpenAI-compatible endpoint. Write-only: `GET /api/settings` returns `secrets: {slot: {label, configured, updated_at}}` and never the key. Keys are visible ASCII without spaces, at most 400 characters; the audit log records the action without the value.
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
