#!/usr/bin/env python3
"""Can a cloned narrator take text instructions? A measured experiment for the Mac (real Qwen3-TTS through mlx-audio).

Qwen3-TTS Base (cloning) has no instruction input; CustomVoice follows instructions but only for its fixed speakers, whose identity
is one row of the talker's codec embedding table (``talker_config.spk_id``). The Base model's speaker encoder produces a vector of
the same size that the Base model inserts in the same prompt position, and Qwen's own fine-tuning recipe writes such a vector into
a CustomVoice-format speaker row. This experiment writes the clone's speaker-encoder vector into a spare CustomVoice speaker row (in
memory only) and asks that "transplanted" speaker for emotions, next to the Base clone itself.

    python tools/e2e/clone_instruct_experiment.py --ref REF.wav --ref-text "exact transcript" --out DIR [--studio-src SRC]

``--studio-src`` is a directory that contains the ``studio`` package (the installed release); it provides the Whisper check and the
pitch measurements the studio already uses. Writes DIR/report.json and one WAV per take. Nothing in the studio's database or settings
is touched. Variants:

  base_icl          Base model, reference clip + transcript (how clones narrate today); the same neutral text for every emotion
  base_icl_script   Base model with an emotionally written script (text-driven delivery: the "lever 1" candidates)
  base_xvec         Base model, speaker vector only (no reference prosody to copy)
  cv_transplant     CustomVoice with the clone's speaker vector in a spare speaker row, plus an instruction per emotion
  cv_transplant_script  the same with the emotional script

Measured per take: speaker similarity to the reference (cosine of Base speaker-encoder vectors), Whisper WER, median pitch, pitch
spread, loudness and speaking rate. Calibration: similarity of the narrator's own Base takes (same person) and of CustomVoice preset
speakers (different people) to the reference, and how close each preset's speaker row is to the encoder vector of its own speech
(is the speaker table in the encoder's space at all?)."""
from __future__ import annotations

import argparse
import json
import sys
import time
import wave
from pathlib import Path

import numpy as np

NEUTRAL_TEXT = ("The lighthouse at Gull Point had stood empty for eleven years before Margaret climbed its stairs. "
                "Nothing answered her but the wind, patient and cold, off the grey water.")
EMOTIONS = {
    # emotion: (instruction in the studio's own wording, an emotionally written script)
    "neutral": (None, NEUTRAL_TEXT),
    "anger": ("Speak strongly angry, with edge.",
              "How dare you! I trusted you with everything, and you threw it away. Get out. Get out of my house, and don't you ever come back!"),
    "fear": ("Speak strongly fearful, breath tight.",
             "Don't move. Please, please don't move. Something is in the room with us. I can hear it breathing. Oh God, it's getting closer."),
    "sadness": ("Speak strongly sad, voice heavy.",
                "She's gone. I keep setting two cups out in the morning, and then I remember. I just wanted one more day. Just one."),
    "joy": ("Speak strongly joyful, warm and bright.",
            "We did it! We actually did it! After all those years, all that rain, all those terrible Saturdays, we won! I can't stop smiling!"),
    "tenderness": ("Speak strongly tender, soft and gentle.",
                   "Hey. Hey, it's alright. Come here. You don't have to be brave tonight. I'm right here, and I'm not going anywhere."),
}
PRESETS = ("ryan", "aiden", "vivian", "serena")
SPARE_ROW = 3000
SR = 24000


def write_wav(path: Path, audio: np.ndarray, sr: int) -> None:
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def collect(gen) -> np.ndarray:
    import mlx.core as mx

    chunks = []
    for res in gen:
        chunks.append(np.array(res.audio.astype(mx.float32)).reshape(-1))
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)


def cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", required=True)
    ap.add_argument("--ref-text", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--studio-src")
    ap.add_argument("--base-model", default="mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16")
    ap.add_argument("--cv-model", default="mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16")
    ap.add_argument("--whisper", default="mlx-community/whisper-large-v3-turbo")
    ap.add_argument("--seeds", type=int, default=2, help="takes per variant and emotion")
    a = ap.parse_args()
    if a.studio_src:
        sys.path.insert(0, a.studio_src)
    import mlx.core as mx
    from mlx_audio.tts.utils import load_model
    from mlx_audio.utils import load_audio

    try:
        from studio.align import evaluate
        from studio.narrative import voicecheck as VC
    except Exception as exc:  # the measurements degrade, the experiment still runs
        print(f"studio package not importable ({exc}); WER and pitch are skipped", file=sys.stderr)
        evaluate = VC = None
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    base = load_model(a.base_model)
    cv = load_model(a.cv_model)

    def embed(audio: np.ndarray) -> np.ndarray:
        e = base.extract_speaker_embedding(mx.array(audio.astype(np.float32)), sr=SR)
        return np.array(e.astype(mx.float32)).reshape(-1)

    ref_audio = np.array(load_audio(a.ref, sample_rate=SR)).reshape(-1)
    ref_vec = embed(ref_audio)

    # --- the transplant: the clone's vector in a spare CustomVoice speaker row (in memory only)
    tc = cv.config.talker_config
    used = set((tc.spk_id or {}).values()) | {getattr(tc, k) for k in dir(tc) if k.endswith("_id") and isinstance(getattr(tc, k), int)}
    if SPARE_ROW in used:
        raise SystemExit(f"row {SPARE_ROW} is in use in this CustomVoice model")
    emb = cv.talker.get_input_embeddings()
    rows_before = np.array(emb.weight.astype(mx.float32))
    w = emb.weight
    w[SPARE_ROW] = mx.array(ref_vec).astype(w.dtype)
    emb.weight = w
    tc.spk_id["narrator_x"] = SPARE_ROW
    cv.supported_speakers.append("narrator_x")

    asr = None
    try:
        import mlx_whisper

        def asr(path: Path) -> str:
            r = mlx_whisper.transcribe(str(path), path_or_hf_repo=a.whisper, language="en", condition_on_previous_text=False, temperature=0.0, verbose=None)
            return (r.get("text") or "").strip()
    except Exception as exc:
        print(f"mlx-whisper unavailable ({exc}); WER skipped", file=sys.stderr)

    takes = []

    def measure(variant: str, emotion: str, text: str, instruct: str | None, audio: np.ndarray, seed: int) -> dict:
        path = out / f"{variant}-{emotion}-s{seed}.wav"
        write_wav(path, audio, SR)
        dur = audio.size / SR
        rec = {"file": path.name, "variant": variant, "emotion": emotion, "seed": seed, "instruct": instruct, "text": text,
               "duration_s": round(dur, 2), "similarity": round(cos(embed(audio), ref_vec), 4) if audio.size > SR // 2 else None,
               "rms_db": round(float(20 * np.log10(np.sqrt(np.mean(audio ** 2)) + 1e-9)), 2), "words_per_s": round(len(text.split()) / max(dur, 0.1), 2)}
        if VC is not None and audio.size:
            f0 = VC.f0_median(audio, SR)
            rec["f0_hz"] = round(f0, 1) if f0 else None
            # pitch spread: median f0 of 0.5 s windows, interquartile range in semitones (expressiveness proxy)
            win = [VC.f0_median(audio[i:i + SR // 2], SR) for i in range(0, max(audio.size - SR // 2, 1), SR // 4)]
            win = np.array([x for x in win if x])
            rec["f0_iqr_st"] = round(float(12 * np.log2(np.percentile(win, 75) / np.percentile(win, 25))), 2) if win.size > 4 else None
        if asr is not None:
            heard = asr(path)
            rec["asr_text"] = heard
            if evaluate is not None:
                rec["wer"] = round(evaluate(text, heard, None, dur)["wer"], 3)
        takes.append(rec)
        print(f"{variant:22s} {emotion:10s} s{seed} sim={rec['similarity']} wer={rec.get('wer')} f0={rec.get('f0_hz')} spread={rec.get('f0_iqr_st')}", flush=True)
        return rec

    gen_opts = {"verbose": False}
    for seed in range(1, a.seeds + 1):
        for emotion, (instruct, script) in EMOTIONS.items():
            mx.random.seed(1000 * seed + 7)
            measure("base_icl", emotion, NEUTRAL_TEXT, None,
                    collect(base.generate(text=NEUTRAL_TEXT, ref_audio=a.ref, ref_text=a.ref_text, lang_code="english", **gen_opts)), seed)
            if emotion != "neutral":
                mx.random.seed(1000 * seed + 11)
                measure("base_icl_script", emotion, script, None,
                        collect(base.generate(text=script, ref_audio=a.ref, ref_text=a.ref_text, lang_code="english", **gen_opts)), seed)
            mx.random.seed(1000 * seed + 13)
            measure("base_xvec", emotion, NEUTRAL_TEXT, None,
                    collect(base.generate(text=NEUTRAL_TEXT, ref_audio=a.ref, lang_code="english", **gen_opts)), seed)
            mx.random.seed(1000 * seed + 17)
            measure("cv_transplant", emotion, NEUTRAL_TEXT, instruct,
                    collect(cv.generate_custom_voice(text=NEUTRAL_TEXT, speaker="narrator_x", language="english", instruct=instruct, **gen_opts)), seed)
            if emotion != "neutral":
                mx.random.seed(1000 * seed + 19)
                measure("cv_transplant_script", emotion, script, instruct,
                        collect(cv.generate_custom_voice(text=script, speaker="narrator_x", language="english", instruct=instruct, **gen_opts)), seed)

    # --- calibration: different people, and is the preset speaker table in the encoder's space?
    calib = []
    for spk in PRESETS:
        if spk not in (tc.spk_id or {}):
            continue
        mx.random.seed(99)
        audio = collect(cv.generate_custom_voice(text=NEUTRAL_TEXT, speaker=spk, language="english", **gen_opts))
        write_wav(out / f"preset-{spk}.wav", audio, SR)
        v = embed(audio)
        row = rows_before[tc.spk_id[spk]]
        calib.append({"speaker": spk, "similarity_to_reference": round(cos(v, ref_vec), 4),
                      "row_vs_own_speech": round(cos(row, v), 4),
                      "row_vs_other_presets_speech": None})
    for c in calib:  # a row should sit closer to its own speaker's speech than to the other presets' speech
        others = [cos(rows_before[tc.spk_id[c["speaker"]]], embed(np.array(load_audio(str(out / f"preset-{o['speaker']}.wav"), sample_rate=SR)).reshape(-1)))
                  for o in calib if o["speaker"] != c["speaker"]]
        c["row_vs_other_presets_speech"] = round(float(np.mean(others)), 4) if others else None
        print(f"preset {c['speaker']:8s} sim_to_ref={c['similarity_to_reference']} row~own={c['row_vs_own_speech']} row~others={c['row_vs_other_presets_speech']}", flush=True)

    def summary(variant: str) -> dict:
        t = [x for x in takes if x["variant"] == variant]
        sims = [x["similarity"] for x in t if x["similarity"] is not None]
        wers = [x["wer"] for x in t if x.get("wer") is not None]
        by = {}
        for e in EMOTIONS:
            te = [x for x in t if x["emotion"] == e]
            if te:
                by[e] = {k: (round(float(np.mean([x[k] for x in te if x.get(k) is not None])), 3) if any(x.get(k) is not None for x in te) else None)
                         for k in ("similarity", "wer", "f0_hz", "f0_iqr_st", "rms_db", "words_per_s")}
        return {"takes": len(t), "similarity_mean": round(float(np.mean(sims)), 4) if sims else None,
                "similarity_min": round(float(np.min(sims)), 4) if sims else None, "wer_mean": round(float(np.mean(wers)), 3) if wers else None, "by_emotion": by}

    report = {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": round(time.time() - t0, 1), "reference": a.ref,
              "models": {"base": a.base_model, "custom_voice": a.cv_model, "whisper": a.whisper}, "spare_row": SPARE_ROW,
              "variants": {v: summary(v) for v in ("base_icl", "base_icl_script", "base_xvec", "cv_transplant", "cv_transplant_script")},
              "calibration_presets": calib, "takes": takes}
    (out / "report.json").write_text(json.dumps(report, indent=2), "utf-8")
    print(json.dumps({v: {k: s[k] for k in ("similarity_mean", "similarity_min", "wer_mean")} for v, s in report["variants"].items()}, indent=2))
    print(f"wrote {out / 'report.json'} in {report['seconds']} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
