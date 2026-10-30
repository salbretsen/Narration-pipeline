"""Emotion reference bank for cloned voices.

A cloned (Qwen3-TTS Base) voice has no instruction input: how it sounds is steered only by its
reference clip. The bank lets a clone carry a few short clips of the *same voice* in different
emotional registers (one adopted clip per bucket). When a segment's direction is strong enough and
its emotion maps to a bucket that has a clip, that clip replaces the base reference for that
segment. Everything here is pure: no database, no engine, no network.
"""
from __future__ import annotations

from . import vocab as V

# bucket -> (label, phrase used to ask a VoiceDesign voice for that register, lab script id)
BUCKET_INFO: dict[str, dict] = {
    "tense": {"label": "Tense / urgent", "emotion_text": "tense, urgent, strained", "script_id": "range-tense"},
    "tender": {"label": "Tender / warm", "emotion_text": "tender, warm, gentle", "script_id": "range-warm"},
    "sorrow": {"label": "Sorrowful / heavy", "emotion_text": "sorrowful, heavy, subdued", "script_id": "range-sad"},
    "bright": {"label": "Bright / lively", "emotion_text": "bright, joyful, lively", "script_id": "range-joy"},
}
BUCKETS = tuple(BUCKET_INFO)

# emotion family (vocab.FAMILIES) -> bucket; None = the voice's base reference is already right
_FAMILY_BUCKET = {"flat": None, "low": "sorrow", "warm": "tender", "high": "bright", "hot": "tense", "tense": "tense"}


def bucket_for(emotion: str | None) -> str | None:
    """Which bank bucket serves an emotion. ``None`` means the base reference (neutral / calm / unknown)."""
    return _FAMILY_BUCKET.get(V.FAMILIES.get(str(emotion or "").lower(), "flat"))


def select(ns, final: dict | None, voice_kind: str, refs_by_bucket: dict[str, dict]) -> tuple[dict | None, str]:
    """-> (bank row or None, human reason). ``ns`` is the book's NarrativeSettings."""
    cfg = ns.delivery
    if cfg.clone != "reference_bank":
        return None, "The emotion reference bank is switched off in Settings."
    if voice_kind != "clone":
        return None, "Only cloned voices use the emotion reference bank."
    if not final:
        return None, "no direction for this segment"
    emotion = str(final.get("emotion") or "neutral")
    try:
        intensity = int(final.get("intensity") or 1)
    except (TypeError, ValueError):
        intensity = 1
    bucket = bucket_for(emotion)
    if bucket is None:
        return None, f"emotion '{emotion}' is served by the base reference"
    if intensity < cfg.bank_min_intensity:
        return None, f"intensity {intensity} is below the reference-bank threshold ({cfg.bank_min_intensity}); using the base reference"
    ref = (refs_by_bucket or {}).get(bucket)
    if not ref:
        return None, f"emotion '{emotion}' maps to the '{bucket}' bucket, which has no adopted reference clip; using the base reference"
    return ref, f"emotion '{emotion}' → '{bucket}' reference clip"


def choose(ns, final: dict | None, voice_kind: str, refs_by_bucket: dict[str, dict]) -> dict | None:
    """The bank row to use for this segment, or None to keep the voice's base reference."""
    return select(ns, final, voice_kind, refs_by_bucket)[0]
