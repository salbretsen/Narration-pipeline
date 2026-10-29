# Changelog

Versions are MAJOR.MINOR.PATCH: MAJOR = a major release update (needs human acceptance); MINOR = feature
additions or overhauls inside the current major release; PATCH = minor adjustments (visual tweaks, bug fixes).

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
