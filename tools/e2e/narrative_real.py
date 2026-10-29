"""Real-engine end-to-end test of the v0.3 narrative pipeline against a running studio (the Mac).

Uses the REAL Qwen3-TTS models (CustomVoice, VoiceDesign, Base) and Whisper through the studio's worker.
The Narrative Director runs its deterministic RULES engine unless the studio has a local LLM configured;
no LLM is installed on the Mac, so this script does not test LLM output quality.

usage: python narrative_real.py PASSWORD OUTDIR      (env: STUDIO_BASE, STUDIO_USER, STUDIO_SSH)
Creates a temporary book, voices and settings, measures, then archives/removes what it created and restores
the global narrative settings. The temporary account is created and removed by the caller (see README).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import wave
from pathlib import Path

import httpx
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src" / "app"))
sys.path.insert(0, str(REPO / "src" / "app" / "tests"))
from narrative_fixtures import TXT as BASE_TXT  # noqa: E402
from studio.narrative import voicecheck  # noqa: E402

BASE = os.environ.get("STUDIO_BASE", "http://<mac-host>:8080")
USER = os.environ.get("STUDIO_USER", "e2e-narr")
PW = sys.argv[1]
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)
SSH = ["ssh", "-i", os.path.expanduser("~/.ssh/<ssh-key>"), "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
       os.environ.get("STUDIO_SSH", "<user>@<mac-host>")]
HOME = "/Users/<user>/audiobook-studio-claude"

CH3 = """Chapter 3

The mill stood at the end of the lane, black against a sky the colour of old pewter. Elena Marsh reached the door first, and for a long moment she did not open it.

"Is he in there?" Tom Briggs whispered, out of breath from the climb.

"I think so." She pressed her palm flat against the wet wood. "Stay behind me."

Dr. Hale caught her arm. "Elena, think. If your husband is alive, the man who wrote that letter wanted you to find him."

"Then he will be disappointed," she said coldly, "because I am not afraid."

"You should be!" Hale shouted, and his voice rang off the stones. "Look at the lock. Someone has been here this morning!"

"I am done being told what I should feel."

"Mrs. Marsh, please," Tom began, but she had already pushed the door open.

The room beyond was empty. A single chair stood in the middle of the floor, and on it, folded very small, lay a child's blue scarf. Elena stared at it. The air went out of her all at once, and she sank to her knees on the cold flagstones, and the sound that came from her was not a word at all.

