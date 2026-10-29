"""The Narrative Director: per-segment delivery direction.

Two interchangeable engines produce the same schema-validated record:

* ``llm``   - a local OpenAI-compatible model reads the segment, its surrounding context, the
  character profile, story notes and the previous delivery decisions;
* ``rules`` - a deterministic engine that uses the lexical and structural cues found in Pass 1.
  It is always available, is what runs when no LLM is configured, and is the fallback when the
  LLM fails. It is modest by design and labelled as such everywhere.

The Director never changes manuscript text. Its output separates ``facts`` (verbatim quotations
that are checked against the supplied text and dropped when they are not found) from
``inferences`` (interpretation). Analysis-only text cannot leak into the TTS instruction: the
delivery string is checked against the surrounding context and truncated if it quotes it.
"""
from __future__ import annotations

import json
import re

from ..util import dumps, short_hash
from . import vocab as V
from .llm import LLMClient, LLMError

PROMPT_VERSION = "director-3"

SYNONYMS = {
    "sad": "sadness", "happy": "joy", "joyful": "joy", "angry": "anger", "scared": "fear", "afraid": "fear", "fearful": "fear",
    "worried": "anxiety", "nervous": "anxiety", "anxious": "anxiety", "excited": "excitement", "grieving": "grief", "tender": "tenderness",
    "loving": "tenderness", "amused": "humor", "resigned": "resignation", "determined": "determination", "surprised": "surprise",
    "disgusted": "disgust", "serene": "calm", "tense": "anxiety", "mournful": "grief", "furious": "anger", "joyous": "joy",
}

SYSTEM_PROMPT = f"""You are the Narrative Director for an audiobook production system. For ONE segment of a manuscript you decide how it should be performed by a text-to-speech narrator or character voice.

Rules:
- Reply with ONE JSON object and nothing else.
- Never rewrite, add, shorten or translate manuscript text. You only describe delivery.
- Use the controlled vocabulary exactly. emotion is one of: {", ".join(V.EMOTIONS)}. pacing is one of: {", ".join(V.PACINGS)}. intensity and tension are integers 1-5.
- "facts" are objective things the manuscript states, each as a SHORT VERBATIM QUOTE copied from the segment or its context. "inferences" are your interpretations. Do not put interpretations in facts.
- Respect continuity: the previous segment's delivery and the speaker's current emotional state persist unless the text gives a reason to change. Set "text_supported_change" to true ONLY when the manuscript itself signals an abrupt change (a shout, a revelation, a scene turn).
- Prefer the user's confirmed character information over anything you infer. Do not invent character traits or events.
- "delivery" is a short natural-language performance note (max 25 words) such as "voice breaking, barely above a whisper". It must not quote the manuscript.
- If the speaker or the emotion is genuinely unclear, say so in "ambiguity" and set needs_review true instead of guessing confidently.

JSON shape:
{{"segment_id": str, "speaker": str, "voice_profile": str, "emotion": str, "intensity": int, "delivery": str, "pacing": str, "tension": int,
 "confidence": {{"speaker": 0-1, "emotion": 0-1, "overall": 0-1}}, "text_supported_change": bool,
 "context_refs": [{{"kind": "previous|following|summary|character|story", "quote": str}}], "facts": [str], "inferences": [str],
 "ambiguity": str|null, "needs_review": bool}}"""


class DirectorError(ValueError):
    pass


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s']", " ", s.lower()).split())


def _grounded(quote: str, haystack_norm: str) -> bool:
    q = _norm(quote)
    return len(q) >= 3 and q in haystack_norm


def leak_guard(delivery: str, forbidden: list[str], window: int = 6) -> tuple[str, bool]:
    """Remove from ``delivery`` any run of ``window`` or more consecutive words copied from the
    forbidden texts (the segment itself and the analysis-only context). -> (clean text, changed)"""
    words = delivery.split()
    if len(words) < window:
        return delivery, False
    pool = " " + " ".join(_norm(t) for t in forbidden) + " "
    keep = [True] * len(words)
    norm_words = [_norm(w) for w in words]
    for i in range(len(words) - window + 1):
        chunk = " ".join(norm_words[i:i + window]).strip()
        if chunk and f" {chunk} " in pool:
            for j in range(i, i + window):
                keep[j] = False
    if all(keep):
        return delivery, False
    return " ".join(w for w, k in zip(words, keep) if k).strip(" ,;"), True


def _int(v, lo=1, hi=5, default=3) -> int:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if 0.0 < f <= 1.0 and not float(f).is_integer():
        f = f * 5
    return int(max(lo, min(hi, round(f))))


