"""Analysis stages that run in the worker, ahead of synthesis.

Stage order: structure -> attribution (LLM, only for flagged spans) -> characters -> summaries ->
segmentation -> director (+ continuity). Each stage persists its output, so a restart resumes
where it stopped, and unchanged inputs are never re-analysed (the Director is cached by a key
over text, context, profile, previous delivery, model and prompt version).
"""
from __future__ import annotations

import bisect
import logging
import re
import time

from ..config import NarrativeSettings
from ..db import row, rows, scalar
from ..util import dumps, loads, now, short_hash
from . import beats as B
from . import cast as C
from . import continuity as CT
from . import delivery as DL
from . import director as D
from . import store as ST
from . import structure as S
from . import vocab as V
from . import windows as W
from .llm import LLMClient, LLMError, extract_json, resolve_models

log = logging.getLogger("studio.analysis")


def make_llm(ns: NarrativeSettings, transport=None, secrets=None) -> LLMClient:
    return LLMClient(ns.llm, transport=transport, secrets=secrets)


# --------------------------------------------------------------------- attribution (LLM)

ATTR_SYSTEM = """You identify who speaks a quoted line in a novel, using ONLY the passage you are given.
Reply with ONE JSON object: {"speaker": <a name from the cast list, or "UNKNOWN">, "confidence": <0-1>, "evidence": <a short VERBATIM quote from the passage that supports your answer>}.
Rules: never invent a character; if the passage does not make the speaker clear, answer UNKNOWN. Do not pick someone just because they were mentioned most recently."""


def _validate_attr(raw: dict, passage: str, names: dict[str, str]) -> dict:
    sp = str(raw.get("speaker", "")).strip()
    conf = max(0.0, min(0.85, float(raw.get("confidence", 0))))  # a model's own certainty is capped
    if sp.upper() == "UNKNOWN" or not sp:
        return {"speaker": None, "confidence": 0.0, "evidence": ""}
    canon = names.get(sp.lower())
    if not canon:
        raise ValueError(f"'{sp}' is not in the cast list")
    ev = str(raw.get("evidence", "")).strip()
    if not ev or D._norm(ev) not in D._norm(passage):
        raise ValueError("the evidence is not a verbatim quote from the passage")
    return {"speaker": canon, "confidence": conf, "evidence": ev[:200]}


def run_attribution(ctx, book_id: int, client: LLMClient, max_spans: int = 6) -> dict:
    """Resolve flagged dialogue spans with the LLM. Returns counts; ``remaining`` is what is still untried."""
    stats = {"tried": 0, "resolved": 0, "unknown": 0, "errors": 0}
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        ns = ST.book_ns(ctx, book)
        chars = C.list_characters(conn, book_id)
        todo = [r for r in rows(conn, "SELECT * FROM nar_spans WHERE book_id=? AND kind='dialogue' AND needs_review=1 AND user_locked=0 ORDER BY chapter_id, start", (book_id,))
                if not loads(r["cues_json"], {}).get("llm_tried")][:max_spans]
        remaining = scalar(conn, "SELECT COUNT(*) FROM nar_spans WHERE book_id=? AND kind='dialogue' AND needs_review=1 AND user_locked=0 AND cues_json NOT LIKE '%llm_tried%'", (book_id,), 0)
    names = {}
    for c in chars:
        if c["kind"] == "narrator":
            continue
        for nm in {c["name"], *c["aliases"]}:
            names[nm.lower()] = c["name"]
    id_of = {c["name"]: c["id"] for c in chars}
    for r in todo:
        with ctx.db.session() as conn:
            ct = row(conn, "SELECT text FROM chapter_text WHERE chapter_id=?", (r["chapter_id"],))["text"]
        lo = max(0, r["start"] - min(ns.context_before_chars, 1500))
        hi = min(len(ct), r["end"] + min(ns.context_after_chars, 600))
        passage = ct[lo:r["start"]] + "<<<" + ct[r["start"]:r["end"]] + ">>>" + ct[r["end"]:hi]
        user = f"Cast: {', '.join(sorted({c['name'] for c in chars if c['kind'] != 'narrator'}))}\n\nPassage (the line to attribute is between <<< and >>>):\n{passage}"
        cues = loads(r["cues_json"], {})
        cues["llm_tried"] = client.model_label
        stats["tried"] += 1
        try:
            res = client.chat_json(ATTR_SYSTEM, user, lambda raw: _validate_attr(raw, passage, names))
        except LLMError:
            stats["errors"] += 1
            with ctx.db.tx() as conn:
                conn.execute("UPDATE nar_spans SET cues_json=? WHERE id=?", (dumps({**cues, "llm_tried": "error"}), r["id"]))
            if stats["errors"] >= 2:
                break
            continue
        with ctx.db.tx() as conn:
            if res["speaker"] and res["confidence"] >= ns.attribution_threshold:
                conn.execute("""UPDATE nar_spans SET speaker_id=?, attribution='llm', confidence=?, needs_review=0, evidence=?, cues_json=? WHERE id=?""",
                             (id_of[res["speaker"]], res["confidence"], f"LLM ({client.model_label}): {res['evidence']}", dumps(cues), r["id"]))
                stats["resolved"] += 1
            else:
                guess = id_of.get(res["speaker"]) if res["speaker"] else None
                conn.execute("UPDATE nar_spans SET guess_id=COALESCE(?, guess_id), cues_json=? WHERE id=?", (guess, dumps(cues), r["id"]))
                stats["unknown"] += 1
    stats["remaining"] = max(0, remaining - stats["tried"])
    return stats


