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

import re

from ..config import DeliverySettings
from . import vocab as V

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
    if len(text) > max_chars:
        cut = text[:max_chars]
        k = max(cut.rfind(". "), cut.rfind(", "), cut.rfind(" "))
        text = (cut[:k] if k > 20 else cut).rstrip(" ,;") + "."
    return text


def build_instruct(final: dict | None, voice_kind: str, model_id: str, cfg: DeliverySettings) -> tuple[str | None, dict]:
    """-> (instruction text for ``SynthParams.instruct`` or None, info). ``info['applied']`` says
    whether the delivery guidance reaches the model."""
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
        return None, {"applied": False, "reason": "neutral delivery needs no instruction"}
    if voice_kind == "design":
        text = f"For this line: {text}"
    return text, {"applied": True, "reason": "passed as the model's instruct input"}
