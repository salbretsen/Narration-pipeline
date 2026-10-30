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
`POST /api/books/{id}/move`, `POST /api/books/{id}/archive`, `POST /api/worker/restart`, `POST /api/voices/{id}/archive`, `DELETE /api/voices/{id}/refs/{ref_id}`,
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
| GET | `/api/events` | SSE: `state` events carrying a digest `{events, segments, jobs, attempts, worker, active, progress, worker_message}`; `?once=1` for tests. **v0.8.0:** the digest is a pure function of stored state and carries no clock-dependent value (`worker` omits `heartbeat_age_s` and `heartbeat_at`; `online` still flips when the heartbeat goes stale), so an idle studio emits no events. The exact heartbeat age is in `GET /api/worker` |
| GET | `/api/overview` | dashboard cards, segment lifecycle counts, recent books (each with `runtime`), flags, events, failed jobs, worker |

## Imports, books, queue

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/imports` | multipart `file`; returns a preview (chapters, words, segments, read/not-read manifest, warnings, SHA-256, token) |
| GET | `/api/imports/{token}` | re-read a preview (kept 48 h) |
| POST | `/api/imports/{token}/confirm` | `{title, author, narrator_credit, publisher, year, description, genre, language, voice_id, instructions, include[], queue}`; idempotent |
| GET | `/api/books` | summaries (`?archived=true` to include archived). **v0.8.0:** each summary carries `runtime` (processing-time estimate or actual, below) |
| GET/PATCH | `/api/books/{id}` | full detail (chapters, renders, exports, release check, logs) / edit metadata. **v0.8.0:** the query count no longer grows with the segment count (4,000 synthetic segments: 30 queries); adds `cover_version` (cover file mtime, the cache key for `/media/cover/{id}`) and `runtime`. `?view=review` returns the **light** payload for the Review page: the summary fields plus `chapters[]` (`id, index, title, words, segments, title_overridden, original_title, est_duration_s, status_counts`) and no readiness, `release_check`, exports, logs, utility jobs, narrative status, manifest or per-chapter `missing_audio`/`render_current`/`renders`. Any other `view` value returns the full payload |
| POST | `/api/books/{id}/narrator` | replace THIS version's narrator: `{voice_id, instructions, confirm_invalidate}`; `409` with `rendered_segments` until confirmed (regenerates this version) |
| POST | `/api/books/{id}/versions` | **narrate the same manuscript with another voice** as a NEW book (v0.7.0): `{voice_id, instructions?, queue?}`. Copies manuscript, chapters, characters, attribution, Director guidance and text edits; every segment starts pending; the source book is untouched. `409` if that voice+instructions already has a version, or the source is rendering. Book payloads carry `work_uid`, `narrator`, `display_title` (`Title - Narrator`), `version_count`; `GET /api/books/{id}` also has `versions[]` |
| POST | `/api/books/{id}/resnapshot` | apply current global settings; `{confirm_invalidate}` |
| POST | `/api/books/{id}/cover` | multipart JPEG/PNG (extension, size <= 10 MB and magic bytes checked); embedded as attached picture in M4B exports. Generated placeholder covers are not offered: they would need Pillow, which is not a dependency |
| POST | `/api/books/{id}/chapters/{cid}/regenerate` | regenerate every rendered segment of one chapter; `{confirm_invalidate}`; `409` with `rendered_segments` until confirmed; deletes that chapter's masters, revokes any release, queues the book at the front |
| PATCH | `/api/books/{id}/chapters/{cid}` | `{title}`: title override (empty restores the parsed title); `chapters.title` is the effective title, `chapters.original_title` (schema v3) keeps the parsed one; book detail exposes `title_overridden` and `original_title`. Audio is never invalidated; the chapter master just becomes "outdated" until re-assembled |
| POST | `/api/books/{id}/assemble` | enqueue chapter-master assembly |
| POST | `/api/books/{id}/export` | `{kind: m4b | chapter_m4b | mp3_zip, chapter_id?}` enqueues an export job; `chapter_m4b` exports one chapter (chapter markers/title tags use the effective title), blocked like the book export if any segment lacks usable audio, labelled DRAFT until the book is released |
| POST | `/api/books/{id}/report` | write the QA report (HTML + JSON) |
| POST | `/api/books/{id}/release` | `{approved_by, listen_scope: full|partial, attest, notes}`; `409` with blockers |
| POST | `/api/books/{id}/release/revoke` | `{reason}` |
| POST | `/api/books/{id}/move` | admin: `{new_root}` copy-verify-switch |
| POST | `/api/books/{id}/archive` | admin: `{archived}`; `409` while a job is active |
| GET | `/api/books/{id}/segments` | `?chapter_id&filter&after&limit&q`; filters `all issues flagged generated passed approved pending overridden noted`; keyset paging (`next`) |
| GET | `/api/books/{id}/logs` | project log |
| GET | `/api/queue` | `?finished_offset=0&finished_limit=10&utility_offset=0&utility_limit=10` (limits 1-50). Reply: `active` (queued/running/paused book jobs), `finished` (**one entry per book**, its latest finished run in the last `window_days` = 30, with `latest_run` and `earlier_runs[]` of `{id, status, finished_at, segments_rendered, active_seconds, kind_hint "full pass"|"touch-up"}`, at most 20), `finished_total` (books), `utility` (assemble/export/preview/analyze jobs, newest first) with `utility_total`, the offsets and limits echoed back, `jobs` (= `active` + `finished`, kept for older callers) and `throughput` (global median seconds per segment). Every book job carries `runtime`, `active_seconds`, and the older `eta_s` / `eta_finish_s` (now derived from the same estimate) |
| POST | `/api/queue` | `{book_id, front}`; **idempotent** (`created:false` when already queued) |
| POST | `/api/queue/reorder` | `{job_ids[]}` |
| POST | `/api/jobs/{id}/move` | `{direction: up|down|top|bottom}` |
| POST | `/api/jobs/{id}/{pause|resume|retry|cancel}` | pause/cancel take effect between segments |
| GET | `/api/jobs/{id}`, `/api/jobs/{id}/log` | job and its log |

**Processing-time `runtime` object (v0.8.0, `studio/runtime.py`).** Carried by book summaries (`/api/books`, `/api/overview`, `/api/books/{id}`) and by every book job in `/api/queue`:
`{state running|queued|finished|idle, processing_s, remaining_s, finish_at, starts_at, total_est_s, estimated, basis}`. All times are **processing** seconds / epoch seconds, never audio length (`est_duration_s` and
`audio_duration_s` are separate fields). `finished`: `processing_s` = sum of `jobs.active_seconds` over all the book's book jobs (jobs from before v0.8.0 fall back to `finished_at - started_at`), `estimated: false`; a finished queue entry that is not the latest completed run of a fully rendered book (failed, cancelled, touch-up) shows that run's own time instead.
`running`: `remaining_s`, `finish_at`. `queued`: `total_est_s`, `starts_at` (null when the job is paused or something ahead has no estimate; paused jobs do not advance the queue). `idle`: `total_est_s` for the pending part.
Rate (seconds per segment): for a running job, its own pace once >= 10 of its segments are done (`active_seconds` / segments, which includes Director time); else the median for the book's TTS model over the 200 most recent segments (>= 20 samples); else the global median (same minimum);
else every estimate is null and `basis` reads "estimate after N more segments". The Director's measured seconds per directed segment are added for undirected pending segments when it has run, except for an observed job rate.
`basis` is one plain sentence on where the number comes from; the UI shows it as a tooltip.

## Segments (review)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/segments/{id}`, `/api/segments/by-sid/{sid}` | detail: attempts (ASR diff, issues, acoustics, versions), reviews, prev/next, chapter timing |
| POST | `/api/segments/{id}/approve` | `{note}`; needs a selected take with the book's fingerprint |
| POST | `/api/segments/{id}/regenerate` | `{note, direction?}`; resets the budget, enqueues the book at the front, revokes release. **v0.8.0:** `direction` is `{emotion?, intensity? 1-5, pacing?, tension? 1-5, delivery? (<= 240 chars)}`; it is stored as the segment's own user direction (the same record `PATCH /api/segments/{id}/narrative` writes, no one-shot mode), fields left out keep their stored value, a field sent as `null` is cleared, an emotion without an intensity means 3, bad vocabulary is `422`. A user direction reaches the voice even when the book's Director is off and on classic books. If the direction change already put the segment back to `pending`, the book is queued without a second reset. The reply gains `delivery` (see `delivery-preview`) |
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
| GET | `/api/segments/{id}/delivery-preview` | **v0.8.0** `?emotion&intensity&pacing&tension&delivery`: what would reach the voice, saving nothing and never calling the LLM. An absent parameter keeps the stored user direction, an empty one clears it. Reply: `{segment_id, voice_kind preset|design|clone, voice {id, name}, model_id, applied (the emotion itself reaches the voice), instruct, pace, pace_reason, bank_clip, bank_bucket, bank_enabled, reason, source you|director|none, direction, user_direction, notes[]}`; for a clone the notes say it cannot take written instructions and whether a reference-bank clip would be used. `409` when the book has no narrator, `422` on bad vocabulary |
| POST | `/api/segments/{id}/preview` | queues a preview with the segment's own voice and delivery (not saved as a take) |
| POST | `/api/segments/{id}/redirect` | marks the direction stale and queues a Director run |
| POST | `/api/books/{id}/narrative/apply-delivery` | **v0.7.0** `{confirm=false, include_approved=false}`: bring already rendered takes in line with the delivery settings now in force. Without `confirm` a `409` (NeedsConfirmation) with `details {segments, approved, chapters, approved_included}` and nothing changes; with it, only segments whose selected take's `gen_key` differs from the current plan are reset (approved ones only with `include_approved`), release approval is revoked, the book is queued first. Returns `{segments, reset, queued, job_id, approved, chapters}` (`reset: 0`, `message` when nothing differs). Old takes stay as history |
| POST | `/api/segments/{id}/ab` | **v0.7.0** `{comparison}` (`directed_vs_undirected`, `llm_vs_template_translator`, `bank_vs_base_clip`, `pace_on_vs_off`): queue a blind A/B round, two `preview` jobs with the same seed. `409` when the pair would sound identical or the voice kind does not apply; `422` for an unknown comparison. Returns `{round_id, jobs, segment_id, book_id, comparison}` |
| GET | `/api/ab/{round_id}` | the round as the listener sees it: `{status pending|ready|failed, sides:[{side x|y, job_id, status, media_url}], voted, mock, engine, title, segment_id, book_id}`; after a vote also `revealed {x, y}` (setting labels), `vote`, `tally`. Clips are `/media/preview/{job_id}`. The mapping of X/Y to settings is not in the response until a vote exists |
| POST | `/api/ab/{round_id}/vote` | `{choice: x|y|same, other_narrator=false}`; one vote per round (`409` otherwise, or while a clip is unfinished). Returns `{winner a|b|same, revealed, tally, comparison}` |
| GET | `/api/books/{id}/ab` | `{comparisons: [{id, title, a_label, b_label, applies}], tally: {comparison: {a_wins, b_wins, same, votes, non_tie, a_share, other_narrator_flags, other_narrator_share, ...}}}` for the book |
| GET | `/api/books/{id}/ab/random` | `?comparison=&exclude=1,2,3`: a segment where the two settings give different audio (`{segment_id, sid, chapter_idx, excerpt}`), `404` if none among up to 200 sampled |

