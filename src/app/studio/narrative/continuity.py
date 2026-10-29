"""Cross-segment emotional continuity.

A *delivery continuity state* is kept per speaker (and read per scene). It is a lightweight record -
last emotion, intensity, pacing and tension of that speaker - that biases the next direction without
locking the character into an emotion:

* an unsupported return to neutral after a strong emotion decays instead of snapping back;
* intensity moves by a bounded step unless the manuscript signals an abrupt change;
* an escalating scene (rising tension, same emotion family) is allowed to keep climbing;
* abrupt changes the manuscript supports (a shout, a revelation) are never smoothed away;
* scene and chapter boundaries reset or decay the state (setting ``reset_scope``).

Smoothing is deterministic and keeps the raw direction alongside the smoothed one, so every
adjustment is auditable.
"""
from __future__ import annotations

from . import vocab as V

FIELDS = ("emotion", "intensity", "pacing", "tension")


def new_state() -> dict:
    return {"speakers": {}, "scene": None, "chapter": None}


def _step_toward(cur: str, target: str) -> str:
    a, b = V.PACINGS.index(cur), V.PACINGS.index(target)
    return V.PACINGS[a + (1 if b > a else -1)] if a != b else cur


def apply(raw: dict, state: dict, *, speaker: str, scene: int, chapter: int, strength: float, enabled: bool, reset_scope: str,
          locked: bool = False) -> tuple[dict, dict, str, str]:
    """Smooth one raw direction against the running state.

    -> (smoothed fields, new state, note, kind) where kind is one of
       ``first`` | ``unchanged`` | ``intentional`` | ``smoothed`` | ``reset`` | ``user``.
    The returned state is a new dict; the input state is not mutated."""
    st = {"speakers": {k: dict(v) for k, v in state.get("speakers", {}).items()}, "scene": scene, "chapter": chapter}
    note, kind = "", "first"
    out = {k: raw[k] for k in FIELDS}
    chapter_changed = state.get("chapter") is not None and state.get("chapter") != chapter
    scene_changed = state.get("scene") is not None and state.get("scene") != scene and not chapter_changed
    if (chapter_changed and reset_scope in ("scene", "chapter")) or (scene_changed and reset_scope == "scene"):
        st["speakers"] = {}
        note, kind = ("state reset at a chapter start" if chapter_changed else "state reset at a scene break"), "reset"
    elif scene_changed and reset_scope == "chapter":
        for v in st["speakers"].values():  # a new scene: emotions fade but do not vanish
            v["intensity"] = max(1, int(v["intensity"]) - 1)
    prev = st["speakers"].get(speaker)

    if locked:
        kind, note = "user", "user override; used as-is and as the new state"
    elif not enabled or prev is None:
        if prev is None and kind != "reset":
            note, kind = "first delivery for this speaker in the current span", "first"
    else:
        support = bool(raw.get("support"))
        s = max(0.0, min(1.0, strength))
        limit = max(1, round(4 - 3 * s))
        if support and raw["emotion"] != prev["emotion"]:
            kind, note = "intentional", f"kept {prev['emotion']}->{raw['emotion']}: the text signals the change"
        else:
            changes = []
            # 1. do not fall to neutral without a reason
            if raw["emotion"] in ("neutral", "calm") and prev["emotion"] not in ("neutral", "calm") and int(prev["intensity"]) >= 3 and not support:
                out["emotion"] = prev["emotion"]
                out["intensity"] = max(int(raw["intensity"]), int(prev["intensity"]) - 1)
                changes.append(f"carried {prev['emotion']} (was {prev['intensity']}) instead of dropping to {raw['emotion']}")
            # 2. bounded intensity step
            tgt = int(out["intensity"])
            if not support and abs(tgt - int(prev["intensity"])) > limit:
                tgt = int(prev["intensity"]) + (limit if tgt > int(prev["intensity"]) else -limit)
                changes.append(f"intensity step limited to {limit}")
            # 3. escalation keeps climbing
            if (out["emotion"] == prev["emotion"] and int(raw["tension"]) > int(prev["tension"])
                    and tgt <= int(prev["intensity"]) and out["emotion"] not in ("neutral", "calm")):
                tgt = min(5, int(prev["intensity"]) + 1)
                changes.append("tension is rising, so intensity continues to build")
            out["intensity"] = V.clamp_intensity(tgt)
            # 4. pacing moves one notch at a time
            if not support and raw["pacing"] != prev["pacing"] and abs(V.PACINGS.index(raw["pacing"]) - V.PACINGS.index(prev["pacing"])) > 1:
                out["pacing"] = _step_toward(prev["pacing"], raw["pacing"])
                changes.append("pacing eased one step")
            if changes:
                kind, note = "smoothed", "; ".join(changes)
            else:
                kind, note = "unchanged", "consistent with the previous delivery"
    st["speakers"][speaker] = {**{k: out[k] for k in FIELDS}, "scene": scene}
    return out, st, note, kind


def final_of(raw: dict, smoothed: dict, user: dict) -> dict:
    """Effective direction: user override > smoothed > raw. Free-text delivery comes from the user or the raw reply."""
    f = {k: smoothed.get(k, raw.get(k)) for k in FIELDS}
    f["delivery"] = raw.get("delivery", "")
    for k, v in (user or {}).items():
        if v not in (None, ""):
            f[k] = v
    return f
