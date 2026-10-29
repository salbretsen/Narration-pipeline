"""Reference-voice lab: sample scripts to read aloud and an objective quality report for a recording.

The scripts are original texts chosen for phonetic variety (plosives, sibilants, long vowels),
sentence-length variety (a long, comma-rich sentence for breath control), numbers and hard
names, dialogue with questions, and - separately, for optional variants only - emotional range.
"""
from __future__ import annotations

import numpy as np

from . import audio as A

SCRIPTS: list[dict] = [
    {"id": "core-harbour", "tier": "core", "category": "Neutral narration", "title": "Harbour at dawn",
     "purpose": "Calm, even narration - the best all-round primary reference.",
     "text": "The morning fog lay thick over the harbour, and the fishing boats rocked quietly at their moorings. Thomas walked along the quay, counting the masts as he had done every day since he was a boy. Seventeen, he thought. One fewer than last spring."},
    {"id": "core-suitcase", "tier": "core", "category": "Neutral narration", "title": "The long sentence",
     "purpose": "One long, comma-rich sentence: shows how you breathe and pace a flowing line.",
     "text": "She packed the blue suitcase with care, folding each shirt twice, tucking the letters between the pages of a worn atlas, and wondering, as the train whistle sounded somewhere beyond the hills, whether the house she was leaving would ever feel like hers again."},
    {"id": "core-sounds", "tier": "core", "category": "Sounds and rhythm", "title": "Sharp sounds",
     "purpose": "Covers S, SH, TH, B, P, T, K and long vowels so the clone learns your hard consonants.",
     "text": "Sixty-six sharp swallows swooped past the stone bridge at dusk, while quick brown foxes slipped through the thick, wet grass. Ben thought the thunder sounded like distant drums, then laughed at himself for being so jumpy."},
    {"id": "core-numbers", "tier": "core", "category": "Names and numbers", "title": "Dates, numbers and names",
     "purpose": "Spoken numbers, titles and an unusual name (Siobhan) - things books are full of.",
     "text": "On the fourteenth of March, nineteen eighty-seven, Doctor Siobhan Maguire recorded a temperature of thirty-eight point five degrees. By half past four, the clinic on Wexford Street had seen two hundred and twelve patients, and Mr. Okonkwo, the pharmacist, had run out of everything except aspirin."},
    {"id": "core-dialogue", "tier": "core", "category": "Dialogue", "title": "Questions and quiet dialogue",
     "purpose": "Rising question intonation and short replies, read as a narrator rather than acted.",
     "text": "\"Where were you last night?\" she asked. \"Out,\" he said. \"Out where?\" \"Does it matter?\" She set the cup down very gently. \"It does to me.\" Rain tapped at the window, and neither of them looked away."},
    {"id": "core-authority", "tier": "core", "category": "Non-fiction", "title": "Clear, authoritative explanation",
     "purpose": "Measured, formal delivery for non-fiction, with a list structure.",
     "text": "The committee concluded that the proposal, while ambitious, lacked sufficient evidence. Three recommendations follow. First, extend the trial. Second, publish the full data. Third, reconsider the budget before any further commitments are made."},
    {"id": "range-warm", "tier": "range", "emotion": "warm", "category": "Optional range", "title": "Warm and intimate",
     "purpose": "Soft, close and affectionate. Use only to build a separate warm variant.",
     "text": "You did it. I always knew you would, even when you didn't. Come here, sit down, and let me tell you how proud I am. Truly, properly proud."},
    {"id": "range-tense", "tier": "range", "emotion": "tense", "category": "Optional range", "title": "Tense and urgent",
     "purpose": "Held breath and pressure. A separate suspense variant only.",
     "text": "Don't move. Don't even breathe. The footsteps had stopped outside the door, and the handle began to turn, slowly, a quarter of an inch at a time."},
    {"id": "range-joy", "tier": "range", "emotion": "joyful", "category": "Optional range", "title": "Joyful and lively",
     "purpose": "Bright and energetic. A separate upbeat variant only.",
     "text": "We won! Can you believe it? After all those years, all that rain, all those terrible Saturdays, we actually won! Somebody find me a trumpet."},
    {"id": "range-sad", "tier": "range", "emotion": "sad", "category": "Optional range", "title": "Quiet and sorrowful",
     "purpose": "Slow, low and reflective. A separate sombre variant only.",
     "text": "The house was quiet now. She sat on the stairs where he used to sit, and listened to the clock, and tried to remember the sound of his laugh."},
]