def validate_direction(raw: dict, *, sid: str, text: str, context: str, known_speaker: str | None, cast_names: list[str]) -> dict:
    """Normalise and check an LLM reply. Raises DirectorError on anything unusable; repairs and
    annotates the rest (nothing is silently trusted)."""
    if not isinstance(raw, dict):
        raise DirectorError("reply is not a JSON object")
    notes: list[str] = []
    emo = str(raw.get("emotion", "")).strip().lower()
    emo = SYNONYMS.get(emo, emo)
    if emo not in V.EMOTIONS:
        raise DirectorError(f"emotion {raw.get('emotion')!r} is not in the controlled vocabulary")
    pacing = str(raw.get("pacing", "normal")).strip().lower()
    pacing = {"slowly": "slow", "quick": "brisk", "quickly": "brisk", "medium": "normal", "moderate": "normal", "steady": "measured"}.get(pacing, pacing)
    if pacing not in V.PACINGS:
        raise DirectorError(f"pacing {raw.get('pacing')!r} is not in the controlled vocabulary")
    hay = _norm(text + " " + context)
    facts, dropped = [], 0
    for f in (raw.get("facts") or [])[:8]:
        if isinstance(f, str) and _grounded(f, hay):
            facts.append(f.strip()[:200])
        else:
            dropped += 1
    if dropped:
        notes.append(f"{dropped} stated fact(s) were not found in the supplied manuscript text and were removed")
    refs = []
    for r in (raw.get("context_refs") or [])[:8]:
        if isinstance(r, dict) and isinstance(r.get("quote"), str) and (_grounded(r["quote"], hay) or r.get("kind") in ("character", "story", "summary")):
            refs.append({"kind": str(r.get("kind", "previous"))[:12], "quote": r["quote"].strip()[:200]})
    delivery = " ".join(str(raw.get("delivery") or "").replace('"', "").replace("\n", " ").split())[:240]
    delivery, changed = leak_guard(delivery, [text, context])
    if changed:
        notes.append("delivery text quoted the manuscript and was trimmed")
    speaker = str(raw.get("speaker") or "").strip()
    needs = bool(raw.get("needs_review", False))
    ambiguity = raw.get("ambiguity") if isinstance(raw.get("ambiguity"), str) and raw.get("ambiguity") else None
    if known_speaker and speaker and speaker.lower() != known_speaker.lower():
        notes.append(f"the model named '{speaker}' as the speaker; the established attribution '{known_speaker}' was kept")
        needs = True
        ambiguity = ambiguity or f"The Director disagrees with the attribution ({speaker} vs {known_speaker})."
    conf = raw.get("confidence") if isinstance(raw.get("confidence"), dict) else {}
    conf = {k: round(max(0.0, min(1.0, float(conf.get(k, 0.5)))), 2) for k in ("speaker", "emotion", "overall")}
    return {
        "segment_id": sid, "speaker": known_speaker or speaker, "voice_profile": str(raw.get("voice_profile") or "")[:200],
        "emotion": emo, "intensity": _int(raw.get("intensity")), "delivery": delivery, "pacing": pacing, "tension": _int(raw.get("tension"), default=2),
        "confidence": conf, "support": bool(raw.get("text_supported_change", False)) and conf["emotion"] >= 0.6,
        "context_refs": refs, "facts": facts, "inferences": [str(x)[:240] for x in (raw.get("inferences") or [])[:6] if isinstance(x, str)],
        "ambiguity": ambiguity, "needs_review": needs or conf["overall"] < 0.4, "notes": notes, "source": "llm",
    }


def rules_direction(sid: str, speaker: str | None, cues: dict, sensitivity: float, text: str, note: str = "") -> dict:
    """Deterministic direction from the Pass 1 cues. Intensity is scaled by the sensitivity setting."""
    emo = cues.get("emotion", "neutral")
    raw_int = int(cues.get("intensity", 1))
    scale = 0.5 + sensitivity  # 0.5 .. 1.5
    inten = V.clamp_intensity(1 + (raw_int - 1) * scale)
    explicit = bool(cues.get("explicit"))
    if not explicit and sensitivity < 0.3 and emo not in ("neutral", "calm"):
        emo, inten = "neutral", 1
    tension = V.clamp_intensity(max(1, round(float(cues.get("tension", 0)))))
    ev = cues.get("evidence") or []
    facts = []
    for e in ev:
        m = re.search(r"'([^']+)'", e)
        if m and _grounded(m.group(1).rstrip("…"), _norm(text)):
            facts.append(m.group(1))
    delivery = {"slow": "unhurried", "measured": "measured and deliberate", "brisk": "quick and urgent", "fast": "rapid, urgent"}.get(cues.get("pace", "normal"), "")
    return {
        "segment_id": sid, "speaker": speaker or "Narrator", "voice_profile": "", "emotion": emo, "intensity": inten, "delivery": delivery if inten >= 3 else "",
        "pacing": cues.get("pace", "normal") if cues.get("pace") in V.PACINGS else "normal", "tension": tension,
        "confidence": {"speaker": 0.5, "emotion": 0.7 if explicit else 0.35, "overall": 0.6 if explicit else 0.35},
        "support": explicit, "context_refs": [], "facts": facts[:4],
        "inferences": ([f"Emotion '{emo}' inferred from: {', '.join(ev)}"] if ev else ["No emotional cue found; neutral delivery assumed"]),
        "ambiguity": None, "needs_review": False, "notes": [note] if note else [], "source": "rules",
    }


