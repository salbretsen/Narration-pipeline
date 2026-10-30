"""The chapter beat sheet: one reasoning pass per chapter that plans the emotional shape before any segment is directed.

A thinking model reads the whole chapter (paragraphs numbered ``[P1]``, ``[P2]``...) and returns scenes and beats: which stretch of
paragraphs carries which emotion, intensity, pacing and tension, and why. The scene pass (director.py) then refines that plan per
segment. Paragraph numbers are used, never character offsets, so a plan survives re-segmentation of the chapter.

Paragraph numbering is 1-based and is the canonical chapter text split on blank lines (the same paragraphs the structure pass numbers,
where ``segments.para_idx`` / ``nar_spans.para_idx`` are 0-based). The plan is validated to cover every paragraph exactly once.

The beat sheet is advice, not authority: it never changes manuscript text, a failed or invalid plan is dropped (the scene pass then
works without it), and nothing is repaired by asking the model again (a thinking call costs minutes).
"""
from __future__ import annotations

import bisect
import json
import logging
import re
import time

from ..db import rows
from ..util import dumps, short_hash
from . import cast as C
from . import vocab as V
from .director import DirectorError, _int, _pause, normalise_emotion, normalise_pacing
from .llm import LLMError, extract_json, resolve_models

log = logging.getLogger("studio.beats")

BEATS_PROMPT_VERSION = "beats-1"
FAILURE_RETRY_S = 1200.0  # how long a failed chapter plan is left alone before it is asked for again

BEATS_SYSTEM_PROMPT = f"""You are the story editor for an audiobook production system. You read ONE chapter of a manuscript and plan how its emotion should rise and fall in the performance, before any line is recorded.

The chapter text is split into numbered paragraphs, marked [P1], [P2]... Plan in paragraph numbers.

Rules:
- Reply with ONE JSON object and nothing else (no commentary, no code fence).
- Never rewrite, add or summarise away manuscript text, and never invent events, motives or relationships that the chapter does not state. You only plan delivery.
- Divide the chapter into scenes (a change of place, time or situation), and each scene into beats (a stretch of paragraphs with one emotional purpose). The beats together must cover every paragraph, in order, with no gaps and no overlaps.
- Use the controlled vocabulary exactly. emotion is one of: {", ".join(V.EMOTIONS)}. pacing is one of: {", ".join(V.PACINGS)}. intensity and tension are integers 1-5.
- Keep restraint. Most of a chapter sits at intensity 1-3. Reserve 4-5 for the real peaks of the chapter and let the performance build to them.
- Prefer the user's confirmed character information and confirmed facts over anything you infer.
- "pause" is the silence to leave after the beat: "none", "beat" (short) or "long" (a scene turn or a shock).

JSON shape:
{{"arc": str (at most 40 words: the emotional shape of the whole chapter),
 "scenes": [{{"paras": [first, last], "summary": str (at most 30 words, only what the text states),
   "beats": [{{"paras": [first, last], "emotion": str, "intensity": int, "pacing": str, "tension": int, "purpose": str (at most 20 words), "pause": "none|beat|long"}}]}}]}}"""


class BeatsError(DirectorError):
    pass


# ------------------------------------------------------------------------------ paragraphs


def paragraph_ranges(text: str) -> list[tuple[int, int]]:
    """(start, end) of each paragraph of the canonical chapter text; paragraphs are separated by blank lines."""
    out, pos = [], 0
    for m in [*re.finditer(r"\n\s*\n", text), None]:
        end = m.start() if m else len(text)
        seg = text[pos:end]
        if seg.strip():
            lead = len(seg) - len(seg.lstrip())
            out.append((pos + lead, pos + len(seg.rstrip())))
        if m:
            pos = m.end()
    return out or [(0, len(text))]


def paragraph_starts(text: str) -> list[int]:
    return [s for s, _ in paragraph_ranges(text)]


def paragraphs(text: str) -> list[str]:
    return [text[s:e] for s, e in paragraph_ranges(text)]


def para_number(starts: list[int], offset: int) -> int:
    """1-based number of the paragraph containing a chapter-text offset."""
    return max(1, bisect.bisect_right(starts, offset))


def marked_text(text: str) -> tuple[str, int]:
    """-> (the chapter with a [Pn] marker in front of each paragraph, the paragraph count)."""
    paras = paragraphs(text)
    return "\n\n".join(f"[P{i}] {p}" for i, p in enumerate(paras, 1)), len(paras)


# ---------------------------------------------------------------------------------- prompt


