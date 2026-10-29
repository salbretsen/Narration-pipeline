"""The two independent windows around a segment.

* The **narration window** is the only text ever handed to the TTS engine: the segment's own
  words (whitespace-normalised), never metadata, instructions or neighbouring text.
* The **narrative context window** is surrounding manuscript text for analysis only. It is
  clipped to the chapter, snapped to sentence starts/ends, and annotated with scene breaks.
"""
from __future__ import annotations

import re

SCENE_MARK = "[scene break]"
_SENT = re.compile(r"(?<=[.!?”\"])\s+")


def narration_window(text: str) -> str:
    return " ".join(text.split())


def _snap_start(text: str, lo: int, limit: int = 240) -> int:
    """Move a window start forward to a paragraph or sentence start (never mid-sentence if one is near)."""
    if lo <= 0:
        return 0
    seg = text[lo:lo + limit]
    m = re.search(r"\n\n", seg)
    if m:
        return lo + m.end()
    m = _SENT.search(seg)
    if m:
        return lo + m.end()
    m = re.search(r"\s", seg)
    return lo + m.end() if m else lo


def _snap_end(text: str, hi: int, limit: int = 240) -> int:
    if hi >= len(text):
        return len(text)
    lo = max(0, hi - limit)
    seg = text[lo:hi]
    cut = max(seg.rfind("\n\n"), -1)
    best = lo + cut if cut >= 0 else -1
    for m in _SENT.finditer(seg):
        best = max(best, lo + m.start())
    if best > lo:
        return best
    k = seg.rfind(" ")
    return lo + k if k > 0 else hi


def context_window(chapter_text: str, start: int, end: int, before: int, after: int, scene_starts: list[int] | None = None) -> dict:
    """Surrounding context for ``chapter_text[start:end]``. ``scene_starts`` are the offsets at which
    a new scene begins (used to mark scene breaks inside the window)."""
    b0 = _snap_start(chapter_text, max(0, start - before)) if before > 0 else start
    b0 = min(b0, start)
    a1 = _snap_end(chapter_text, min(len(chapter_text), end + after)) if after > 0 else end
    a1 = max(a1, end)
    marks = sorted(s for s in (scene_starts or []) if s > 0)

    def annotate(lo: int, hi: int) -> str:
        out, pos = [], lo
        for s in marks:
            if lo < s < hi:
                out.append(chapter_text[pos:s].rstrip())
                out.append(SCENE_MARK)
                pos = s
        out.append(chapter_text[pos:hi])
        return "\n\n".join(x for x in out if x.strip())

    return {
        "before": annotate(b0, start).strip() if before > 0 else "",
        "after": annotate(end, a1).strip() if after > 0 else "",
        "chapter_start": b0 <= 0 and start - before <= 0,
        "chapter_end": a1 >= len(chapter_text),
        "scene_break_before": any(b0 < s <= start for s in marks),
        "scene_break_after": any(end < s < a1 for s in marks),
        "before_chars": start - b0,
        "after_chars": a1 - end,
    }
