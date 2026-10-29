"""Analysis stages that run in the worker, ahead of synthesis.

Stage order: structure -> attribution (LLM, only for flagged spans) -> characters -> summaries ->
segmentation -> director (+ continuity). Each stage persists its output, so a restart resumes
where it stopped, and unchanged inputs are never re-analysed (the Director is cached by a key
over text, context, profile, previous delivery, model and prompt version).
"""
from __future__ import annotations

import re
import time

from ..config import NarrativeSettings
from ..db import row, rows, scalar
from ..util import dumps, loads, now, short_hash
from . import cast as C
from . import continuity as CT
from . import director as D
from . import store as ST
from . import structure as S
from . import vocab as V
from . import windows as W
from .llm import LLMClient, LLMError, extract_json


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


def _cache_fns(conn):
    def get(key):
        r = row(conn, "SELECT response_json FROM director_cache WHERE key=?", (key,))
        if r:
            conn.execute("UPDATE director_cache SET hits=hits+1 WHERE key=?", (key,))
            return loads(r["response_json"], None)
        return None

    def put(key, d, model):
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


def director_batch(ctx, book_id: int, limit: int | None = None, client: LLMClient | None = None, redo_stale: bool = False) -> dict:
    """Direct the next ``limit`` segments (in book order) that have no direction yet."""
    t0 = time.time()
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        ns = ST.book_ns(ctx, book)
    limit = limit or ns.director_lookahead
    client = client if client is not None else make_llm(ns, secrets=ctx.secrets)
    director = D.Director(client)
    done = 0
    with ctx.db.tx() as conn:
        segs = _ordered(conn, book_id)
        have = {r["segment_id"]: r for r in rows(conn, "SELECT segment_id, stale FROM directions WHERE book_id=?", (book_id,))}
        chars = {c["id"]: c for c in C.list_characters(conn, book_id, include_archived=True)}
        notes = rows(conn, "SELECT * FROM story_notes WHERE book_id=? ORDER BY id", (book_id,))
        get, put = _cache_fns(conn)
        order = [i for i, s in enumerate(segs) if s["id"] not in have or (redo_stale and have[s["id"]]["stale"])]
        for i in order[:limit]:
            seg = segs[i]
            if seg["src_start"] is None:
                continue
            prev = segs[i - 1] if i > 0 else None
            prev_dir = row(conn, "SELECT final_json, state_json FROM directions WHERE segment_id=?", (prev["id"],)) if prev else None
            state_in = loads(prev_dir["state_json"], None) if prev_dir else None
            state_in = state_in or CT.new_state()
            ct = row(conn, "SELECT text, meta_json FROM chapter_text WHERE chapter_id=?", (seg["chapter_id"],))
            meta = loads(ct["meta_json"], {})
            ctxw = W.context_window(ct["text"], seg["src_start"], seg["src_end"], ns.context_before_chars, ns.context_after_chars, meta.get("scene_starts"))
            label = _speaker_label(seg, chars)
            ch = chars.get(seg["character_id"]) if seg["character_id"] else None
            nxt = segs[i + 1] if i + 1 < len(segs) and segs[i + 1]["chapter_id"] == seg["chapter_id"] else None
            up = None
            if nxt is not None and nxt["src_start"] is not None:
                nc = segment_cues(conn, nxt)
                up = {"emotion": nc["emotion"], "intensity": nc["intensity"], "speaker": _speaker_label(nxt, chars)}
            ch_notes = [n["text"][:500] for n in notes if n["chapter_idx"] == seg["ch_idx"] and n["kind"] == "summary" and n["scene_idx"] in (None, seg["scene_idx"])][:3]
            ch_notes += [n["text"][:300] for n in notes if n["kind"] == "fact" and n["confirmed"]][:5]
            existing = row(conn, "SELECT user_json FROM directions WHERE segment_id=?", (seg["id"],))
            user = loads(existing["user_json"], {}) if existing else {}
            hints = (ch["delivery_pref"] if ch else "") or ""
            speaker_name = label or "Narrator"
            p = {"sid": seg["sid"], "text": W.narration_window(seg["original_text"]), "kind": seg["span_kind"], "speaker": label, "voice_label": None,
                 "chapter": {"index": seg["ch_idx"]}, "scene": seg["scene_idx"], "context": ctxw, "notes": ch_notes,
                 "profile": C.profile_for_prompt(ch) if ch else {}, "prev": ({"speaker": _speaker_label(prev, chars), **loads(prev_dir["final_json"], {})} if prev_dir else None),
                 "speaker_state": state_in["speakers"].get(speaker_name), "upcoming": up, "hints": hints, "sensitivity": ns.emotion_sensitivity,
                 "cues": segment_cues(conn, seg), "cast_names": [c["name"] for c in chars.values() if not c["archived"]]}
            if p["prev"]:
                p["prev"] = {k: p["prev"].get(k) for k in ("speaker", "emotion", "intensity", "pacing", "delivery")}
            raw, key = director.direct(p, get, put)
            _smooth_and_store(conn, ctx, {**seg, "ch_idx": seg["ch_idx"]}, raw, user, state_in, ns, speaker_name, raw.get("source", "rules"), key)
            done += 1
        remaining = max(0, len(order) - done)
        ST.set_stage(conn, book_id, "director", "done" if not remaining else "running",
                     {"directed": scalar(conn, "SELECT COUNT(*) FROM directions WHERE book_id=?", (book_id,), 0), "remaining": remaining,
                      "model": director.model_label, **{k: _acc(conn, book_id, "director", k, v) for k, v in director.stats.items()},
                      "seconds": round(_acc(conn, book_id, "director", "seconds", time.time() - t0), 3)})
    return {"directed": done, "remaining": remaining, "seconds": round(time.time() - t0, 3), **director.stats}


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
