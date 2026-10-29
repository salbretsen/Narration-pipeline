"""Pass 2 - narration segment construction from the Pass 1 structure.

Every segment is an exact ``[start, end)`` slice of the chapter's canonical text, so wording,
punctuation and capitalisation are preserved by construction and the segments of a chapter
cover every non-whitespace character exactly once, in order. The maximum length is a ceiling:
a segment ends at the first natural boundary (paragraph, speaker change, scene, emotional turn)
once it is long enough, rather than being filled up to the limit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from ..chunker import ABBREVIATIONS
from .structure import ChapterStructure, Span

_BOUNDARY = re.compile(r"([.!?…]+[\"'”’)\]]*)(\s+)")
_STRONG = re.compile(r"[;:—–](?=\s)")
_CONJ = re.compile(r",(?=\s+(?:and|but|or|so|yet|which|who|whom|whose|while|when|because|although|though|if|then|until|as|where|before|after)\b)", re.I)
_COMMA = re.compile(r",(?=\s)")
_HARD_LIMIT = 1200  # a user-forced join may exceed the ceiling, never this


@dataclass
class Atom:
    start: int
    end: int
    para: int
    scene: int
    kind: str  # narration | dialogue | heading
    speaker: str | None
    voice_key: str
    shift: float = 0.0
    needs_review: bool = False
    first_of_span: bool = True


@dataclass
class Seg:
    start: int
    end: int
    text: str
    kind: str  # body | heading
    para_idx: int
    scene_idx: int
    speaker: str | None
    span_kind: str  # narration | dialogue | mixed | heading
    voice_key: str
    needs_review: bool = False
    pause_kind: str = "sentence"
    manual: bool = False
    atoms: list[Atom] = field(default_factory=list, repr=False)


def sentence_ranges(text: str, base: int = 0) -> list[tuple[int, int]]:
    """Sentence boundaries of ``text`` as absolute ``(start, end)`` ranges (whitespace trimmed)."""
    out: list[tuple[int, int]] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        end = m.end(1)
        word = text[max(start, end - 14):end].split(" ")[-1]
        tok = re.findall(r"([A-Za-z][A-Za-z.]*)\.+$", word)
        token = tok[0].lower().rstrip(".") if tok else ""
        nxt = text[m.end():m.end() + 1]
        if m.group(1).startswith(".") and (token in ABBREVIATIONS or re.fullmatch(r"[a-z]", token)):
            continue
        if nxt and nxt.islower():
            continue
        out.append((base + start, base + end))
        start = m.end()
    if text[start:].strip():
        out.append((base + start, base + len(text.rstrip())))
    return out


def _best_split(text: str, lo: int, hi: int, max_chars: int) -> int:
    """Offset (within text[lo:hi]) of the least disruptive split. Higher-priority punctuation is
    preferred as long as neither side is a tiny orphan; otherwise the nearest word boundary to
    the middle is used."""
    seg = text[lo:hi]
    n = len(seg)
    mid = n / 2

    def pick(positions: list[int]) -> int | None:
        ok = [p for p in positions if 0.2 * n <= p <= 0.8 * n or (p <= max_chars and n - p <= max_chars and min(p, n - p) >= 0.12 * n)]
        return min(ok, key=lambda p: abs(p - mid)) if ok else None

    for rx in (_STRONG, _CONJ, _COMMA):
        pos = [m.end() for m in rx.finditer(seg)]
        got = pick(pos)
        if got is not None:
            return got
    spaces = [m.start() for m in re.finditer(r"\s", seg) if 0 < m.start() < n - 1]
    return min(spaces, key=lambda p: abs(p - mid)) if spaces else n


def split_long(text: str, start: int, end: int, max_chars: int) -> list[tuple[int, int]]:
    """Split an over-long sentence into pieces of at most ``max_chars`` (a single word longer than
    that is kept whole)."""
    if end - start <= max_chars:
        return [(start, end)]
    cut = start + _best_split(text, start, end, max_chars)
    left_end = cut
    while left_end > start and text[left_end - 1].isspace():
        left_end -= 1
    right_start = cut
    while right_start < end and text[right_start].isspace():
        right_start += 1
    if left_end <= start or right_start >= end:
        return [(start, end)]
    return split_long(text, start, left_end, max_chars) + split_long(text, right_start, end, max_chars)


def _apply_cuts(atoms: list[Atom], cuts: set[int], text: str) -> list[Atom]:
    """Split any atom that contains a manual cut offset strictly inside it."""
    if not cuts:
        return atoms
    out: list[Atom] = []
    for a in atoms:
        pieces = [a]
        for c in sorted(x for x in cuts if a.start < x < a.end):
            last = pieces[-1]
            if not last.start < c < last.end:
                continue
            left_end = c
            while left_end > last.start and text[left_end - 1].isspace():
                left_end -= 1
            if left_end <= last.start:
                continue
            right = Atom(c, last.end, last.para, last.scene, last.kind, last.speaker, last.voice_key, 0.0, last.needs_review, False)
            pieces[-1] = Atom(last.start, left_end, last.para, last.scene, last.kind, last.speaker, last.voice_key, last.shift, last.needs_review, last.first_of_span)
            pieces.append(right)
        out.extend(pieces)
    return out


def _atoms(struct: ChapterStructure, max_chars: int, voice_key_of: Callable[[str | None], str], tags_with_speaker: bool = False) -> list[Atom]:
    text = struct.text
    out: list[Atom] = []
    for sp in struct.spans:
        vk = voice_key_of(sp.speaker if sp.kind == "dialogue" else (sp.tag_speaker if tags_with_speaker and sp.is_tag else None))
        if sp.kind == "heading":
            out.append(Atom(sp.start, sp.end, sp.para_idx, sp.scene_idx, "heading", None, voice_key_of(None)))
            continue
        shift = float(sp.cues.get("shift", 0.0))
        pieces: list[tuple[int, int]] = []
        if sp.end - sp.start <= max_chars:
            pieces = [(sp.start, sp.end)]
        else:
            for a, b in sentence_ranges(text[sp.start:sp.end], sp.start):
                pieces.extend(split_long(text, a, b, max_chars))
        for i, (a, b) in enumerate(pieces):
            out.append(Atom(a, b, sp.para_idx, sp.scene_idx, sp.kind, sp.speaker if sp.kind == "dialogue" else None, vk,
                            shift if i == 0 else 0.0, sp.needs_review, i == 0))
    return out


def build(struct: ChapterStructure, *, max_chars: int = 500, min_chars: int = 120, sensitivity: float = 0.5,
          voice_key_of: Callable[[str | None], str] = lambda s: "narrator", cuts: set[int] | None = None,
          joins: set[int] | None = None, tags_with_speaker: bool = False) -> list[Seg]:
    cuts, joins = cuts or set(), joins or set()
    text = struct.text
    atoms = _apply_cuts(_atoms(struct, max_chars, voice_key_of, tags_with_speaker), cuts, text)
    shift_gate = 1.0 - sensitivity if sensitivity > 0 else 9.0

    def barrier(prev: Atom, a: Atom, cur_len: int, shift_ok: bool = True) -> bool:
        if prev.kind == "heading" or a.kind == "heading" or prev.scene != a.scene:
            return True
        if a.start in cuts:
            return True
        if a.start in joins:
            return False
        if prev.voice_key != a.voice_key:
            return True
        if prev.kind == "dialogue" and a.kind == "dialogue" and (prev.speaker != a.speaker or (prev.speaker is None and prev.para != a.para)):
            return True
        if (prev.kind == "dialogue") != (a.kind == "dialogue") and prev.para != a.para:
            return True
        if shift_ok and a.first_of_span and a.shift >= shift_gate and cur_len >= 40:
            return True  # a meaningful emotional transition deserves its own delivery guidance
        return False

    groups: list[list[Atom]] = []
    cur: list[Atom] = []
    for a in atoms:
        if cur:
            span_len = a.end - cur[0].start
            forced = a.start in joins and span_len <= _HARD_LIMIT
            cur_len = cur[-1].end - cur[0].start
            if barrier(cur[-1], a, cur_len) or (span_len > max_chars and not forced):
                groups.append(cur)
                cur = []
            elif a.para != cur[-1].para and cur_len >= min_chars and not forced:
                groups.append(cur)  # natural paragraph boundary once the segment is long enough
                cur = []
        cur.append(a)
    if cur:
        groups.append(cur)

    # fold fragments that are too short into a neighbour when nothing separates them
    i = 0
    while i < len(groups):
        g = groups[i]
        length = g[-1].end - g[0].start
        if length < min_chars and g[0].kind != "heading":
            merged = False
            for j in (i - 1, i + 1):
                if 0 <= j < len(groups):
                    a, b = (groups[j], g) if j < i else (g, groups[j])
                    la, lb = a[-1].end - a[0].start, b[-1].end - b[0].start
                    if barrier(a[-1], b[0], la, shift_ok=min(la, lb) >= 25) and b[0].start not in joins:
                        continue
                    if b[-1].end - a[0].start <= max_chars:
                        groups[min(i, j)] = a + b
                        del groups[max(i, j)]
                        i = max(0, min(i, j) - 0)
                        merged = True
                        break
            if merged:
                continue
        i += 1

    segs: list[Seg] = []
    for g in groups:
        s, e = g[0].start, g[-1].end
        dlg = [a for a in g if a.kind == "dialogue"]
        kinds = {a.kind for a in g}
        span_kind = "heading" if kinds == {"heading"} else "mixed" if len(kinds) > 1 else next(iter(kinds))
        speaker = dlg[0].speaker if dlg else None
        segs.append(Seg(s, e, text[s:e], "heading" if span_kind == "heading" else "body", g[0].para, g[0].scene, speaker, span_kind,
                        g[0].voice_key if not dlg else dlg[0].voice_key, any(a.needs_review for a in g), manual=s in cuts or s in joins, atoms=g))
    for k, sg in enumerate(segs):
        nxt = segs[k + 1] if k + 1 < len(segs) else None
        if sg.kind == "heading":
            sg.pause_kind = "heading"
        elif nxt is None:
            sg.pause_kind = "chapter_end"
        elif nxt.scene_idx != sg.scene_idx:
            sg.pause_kind = "scene"
        elif nxt.para_idx != sg.para_idx:
            sg.pause_kind = "paragraph"
        else:
            sg.pause_kind = "sentence"
    return segs


def check_coverage(struct: ChapterStructure, segs: list[Seg]) -> list[str]:
    """Invariants: ordered, non-overlapping, exact slices, and every non-whitespace character
    of the chapter is in exactly one segment. Returns a list of violations (empty = sound)."""
    problems: list[str] = []
    text = struct.text
    covered = [False] * len(text)
    last_end = -1
    for sg in segs:
        if sg.start < last_end:
            problems.append(f"segment at {sg.start} overlaps or is out of order")
        last_end = sg.end
        if text[sg.start:sg.end] != sg.text:
            problems.append(f"segment at {sg.start} is not an exact slice")
        for k in range(sg.start, sg.end):
            if covered[k] and not text[k].isspace():
                problems.append(f"character {k} is in two segments")
            covered[k] = True
    for k, ch in enumerate(text):
        if not ch.isspace() and not covered[k]:
            problems.append(f"character {k} ({ch!r}) is in no segment")
            break
    return problems[:10]
