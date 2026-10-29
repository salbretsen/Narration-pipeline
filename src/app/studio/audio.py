"""Audio I/O, acoustic QA and FFmpeg/ffprobe helpers.

All intermediate takes are 16-bit PCM mono WAV written atomically. Reading uses the stdlib
``wave`` module plus NumPy so no native audio library beyond FFmpeg is required.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

from .util import temp_sibling


class AudioToolError(RuntimeError):
    pass


class AudioValidationError(ValueError):
    pass


# ------------------------------------------------------------------------ WAV I/O


def to_mono_float(audio) -> np.ndarray:
    a = np.asarray(audio, dtype=np.float32)
    if a.ndim > 1:
        # (channels, n) or (n, channels)
        a = a.mean(axis=0 if a.shape[0] < a.shape[-1] else 1)
    return np.nan_to_num(a.reshape(-1), nan=0.0, posinf=0.0, neginf=0.0)


def write_wav(path: str | Path, audio, sr: int) -> Path:
    """Atomically write mono float audio as 16-bit PCM WAV."""
    path = Path(path)
    a = np.clip(to_mono_float(audio), -1.0, 1.0)
    pcm = (a * 32767.0).round().astype("<i2")
    tmp = temp_sibling(path, ".wav")
    try:
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(sr))
            w.writeframes(pcm.tobytes())
        with open(tmp, "rb+") as fh:
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return path


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        width = w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if width == 2:
        a = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        a = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        a = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 3:
        b = np.frombuffer(frames, dtype=np.uint8).reshape(-1, 3)
        ints = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        a = ints.astype(np.float32) / 8388608.0
    else:
        raise AudioValidationError(f"unsupported WAV sample width {width}")
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    return a, sr


def resample_linear(a: np.ndarray, sr_in: int, sr_out: int) -> np.ndarray:
    if sr_in == sr_out or a.size == 0:
        return a
    n_out = int(round(a.size * sr_out / sr_in))
    x_old = np.linspace(0.0, 1.0, num=a.size, endpoint=False)
    x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
    return np.interp(x_new, x_old, a).astype(np.float32)


def time_stretch(a: np.ndarray, sr: int, rate: float) -> np.ndarray:
    """Change speaking pace without changing pitch (WSOLA, NumPy only). ``rate`` > 1 is faster/shorter,
    < 1 is slower/longer. Intended for modest factors (about 0.8-1.25); quality degrades beyond that."""
    a = np.asarray(a, dtype=np.float32).reshape(-1)
    if abs(rate - 1.0) < 1e-3 or a.size < int(sr * 0.2) or rate <= 0:
        return a
    n = int(sr * 0.04) // 2 * 2          # 40 ms frames
    hs = n // 2                            # synthesis hop (50% overlap, periodic Hann sums to 1)
    tol = int(sr * 0.012)                  # search +-12 ms for the best-matching splice point
    ha = hs * rate                         # analysis hop
    win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(n) / n)).astype(np.float32)
    pad = n + tol
    x = np.concatenate([np.zeros(pad, np.float32), a, np.zeros(pad + n, np.float32)])
    out_len = int(a.size / rate)
    out = np.zeros(out_len + 3 * n, np.float32)
    prev = None
    m = 0
    while True:
        o = m * hs
        nominal = pad + int(round((m - 1) * ha))
        if o + n > out.size or nominal + n + tol > x.size:
            break
        if prev is None:
            pos = nominal
        else:
            target = x[prev + hs: prev + hs + n]
            lo = max(0, nominal - tol)
            cand = np.lib.stride_tricks.sliding_window_view(x[lo: nominal + tol + n], n)
            score = cand @ target / np.sqrt(np.einsum("ij,ij->i", cand, cand) + 1e-6)
            pos = lo + int(np.argmax(score))
        out[o: o + n] += win * x[pos: pos + n]
        prev = pos
        m += 1
    return out[hs: hs + out_len].astype(np.float32)


# ---------------------------------------------------------------- acoustic QA


def db(x: float) -> float:
    return float(20.0 * np.log10(max(x, 1e-9)))


def frame_rms_db(a: np.ndarray, sr: int, frame_ms: int = 20) -> np.ndarray:
    n = max(1, int(sr * frame_ms / 1000))
    if a.size < n:
        return np.array([db(float(np.sqrt(np.mean(a**2)))) if a.size else -180.0])
    k = a.size // n
    frames = a[: k * n].reshape(k, n)
    rms = np.sqrt(np.mean(frames**2, axis=1))
    return 20.0 * np.log10(np.maximum(rms, 1e-9))


def silence_threshold_db(a: np.ndarray, sr: int) -> float:
    fr = frame_rms_db(a, sr)
    voiced = fr[fr > -60.0]
    speech_level = float(np.percentile(voiced, 75)) if voiced.size else -60.0
    return max(-60.0, min(-35.0, speech_level - 28.0))


def trim_silence(a: np.ndarray, sr: int, keep_ms: int = 60) -> np.ndarray:
    if a.size == 0:
        return a
    fr = frame_rms_db(a, sr)
    thr = silence_threshold_db(a, sr)
    voiced = np.where(fr > thr)[0]
    if voiced.size == 0:
        return a[:0]
    n = max(1, int(sr * 0.02))
    keep = int(sr * keep_ms / 1000)
    start = max(0, voiced[0] * n - keep)
    end = min(a.size, (voiced[-1] + 1) * n + keep)
    return a[start:end]


def expected_duration(text: str, wpm: float) -> float:
    words = max(1, len(text.split()))
    return words * 60.0 / wpm


def analyze(a: np.ndarray, sr: int, expected_s: float | None, qa: dict) -> dict:
    """Return {'metrics': {...}, 'issues': [...]} for one take."""
    issues: list[dict] = []
    metrics: dict = {"sample_rate": sr}

    def issue(sev, kind, msg, at=None):
        issues.append({"severity": sev, "kind": kind, "message": msg, "at_s": at, "source": "acoustic"})

    if a.size == 0:
        issue("error", "empty", "Empty audio (no samples).")
        metrics["duration_s"] = 0.0
        return {"metrics": metrics, "issues": issues}
    if not np.all(np.isfinite(a)):
        issue("error", "corrupt", "Audio contains NaN/Inf samples.")
        a = np.nan_to_num(a)
    dur = a.size / sr
    metrics["duration_s"] = round(dur, 3)
    peak = float(np.max(np.abs(a)))
    rms = float(np.sqrt(np.mean(a**2)))
    metrics["peak_dbfs"] = round(db(peak), 2)
    clip_ratio = float(np.mean(np.abs(a) >= 0.999))
    metrics["clip_ratio"] = round(clip_ratio, 6)
    if dur < 0.25:
        issue("error", "empty", f"Audio is only {dur:.2f}s long.")
    fr = frame_rms_db(a, sr)
    thr = silence_threshold_db(a, sr)
    voiced_mask = fr > thr
    speech_rms = float(np.sqrt(np.mean((a[: fr.size * max(1, int(sr * 0.02))].reshape(fr.size, -1)[voiced_mask] ** 2)))) if voiced_mask.any() else rms
    metrics["rms_dbfs"] = round(db(speech_rms), 2)
    metrics["silence_threshold_db"] = round(thr, 1)
    # noise floor: the quietest 100 ms windows (power-averaged), robust to a take with few pauses
    pw = 10 ** (fr / 10.0)
    win = 5
    if pw.size >= win:
        sm = np.convolve(pw, np.ones(win) / win, mode="valid")
    else:
        sm = pw
    metrics["noise_floor_dbfs"] = round(max(-90.0, float(10 * np.log10(max(float(np.percentile(sm, 5)), 1e-9)))), 1)
    frame_s = 0.02
    if voiced_mask.any():
        idx = np.where(voiced_mask)[0]
        lead = idx[0] * frame_s
        trail = (fr.size - 1 - idx[-1]) * frame_s
        longest, run, run_start, longest_at = 0, 0, 0, None
        for k in range(idx[0], idx[-1] + 1):
            if not voiced_mask[k]:
                if run == 0:
                    run_start = k
                run += 1
                if run > longest:
                    longest, longest_at = run, run_start * frame_s
            else:
                run = 0
        internal = longest * frame_s
    else:
        lead, trail, internal, longest_at = dur, 0.0, 0.0, None
        issue("error", "silent", "No speech detected - the take is silent.")
    metrics.update({"lead_silence_s": round(lead, 2), "trail_silence_s": round(trail, 2), "longest_internal_silence_s": round(internal, 2)})

    if clip_ratio > qa.get("clip_ratio_limit", 0.001):
        issue("error", "clipping", f"Clipping: {clip_ratio * 100:.2f}% of samples at full scale.")
    elif peak >= 0.999:
        issue("warning", "clipping", "Peak touches full scale.")
    if voiced_mask.any():
        if metrics["rms_dbfs"] < qa.get("min_rms_dbfs", -42.0):
            issue("error", "too_quiet", f"Speech level {metrics['rms_dbfs']} dBFS is abnormally low.")
        if metrics["rms_dbfs"] > qa.get("max_rms_dbfs", -8.0):
            issue("error", "too_loud", f"Speech level {metrics['rms_dbfs']} dBFS is abnormally high.")
    dead = qa.get("dead_air_seconds", 1.6)
    if internal > dead:
        issue("error", "dead_air", f"Dead air: {internal:.1f}s of silence inside the take.", round(longest_at or 0.0, 2))
    if trail > max(2.5, dead * 1.5):
        issue("warning", "trailing_silence", f"{trail:.1f}s trailing silence (trimmed at assembly).")
    if expected_s:
        speech_dur = max(0.01, dur - lead - trail)
        ratio = speech_dur / expected_s
        metrics["duration_ratio"] = round(ratio, 3)
        metrics["expected_s"] = round(expected_s, 2)
        if ratio < qa.get("duration_ratio_min", 0.5):
            issue("error", "too_short", f"Suspiciously short: {speech_dur:.1f}s of speech for ~{expected_s:.1f}s of text.")
        elif ratio > qa.get("duration_ratio_max", 2.0):
            issue("error", "too_long", f"Suspiciously long: {speech_dur:.1f}s of speech for ~{expected_s:.1f}s of text.")
    return {"metrics": metrics, "issues": issues}


def room_tone(n: int, dbfs: float, seed: int = 0) -> np.ndarray:
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    rng = np.random.default_rng(seed)
    amp = 10 ** (dbfs / 20.0)
    return (rng.standard_normal(n) * amp).astype(np.float32)


# ------------------------------------------------------------------ ffmpeg tools


def _tool(name: str, env_var: str) -> str:
    """Resolution order: explicit override, the app-owned copy in <root>/app/tools/bin (managed installs), PATH, and finally a
    pre-existing Homebrew copy so old Bash-era installs keep working. Nothing here ever installs anything."""
    b = os.environ.get(env_var)
    if not b:
        from .update.layout import Layout

        owned = Layout.discover().tools_bin / name
        if owned.exists():
            b = str(owned)
    b = b or shutil.which(name) or (f"/opt/homebrew/bin/{name}" if Path(f"/opt/homebrew/bin/{name}").exists() else None)
    if not b:
        raise AudioToolError(f"{name} not found. The app-owned copy is missing: re-run the installer to repair it (audiobook-studio doctor shows details).")
    return b


def ffmpeg_bin() -> str:
    return _tool("ffmpeg", "STUDIO_FFMPEG")


def ffprobe_bin() -> str:
    return _tool("ffprobe", "STUDIO_FFPROBE")


def run_ffmpeg(args: list[str], timeout: int = 3600) -> subprocess.CompletedProcess:
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", *args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise AudioToolError(f"ffmpeg timed out after {timeout}s") from exc
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-12:])
        raise AudioToolError(f"ffmpeg failed ({proc.returncode}): {tail}")
    return proc


def probe(path: str | Path) -> dict:
    cmd = [ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", "-show_chapters", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise AudioToolError("ffprobe timed out") from exc
    if proc.returncode != 0:
        raise AudioToolError(f"ffprobe could not read {Path(path).name}: {proc.stderr.strip()[-400:]}")
    return json.loads(proc.stdout or "{}")


def probe_summary(path: str | Path) -> dict:
    info = probe(path)
    fmt = info.get("format", {})
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    return {
        "duration_s": float(fmt.get("duration") or 0.0),
        "format_name": fmt.get("format_name"),
        "bit_rate": int(fmt.get("bit_rate") or 0) or None,
        "codec": audio.get("codec_name") if audio else None,
        "sample_rate": int(audio.get("sample_rate") or 0) if audio else None,
        "channels": audio.get("channels") if audio else None,
        "chapters": [
            {"title": (c.get("tags") or {}).get("title"), "start_s": float(c.get("start_time", 0)), "end_s": float(c.get("end_time", 0))}
            for c in info.get("chapters", [])
        ],
        "has_cover": any(s.get("codec_type") == "video" for s in info.get("streams", [])),
        "tags": fmt.get("tags", {}),
    }


def ffmpeg_version() -> str:
    try:
        out = subprocess.run([ffmpeg_bin(), "-version"], capture_output=True, text=True, timeout=20).stdout
        return out.splitlines()[0] if out else "unknown"
    except (AudioToolError, OSError, subprocess.TimeoutExpired):
        return "not found"


# --------------------------------------------------------- reference voice clips

AUDIO_UPLOAD_EXTS = {".wav", ".mp3", ".m4a", ".flac"}


def sniff_audio(data: bytes, ext: str) -> bool:
    head = data[:16]
    if ext == ".wav":
        return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
    if ext == ".flac":
        return head[:4] == b"fLaC"
    if ext == ".mp3":
        return head[:3] == b"ID3" or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
    if ext == ".m4a":
        return head[4:8] == b"ftyp"
    return False


def convert_reference(src: Path, dst: Path, sr: int = 24000, min_s: float = 3.0, max_s: float = 30.0) -> dict:
    """Convert an uploaded reference clip to mono PCM WAV at ``sr``, validate and normalise it."""
    try:
        info = probe_summary(src)
    except AudioToolError as exc:
        raise AudioValidationError(f"Not a readable audio file: {exc}") from exc
    if not info.get("codec"):
        raise AudioValidationError("The file contains no audio stream.")
    tmp = temp_sibling(dst, ".wav")
    try:
        run_ffmpeg(["-i", str(src), "-vn", "-ac", "1", "-ar", str(sr), "-sample_fmt", "s16", "-f", "wav", str(tmp)], timeout=300)
        a, rate = read_wav(tmp)
    except AudioToolError as exc:
        raise AudioValidationError(f"Could not decode audio: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)
    a = trim_silence(a, rate, keep_ms=150)
    dur = a.size / rate
    if dur < min_s:
        raise AudioValidationError(f"Reference clip is {dur:.1f}s of speech; at least {min_s:.0f}s is required.")
    if dur > max_s:
        raise AudioValidationError(f"Reference clip is {dur:.1f}s long; trim it to at most {max_s:.0f}s (5-15s works best).")
    peak = float(np.max(np.abs(a))) if a.size else 0.0
    if peak < 0.01:
        raise AudioValidationError("Reference clip is silent or nearly silent.")
    analysis = analyze(a, rate, None, {"clip_ratio_limit": 0.002, "min_rms_dbfs": -50, "max_rms_dbfs": -3, "dead_air_seconds": 3.0})
    a = a * min(10.0, 0.89 / peak)  # peak-normalise to about -1 dBFS
    write_wav(dst, a, rate)
    warnings = [i["message"] for i in analysis["issues"]]
    if info.get("sample_rate") and info["sample_rate"] < 16000:
        warnings.append(f"Source sample rate is only {info['sample_rate']} Hz; cloning quality may suffer.")
    return {"duration_s": round(dur, 2), "source": info, "sample_rate": rate, "warnings": warnings}
