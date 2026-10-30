"""Translate a final direction into inputs a given Qwen3-TTS model actually accepts.

Verified against mlx-audio 0.6.3 (``mlx_audio/tts/models/qwen3_tts/qwen3_tts.py``):

* CustomVoice  ``generate_custom_voice(text, speaker, language, instruct=...)`` - ``instruct`` is an
  emotion/style instruction. The 0.6B CustomVoice model ignores it (the library sets it to None).
* VoiceDesign  ``generate_voice_design(text, instruct, ...)`` - ``instruct`` is the *voice
  description itself*; there is no separate delivery channel, so delivery guidance can only be
  appended to the stable description, and the voice is re-created on every call (identity can drift).
* Base (cloning)  ``generate(text, ref_audio=..., ref_text=...)`` - reference conditioning only. The
  library's cloning path requires ``instruct is None``; there is no instruction input.
"""
from __future__ import annotations

import json
import re

from ..config import DeliverySettings, LLMSettings
from ..db import row
from ..util import dumps, loads, short_hash
from . import vocab as V
from .director import leak_guard
from .llm import LLMError, resolve_models

EMOTION_ADJ = {
    "neutral": "neutral", "calm": "calm", "joy": "joyful", "excitement": "excited", "tenderness": "tender", "sadness": "sad",
    "grief": "grief-stricken", "fear": "fearful", "anxiety": "anxious", "anger": "angry", "surprise": "surprised",
    "disgust": "disgusted", "determination": "determined", "humor": "amused", "resignation": "resigned",
}


def model_supports_instruct(kind: str, model_id: str) -> tuple[bool, str]:
    """(supported, reason) for a voice kind on a model."""
    if kind == "clone":
        return False, "The Base (voice cloning) model has no instruction input; delivery is conditioned only by the reference clip."
    if kind == "preset" and re.search(r"0[._]?6B", model_id, re.I):
        return False, "The 0.6B CustomVoice model ignores instructions."
    return True, ""


def compose(final: dict, max_chars: int = 160) -> str | None:
    emotion = final.get("emotion", "neutral")
    inten = int(final.get("intensity", 1))
    pacing = final.get("pacing", "normal")
    free = " ".join(str(final.get("delivery") or "").replace('"', "").split())
    neutral = emotion == "neutral" and inten <= 2
    parts = []
    if not neutral:
        adv = V.INTENSITY_ADVERB.get(inten, "moderately")
        parts.append(f"Speak {adv} {EMOTION_ADJ.get(emotion, emotion)}")
    if pacing != "normal":
        parts.append(V.PACING_PHRASES.get(pacing, pacing))
    text = ", ".join(parts)
    if free:
        text = f"{text}. {free}" if text else free
    if not text:
        return None
    text = text.strip()
    if not text.endswith((".", "!", "?")):
        text += "."
    return _cap(text, max_chars)


def _cap(text: str, max_chars: int) -> str:
    """Shorten to ``max_chars`` at a clause boundary (a cut mid-word is the last resort)."""
    if len(text) > max_chars:
        cut = text[:max_chars]
        k = max(cut.rfind(". "), cut.rfind(", "), cut.rfind(" "))
        text = (cut[:k] if k > 20 else cut).rstrip(" ,;") + "."
    return text


def build_instruct(final: dict | None, voice_kind: str, model_id: str, cfg: DeliverySettings, translated: str | None = None) -> tuple[str | None, dict]:
    """-> (instruction text for ``SynthParams.instruct`` or None, info). ``info['applied']`` says
    whether the delivery guidance reaches the model. ``translated`` is a stored LLM translation of ``final``; it replaces the
    template text only when ``cfg.translator == "llm"`` (it already encodes emotion, intensity and pacing)."""
    if not final:
        return None, {"applied": False, "reason": "no direction for this segment"}
    ok, why = model_supports_instruct(voice_kind, model_id)
    if not ok:
        return None, {"applied": False, "reason": why}
    mode = cfg.custom_voice if voice_kind == "preset" else cfg.voice_design
    if mode == "off":
        return None, {"applied": False, "reason": f"Delivery instructions are switched off for {voice_kind} voices in Settings."}
    text = compose(final, cfg.max_instruct_chars)
    if not text:
        return None, {"applied": False, "reason": "neutral delivery needs no instruction"}  # also decides that nothing is worth translating
    reason = "passed as the model's instruct input"
    if cfg.translator == "llm":
        try:
            text, reason = validate_translation(translated, cfg.max_instruct_chars), "LLM-translated instruction"
        except ValueError:  # none stored yet (or unusable): the template still narrates the line
            reason = "LLM translation not available yet, template used"
    if voice_kind == "design":
        text = f"For this line: {text}"
    return text, {"applied": True, "reason": reason}


