# 9. The narrative pipeline (v0.3): semantic segmentation, Narrative Director, characters

Opt-in layer (Settings > Narrative). With `narrative.semantic_segmentation` off nothing below runs and books behave exactly as
before (legacy chunker, one narrator). Code: `src/app/studio/narrative/`; API: `studio/web/narrative_api.py`; UI:
`js/views/narrative.js`, `inspector.js`, `narrative-fields.js`, `voices.js` (reference bank). Tests: `tests/test_narrative.py` + `tests/narrative_fixtures.py`; v0.7.0: `test_plumbing_v5.py`,
`test_delivery_audio.py`, `test_director_v2.py`, `test_translator.py`, `test_ab.py`.

## Pipeline and stage status

`import → Pass 1 (structure + speakers) → characters → summaries → Pass 2 (segments) → windows → Director → continuity → synthesis → Whisper → review → assembly → export`

| Stage (`analysis_stages.stage`) | Runs | Notes |
|---|---|---|
| `structure` | at import (in the request) and on "Re-run structure" | deterministic, ~milliseconds per chapter |
| `attribution` | `analyze` job, only if an LLM is configured | resolves flagged spans; skipped (and reported) otherwise |
| `characters` | with structure | cast sync into `characters` |
| `summaries` | `analyze` job | extractive (verbatim sentences + offsets); optional LLM summary stored unconfirmed |
| `segmentation` | at import, after voice/merge/boundary changes, on "Resegment" | reconciles with existing segments (see below) |
| `director` | `analyze` job and inside book jobs (look-ahead, and `direct_scope` for the segment about to be narrated) | rules engine, or with an LLM: chapter beat sheet + scene-batched direction; cached |
| `translator` (not a stage row) | after the Director, only if `delivery.translator = llm` | `analysis.translate_pending` stores LLM-written instructions in the Director cache |
| `continuity` | with the Director and after edits | smoothing + bounded downstream recompute |

Status and errors are in `GET /api/books/{id}/narrative`, shown on the book's Narrative page, and as `analyze` jobs (label "Narrative
analysis") in the queue. The worker state shows `analyzing`. An `analyze` job advances one bounded unit per worker step, so
pause/cancel/other jobs interleave; it is resumable (state is in the DB). When the segment the worker is about to narrate has no direction, the book job calls
`analysis.direct_scope` (v0.7.0): it directs the window of up to 16 undirected segments of that segment's scene that contains it. Before v0.7.0 the job called `director_batch` for the
earliest undirected segments, which missed the picked segment whenever model-first scheduling (`pick_segment`) jumped ahead. `director_batch` (look-ahead, `analyze` job) still exists.
Neither holds a database write lock across an LLM request (doc 8, gotchas).

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

### With an LLM since v0.7.0: chapter beat sheet, then scene batches

The per-segment request above (`Director.direct`, prompt `director-3`) is still the engine for the rules path and for single segments. When an LLM is configured, `analysis.director_batch` and
`analysis.direct_scope` use two passes instead (one `_Batch` per run; reads in short sessions, **no write transaction while a request is in flight**):

