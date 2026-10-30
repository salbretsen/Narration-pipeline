# Changelog

Versions are X.Y.Z: X = Major (a major release update, owner-triggered only); Y = Feature (any new feature);
Z = Minor/Patch (any bug fix, feature alteration, documentation update or housekeeping).

## v0.9.0
Feature release: emotion clips that keep the narrator's voice, directable cloned narrators, and a new Narrator Studio. **What ran where:** the clone-instruction
experiment ran on the Mac with the real Qwen3-TTS 1.7B models and Whisper (`docs/clone_instruct_experiment.json`). This version was then installed on the Mac
(installer re-run; schema 6 → 7) and `tools/e2e/emotion_takes_real.py` drove the running studio's API with the real engine for the narrator "Dalinar (stable)":
the narrator sample (generated in 9 s, served as audio/wav, a second press just plays) and one take per source, each refused a second concurrent take (409),
all Whisper-clean (WER 0%), speaker similarity: clone takes anger 0.993 and joy 0.995, directable sadness 0.989 and fear 0.977, design tenderness 0.968
(different people measured 0.934-0.965); all takes were discarded afterwards. Everything else ran only with the mock engine: 547 pytest tests and headless Edge
(`tools/e2e/polish_voices.py`, `delivery_ui.py`). **Not run on the Mac:** the new pages in a browser (Safari or otherwise), adopting a take, directable
narration of a book; nobody has listened to a directable take blind.

**Emotion clips that sound like the narrator**
- **Why the old candidates sounded like someone else:** they were made by the designed source voice (VoiceDesign), which builds a new voice from its description
  on every call, with a different seed and instruction from the clone's base clip. The clone then copied that other voice.
- **Takes are now made by the clone itself** by default: the narrator reads an emotionally written script (13 new `emotion-*` scripts) with its own reference, so
  its identity is kept by construction. The Mac run measured speaker similarity 0.989-0.995 for these takes (different people: 0.934-0.965) and clear delivery
  changes (median pitch anger 130 Hz, joy 137, tenderness 149 against 98 neutral). This works for clones of human voices too (consent type `self`).
- **Intensify**: a take made from an emotion's adopted clip instead of the base reference.
- **Directable takes and directable narration (experimental, opt-in)**: the clone's own speaker vector (the Base model's speaker encoder) is written into a spare
  speaker row of the CustomVoice model in memory, which then follows a written instruction in the narrator's voice. Measured similarity 0.972-0.993. Used for
  `directable` takes, and for narration when *Settings → Delivery → Cloned voices* is `directable` (segments directed at or above the intensity threshold; other
  segments narrate as before). *Settings → Models → Directable cloned narrators* switches the route off.
- **Speaker similarity** on every take (cosine of speaker-encoder vectors against the base reference), shown as "sounds like the narrator / close / likely a
  different voice"; *Lowest take similarity* (`delivery.bank_min_similarity`, default unset) refuses adoption below a limit. Mock-engine values are labelled and
  never enforced.
- **Design takes** (the old route) now start from the seed and instruction that made the clone's base clip; promotion records them.

**One clip per emotion; one take at a time; reject and discard**
- The bank has one clip per emotion offered in Review & QA and by the Director (13: joy, excitement, tenderness, sadness, grief, fear, anxiety, anger, surprise,
  disgust, determination, humor, resignation; neutral and calm use the base clip). Anger and fear are separate. A missing clip falls back to a related emotion's
  clip (e.g. anxiety → fear, grief → sadness → resignation), then to the base clip; the reason says which. Schema 7 renames existing clips: tense → fear, tender →
  tenderness, sorrow → sadness, bright → joy.
- *Generate a take* queues exactly one take (`409` while one is in flight for that emotion), was three.
- Takes are listed under their emotion (they survive leaving the page) with **Discard** and **Discard all takes** (also withdraws a queued take). Replacing a clip
  also deletes the old clip's files (an original with another extension used to be left behind).

**Narrator Studio and the narrator page**
- Narrator Studio shows **tiles**: name, description and a play button. The first press generates a short sample on the GPU and keeps it (one per voice
  version, outside the pruned previews folder); later presses just play it. *Add a narrator* is a collapsible section below the tiles.
- The name or description opens the narrator's own page (`#/narrator/ID`) with tabs: **Overview** (details and version history), **Emotion references**
  (clones), **Preview**, **Record & upload** (the lab, now with the emotion scripts) and **Tools** (Edit, Duplicate & retune, Retune from source, Archive).
- New endpoints: `GET/POST /api/voices/{id}/sample`, `/media/voice/{id}/sample`, `DELETE /api/voices/{id}/refs/candidates/{rid}` and `?bucket=`;
  `POST .../refs/candidates` takes `source` instead of `n`. Doc 3, 4, 9 and the roadmap (C17) are updated.

**Fixes**
- **An in-app update could roll back for no reason ("did not become healthy within 120 s").** Found on the Mac updating 0.8.1 → 0.8.2: `launchctl bootout`
  returned while the old service was still shutting down, so the following start saw it as loaded, only kickstarted a job that was going away, and nothing ran
  until the rollback. Stopping now waits until launchd has really unloaded the service, and starting loads it again if it vanished before the kickstart.
  The fix takes effect for updates made *from* this version on; an update from 0.8.1 or 0.8.2 may still roll back once (harmlessly) and succeed on a retry.

**Not verified:** a blind listen of directable takes and of directable narration across a chapter; similarity bands on more than one narrator; Safari's
autoplay rule for the tile button (the tile falls back to "Ready: press to play").