# ----------------------------------------------------------------------------- LLM translator
#
# Optional second route from a direction to an instruction: a small LLM writes the performance note instead of the template. It never
# runs while a plan is made (plans are built inside write transactions and web requests): analysis.translate_pending asks the model
# ahead of time and stores the answer in the shared director_cache, and plan_for_segment only reads it back.

TRANSLATOR_PROMPT_VERSION = "translate-1"

TRANSLATOR_SYSTEM_PROMPT = """You write the performance instruction that a text-to-speech voice model receives with one line of an audiobook. You are given the Director's structured direction for the line (emotion, intensity 1-5, pacing, tension, pause, an optional delivery note). Turn it into ONE short, natural instruction.

Rules:
- Reply with ONE JSON object and nothing else: {"instruction": str}.
- The instruction is an imperative performance note about how the voice sounds, for example: voice breaking, barely above a whisper, pausing between phrases. Describe breath, pitch, volume, tempo and pauses.
- Stay faithful to the direction: do not add emotions it does not contain and do not change its intensity. The delivery note, when present, is part of the direction.
- Never quote or paraphrase the line, the scene summary or the context; they are only there so you understand the moment.
- No double quotes, no line breaks, no names, no stage directions. Plain sentences or comma-separated phrases only.
- Stay within the character limit given in the request."""


def translator_label(llm_cfg: LLMSettings) -> str | None:
    """The translator model's cache label, or None when no LLM translator is configured."""
    model = resolve_models(llm_cfg)["translator"]
    return f"{llm_cfg.provider}:{model}" if model and llm_cfg.provider == "openai_compatible" else None


def build_translator_prompt(final: dict, speaker_label: str | None, voice_kind: str, scene_summary: str = "", prev_delivery: str = "",
                            text_excerpt: str = "", hints: str = "", max_chars: int = 160) -> str:
    """The user message ("Translate this direction." header, then one JSON object)."""
    payload = {
        "direction": {"emotion": final.get("emotion", "neutral"), "intensity": final.get("intensity", 1), "pacing": final.get("pacing", "normal"),
                      "tension": final.get("tension", 1), "pause_after": final.get("pause", "none"), "delivery_note": final.get("delivery") or ""},
        "speaker": speaker_label or "Narrator", "voice_kind": voice_kind, "max_characters": max_chars,
        "user_instructions": hints or "",
        "context_for_understanding_only": {"scene_summary": scene_summary or "", "previous_delivery": prev_delivery or "", "line_excerpt": (text_excerpt or "")[:200]},
    }
    return "Translate this direction.\n" + json.dumps(payload, ensure_ascii=False, indent=1)


def translate_cache_key(final: dict, voice_kind: str, model_label: str, max_chars: int, speaker: str | None = None, hints: str = "") -> str:
    """Over the direction and the voice only. The segment text and its neighbours are deliberately not part of it: identical directions share
    one translation, and an edit elsewhere in the manuscript does not invalidate it. ``speaker`` counts only as narrator or character."""
    basis = {
        "v": TRANSLATOR_PROMPT_VERSION, "m": model_label, "k": voice_kind, "n": int(max_chars),
        "e": final.get("emotion", "neutral"), "i": int(final.get("intensity", 1)), "p": final.get("pacing", "normal"),
        "t": int(final.get("tension", 1)), "d": " ".join(str(final.get("delivery") or "").split()), "z": final.get("pause", "none"),
        "c": bool(speaker) and speaker != "Narrator", "h": " ".join((hints or "").split()),
    }
    return "tr:" + short_hash(dumps(basis), 24)


_QUOTE_CHARS = dict.fromkeys(map(ord, '"“”„‟«»'))


def validate_translation(raw, max_chars: int, forbidden_texts=()) -> str:
    """The instruction from a reply (``{"instruction": str}`` or the bare string), made safe for the engine: no quotes or line breaks, no run
    of words copied from ``forbidden_texts`` (the line and its context), at most ``max_chars``. Raises ValueError when nothing usable is left."""
    value = raw.get("instruction") if isinstance(raw, dict) else raw
    if not isinstance(value, str):
        raise ValueError("'instruction' must be a string")
    text = " ".join(value.translate(_QUOTE_CHARS).split())
    if forbidden_texts:
        text, _changed = leak_guard(text, [t for t in forbidden_texts if t])
    text = text.strip(" ,;:-")
    if len(re.sub(r"\W", "", text)) < 3:
        raise ValueError("the instruction is empty")
    if not text.endswith((".", "!", "?")):
        text += "."
    return _cap(text, max_chars)


