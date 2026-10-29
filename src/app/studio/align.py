"""Word alignment between the intended narration text and the independent ASR transcript.

Produces word error rate plus classified, located issues: dropped words, cut-off endings,
repeated phrases, hallucinated/extra speech and substitutions. Agreement with Whisper is
*evidence*, not proof, that the narration is right - see the QA limitations in the README.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from .normalize import compare_tokens

SOFT_SUB_WEIGHT = 0.3


@dataclass
class Op:
    op: str  # eq | sub | del | ins
    ref: str | None
    hyp: str | None
    ri: int | None
    hj: int | None


def align_tokens(ref: list[str], hyp: list[str]) -> list[Op]:
    n, m = len(ref), len(hyp)
    # dp[i][j] = edit distance between ref[:i] and hyp[:j]
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = i
    for j in range(1, m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        ri = ref[i - 1]
        row, prev = dp[i], dp[i - 1]
        for j in range(1, m + 1):
            cost = 0 if ri == hyp[j - 1] else 1
            row[j] = min(prev[j - 1] + cost, prev[j] + 1, row[j - 1] + 1)
    ops: list[Op] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + (0 if ref[i - 1] == hyp[j - 1] else 1):
            ops.append(Op("eq" if ref[i - 1] == hyp[j - 1] else "sub", ref[i - 1], hyp[j - 1], i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i > 0 and dp[i][j] == dp[i - 1][j] + 1:
            ops.append(Op("del", ref[i - 1], None, i - 1, None))
            i -= 1
        else:
            ops.append(Op("ins", None, hyp[j - 1], None, j - 1))
            j -= 1
    ops.reverse()
    return ops


def _similar(a: str, b: str) -> bool:
    if a.isdigit() or b.isdigit():
        return False
    return SequenceMatcher(None, a, b).ratio() >= 0.8 or (len(a) > 3 and len(b) > 3 and (a.startswith(b) or b.startswith(a)))


def _hyp_times(words: list[dict] | None, hyp_tokens: list[str], duration: float | None) -> list[tuple[float, float] | None]:
    """Map each normalised hypothesis token to (start, end) seconds where possible."""
    if words:
        flat: list[tuple[float, float]] = []
        for w in words:
            toks = compare_tokens(str(w.get("word", "")))
            s, e = float(w.get("start") or 0.0), float(w.get("end") or 0.0)
            for _ in toks:
                flat.append((s, e))
        if len(flat) == len(hyp_tokens):
            return list(flat)
    if duration and hyp_tokens:
        step = duration / len(hyp_tokens)
        return [(k * step, (k + 1) * step) for k in range(len(hyp_tokens))]
    return [None] * len(hyp_tokens)


def evaluate(ref_text: str, hyp_text: str, words: list[dict] | None = None, duration: float | None = None,
             max_dropped: int = 2, max_inserted: int = 2) -> dict:
    ref = compare_tokens(ref_text)
    hyp = compare_tokens(hyp_text)
    ops = align_tokens(ref, hyp)
    times = _hyp_times(words, hyp, duration)

    def t_at(j: int | None) -> float | None:
        if j is None or not times or j < 0 or j >= len(times) or times[j] is None:
            return None
        return round(times[j][0], 2)

    S = D = I = 0
    soft = 0
    subs: list[str] = []
    for o in ops:
        if o.op == "sub":
            if _similar(o.ref or "", o.hyp or ""):
                soft += 1
            else:
                S += 1
            subs.append(f"{o.ref}→{o.hyp}")
        elif o.op == "del":
            D += 1
        elif o.op == "ins":
            I += 1
    N = max(1, len(ref))
    wer_raw = (S + soft + D + I) / N
    wer = (S + SOFT_SUB_WEIGHT * soft + D + I) / N

    issues: list[dict] = []
    # nearest hypothesis index for locating deletions
    last_hj = None
    k = 0
    while k < len(ops):
        o = ops[k]
        if o.hj is not None:
            last_hj = o.hj
        if o.op in ("del", "ins", "sub"):
            run_kind = o.op
            start = k
            while k + 1 < len(ops) and ops[k + 1].op == run_kind:
                k += 1
            run = ops[start:k + 1]
            at_start = start == 0
            at_end = k == len(ops) - 1
            if run_kind == "del":
                words_ = [r.ref for r in run]
                loc = t_at(last_hj)
                if at_end and len(words_) >= 2:
                    issues.append(_issue("error", "truncated", f"Ending cut off - missing: “{' '.join(words_)}”", loc, words_))
                elif len(words_) > max_dropped:
                    issues.append(_issue("error", "dropped", f"Dropped {len(words_)} words: “{' '.join(words_)}”", loc, words_))
                else:
                    issues.append(_issue("warning", "dropped", f"Missing word(s): “{' '.join(words_)}”", loc, words_))
            elif run_kind == "ins":
                words_ = [r.hyp for r in run]
                hj0 = run[0].hj or 0
                L = len(words_)
                before = hyp[max(0, hj0 - L):hj0]
                after = hyp[hj0 + L: hj0 + 2 * L]
                loc = t_at(run[0].hj)
                if L >= 1 and (before == words_ or after == words_) and (L >= 2 or len(words_[0]) > 3):
                    issues.append(_issue("error" if L >= 2 else "warning", "repetition", f"Repeated: “{' '.join(words_)}”", loc, words_))
                elif at_end and L >= 2:
                    issues.append(_issue("error", "hallucination", f"Extra speech after the text ends: “{' '.join(words_)}”", loc, words_))
                elif at_start and L >= 2:
                    issues.append(_issue("error", "hallucination", f"Extra speech before the text: “{' '.join(words_)}”", loc, words_))
                elif L > max_inserted:
                    issues.append(_issue("error", "hallucination", f"Inserted {L} words not in the text: “{' '.join(words_)}”", loc, words_))
                else:
                    issues.append(_issue("warning", "inserted", f"Extra word(s): “{' '.join(words_)}”", loc, words_))
            else:
                if len(run) >= 4:
                    words_ = [f"{r.ref}→{r.hyp}" for r in run]
                    issues.append(_issue("error", "garbled", f"Garbled passage ({len(run)} consecutive substitutions)", t_at(run[0].hj), words_))
        k += 1
    hard_subs = [f"{o.ref}→{o.hyp}" for o in ops if o.op == "sub" and not _similar(o.ref or "", o.hyp or "")]
    if hard_subs:
        issues.append(_issue("warning", "substitution", "Substitutions: " + ", ".join(hard_subs[:12]) + (" …" if len(hard_subs) > 12 else ""), None, hard_subs[:30]))
    soft_subs = [f"{o.ref}→{o.hyp}" for o in ops if o.op == "sub" and _similar(o.ref or "", o.hyp or "")]
    if soft_subs:
        issues.append(_issue("info", "spelling", "Near-identical spellings (weighted lightly, often names): " + ", ".join(soft_subs[:12]), None, soft_subs[:30]))
    return {
        "ref_tokens": len(ref),
        "hyp_tokens": len(hyp),
        "wer": round(wer, 4),
        "wer_raw": round(wer_raw, 4),
        "counts": {"sub": S, "soft_sub": soft, "del": D, "ins": I},
        "issues": issues,
        "diff": [{"op": o.op, "ref": o.ref, "hyp": o.hyp} for o in ops],
    }


def _issue(severity: str, kind: str, message: str, at: float | None, words: list) -> dict:
    return {"severity": severity, "kind": kind, "message": message, "at_s": at, "words": words, "source": "asr"}