## v0.8.2
Patch: release notes reach GitHub and the Update Center. Tested with pytest only (no browser run and nothing run on the Mac); the release workflow
change is verified by the release of this version itself.
- **The Update Center showed "No notes were published for this release"** for every release. The Release workflow signed the index without
  `--notes-file`, so every index entry had empty notes. `tools/sign_release.py --notes-from-changelog` now writes CHANGELOG.md notes into every
  entry it signs, including entries kept from the previous index (the whole index is re-signed; archive digests are not touched), so the
  v0.8.1 entry gets its notes too.
- **Versions that were never published lost their notes.** v0.6.3 to v0.8.0 were never released on GitHub (v0.6.3/v0.6.4 were folded into
  v0.7.0; v0.7.0 and v0.8.0 were never tagged), and the v0.8.1 release described only the license change. A release's notes now contain its own
  section plus every changelog section since the previous *published* release (`tools/release_notes.py vX --since vY`, with
  `--previous` to find vY from `gh release list`); the workflow's backfill step applies the same rule to earlier releases, so the v0.8.1
  GitHub release now lists v0.8.1, v0.8.0 and v0.7.0. Unsigned builds keep their "unsigned" warning instead of being overwritten.
- **The Update Center lists the notes of every version between the installed one and the offered one** (`notes_since_installed` in
  `/api/update/status`, one block per version), and says the notes are on the release page when an index carries none. The notes limit
  is 60000 characters (was 8000 in the client, 7000 in the signer); older studios truncate long notes, they do not reject the index.

## v0.8.1
- **License changed from MIT to proprietary (all rights reserved).** The root `LICENSE` now reads: "Audiobook Studio, Copyright (c) 2026 Scott Albretsen, TSAL Solutions LLC. All rights reserved. This software is
  proprietary and may not be copied, modified, redistributed, sublicensed, or commercially exploited except as permitted by an applicable license agreement. Third-party components are governed by their respective licenses."
  This applies to this version onward. Versions already published under MIT (v0.5.7 to v0.6.2) remain available to anyone who has them under those terms. No code changed; no end-user license agreement exists yet,
  so nobody but the copyright holder has permission to use, copy or distribute this software. Third-party components (FFmpeg, uv, Python packages, models) keep their own licenses (see `MODEL_LICENSES.md` and System & Licenses).

## v0.8.0
Feature release (polish batch). **Tested only with the mock engine, HTTP stub servers, 531 pytest tests and headless Edge scripts on Windows (`tools/e2e/foundation_ui.py`, `polish_*.py`). Nothing in
this release has been run on the Mac, with the real engine, or in real Safari; processing-time estimates were never compared with real timings.** See *Not verified* at the end.

**Live updates and repainting**
- **The live-update storm is fixed.** The SSE state digest (`services.state_digest`) no longer contains the worker heartbeat age or time (they changed every second, so the stream pushed an event
  every second and every page repainted). The digest is now a pure function of stored state; `/api/worker` and the Overview still return the exact heartbeat age, and the top-bar pill
  derives it from `heartbeat_at` when the digest has none. `onState` (`ui.js`) skips a payload equal to the last one it delivered to that subscriber (`sameState`); polled-fallback payloads
  are always delivered.
- **Views repaint live regions only.** New `patchHTML` / `patchRegions` (`ui.js`) repaint a region in place only when its markup changed and keep scroll positions, keyboard focus, text selection
  and `<details id>` open state. Overview, Library, Queue, Book, Disk and Review are now a fixed shell plus live regions (Settings and Narrator Studio keep their forms and previews across redraws instead). Visible results: the Overview activity feed no longer jumps to the top
  (a "N new events - back to top" hint appears instead), "Needs attention" is capped at 5 rows with *Show all (N)* and lists a book once, a Library narrator dropdown keeps its selection and focus.
- **Book page forms are drawn once.** Metadata, cover, release sign-off (approved by, notes, attestation) and delivery-instruction fields are never touched by a live update, so typed values survive; the
  stats, chapters, exports and log regions update in place. A playing chapter mini-player is not re-created. The cover is no longer re-downloaded on every repaint: `GET /api/books/{id}` carries
  `cover_version` (the cover file's mtime) as the cache key.

**Navigation and performance**
- **Router.** Each navigation renders into its own host element with a stale-render guard (a superseded or slow render can never paint over a newer page); the old page stays visible but dimmed and a
  "Loading..." status appears after 120 ms. Views receive `nav` (`nav.signal`, `nav.isCurrent()`) as a fourth argument and fetch in parallel. Focus moves to the new page's `<h1>` and the page scrolls to the top only when
  the route actually changed.
- **`get_book` no longer grows with the number of segments**: chapter readiness, take lookups and release check are batched (one file-existence check per path). 4,000 synthetic segments: 6,523 queries
  down to 30, about 333 ms down to about 104 ms. Equivalence with the old per-segment implementation is tested. `GET /api/books/{id}?view=review` returns a light payload (summary plus chapter
  rows, no readiness, release check, exports, logs or narrative status) for the Review page; the segment-detail chapter timing is looked up with `json_each` instead of decoding the whole map.
- **Disk & Backups paints at once** with "Measuring disk usage..." placeholders and measures in the background. `GET /api/disk/usage` serves a 60 s cache (one measurement at a time, one `lstat` per file) and
  returns `measured_at`, `age_s` and `cached`; `?refresh=1` (the *Refresh* button) forces a new measurement. Delete, purge, backup and restore drop the cache.
- **Schema v6** (migration applied automatically, keeps all data): indexes `ix_segments_updated`, `ix_jobs_updated`, `ix_jobs_book` and the column `jobs.active_seconds REAL NOT NULL DEFAULT 0`.

**Review & QA**
- **Regenerate with direction...** (Review panel): emotion, intensity, pacing and a delivery note, with a read-only **Preview** that explains, per voice kind, what actually reaches the voice
  (`GET /api/segments/{id}/delivery-preview`): preset and designed voices receive a written instruction; a cloned voice cannot take instructions, so only an emotion reference-bank clip (if enabled and present)
  and pace can change the take, and the panel says so. `POST /api/segments/{id}/regenerate` now accepts `direction`.
- **A user direction takes effect even when the book's Director is off and on classic (non-semantic) books** (`plan_for_segment` builds the direction from the user's fields alone); it is always stored as the
  segment's own user direction, visible and clearable in Inspect.
