"""Split chapter text into natural narration segments (default 150-400 characters).

Invariants (tested):
  * chunks never cut a word in half;
  * joining a paragraph's chunks with single spaces reproduces the paragraph exactly
    (after whitespace normalisation) - nothing is dropped or reordered;
  * a chunk never exceeds ``max_chars`` unless a single word is longer than that.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "st", "jr", "sr", "prof", "rev", "gen", "col", "capt", "lt", "sgt",
    "mt", "ft", "vs", "etc", "no", "vol", "ch", "fig", "inc", "ltd", "co", "corp", "e.g", "i.e",
    "approx", "dept", "est", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec", "mon", "tue", "wed", "thu", "fri", "sat", "sun", "u.s", "u.k", "a.m", "p.m",
}

# candidate sentence boundary: terminal punctuation, optional closing quotes/brackets, whitespace
_BOUNDARY = re.compile(r"([.!?…]+[\"'”’)\]]*)(\s+)")
_CLAUSE = re.compile(r"([,;:—–]|\s-\s)(\s*)")


@dataclass
class Segment:
    text: str
    kind: str  # heading | body
    para_idx: int
    pause_kind: str  # sentence | paragraph | scene | heading | chapter_end


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text: str) -> list[str]:
    text = normalize_ws(text)
    if not text:
        return []
    out: list[str] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        end = m.end(1)
        before = text[start:end]
        last_word = re.findall(r"([A-Za-z][A-Za-z.]*)\.+$", text[max(start, end - 12):end].split(" ")[-1])
        token = last_word[0].lower().rstrip(".") if last_word else ""
        nxt = text[m.end():m.end() + 1]
        if m.group(1).startswith(".") and (token in ABBREVIATIONS or re.fullmatch(r"[a-z]", token)):
            continue  # "Dr. Smith", "J. R. R."
        if nxt and nxt.islower():
            continue  # "e.g. this" / ellipsis mid-sentence
        out.append(before.strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _split_long(sentence: str, max_chars: int) -> list[str]:
    """Split one over-long sentence at clause punctuation, then at spaces."""
    if len(sentence) <= max_chars:
        return [sentence]
    pieces: list[str] = []
    start = 0
    for m in _CLAUSE.finditer(sentence):
        end = m.end(1)
        if sentence[end:end + 1] == " " or m.group(2):
            pieces.append(sentence[start:end].strip())
            start = m.end()
    pieces.append(sentence[start:].strip())
    pieces = [p for p in pieces if p]
    out: list[str] = []
    for p in _pack(pieces, max_chars, min_chars=0):
        if len(p) <= max_chars:
            out.append(p)
            continue
        words = p.split(" ")
        cur = ""
        for w in words:
            cand = f"{cur} {w}" if cur else w
            if len(cand) > max_chars and cur:
                out.append(cur)
                cur = w
            else:
                cur = cand
        if cur:
            out.append(cur)
    return out


def _pack(units: list[str], max_chars: int, min_chars: int) -> list[str]:
    chunks: list[str] = []
    cur = ""
    for u in units:
        if not cur:
            cur = u
            continue
        cand = f"{cur} {u}"
        if len(cand) <= max_chars:
            cur = cand
        else:
            chunks.append(cur)
            cur = u
    if cur:
        chunks.append(cur)
    # merge a short tail into its predecessor when it fits
    if len(chunks) >= 2 and len(chunks[-1]) < min_chars and len(chunks[-2]) + 1 + len(chunks[-1]) <= max_chars:
        chunks[-2] = f"{chunks[-2]} {chunks[-1]}"
        chunks.pop()
    return chunks


def chunk_paragraph(text: str, min_chars: int = 150, max_chars: int = 400) -> list[str]:
    units: list[str] = []
    for s in split_sentences(text):
        units.extend(_split_long(s, max_chars))
    return _pack(units, max_chars, min_chars)


def build_segments(blocks, min_chars: int, max_chars: int, merge_short: bool = True) -> list[Segment]:
    """Turn readable chapter blocks (heading/paragraph/scene_break) into segments.

    Consecutive short paragraphs (e.g. dialogue lines) are merged up to ``max_chars`` when
    ``merge_short`` is set, because very short TTS inputs are less stable."""
    segs: list[Segment] = []
    pending_short: list[tuple[int, str]] = []

    def flush_short():
        if not pending_short:
            return
        texts = [t for _, t in pending_short]
        para0 = pending_short[0][0]
        for chunk in _pack(texts, max_chars, 0):
            segs.append(Segment(chunk, "body", para0, "sentence"))
        segs[-1].pause_kind = "paragraph"
        pending_short.clear()

    for pidx, b in enumerate(blocks):
        if b.kind == "scene_break":
            flush_short()
            if segs:
                segs[-1].pause_kind = "scene"
            continue
        if b.kind == "heading":
            flush_short()
            segs.append(Segment(normalize_ws(b.text), "heading", pidx, "heading"))
            continue
        text = normalize_ws(b.text)
        if not text:
            continue
        if merge_short and len(text) < min_chars:
            total = sum(len(t) + 1 for _, t in pending_short) + len(text)
            if total > max_chars:
                flush_short()
            pending_short.append((pidx, text))
            continue
        flush_short()
        chunks = chunk_paragraph(text, min_chars, max_chars)
        for i, c in enumerate(chunks):
            segs.append(Segment(c, "body", pidx, "paragraph" if i == len(chunks) - 1 else "sentence"))
    flush_short()
    if segs:
        segs[-1].pause_kind = "chapter_end"
    return segs