# -------------------------------------------------------------------------- summaries


def _sentences(text: str) -> list[tuple[int, int]]:
    from .segmenter import sentence_ranges

    return sentence_ranges(text, 0)


def run_summaries(ctx, book_id: int, client: LLMClient | None) -> dict:
    """Extractive chapter and scene summaries: sentences copied verbatim from the chapter, with their
    source offsets, so a summary can never contain an event that is not in the manuscript. When an
    LLM is configured it may add an abstractive summary, which is stored as unconfirmed inference and
    rejected if it names anything that does not occur in the chapter."""
    made = skipped = rejected = 0
    with ctx.db.session() as conn:
        chapters = rows(conn, "SELECT c.id, c.idx, c.title, t.text, t.sha256, t.meta_json FROM chapters c JOIN chapter_text t ON t.chapter_id=c.id WHERE c.book_id=? ORDER BY c.idx", (book_id,))
        have = {(r["chapter_idx"], r["scene_idx"], r["source"]): loads(r["refs_json"], []) for r in rows(conn, "SELECT chapter_idx, scene_idx, source, refs_json FROM story_notes WHERE book_id=? AND kind='summary'", (book_id,))}
    for ch in chapters:
        digest = ch["sha256"][:12]
        meta = loads(ch["meta_json"], {})
        text = ch["text"]
        scene_starts = meta.get("scene_starts") or [0]
        bounds = list(zip(scene_starts, scene_starts[1:] + [len(text)]))
        notes = []
        for sc, (a, b) in enumerate(bounds):
            body = text[a:b]
            sents = [(x + a, y + a) for x, y in _sentences(body) if y - x > 25]
            if not sents:
                continue
            pick = [sents[0]]
            if len(sents) > 2:
                scored = max(sents[1:-1], key=lambda se: V.tension_score(text[se[0]:se[1]]) + len(V.cue_scores(text[se[0]:se[1]])))
                pick.append(scored)
            if len(sents) > 1:
                pick.append(sents[-1])
            quote = " … ".join(text[x:y] for x, y in sorted(set(pick)))[:900]
            notes.append((sc, quote, [{"start": x, "end": y} for x, y in sorted(set(pick))]))
        with ctx.db.tx() as conn:
            if have.get((ch["idx"], 0, "extractive")) and have[(ch["idx"], 0, "extractive")][:1] == [{"sha": digest}]:
                skipped += 1
            else:
                conn.execute("DELETE FROM story_notes WHERE book_id=? AND chapter_idx=? AND kind='summary' AND source='extractive'", (book_id, ch["idx"]))
                for sc, quote, refs in notes:
                    conn.execute("""INSERT INTO story_notes(book_id, chapter_idx, scene_idx, kind, text, source, confidence, confirmed, refs_json, created_at)
                                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (book_id, ch["idx"], sc, "summary", quote, "extractive", 1.0, 0, dumps([{"sha": digest}, *refs]), now()))
                made += 1
        if client is not None and client.enabled and not have.get((ch["idx"], None, "llm")):
            try:
                excerpt = text[:6000]
                out = client.chat("Summarise the chapter excerpt in at most 3 sentences. Use only events stated in the text; never add events, names or motives. Reply as JSON: {\"summary\": str}.", excerpt)
                summ = str(extract_json(out).get("summary", ""))
            except (LLMError, ValueError):
                summ = ""
            caps = set(re.findall(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-z]{2,})\b", summ))
            if summ and all(c.lower() in text.lower() for c in caps):
                with ctx.db.tx() as conn:
                    conn.execute("""INSERT INTO story_notes(book_id, chapter_idx, scene_idx, kind, text, source, confidence, confirmed, refs_json, created_at)
                                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (book_id, ch["idx"], None, "summary", summ[:900], "llm", 0.6, 0, dumps([{"sha": digest}]), now()))
            elif summ:
                rejected += 1
    return {"chapters": len(chapters), "made": made, "skipped": skipped, "rejected_llm": rejected}


# --------------------------------------------------------------------------- director


def segment_cues(conn, seg: dict) -> dict:
    """Aggregate the Pass 1 cues of the spans a segment covers."""
    best = None
    explicit = False
    tension = 0.0
    for r in rows(conn, 'SELECT kind, cues_json FROM nar_spans WHERE chapter_id=? AND "end">? AND start<?', (seg["chapter_id"], seg["src_start"], seg["src_end"])):
        c = loads(r["cues_json"], {})
        if not c:
            continue
        explicit = explicit or bool(c.get("explicit"))
        tension = max(tension, float(c.get("tension", 0)))
        rank = (r["kind"] == "dialogue", int(c.get("intensity", 1)))
        if best is None or rank > best[0]:
            best = (rank, c)
    out = dict(best[1]) if best else {"emotion": "neutral", "intensity": 1, "pace": "normal", "evidence": []}
    out["explicit"], out["tension"] = explicit, tension
    return out


def _ordered(conn, book_id: int) -> list[dict]:
    return rows(conn, """SELECT s.*, c.idx AS ch_idx FROM segments s JOIN chapters c ON c.id=s.chapter_id
                         WHERE s.book_id=? ORDER BY c.idx, s.idx""", (book_id,))


def _speaker_label(seg: dict, chars: dict[int, dict]) -> str | None:
    if seg["character_id"] and seg["character_id"] in chars:
        return chars[seg["character_id"]]["name"]
    return None if seg["span_kind"] in ("dialogue", "mixed") else S.NARRATOR_NAME