- **Fix: the Director-on cascade now queues the segments it resets.** Setting a direction with the Director on reset dependent segments to pending but did not enqueue them; they are now queued
  (`regenerating` in the reply refers to this segment).
- **Fix (found during integration): the Review list no longer jumps to the selected segment on a live refresh**; it scrolls to the selection only for an explicit pick (click, J/K, deep link).

**Queue**
- **One "recently finished" row per book**, with a "+N earlier runs" disclosure (touch-ups from segment regenerates are folded in; each run shows segments rendered, processing time and "full pass" or "touch-up"),
  a Book column, and a 30-day window (was 7 days).
- **Pagination, 10 per page**, for *Recently finished* and for *Assembly, export, preview & analysis jobs* (`ui.js` `pager` / `wirePager`).

**Processing time** (`studio/runtime.py`, `js/runtime.js`; shown on Overview, Library and Queue)
- Running: "about ... left · done about HH:MM". Queued: "about ... processing · starts about HH:MM". Finished: "processed in ...". Paused jobs show "paused" and do not advance the queue estimate.
- The rate is a running job's own pace once at least 10 of its segments are done, else the median for the book's TTS model over the last 200 segments (at least 20 samples), else the global median, else
  "estimate after N more segments". Director (LLM) seconds are added for undirected pending segments, except for an observed job rate (which already includes them).
- Actual time is `jobs.active_seconds`, accumulated by the worker per step (queued waits, pauses and restarts are not counted; `updated_at` is left alone so it does not wake the live digest). Jobs
  finished before v0.8.0 fall back to `finished_at - started_at`.
- Always labelled and tooltipped as **processing time, not audio length**; the audio length is shown separately.
- `GET /api/overview`, `/api/books`, `/api/books/{id}` and `/api/queue` entries carry a `runtime` object (`state`, `processing_s`, `remaining_s`, `finish_at`, `starts_at`, `total_est_s`, `estimated`, `basis`).

**Settings & Storage**
- **Seven tabs** over one form and one Save: Storage, Production, Quality & verification, Voices & models, Narrative, Local LLM, Server & advanced (`#/settings/<tab>`; arrow keys, Home and End navigate). Unsaved-change dots per tab,
  Discard, a warning when leaving with unsaved edits, and drafts survive a back/forward. **The saved payload is unchanged** (same sections and keys; a hidden tab or dependent field never drops a key).
- **Wide-screen layout**: page content is a centred column capped at 1440 px (Review 1680 px); stat, voice, take and Overview grids cap their column counts. No horizontal scroll at 360 or 768 px on Overview, Library, Queue and Settings (the Book page is the known exception, below).

**Narrator Studio**
- The edit dialog says honestly what can change for each voice kind (built-in presets: delivery instruction only; presets: speaker and instruction; designed: description; clones: name only), the version
  number a save will create, and which books still use an older version (`books_on_older_version` now carries `voice_version`).
