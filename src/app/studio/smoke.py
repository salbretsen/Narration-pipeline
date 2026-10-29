"""Real-hardware smoke test for Apple Silicon (MLX). Run with ./smoke-test on the target Mac.

It exercises real Qwen3-TTS inference (CustomVoice, Base cloning, VoiceDesign), real
mlx-whisper verification, the queue with two manuscripts and a chapter M4B export, and writes
a report with measured real-time factor, memory, model download size and any defects.
This test is never run with the mock engine.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
import traceback
from pathlib import Path

SMOKE_TEXT = """Chapter One: The Lighthouse Keeper

The lighthouse at Gull Point had stood empty for eleven years before Margaret Hale climbed its spiral stairs with a lantern in one hand and the deed in the other. The wind came off the sea in long, patient breaths, and the glass of the lamp room was crusted white with salt. She wiped a small circle clear with her sleeve and looked out across the water, where the evening light was turning the waves the colour of old pewter.

Her uncle had kept the light for thirty-two years. He had written everything down: the weather at dawn and dusk, the ships that passed, the birds that struck the glass on foggy nights. His journals filled two shelves in the keeper's cottage, bound in cracked green leather, and Margaret had spent her first week reading them by the stove while the rain drummed on the roof.

On the ninth of March, 1987, he had written only one line: "A light on the water where no light should be."

Chapter Two: The Second Light

Margaret did not believe in ghosts, but she believed in records. She counted the entries that mentioned the strange light and found forty-one of them, scattered over twenty years, always in the weeks before a storm. Her uncle had measured its bearing with a brass compass and drawn each sighting on a chart that was now soft as cloth.

The bearings crossed at a single point, a shoal two miles north of the point, marked on the old charts as Widow's Ledge. No ship would anchor there. The water was shallow and the currents were cruel.

