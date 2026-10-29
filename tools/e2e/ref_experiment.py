"""Does a clone inherit the delivery (emotion) of its reference? Real Qwen3-TTS Base model on the M3 Max.
References are synthetic (CustomVoice Ryan with different style instructions), so this measures the cloning
mechanism, not human emotional speech."""
import io, json, sys, time, wave
import httpx, numpy as np

# usage: python ref_experiment.py USER PASSWORD OUT.json   (env: STUDIO_BASE) - needs an ADMIN account (creates and you must archive temp narrators)
import os
BASE, USER, PW = os.environ.get("STUDIO_BASE", "https://<mac-host>:8443"), sys.argv[1], sys.argv[2]
c = httpx.Client(base_url=BASE, headers={"X-Studio-Request": "1"}, timeout=120, verify=False)
assert c.post("/api/auth/login", json={"username": USER, "password": PW}).status_code == 200

REF_TEXT = ("The morning fog lay thick over the harbour, and the fishing boats rocked quietly at their moorings. "
            "Thomas walked along the quay, counting the masts as he had done every day since he was a boy. Seventeen, he thought. One fewer than last spring.")
TEST_TEXT = ("The lighthouse at Gull Point had stood empty for eleven years before Margaret Hale climbed its stairs. "
             "She set down her bag, opened the shutters, and looked out across the grey water.")
STYLES = {
    "neutral": "Calm, clear, even audiobook narration at a steady moderate pace.",
    "joyful": "Very happy, excited and energetic, bright and fast, with big pitch variation.",
    "tense": "Tense and urgent, hushed and hurried, as if afraid of being overheard.",
    "sad": "Sad and sorrowful, very slow, low and quiet, with heavy pauses.",
}


def job_wait(jid, t=300):
    t0 = time.time()
    while time.time() - t0 < t:
        j = c.get(f"/api/jobs/{jid}").json()
        if j["status"] in ("completed", "failed"):
            return j
        time.sleep(1.5)
    raise TimeoutError(jid)


def wav_of(jid):
    r = c.get(f"/media/preview/{jid}"); assert r.status_code == 200, r.status_code
    return r.content


def read(b):
    with wave.open(io.BytesIO(b)) as w:
        sr = w.getframerate(); x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    return x, sr


def metrics(b, words):
    x, sr = read(b)
    hop, win = int(.01 * sr), int(.04 * sr)
    lv = np.array([20 * np.log10(max(np.sqrt(np.mean(x[i:i + win] ** 2)), 1e-9)) for i in range(0, len(x) - win, hop)])
    voiced = lv > lv.max() - 35
    idx = np.where(voiced)[0]
    span = (idx[-1] - idx[0]) * .01
    f0 = []
    lmin, lmax = int(sr / 320), int(sr / 70)
    for i in np.where(lv > lv.max() - 25)[0]:
        seg = x[i * hop:i * hop + win]
        if len(seg) < win: continue
        seg = seg - seg.mean(); ac = np.correlate(seg, seg, "full")[win - 1:]
        if ac[0] <= 0: continue
        ac = ac / ac[0]; l = int(np.argmax(ac[lmin:lmax])) + lmin
        if ac[l] > 0.5: f0.append(sr / l)
    f0 = np.array(f0)
    st = 12 * np.log2(f0 / np.median(f0)) if len(f0) > 5 else np.array([0.0])
    pauses = float(np.mean(~voiced[idx[0]:idx[-1]])) if len(idx) > 1 else 0
    return {"seconds": round(len(x) / sr, 1), "words_per_sec": round(words / span, 2), "level_dbfs": round(float(lv[voiced].mean()), 1),
            "f0_median_hz": int(np.median(f0)) if len(f0) else 0, "pitch_var_semitones": round(float(st.std()), 2), "pause_fraction": round(pauses, 2)}


# 1) find/create one temporary preset narrator per style (archived afterwards by the caller)
vs = {v["name"]: v for v in c.get("/api/voices").json()["voices"]}
pre = {}
for k, ins in STYLES.items():
    name = f"zz-exp-{k}"
    v = vs.get(name) or c.post("/api/voices/preset", json={"name": name, "speaker": "Ryan", "instruct": ins}).json()
    pre[k] = v["id"]
refs = {}
for k, vid in pre.items():
    j = job_wait(c.post(f"/api/voices/{vid}/preview", json={"text": REF_TEXT, "seed": 11}).json()["id"])
    assert j["status"] == "completed", j.get("error")
    refs[k] = wav_of(j["id"])
    print(f"reference {k}: generated", flush=True)
# mixed reference: a SHORT neutral clip + a SHORT joyful clip back to back (one clip, both transcripts)
SHORT = "The morning fog lay thick over the harbour, and the fishing boats rocked quietly at their moorings."
sh = {}
for k in ("neutral", "joyful"):
    j = job_wait(c.post(f"/api/voices/{pre[k]}/preview", json={"text": SHORT, "seed": 11}).json()["id"]); sh[k] = wav_of(j["id"])
a, sr = read(sh["neutral"]); b, _ = read(sh["joyful"])
mix = np.concatenate([a, np.zeros(int(sr * .4), np.float32), b])
buf = io.BytesIO()
with wave.open(buf, "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes((np.clip(mix, -1, 1) * 32767).astype("<i2").tobytes())
refs["mixed(neutral+joyful)"] = buf.getvalue()

nwords_ref = len(REF_TEXT.split()); nwords_test = len(TEST_TEXT.split())
rows = []
rec_ids = []
for k, data in refs.items():
    txt = REF_TEXT if "mixed" not in k else SHORT + " " + SHORT
    r = c.post("/api/recordings", data={"script_id": "", "ref_text": txt}, files={"file": ("r.wav", data, "audio/wav")})
    assert r.status_code == 200, (k, r.text[:200])
    rid = r.json()["id"]; rec_ids.append(rid)
    j = job_wait(c.post(f"/api/recordings/{rid}/job", json={"mode": "audition", "text": TEST_TEXT}).json()["id"])
    assert j["status"] == "completed", j.get("error")
    out = wav_of(j["id"])
    rows.append((k, metrics(data, nwords_ref if "mixed" not in k else 2 * len(SHORT.split())), metrics(out, nwords_test), j["result"].get("wer")))
    print(f"clone from {k}: done", flush=True)
for rid in rec_ids:
    c.delete(f"/api/recordings/{rid}")
print("\nREFERENCE (what you recorded)  ->  CLONE reading the SAME neutral test text")
print(f"{'reference':24s} {'ref wps':>8s} {'ref pitchVar':>12s} {'ref dB':>7s} | {'clone wps':>9s} {'clone pitchVar':>14s} {'clone dB':>8s} {'clone F0':>8s} {'pauses':>7s} {'WER':>5s}")
for k, r, o, w in rows:
    print(f"{k:24s} {r['words_per_sec']:8.2f} {r['pitch_var_semitones']:12.2f} {r['level_dbfs']:7.1f} | {o['words_per_sec']:9.2f} {o['pitch_var_semitones']:14.2f} {o['level_dbfs']:8.1f} {o['f0_median_hz']:8d} {o['pause_fraction']:7.2f} {w if w is None else round(w, 2)!s:>5s}")
json.dump([(k, r, o, w) for k, r, o, w in rows], open(sys.argv[3], "w"), indent=1)
print("preset ids to archive:", list(pre.values()))