def speaker_and_hints(conn, seg: dict) -> tuple[str, str]:
    """(speaker label, the character's own delivery preference) as the translator key and prompt see them."""
    ch = row(conn, "SELECT name, delivery_pref FROM characters WHERE id=?", (seg["character_id"],)) if seg.get("character_id") else None
    return (ch["name"], ch["delivery_pref"] or "") if ch else ("Narrator", "")


def stored_translation(conn, final: dict, voice_kind: str, seg: dict, ns) -> str | None:
    """The translation analysis stored for this direction, or None. A plain SELECT: no request, no write, no hit counter."""
    label = translator_label(ns.llm)
    if not label:
        return None
    speaker, hints = speaker_and_hints(conn, seg)
    r = row(conn, "SELECT response_json FROM director_cache WHERE key=?", (translate_cache_key(final, voice_kind, label, ns.delivery.max_instruct_chars, speaker, hints),))
    d = loads(r["response_json"], None) if r else None
    return d["instruction"] if isinstance(d, dict) and isinstance(d.get("instruction"), str) and d["instruction"].strip() else None


def translate_llm(client, final: dict, voice_kind: str, *, cache_get, cache_put, speaker: str | None = "Narrator", scene_summary: str = "",
                  prev_delivery: str = "", text_excerpt: str = "", hints: str = "", forbidden=(), max_chars: int = 160, stats: dict | None = None) -> str | None:
    """Ask the translator model for the instruction of ``final`` (thinking off, JSON mode, one repair round) unless it is already cached.
    -> the validated instruction, or None (no translator configured, or the model failed). Failures are not cached, so a later run retries.
    ``cache_get``/``cache_put`` must be self-contained short transactions; the caller holds no write lock across this call."""
    label = translator_label(client.cfg)
    if not label:
        return None

    def count(name: str) -> None:
        if stats is not None:
            stats[name] = stats.get(name, 0) + 1

    key = translate_cache_key(final, voice_kind, label, max_chars, speaker, hints)
    cached = cache_get(key)
    if isinstance(cached, dict) and isinstance(cached.get("instruction"), str) and cached["instruction"].strip():
        count("translation_cache_hits")
        return cached["instruction"]
    tc = client.derive(model=resolve_models(client.cfg)["translator"], thinking=False)
    prompt = build_translator_prompt(final, speaker, voice_kind, scene_summary, prev_delivery, text_excerpt, hints, max_chars)
    try:
        text = tc.chat_json(TRANSLATOR_SYSTEM_PROMPT, prompt, lambda raw: validate_translation(raw, max_chars, forbidden))
    except LLMError:
        count("translation_failures")
        return None
    cache_put(key, {"instruction": text, "version": TRANSLATOR_PROMPT_VERSION}, label)
    count("translations")
    return text


# ----------------------------------------------------------------------------- pace from direction

# Pitch-preserving time stretch (audio.time_stretch) is only transparent for modest factors, so the
# Director's pacing words map to small multipliers on top of the voice's own pace.
PACE_FACTORS = {"slow": 0.92, "measured": 0.96, "normal": 1.0, "brisk": 1.04, "fast": 1.08}
PACE_MIN, PACE_MAX = 0.8, 1.25


def effective_pace(voice_pace: float, final: dict | None, mode: str, instruct_reaches_model: bool) -> tuple[float, str]:
    """-> (stretch factor, reason). ``mode`` is DeliverySettings.pace_from_direction: off | auto | always.
    ``auto`` stretches only when no instruction reaches the model (clones, 0.6B presets, delivery off),
    because otherwise the model already hears the pacing words."""
    base = float(voice_pace or 1.0)
    if mode == "off" or not final:
        return base, ""
    if mode == "auto" and instruct_reaches_model:
        return base, "pacing is already part of the delivery instruction"
    pacing = str(final.get("pacing") or "normal")
    mult = PACE_FACTORS.get(pacing, 1.0)
    pace = min(PACE_MAX, max(PACE_MIN, base * mult))
    return pace, (f"pacing '{pacing}' → x{mult:g}" if abs(mult - 1.0) > 1e-9 else "")