That night the barometer began to fall. Margaret lit the great lamp for the first time, listened to the mechanism turn, and sat at the window with her uncle's compass in her lap. At a quarter past eleven, low on the black water to the north, a small yellow light appeared, steady and unblinking, exactly where the chart said it would be. She wrote the time in a new journal, and then, after a moment, she wrote the bearing too.
"""

PREVIEW_TEXT = "The lighthouse at Gull Point had stood empty for eleven years, and the wind came off the sea in long, patient breaths."


def _du(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _sysctl(name: str) -> str:
    try:
        return subprocess.run(["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""


def _rss_gb() -> float | None:
    try:
        import resource

        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(r / 1e9 if sys.platform == "darwin" else r / 1e6, 2)
    except Exception:
        return None


def run_smoke(args) -> int:
    from . import services as S
    from .align import evaluate
    from .assembly import export_book
    from .audio import probe_summary, write_wav
    from .context import Context, setup_logging
    from .db import row, rows
    from .engines import MlxQwenEngine, MlxWhisperASR, SynthParams, VoiceSpec, max_tokens_for
    from .worker import Worker

    ctx = Context()
    setup_logging(ctx, "smoke")
    if ctx.engine_mode != "mlx":
        print("The smoke test needs the real MLX engine (Apple Silicon). Current engine mode is 'mock'. Nothing was run.")
        return 2
    s = ctx.settings()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = ctx.logs_dir / f"smoke-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    hf_home = Path(os.environ.get("HF_HOME") or (ctx.cache_dir / "huggingface"))
    cache_before = _du(hf_home)
    rep = {"started": stamp, "platform": {"machine": platform.machine(), "macos": platform.mac_ver()[0], "chip": _sysctl("machdep.cpu.brand_string"),
                                          "memory_gb": round(int(_sysctl("hw.memsize") or 0) / 1024**3, 1), "python": platform.python_version()},
           "steps": [], "defects": []}
    tts = MlxQwenEngine(max_warm=s.models.max_warm_models, allow_downloads=s.models.allow_downloads)
    asr = MlxWhisperASR(s.models.asr_model, allow_downloads=s.models.allow_downloads)
    rep["versions"] = tts.versions()

    def step(name, fn):
        t0 = time.time()
        print(f"\n== {name}", flush=True)
        entry = {"name": name, "ok": False}
        try:
            entry["details"] = fn() or {}
            entry["ok"] = True
            print("   OK", json.dumps(entry["details"], default=str)[:600], flush=True)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["traceback"] = traceback.format_exc()[-3000:]
            rep["defects"].append(f"{name}: {entry['error']}")
            print("   FAILED", entry["error"], flush=True)
        entry["seconds"] = round(time.time() - t0, 2)
        rep["steps"].append(entry)
        return entry

    def synth_and_verify(label: str, spec: VoiceSpec, text: str) -> dict:
        t0 = time.time()
        res = tts.synthesize(spec, text, SynthParams(seed=42, max_tokens=max_tokens_for(text)))
        wav = out_dir / f"{label}.wav"
        write_wav(wav, res.audio, res.sample_rate)
        dur = res.audio.size / res.sample_rate
        tr = asr.transcribe(wav)
        ev = evaluate(text, tr["text"], tr.get("words"), dur)
        if ev["wer"] > s.qa.wer_threshold:
            rep["defects"].append(f"{label}: WER {ev['wer']:.3f} above threshold ({tr['text'][:120]!r})")
        return {"wav": str(wav), "sample_rate": res.sample_rate, "audio_s": round(dur, 2), "synth_s": round(res.seconds, 2),
                "rtf": round(res.seconds / dur, 3) if dur else None, "peak_mem_gb": res.peak_mem_gb, "asr_s": round(tr["seconds"], 2),
                "wer": ev["wer"], "asr_text": tr["text"], "wall_s": round(time.time() - t0, 2), "model": spec.model_id}

    preset = VoiceSpec(kind="preset", model_id=s.models.custom_voice_model, speaker="Ryan", language=s.models.language,
                       instruct="Calm, clear audiobook narration.")
    r1 = step("CustomVoice preview (Ryan) + Whisper verification", lambda: synth_and_verify("ryan", preset, PREVIEW_TEXT))

    def clone_step():
        if args.ref_audio:
            if not args.ref_text:
                raise ValueError("--ref-text is required with --ref-audio")
            ref_path, ref_text = Path(args.ref_audio), args.ref_text
            consent = {"owner_attest": True, "rights_holder": "smoke-test operator (attested via --ref-audio)", "statement": "smoke test"}
        else:
            if not r1["ok"]:
                raise RuntimeError("no reference available (CustomVoice step failed and no --ref-audio given)")
            ref_path, ref_text = Path(r1["details"]["wav"]), PREVIEW_TEXT
            consent = {"owner_attest": True, "rights_holder": "synthetic Ryan preset (smoke test)", "statement": "synthetic reference generated locally"}
        v = S.create_clone_voice(ctx, f"Smoke clone {stamp}", ref_path.name, ref_path.read_bytes(), ref_text, consent)
        with ctx.db.session() as conn:
            vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (v["version"]["id"],))
        spec = VoiceSpec(kind="clone", model_id=vv["model_id"], language=vv["language"], ref_audio_path=vv["ref_audio_path"], ref_text=vv["ref_text"])
        d = synth_and_verify("clone", spec, "Margaret did not believe in ghosts, but she believed in records.")
        d["voice_id"] = v["id"]
        S.archive_voice(ctx, v["id"])
        return d

    step("Base model voice clone (ref_audio + ref_text) + Whisper", clone_step)
    if not args.skip_design:
        design = VoiceSpec(kind="design", model_id=s.models.voice_design_model, language=s.models.language,
                           instruct="A mature, warm female narrator with a low, steady voice and a slight British accent, unhurried pacing.")
        step("VoiceDesign + Whisper", lambda: synth_and_verify("design", design, "The bearings crossed at a single point, two miles north of the point."))
    tts.unload_all()

    books: list[int] = []

    def queue_step():
        S.ensure_builtin_voices(ctx)
        ryan = next(v for v in S.list_voices(ctx) if v["name"] == "Ryan")
        for title in ("Smoke Test A", "Smoke Test B"):
            prev = S.create_import(ctx, f"smoke-{title[-1].lower()}.txt", SMOKE_TEXT.encode())
            b = S.confirm_import(ctx, prev["token"], {"title": f"{title} {stamp}", "author": "Smoke Test", "voice_id": ryan["id"], "queue": True})
            books.append(b["id"])
        t0 = time.time()
        w = Worker(ctx, tts=MlxQwenEngine(max_warm=s.models.max_warm_models), asr=asr)
        if w.acquire_lease():
            try:
                w.recover()
                w.run_until_idle()
            finally:
                w.release_lease()
            mode = "in-process worker"
        else:
            mode = "studio worker (web server running)"
            while time.time() - t0 < 3600:
                with ctx.db.session() as conn:
                    left = row(conn, f"SELECT COUNT(*) n FROM jobs WHERE book_id IN ({','.join('?' * len(books))}) AND status IN ('queued','running')", books)["n"]
                if not left:
                    break
                time.sleep(5)
        with ctx.db.session() as conn:
            st = rows(conn, f"SELECT status, COUNT(*) n FROM segments WHERE book_id IN ({','.join('?' * len(books))}) GROUP BY status", books)
            perf = row(conn, f"""SELECT AVG(a.rtf) rtf, MAX(a.peak_mem_gb) mem, COUNT(*) n, SUM(a.duration_s) audio FROM attempts a JOIN segments s ON s.id=a.segment_id
                                 WHERE s.book_id IN ({','.join('?' * len(books))}) AND a.status='done'""", books)
        counts = {r["status"]: r["n"] for r in st}
        if counts.get("pending") or counts.get("synthesizing"):
            raise RuntimeError(f"queue did not finish: {counts}")
        return {"mode": mode, "segment_status": counts, "attempts": perf["n"], "mean_rtf": round(perf["rtf"] or 0, 3),
                "peak_mem_gb": perf["mem"], "audio_s": round(perf["audio"] or 0, 1), "wall_s": round(time.time() - t0, 1)}

    step("Queue two manuscripts and process them (real TTS + Whisper QA)", queue_step)

    def export_step():
        if not books:
            raise RuntimeError("no books")
        with ctx.db.session() as conn:
            ch = row(conn, "SELECT id FROM chapters WHERE book_id=? ORDER BY idx LIMIT 1", (books[0],))
            flagged = rows(conn, "SELECT id FROM segments WHERE chapter_id=? AND status='flagged'", (ch["id"],))
        r = export_book(ctx, books[0], "chapter_m4b", ch["id"])
        info = probe_summary(r["path"])
        return {"path": r["path"], "draft": r["draft"], "duration_s": info["duration_s"], "codec": info["codec"], "chapters": len(info["chapters"]),
                "sample_rate": info["sample_rate"], "flagged_in_chapter": len(flagged),
                "note": "draft export; flagged segments still need human review"}

    step("Export chapter M4B (ffmpeg) and validate with ffprobe", export_step)
    if not args.keep:
        for b in books:
            try:
                S.archive_book(ctx, b, True)
            except Exception:
                pass
    rep["model_download_bytes"] = max(0, _du(hf_home) - cache_before)
    rep["hf_cache_bytes"] = _du(hf_home)
    rep["max_rss_gb"] = _rss_gb()
    rep["finished"] = time.strftime("%Y%m%d-%H%M%S")
    ok = all(st["ok"] for st in rep["steps"])
    rep["result"] = "PASS" if ok and not rep["defects"] else ("PASS WITH DEFECTS" if ok else "FAIL")
    (out_dir / "smoke_report.json").write_text(json.dumps(rep, indent=2, default=str), "utf-8")
    md = [f"# Smoke test {stamp}: {rep['result']}", "", f"Platform: {rep['platform']}", f"Versions: {rep['versions']}",
          f"Model download during test: {rep['model_download_bytes'] / 1e9:.2f} GB (HF cache now {rep['hf_cache_bytes'] / 1e9:.2f} GB)",
          f"Max RSS: {rep['max_rss_gb']} GB", "", "| Step | OK | Seconds | Key results |", "|---|---|---|---|"]
    for st in rep["steps"]:
        d = st.get("details") or {}
        key = ", ".join(f"{k}={d[k]}" for k in ("rtf", "mean_rtf", "wer", "peak_mem_gb", "audio_s", "segment_status", "chapters", "codec") if k in d)
        md.append(f"| {st['name']} | {'yes' if st['ok'] else 'NO'} | {st['seconds']} | {key or st.get('error', '')} |")
    md += ["", "## Defects", *(f"- {d}" for d in rep["defects"] or ["none"]), "",
           "## Still manual", "- Open the studio URL (see ./studio-service status), sign in, listen to an output segment in Review, approve it, and record release signoff.",
           "- Listen to the exported chapter M4B in Apple Books / QuickTime and check chapter markers."]
    (out_dir / "smoke_report.md").write_text("\n".join(md) + "\n", "utf-8")
    print(f"\nSmoke test {rep['result']}. Report: {out_dir / 'smoke_report.md'}")
    return 0 if ok else 1