- `voices.description` stays in sync for designed voices when their description is edited.
- **Duplicate & retune...** (`POST /api/voices/{id}/duplicate`) copies a preset or designed voice (built-ins included) into a new editable voice with different delivery; **Retune from '<source>'...**
  (`POST /api/voices/{id}/retune`) makes a designed copy of the voice a promoted clone came from. Voices carry `derived_from` (lineage, stored in the new voice's consent JSON) and the card shows it; clones carry `source_voice`.
- `GET /api/voices` runs a constant number of queries however many voices exist. Preview form text survives redraws and a trip to the Record tab.

**API and schema summary**
- Schema v6: see above. No setting changed.
- `GET /api/queue?finished_offset&finished_limit&utility_offset&utility_limit` (limits 1-50, default 10); the reply gains `active`, `finished` (one entry per book with `latest_run`, `earlier_runs`), `finished_total`,
  `utility_total`, the offsets/limits and `window_days`; `jobs` (active plus finished entries) and `throughput` stay.
- `POST /api/segments/{id}/regenerate` accepts `direction {emotion, intensity, pacing, tension, delivery}` and returns `delivery` (what the voice will be given).
- `GET /api/segments/{id}/delivery-preview?emotion&intensity&pacing&tension&delivery` (absent = stored value, empty = cleared; nothing is saved).
- `GET /api/disk/usage?refresh=1`; `GET /api/books/{id}?view=review`; `POST /api/voices/{id}/duplicate`; `POST /api/voices/{id}/retune`. See doc 3.

**Housekeeping**
- `test_background_download_endpoint_flow_and_busy_guard` (update flow) no longer fails on a loaded machine: it waits up to 20 s and reports the real state and error.
- 80 new pytest tests (451 to 531): `test_live_foundation`, `test_book_perf`, `test_disk_cache`, `test_runtime`, `test_overview_runtime`, `test_regen_direction`, `test_voice_retune`, `test_settings_ui`; three node unit suites
  in `tests/js/` run through pytest wrappers. New browser scripts in `tools/e2e/`: `foundation_ui.py`, `polish_book.py`, `polish_overview.py`, `polish_queue.py`, `polish_review.py`, `polish_settings.py`, `polish_voices.py`.

**Not verified**
- Everything ran on Windows against the mock engine (tones), HTTP stub servers and headless Edge. **Nothing of v0.8.0 ran on the Mac, with the real engine, or in real Safari.**
- Browser scripts (headless Edge, mock engine): `foundation_ui.py` 16 checks, `polish_book.py` 29, `polish_overview.py` 27, `polish_queue.py` 27, `polish_review.py` 20, `polish_settings.py` 85, `polish_voices.py` 24, plus the earlier
  `versions_ui.py` 29 and `delivery_ui.py` 13.
- Processing-time estimates were never compared with real timings (mock timings are not representative); their accuracy on the M3 Max is unknown. The query-count and timing figures for `get_book` are from synthetic
  data on a Windows machine.
- The Settings folder browser and Finder picker, and the API key *Save* / *Test connection* clicks, were not exercised in a browser. Queue drag-to-reorder was not browser-tested.
- Whether "Regenerate with direction" changes how a real model sounds is unmeasured: the explanation describes what is sent, not what is heard.
- **Known, unfixed:** the Book page at 360 px width has a horizontal overflow from a `nowrap` table in `book.js`.
- Schema v6 was migrated on mock databases only, not on the Mac's real library (a pre-migration backup is written automatically).

## v0.7.0
Feature release. **Tested only with the mock engine, stub LLM servers (HTTP stubs, no real model) and, for narrator versions and the delete/import/review fixes, a browser
run in Edge (`tools/e2e/versions_ui.py`, mock engine). Nothing in this release has been run on the Mac, with a real LLM, with the real Qwen3-TTS/Whisper models, or in
real Safari.** Versions 0.6.3 and 0.6.4 were never published separately; their fixes are included here (see *Fixes*).

**Narrator versions & library**
- **Narrate the same manuscript with another voice without losing the earlier pass.** On a book page, *Narrate with another voice* creates a new version
  (same text, chapters, characters, speaker attribution, Director guidance, text edits and pronunciations; every segment pending; the existing narration is untouched, which
  a test verifies byte for byte). Each version has its own audio, review, exports and release approval. Versions of one manuscript share a new `books.work_uid`
  (schema v4; every existing book is its own work). The old in-place *Apply narrator change* is kept, tucked under *Replace this version's narrator instead*.
- **Names carry the narrator**: the queue, review picker, review header, overview, disk page, GPU-worker status line and downloaded/exported file names show `Title - Narrator`.
- **Library** groups versions of one manuscript into one row with a narrator dropdown (shown when more than one narrator has worked on the book). The Book page has a narrator-version switcher.
- **Delete (administrators only, typed confirmation, audited):** delete one narrator version, or *Remove book and all versions* (all-or-nothing: refused while any version is
  queued, rendering or paused). Both show the space that will be freed and the number of other versions that stay.
- Duplicate-upload warning now points at *Narrate with another voice*.

**Delivery that reaches clone voices**
- **Why.** The Qwen3-TTS Base (clone) model has no instruction input, so the Director's wording never reached it: on the owner's test book all 370 takes of the clone narrator
  had `delivery_applied = 0`. Two routes now exist for clones, both off by default so existing takes never change silently.
- **Emotion reference bank** (Settings > Narrative > Delivery > *Cloned voices*, `delivery.clone = reference_bank`). A cloned voice can hold up to four short clips of the
  same voice in other registers (tense, tender, sorrowful, bright). A segment whose direction maps to a bucket with an adopted clip, at or above `delivery.bank_min_intensity`
  (default 3), is cloned from that clip instead of the base reference; the clip's SHA joins the take's generation key and `attempts.ref_id_used` records it. Voices panel
  (`voices.js`): per-bucket clip, audition, remove (admin only), *Generate candidates* (clones made from a designed voice: the designed voice speaks the bucket's lab script
  in that register, three candidates, saved as drafts in the reference lab) and *Adopt a lab recording*. Adoption rules: Whisper check required and at most 8% word error,
  not rated poor (recordings), speech level matched to the base clip (RMS), pitch and brightness drift from the base clip recorded and shown. Consent: a clip for a clone of a
  human voice needs the owner attestation and a statement; a clone promoted from a designed voice accepts only clips generated by that same designed voice (promote now records
  `source_voice_id`). Mock-engine clips are marked "mock test tones".
- **Pace and pauses from the direction** (`delivery.pace_from_direction` off | auto | always; `delivery.pauses_from_direction`). The Director's pacing words become small
  pitch-preserving stretch factors on top of the voice's own pace (slow 0.92, measured 0.96, normal 1.0, brisk 1.04, fast 1.08; total limited to 0.8-1.25); `auto` applies
  it only when no instruction reaches the model (clones, 0.6B presets, delivery off). A direction's pause hint (`none | beat | long`) raises the gap after a sentence or
  paragraph to at least 350 ms / 900 ms at assembly; the hints are part of the chapter's takes digest, so a change marks masters outdated. The pace used is recorded per
  attempt (`attempts.pace_used`) and is part of the generation key. Segment previews now apply the same stretch as production takes. Fix: the calibrated words-per-minute
  estimate counted the stretch twice.
- **Optional LLM translator for delivery instructions** (`delivery.translator = template | llm`, model `llm.translator_model`; default template). A small model writes the
  instruction instead of the built-in template (quotes and copied manuscript words removed, length capped). It never runs while a plan is built: `analysis.translate_pending`
  asks ahead of time and stores answers in the Director cache (`tr:<hash>`, keyed on the direction and voice, not on the text); `plan_for_segment` only reads the cache and falls
  back to the template until an answer exists. Three consecutive failures stop a run; the worker backs off for two minutes.

**Director redesign** (applies when an LLM is configured; the rules engine is unchanged)
- **Chapter beat sheet** (`narrative/beats.py`, `narrative.director_chapter_plan`, default on): one thinking-mode request per chapter (no JSON mode, own model, token budget
  and timeout: `llm.chapter_model`, `chapter_max_tokens` 16000, `chapter_timeout_s` 900) returns scenes and beats (emotion, intensity, pacing, tension, purpose, pause) over
  numbered paragraphs `[P1]...`. It is validated to cover every paragraph exactly once, cached in `director_cache` as `beats:<hash>` (chapter text, cast, facts, model), and a failure is
  remembered (`beats-fail:`) for 20 minutes instead of being retried per window. The plan is advice: a failed plan means the scene pass runs without it.
- **Scene-batched direction** (`director.direct_scene`, prompt `scene-1`): one request directs up to 16 consecutive segments of one scene in a compact format, validated item by
  item; failed items are asked again once, still-failing items fall back to the rules engine with `needs_review`. The planned beat is the baseline; a planned turn in
  emotion family is not smoothed flat. Optional `llm.scene_model`. Cached per window.
- `analysis.director_batch` rewritten around the above; new `analysis.direct_scope` is what the worker calls before narrating a segment, so a segment picked out of order
  (model-first scheduling) is directed too (it used to be skipped when the look-ahead directed other segments). Counters in the Director stage stats: `chapter_plans`,
  `chapter_plan_failures`, `scene_calls`, `reasoning_chars`, `would_escalate` (segments a stronger model could have been asked about; logged only), translator counters.
- `LLMClient.derive` (another model, thinking on, bigger budget; shares the transport and the server-quirk memory) and `resolve_models`.

**A/B & apply**
- **Blind A/B listening** (`narrative/ab.py`). Segment Inspector > *Compare settings (blind A/B)*: pick one of four comparisons (Director's delivery vs plain reading; LLM vs
  template wording; reference bank vs base clip; pace from direction vs steady), render the segment twice with the same seed, hear clips X and Y in random order, vote
  (X, Y, no real difference, plus "sounds like a different narrator"), then see which setting was which. The settings of each side are explicit patches (only `director_enabled`
  and delivery keys), so a round never depends on what the book has switched on and never touches its takes. *Pick a segment where they differ* finds one whose two plans
  differ. The Narrative page lists the tally per comparison with the guidance to adopt a feature at 60% or more of the non-tie votes with "another narrator" flags at 10% or
  less. Votes are per book (`ab_votes`), not copied to new versions. Rounds from the mock engine are labelled as tones, not speech.
- **Apply new delivery settings to existing takes** (Narrative page): lists how many rendered segments would sound different under the settings now in force
  (`invalidate_changed(dry_run=True)`), asks for confirmation, then resets only those (approved segments only if you tick the box), revokes release approval and queues the
  book first. Old takes stay as history.

**Fixes**
- **"GPU worker offline" loop whenever the Narrative Director used a local LLM** (found on the Mac with `director_enabled` and look-ahead 12; was 0.6.3). The Director held one
  database write transaction open across all of its LLM requests (minutes). The worker heartbeat could not write (`database is locked` after the 30 s busy
  timeout), the lease went stale after 60 s, the web server sent the worker SIGTERM and started a new one, which resumed the same job and repeated, so nothing
  ever rendered. The Director now reads without a lock and takes the write lock only for each cache lookup or result, never across an LLM request (the new chapter, scene and
  translator passes follow the same rule). A regression test fails if a write lock is held during an LLM request.
- **Truncated / empty Director replies from thinking models** (found on the Mac with `gemma-4-26B-A4B-it-oQ4e-mtp` on oMLX; was 0.6.4). Measured against the real server: with JSON
  mode on, the model degenerated into `{"segment_` and ran to `max_tokens`; with it off, it reasoned into a separate `reasoning_content` field until the budget ran out and `content`
  was empty. Requests now send `chat_template_kwargs: {enable_thinking: false}` (the same prompt then answered in about 3 s with valid JSON; the chapter pass sets it to true and
  never uses JSON mode). A server that answers 400 to an optional field has `response_format`, then that field, dropped and remembered. An empty answer says whether the model only
  reasoned, a reply cut off at `max_tokens` is logged (and is an error in thinking mode), an inline `<think>` block is stripped, and **Test connection** fails (instead of "Replied: (empty)")
  for a model that only thinks. Test connection also checks that the chapter, scene and translator models, when they differ, are listed by the server.
- Deleting a book that had pronunciation entries failed (the v0.6.0 delete did not remove them). Covered by a test.
- **Review list no longer jumps to the top while a job runs.** Live updates refresh the segment list in place: the scroll position, the segments you loaded with *Load more* and
  the selected segment's panel (only when it changed) are kept.
- **Import form.** The narrator dropdown no longer silently defaults to the first voice (Aiden): it starts on "Choose a narrator..." and import is refused until one is chosen. Values
  typed or chosen in the import form (and the new-version form) survive the page redrawing itself (uploading another file, queueing a book, live updates); before, a chosen narrator could snap back.

**Schema and settings**
- **Schema v5** (migration applied automatically, keeps all data): tables `voice_refs` (one adopted clip per clone voice and bucket) and `ab_votes`; columns `attempts.pace_used` and
  `attempts.ref_id_used`. (v4, earlier in this release: `books.work_uid`.) Disk usage counts bank clips; deleting a book removes its votes.
- New settings, all defaulting to the old behaviour: `narrative.llm.chapter_model`, `scene_model`, `translator_model` (blank = main model, blank translator = none), `chapter_max_tokens`,
  `chapter_timeout_s`; `narrative.delivery.clone` (`reference_bank | off`, default off), `pace_from_direction` (`off | auto | always`, default off), `pauses_from_direction` (default off),
  `translator` (`template | llm`, default template), `bank_min_intensity` (default 3); `narrative.director_chapter_plan` (default on, matters only with an LLM).
- API: `/api/voices/{id}/refs...`, `/media/voice/{id}/ref/{ref_id}`, `/api/segments/{id}/ab`, `/api/ab/{round}`, `/api/books/{id}/ab`, `/api/books/{id}/ab/random`,
  `/api/books/{id}/narrative/apply-delivery`; `POST /api/narrative/llm/test` accepts `chapter_model`, `scene_model`, `translator_model`. See doc 3.

**Not verified**
- Everything above ran against the mock engine (deterministic tones) and stub LLM servers only. No real Qwen3-TTS or Whisper run, no real LLM, nothing on the Mac.
- Whether the reference bank, pace factors, pause hints, the beat sheet, scene batching or the LLM translator make narration sound better is **unmeasured**: nobody has listened. The A/B tool
  exists to find out, and its ratings from the mock engine mean nothing.
- Thinking-mode behaviour of the chapter pass (token budget, time per chapter, oMLX quirks) was not run against a real thinking model; the 0.6.3/0.6.4 findings come from the earlier
  Mac session, the fixes were verified against stubs.
- UI: the narrator-versions, import-form, review-scroll and delete changes (`tools/e2e/versions_ui.py`, 27 checks) and the Voices reference-bank panel, Inspector blind A/B card (pair, two unlabelled clips, vote, reveal)
  and Narrative A/B results / *Apply new delivery settings* (`tools/e2e/delivery_ui.py`, 13 checks) were driven in Edge against the mock engine (tones, not speech). The bank *generate / adopt* flow and the apply-delivery confirmation dialog
  were not exercised end to end in a browser. Real Safari is untested.
- Pace stretch (WSOLA) and pause hints were not audited on real speech. Escalation to a stronger model is only counted (`would_escalate`), not implemented.
- Schema v5 migration was tested on mock databases, not on the Mac's real library.

## v0.6.2
- GitHub release notes are now this version's section of this changelog (`tools/release_notes.py`), instead of an automatic commit list. The Release workflow
  refuses to publish a version that has no changelog section, and re-applies the changelog text to the notes of all earlier releases (text only; assets and the
  signed index are untouched). A test requires a section for the current version.

## v0.6.1
- Fix: the installer's web-address summary (v0.5.9) failed the CI `shellcheck` step (SC2086), so the v0.5.9 and v0.6.0 releases were never built or published.
  The summary now uses `read`. v0.6.1 is the first published build containing everything from v0.5.9 and v0.6.0.

## v0.6.0
First feature release after v0.5 (built by six parallel work streams; mock engine only, nothing in this release has been run on the Mac).
- Settings > Narrative > Local LLM: **Test connection** button. Checks the address and model name typed in the form (saved or not): reachable, key accepted,
  model listed, one tiny question answered. Touches no book.
- Pronunciation dictionary, global and per book: respellings change only the text sent to the voice, never the manuscript. A Pronunciation page adds entries
  and has a Try box. Changing an entry re-renders only the passages that contain it, after you confirm. Per-voice pace (0.8-1.25) applied as a pitch-preserving
  time stretch, because the speech model has no pace control.
- Disk & Backups (admin): per-book and per-category usage with free space; purge superseded takes, exports or masters (selected and approved takes always kept) and
  delete a book, with explicit confirmation, audited, refused while queued or rendering; single-zip book backup and restore-as-new-book; verified SQLite backup.
- Per-chapter tools: regenerate a whole chapter (confirmation; revokes release approval) and edit chapter titles (parsed title preserved and restorable; the
  chapter master is marked outdated until re-assembled). Single-chapter M4B export and cover upload are documented and covered by tests; generated placeholder
  covers are not included (Pillow is not a dependency).
- Reference lab: compare several takes side by side (verdict, length, peak/clipping, noise, voice-to-noise, Whisper match, audition) with an advisory suggestion;
  pick the one to use. `GET /api/recordings/compare`.
- Audit and review records carry the signed-in user's name instead of "operator" (work done by the background worker stays "system"); the QA report lists each
  review action's actor.
- Manual rollback: `audiobook-studio update --rollback` and a "Roll back to previous version" button in Updates (admin only, confirmation, refuses if the
  previous release cannot open the current database schema). After each install or rollback the updater deletes tool archives in `app/downloads`, prunes unused
  runtimes and empty staging folders.
- `tools/scrub_check.py` (private IPs, `.lan`/`.local` hosts, home paths, PEM private keys) runs in CI.
- Container image definition (`Dockerfile`, `docs/12-container.md`): mock/CPU only, tag-based upgrades; written, NOT built, NOT tested.
- Database schema v3 (`pronunciations`, `voices.pace`, `chapters.original_title`); the migration is applied automatically and keeps all data.

## v0.5.9
- The installer's final summary now lists every web address: HTTP and HTTPS on this Mac, and on the network by IP address and `.local` name, read from the saved settings.

## v0.5.8
- First sign-in without leaving the web console: a fresh managed install creates `admin` with the starter password `admin`, flagged "must change". The sign-in
  leads straight to a "Choose a new password" page; every other API call is refused (403, `must_change_password`) until it is done. The random password file
  `initial-admin-password.txt` is no longer written. Existing accounts are unaffected. `audiobook-studio auth add-user NAME --temporary`.
- Note: until the password is changed, anyone who can reach the Mac on the network can sign in as `admin`; the installer says so.

## v0.5.7
- Added the MIT `LICENSE` (copyright notice must be kept: others may use it if properly credited).
- A fresh managed install now serves the LAN by default (HTTP and HTTPS, sign-in required); `--local-only` restricts it to this Mac. Existing
  installs and `--migrate-from` keep their current network setting. `audiobook-studio configure --lan | --local-only`.

## v0.5.6
- Documentation only: dropped the GitHub branch/tag protection setup (roadmap B3) as unnecessary for this tool. The automatic workflow guard stays; no owner-side configuration is required.

## v0.5.5
- Branch and channel policy adopted (docs/11-roadmap.md section 1, owner-approved): `main` = stable (`vX.Y.Z` tags), `dev` = beta (`vX.Y.Z-beta.N` tags),
  day-to-day work on `dev`, promotion to stable by fast-forwarding `main`.
- Release workflow guard: a plain `vX.Y.Z` tag is refused unless its commit is already on `origin/main` (beta tags: `origin/dev` or `origin/main`), and a
  release that already exists is never moved or re-created.
- Version scheme renamed (numbering unchanged): X = Major (owner only), Y = Feature, Z = Minor/Patch (bug fix, feature alteration, docs, housekeeping).
  Docs, CLAUDE.md and the module docstring updated.

## v0.5.4
- Documentation only: `docs/11-roadmap.md` (was a draft for owner review: branch/tag/channel policy, remaining roadmap, open owner decisions). Not yet tagged or published.

## v0.5.3
- First release delivered through the real update path (GitHub Releases, signed index, in-app Update Center, new private Python environment built by
  the updater). No functional change to narration, QA, review or export. Repository history was scrubbed of local network details before this release;
  the screenshots were removed and the lock tool no longer writes the developer's temp path into `requirements.lock`.

## v0.5.2
- Fixes found by the first real run on the M3 Max (macOS 27; results in doc 10):
  - Model cache: `HF_HOME` is now set by the launcher (and the LaunchAgent runs the launcher) before Python starts, instead of being changed in-process after
    `huggingface_hub` was imported (that produced "Tokenizer not loaded"). `Context.apply_model_cache_env` only acts if the library is not imported yet.
  - A supervised worker exits (after its current segment) when its server disappears, so `kill -9` of the server no longer leaves a stale worker holding the GPU lease.
  - `requirements.lock` pins `transformers 5.21.0`, `huggingface-hub 2.3.0`, `pydantic 2.14.1` (the combination verified on the Mac).
  - Re-running an installer build of the same version re-extracts the files (compares the build id).
- The stable interpreter path is now the directory link `app/venv` (a link to the interpreter file alone ran without the environment).
- Installer: the version carried a `v` prefix that the release store rejected; the bare version is used for release folders.

## v0.5.1
- **Production release-signing key pinned** (id `23bb536587702e29`, public half in `studio/update/pinned_keys.json`). The private key is held outside the
  repository by the owner's assistant session and must be set as the CI secret `RELEASE_SIGNING_KEY`; no release has been signed yet. The updater is
  now enabled and will accept only indexes signed by that key. Tests that assumed "no key" now use an empty trust store explicitly.

## v0.5.0
Installation, launch and updates. Nothing about narration, QA, review or export changed; the data model is unchanged (schema v2).
**Verified on the production M3 Max in v0.5.2** (`docs/10-installation-and-updates.md`, "Results of the first hardware run"); no release has been published or signed on GitHub yet.
- **No-prerequisites installer** `install_audiobook_studio_mac.sh`: needs no Homebrew, Git, Python, pip, FFmpeg, Node, Xcode or Command Line Tools,
  no sudo, and edits no shell profile or system directory. Installs a private Python 3.12 with the pinned `uv`, FFmpeg/FFprobe 9.0.2 (GPLv3 build,
  downloaded not bundled, AAC and MP3 encoders verified) into the app folder, and hash-locked prebuilt wheels (`requirements.lock`, 88 packages,
  `--require-hashes --only-binary :all:`; a package without a wheel stops the install instead of reaching for a compiler). Every download is size- and
  SHA-256-checked against values pinned in the script before use and fails closed. Pre-flight (macOS 14+, native arm64, RAM, disk per location, mounted
  external disks, writable paths, reachability, free port), `--check`, prompts or flags for library/model-cache location and the optional model
  download, idempotent and resumable, `--uninstall` (data only with a typed confirmation). The old Homebrew installer remains for existing installs.
- **Managed layout**: replaceable releases under `app/releases/<ver>` with an atomic `current` pointer, private runtimes, user data in `state/`, logs in
  `~/Library/Logs/Audiobook Studio`, model cache and library anywhere you choose (`models.cache_dir` setting; applied as `HF_HOME`).
- **Per-user launchd service** (`gui/<uid>`, no root): starts at login, restarts after failures only, 10 s throttle, 127.0.0.1 by default, port
  collision and double-run detection, `GET /healthz` and `GET /api/version`. CLI: `doctor start stop restart status logs version update service migrate-legacy configure`.
- **Update Center** (sidebar Updates, nav dot, Overview notice): check, release notes (plain text), download and verify, stage, **Install now** (next
  segment boundary) or **Install after current book**, cancel, restart service, automatic checks (daily by default; never auto-download or install),
  eight progress phases, history, clear states for offline / incompatible / failed / rolled back / switched off. Admin-only, CSRF/Host/same-origin protected;
  no endpoint accepts a URL or command.
- **Signed releases**: Ed25519-signed per-channel release index (version, channel, platform, arch, URL, size, SHA-256, published, notes URL, min compatible
  version/OS, schema, runtime) with sequence number (anti-rollback), expiry, revoked versions and keys; public key pinned in the app
  (`pinned_keys.json`, **empty until the owner generates a key**, which keeps the updater off). HTTPS only, host allowlist re-checked on every redirect,
  size limits, resumable download, hardened archive extraction (traversal, links, bombs, per-file manifest). GitHub Releases first; `HttpsRepository` implements the
  same contract for a future first-party host.
- **Separate updater process** (one-shot launchd job or `update --install`): drain the queue at a segment/job boundary, consistent database backup, atomic
  switch, `studio migrate` from the new release, restart, health check, automatic rollback (database restored only if the schema moved), repair note as a
  last resort; user data is never deleted.
- **Migration** from the Bash/Homebrew install (`--migrate-from` / `studio migrate-legacy`): backup, copy, library and model cache kept in place, new backend
  tested on a spare port first, only then the old LaunchAgent is replaced (plist saved); the old folder is never modified; network binding off unless `--keep-network`.
- **Boundaries**: `studio/interfaces.py` (PlatformService, InferenceProvider, TTSProvider, ASRProvider, ModelRegistry, ReleaseRepository, UpdateManager,
  StorageManager, QueueScheduler) with launchd, process, MLX/mock, settings and GitHub/HTTPS implementations. Windows/Linux/containers/remote providers are designed for, not built.
- **Tooling and CI**: `tools/build_release.py`, `sign_release.py` (refuses without a key), `verify_index.py`, `release_keys.py`, `lock_requirements.py`,
  `check_lock_wheels.py`, `tools/e2e/updates_ui.py`; GitHub Actions `ci.yml` and `release.yml` (signs only if `RELEASE_SIGNING_KEY` exists, verifies the index
  against the pinned key, otherwise publishes an UNSIGNED prerelease and no index).
- FFmpeg lookup now prefers the app-owned copy; error messages no longer suggest Homebrew. 80 new tests (242 total).

## v0.4.0
- **API keys can be entered in the web UI.** Settings > Narrative > Local LLM has an API key field with Save key / Remove key, so an
  OpenAI-compatible endpoint no longer needs an environment variable on the Mac. The key is write-only: it is stored in
  `config/secrets.json` (mode 0600), never in `settings.json`, the database, the audit or event logs, and no API response contains it;
  the UI only shows whether a key is saved and when. Saving or removing a key is admin-only, takes effect immediately for the worker,
  and is audited without the value. A saved key wins over the old `api_key_env` environment variable, which still works as a fallback.
  Keys are kept in named slots (`studio/secrets_store.py`), so any further OpenAI-compatible endpoint adds a slot, not a new mechanism.
  Endpoints: `PUT /api/secrets/{slot}` `{"value": ...}`, `DELETE /api/secrets/{slot}`; `GET /api/settings` reports `secrets` status.

## v0.3.0
Context-aware multi-character narration. Everything below is opt-in (Settings > Narrative); with it off, books import and
render exactly as in v0.2.2.
- **Two-pass semantic segmentation.** Pass 1 parses each chapter into paragraphs, scenes and dialogue/narration spans with exact
  character offsets, attributes quoted speech from tags, beats, pronouns plus gender, quote continuation and turn-taking (it never
  assumes "the last speaker"; uncertain lines are flagged for review), tracks characters and aliases across chapters, and records
  emotional cues and tension. Pass 2 builds the narration segments (default ceiling 500 characters, never filled for its own sake),
  splitting at speaker changes (when voices differ), scene breaks and meaningful emotional turns, joining tiny fragments, and
  splitting over-long sentences at the least disruptive punctuation. Segments are exact slices of the chapter text.
- **Dual context windows.** The spoken text is only the segment's words. A separate narrative context window (default 1,250
  characters before and after, set independently) feeds analysis only and cannot reach the TTS engine.
