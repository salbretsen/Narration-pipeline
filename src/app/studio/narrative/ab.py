"""Blind A/B listening comparisons: the named comparisons, the settings patches behind them and the vote tally.

A comparison renders ONE segment twice with two explicit settings patches (never "whatever the book has switched on"), so
the result does not depend on the book's current configuration. This module is pure data and SQL (no synthesis, no
imports from the store), so ``narrative.store`` can include the tally in the Narrative status.
"""
from __future__ import annotations

ADOPT_SHARE = 0.60          # guidance shown in the UI: adopt a feature when it wins at least this share of the non-tie votes ...
OTHER_NARRATOR_MAX = 0.10   # ... and "sounds like another narrator" is flagged on at most this share of the votes

# kinds: which voice kinds the comparison can change. Presets and designed voices take an instruction; clones take a reference clip.
AB_COMPARISONS: dict[str, dict] = {
    "directed_vs_undirected": {
        "title": "Director's delivery vs plain reading",
        "a_label": "With the Director's delivery", "b_label": "Plain reading (no direction)",
        "a_patch": {"director_enabled": True},
        "b_patch": {"director_enabled": False, "delivery": {"pace_from_direction": "off"}},
        "kinds": ("preset", "design", "clone"),
        "applies": "any voice, for a segment that has a direction",
    },
    "llm_vs_template_translator": {
        "title": "LLM vs template delivery wording",
        "a_label": "LLM-written delivery instruction", "b_label": "Template delivery instruction",
        "a_patch": {"delivery": {"translator": "llm"}}, "b_patch": {"delivery": {"translator": "template"}},
        "kinds": ("preset", "design"),
        "applies": "preset and designed voices (a cloned voice takes no instruction)",
    },
    "bank_vs_base_clip": {
        "title": "Emotion reference bank vs the voice's own clip",
        "a_label": "Emotion clip from the reference bank", "b_label": "The voice's own base clip",
        "a_patch": {"delivery": {"clone": "reference_bank"}}, "b_patch": {"delivery": {"clone": "off"}},
        "kinds": ("clone",),
        "applies": "cloned voices with an adopted bank clip for the segment's emotion",
    },
    "pace_on_vs_off": {
        "title": "Pace from the direction vs steady pace",
        "a_label": "Pace follows the direction", "b_label": "Steady pace",
        "a_patch": {"delivery": {"pace_from_direction": "always"}}, "b_patch": {"delivery": {"pace_from_direction": "off"}},
        "kinds": ("preset", "design", "clone"),
        "applies": "any voice, for a segment whose direction asks for a pace other than normal",
    },
}


def comparison(name: str) -> dict | None:
    return AB_COMPARISONS.get(name)


def catalog() -> list[dict]:
    return [{"id": k, "title": c["title"], "a_label": c["a_label"], "b_label": c["b_label"], "applies": c["applies"]} for k, c in AB_COMPARISONS.items()]


def tally(conn, book_id: int, comparison_id: str | None = None) -> dict:
    """{comparison: {title, a_label, b_label, a_wins, b_wins, same, other_narrator_flags, votes, non_tie, a_share}}.
    Every named comparison is present (zeros when unvoted). a_share is a's share of the non-tie votes, None without any."""
    out = {}
    for k, c in AB_COMPARISONS.items():
        if comparison_id and k != comparison_id:
            continue
        out[k] = {"title": c["title"], "a_label": c["a_label"], "b_label": c["b_label"], "a_wins": 0, "b_wins": 0, "same": 0,
                  "other_narrator_flags": 0, "votes": 0, "non_tie": 0, "a_share": None}
    for r in conn.execute("SELECT comparison, winner, other_narrator FROM ab_votes WHERE book_id=?", (book_id,)).fetchall():
        t = out.get(r[0])
        if t is None:
            continue
        t["votes"] += 1
        t["a_wins" if r[1] == "a" else "b_wins" if r[1] == "b" else "same"] += 1
        t["other_narrator_flags"] += 1 if r[2] else 0
    for t in out.values():
        t["non_tie"] = t["a_wins"] + t["b_wins"]
        t["a_share"] = round(t["a_wins"] / t["non_tie"], 4) if t["non_tie"] else None
        t["other_narrator_share"] = round(t["other_narrator_flags"] / t["votes"], 4) if t["votes"] else None
    return out
