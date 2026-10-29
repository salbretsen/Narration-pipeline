# 9. The narrative pipeline (v0.3): semantic segmentation, Narrative Director, characters

Opt-in layer (Settings > Narrative). With `narrative.semantic_segmentation` off nothing below runs and books behave exactly as
before (legacy chunker, one narrator). Code: `src/app/studio/narrative/`; API: `studio/web/narrative_api.py`; UI:
`js/views/narrative.js`, `inspector.js`, `narrative-fields.js`. Tests: `tests/test_narrative.py` + `tests/narrative_fixtures.py`.

## Pipeline and stage status

`import → Pass 1 (structure + speakers) → characters → summaries → Pass 2 (segments) → windows → Director → continuity → synthesis → Whisper → review → assembly → export`

| Stage (`analysis_stages.stage`) | Runs | Notes |
|---|---|---|
| `structure` | at import (in the request) and on "Re-run structure" | deterministic, ~milliseconds per chapter |
| `attribution` | `analyze` job, only if an LLM is configured | resolves flagged spans; skipped (and reported) otherwise |
| `characters` | with structure | cast sync into `characters` |
| `summaries` | `analyze` job | extractive (verbatim sentences + offsets); optional LLM summary stored unconfirmed |
| `segmentation` | at import, after voice/merge/boundary changes, on "Resegment" | reconciles with existing segments (see below) |
| `director` | `analyze` job and inside book jobs (look-ahead) | LLM or rules engine; cached |
| `continuity` | with the Director and after edits | smoothing + bounded downstream recompute |

Status and errors are in `GET /api/books/{id}/narrative`, shown on the book's Narrative page, and as `analyze` jobs (label "Narrative
analysis") in the queue. The worker state shows `analyzing`. An `analyze` job advances one bounded unit per worker step, so
pause/cancel/other jobs interleave; it is resumable (state is in the DB). Book jobs call `director_batch` for the next
`director_lookahead` segments when the next pending segment has no direction, so analysis runs ahead of synthesis without an unbounded queue.

## Pass 1 - `structure.py`

Canonical chapter text = readable blocks (title first if `speak_chapter_titles`), whitespace-normalised, joined by a blank line;
stored in `chapter_text`. **Offsets everywhere are into this text.** Output per chapter: paragraphs, scenes (scene breaks and headings),
spans (`narration | dialogue | heading`, partitioning every non-whitespace character), perspective (first/third), tension profile.

Quotes: straight and curly double quotes; scare-quotes/titles inside a sentence are narration; a quote left open at a paragraph end
continues into the next paragraph if that starts with a quote. (Single-quote dialogue is not detected.)

