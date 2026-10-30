#!/usr/bin/env python3
"""Real-engine check of v0.9.0 on the Mac: the narrator sample, and emotion takes for a cloned narrator from each source, through the running
studio's API (real Qwen3-TTS and Whisper). Read-mostly: takes are discarded at the end; nothing is adopted; the samples it generates are kept
(they are what the tile play button would make anyway).

    STUDIO_USER=... STUDIO_PASSWORD=... python tools/e2e/emotion_takes_real.py "Narrator name" [BASE_URL]

Run it on the Mac with the installed runtime (httpx is part of it) and a temporary admin account; remove the account afterwards."""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

NAME = sys.argv[1]
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:7861"
c = httpx.Client(base_url=BASE, headers={"X-Studio-Request": "1"}, timeout=60)
r = c.post("/api/auth/login", json={"username": os.environ["STUDIO_USER"], "password": os.environ["STUDIO_PASSWORD"]})
assert r.status_code == 200, r.text
out: dict = {"base": BASE, "narrator": NAME}


def wait_job(jid: int, limit: float = 900) -> dict:
    end = time.time() + limit
    while time.time() < end:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("completed", "failed", "cancelled"):
            return j
        time.sleep(3)
    raise TimeoutError(f"job {jid}")


voices = {v["name"]: v for v in c.get("/api/voices").json()["voices"]}
v = voices[NAME]
vid = v["id"]

# ---- the tile sample
t0 = time.time()
s = c.post(f"/api/voices/{vid}/sample").json()
while s["status"] in ("queued", "running"):
    time.sleep(3)
    s = c.get(f"/api/voices/{vid}/sample").json()
head = c.head(s["url"]) if s.get("url") else None
again = c.post(f"/api/voices/{vid}/sample").json()
out["sample"] = {"status": s["status"], "seconds": round(time.time() - t0, 1), "media_status": head.status_code if head else None,
                 "content_type": head.headers.get("content-type") if head else None, "second_press": again["status"]}
print("sample", out["sample"], flush=True)

# ---- one take per source
refs = c.get(f"/api/voices/{vid}/refs").json()
out["sources"] = refs["sources"]
plan = [("anger", "clone"), ("sadness", "directable"), ("joy", "clone"), ("fear", "directable")]
if "design" in refs["sources"]:
    plan.append(("tenderness", "design"))
out["takes"] = []
for bucket, source in plan:
    r = c.post(f"/api/voices/{vid}/refs/candidates", json={"bucket": bucket, "source": source})
    if r.status_code != 200:
        out["takes"].append({"bucket": bucket, "source": source, "error": r.text[:300]})
        continue
    busy = c.post(f"/api/voices/{vid}/refs/candidates", json={"bucket": bucket, "source": source}).status_code
    t0 = time.time()
    j = wait_job(r.json()["job"]["id"])
    res = j.get("result") or {}
    take = {"bucket": bucket, "source": source, "status": j["status"], "error": j.get("error"), "seconds": round(time.time() - t0, 1),
            "second_request_status": busy, "engine": res.get("engine"), "model_id": res.get("model_id"), "duration_s": res.get("duration_s"),
            "similarity": res.get("similarity"), "wer": (res.get("asr_check") or {}).get("wer"), "matches": (res.get("asr_check") or {}).get("matches"),
            "f0_delta_hz": (res.get("drift") or {}).get("f0_delta_hz"), "recording_id": res.get("recording_id")}
    out["takes"].append(take)
    print("take", take, flush=True)

# ---- listed, then discarded (nothing is adopted into the owner's narrator)
listed = {b["bucket"]: len(b["candidates"]) for b in c.get(f"/api/voices/{vid}/refs").json()["buckets"]}
out["listed_before_discard"] = {k: n for k, n in listed.items() if n}
for bucket, _ in plan:
    d = c.request("DELETE", f"/api/voices/{vid}/refs/candidates", params={"bucket": bucket})
    out.setdefault("discarded", {})[bucket] = d.json().get("discarded") if d.status_code == 200 else d.text[:200]
out["left_after_discard"] = sum(len(b["candidates"]) for b in c.get(f"/api/voices/{vid}/refs").json()["buckets"])
print(json.dumps(out, indent=2))
