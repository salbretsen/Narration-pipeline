"""Context-aware narration: semantic segmentation, speaker analysis, Narrative Director.

Module map
  vocab        controlled emotion vocabulary and the lexical cue tables
  structure    Pass 1  - paragraphs, scenes, dialogue spans, speaker attribution, cues
  segmenter    Pass 2  - narration segments built from the Pass 1 structure
  windows      narration window (spoken text) and narrative context window (analysis only)
  llm          OpenAI-compatible chat client (no new dependencies)
  director     Narrative Director: prompt, schema validation, rules fallback, leak guard
  continuity   delivery continuity state and smoothing
  delivery     translation of a direction into model-appropriate Qwen3-TTS inputs
  cast         per-manuscript characters, aliases and story notes
  store        persistence and orchestration (stages, edits, inspector, generation plan)
  voicecheck   acoustic voice-consistency measurement across independently generated segments
"""