Narrative status (`GET /api/books/{id}/narrative`) now also carries `ab` (the tally of every comparison). `GET /api/segments/{id}/inspect` `delivery` carries `pace`, `pace_reason`, `ref_id`, `ref_bucket`, and each attempt `pace_used` and `ref_id_used`. Director stage stats gain `chapter_plans`, `chapter_plan_failures`, `chapter_plan_cache_hits`, `scene_calls`, `reasoning_chars`, `would_escalate`, `translations`, `translation_cache_hits`, `translation_failures`.

List filters added: `speaker_review`, `direction_review`. Attempts in `GET /api/segments/{id}` now include `gen_key, instruct_used, delivery_applied, asr_skipped, peak_mem_gb, voice_id_used`.

## Voices and the recording lab

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/voices` | voices, supported speakers (`Ryan Aiden` English; others are Chinese-native). **v0.8.0:** constant number of queries however many voices exist; each voice carries `derived_from` (lineage or `null`) and clones carry `source_voice {id, name, archived}` (or `null`) |
| GET/PATCH | `/api/voices/{id}` | detail with versions / edit (sound-affecting edits create a new version; a name-only edit does not). The reply carries `books_on_older_version [{id, title, voice_version}]`. **v0.8.0:** editing a designed voice's description keeps `voices.description` in sync |
| POST | `/api/voices/{id}/duplicate` | **v0.8.0** `{name, instruct?, speaker?, description?}`: copy a **preset or designed** voice (built-ins included) into a new, non-built-in voice at version 1 from the source's current version, optionally with another instruction / speaker (presets only) / description. The source is untouched. `422` for a clone, a bad speaker, an unusable name or a design description outside 10-1000 characters. Reply: the new voice, with `derived_from {relation "copy", voice_id, voice_name, version}` |
| POST | `/api/voices/{id}/retune` | **v0.8.0** `{name, instruct?}` on a **promoted clone**: creates a new designed voice from the clone's source design voice (its current description, or `instruct`), ready for new previews and another promotion. `422` if the voice is not a clone or no source design voice is recorded. Reply: the new voice, `derived_from {relation "retune", ..., clone_voice_id, clone_name}` and `source {id, name, archived}` |
| POST | `/api/voices/preset` and `/api/voices/design` | create preset `{name, speaker, instruct}` / designed `{name, description}` |
| POST | `/api/voices/clone` | multipart upload: `file, name, ref_text, rights_holder, owner_attest, statement`; consent is mandatory |
| POST | `/api/voices/{id}/preview` | `{text, instruct, verify, seed}` enqueues a preview job (real synthesis) |
| POST | `/api/voices/promote` | `{job_id, name}` turn a designed-voice preview into a stable clone narrator |
| POST | `/api/voices/{id}/archive` | admin |
| GET | `/api/voices/{id}/refs` | emotion reference bank of a CLONED voice (`422` for other kinds). **v0.9.0: one row per emotion** (the 13 of `vocab.EMOTIONS` other than neutral and calm): `{voice_id, synthetic, source_voice|null, sources:[clone, intensify, design?, directable?], min_similarity, buckets:[{bucket, label, family, emotion_text, script_id, ref|null, pending {job_id, status, source}|null, candidates:[{id, label, created_at, duration_s, source, asr_check, drift, similarity, engine, mock, note}]}]}`; a `ref` is `{id, bucket, ref_text, duration_s, source recording|synthetic, source_ref, metrics {gain_db, f0_delta_hz, centroid_delta_hz, wer, engine, similarity, candidate_source, ...}, mock}` |
| POST | `/api/voices/{id}/refs/candidates` | **v0.9.0** `{bucket, source=clone|intensify|directable|design, source_voice_id?}`: queue ONE take of that emotion's script (`409` while one is queued or running for that emotion). `clone`: read by the clone itself (Base model, its base reference); `intensify`: the same from the emotion's adopted clip (`422` without one); `directable`: the clone's speaker vector in the CustomVoice model with the emotion as an instruction (`422` when `models.directable_clones` is off); `design`: the designed source voice with the seed and instruction that made the clone's base clip (`422` unless the clone came from a designed voice). Returns `{voice_id, bucket, source, job}`. The finished job registers a draft recording (`target_voice_id`, `candidate_source`, `asr_check`, `drift`, `similarity`) |
| DELETE | `/api/voices/{id}/refs/candidates/{recording_id}` | **v0.9.0** discard one take (its draft recording); `404` when it was not made for this narrator. Returns the refs body |
| DELETE | `/api/voices/{id}/refs/candidates?bucket=` | **v0.9.0** discard every take of one emotion and withdraw a queued one. Returns the refs body plus `discarded` |
| POST | `/api/voices/{id}/refs/adopt` | `{bucket, recording_id, owner_attest=false, statement=""}`: adopt a lab recording as the bucket's clip (replaces an existing one). `422` unless the recording has a Whisper check with word error <= 8%, is not rated poor (microphone recordings), and satisfies the consent rule: a take the clone made itself (`clone`/`intensify`/`directable`) needs no attestation (consent type `self`); otherwise human clone = `owner_attest` + statement >= 10 chars and a non-synthetic recording; synthetic clone = a candidate generated by its own source voice. v0.9.0: refused when `narrative.delivery.bank_min_similarity` is set and the take's measured speaker similarity is below it (mock-engine similarities never gate). Returns the new `GET .../refs` body |
| GET | `/api/voices/{id}/sample` | **v0.9.0** the narrator's kept sample: `{status: none|queued|running|ready, job_id?, url?}`. `GET /api/voices` carries the same as `sample` on every voice |
| POST | `/api/voices/{id}/sample` | **v0.9.0** generate the sample once (a deduplicated preview job; kept as `references/<uid>/sample-v<version>.wav`, outside the pruned previews folder, until the voice gets a new version); returns the same body. Served at `GET /media/voice/{id}/sample` |
| DELETE | `/api/voices/{id}/refs/{ref_id}` | **admin only**: remove a clip and its files. Takes already made with it keep their audio |
| GET | `/api/reference-scripts` | the lab's sample scripts (id, tier core/range, category, title, purpose, text, words, est_seconds) and the audition text |
| POST | `/api/recordings` | multipart `file, script_id, ref_text, label` → draft recording with quality report; `422` (with `details.quality`) if too short/silent/undecodable |
| GET | `/api/recordings` | drafts (newest first; drafts older than 14 days are deleted on listing) |
| GET | `/api/recordings/compare?ids=rec_a,rec_b[,...]` | 2-8 saved takes side by side → `{rows:[{id, label, script_title, verdict, duration_s, peak_dbfs, rms_dbfs, noise_floor_dbfs, snr_db, clip_ratio, warnings, fails, ref_text, rank, recommended}], recommended_id}`. Advisory rank: verdict, then fewer warnings, then voice-to-noise; poor takes are never recommended (`recommended_id` null if all poor). `422` outside 2-8 ids, `404` unknown id. Whisper transcript match comes from the existing `check` jobs (the UI merges them). Picking a take = the existing `POST /api/voices/from-recording` |
| PATCH / DELETE | `/api/recordings/{rid}` | edit the transcript `{ref_text}` / delete |
| POST | `/api/recordings/{rid}/job` | `{mode: check|audition, text?}` → a preview-kind job. `check` = Whisper vs transcript; `audition` = Base-model clone of `text` |; since v0.7.0 a finished `check` also stores `asr_check {matches, wer, ...}` in the recording's meta (editing the transcript clears it), and recordings made from reference-bank candidates carry `source: synthetic`, `source_voice_id`, `bucket`, `drift` |
| POST | `/api/voices/from-recording` | `{recording_id, name, rights_holder, owner_attest, statement}`; `422` if not attested or rated poor |

## Pronunciation and voice pace (C2; `web/pronunciation_api.py`, logic in `pronunciation.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/pronunciations[?book_id=N]` | `{global: [...], book: [...] (only with book_id), pace_range: [0.8, 1.25]}`; an entry is `{id, book_id, scope, written, spoken, case_sensitive, whole_word, enabled, note}` |
| POST | `/api/pronunciations` | `{written, spoken, book_id?, case_sensitive=false, whole_word=true, note, confirm_invalidate=false}`; `book_id` omitted = global; `409` if the same written form already exists in that dictionary |
| PATCH | `/api/pronunciations/{id}` | any of `written, spoken, case_sensitive, whole_word, enabled, note, confirm_invalidate` |
| DELETE | `/api/pronunciations/{id}[?confirm_invalidate=true]` | remove an entry |
| POST | `/api/pronunciations/try` | `{text, book_id?}` -> `{input, output, changed}`: what the dictionary would hand the voice (nothing is stored) |
| PUT | `/api/voices/{id}/pace` | `{pace 0.8-1.25, confirm_invalidate}`; `GET /api/voices` items now carry `pace` |

Every write that changes the spoken text of already-rendered segments answers **`409` with `details: {stale_segments, books}`** and changes nothing
until repeated with `confirm_invalidate: true` (then only those segments go back to `pending`, old takes remain as history, any release approval is
revoked). Writes that affect no rendered segment succeed immediately with `stale_segments: 0`. Re-queue the book (`POST /api/queue`) to regenerate.

## Media (all support GET and HEAD, single-range requests, correct MIME)

`/media/attempt/{id}` (a take; `?download=1`) · `/media/chapter/{chapter_id}.{wav|mp3}` · `/media/export/{id}` ·
`/media/preview/{job_id}` · `/media/recording/{rid}` · `/media/voice/{voice_id}/reference` · `/media/voice/{voice_id}/ref/{ref_id}` (a bank clip, v0.7.0) · `/media/cover/{book_id}` ·
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
| GET | `/api/disk/usage` | **v0.8.0:** served from a 60 s cache (`?refresh=1` forces a new measurement; one measurement at a time, concurrent callers share it; delete, purge, backup and restore drop the cache). The reply adds `measured_at` (epoch s), `age_s` and `cached`. Per book and per category sizes (manuscripts, segments = selected takes, alternates = other takes/attempts, reports, masters, exports; plus a `purgeable` estimate), global items (voices, recordings, previews, import copies, database, backups, logs, model cache), free space of the library volume, model cache and backups folder, and the backup list |
| GET | `/api/disk/backups` | `{books: [...zip files in backups/books], databases: [...files in backups/db]}` |
| POST | `/api/disk/books/{id}/delete` | `{confirm: true}`: delete the book (database rows, its storage folders, its import copy). Audited. Moved-away old copies are not touched |
| POST | `/api/disk/books/{id}/delete-all-versions` | `{confirm: true}`: remove the book entirely, i.e. every narrator version of its manuscript (v0.7.0). All-or-nothing: refused with `409` if any version is queued, rendering or paused. Admin only; `409` + preview (versions, bytes) until confirmed. Audited. `/delete` above removes just one version |
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
- `narrative` (v0.3, doc 9): `semantic_segmentation` (default off), `max_segment_chars 500`, `min_segment_chars 120`, `context_before_chars 1250`, `context_after_chars 1250`, `attribution_threshold 0.6`, `emotion_sensitivity 0.5`, `director_enabled`, `director_lookahead 12`, `continuity_smoothing`, `smoothing_strength 0.6`, `reset_scope`, `cascade_limit 24`, `verify_mode every|sample|off`, `delivery{custom_voice, voice_design, max_instruct_chars, clone, pace_from_direction, pauses_from_direction, translator, bank_min_intensity}`, `director_chapter_plan`, `llm{provider, base_url, model, chapter_model, scene_model, translator_model, api_key_env, timeout_s, temperature, max_tokens, chapter_max_tokens, chapter_timeout_s}` (new keys and defaults: doc 9; the API key is not a setting: save it with `PUT /api/secrets/llm`; `api_key_env` is a fallback environment-variable name `STUDIO_LLM_*`).
- `POST /api/narrative/llm/test` `{base_url, model, chapter_model?, scene_model?, translator_model?}` (Settings > Narrative > Local LLM > Test connection; admin; uses the typed values, saved or not, and the saved key): returns `{ok, checks:[{name, ok, detail}]}` for reachable, key accepted, model listed, one tiny question answered, and (v0.7.0, when the server lists its models) each distinct chapter/scene/translator model is listed. A model that only produces reasoning fails the question check instead of reporting an empty reply.
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