1. **Chapter beat sheet** (`beats.py`, only when `director_chapter_plan` is on and the chapter has 2+ paragraphs). The canonical chapter text is split on blank lines and marked `[P1] [P2]...`
   (1-based; `para_number` maps a segment's `src_start` to its paragraph). ONE request with thinking on and **no JSON mode** (`LLMClient.derive(model=llm.chapter_model, thinking=True,
   max_tokens=llm.chapter_max_tokens, timeout_s=llm.chapter_timeout_s)`) returns `{arc, scenes[{paras, summary, beats[{paras, emotion, intensity, pacing, tension, purpose, pause}]}]}`.
   `validate_beats` maps synonyms into the vocabulary, drops beats with unknown vocabulary, and forces the beats to cover paragraphs 1..N exactly once (later start wins, gaps closed). Input is the
   chapter, the cast with **user-confirmed fields only**, and confirmed facts. It is advice: nothing is repaired by asking again (a thinking call costs minutes), an invalid or failed reply returns
   `None`, and the scene pass runs without a plan. Beats are keyed on paragraph numbers, not character offsets, so re-segmenting the chapter does not invalidate a plan.
2. **Scene windows** (`director.direct_scene`, prompt `scene-1`). `_windows` groups the picked segments into runs of adjacent segments of one chapter and scene, at most 16 (`SCENE_WINDOW`). One request
   per window returns `{"items":[{id, emotion, intensity, pacing, tension, delivery, change, pause, confidence, review}]}` with window-local ids `1..n`. The prompt carries the scene summary
   (the beat's scene summary, else the extractive one), the chapter arc, the baseline beats (`B1`, `B2`.. referenced per segment), the cast and the user's delivery hints, confirmed facts, the text just
   before and after the window (confined to the scene, 800 characters), and the two previous segments' final delivery. Validation is per item (`validate_scene_items`): the vocabulary check, `leak_guard` on
   the delivery note, `change` becomes `support`. Failed or missing ids are asked again once (only those); anything still failing falls back to `rules_direction` with `needs_review`. Three windows in a row
   with no usable item stop LLM calls for the batch. The first segment of a beat whose emotion family differs from the previous beat's is a planned turn: it is stored with `support = true` so smoothing keeps it.
   The stored schema per segment is identical to the single-segment engine's, plus `pause` (`none|beat|long`) and, when planned, `beat`.

`would_escalate` counts segments with overall confidence under 0.5, `needs_review`, or a disagreement with explicit lexical cues. It is **logged only**: no stronger model is called (deferred, doc 11).

**Optional LLM translator** (`delivery.translator = llm`, `llm.translator_model`; default `template`). A direction is normally turned into an instruction by the template (`delivery.compose`). With the
translator on, `analysis.translate_pending` (called after directing, and by the worker for an already-directed segment about to be narrated) asks a small model (thinking off, JSON mode, one repair round,
`translate-1`) for one short performance note and stores it in `director_cache` as `tr:<hash>`. The key covers the direction fields, the voice kind, the instruction length, the model, whether the speaker is a character
and their delivery hint; **not** the segment text or neighbours, so identical directions share one translation and edits elsewhere do not invalidate it. The answer must pass `validate_translation`
(no quotes or line breaks, no 6-word run copied from the line, scene summary or context, capped at `max_instruct_chars`). Failures are not cached; three in a row end the run, and the worker leaves the translator
alone for 120 s after a run in which every request failed. **`plan_for_segment` only reads the cache** (`delivery.stored_translation`, a plain SELECT) and uses the template until an answer exists, so
plans stay pure and cheap. Only voices that take an instruction (CustomVoice 1.7B, VoiceDesign) are translated; a cloned voice is not.

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
| clone / Base | `generate(text, ref_audio, ref_text)` | **no instruction** (the cloning path requires `instruct is None`). Two other routes since v0.7.0, both off by default: **reference bank** (a per-emotion reference clip replaces the base clip for that segment) and **pace** (stretch from the Director's pacing word) | reference audio + transcript conditioning only |

Per-model switches: `narrative.delivery.custom_voice (instruct|off)`, `voice_design (append|off)`; the Base row has no instruction switch. `delivery.build_instruct` returns why guidance was
not applied, shown in the inspector and stored on each attempt (`instruct_used`, `delivery_applied`).

**Why a reference bank rather than an instruction for clones.** The Base model cannot be told how to sound; it imitates its reference clip (the reference-emotion experiment, doc 7: the clone's pitch
variation followed the reference's). Before v0.7.0 a clone narrator therefore ignored the Director completely: on the owner's test book all 370 takes of the clone narrator had `delivery_applied = 0`.
The bank (`narrative/refbank.py`, table `voice_refs`) gives the clone up to four extra clips of the same voice, one per bucket, and `refbank.select` picks one per segment:

| Bucket | Emotion families (`vocab.FAMILIES`) | Emotions |
|---|---|---|
| `tense` | hot, tense | anger, disgust, fear, anxiety |
| `tender` | warm | tenderness, joy, humor |
| `sorrow` | low | sadness, grief, resignation |
| `bright` | high | excitement, surprise, determination |
| none (base clip) | flat | neutral, calm |

A clip is used only when `delivery.clone = reference_bank`, the segment has a direction whose intensity is at least `delivery.bank_min_intensity` (default 3), and that bucket has an adopted clip. The
clip's path/text replace the voice's `ref_audio_path`/`ref_text` in the `VoiceSpec`, its SHA is added to the generation key (so a take made with another clip is a different take and `invalidate_changed`
finds it), and the attempt stores `ref_id_used`; the inspector shows the chosen bucket or the reason none was used. Bank rows get ids that are never reused, so `ref_id_used` always points at one clip.
How clips get in (`services.adopt_ref`, routes in doc 3):
- **Candidates** (`generate_ref_candidates`): only for a clone promoted from a designed voice (`promote` records `source_voice_id` in the consent record; older clones are matched by the name in `rights_holder`).
  It queues 1-5 `preview` jobs on the designed voice (script `range-tense|warm|sad|joy` from the lab, instruction "Deliver the whole passage in a ... manner", `to_lab: true`). The worker (`_candidate_to_lab`)
  registers each result as a draft recording in the reference lab with provenance (`source: synthetic`, source voice, bucket, seed), runs Whisper against the script, and measures drift from the clone's base clip.
- **Adoption** needs a Whisper check on the recording with word error at most 8% (`BANK_MAX_WER`); recordings from a microphone must not be rated poor. The clip is level-matched to the base clip's speech RMS
  (`voicecheck.match_level`, never past 0.97 peak), the original is kept next to the converted clip, and pitch (f0) and brightness (spectral centroid) deltas from the base clip are stored in `metrics_json`
  and shown (advisory only, no threshold). Consent: a clone of a human voice needs the owner attestation and a statement (>= 10 characters); a synthetic clone accepts only a candidate generated by its own source
  voice, and a human recording cannot be adopted into it (and vice versa). Replacing a bucket deletes the old row; removal is admin-only.
- Mock-engine clips are test tones and are labelled so in the list; they exercise the plumbing only.

**Pace and pauses from the direction.** `delivery.pace_from_direction` (`off | auto | always`) scales the voice's own pace (doc 3, pronunciation/pace) by `PACE_FACTORS` (slow 0.92, measured 0.96, normal 1.0,
brisk 1.04, fast 1.08) and clamps to 0.8-1.25; `auto` does nothing for a segment whose instruction already reaches the model (it hears the pacing word). With the Director off, a pace is taken from a direction
only if the user edited it. The result is `Plan.pace` (+ `pace_reason`), part of the generation key when not 1.0, stretched in `pipeline.process_segment` (and, since v0.7.0, in segment previews) and stored as
`attempts.pace_used`. `calibrated_wpm` divides the stored (already stretched) duration by `pace_used`. `delivery.pauses_from_direction` makes `assembly` leave at least 350 ms (`beat`) or 900 ms (`long`) after a
sentence or paragraph whose direction has that pause hint; the hints enter the takes digest (`assembly.pause_hints`) only when present, so existing digests are unchanged. A pause never shortens a configured gap.

## Caching, invalidation, scheduling

- `attempts.gen_key` = hash(spoken text, voice kind/model/speaker/description/reference SHA, final instruction, engine, language, sampling parameters). A voice or delivery
  change resets only segments whose selected take's key differs (`store.invalidate_changed`); `select_take`/`approve` refuse a take with another key.
- Before synthesising, `pipeline.reusable_take` reuses an earlier passing take of the same segment with the same key (e.g. after reverting a voice). A user-requested regenerate never reuses.
- `gen_key` also covers (v0.7.0) the pace when not 1.0 and the SHA of the bank clip when one is used, so turning on `pace_from_direction` or `delivery.clone` changes the key of exactly the segments whose
  sound would change. **Apply new delivery settings to existing takes** (Narrative page, `POST /api/books/{id}/narrative/apply-delivery`) is the explicit way to bring finished takes in line:
  `invalidate_changed(dry_run=True)` counts them, the user confirms, then only those are reset (approved ones only on request) and queued first. Nothing is regenerated silently when a setting is changed.
- `director_cache` (one table, several key families; `response_json`, `model`, `hits`): per-segment direction (the hash described above), **`<hash>` of a scene window** (`scene-1`: the window's texts, speakers, kinds, beat
  signatures, hints, profiles, scene summary, arc, facts, the text around it, sensitivity and model; deliberately **not** the previous segments' delivery, which would chain every scene to the one before
  and make one edit invalidate the rest of the book: continuity smoothing applies it afterwards), **`beats:<hash>`** (chapter text SHA, cast, facts, model, `beats-1`), **`beats-fail:beats:<hash>`** (failure marker with a
  timestamp, honoured for 20 minutes), and **`tr:<hash>`** (LLM translation of a direction, see above). A window is cached only when every item in it is valid LLM output; windows with rules fallbacks are asked again next time.
- Attempts and disk: `attempts.pace_used` and `attempts.ref_id_used` (schema v5) record what each take was made with; they do not enter any decision except `calibrated_wpm`.
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
Added in v0.7.0 (every default keeps the old behaviour):

| Setting | Default | Meaning |
|---|---|---|
| `director_chapter_plan` | on | chapter beat sheet before the scene pass (only with an LLM) |
| `llm.chapter_model` / `scene_model` | blank = `llm.model` | model for the beat sheet (thinking) / for scene windows |
| `llm.translator_model` | blank = no LLM translator | model for `delivery.translator = llm` |
| `llm.chapter_max_tokens` / `chapter_timeout_s` | 16000 / 900 | budget (includes reasoning) and timeout of the chapter pass |
| `delivery.clone` | `off` | `reference_bank`: clones use the bank clip for the segment's emotion |
| `delivery.bank_min_intensity` | 3 | weakest direction intensity (1-5) that uses a bank clip |
| `delivery.pace_from_direction` | `off` | `auto` (only when no instruction reaches the model) or `always` |
| `delivery.pauses_from_direction` | off | honour the Director's `pause` hint at assembly |
| `delivery.translator` | `template` | `llm`: a small model writes the instruction (cached ahead of time) |

Settings > Narrative > Local LLM has **Test connection**, which also checks that the chapter, scene and translator models (when different) are listed by the server.
The API key is entered in Settings > Narrative > Local LLM (write-only, admin only) and stored in `config/secrets.json` (mode 0600; plaintext at rest, like the TLS key and `auth.json`). It is not in `settings.json`, the database, the audit/event logs or any API response, and it is read by the worker on every request, so changes apply immediately. Fallback: `api_key_env` names an environment variable that must start with `STUDIO_LLM_` and be set for the service (e.g. in the LaunchAgent environment); a saved key wins over it. Effective
settings = global (live) + the book's overrides; segmentation parameters take effect on the next (re)segmentation.

## Database (migrations v2, v4, v5)

New: `chapter_text`, `characters`, `nar_spans`, `story_notes`, `directions`, `direction_log`, `director_cache`, `seg_edits`, `segment_history`, `analysis_stages`.
Columns: `books.narrative_json`; `segments.src_start/src_end/character_id/span_kind/scene_idx/voice_override_id/needs_review/review_reason/boundary_manual`;
`attempts.gen_key/instruct_used/delivery_applied/voice_id_used/asr_skipped`. `jobs` was rebuilt to allow kind `analyze` (one active per book); `migrate()` turns foreign keys off
around it, verifies `foreign_key_check` before each commit, and backs the database up first (`backups/db/`).

**v4** (narrator versions): `books.work_uid` (doc 2). **v5** (v0.7.0): `voice_refs` (`voice_id`, `bucket` tense|tender|sorrow|bright, `ref_audio_path`, `ref_original_path`, `ref_text`, `ref_sha256`, `ref_duration_s`, `source` recording|synthetic, `source_ref`, `consent_json`, `metrics_json`; UNIQUE per voice and bucket), `ab_votes` (`book_id`, `segment_id`, `comparison`, labels, both `gen_key`s, `winner` a|b|same, `other_narrator`, `actor`), and `attempts.pace_used`, `attempts.ref_id_used`. Bank clips live under the references category (`<voice uid>/refs/`) and count toward disk usage; `ab_votes` rows are removed when a book is deleted and are not copied to a new narrator version.

## Running a local LLM (optional)

Any OpenAI-compatible server: LM Studio (`http://127.0.0.1:1234/v1`), `mlx_lm.server`, llama.cpp's server, Ollama's `/v1`. Set provider, URL and model in Settings > Narrative (or per
book), restart nothing. **No LLM is installed on the production Mac**; the LLM path is covered by tests against an HTTP stub, not by a real model.

