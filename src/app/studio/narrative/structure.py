"""Pass 1 - manuscript structure and speaker analysis.

Deterministic parsing of one chapter into paragraphs, scenes and dialogue/narration spans with
exact character offsets into the chapter's *canonical text* (readable blocks, whitespace
normalised, joined by a blank line). Speaker attribution looks at the tags and beats around a
quote, at the quote-continuation convention across paragraphs, and at the turn-taking of a
conversation. It never falls back to "the most recently mentioned character": a line with no
evidence is left unattributed and flagged for review. A local LLM can later resolve the
flagged spans (see ``store``); this module is the evidence-based first pass.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import vocab as V

NARRATOR_NAME = "Narrator"
_WS = re.compile(r"\s+")


def normalize_ws(text: str) -> str:
    return _WS.sub(" ", text).strip()


# ----------------------------------------------------------------------------- data classes


@dataclass
class KnownCharacter:
    name: str
    aliases: list[str] = field(default_factory=list)
    gender: str = ""
    kind: str = "character"
    id: int | None = None


@dataclass
class Para:
    idx: int
    kind: str  # heading | paragraph
    start: int
    end: int
    text: str
    scene_idx: int


@dataclass
class Span:
    para_idx: int
    idx: int
    start: int
    end: int
    kind: str  # narration | dialogue | heading
    text: str
    scene_idx: int
    speaker: str | None = None
    attribution: str = "none"  # explicit | beat | pronoun | alternation | continuation | user | none
    confidence: float = 0.0
    needs_review: bool = False
    evidence: str = ""
    candidates: list[str] = field(default_factory=list)
    cues: dict = field(default_factory=dict)
    closed: bool = True
    user_locked: bool = False
    is_tag: bool = False  # a short narration tag ("she said.") attached to the dialogue before or after it
    tag_speaker: str | None = None


@dataclass
class ChapterStructure:
    text: str
    paragraphs: list[Para]
    spans: list[Span]
    scenes: int
    perspective: str
    warnings: list[str]
    tension: list[float]
    tension_shifts: list[dict]


# ----------------------------------------------------------------------------------- cast


@dataclass
class Entry:
    name: str
    aliases: set[str] = field(default_factory=set)
    gender: str = ""
    kind: str = "character"
    id: int | None = None
    discovered: bool = False
    known: bool = False  # came from the database (user-curated or earlier chapters)


def _tokens(raw: str) -> tuple[str, ...]:
    toks = [t for t in re.findall(r"[A-Za-z][A-Za-z'’\-]*", raw.lower())]
    while toks and toks[0] in V.TITLES and len(toks) > 1:
        toks.pop(0)
    return tuple(toks)


def title_gender(raw: str) -> str:
    m = re.match(r"\s*([A-Za-z]+)\.?\s", raw)
    return V.TITLE_GENDER.get(m.group(1).lower(), "") if m else ""


class Cast:
    """Characters seen so far in a manuscript, with alias resolution that refuses to guess."""

    def __init__(self, known: list[KnownCharacter] | None = None):
        self.entries: dict[str, Entry] = {}
        for k in known or []:
            e = Entry(name=k.name, aliases=set(k.aliases), gender=k.gender, kind=k.kind, id=k.id, known=True)
            self.entries[k.name] = e
        if not any(e.kind == "narrator" for e in self.entries.values()):
            self.entries[NARRATOR_NAME] = Entry(name=NARRATOR_NAME, kind="narrator", known=True)

    def narrator(self) -> Entry:
        return next(e for e in self.entries.values() if e.kind == "narrator")

    def _keys(self, e: Entry) -> list[tuple[str, ...]]:
        return [t for t in (_tokens(e.name), *(_tokens(a) for a in e.aliases)) if t]

    def resolve(self, raw: str) -> Entry | None:
        toks = _tokens(raw)
        if not toks:
            return None
        exact = [e for e in self.entries.values() if e.kind != "narrator" and toks in self._keys(e)]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            return None
        part = [e for e in self.entries.values() if e.kind != "narrator" and any(set(toks) <= set(k) for k in self._keys(e))]
        return part[0] if len(part) == 1 else None

    def add(self, raw: str) -> Entry:
        cur = self.resolve(raw)
        if cur is None:  # a longer form of a name already discovered: "Elena" + "Elena Marsh"
            toks0 = _tokens(raw)
            ext = [e for e in self.entries.values() if e.kind != "narrator" and not e.known and _tokens(e.name) and set(_tokens(e.name)) <= set(toks0)]
            cur = ext[0] if len(ext) == 1 else None
        clean =" ".join(w for w in re.findall(r"[A-Za-z][A-Za-z'’\-]*\.?", raw))
        toks = _tokens(raw)
        g = title_gender(raw)
        if cur:
            if g and not cur.gender:
                cur.gender = g
            if len(toks) > len(_tokens(cur.name)) and not cur.known and set(_tokens(cur.name)) <= set(toks):
                del self.entries[cur.name]
                cur.aliases.add(cur.name)
                cur.name = " ".join(w.capitalize() for w in toks)
                self.entries[cur.name] = cur
            return cur
        name = " ".join(w.capitalize() if not w.isupper() else w for w in (clean.split() if clean else raw.split()))
        # a leading title stays out of the canonical name ("Dr. Hale" -> "Hale") but remains an alias
        words = name.split()
        if len(words) > 1 and words[0].lower().rstrip(".") in V.TITLES:
            alias = name
            name = " ".join(words[1:])
        else:
            alias = ""
        e = Entry(name=name, gender=g, discovered=True)
        if alias:
            e.aliases.add(alias)
        self.entries[name] = e
        return e


# ----------------------------------------------------------------------------- text build


def build_chapter_text(blocks, title: str | None = None) -> tuple[str, list[Para], int]:
    """Canonical chapter text and its paragraphs. Scene breaks and headings start a new scene."""
    parts: list[tuple[str, str, int]] = []
    scene = 0
    seen_content = False
    if title:
        parts.append(("heading", normalize_ws(title), scene))
        seen_content = True
    for b in blocks:
        if b.kind == "scene_break":
            if seen_content:
                scene += 1
            continue
        text = normalize_ws(b.text)
        if not text:
            continue
        if b.kind == "heading" and seen_content and parts and parts[-1][0] != "heading":
            scene += 1
        parts.append((b.kind if b.kind in ("heading", "paragraph") else "paragraph", text, scene))
        seen_content = True
    paras: list[Para] = []
    pos = 0
    out = []
    for i, (kind, text, sc) in enumerate(parts):
        if i:
            pos += 2
        paras.append(Para(i, kind, pos, pos + len(text), text, sc))
        out.append(text)
        pos += len(text)
    return "\n\n".join(out), paras, (scene + 1 if parts else 0)


# ------------------------------------------------------------------------------ quotes

_OPENERS = "“\""
_CLOSERS = "”\""


def _find_quotes(text: str) -> list[tuple[int, int, bool]]:
    out: list[tuple[int, int, bool]] = []
    open_at: int | None = None
    for i, c in enumerate(text):
        if open_at is None:
            if c in _OPENERS:
                open_at = i
        elif c in _CLOSERS:
            out.append((open_at, i + 1, True))
            open_at = None
        elif c == "“":
            out.append((open_at, i, False))
            open_at = i
    if open_at is not None:
        out.append((open_at, len(text), False))
    return out


def _is_dialogue(text: str, s: int, e: int, closed: bool) -> bool:
    inner = text[s + 1:e - 1 if closed else e]
    words = len(inner.split())
    before = text[:s].rstrip()
    at_start = not before
    if at_start or (before and before[-1] in ",:.!?—-"):
        return True
    if words >= 4 or re.search(r"[,.!?…]\s*$", inner):
        return True
    return False  # a short quoted phrase inside a sentence: scare quotes, a title, a coined term


def _split_spans(para: Para, base_idx: int) -> list[Span]:
    text, off = para.text, para.start
    cuts: list[tuple[int, int, str, bool]] = []
    for s, e, closed in _find_quotes(text):
        if _is_dialogue(text, s, e, closed):
            cuts.append((s, e, "dialogue", closed))
    spans: list[Span] = []
    pos = 0

    def push(a: int, b: int, kind: str, closed: bool = True):
        while a < b and text[a].isspace():
            a += 1
        while b > a and text[b - 1].isspace():
            b -= 1
        if b > a:
            spans.append(Span(para.idx, base_idx + len(spans), off + a, off + b, kind, text[a:b], para.scene_idx, closed=closed))

    for s, e, kind, closed in cuts:
        push(pos, s, "narration")
        push(s, e, kind, closed)
        pos = e
    push(pos, len(text), "narration")
    return spans


# -------------------------------------------------------------------------------- tags

_TITLE_RE = r"(?:(?:" + "|".join(t.capitalize() for t in V.TITLES) + r")\.?\s+)"
_NAME_TOKEN = r"[A-Z][A-Za-z'’\-]+"
_NAME = rf"(?:{_TITLE_RE})?{_NAME_TOKEN}(?:\s+{_NAME_TOKEN}){{0,2}}"
_DESCR = r"[Tt]he\s+[a-z]+(?:\s+[a-z]+)?"
_PRON = r"(?i:he|she|they|i)"
_SUBJ = rf"(?P<s>{_PRON}\b|{_NAME}|{_DESCR})"
_VERBS = rf"(?:{V.SPEECH_VERBS})"
_ADV = r"(?:[a-z]+ly\s+)?"
_LEAD = r"^[\s,.:;—–\-]*"

POST_TAG = re.compile(_LEAD + _SUBJ + r"\s+" + _ADV + rf"(?P<v>{_VERBS})\b")
POST_INV = re.compile(_LEAD + rf"(?P<v>{_VERBS})\s+(?P<s>{_PRON}\b|{_NAME}|{_DESCR})")
PRE_TAG = re.compile(_SUBJ + r"\s+" + _ADV + rf"(?P<v>{_VERBS})\s*[,:]?\s*$")
PRE_INV = re.compile(rf"(?P<v>{_VERBS})\s+(?P<s>{_PRON}\b|{_NAME}|{_DESCR})\s*[,:]?\s*$")
BEAT_START = re.compile(_LEAD + rf"(?P<s>{_NAME})\s+[a-z]")
PRON_BEAT = re.compile(_LEAD + r"(?P<s>(?i:he|she))\s+[a-z]")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _plausible_name(subj: str) -> bool:
    toks = re.findall(r"[A-Za-z][A-Za-z'’\-]*", subj)
    toks = [t for t in toks if t.lower() not in V.TITLES]
    return bool(toks) and all(t.lower() not in V.NOT_NAMES for t in toks) and all(t[0].isupper() for t in toks)


@dataclass
class _Tag:
    subject: str
    verb: str | None
    kind: str  # explicit | beat
    adverb: str | None = None
    text: str = ""


def _tag_from_post(post: str, cast: Cast) -> _Tag | None:
    m = POST_TAG.match(post) or POST_INV.match(post)
    if m:
        adv = re.search(r"\b([a-z]+ly)\s+" + m.group("v"), post[:m.end()])
        return _Tag(m.group("s").strip(), m.group("v"), "explicit", adv.group(1) if adv else None, post[:m.end()])
    m = BEAT_START.match(post)
    if m and cast.resolve(m.group("s")):
        return _Tag(m.group("s").strip(), None, "beat", None, post[:m.end()])
    m = PRON_BEAT.match(post)
    if m:
        return _Tag(m.group("s").strip(), None, "beat", None, post[:m.end()])
    return None


def _tag_from_pre(pre: str, cast: Cast) -> _Tag | None:
    pre = pre.strip()
    last = _SENT_SPLIT.split(pre)[-1] if pre else ""
    m = PRE_TAG.search(last) or PRE_INV.search(last)
    if m:
        adv = re.search(r"\b([a-z]+ly)\s+" + m.group("v"), last)
        return _Tag(m.group("s").strip(), m.group("v"), "explicit", adv.group(1) if adv else None, last)
    m = re.match(rf"^\s*(?P<s>{_NAME})\s+[a-z]", last)
    if m and cast.resolve(m.group("s")):
        return _Tag(m.group("s").strip(), None, "beat", None, last)
    m = re.match(r"^\s*(?P<s>(?i:he|she))\s+[a-z]", last)
    if m:
        return _Tag(m.group("s").strip(), None, "beat", None, last)
    return None


# --------------------------------------------------------------------------------- cues


def span_cues(span_text: str, tag_text: str = "", verb: str | None = None, adverb: str | None = None) -> dict:
    scores = V.cue_scores(span_text)
    for emo, sc in V.cue_scores(tag_text).items():
        scores[emo] = scores.get(emo, 0.0) + 0.5 * sc
    pace, delta, explicit, evidence = "normal", 0.0, False, []
    if verb and verb in V.VERB_COLOUR:
        emo, d, p = V.VERB_COLOUR[verb]
        if emo:
            scores[emo] = scores.get(emo, 0.0) + 1.6
        delta += d
        pace = p or pace
        explicit = True
        evidence.append(f"speech verb '{verb}'")
    if adverb and adverb in V.ADVERB_COLOUR:
        emo, d = V.ADVERB_COLOUR[adverb]
        scores[emo] = scores.get(emo, 0.0) + 1.4
        delta += d
        explicit = True
        evidence.append(f"adverb '{adverb}'")
    if not scores:
        emo, inten = ("neutral", 1)
    else:
        emo = max(scores, key=lambda k: scores[k])
        top = scores[emo]
        if top >= 2.0:
            explicit = True
        inten = V.clamp_intensity(1 + top * 1.1 + delta)
        if verb in ("whispered", "murmured") and emo == "neutral":
            emo = "calm"
        if span_text.count("!") >= 2:
            inten = V.clamp_intensity(inten + 1)
        for stem, e2 in V.CUE_STEMS:
            if e2 == emo and re.search(r"\b" + re.escape(stem), span_text.lower()):
                evidence.append(f"word '{stem}…' ({emo})")
                break
    if pace == "normal":
        if span_text.count("…") + span_text.count("...") >= 1 and emo in ("sadness", "grief", "resignation", "fear"):
            pace = "slow"
        elif inten >= 4 and emo in ("anger", "fear", "excitement", "anxiety"):
            pace = "brisk"
        elif emo in ("sadness", "grief", "resignation"):
            pace = "measured"
    return {"emotion": emo, "intensity": inten, "pace": pace, "tension": V.tension_score(span_text + " " + tag_text),
            "explicit": explicit, "evidence": evidence[:4]}


# ------------------------------------------------------------------------- the analysis


@dataclass
class _Hist:
    para: int
    speaker: str
    conf: float


def _run(blocks, title: str | None, cast: Cast, threshold: float, track: bool,
         locked: dict[tuple[int, int], str] | None, first_person: bool | None) -> ChapterStructure:
    text, paras, scenes = build_chapter_text(blocks, title)
    warnings: list[str] = []
    spans: list[Span] = []
    for p in paras:
        if p.kind == "heading":
            spans.append(Span(p.idx, len(spans), p.start, p.end, "heading", p.text, p.scene_idx, speaker=None, attribution="none"))
            continue
        for sp in _split_spans(p, len(spans)):
            sp.idx = len(spans)
            spans.append(sp)

    # narrator perspective: share of first-person pronouns in narration
    narr = " ".join(s.text for s in spans if s.kind == "narration")
    words = re.findall(r"[A-Za-z']+", narr)
    fp = sum(1 for w in words if w.lower() in ("i", "my", "me", "myself", "i'm", "i've", "i'd", "i'll"))
    perspective = "first" if (first_person if first_person is not None else (words and fp / len(words) >= 0.015)) else "third"
    narrator = cast.narrator()

    by_para: dict[int, list[Span]] = {}
    for s in spans:
        by_para.setdefault(s.para_idx, []).append(s)

    hist: dict[int, list[_Hist]] = {}  # scene -> attributed dialogue so far
    participants: dict[int, dict[str, Entry]] = {}  # scene -> entries in play
    prev_open: Span | None = None  # last dialogue span of the previous paragraph if it was left unclosed

    def note(scene: int, e: Entry):
        if e.kind != "narrator":
            participants.setdefault(scene, {})[e.name] = e

    def mentions(scene: int, ptext: str):
        for e in list(cast.entries.values()):
            if e.kind == "narrator":
                continue
            for nm in {e.name, *e.aliases}:
                if nm and re.search(r"\b" + re.escape(nm) + r"\b", ptext):
                    note(scene, e)
                    break

    def resolve_subject(sub: str, scene: int, via_verb: bool) -> tuple[Entry | None, str, float, list[str]]:
        """-> (entry, attribution, confidence, candidates)"""
        low = sub.lower()
        if low == "i":
            if perspective == "first":
                return narrator, "explicit", 0.9, []
            return None, "none", 0.0, []
        if low in ("he", "she", "they"):
            g = {"he": "male", "she": "female"}.get(low, "")
            pool = [e for e in participants.get(scene, {}).values()]
            if not g:
                return None, "none", 0.0, [e.name for e in pool]
            match = [e for e in pool if e.gender == g]
            unknown = [e for e in pool if not e.gender]
            if len(match) == 1 and not unknown:
                return match[0], "pronoun", 0.72, []
            return None, "none", 0.0, [e.name for e in match + unknown]
        if low.startswith("the "):
            e = cast.resolve(sub)
            return (e, "explicit", 0.9, []) if e else (None, "none", 0.0, [sub])
        e = cast.resolve(sub)
        if e:
            return e, "explicit", 0.95, []
        if track and via_verb and _plausible_name(sub):
            e = cast.add(sub)
            return e, "explicit", 0.9, []
        return None, "none", 0.0, [sub]

    for p in paras:
        if p.kind == "heading":
            continue
        ps = by_para.get(p.idx, [])
        mentions(p.scene_idx, p.text)
        consumed: set[int] = set()
        for k, sp in enumerate(ps):
            if sp.kind != "dialogue":
                continue
            nxt = ps[k + 1] if k + 1 < len(ps) else None
            prv = ps[k - 1] if k > 0 else None
            post_tag = _tag_from_post(nxt.text, cast) if nxt is not None and nxt.kind == "narration" else None
            pre_tag = (_tag_from_pre(prv.text, cast) if prv is not None and prv.kind == "narration" and id(prv) not in consumed else None)
            tag, tag_src = (post_tag, nxt) if post_tag else (pre_tag, prv)
            if post_tag and pre_tag and post_tag.kind == "beat" and pre_tag.kind == "explicit":
                tag, tag_src = pre_tag, prv
            ent: Entry | None = None
            if tag:
                ent, how, conf, cands = resolve_subject(tag.subject, p.scene_idx, via_verb=tag.verb is not None)
                if ent is not None:
                    sp.attribution = "beat" if tag.kind == "beat" else how
                    if tag.kind == "beat":
                        sp.confidence = min(conf, 0.62) if tag.subject.lower() in ("he", "she") else 0.8
                    else:
                        sp.confidence = conf
                    sp.evidence = f"{tag.text.strip()!r}"[:160]
                    consumed.add(id(tag_src))
                    if tag.verb and tag_src is not None and len(tag_src.text) <= 70:
                        tag_src.is_tag, tag_src.tag_speaker = True, ent.name
                    sp.cues = span_cues(sp.text, tag_src.text if tag_src is not None else "", tag.verb, tag.adverb)
                else:
                    sp.candidates = cands
                    sp.evidence = f"tag names '{tag.subject}' but it does not identify a single known character"
                    if tag.verb:
                        consumed.add(id(tag_src))
                    sp.cues = span_cues(sp.text, tag_src.text if tag_src is not None else "", tag.verb, tag.adverb)
            if ent is None and not tag and prv is not None:
                # a later quote in the same paragraph, after a tag: `"A," she said, "b."`
                before = prv if prv.kind == "dialogue" else (ps[k - 2] if k >= 2 and id(prv) in consumed and ps[k - 2].kind == "dialogue" else None)
                if before is not None and before.speaker:
                    ent = cast.entries.get(before.speaker)
                    if ent:
                        sp.attribution, sp.confidence = "continuation", round(before.confidence * 0.95, 3)
                        sp.evidence = "continues the same speech within the paragraph"
            if ent is None and not tag:
                # same speech continued in the next paragraph (opening quote repeated, previous quote left open)
                if prev_open is not None and k == 0 and sp.start == p.start and prev_open.speaker:
                    ent = cast.entries.get(prev_open.speaker)
                    if ent:
                        sp.attribution, sp.confidence = "continuation", round(prev_open.confidence * 0.95, 3)
                        sp.evidence = "unclosed quote in the previous paragraph continues here"
            if ent is None and not tag:
                h = hist.get(p.scene_idx, [])
                if len(h) >= 2 and h[-1].speaker != h[-2].speaker and p.idx - h[-1].para <= 2 and p.idx != h[-1].para:
                    cand = h[-2].speaker
                    conf = round(min(0.8, min(h[-1].conf, h[-2].conf) * 0.85), 3)
                    ent = cast.entries.get(cand)
                    if ent:
                        sp.attribution, sp.confidence = "alternation", conf
                        sp.evidence = f"turn-taking between {h[-2].speaker} and {h[-1].speaker}"
                        sp.candidates = [h[-2].speaker, h[-1].speaker]
                else:
                    sp.evidence = sp.evidence or "no speaker tag, beat or conversation pattern identifies the speaker"
                    sp.candidates = [e.name for e in participants.get(p.scene_idx, {}).values()][:6]
                if not sp.cues:
                    sp.cues = span_cues(sp.text)
            if ent is not None and sp.confidence >= threshold:
                sp.speaker = ent.name
                note(p.scene_idx, ent)
                h = hist.setdefault(p.scene_idx, [])
                if not (h and h[-1].para == p.idx and h[-1].speaker == ent.name):
                    h.append(_Hist(p.idx, ent.name, sp.confidence))
                if not sp.cues:
                    sp.cues = span_cues(sp.text)
            else:
                if ent is not None:
                    sp.candidates = [ent.name] + [c for c in sp.candidates if c != ent.name]
                    sp.evidence += f" (confidence {sp.confidence:.2f} is below the {threshold:.2f} threshold)"
                sp.speaker = None
                sp.needs_review = True
                sp.confidence = sp.confidence if ent is not None else 0.0
                if not sp.cues:
                    sp.cues = span_cues(sp.text)
        # narration spans: cues from their own text
        for sp in ps:
            if sp.kind == "narration":
                sp.cues = span_cues(sp.text)
                sp.speaker = narrator.name
                sp.attribution = "explicit"
                sp.confidence = 1.0
        # open-quote carry to the next paragraph
        dl = [s for s in ps if s.kind == "dialogue"]
        prev_open = dl[-1] if dl and not dl[-1].closed else None
        if prev_open is not None:
            nxt_p = paras[p.idx + 1] if p.idx + 1 < len(paras) else None
            if nxt_p is None or not nxt_p.text.startswith(tuple(_OPENERS)):
                warnings.append(f"Paragraph {p.idx}: a quotation is opened but never closed.")
                prev_open = None
    for s in spans:
        if s.kind == "heading":
            s.cues = {"emotion": "neutral", "intensity": 1, "pace": "measured", "tension": 0.0, "explicit": False, "evidence": []}
            s.speaker, s.attribution, s.confidence = narrator.name, "explicit", 1.0

    # locked (user-confirmed) attributions survive re-analysis
    if locked:
        for s in spans:
            nm = locked.get((s.start, s.end))
            if nm and s.kind == "dialogue":
                ent = cast.entries.get(nm)
                if ent:
                    s.speaker, s.attribution, s.confidence, s.needs_review, s.user_locked = ent.name, "user", 1.0, False, True
                    s.evidence = "confirmed by the user"

    # emotional transitions between consecutive spans of the same scene
    prev: Span | None = None
    for s in spans:
        shift = 0.0
        if prev is not None and prev.scene_idx == s.scene_idx and s.kind != "heading" and prev.kind != "heading":
            a, b = prev.cues, s.cues
            fam = V.FAMILIES.get(a.get("emotion", "neutral")) != V.FAMILIES.get(b.get("emotion", "neutral"))
            d = abs(int(b.get("intensity", 1)) - int(a.get("intensity", 1)))
            if (fam and max(a.get("intensity", 1), b.get("intensity", 1)) >= 3) or d >= 2:
                shift = round(min(1.0, 0.5 * (d / 4) + (0.5 if fam else 0.0)), 2)
        s.cues["shift"] = shift
        prev = s

    # tension profile per paragraph and the notable jumps in it
    tension = [V.tension_score(p.text) for p in paras]
    shifts = []
    for i in range(1, len(tension)):
        base = sum(tension[max(0, i - 3):i]) / len(tension[max(0, i - 3):i])
        if abs(tension[i] - base) >= 2.0:
            shifts.append({"para": i, "start": paras[i].start, "from": round(base, 2), "to": tension[i]})
    return ChapterStructure(text, paras, spans, scenes, perspective, warnings, tension, shifts)


_PRON_F = re.compile(r"\b(she|her|hers|herself)\b", re.I)
_PRON_M = re.compile(r"\b(he|him|his|himself)\b", re.I)


def _outside_quotes(text: str) -> str:
    out, last = [], 0
    for s, e, _c in _find_quotes(text):
        out.append(text[last:s])
        last = e
    out.append(text[last:])
    return " ".join(out)


def enrich_cast(cast: Cast, text: str) -> None:
    """Learn full names ("Elena" -> "Elena Marsh") and genders (from titles and from pronouns that
    share a sentence with exactly one named character) from the chapter text."""
    narration = _outside_quotes(text)
    for e in list(cast.entries.values()):
        if e.kind == "narrator" or e.known or len(_tokens(e.name)) != 1:
            continue
        found: dict[str, int] = {}
        for m in re.finditer(rf"\b{re.escape(e.name)}\s+({_NAME_TOKEN})\b", narration):
            sur = m.group(1)
            if sur.lower() not in V.NOT_NAMES and sur.lower() not in V.TITLES:
                found[sur] = found.get(sur, 0) + 1
        if found:
            best = max(found, key=lambda k: found[k])
            if all(best.lower() not in _tokens(o.name) for o in cast.entries.values() if o is not e):
                cast.add(f"{e.name} {best}")
    votes: dict[str, dict[str, int]] = {}
    for sent in _SENT_SPLIT.split(narration):
        present = []
        for e in cast.entries.values():
            if e.kind == "narrator":
                continue
            for nm in {e.name, *e.aliases}:
                m = re.search(r"\b" + re.escape(nm) + r"\b", sent)
                if m:
                    present.append((e, m.end()))
                    break
        if len(present) != 1:
            continue
        e, end = present[0]
        tail = sent[end:]
        f, m_ = bool(_PRON_F.search(tail)), bool(_PRON_M.search(tail))
        if f != m_:
            votes.setdefault(e.name, {"female": 0, "male": 0})["female" if f else "male"] += 1
    for name, v in votes.items():
        e = cast.entries.get(name)
        if e is not None and not e.gender and not e.known:
            top = max(v, key=lambda k: v[k])
            if v[top] >= 1 and v["female" if top == "male" else "male"] == 0:
                e.gender = top


def analyze_chapter(blocks, title: str | None, cast: Cast, threshold: float = 0.6, track: bool = True,
                    locked: dict[tuple[int, int], str] | None = None, first_person: bool | None = None) -> ChapterStructure:
    """Two runs over the chapter: the first discovers names (and learns surnames and genders),
    the second attributes every quote with that knowledge available from the first line."""
    res = _run(blocks, title, cast, threshold, track, locked, first_person)
    enrich_cast(cast, res.text)
    return _run(blocks, title, cast, threshold, track, locked, first_person)
