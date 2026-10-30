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
from .llm import LLMClient, LLMError, extract_json

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


def normalise_emotion(value) -> str:
    """A reply's emotion word mapped into the controlled vocabulary (synonyms allowed). Raises DirectorError otherwise."""
    emo = str(value if value is not None else "").strip().lower()
    emo = SYNONYMS.get(emo, emo)
    if emo not in V.EMOTIONS:
        raise DirectorError(f"emotion {value!r} is not in the controlled vocabulary")
    return emo


PACING_SYNONYMS = {"slowly": "slow", "quick": "brisk", "quickly": "brisk", "medium": "normal", "moderate": "normal", "steady": "measured"}


def normalise_pacing(value) -> str:
    pacing = str(value if value is not None else "normal").strip().lower()
    pacing = PACING_SYNONYMS.get(pacing, pacing)
    if pacing not in V.PACINGS:
        raise DirectorError(f"pacing {value!r} is not in the controlled vocabulary")
    return pacing


PAUSES = ("none", "beat", "long")


def _pause(value) -> str:
    v = str(value if value is not None else "none").strip().lower()
    return v if v in PAUSES else "none"


def _bool(value) -> bool:
    """A JSON-ish truth value: models sometimes answer "false" as a string."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "yes", "1")
    return bool(value)


def validate_direction(raw: dict, *, sid: str, text: str, context: str, known_speaker: str | None, cast_names: list[str]) -> dict:
    """Normalise and check an LLM reply. Raises DirectorError on anything unusable; repairs and
    annotates the rest (nothing is silently trusted)."""
    if not isinstance(raw, dict):
        raise DirectorError("reply is not a JSON object")
    notes: list[str] = []
    emo = normalise_emotion(raw.get("emotion"))
    pacing = normalise_pacing(raw.get("pacing", "normal"))
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
        "pause": _pause(raw.get("pause")),
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
        "ambiguity": None, "needs_review": False, "notes": [note] if note else [], "source": "rules", "pause": "none",
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


# ----------------------------------------------------------------------- scene-batched direction
#
# One request directs a run of consecutive segments of one scene. The chapter beat sheet (beats.py), when there is one, supplies the
# baseline; the scene pass refines it per segment. Continuity smoothing still runs afterwards, so the schema stored per segment is
# exactly the one the single-segment engine produces.

SCENE_PROMPT_VERSION = "scene-1"

SCENE_SYSTEM_PROMPT = f"""You are the Narrative Director for an audiobook production system. You decide how a run of consecutive segments from ONE scene of a manuscript should be performed by a text-to-speech narrator or character voices.

Rules:
- Reply with ONE JSON object and nothing else: {{"items": [...]}} with exactly one entry per segment, in order, using the segment's "id".
- Never rewrite, add, shorten or translate manuscript text. You only describe delivery.
- Use the controlled vocabulary exactly. emotion is one of: {", ".join(V.EMOTIONS)}. pacing is one of: {", ".join(V.PACINGS)}. intensity and tension are integers 1-5.
- When "beats" are given, a segment's "beat" names the planned beat for it: treat that beat's emotion, intensity, pacing and tension as the baseline and deviate only when the segment's own text clearly calls for it. Keep restraint: reserve intensity 4-5 for real peaks.
- Respect continuity: "previous_delivery" is how the segments just before this run were performed. It persists unless the text gives a reason to change. Set "change" to true ONLY when the manuscript itself signals an abrupt change (a shout, a revelation, a scene turn).
- Prefer the user's confirmed character information over anything you infer. Do not invent character traits or events.
- "delivery" is a short natural performance note (at most 12 words) such as "voice breaking, barely above a whisper". It must not quote the manuscript.
- "pause" is the silence to leave after the segment: "none", "beat" (a short pause) or "long".
- "confidence" is 0-1. Set "review" to true when the emotion is genuinely unclear instead of guessing confidently.