def build_prompt(p: dict) -> str:
    """The user message. ``p`` carries only analysis material; the segment text is quoted once under
    ``segment.text`` and is the only thing the performance is for."""
    payload = {
        "segment": {"id": p["sid"], "text": p["text"], "kind": p["kind"], "speaker": p.get("speaker"), "voice": p.get("voice_label")},
        "location": {"chapter": p.get("chapter"), "scene": p.get("scene")},
        "context_before": p["context"]["before"], "context_after": p["context"]["after"],
        "story_notes": p.get("notes") or [],
        "character_profile": p.get("profile") or {},
        "previous_segment": p.get("prev"), "speaker_state": p.get("speaker_state"), "upcoming": p.get("upcoming"),
        "user_instructions": p.get("hints") or "",
        "emotion_sensitivity": p.get("sensitivity"),
    }
    return "Direct this segment.\n" + json.dumps(payload, ensure_ascii=False, indent=1)


def cache_key(p: dict, model_label: str) -> str:
    prev = p.get("prev") or {}
    st = p.get("speaker_state") or {}
    basis = {
        "v": PROMPT_VERSION, "m": model_label, "t": p["text"], "b": p["context"]["before"], "a": p["context"]["after"],
        "n": p.get("notes") or [], "pf": p.get("profile") or {}, "sp": p.get("speaker"),
        "prev": [prev.get("speaker"), prev.get("emotion"), prev.get("intensity")], "st": [st.get("emotion"), st.get("intensity")],
        "up": p.get("upcoming"), "h": p.get("hints") or "", "s": round(float(p.get("sensitivity", 0.5)), 2),
    }
    return short_hash(dumps(basis), 24)


class Director:
    """Facade used by the analysis stage."""

    def __init__(self, llm: LLMClient | None):
        self.llm = llm if llm is not None and llm.enabled else None
        self.llm_failures = 0
        self.stats = {"llm_calls": 0, "cache_hits": 0, "rules": 0, "fallbacks": 0}

    @property
    def model_label(self) -> str:
        return self.llm.model_label if self.llm else "rules"

    def direct(self, p: dict, cache_get, cache_put) -> tuple[dict, str]:
        """-> (direction, cache_key)."""
        key = cache_key(p, self.model_label)
        cached = cache_get(key)
        if cached is not None:
            self.stats["cache_hits"] += 1
            return cached, key
        context = (p["context"]["before"] + " " + p["context"]["after"]).strip()
        cast_names = p.get("cast_names") or []
        if self.llm is not None and self.llm_failures < 3:
            try:
                d = self.llm.chat_json(SYSTEM_PROMPT, build_prompt(p),
                                       lambda raw: validate_direction(raw, sid=p["sid"], text=p["text"], context=context,
                                                                       known_speaker=p.get("speaker"), cast_names=cast_names))
                self.stats["llm_calls"] += 1
                self.llm_failures = 0
                cache_put(key, d, self.model_label)
                return d, key
            except (LLMError, DirectorError) as exc:
                self.llm_failures += 1
                self.stats["fallbacks"] += 1
                d = rules_direction(p["sid"], p.get("speaker"), p.get("cues") or {}, p.get("sensitivity", 0.5), p["text"],
                                    note=f"LLM unavailable or invalid ({str(exc)[:120]}); rules engine used")
                d["needs_review"] = True
                return d, key  # fallbacks are not cached, so a later run retries the LLM
        self.stats["rules"] += 1
        d = rules_direction(p["sid"], p.get("speaker"), p.get("cues") or {}, p.get("sensitivity", 0.5), p["text"])
        cache_put(key, d, "rules")
        return d, key