def cast_basis(chars: list[dict]) -> list[dict]:
    """The cast as the beat sheet sees it: names and USER-CONFIRMED profile fields only (inferred fields are not evidence)."""
    out = []
    for c in chars:
        if c.get("kind") == "narrator" or c.get("archived"):
            continue
        prof = C.profile_for_prompt(c)
        out.append({"name": c["name"], "aliases": prof.get("aliases", [])[:4], "confirmed": prof.get("confirmed", {})})
    return sorted(out, key=lambda x: x["name"].lower())


def build_beats_prompt(chapter_marked: str, cast: list[dict], facts: list[str], title: str = "", n_paras: int | None = None) -> str:
    """The user message. Starts with the header the dispatch (and the test stub) keys on."""
    head = {"chapter_title": title, "paragraphs": n_paras, "cast": cast, "confirmed_facts": facts}
    return ("Chapter beat sheet.\n" + json.dumps(head, ensure_ascii=False, indent=1)
            + "\n\nChapter text (paragraphs are marked [P1], [P2]...):\n\n" + chapter_marked)


def beats_cache_key(chapter_sha256: str, cast: list[dict], facts: list[str], model_label: str) -> str:
    return short_hash(dumps({"v": BEATS_PROMPT_VERSION, "sha": chapter_sha256, "cast": cast, "facts": facts, "m": model_label}), 24)


# ------------------------------------------------------------------------------ validation


def _parse_range(v) -> tuple[int, int] | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v), int(v)
    if isinstance(v, str):
        nums = re.findall(r"\d+", v)
        if len(nums) == 1:
            return int(nums[0]), int(nums[0])
        if len(nums) >= 2:
            return int(nums[0]), int(nums[1])
        return None
    if isinstance(v, (list, tuple)) and v:
        try:
            a = int(float(str(v[0]).lstrip("Pp")))
            b = int(float(str(v[-1]).lstrip("Pp")))
        except (TypeError, ValueError):
            return None
        return a, b
    return None


def _clean(s, n: int) -> str:
    return " ".join(str(s or "").split())[:n]


def validate_beats(raw, n_paras: int) -> dict:
    """Normalise a beat sheet reply into a plan that covers paragraphs 1..n_paras exactly once.

    * emotion/pacing go through the Director's synonyms and vocabulary; a beat whose emotion is outside it is dropped;
    * ranges are clipped to 1..n_paras; overlaps and gaps are resolved by letting each beat run until the next one starts (a later
      start wins), and the first beat is extended back to paragraph 1;
    * raises BeatsError when nothing usable is left.

    Plan shape: ``{"arc", "n_paras", "scenes": [{"paras": [a, b], "summary", "beats": [{"paras": [a, b], "emotion", "intensity",
    "pacing", "tension", "purpose", "pause"}]}]}``."""
    if n_paras < 1:
        raise BeatsError("the chapter has no paragraphs")
    if not isinstance(raw, dict):
        raise BeatsError("reply is not a JSON object")
    scenes_in = raw.get("scenes")
    if not isinstance(scenes_in, list) or not scenes_in:
        raise BeatsError("reply has no scenes")
    flat: list[dict] = []
    summaries: dict[int, str] = {}
    for sc_no, sc in enumerate(scenes_in):
        if not isinstance(sc, dict):
            continue
        summaries[sc_no] = _clean(sc.get("summary"), 240)
        for b in sc.get("beats") or []:
            if not isinstance(b, dict):
                continue
            rng = _parse_range(b.get("paras"))
            if rng is None:
                continue
            a, z = sorted(rng)
            if z < 1 or a > n_paras:
                continue
            try:
                emo = normalise_emotion(b.get("emotion"))
                pacing = normalise_pacing(b.get("pacing", "normal"))
            except DirectorError:
                continue
            flat.append({"scene": sc_no, "start": max(1, a), "end": min(n_paras, z), "emotion": emo, "intensity": _int(b.get("intensity")),
                         "pacing": pacing, "tension": _int(b.get("tension"), default=2), "purpose": _clean(b.get("purpose"), 160),
                         "pause": _pause(b.get("pause"))})
    if not flat:
        raise BeatsError("reply has no usable beats")
    flat.sort(key=lambda b: b["start"])  # stable: equal starts stay in reply order
    kept: list[dict] = []
    for b in flat:
        if kept and kept[-1]["start"] == b["start"]:
            kept[-1] = b  # the later of two beats starting together wins
        else:
            kept.append(b)
    kept[0]["start"] = 1
    for k, b in enumerate(kept):
        b["end"] = kept[k + 1]["start"] - 1 if k + 1 < len(kept) else n_paras
    scenes: dict[int, dict] = {}
    for b in kept:
        sc = scenes.setdefault(b["scene"], {"summary": summaries.get(b["scene"], ""), "beats": []})
        sc["beats"].append({"paras": [b["start"], b["end"]], **{k: b[k] for k in ("emotion", "intensity", "pacing", "tension", "purpose", "pause")}})
    out_scenes = []
    for sc in sorted(scenes.values(), key=lambda s: s["beats"][0]["paras"][0]):
        out_scenes.append({"paras": [sc["beats"][0]["paras"][0], sc["beats"][-1]["paras"][1]], "summary": sc["summary"], "beats": sc["beats"]})
    return {"arc": _clean(raw.get("arc"), 300), "n_paras": n_paras, "scenes": out_scenes}