"Oh, Elena," said Hale softly. He knelt beside her and, for the first time in thirty years, found that he had nothing at all to say.
"""
TEXT = BASE_TXT + "\n" + CH3

c = httpx.Client(base_url=BASE, headers={"X-Studio-Request": "1"}, timeout=120)
R: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "base": BASE, "notes": []}
checks: list[tuple[str, bool, str]] = []


def ssh(cmd: str, timeout: int = 60) -> str:
    return subprocess.run(SSH + [cmd], capture_output=True, text=True, timeout=timeout).stdout.strip()


def chk(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, bool(ok), str(detail)[:300]))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{str(detail)[:220]}]" if detail else ""), flush=True)


def api(method: str, path: str, **kw):
    r = c.request(method, path, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"{method} {path} -> {r.status_code} {r.text[:300]}")
    return r.json() if r.headers.get("content-type", "").startswith("application/json") else r


def wait_job(jid: int, timeout: float, rss: list | None = None) -> dict:
    t0 = time.time()
    last_rss = 0.0
    while time.time() - t0 < timeout:
        j = api("GET", f"/api/jobs/{jid}")
        if j["status"] in ("completed", "failed", "cancelled"):
            return j
        if rss is not None and time.time() - last_rss > 12:
            last_rss = time.time()
            v = ssh(f"ps -o rss= -p $(cat {HOME}/run/worker.pid 2>/dev/null) 2>/dev/null")
            if v.strip().isdigit():
                rss.append(int(v) / 1048576)
        time.sleep(2)
    raise TimeoutError(f"job {jid} did not finish in {timeout}s")


def wait_idle(book_id: int, timeout: float, rss: list | None = None) -> float:
    t0 = time.time()
    last_rss = 0.0
    while time.time() - t0 < timeout:
        b = api("GET", f"/api/books/{book_id}")
        if b["pending"] == 0 and b["state"] not in ("queued", "generating"):
            return time.time() - t0
        if rss is not None and time.time() - last_rss > 12:
            last_rss = time.time()
            v = ssh(f"ps -o rss= -p $(cat {HOME}/run/worker.pid 2>/dev/null) 2>/dev/null")
            if v.strip().isdigit():
                rss.append(int(v) / 1048576)
        time.sleep(3)
    raise TimeoutError("book did not finish")


def segs(book_id: int) -> list[dict]:
    out, after = [], None
    while True:
        r = api("GET", f"/api/books/{book_id}/segments", params={"limit": 200, **({"after": after} if after else {})})
        out += r["items"]
        after = r["next"]
        if not after:
            return out


def tokens(s: str) -> list[str]:
    return re.findall(r"\S+", s)


def read_wav(b: bytes) -> tuple[np.ndarray, int]:
    import io

    with wave.open(io.BytesIO(b)) as w:
        sr, n = w.getframerate(), w.getnframes()
        a = np.frombuffer(w.readframes(n), dtype="<i2").astype(np.float64) / 32768.0
    return a, sr


# ------------------------------------------------------------------------------------------ run
login = c.post("/api/auth/login", json={"username": USER, "password": PW})
chk("sign in", login.status_code == 200, login.status_code)
if login.status_code != 200:
    sys.exit(1)
status = api("GET", "/api/auth/status")
R["version"], R["engine"] = status["version"], status["engine"]
chk("studio is the real MLX engine", status["engine"] == "mlx", status["engine"])
original_settings = api("GET", "/api/settings")["settings"]
original_narrative = original_settings["narrative"]
made_voices: list[int] = []
book_id = None
try:
    api("PUT", "/api/settings", json={"narrative": {"semantic_segmentation": True, "director_enabled": True, "director_lookahead": 12,
                                                     "emotion_sensitivity": 0.6, "verify_mode": "every"}})
    voices = {v["name"]: v for v in api("GET", "/api/voices")["voices"]}
    ryan, aiden = voices["Ryan"], voices["Aiden"]

    # ---- import + Pass 1 / Pass 2 (deterministic, runs in the import request)
    t0 = time.time()
    prev = api("POST", "/api/imports", files={"file": ("e2e-flood.txt", TEXT.encode("utf-8"), "text/plain")})
    book = api("POST", f"/api/imports/{prev['token']}/confirm", json={"title": "zz-e2e Narrative Flood", "voice_id": ryan["id"], "queue": False})
    book_id = book["id"]
    R["import_request_seconds"] = round(time.time() - t0, 2)
    nar = api("GET", f"/api/books/{book_id}/narrative")
    R["structure_seconds_server"] = nar["stages"]["structure"]["stats"].get("seconds")
    R["spans"], R["segments"], R["characters"] = nar["stages"]["structure"]["stats"].get("spans"), nar["segments"], nar["characters"]
    R["speaker_review_flags"] = nar["needs_review"]
    chk("semantic book imported", nar["semantic"] and nar["segments"] >= 25, f"{nar['segments']} segments, {nar['characters']} characters, {nar['needs_review']} flagged")
    cs = {ch["name"]: ch for ch in api("GET", f"/api/books/{book_id}/characters")["characters"]}
    chk("characters discovered", {"Hale", "Elena Marsh", "Tom Briggs"} <= set(cs), sorted(cs))

    # ---- fidelity of the segmentation against the manuscript
    ss = segs(book_id)
    src = [ln for ln in TEXT.split("\n") if ln.strip() and not re.fullmatch(r"(Chapter \d+|The Flood|\* \* \*)", ln.strip())]
    seg_tokens = [t for s in ss if s["kind"] != "heading" for t in tokens(s["original_text"])]
    chk("every source word appears once, in order, in the segments", tokens(" ".join(src)) == seg_tokens,
        f"source {len(tokens(' '.join(src)))} words, segments {len(seg_tokens)} words")
    R["segment_length_chars"] = {"max": max(len(s["original_text"]) for s in ss), "median": int(np.median([len(s["original_text"]) for s in ss]))}
    chk("no segment exceeds the 500-character ceiling", R["segment_length_chars"]["max"] <= 500, R["segment_length_chars"])

    # ---- voices: preset (Aiden), designed (Elena), cloned (Tom: reference = real model speech, promoted from a preview)
    d = api("POST", "/api/voices/design", json={"name": "zz-e2e Elena", "description": "A warm adult woman's voice, mid-low pitch, steady and slightly tired, British English."})
    made_voices.append(d["id"])
    # only previews of designed voices can be promoted to a stable (cloned) narrator, so Tom's reference is real VoiceDesign speech
    tom_design = api("POST", "/api/voices/design", json={"name": "zz-e2e Tom source", "description": "A young man's voice, bright and slightly breathless, light London accent."})
    made_voices.append(tom_design["id"])
    ref_text = "The old house on Harrow Lane smelled of damp wool and woodsmoke, and the rain had not stopped for three days."
    pj = api("POST", f"/api/voices/{tom_design['id']}/preview", json={"text": ref_text, "seed": 7})
    pj = wait_job(pj["id"], 300)
    chk("reference preview synthesised by the real model", pj["status"] == "completed", pj.get("error"))
    clone = api("POST", "/api/voices/promote", json={"job_id": pj["id"], "name": "zz-e2e Tom clone"})
    made_voices.append(clone["id"])
    api("PATCH", f"/api/characters/{cs['Hale']['id']}", json={"voice_id": aiden["id"], "gender": "male"})
    api("PATCH", f"/api/characters/{cs['Elena Marsh']['id']}", json={"voice_id": d["id"], "accent": "British", "personality": "steady, proud, grieving"})
    r = api("PATCH", f"/api/characters/{cs['Tom Briggs']['id']}", json={"voice_id": clone["id"]})
    R["after_voice_assignment"] = r.get("resegmented")
    ss = segs(book_id)
    R["segments_after_voices"] = len(ss)

    # ---- analysis job (summaries, director) on the worker
    t0 = time.time()
    aj = api("POST", f"/api/books/{book_id}/analyze", json={})
    aj = wait_job(aj["id"], 600)
    R["analysis_job_seconds"] = round(time.time() - t0, 2)
    chk("analysis job completed", aj["status"] == "completed", aj.get("error"))
    nar = api("GET", f"/api/books/{book_id}/narrative")
    dstats = nar["stages"]["director"]["stats"]
    R["director"] = {"seconds": dstats.get("seconds"), "directed": nar["directed"], "rules": dstats.get("rules"), "llm_calls": dstats.get("llm_calls"),
                     "cache_hits": dstats.get("cache_hits")}
    chk("every segment has a direction", nar["directed"] == len(ss), f"{nar['directed']}/{len(ss)}")

    # ---- real synthesis + Whisper QA
    rss: list[float] = []
    t0 = time.time()
    api("POST", "/api/queue", json={"book_id": book_id})
    wall = wait_idle(book_id, 3600, rss)
    R["tts_wall_seconds"] = round(time.time() - t0, 1)
    ss = segs(book_id)
    R["status_counts"] = {k: sum(1 for s in ss if s["status"] == k) for k in {s["status"] for s in ss}}
    details = {s["id"]: api("GET", f"/api/segments/{s['id']}") for s in ss}
    atts = [a for d_ in details.values() for a in d_["attempts"]]
    sel = [a for d_ in details.values() for a in d_["attempts"] if a["selected"]]
    audio_s = sum(a["duration_s"] or 0 for a in sel)
    synth = [a for a in atts if a["status"] == "done"]
    R["tts"] = {"audio_seconds": round(audio_s, 1), "overall_rtf": round(R["tts_wall_seconds"] / audio_s, 2) if audio_s else None,
                "attempts": len(atts), "retries": sum(1 for a in atts if a["attempt_no"] > 1), "median_attempt_rtf": round(float(np.median([a["rtf"] for a in synth if a["rtf"]])), 2),
                "peak_gpu_memory_gb": max((a.get("peak_mem_gb") or 0) for a in atts) if any("peak_mem_gb" in a for a in atts) else None,
                "worker_rss_peak_gb": round(max(rss), 1) if rss else None, "mean_wer": round(float(np.mean([a["wer"] for a in sel if a["wer"] is not None])), 4),
                "max_wer": round(max(a["wer"] for a in sel if a["wer"] is not None), 3)}
    chk("all segments rendered", all(s["status"] in ("passed", "flagged", "approved") for s in ss), R["status_counts"])
    chk("real engine produced the takes", all(a["engine"] == "mlx-qwen3-tts" for a in synth), {a["engine"] for a in synth})
    flagged = [s for s in ss if s["status"] == "flagged"]
    R["flagged_after_retries"] = [{"sid": s["sid"], "reason": s["flag_reason"]} for s in flagged]

    # ---- which instruction reached which model
    insp = {s["id"]: api("GET", f"/api/segments/{s['id']}/inspect") for s in ss}
    by_voice: dict[str, dict] = {}
    for s in ss:
        i = insp[s["id"]]
        v = i["voice"]
        e = by_voice.setdefault(v["name"], {"kind": v["kind"], "segments": 0, "with_instruction": 0, "emotions": {}})
        e["segments"] += 1
        e["with_instruction"] += 1 if i["delivery"]["instruct"] else 0
        em = (i["direction"] or {}).get("final", {}).get("emotion", "none")
        e["emotions"][em] = e["emotions"].get(em, 0) + 1
    R["delivery_by_voice"] = by_voice
    chk("Base (clone) voice received no instruction", by_voice["zz-e2e Tom clone"]["with_instruction"] == 0 and by_voice["zz-e2e Tom clone"]["segments"] > 0)
    chk("CustomVoice received delivery instructions", by_voice["Aiden"]["with_instruction"] + by_voice["Ryan"]["with_instruction"] > 0)
    spoken_ok = all(insp[s["id"]]["delivery"]["spoken_text_sent_to_tts"] == " ".join(((details[s["id"]]["segment"]["override_text"] or details[s["id"]]["segment"]["narration_text"])).split())
                    for s in ss)
    chk("TTS input is exactly the segment text for every segment", spoken_ok)
    sample = next(i for i in insp.values() if i["direction"] and i["direction"]["smoothed"] and "smoothed" in (i["direction"]["smoothing_note"] or ""))
    R["example_smoothing"] = {"sid": sample["sid"], "raw": sample["direction"]["raw"]["emotion"], "final": sample["direction"]["final"], "note": sample["direction"]["smoothing_note"]} if sample else None

    # ---- voice consistency across independently generated segments (acoustic, not speaker verification)
    items = []
    for s in ss:
        a = next((x for x in details[s["id"]]["attempts"] if x["selected"]), None)
        if not a:
            continue
        wav = c.get(f"/media/attempt/{a['id']}")
        if wav.status_code == 200:
            audio, sr = read_wav(wav.content)
            if audio.size > sr * 1.5:  # very short takes have too little voiced signal to measure pitch
                items.append((insp[s["id"]]["voice"]["name"], audio, sr))
    R["voice_consistency"] = voicecheck.consistency(items)

    # ---- targeted regeneration and the audio cache
    before = {s["sid"]: (s["status"], s["attempt_id"]) for s in ss}
    n_att_before = len(atts)
    t0 = time.time()
    r = api("PATCH", f"/api/characters/{cs['Hale']['id']}", json={"voice_id": ryan["id"]})
    reset = len(r["reset"])
    wait_idle(book_id, 1800)
    t_change = time.time() - t0
    mid = {s["sid"]: (s["status"], s["attempt_id"]) for s in segs(book_id)}
    untouched = sum(1 for sid, v in before.items() if sid in mid and mid[sid] == v)
    R["targeted_regeneration"] = {"segments_total": len(before), "reset": reset, "untouched_with_same_take": untouched, "seconds": round(t_change, 1)}
    chk("voice change regenerated only the affected segments", 0 < reset < len(before) // 2 and untouched >= len(before) - reset - 3, R["targeted_regeneration"])
    t0 = time.time()
    r = api("PATCH", f"/api/characters/{cs['Hale']['id']}", json={"voice_id": aiden["id"]})
    wait_idle(book_id, 1800)
    t_revert = time.time() - t0
    cache_reuse = sum(1 for s in segs(book_id) for h in api("GET", f"/api/segments/{s['id']}")["reviews"] if h["action"] == "cache_reuse")
    R["audio_cache"] = {"reset_on_revert": len(r["reset"]), "takes_reused_from_cache": cache_reuse, "seconds_to_restore": round(t_revert, 1), "seconds_to_change": round(t_change, 1)}
    chk("reverting the voice reused the cached takes", cache_reuse >= 1 and t_revert < t_change * 0.6, R["audio_cache"])

    # ---- Director cache: re-direct some segments, nothing should need a new analysis
    for s in ss[:6]:
        api("POST", f"/api/segments/{s['id']}/redirect")
    time.sleep(1)
    for j in [j for j in api("GET", f"/api/books/{book_id}")["utility_jobs"] if j["kind"] == "analyze" and j["status"] in ("queued", "running")]:
        wait_job(j["id"], 300)
    time.sleep(3)
    dnow = api("GET", f"/api/books/{book_id}/narrative")["stages"]["director"]["stats"]
    R["director_cache"] = {"hits_before": R["director"]["cache_hits"], "hits_after": dnow["cache_hits"], "rules_calls_total": dnow["rules"]}
    chk("re-directing re-used the Director cache", dnow["cache_hits"] > (R["director"]["cache_hits"] or 0), R["director_cache"])

    # ---- manual boundary edit: only the cut segment is re-rendered
    victim = next(s for s in segs(book_id) if s["kind"] == "body" and len(s["original_text"]) > 200 and s["status"] == "passed")
    cut = victim["src_start"] + victim["original_text"].index(" ", 100)
    before2 = {s["sid"]: s["status"] for s in segs(book_id)}
    t0 = time.time()
    out = api("POST", f"/api/segments/{victim['id']}/boundary", json={"op": "split", "offset": cut})
    api("POST", "/api/queue", json={"book_id": book_id})
    wait_idle(book_id, 900)
    after2 = {s["sid"]: s["status"] for s in segs(book_id)}
    R["boundary_edit"] = {"created": out["created"], "retired": out["retired"], "other_segments_unchanged": all(after2.get(k) == v for k, v in before2.items() if k != victim["sid"]),
                          "seconds": round(time.time() - t0, 1)}
    chk("boundary edit touched only the split segment", out["created"] == 2 and out["retired"] == 1 and R["boundary_edit"]["other_segments_unchanged"], R["boundary_edit"])
    chk("book still fully rendered after edits", all(s["status"] in ("passed", "approved", "flagged") for s in segs(book_id)))
finally:
    # ------------------------------------------------------------------------------------ cleanup
    def step(label, fn):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - every cleanup step must run even if another fails
            print("CLEANUP PROBLEM", label, exc)

    if book_id:
        bk = api("GET", f"/api/books/{book_id}")
        dirs = list((bk.get("storage") or {}).values())
        step("wait for jobs", lambda: [wait_job(j["id"], 300) for j in api("GET", f"/api/books/{book_id}")["utility_jobs"] if j["status"] in ("queued", "running")])
        step("archive book", lambda: api("POST", f"/api/books/{book_id}/archive", json={"archived": True}))
        for d_ in dirs:
            if bk["uid"] in d_ and "/audiobook-studio-claude/" in d_:
                step("remove " + d_, lambda d_=d_: ssh(f"rm -rf '{d_}'"))
    for vid in made_voices:
        step(f"archive voice {vid}", lambda vid=vid: api("POST", f"/api/voices/{vid}/archive"))
    step("restore narrative settings", lambda: api("PUT", "/api/settings", json={"narrative": original_narrative}))

R["checks"] = [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]
R["passed"], R["total"] = sum(1 for _n, ok, _d in checks if ok), len(checks)
(OUT / "narrative_real.json").write_text(json.dumps(R, indent=2, default=str), encoding="utf-8")
print(f"\n{R['passed']}/{R['total']} checks passed; report: {OUT / 'narrative_real.json'}")
print(json.dumps({k: R.get(k) for k in ("import_request_seconds", "structure_seconds_server", "analysis_job_seconds", "director", "tts", "voice_consistency",
                                      "targeted_regeneration", "audio_cache", "director_cache", "boundary_edit", "flagged_after_retries")}, indent=1, default=str))
sys.exit(0 if R["passed"] == R["total"] else 1)