- **Narrative Director** with a controlled emotion vocabulary, schema-validated and auditable output (facts are verified against the
  text and dropped if ungrounded; interpretations are kept separate), a configurable OpenAI-compatible local LLM, and a deterministic
  rules engine that is used when no LLM is configured or when it fails. Results are cached by text, context, profile, previous
  delivery, model and prompt version.
- **Cross-segment continuity.** Per-speaker delivery state with bounded intensity steps, decay instead of snapping back to neutral,
  escalation, scene/chapter resets, and no smoothing of changes the manuscript supports. Edits recompute downstream directions
  only until they stabilise (bounded cascade), without new LLM calls.
- **Characters and story notes** per manuscript: aliases, merge, voice assignment, accents and personality with provenance and
  confidence; user-confirmed values always win. Extractive chapter/scene summaries (verbatim sentences) so summaries cannot invent events.
- **Model-aware delivery**: CustomVoice gets an `instruct` style instruction; VoiceDesign gets the delivery appended to the voice
  description; the Base cloning model receives no instruction (the library has no such input) and keeps its reference conditioning.
- **Segment Inspector, character directory, boundary editing, per-segment preview** in the web UI; project-level overrides of every
  narrative setting; a Narrative analysis job in the queue with pause, cancel and restart recovery.
- **Targeted regeneration and audio cache.** Every take records a generation key (text, voice, delivery, model parameters). A voice or
  delivery change resets only the affected segments, and reverting it reuses the earlier take.