def plan_beats(plan: dict) -> list[dict]:
    """The plan's beats in order, each with its scene's summary and its global index."""
    out = []
    for si, sc in enumerate(plan.get("scenes") or []):
        for b in sc["beats"]:
            out.append({**b, "scene_no": si, "scene_summary": sc.get("summary", ""), "index": len(out)})
    return out


def beat_for(plan: dict | None, para: int) -> dict | None:
    """The beat containing paragraph ``para`` (1-based, the [Pn] numbering), with ``index``, ``scene_no`` and ``scene_summary`` added."""
    if not plan:
        return None
    for b in plan_beats(plan):
        if b["paras"][0] <= para <= b["paras"][1]:
            return b
    return None


def beat_at(plan: dict | None, index: int) -> dict | None:
    flat = plan_beats(plan) if plan else []
    return flat[index] if 0 <= index < len(flat) else None


# ------------------------------------------------------------------------------ the pass


def ensure_chapter_plan(ctx, book: dict, chapter: dict, client, ns, cache_get, cache_put, stats: dict | None = None) -> dict | None:
    """The beat sheet for one chapter: cached, or planned now with ONE thinking request. ``chapter`` carries ``id``, ``idx``, ``text`` and
    (optionally) ``sha256`` and ``title``. Returns None when planning is off, there is no LLM, the chapter is a single paragraph, or the
    request/reply failed (logged; the scene pass then runs without a plan). No write lock is held during the request, and a failed or
    invalid reply is never retried or cached."""
    st = stats if stats is not None else {}

    def bump(k: str, n: int = 1):
        st[k] = st.get(k, 0) + n

    if client is None or not client.enabled or not getattr(ns, "director_chapter_plan", False):
        return None
    text = chapter["text"]
    marked, n_paras = marked_text(text)
    if n_paras < 2:
        return None
    with ctx.db.session() as conn:
        chars = C.list_characters(conn, book["id"])
        facts = [r["text"][:300] for r in rows(
            conn, "SELECT text FROM story_notes WHERE book_id=? AND kind='fact' AND confirmed=1 AND (chapter_idx IS NULL OR chapter_idx=?) ORDER BY id",
            (book["id"], chapter.get("idx")))][:20]
    cast = cast_basis(chars)
    cfg = client.cfg
    model = resolve_models(cfg)["chapter"]
    pass_client = client.derive(model=model, thinking=True, max_tokens=cfg.chapter_max_tokens, timeout_s=cfg.chapter_timeout_s)
    sha = chapter.get("sha256") or short_hash(text, 32)
    key = "beats:" + beats_cache_key(sha, cast, facts, pass_client.model_label)
    cached = cache_get(key)
    if isinstance(cached, dict) and cached.get("scenes") and cached.get("n_paras") == n_paras:
        bump("chapter_plans")
        bump("chapter_plan_cache_hits")
        return cached
    failed = cache_get("beats-fail:" + key)  # a failed plan is not retried for a while: every later window of the chapter would otherwise ask again
    if isinstance(failed, dict) and time.time() - float(failed.get("failed_at", 0)) < FAILURE_RETRY_S:
        bump("chapter_plan_failures")
        return None
    try:
        reply = pass_client.chat(BEATS_SYSTEM_PROMPT, build_beats_prompt(marked, cast, facts, chapter.get("title") or "", n_paras), json_mode=False)
        bump("reasoning_chars", len(pass_client.last_reasoning))
        plan = validate_beats(extract_json(reply), n_paras)
    except (LLMError, ValueError) as exc:  # BeatsError is a ValueError
        log.warning("chapter %s beat sheet failed: %s", chapter.get("idx"), str(exc)[:300])
        bump("reasoning_chars", len(pass_client.last_reasoning))
        bump("chapter_plan_failures")
        cache_put("beats-fail:" + key, {"failed_at": time.time(), "error": str(exc)[:300]}, pass_client.model_label)
        return None
    plan["model"] = pass_client.model_label
    cache_put(key, plan, pass_client.model_label)
    bump("chapter_plans")
    return plan