def _cache_fns(db):
    """Director cache accessors. Each call is its own short transaction: the Director calls these around a slow LLM request,
    and a write lock held across that request starves the worker heartbeat (the web server then kills the worker)."""
    def get(key):
        with db.tx() as conn:
            r = row(conn, "SELECT response_json FROM director_cache WHERE key=?", (key,))
            if r:
                conn.execute("UPDATE director_cache SET hits=hits+1 WHERE key=?", (key,))
                return loads(r["response_json"], None)
        return None

    def put(key, d, model):
        with db.tx() as conn:
            conn.execute("INSERT OR REPLACE INTO director_cache(key, response_json, model, hits, created_at) VALUES (?,?,?,0,?)", (key, dumps(d), model, now()))

    return get, put


def _smooth_and_store(conn, ctx, seg: dict, raw: dict, user: dict, state_in: dict, ns: NarrativeSettings, speaker: str, source: str, key: str | None, actor: str = "system") -> tuple[dict, dict]:
    locked = any(k in user for k in ("emotion", "intensity"))
    base = {**raw, **{k: user[k] for k in ("emotion", "intensity", "pacing", "tension") if k in user}} if locked else raw
    smoothed, state_out, note, kind = CT.apply(base, state_in, speaker=speaker, scene=seg["scene_idx"], chapter=seg["ch_idx"], strength=ns.smoothing_strength,
                                              enabled=ns.continuity_smoothing, reset_scope=ns.reset_scope, locked=locked)
    final = CT.final_of(raw, smoothed, user)
    final["continuity"] = kind
    d = row(conn, "SELECT id FROM directions WHERE segment_id=?", (seg["id"],))
    vals = (source, dumps(raw), dumps(smoothed), dumps(user), dumps(final), f"{kind}: {note}"[:400], 1 if raw.get("needs_review") else 0, key, dumps(state_out), now())
    if d:
        conn.execute("""UPDATE directions SET source=?, raw_json=?, smoothed_json=?, user_json=?, final_json=?, smoothing_note=?, needs_review=?, cache_key=COALESCE(?, cache_key),
                            state_json=?, stale=0, updated_at=? WHERE segment_id=?""", (*vals, seg["id"]))
    else:
        conn.execute("""INSERT INTO directions(segment_id, book_id, source, raw_json, smoothed_json, user_json, final_json, smoothing_note, needs_review, cache_key, state_json, updated_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (seg["id"], seg["book_id"], *vals))
    conn.execute("INSERT INTO direction_log(segment_id, source, payload_json, actor, created_at) VALUES (?,?,?,?,?)",
                 (seg["id"], source, dumps({"final": final, "note": note, "kind": kind}), actor, now()))
    return final, state_out


SCENE_WINDOW = 16  # most segments directed by one scene request


class _Batch:
    """What one director run shares across its chapters and windows. All reads happen in short sessions; nothing here holds a write
    transaction, and none is open while an LLM request is in flight."""

    def __init__(self, ctx, book_id: int, client: LLMClient | None, redo_stale: bool = False):
        self.ctx = ctx
        with ctx.db.session() as conn:
            self.book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
            self.ns = ST.book_ns(ctx, self.book)
            self.segs = _ordered(conn, book_id)
            have = {r["segment_id"]: r for r in rows(conn, "SELECT segment_id, stale FROM directions WHERE book_id=?", (book_id,))}
            self.chars = {c["id"]: c for c in C.list_characters(conn, book_id, include_archived=True)}
            self.notes = rows(conn, "SELECT * FROM story_notes WHERE book_id=? ORDER BY id", (book_id,))
        self.book_id = book_id
        self.have = have
        self.order = [i for i, s in enumerate(self.segs) if s["id"] not in have or (redo_stale and have[s["id"]]["stale"])]
        self.client = client if client is not None else make_llm(self.ns, secrets=ctx.secrets)
        self.director = D.Director(self.client)
        self.get, self.put = _cache_fns(ctx.db)
        self.texts: dict[int, dict] = {}   # chapter_id -> text, meta, paragraph starts
        self.plans: dict[int, dict | None] = {}  # chapter_id -> beat sheet (None: none available)
        self.scene_client = None
        if self.director.llm is not None:
            self.scene_client = self.client.derive(model=resolve_models(self.client.cfg)["scene"])

    def chapter(self, conn, chapter_id: int) -> dict:
        if chapter_id not in self.texts:
            ct = row(conn, "SELECT t.text, t.meta_json, t.sha256, c.idx, c.title FROM chapter_text t JOIN chapters c ON c.id=t.chapter_id WHERE t.chapter_id=?", (chapter_id,))
            self.texts[chapter_id] = {"id": chapter_id, "idx": ct["idx"], "title": ct["title"], "text": ct["text"], "sha256": ct["sha256"],
                                      "meta": loads(ct["meta_json"], {}), "starts": B.paragraph_starts(ct["text"])}
        return self.texts[chapter_id]

    def plan(self, chapter_id: int) -> dict | None:
        """The chapter's beat sheet, planned (one thinking request, no lock held) the first time a window of that chapter is directed."""
        if chapter_id not in self.plans:
            with self.ctx.db.session() as conn:
                ch = self.chapter(conn, chapter_id)
            self.plans[chapter_id] = B.ensure_chapter_plan(self.ctx, self.book, ch, self.client, self.ns, self.get, self.put, stats=self.director.stats)
        return self.plans[chapter_id]


def _seg_payload(conn, bt: _Batch, i: int) -> tuple[dict, dict, dict, str]:
    """The Director's input for segment ``i`` -> (payload, existing user overrides, continuity state going in, speaker name)."""
    segs, chars, notes, ns = bt.segs, bt.chars, bt.notes, bt.ns
    seg = segs[i]
    prev = segs[i - 1] if i > 0 else None
    prev_dir = row(conn, "SELECT final_json, state_json FROM directions WHERE segment_id=?", (prev["id"],)) if prev else None
    state_in = loads(prev_dir["state_json"], None) if prev_dir else None
    state_in = state_in or CT.new_state()
    ct = bt.chapter(conn, seg["chapter_id"])
    ctxw = W.context_window(ct["text"], seg["src_start"], seg["src_end"], ns.context_before_chars, ns.context_after_chars, ct["meta"].get("scene_starts"))
    label = _speaker_label(seg, chars)
    ch = chars.get(seg["character_id"]) if seg["character_id"] else None
    nxt = segs[i + 1] if i + 1 < len(segs) and segs[i + 1]["chapter_id"] == seg["chapter_id"] else None
    up = None
    if nxt is not None and nxt["src_start"] is not None:
        nc = segment_cues(conn, nxt)
        up = {"emotion": nc["emotion"], "intensity": nc["intensity"], "speaker": _speaker_label(nxt, chars)}
    summaries = [n["text"][:500] for n in notes if n["chapter_idx"] == seg["ch_idx"] and n["kind"] == "summary" and n["scene_idx"] in (None, seg["scene_idx"])][:3]
    facts = [n["text"][:300] for n in notes if n["kind"] == "fact" and n["confirmed"]][:5]
    existing = row(conn, "SELECT user_json FROM directions WHERE segment_id=?", (seg["id"],))
    user = loads(existing["user_json"], {}) if existing else {}
    hints = (ch["delivery_pref"] if ch else "") or ""
    speaker_name = label or "Narrator"
    p = {"sid": seg["sid"], "text": W.narration_window(seg["original_text"]), "kind": seg["span_kind"], "speaker": label, "voice_label": None,
         "chapter": {"index": seg["ch_idx"]}, "scene": seg["scene_idx"], "context": ctxw, "notes": summaries + facts, "summaries": summaries, "facts": facts,
         "profile": C.profile_for_prompt(ch) if ch else {}, "prev": ({"speaker": _speaker_label(prev, chars), **loads(prev_dir["final_json"], {})} if prev_dir else None),
         "speaker_state": state_in["speakers"].get(speaker_name), "upcoming": up, "hints": hints, "sensitivity": bt.ns.emotion_sensitivity,
         "cues": segment_cues(conn, seg), "cast_names": [c["name"] for c in chars.values() if not c["archived"]]}
    if p["prev"]:
        p["prev"] = {k: p["prev"].get(k) for k in ("speaker", "emotion", "intensity", "pacing", "delivery")}
    return p, user, state_in, speaker_name


def _windows(segs: list[dict], picked: list[int], cap: int = SCENE_WINDOW) -> list[list[int]]:
    """Group segment positions (book order) into runs of adjacent segments of one chapter and scene, at most ``cap`` long."""
    out: list[list[int]] = []
    for i in picked:
        cur = out[-1] if out else None
        if cur and i == cur[-1] + 1 and len(cur) < cap and segs[i]["chapter_id"] == segs[cur[0]]["chapter_id"] and segs[i]["scene_idx"] == segs[cur[0]]["scene_idx"]:
            cur.append(i)
        else:
            out.append([i])
    return out


def _direct_each(bt: _Batch, positions: list[int]) -> int:
    """No LLM: the deterministic engine, one segment at a time (cached per segment)."""
    done = 0
    for i in positions:
        with bt.ctx.db.session() as conn:
            p, user, state_in, speaker_name = _seg_payload(conn, bt, i)
        raw, key = bt.director.direct(p, bt.get, bt.put)
        with bt.ctx.db.tx() as wconn:
            _smooth_and_store(wconn, bt.ctx, bt.segs[i], raw, user, state_in, bt.ns, speaker_name, raw.get("source", "rules"), key)
        done += 1
    return done


def _scene_bounds(ct: dict, offset: int) -> tuple[int, int]:
    starts = sorted({0, *[s for s in (ct["meta"].get("scene_starts") or []) if s > 0]})
    k = max(0, bisect.bisect_right(starts, offset) - 1)
    return starts[k], (starts[k + 1] if k + 1 < len(starts) else len(ct["text"]))


def _beat_signature(beat: dict | None) -> dict | None:
    return {k: beat[k] for k in ("index", "emotion", "intensity", "pacing", "tension", "purpose", "pause")} if beat else None


def _direct_window(bt: _Batch, window: list[int]) -> int:
    """Direct one window of consecutive segments of one scene: chapter plan (once per chapter), ONE scene request with no lock held,
    then ONE short write transaction that smooths and stores each segment in order."""
    ns, segs, director = bt.ns, bt.segs, bt.director
    first, last = segs[window[0]], segs[window[-1]]
    plan = bt.plan(first["chapter_id"]) if ns.director_chapter_plan else None
    with bt.ctx.db.session() as conn:
        ct = bt.chapter(conn, first["chapter_id"])
        items, users, beats, firsts = [], [], [], []
        state0 = None
        for k, i in enumerate(window):
            p, user, state_in, speaker_name = _seg_payload(conn, bt, i)
            para = B.para_number(ct["starts"], segs[i]["src_start"])
            beat = B.beat_for(plan, para)
            prev_seg = segs[i - 1] if i > 0 and segs[i - 1]["chapter_id"] == segs[i]["chapter_id"] and segs[i - 1]["src_start"] is not None else None
            prev_beat = B.beat_for(plan, B.para_number(ct["starts"], prev_seg["src_start"])) if prev_seg is not None else None
            turn = False  # the first segment of a beat whose emotion family differs from the beat before: a planned turn
            if beat is not None and beat["index"] > 0 and (prev_beat is None or prev_beat["index"] != beat["index"]):
                before = B.beat_at(plan, beat["index"] - 1)
                turn = before is not None and V.FAMILIES.get(before["emotion"]) != V.FAMILIES.get(beat["emotion"])
            p["beat"] = beat
            items.append(p), users.append(user), beats.append(beat), firsts.append(turn)
            if k == 0:
                state0 = state_in
        i0 = window[0]
        tail = []
        for j in (i0 - 2, i0 - 1):  # the two segments just before the window, when they are in the same scene
            if j >= 0 and segs[j]["chapter_id"] == first["chapter_id"] and segs[j]["scene_idx"] == first["scene_idx"]:
                d = row(conn, "SELECT final_json FROM directions WHERE segment_id=?", (segs[j]["id"],))
                if d:
                    f = loads(d["final_json"], {})
                    tail.append({"speaker": _speaker_label(segs[j], bt.chars) or "Narrator", "emotion": f.get("emotion"), "intensity": f.get("intensity"),
                                 "pacing": f.get("pacing"), "delivery": (f.get("delivery") or "")[:100]})
    s0, s1 = _scene_bounds(ct, first["src_start"])
    window_ctx = W.context_window(ct["text"][s0:s1], first["src_start"] - s0, last["src_end"] - s0, min(ns.context_before_chars, 800), min(ns.context_after_chars, 800))
    first_beat = beats[0]
    scene_ctx = {"chapter": first["ch_idx"], "scene": first["scene_idx"],
                 "summary": (first_beat["scene_summary"] if first_beat and first_beat.get("scene_summary") else " ".join(items[0]["summaries"][:2]))[:900],
                 "arc": (plan or {}).get("arc", ""), "facts": items[0]["facts"], "previous": tail,
                 "before": window_ctx["before"], "after": window_ctx["after"]}
    results = director.direct_scene(items, scene_ctx, bt.get, bt.put, bt.scene_client)  # no write lock is held here
    for p in items:
        d = results[p["sid"]][0]
        cues = p.get("cues") or {}
        disagrees = bool(cues.get("explicit")) and V.FAMILIES.get(cues.get("emotion")) != V.FAMILIES.get(d["emotion"])
        if d["confidence"]["overall"] < 0.5 or d["needs_review"] or disagrees:
            director.stats["would_escalate"] += 1  # logged only: a stronger model could be asked about these
    with bt.ctx.db.tx() as wconn:
        state = state0
        for k, i in enumerate(window):
            raw, key = results[items[k]["sid"]]
            raw = {**raw}
            if beats[k] is not None:
                raw["beat"] = _beat_signature(beats[k])
            if firsts[k]:
                raw["support"] = True  # a turn the chapter plan intended must not be smoothed flat
            speaker_name = items[k]["speaker"] or "Narrator"
            _final, state = _smooth_and_store(wconn, bt.ctx, segs[i], raw, users[k], state, ns, speaker_name, raw.get("source", "rules"), key)
    return len(window)


# ------------------------------------------------------------------------- LLM translator

TRANSLATE_FAIL_STREAK = 3  # consecutive failed requests after which one run stops asking (the template narrates the rest)


def translate_pending(ctx, book_id: int, segment_ids: list[int], client: LLMClient | None = None, stats: dict | None = None) -> dict:
    """Make sure the segments' final directions have an LLM-translated instruction in the director cache, so plan_for_segment can read it.
    One read-only session gathers what each segment needs, then one request per DISTINCT missing translation runs with no lock held (the
    cache writes are their own short transactions). Only segments whose voice takes an instruction and whose template text is not empty are
    considered. -> the counters ``translations``, ``translation_cache_hits``, ``translation_failures`` (added to ``stats`` when given)."""
    out = stats if stats is not None else {}
    for k in ("translations", "translation_cache_hits", "translation_failures"):
        out.setdefault(k, 0)
    wanted: list[dict] = []
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        ns = ST.book_ns(ctx, book) if book else None
        if ns is None or not ns.director_enabled or ns.delivery.translator != "llm" or not DL.translator_label(ns.llm):
            return out
        summaries = rows(conn, "SELECT chapter_idx, scene_idx, text FROM story_notes WHERE book_id=? AND kind='summary' ORDER BY id", (book_id,))
        for sid in segment_ids:
            seg = row(conn, "SELECT s.*, c.idx AS ch_idx FROM segments s JOIN chapters c ON c.id=s.chapter_id WHERE s.id=? AND s.book_id=?", (sid, book_id))
            if not seg:
                continue
            try:
                plan = ST.plan_for_segment(conn, ctx, seg, book)
            except ValueError:  # no narrator voice assigned: nothing to plan
                continue
            final = plan.direction
            if not final or DL.build_instruct(final, plan.voice_kind, plan.voice.model_id, ns.delivery)[0] is None:
                continue  # no direction, a voice without an instruction input, delivery off, or neutral delivery
            speaker, hints = DL.speaker_and_hints(conn, seg)
            summary = " ".join(n["text"][:400] for n in summaries if n["chapter_idx"] == seg["ch_idx"] and n["scene_idx"] in (None, seg["scene_idx"]))[:600]
            prev = row(conn, """SELECT d.final_json FROM directions d JOIN segments p ON p.id=d.segment_id
                                WHERE p.chapter_id=? AND p.idx<? ORDER BY p.idx DESC LIMIT 1""", (seg["chapter_id"], seg["idx"]))
            pf = loads(prev["final_json"], {}) if prev else {}
            prev_delivery = ", ".join(str(x) for x in (pf.get("emotion"), pf.get("pacing"), pf.get("delivery")) if x)
            forbidden = [seg["original_text"] or "", plan.text, summary]
            if seg["src_start"] is not None:
                ct = row(conn, "SELECT text, meta_json FROM chapter_text WHERE chapter_id=?", (seg["chapter_id"],))
                if ct:
                    cw = W.context_window(ct["text"], seg["src_start"], seg["src_end"], ns.context_before_chars, ns.context_after_chars, loads(ct["meta_json"], {}).get("scene_starts"))
                    forbidden += [cw["before"], cw["after"]]
            wanted.append({"final": final, "voice_kind": plan.voice_kind, "speaker": speaker, "hints": hints, "scene_summary": summary,
                           "prev_delivery": prev_delivery, "text_excerpt": plan.text[:200], "forbidden": forbidden, "max_chars": ns.delivery.max_instruct_chars})
    client = client if client is not None else make_llm(ns, secrets=ctx.secrets)
    label = DL.translator_label(client.cfg)
    if not label:
        return out
    get, put = _cache_fns(ctx.db)
    seen: set[str] = set()
    streak = 0
    for w in wanted:  # no database session is open from here on
        key = DL.translate_cache_key(w["final"], w["voice_kind"], label, w["max_chars"], w["speaker"], w["hints"])
        if key in seen:
            continue  # identical directions share one translation
        seen.add(key)
        failed = out["translation_failures"]
        DL.translate_llm(client, cache_get=get, cache_put=put, stats=out, **w)
        streak = streak + 1 if out["translation_failures"] > failed else 0
        if streak >= TRANSLATE_FAIL_STREAK:
            break
    return out


def _translate_batch(bt: _Batch, segment_ids: list[int]) -> None:
    """Translate the directions just stored (when the LLM translator is on). A translator problem must never stop directing."""
    if bt.ns.delivery.translator != "llm" or not segment_ids:
        return
    try:
        translate_pending(bt.ctx, bt.book_id, segment_ids, bt.client, bt.director.stats)
    except Exception:
        log.exception("delivery translation failed")


def _finish_batch(bt: _Batch, done: int, remaining: int, t0: float) -> dict:
    book_id, director = bt.book_id, bt.director
    with bt.ctx.db.tx() as conn:
        ST.set_stage(conn, book_id, "director", "done" if not remaining else "running",
                     {"directed": scalar(conn, "SELECT COUNT(*) FROM directions WHERE book_id=?", (book_id,), 0), "remaining": remaining,
                      "model": director.model_label, **{k: _acc(conn, book_id, "director", k, v) for k, v in director.stats.items()},
                      "seconds": round(_acc(conn, book_id, "director", "seconds", time.time() - t0), 3)})
    return {"directed": done, "remaining": remaining, "seconds": round(time.time() - t0, 3), **director.stats}


def director_batch(ctx, book_id: int, limit: int | None = None, client: LLMClient | None = None, redo_stale: bool = False) -> dict:
    """Direct the next ``limit`` segments (in book order) that have no direction yet.

    With an LLM: for each chapter touched, a beat sheet (one thinking request, cached) and then scene windows of up to 16 consecutive
    segments (one request each). Without one, the rules engine, one segment at a time. A write transaction is only ever open for the
    short store step after a window, never across a request."""
    t0 = time.time()
    bt = _Batch(ctx, book_id, client, redo_stale)
    limit = limit or bt.ns.director_lookahead
    picked = [i for i in bt.order[:limit] if bt.segs[i]["src_start"] is not None]
    done = 0
    if bt.director.llm is None:
        done = _direct_each(bt, picked)
    else:
        for window in _windows(bt.segs, picked):
            done += _direct_window(bt, window)
    _translate_batch(bt, [bt.segs[i]["id"] for i in picked])
    return _finish_batch(bt, done, max(0, len(bt.order) - done), t0)


def direct_scope(ctx, book_id: int, segment_id: int, client: LLMClient | None = None) -> dict:
    """Make sure ``segment_id`` is directed before it is narrated, whatever order the worker picked it in: direct the window of up to 16
    undirected segments of its scene that contains it (waiting for that chapter's beat sheet first). Same return shape as director_batch."""
    t0 = time.time()
    bt = _Batch(ctx, book_id, client)
    pos = next((i for i, s in enumerate(bt.segs) if s["id"] == segment_id), None)
    if pos is None or bt.segs[pos]["id"] in bt.have or bt.segs[pos]["src_start"] is None:
        return _finish_batch(bt, 0, len(bt.order), t0)
    seg = bt.segs[pos]
    same = lambda j: 0 <= j < len(bt.segs) and bt.segs[j]["chapter_id"] == seg["chapter_id"] and bt.segs[j]["scene_idx"] == seg["scene_idx"] \
        and bt.segs[j]["id"] not in bt.have and bt.segs[j]["src_start"] is not None  # noqa: E731
    lo = pos
    while same(lo - 1):
        lo -= 1
    hi = pos
    while same(hi + 1):
        hi += 1
    run = list(range(lo, hi + 1))
    k = (pos - lo) // SCENE_WINDOW * SCENE_WINDOW
    window = run[k:k + SCENE_WINDOW]
    done = _direct_each(bt, window) if bt.director.llm is None else _direct_window(bt, window)
    _translate_batch(bt, [segment_id])  # only the segment about to be narrated; the rest of the window is translated when its turn comes
    return _finish_batch(bt, done, max(0, len(bt.order) - done), t0)


def _acc(conn, book_id: int, stage: str, key: str, add: float) -> float:
    """Accumulate a counter in a stage's stats across batches."""
    r = row(conn, "SELECT stats_json FROM analysis_stages WHERE book_id=? AND stage=?", (book_id, stage))
    return (loads(r["stats_json"], {}).get(key, 0) if r else 0) + add


def store_user_direction(conn, ctx, seg: dict, book: dict, user: dict, actor: str) -> None:
    """Record the user's overrides; create the direction (from the rules engine) if the Director has not run."""
    ns = ST.book_ns(ctx, book)
    d = row(conn, "SELECT * FROM directions WHERE segment_id=?", (seg["id"],))
    chars = {c["id"]: c for c in C.list_characters(conn, book["id"], include_archived=True)}
    ch_idx = scalar(conn, "SELECT idx FROM chapters WHERE id=?", (seg["chapter_id"],), 1)
    if d:
        conn.execute("UPDATE directions SET user_json=?, updated_at=? WHERE segment_id=?", (dumps(user), now(), seg["id"]))
        conn.execute("INSERT INTO direction_log(segment_id, source, payload_json, actor, created_at) VALUES (?,?,?,?,?)", (seg["id"], "user", dumps({"user": user}), actor, now()))
        return
    cues = segment_cues(conn, seg) if seg["src_start"] is not None else {}
    raw = D.rules_direction(seg["sid"], _speaker_label(seg, chars), cues, ns.emotion_sensitivity, seg["original_text"])
    _smooth_and_store(conn, ctx, {**seg, "ch_idx": ch_idx}, raw, user, CT.new_state(), ns, _speaker_label(seg, chars) or "Narrator", "user", None, actor)


def recompute_from(ctx, book_id: int, segment_id: int) -> dict:
    """Re-run continuity smoothing from one segment forward using the stored raw directions (no new
    LLM calls). Stops as soon as a segment's result and state are unchanged, or at the cascade limit."""
    with ctx.db.tx() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        ns = ST.book_ns(ctx, book)
        segs = _ordered(conn, book_id)
        chars = {c["id"]: c for c in C.list_characters(conn, book_id, include_archived=True)}
        pos = next((i for i, s in enumerate(segs) if s["id"] == segment_id), None)
        if pos is None:
            return {"recomputed": 0, "changed": []}
        prev = row(conn, "SELECT state_json FROM directions WHERE segment_id=?", (segs[pos - 1]["id"],)) if pos > 0 else None
        state = (loads(prev["state_json"], None) if prev else None) or CT.new_state()
        changed, count = [], 0
        for i in range(pos, len(segs)):
            seg = segs[i]
            d = row(conn, "SELECT * FROM directions WHERE segment_id=?", (seg["id"],))
            if not d:
                break
            if i - pos >= ns.cascade_limit:
                conn.execute("UPDATE directions SET stale=1 WHERE segment_id=?", (seg["id"],))
                break
            raw, user = loads(d["raw_json"], {}), loads(d["user_json"], {})
            before_final, before_state = d["final_json"], d["state_json"]
            final, state = _smooth_and_store(conn, ctx, seg, raw, user, state, ns, _speaker_label(seg, chars) or "Narrator", d["source"], None)
            count += 1
            if dumps(final) != before_final:
                changed.append(seg["id"])
            if i > pos and dumps(final) == before_final and dumps(state) == before_state:
                break
        reset = ST.invalidate_changed(conn, ctx, book_id, only_ids=changed) if ns.director_enabled else []
        ST.set_stage(conn, book_id, "continuity", "done", {"recomputed": count, "changed": len(changed), "audio_reset": len(reset)})
    return {"recomputed": count, "changed": changed, "audio_reset": reset}


# ----------------------------------------------------------------------- worker job


def enqueue(ctx, book_id: int, stages: list[str] | None = None, rebuild: bool = False, confirm: bool = False) -> dict:
    from ..services import NeedsConfirmation, ServiceError, get_job

    stages = stages or list(ST.STAGES)
    bad = [s for s in stages if s not in ST.STAGES]
    if bad:
        raise ServiceError(f"Unknown stage(s): {', '.join(bad)}", 422)
    with ctx.db.tx() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=? AND archived=0", (book_id,))
        if not b:
            raise ServiceError("Book not found", 404)
        existing = row(conn, "SELECT id FROM jobs WHERE book_id=? AND kind='analyze' AND status IN ('queued','running','paused')", (book_id,))
        if existing:
            return {**get_job(ctx, existing["id"]), "created": False}
        legacy = scalar(conn, "SELECT COUNT(*) FROM segments WHERE book_id=? AND src_start IS NULL", (book_id,), 0)
        rendered = scalar(conn, "SELECT COUNT(*) FROM segments WHERE book_id=? AND status<>'pending'", (book_id,), 0)
        if legacy and not row(conn, "SELECT 1 FROM chapter_text WHERE book_id=? LIMIT 1", (book_id,)):
            rebuild = True
            if rendered and not confirm:
                raise NeedsConfirmation(f"Converting this book to semantic segmentation re-cuts every segment; {rendered} rendered segment(s) would be regenerated "
                                        "(their takes are kept as history, not reused).", {"rendered_segments": rendered})
            ns_over = loads(b["narrative_json"], {})
            ns_over["semantic_segmentation"] = True
            conn.execute("UPDATE books SET narrative_json=? WHERE id=?", (dumps(ns_over), book_id))
        jid = conn.execute("INSERT INTO jobs(kind, book_id, payload_json, status, position, created_at, updated_at) VALUES ('analyze',?,?, 'queued', 0, ?, ?)",
                           (book_id, dumps({"stages": stages, "rebuild": rebuild, "progress": {}}), now(), now())).lastrowid
        ctx.job_log(conn, jid, book_id, f"Analysis queued ({', '.join(stages)})")
        ctx.event(conn, "analysis", f"Narrative analysis queued for '{b['title']}'", book_id)
    return {**get_job(ctx, jid), "created": True}


def analysis_step(ctx, job: dict, llm_transport=None) -> str:
    """One bounded unit of analysis work. Returns 'continue' or 'done'. Called repeatedly by the worker, so
    pause/cancel and other jobs are honoured between units."""
    jid, bid = job["id"], job["book_id"]
    p = loads(job["payload_json"], {})
    prog = p.setdefault("progress", {})
    stages = p.get("stages") or list(ST.STAGES)
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (bid,))
    ns = ST.book_ns(ctx, book)
    client = make_llm(ns, llm_transport, ctx.secrets)
    t0 = time.time()

    def save(msg: str | None = None):
        with ctx.db.tx() as conn:
            conn.execute("UPDATE jobs SET payload_json=?, status=CASE WHEN status='queued' THEN 'running' ELSE status END, started_at=COALESCE(started_at, ?), updated_at=? WHERE id=?",
                         (dumps(p), now(), now(), jid))
            if msg:
                ctx.job_log(conn, jid, bid, msg)

    def stage(name: str, status: str, stats: dict | None = None, error: str | None = None):
        with ctx.db.tx() as conn:
            ST.set_stage(conn, bid, name, status, stats, error)

    try:
        if p.get("rebuild") and not prog.get("structure"):
            stage("structure", "running")
            r = ST.rebuild_structure(ctx, bid)
            prog["structure"] = True
            prog["structure_seconds"] = r["seconds"]
            save(f"Pass 1 complete in {r['seconds']}s; {r['review']} dialogue span(s) need review")
            return "continue"
        if "attribution" in stages and not prog.get("attribution") and client.enabled:
            stage("attribution", "running")
            r = run_attribution(ctx, bid, client)
            stage("attribution", "running" if r["remaining"] else "done", r)
            if r["remaining"] and r["tried"]:
                save(f"LLM attribution: {r['resolved']} resolved, {r['unknown']} left for review ({r['remaining']} to go)")
                return "continue"
            prog["attribution"] = True
            prog["resegment"] = False
        elif "attribution" in stages and not client.enabled and not prog.get("attribution"):
            stage("attribution", "skipped", {"reason": "no LLM configured; flagged speakers stay flagged for review"})
            prog["attribution"] = True
        if "segmentation" in stages and not prog.get("resegment"):
            stage("segmentation", "running")
            r = ST.resegment_book(ctx, bid)
            with ctx.db.tx() as conn:
                reset = ST.invalidate_changed(conn, ctx, bid)
            prog["resegment"] = True
            prog["reset"] = len(reset)
            save(f"Pass 2: {r['kept']} segment(s) unchanged, {r['created']} new, {r['retired']} retired; {len(reset)} regenerated for voice changes")
            return "continue"
        if "summaries" in stages and ns.chapter_summaries and not prog.get("summaries"):
            stage("summaries", "running")
            r = run_summaries(ctx, bid, client if client.enabled else None)
            stage("summaries", "done", r)
            prog["summaries"] = True
            save(f"Summaries: {r['made']} chapter(s) summarised, {r['skipped']} unchanged")
            return "continue"
        if "director" in stages and ns.director_enabled:
            r = director_batch(ctx, bid, ns.director_lookahead, client, redo_stale=True)
            prog["directed"] = prog.get("directed", 0) + r["directed"]
            if r["directed"]:
                save(f"Director: {prog['directed']} segment(s) directed ({r['cache_hits']} cached, {r['llm_calls']} LLM calls, {r['rules']} rules)")
            if r["remaining"] and r["directed"]:
                return "continue"
        elif "director" in stages:
            stage("director", "skipped", {"reason": "Narrative Director is off for this book"})
        with ctx.db.tx() as conn:
            res = {"stages": {x["stage"]: x["status"] for x in rows(conn, "SELECT stage, status FROM analysis_stages WHERE book_id=?", (bid,))}, "progress": prog}
            conn.execute("UPDATE jobs SET status='completed', result_json=?, finished_at=?, updated_at=? WHERE id=?", (dumps(res), now(), now(), jid))
            ctx.job_log(conn, jid, bid, "Analysis finished")
            ctx.event(conn, "analysis", f"Narrative analysis finished for '{book['title']}'", bid)
        return "done"
    except Exception as exc:
        with ctx.db.tx() as conn:
            ST.set_stage(conn, bid, "director" if prog.get("resegment") else "structure", "error", None, f"{type(exc).__name__}: {exc}"[:400])
        raise