Speaker attribution evidence, in order, with the confidence assigned:
1. speech-verb tag after/before the quote, subject a name (0.95/0.90), `I` in a first-person text (0.90 -> Narrator);
2. action beat naming a known character in the adjacent sentence (0.80);
3. pronoun tag/beat: only if exactly one participant of the scene has the pronoun's gender (0.72; beat 0.62), else flagged with candidates;
4. same speech continued after a tag or across paragraphs (0.95 x the source's confidence);
5. turn-taking: the speaker before last, if the last two attributed turns in the scene are two different speakers, within 2 paragraphs
   (0.85 x the weaker of the two, max 0.80, so about two inferred turns in a row clear the default 0.6 threshold).

No evidence -> **unattributed and flagged** (`needs_review`), never "the last speaker". Below `attribution_threshold` -> flagged with the
guess kept as a candidate. A flagged dialogue segment is voiced by the narrator until resolved. `analyze_chapter` runs the chapter twice:
the first run discovers names, learns surnames (`Elena` -> `Elena Marsh`) and genders (titles, or pronouns sharing a sentence with exactly one
character); the second attributes every quote with that knowledge. Aliases resolve by whole name, then by a token that identifies exactly one
character; an ambiguous token (two Marshes) resolves to nobody. Characters in the DB are passed as `known` so analysis never renames or duplicates them.
User-confirmed attributions (`nar_spans.user_locked`) are re-applied by offset after any re-analysis.

Cues (`vocab.py`): lexical emotion stems, speech verbs/adverbs, `!`, ellipses; per span `{emotion, intensity 1-5, pace, tension, explicit, evidence, shift}`.
`shift` = size of the emotional change from the previous span in the same scene. These are heuristics; they feed the rules Director and the
decision to split at an emotional turn.

## Pass 2 - `segmenter.py`

Atoms = spans (long ones split into sentences, over-long sentences at `;:—`, then `, and/but/which...`, then any comma, then the word nearest
the middle, preferring splits where neither side is a tiny orphan). Merge rules (`max_segment_chars` is a ceiling, default 500; `min_segment_chars`
default 120):
- never across a heading, a scene break, a manual cut, a different voice (`character.voice_id`; characters without their own voice share the narrator's),
  or two different/unknown dialogue speakers; dialogue and narration only mix inside one paragraph;
- an emotional turn (`shift >= 1 - emotion_sensitivity`) starts a new segment (not for fragments under ~25 characters);
- a new paragraph starts a new segment once the current one has reached `min_segment_chars`; tiny fragments are folded into a neighbour when nothing separates them;
- optional `tags_with_speaker`: a short tag ("she said.") is read by the speaker's voice instead of the narrator's.
Every segment is an exact slice `chapter_text[src_start:src_end]` (`G.check_coverage` asserts order, exact slices, no overlap, no gap; the importer raises if it fails).

**Reconciliation** (`store.resegment_chapter`): new segments whose `(src_start, src_end)` equal an existing one keep their row, `sid`, takes, direction and
review history. Others are inserted with new `sid`s (numbers never reused; `segment_history` keeps retired ones with text and reason). Takes of retired
segments stay on disk. This is why a voice assignment, a merge or a boundary edit changes only the affected segments.

Manual edits (`seg_edits`): `split` (cut at a word), `merge_next` (join), `move_end` (cut + join). Cuts split atoms; joins override the ceiling up to 1200 characters.

## Windows - `windows.py`

Narration window = `" ".join(segment.split())`, the only string the engine receives (`inspect.delivery.spoken_text_sent_to_tts`, asserted equal in tests).
Context window = text before/after (`context_before_chars` / `context_after_chars`, default 1250 each, independent), clipped to the chapter, snapped to
sentence starts/ends, with `[scene break]` markers. It is used only in Director/attribution prompts. The instruction string is checked against the surrounding
text (`director.leak_guard`: 6+ consecutive copied words are removed).

## Narrative Director - `director.py`

Controlled vocabulary: emotions `neutral calm joy excitement tenderness sadness grief fear anxiety anger surprise disgust determination humor resignation`;
pacing `slow measured normal brisk fast`; intensity and tension 1-5. Output (validated; unknown vocabulary rejected after one repair round):
`segment_id, speaker, voice_profile, emotion, intensity, delivery (free text, max 240), pacing, tension, confidence{speaker,emotion,overall}, context_refs,
facts (verbatim quotes, checked against the text and context, ungrounded ones dropped), inferences, ambiguity, needs_review, text_supported_change`.
The Director cannot change the attribution (a disagreement sets `needs_review`) or the text.

Engines: **llm** (OpenAI-compatible `/chat/completions`; prompt in `director.SYSTEM_PROMPT`/`build_prompt`, inputs: segment, context before/after, chapter/scene
summaries, confirmed story facts, the character profile with user-confirmed fields separated from inferred ones, previous segment's delivery, the speaker's state,
the next segment's cue, user hints, sensitivity) and **rules** (cue-based, sensitivity-scaled; the default when no LLM is configured and the fallback on LLM failure,
in which case the record is marked `needs_review`; three consecutive LLM failures stop calling it for the batch). Cache `director_cache` key =
prompt version + model + text + both contexts + notes + profile + previous delivery bucket + speaker state + upcoming + hints + sensitivity.

Raw output, smoothed output, user overrides and the final result are stored separately in `directions` (+ `direction_log`); user values always win.

## Continuity - `continuity.py`

State per speaker (emotion, intensity, pacing, tension) in `directions.state_json`. `continuity_smoothing` (strength 0..1): bounded intensity step (`round(4-3s)`),
unsupported fall to neutral decays by one instead of snapping back, rising tension keeps intensity climbing, pacing moves one notch at a time, **changes the text
supports (`text_supported_change`/explicit cues) are kept** and recorded as `intentional`. `reset_scope`: `none | scene | chapter` (default chapter; scene breaks decay
intensity by one). Editing a segment recomputes downstream from stored raw directions (no new LLM calls) until a segment's result and state are unchanged, at most
`cascade_limit` segments (the next is then marked `stale`); `passed`/`flagged` segments whose delivery changed go back to pending, `approved` ones are not touched
by a cascade (a direct edit of that segment does reset it).

## Characters and story notes - `cast.py`

`characters` (name, aliases, gender, voice, accent, speech pattern, personality, description, relationships, delivery preference, `prov_json` per field:
`{source: rules|llm|user, confidence, confirmed}`), `story_notes` (summaries/facts with source and confirmed flag). Fields set by the user are confirmed and never
overwritten by analysis. Merge re-points spans, segments and notes and keeps the old name as an alias (the source is archived, not deleted). The narrator is a character
that always uses the book's narrator voice. Summaries are extractive; an LLM summary is stored unconfirmed and rejected if it names anything absent from the chapter.

## What each Qwen3-TTS model receives (verified against mlx-audio 0.6.3 source)

| Voice kind / model | Call | Delivery guidance | Consistency note |
|---|---|---|---|
| preset / CustomVoice 1.7B | `generate_custom_voice(text, speaker, language, instruct)` | `instruct` = "Speak <adv> <emotion adjective>, <pace>. <delivery>" (<= `delivery.max_instruct_chars`) | stable speaker identity |
| preset / CustomVoice 0.6B | same | **ignored by the library** (reported as "not applied") | |
| design / VoiceDesign | `generate_voice_design(text, instruct, language)` | `instruct` IS the voice description; delivery is appended ("For this line: ...") | the voice is re-created per call; no persistent embedding; identity can drift |
| clone / Base | `generate(text, ref_audio, ref_text)` | **none** (no instruction input; the cloning path requires `instruct is None`) | reference audio + transcript conditioning only |

Per-model switches: `narrative.delivery.custom_voice (instruct|off)`, `voice_design (append|off)`; the Base row is fixed. `delivery.build_instruct` returns why guidance was
not applied, shown in the inspector and stored on each attempt (`instruct_used`, `delivery_applied`).

## Caching, invalidation, scheduling

- `attempts.gen_key` = hash(spoken text, voice kind/model/speaker/description/reference SHA, final instruction, engine, language, sampling parameters). A voice or delivery
  change resets only segments whose selected take's key differs (`store.invalidate_changed`); `select_take`/`approve` refuse a take with another key.
- Before synthesising, `pipeline.reusable_take` reuses an earlier passing take of the same segment with the same key (e.g. after reverting a voice). A user-requested regenerate never reuses.
- The book's fingerprint is unchanged by character voices; `books.synth_fingerprint` still guards narrator/model changes.
- The worker prefers a pending segment whose model is already loaded (`store.pick_segment`, window 150) because chapters are assembled by index, which avoids reloading a model for every voice switch.
- Inference stays in the worker process (one at a time). The LLM is an external HTTP endpoint; to avoid holding an LLM and TTS models in memory together, run the LLM in a server that
  unloads idle models or accept the shared memory (48 GB unified memory on the target Mac).
- Whisper: `narrative.verify_mode` = `every` (default, the v0.2 behaviour) | `sample` (`verify_sample_rate`, deterministic per segment+round) | `off`. `qa.enable_asr=false` in a book still wins.
  Skipped checks are recorded (`attempts.asr_skipped`) and shown as an info issue. Whisper evidence is about words, not emotion or speaker identity.

## Settings (`narrative`, global defaults; per-book overrides in `books.narrative_json`, `PUT /api/books/{id}/narrative/settings`, admin)

`semantic_segmentation` (stamped into a book at import and not switchable off afterwards), `max_segment_chars 500`, `min_segment_chars 120`, `context_before_chars 1250`,
`context_after_chars 1250`, `attribution_threshold 0.6`, `emotion_sensitivity 0.5`, `auto_character_tracking`, `chapter_summaries`, `tags_with_speaker`, `director_enabled`
(requires semantic segmentation), `director_lookahead 12`, `continuity_smoothing`, `smoothing_strength 0.6`, `reset_scope chapter`, `cascade_limit 24`, `verify_mode`,
`verify_sample_rate 0.25`, `delivery{custom_voice, voice_design, max_instruct_chars 160}`, `llm{provider none|openai_compatible, base_url, model, api_key_env, timeout_s, temperature, max_tokens}`.
The API key is never stored: `api_key_env` names an environment variable that must start with `STUDIO_LLM_` and be set for the service (e.g. in the LaunchAgent environment). Effective
settings = global (live) + the book's overrides; segmentation parameters take effect on the next (re)segmentation.

## Database (migration v2)

New: `chapter_text`, `characters`, `nar_spans`, `story_notes`, `directions`, `direction_log`, `director_cache`, `seg_edits`, `segment_history`, `analysis_stages`.
Columns: `books.narrative_json`; `segments.src_start/src_end/character_id/span_kind/scene_idx/voice_override_id/needs_review/review_reason/boundary_manual`;
`attempts.gen_key/instruct_used/delivery_applied/voice_id_used/asr_skipped`. `jobs` was rebuilt to allow kind `analyze` (one active per book); `migrate()` turns foreign keys off
around it, verifies `foreign_key_check` before each commit, and backs the database up first (`backups/db/`).

## Running a local LLM (optional)

Any OpenAI-compatible server: LM Studio (`http://127.0.0.1:1234/v1`), `mlx_lm.server`, llama.cpp's server, Ollama's `/v1`. Set provider, URL and model in Settings > Narrative (or per
book), restart nothing. **No LLM is installed on the production Mac**; the LLM path is covered by tests against an HTTP stub, not by a real model.

## Known limitations

See `08-decisions-gotchas-backlog.md` ("Narrative layer known limitations") and the measured results in `07-testing-and-baseline.md` ("Baseline results recorded at v0.3.0"). Real-hardware script: `tools/e2e/narrative_real.py`.