Thinking models (Gemma 4, Qwen3...): ordinary requests send `chat_template_kwargs: {enable_thinking: false}` and JSON mode, because a model that is allowed to think spends `max_tokens` on reasoning and answers with nothing (measured on oMLX with `gemma-4-26B-A4B-it-oQ4e-mtp`). The chapter beat sheet is the one deliberate exception: thinking on, JSON mode off, a large budget. A server that answers 400 to an optional field has it dropped and remembered (`LLMClient._rejected`).

## Blind A/B listening (v0.7.0)

`narrative/ab.py` holds four named comparisons, each two explicit settings patches (only `director_enabled` and the delivery keys can be patched: `store.effective_narrative_with`):

| Comparison | A | B | Applies to |
|---|---|---|---|
| `directed_vs_undirected` | Director on | Director off, no pace from direction | any voice, segment with a direction |
| `llm_vs_template_translator` | `translator = llm` | `template` | preset and designed voices |
| `bank_vs_base_clip` | `clone = reference_bank` | `off` | clones with an adopted clip for the segment's emotion |
| `pace_on_vs_off` | `pace_from_direction = always` | `off` | segment whose pacing is not `normal` |

`services.enqueue_ab` refuses (409) a pair that would sound the same (equal generation key, text, instruction, clip and pace) or a segment of the wrong voice kind, so a round is never a guaranteed tie. It queues two `preview` jobs with the same seed, `ns_patch` and an `ab` block; which job is clip X is random and stored only on the server. The worker renders each with `plan_for_segment(..., ns_patch=...)` and the production time stretch, and writes nothing to the segment's takes. `GET /api/ab/{round}` hides the labels until `POST .../vote` (`x | y | same`, optional `other_narrator`); one vote per round. `random_differing_segment` samples up to 200 segments (directed ones first) for one where the pair differs. The tally (`ab.tally`, also in `GET /api/books/{id}/narrative`) shows per comparison the share of non-tie votes for A and the share of "another narrator" flags; the guidance rule is **adopt when A wins at least 60% of the non-tie votes and the other-narrator flag is at most 10%**. It is guidance for the owner, nothing enforces or acts on it. Mock-engine rounds are flagged as tones, and their votes should be discarded: they measure nothing about speech.