JSON shape:
{{"items": [{{"id": str, "emotion": str, "intensity": int, "pacing": str, "tension": int, "delivery": str, "change": bool, "pause": "none|beat|long", "confidence": 0-1, "review": bool}}]}}"""


def scene_ids(items: list[dict]) -> list[str]:
    """Window-local ids: short, so the model echoes them reliably. Position in the window, 1-based."""
    return [str(i + 1) for i in range(len(items))]


def _beat_sig(beat: dict | None) -> dict | None:
    if not beat:
        return None
    return {k: beat.get(k) for k in ("emotion", "intensity", "pacing", "tension", "purpose", "pause")}


def build_scene_prompt(items: list[dict], scene_ctx: dict, only: list[str] | None = None, feedback: str | None = None) -> str:
    """The user message for one window. ``items`` are the per-segment payloads in order; ``scene_ctx`` carries the scene summary, story
    facts, the text just before and after the window (confined to the scene) and the previous segments' final delivery. ``only`` limits
    the request to some ids (the re-ask); ``feedback`` says why the previous reply was rejected."""
    ids = scene_ids(items)
    beat_labels: dict[int, str] = {}
    beats: dict[str, dict] = {}
    for p in items:
        b = p.get("beat")
        if b and b.get("index") not in beat_labels:
            beat_labels[b["index"]] = f"B{len(beat_labels) + 1}"
            beats[beat_labels[b["index"]]] = _beat_sig(b)
    segments = []
    for sid_local, p in zip(ids, items):
        if only is not None and sid_local not in only:
            continue
        b = p.get("beat")
        seg = {"id": sid_local, "speaker": p.get("speaker") or "Narrator", "kind": p.get("kind"), "text": p["text"]}
        if b:
            seg["beat"] = beat_labels[b["index"]]
        segments.append(seg)
    cast: dict[str, dict] = {}
    hints: dict[str, str] = {}
    for p in items:
        name = p.get("speaker")
        if name and p.get("profile") and name not in cast:
            cast[name] = p["profile"]
        if name and p.get("hints") and name not in hints:
            hints[name] = p["hints"]
    payload = {
        "scene": {"chapter": scene_ctx.get("chapter"), "scene": scene_ctx.get("scene"), "summary": scene_ctx.get("summary") or "", "chapter_arc": scene_ctx.get("arc") or ""},
        "story_facts": scene_ctx.get("facts") or [],
        "beats": beats,
        "cast": cast,
        "previous_delivery": scene_ctx.get("previous") or [],
        "context_before": scene_ctx.get("before") or "",
        "context_after": scene_ctx.get("after") or "",
        "user_instructions": hints,
        "emotion_sensitivity": items[0].get("sensitivity") if items else None,
        "segments": segments,
    }
    msg = "Direct this scene.\n" + json.dumps(payload, ensure_ascii=False, indent=1)
    if feedback:
        msg += f"\n\nYour previous reply was rejected: {feedback[:300]}\nReply again with ONLY the corrected JSON object, containing entries for exactly these segment ids: {', '.join(only or ids)}."
    return msg


def scene_cache_key(items: list[dict], scene_ctx: dict, model_label: str) -> str:
    """Over everything in the prompt that describes THIS window. The previous delivery is deliberately not part of it (it would chain every
    scene to the one before, so one edit would invalidate the rest of the book); continuity smoothing applies it afterwards."""
    basis = {
        "v": SCENE_PROMPT_VERSION, "m": model_label,
        "it": [[p["text"], p.get("speaker"), p.get("kind"), _beat_sig(p.get("beat")), p.get("hints") or "", p.get("profile") or {}] for p in items],
        "sum": scene_ctx.get("summary") or "", "arc": scene_ctx.get("arc") or "", "facts": scene_ctx.get("facts") or [],
        "b": scene_ctx.get("before") or "", "a": scene_ctx.get("after") or "",
        "s": round(float(items[0].get("sensitivity", 0.5)), 2) if items else 0.5,
    }
    return short_hash(dumps(basis), 24)


def _scene_item(it: dict, c: dict) -> dict:
    emo = normalise_emotion(it.get("emotion"))
    pacing = normalise_pacing(it.get("pacing", "normal"))
    delivery = " ".join(str(it.get("delivery") or "").replace('"', "").replace("\n", " ").split()[:25])[:240]
    notes: list[str] = []
    delivery, changed = leak_guard(delivery, [c["text"], c.get("context", "")])
    if changed:
        notes.append("delivery text quoted the manuscript and was trimmed")
    try:
        conf = round(max(0.0, min(1.0, float(it.get("confidence", 0.5)))), 2)
    except (TypeError, ValueError):
        conf = 0.5
    review = _bool(it.get("review", False))
    return {
        "segment_id": c["sid"], "speaker": c.get("speaker") or "", "voice_profile": "", "emotion": emo, "intensity": _int(it.get("intensity")),
        "delivery": delivery, "pacing": pacing, "tension": _int(it.get("tension"), default=2),
        "confidence": {"speaker": conf, "emotion": conf, "overall": conf}, "support": _bool(it.get("change", False)) and conf >= 0.6,
        "context_refs": [], "facts": [], "inferences": [],
        "ambiguity": "The Director marked this segment for review." if review else None,
        "needs_review": review or conf < 0.4, "notes": notes, "source": "llm", "pause": _pause(it.get("pause")),
    }


def validate_scene_items(raw, expected_ids: list[str], ctx_by_id: dict[str, dict]) -> tuple[dict[str, dict], list[str]]:
    """Validate a scene reply item by item. -> (valid items by id, normalised into the full direction schema; ids that failed).
    Raises DirectorError only when the reply as a whole is unusable (not an object, no items list)."""
    if not isinstance(raw, dict):
        raise DirectorError("reply is not a JSON object")
    items = raw.get("items")
    if isinstance(items, dict):  # tolerate {"1": {...}, "2": {...}}
        items = [{"id": k, **v} for k, v in items.items() if isinstance(v, dict)]
    if not isinstance(items, list):
        raise DirectorError("reply has no 'items' list")
    wanted = set(expected_ids)
    ok: dict[str, dict] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id", "")).strip()
        if iid not in wanted or iid in ok:
            continue
        try:
            ok[iid] = _scene_item(it, ctx_by_id[iid])
        except (DirectorError, TypeError, ValueError):
            continue
    return ok, [i for i in expected_ids if i not in ok]


class Director:
    """Facade used by the analysis stage."""

    def __init__(self, llm: LLMClient | None):
        self.llm = llm if llm is not None and llm.enabled else None
        self.llm_failures = 0
        self.stats = {"llm_calls": 0, "cache_hits": 0, "rules": 0, "fallbacks": 0,
                      "scene_calls": 0, "would_escalate": 0, "chapter_plans": 0, "chapter_plan_failures": 0,
                      "translations": 0, "translation_cache_hits": 0, "translation_failures": 0}

    def direct_scene(self, items: list[dict], scene_ctx: dict, cache_get, cache_put, client_scene: LLMClient | None = None) -> dict[str, tuple[dict, str]]:
        """Direct a window of consecutive segments of one scene with ONE request. -> {sid: (direction, cache_key)}.
        Failing ids are asked again once (only those); what still fails falls back to the rules engine with needs_review."""
        if not items:
            return {}
        llm = client_scene if client_scene is not None and client_scene.enabled else self.llm
        if llm is None:  # no LLM at all: the per-segment rules engine, cached per segment as before
            return {p["sid"]: self.direct(p, cache_get, cache_put) for p in items}
        label = llm.model_label
        key = scene_cache_key(items, scene_ctx, label)
        cached = cache_get(key)
        if isinstance(cached, dict) and isinstance(cached.get("items"), list) and len(cached["items"]) == len(items):
            self.stats["cache_hits"] += len(items)  # counted per segment, like the single-segment cache
            return {p["sid"]: ({**d, "segment_id": p["sid"]}, key) for p, d in zip(items, cached["items"])}
        ids = scene_ids(items)
        around = ((scene_ctx.get("before") or "") + " " + (scene_ctx.get("after") or "") + " " + " ".join(p["text"] for p in items)).strip()
        ctx_by_id = {i: {"sid": p["sid"], "text": p["text"], "speaker": p.get("speaker"), "context": around} for i, p in zip(ids, items)}
        got: dict[str, dict] = {}
        reasons: dict[str, str] = {}
        if self.llm_failures >= 3:
            reasons = {i: "the LLM was paused after repeated failures" for i in ids}
        else:
            self.stats["scene_calls"] += 1
            pending, feedback = list(ids), None
            for _attempt in range(2):
                try:
                    tokens = int(min(8000, max(llm.cfg.max_tokens, 120 * len(pending) + 200)))
                    reply = llm.derive(max_tokens=tokens).chat(SCENE_SYSTEM_PROMPT, build_scene_prompt(items, scene_ctx, only=pending if feedback else None, feedback=feedback))
                except LLMError as exc:  # unreachable or refused: asking again straight away would not help
                    feedback = str(exc)
                    break
                self.stats["llm_calls"] += 1
                try:
                    ok, bad = validate_scene_items(extract_json(reply), pending, ctx_by_id)
                    feedback = "these ids were missing, duplicated or outside the controlled vocabulary"
                except ValueError as exc:  # also DirectorError
                    ok, bad, feedback = {}, list(pending), str(exc)
                got.update(ok)
                pending = bad
                if not pending:
                    break
            reasons = {i: feedback or "invalid reply" for i in ids if i not in got}
            if not got:
                self.llm_failures += 1
            else:
                self.llm_failures = 0
        out: dict[str, tuple[dict, str]] = {}
        for i, p in zip(ids, items):
            if i in got:
                out[p["sid"]] = (got[i], key)
                continue
            self.stats["fallbacks"] += 1
            d = rules_direction(p["sid"], p.get("speaker"), p.get("cues") or {}, p.get("sensitivity", 0.5), p["text"],
                                note=f"LLM unavailable or invalid ({reasons.get(i, '')[:120]}); rules engine used")
            d["needs_review"] = True
            out[p["sid"]] = (d, key)
        if not reasons:  # every item is valid LLM output: the whole window is cached
            cache_put(key, {"items": [out[p["sid"]][0] for p in items]}, label)
        return out

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