AUDITION_TEXT = ("The lighthouse at Gull Point had stood empty for eleven years before Margaret Hale climbed its stairs. "
                 "\"Is anyone there?\" she called. Nothing answered but the wind, patient and cold, off the grey water.")


def script_by_id(sid: str) -> dict | None:
    return next((s for s in SCRIPTS if s["id"] == sid), None)


def word_count(text: str) -> int:
    return len(text.split())


def estimated_seconds(text: str, wpm: float = 155.0) -> float:
    return round(word_count(text) * 60.0 / wpm, 1)


def public_scripts() -> list[dict]:
    return [{**s, "words": word_count(s["text"]), "est_seconds": estimated_seconds(s["text"])} for s in SCRIPTS]


def analyze_recording(path) -> dict:
    """Objective analysis of the raw recording (before the studio trims or normalises it)."""
    tmp = A.temp_sibling(__import__("pathlib").Path(path), ".wav")
    try:
        A.run_ffmpeg(["-i", str(path), "-vn", "-ac", "1", "-ar", "24000", "-sample_fmt", "s16", "-f", "wav", str(tmp)], timeout=120)
        a, sr = A.read_wav(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    result = A.analyze(a, sr, None, {"clip_ratio_limit": 0.0005, "min_rms_dbfs": -50, "max_rms_dbfs": -3, "dead_air_seconds": 1.5})
    return {"metrics": result["metrics"], "issues": result["issues"]}


def quality_report(metrics: dict) -> dict:
    """Plain-language checks with a verdict: excellent / good / usable / poor."""
    checks: list[dict] = []

    def add(name, status, detail, tip=""):
        checks.append({"name": name, "status": status, "detail": detail, "tip": tip})

    dur = metrics.get("duration_s", 0.0)
    if dur < 3:
        add("Length", "fail", f"{dur:.1f}s is too short.", "Record at least 5 seconds; 8-15 seconds is ideal.")
    elif dur < 5:
        add("Length", "warn", f"{dur:.1f}s is short.", "Aim for 8-15 seconds of continuous speech.")
    elif dur <= 20:
        add("Length", "pass", f"{dur:.1f}s" + (" - ideal." if 8 <= dur <= 15 else "."), "")
    elif dur <= 30:
        add("Length", "warn", f"{dur:.1f}s is long.", "Very long references slow generation and can make the voice drift. Trim to 8-15 seconds.")
    else:
        add("Length", "fail", f"{dur:.1f}s is too long.", "Keep references under 30 seconds.")

    peak, clip = metrics.get("peak_dbfs", -90), metrics.get("clip_ratio", 0.0)
    if clip > 0.0005 or peak >= -0.5:
        add("Clipping", "fail", f"Peaks hit full scale (peak {peak:.1f} dBFS).", "Move back from the microphone or lower its input gain, then re-record.")
    elif peak > -3:
        add("Clipping", "warn", f"Peak {peak:.1f} dBFS is very hot.", "Lower the input gain a little for safety margin.")
    elif peak < -30:
        add("Clipping", "warn", f"Peak {peak:.1f} dBFS is very low.", "Raise the input gain or move closer.")
    else:
        add("Clipping", "pass", f"Peak {peak:.1f} dBFS - healthy headroom.", "")

    rms = metrics.get("rms_dbfs", -90)
    if rms < -42:
        add("Speech level", "fail", f"Speech averages {rms:.1f} dBFS - far too quiet.", "Move closer (a hand-span, about 15-20 cm) or raise the input gain.")
    elif rms < -32:
        add("Speech level", "warn", f"Speech averages {rms:.1f} dBFS - quiet.", "Move a little closer or raise the input gain. Aim for about -26 to -16 dBFS.")
    elif rms > -10:
        add("Speech level", "warn", f"Speech averages {rms:.1f} dBFS - very loud.", "Lower the gain.")
    else:
        add("Speech level", "pass", f"Speech averages {rms:.1f} dBFS.", "")

    floor = metrics.get("noise_floor_dbfs", -90)
    snr = rms - floor
    if snr < 12:
        add("Background noise", "warn", "No quiet moment found, so the background noise cannot be measured.",
            "Leave about half a second of silence before and after you speak. The Room check in step 2 measures your room directly.")
    elif floor <= -55:
        add("Background noise", "pass", f"Noise floor {floor:.0f} dBFS - very quiet room.", "")
    elif floor <= -50:
        add("Background noise", "pass", f"Noise floor {floor:.0f} dBFS - good.", "")
    elif floor <= -42:
        add("Background noise", "warn", f"Noise floor {floor:.0f} dBFS - audible background.", "Switch off fans, air conditioning and appliances, close windows, and try a smaller, softer room.")
    else:
        add("Background noise", "fail", f"Noise floor {floor:.0f} dBFS - too noisy for cloning.", "The clone will reproduce this noise. Find a quieter room or time of day.")
    if snr < 12:
        pass
    elif snr < 25:
        add("Signal-to-noise", "fail", f"Only {snr:.0f} dB between voice and noise.", "Get closer to the microphone and reduce background noise.")
    elif snr < 35:
        add("Signal-to-noise", "warn", f"{snr:.0f} dB between voice and noise.", "Good enough to try; closer to the mic would be better.")
    else:
        add("Signal-to-noise", "pass", f"{snr:.0f} dB between voice and noise.", "")

    pause = metrics.get("longest_internal_silence_s", 0.0)
    if pause > 1.5:
        add("Pauses", "warn", f"A {pause:.1f}s pause inside the take.", "Long gaps teach the clone to hesitate. Re-record with a steady flow.")
    else:
        add("Pauses", "pass", "Even pacing.", "")
    lead = metrics.get("lead_silence_s", 0.0)
    if lead > 2.0:
        add("Lead-in", "warn", f"{lead:.1f}s of silence before you start.", "Start speaking about half a second after pressing record.")

    fails = sum(1 for c in checks if c["status"] == "fail")
    warns = sum(1 for c in checks if c["status"] == "warn")
    verdict = "poor" if fails else ("usable" if warns >= 2 else ("good" if warns == 1 else "excellent"))
    summary = {"poor": "Not suitable - fix the red items and record again.",
               "usable": "Usable, but a cleaner take will give a better clone.",
               "good": "Good. One small improvement is possible.",
               "excellent": "Excellent reference quality."}[verdict]
    return {"verdict": verdict, "summary": summary, "checks": checks}


_VERDICT_RANK = {"excellent": 3, "good": 2, "usable": 1, "poor": 0}


def compare_takes(takes: list[dict]) -> dict:
    """Side-by-side comparison of several takes of the same reference (their saved metadata).

    Ranking is advisory: verdict first, then fewer warnings, then signal-to-noise. Poor takes are never recommended.
    Transcript match (Whisper) is run per take by the existing check job and merged by the client."""
    rows = []
    for t in takes:
        m, q = t.get("metrics", {}), t.get("quality", {})
        checks = q.get("checks", [])
        snr = round(m.get("rms_dbfs", -90) - m.get("noise_floor_dbfs", -90), 1)
        rows.append({"id": t["id"], "label": t.get("label", ""), "script_title": t.get("script_title", ""), "verdict": q.get("verdict", "poor"),
                     "duration_s": t.get("duration_s", m.get("duration_s", 0.0)), "peak_dbfs": m.get("peak_dbfs"), "rms_dbfs": m.get("rms_dbfs"),
                     "noise_floor_dbfs": m.get("noise_floor_dbfs"), "snr_db": snr, "clip_ratio": m.get("clip_ratio", 0.0),
                     "warnings": sum(1 for c in checks if c["status"] == "warn"), "fails": sum(1 for c in checks if c["status"] == "fail"),
                     "ref_text": t.get("ref_text", "")})
    order = sorted(rows, key=lambda r: (-_VERDICT_RANK.get(r["verdict"], 0), r["warnings"], -r["snr_db"], r["clip_ratio"]))
    for i, r in enumerate(order):
        r["rank"] = i + 1
    best = order[0] if order and order[0]["verdict"] != "poor" else None
    for r in rows:
        r["recommended"] = bool(best and r["id"] == best["id"])
    return {"rows": rows, "recommended_id": best["id"] if best else None}
