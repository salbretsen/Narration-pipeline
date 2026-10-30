"""Emotion reference bank for cloned voices.

A cloned (Qwen3-TTS Base) voice has no instruction input: how it sounds is steered only by its
reference clip. The bank lets a clone carry short clips of the *same voice* in different
emotional registers, one adopted clip per emotion the Director and the Review page can ask for
(every emotion in ``vocab.EMOTIONS`` except neutral and calm, which the base reference already
serves). When a segment's direction is strong enough, the clip for its emotion replaces the base
reference for that segment; without one, a related emotion's clip is used, else the base reference.
Everything here is pure: no database, no engine, no network.
"""
from __future__ import annotations

from . import vocab as V

# emotions the base reference already serves
BASE_EMOTIONS = ("neutral", "calm")

# emotion -> label, phrase used to ask a VoiceDesign voice for that register, and its emotion script (reference.py)
BUCKET_INFO: dict[str, dict] = {
    e: {"label": e.capitalize(), "family": V.FAMILIES[e], "emotion_text": V.EMOTION_PHRASES[e], "script_id": f"emotion-{e}"}
    for e in V.EMOTIONS if e not in BASE_EMOTIONS
}
BUCKETS = tuple(BUCKET_INFO)

# when an emotion has no clip of its own: the closest emotions to try, in order
NEIGHBOURS: dict[str, tuple[str, ...]] = {
    "anger": ("disgust", "determination"),
    "disgust": ("anger",),
    "determination": ("anger", "excitement"),
    "fear": ("anxiety", "surprise"),
    "anxiety": ("fear",),
    "sadness": ("grief", "resignation"),
    "grief": ("sadness", "resignation"),
    "resignation": ("sadness", "grief"),
    "joy": ("excitement", "humor", "tenderness"),
    "excitement": ("joy", "surprise"),
    "surprise": ("excitement", "fear"),
    "tenderness": ("joy",),
    "humor": ("joy",),
}

# bucket names before v0.9.0 (one clip served a whole emotion family) -> the emotion their clip was written for
LEGACY_BUCKETS = {"tense": "fear", "tender": "tenderness", "sorrow": "sadness", "bright": "joy"}


def bucket_for(emotion: str | None) -> str | None:
    """Which bank clip serves an emotion. ``None`` means the base reference (neutral / calm / unknown)."""
    e = str(emotion or "").lower()
    return e if e in BUCKET_INFO else None


def fallback_chain(bucket: str) -> tuple[str, ...]:
    """The emotion itself, then its neighbours."""
    return (bucket, *NEIGHBOURS.get(bucket, ()))


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
    refs = refs_by_bucket or {}
    for b in fallback_chain(bucket):
        if refs.get(b):
            if b == bucket:
                return refs[b], f"emotion '{emotion}' → '{b}' reference clip"
            return refs[b], f"emotion '{emotion}' has no clip of its own → the related '{b}' reference clip"
    return None, f"emotion '{emotion}' has no adopted reference clip (nor a related one); using the base reference"


def choose(ns, final: dict | None, voice_kind: str, refs_by_bucket: dict[str, dict]) -> dict | None:
    """The bank row to use for this segment, or None to keep the voice's base reference."""
    return select(ns, final, voice_kind, refs_by_bucket)[0]