## Known limitations

See `08-decisions-gotchas-backlog.md` ("Narrative layer known limitations") and the measured results in `07-testing-and-baseline.md` ("Baseline results recorded at v0.3.0"). Real-hardware script: `tools/e2e/narrative_real.py`.

v0.7.0 additions (all measured with the mock engine and stub LLM servers only; none on the Mac, with a real model, or by listening):

- **Escalation is deferred.** `would_escalate` counts low-confidence, `needs_review` or cue-disagreeing segments, but no stronger model is asked about them. Whether it is worth building is for the A/B tool and a real model to show.
- **Real-model behaviour is unmeasured**: chapter-pass time and token use on a real thinking model, how often the beat sheet validates, scene-window failure/fallback rate, translator output quality, `llm.scene_model` as a smaller model.
- **Audible effect is unmeasured**: reference-bank clips, pace factors, pause hints and LLM wording have never been listened to on real speech. The pitch-preserving stretch (WSOLA) was not audited at these small factors.
- The bank is only as good as its clips: a clip that changes the voice's timbre makes the clone sound like another narrator (the A/B "another narrator" flag exists for that). Candidates for a human-voice clone cannot be generated; they must be recorded and Whisper-checked.
- Beat sheets and scene windows need an LLM; with the rules engine the pause hint is always `none` and beats are absent.
- Mock-engine A/B rounds and mock bank clips are test tones; votes from them say nothing about narration.