- **Whisper verification modes**: every segment (default), a deterministic sample, or off.
- Database migration v2 (a backup is taken first); installer rebuilt. New tests: `tests/test_narrative.py`.

## v0.2.2
- Documentation set for new sessions: `docs/` (overview, architecture, API, frontend, development workflow, deployment and
  operations, testing and baseline, decisions/gotchas/backlog) and a root `CLAUDE.md`.
- The end-to-end verification scripts moved into the repo as `tools/e2e/` (parameterised, with a fixtures generator).
- `.venv/` ignored by git. No application behaviour changed.

## v0.2.1
- The Reference voice lab moved into the Narrator Studio "Add a narrator" card as the **Record a reference voice** tab.
- Leaving that tab now turns the microphone off, and the lab screen shows it as off when you return.
- Versioning changed to MAJOR.MINOR.PATCH; the "prerelease" label was dropped.

## v0.2
- Reference voice lab: record a reference voice in the browser (guided setup, room check, sample scripts,
  quality analysis, Whisper transcript check, audition, create narrator). It now lives in the
  Narrator Studio "Add a narrator" card, as the **Record a reference voice** tab.
- HTTPS (self-signed) on port 8443 with `--lan`, required for microphone access from other computers.
- Folder browser for storage locations (admin), `studio auth remove-user`, password confirmation,
  About page, short-segment WER false-positive fix.

## v0.1
- First prerelease: manuscript import, durable queue, Qwen3-TTS narration with Whisper QA, review workspace,
  chapter-marked M4B export, release signoff, LAN access with authentication, background service.
