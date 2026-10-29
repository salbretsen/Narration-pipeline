"""Speech synthesis and recognition engines.

``MlxQwenEngine`` / ``MlxWhisperASR`` run real local inference on Apple Silicon through
mlx-audio (Qwen3-TTS) and mlx-whisper. ``MockEngine`` / ``MockASR`` produce deterministic
*synthetic test tones* for automated tests and non-Mac development; they are labelled
``engine="mock"`` everywhere and are never presented as model inference.
"""
from __future__ import annotations

import gc
import hashlib
import importlib.metadata as md
import inspect
import json
import logging
import os
import platform
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

log = logging.getLogger("studio.engines")


class EngineUnavailable(RuntimeError):
    """The engine or its model cannot be loaded (missing package, download blocked...)."""


@dataclass
class VoiceSpec:
    kind: str  # preset | design | clone
    model_id: str
    speaker: str | None = None
    language: str = "English"
    instruct: str | None = None
    ref_audio_path: str | None = None
    ref_text: str | None = None
    version_id: int | None = None

    @classmethod
    def from_rows(cls, voice: dict, version: dict) -> "VoiceSpec":
        return cls(
            kind=voice["kind"],
            model_id=version["model_id"],
            speaker=version.get("speaker"),
            language=version.get("language") or "English",
            instruct=version.get("instruct"),
            ref_audio_path=version.get("ref_audio_path"),
            ref_text=version.get("ref_text"),
            version_id=version.get("id"),
        )


@dataclass
class SynthParams:
    seed: int = 0
    temperature: float | None = None
    top_p: float | None = None
    repetition_penalty: float | None = None
    max_tokens: int | None = None
    instruct: str | None = None  # per-book delivery instruction


@dataclass
class SynthResult:
    audio: np.ndarray
    sample_rate: int
    engine: str
    model_id: str
    seconds: float
    rtf: float | None = None
    peak_mem_gb: float | None = None
    meta: dict = field(default_factory=dict)


def package_version(name: str) -> str | None:
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


def hf_revision(model_id: str) -> str | None:
    """Commit hash of the cached Hugging Face snapshot, if present."""
    if model_id.startswith("/"):
        return "local"
    try:
        from huggingface_hub import scan_cache_dir  # type: ignore

        for repo in scan_cache_dir().repos:
            if repo.repo_id == model_id:
                revs = sorted(repo.revisions, key=lambda r: r.last_modified, reverse=True)
                return revs[0].commit_hash if revs else None
    except Exception:
        return None
    return None


def max_tokens_for(text: str, wpm: float = 155.0) -> int:
    # Qwen3-TTS-12Hz emits ~12.5 codec frames per second; cap generation at 2.5x the
    # expected duration to stop runaway babble without truncating slow delivery.
    words = max(1, len(text.split()))
    est_s = words * 60.0 / wpm
    return int(max(160, est_s * 12.5 * 2.5 + 60))


# =============================================================================== MLX


class MlxQwenEngine:
    name = "mlx-qwen3-tts"

    def __init__(self, max_warm: int = 2, allow_downloads: bool = True):
        self.max_warm = max(1, int(max_warm))
        self.allow_downloads = allow_downloads
        self._models: "OrderedDict[str, object]" = OrderedDict()
        self._lock = threading.Lock()

    def versions(self) -> dict:
        return {
            "mlx": package_version("mlx"),
            "mlx-audio": package_version("mlx-audio"),
            "mlx-whisper": package_version("mlx-whisper"),
            "transformers": package_version("transformers"),
            "python": platform.python_version(),
            "machine": platform.machine(),
        }

    def _mx(self):
        try:
            import mlx.core as mx  # type: ignore
        except Exception as exc:  # pragma: no cover - only on non-Apple machines
            raise EngineUnavailable(f"MLX is not available on this machine: {exc}") from exc
        return mx

    def _model(self, model_id: str):
        with self._lock:
            if model_id in self._models:
                self._models.move_to_end(model_id)
                return self._models[model_id]
            mx = self._mx()
            while len(self._models) >= self.max_warm:
                old_id, old = self._models.popitem(last=False)
                log.info("Unloading model %s", old_id)
                del old
                gc.collect()
                try:
                    mx.clear_cache()
                except AttributeError:  # older MLX
                    mx.metal.clear_cache()
            if not self.allow_downloads:
                os.environ["HF_HUB_OFFLINE"] = "1"
            try:
                from mlx_audio.tts.utils import load_model  # type: ignore
            except Exception as exc:
                raise EngineUnavailable(f"mlx-audio is not installed or failed to import: {exc}") from exc
            t0 = time.time()
            try:
                model = load_model(model_id)
            except Exception as exc:
                hint = " (model downloads are disabled in Settings)" if not self.allow_downloads else ""
                raise EngineUnavailable(f"Could not load {model_id}{hint}: {exc}") from exc
            log.info("Loaded %s in %.1fs (sample rate %s)", model_id, time.time() - t0, getattr(model, "sample_rate", "?"))
            self._models[model_id] = model
            return model

    def unload_all(self) -> None:
        with self._lock:
            self._models.clear()
            gc.collect()

    @staticmethod
    def _filter_kwargs(fn, kwargs: dict) -> dict:
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return {k: v for k, v in kwargs.items() if v is not None}
        accepts_var = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
        return {k: v for k, v in kwargs.items() if v is not None and (k in params or accepts_var)}

    def synthesize(self, voice: VoiceSpec, text: str, params: SynthParams, sid: str | None = None) -> SynthResult:
        mx = self._mx()
        model = self._model(voice.model_id)
        text = " ".join(text.split())  # no newlines: generate() splits on them
        mx.random.seed(int(params.seed) % (2**32))
        common = {
            "temperature": params.temperature,
            "top_p": params.top_p,
            "repetition_penalty": params.repetition_penalty,
            "max_tokens": params.max_tokens,
            "verbose": False,
        }
        instruct = " ".join(x for x in (voice.instruct, params.instruct) if x and x.strip()) or None
        t0 = time.time()
        try:
            if voice.kind == "preset":
                if not voice.speaker:
                    raise ValueError("preset voice has no speaker")
                fn = model.generate_custom_voice
                gen = fn(text=text, speaker=voice.speaker, language=voice.language, instruct=instruct,
                         **self._filter_kwargs(fn, common))
            elif voice.kind == "design":
                if not instruct:
                    raise ValueError("designed voice requires a description")
                fn = model.generate_voice_design
                gen = fn(text=text, instruct=instruct, language=voice.language, **self._filter_kwargs(fn, common))
            elif voice.kind == "clone":
                if not (voice.ref_audio_path and voice.ref_text):
                    raise ValueError("cloned voice requires reference audio and its exact transcript")
                fn = model.generate
                # Base models take ref_audio/ref_text. Never pass a speaker/voice argument here.
                extra = dict(common, lang_code=(voice.language or "auto").lower())
                gen = fn(text=text, ref_audio=voice.ref_audio_path, ref_text=voice.ref_text,
                         **self._filter_kwargs(fn, extra))
            else:
                raise ValueError(f"unknown voice kind {voice.kind!r}")
            chunks, sr, rtf, peak = [], None, None, None
            for res in gen:
                arr = res.audio
                try:
                    arr = arr.astype(mx.float32)
                except AttributeError:
                    pass
                chunks.append(np.array(arr, dtype=np.float32).reshape(-1))
                sr = getattr(res, "sample_rate", None) or sr
                rtf = getattr(res, "real_time_factor", None) or rtf
                peak = getattr(res, "peak_memory_usage", None) or peak
        except EngineUnavailable:
            raise
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        sr = int(sr or getattr(model, "sample_rate", 24000))
        elapsed = time.time() - t0
        dur = audio.size / sr if sr else 0.0
        try:
            peak_gb = float(mx.get_peak_memory()) / 1e9
        except AttributeError:
            try:
                peak_gb = float(mx.metal.get_peak_memory()) / 1e9
            except Exception:
                peak_gb = float(peak) if peak else None
        return SynthResult(
            audio=audio, sample_rate=sr, engine=self.name, model_id=voice.model_id, seconds=elapsed,
            rtf=(elapsed / dur) if dur else None, peak_mem_gb=peak_gb,
            meta={"library_rtf": rtf, "model_revision": hf_revision(voice.model_id)},
        )


class MlxWhisperASR:
    name = "mlx-whisper"

    def __init__(self, model_id: str, allow_downloads: bool = True):
        self.model_id = model_id
        self.allow_downloads = allow_downloads

    def transcribe(self, wav_path: str | Path, language: str = "en") -> dict:
        if not self.allow_downloads:
            os.environ["HF_HUB_OFFLINE"] = "1"
        try:
            import mlx_whisper  # type: ignore
        except Exception as exc:
            raise EngineUnavailable(f"mlx-whisper is not installed or failed to import: {exc}") from exc
        t0 = time.time()
        try:
            out = mlx_whisper.transcribe(
                str(wav_path),
                path_or_hf_repo=self.model_id,
                language=language,
                word_timestamps=True,
                condition_on_previous_text=False,
                temperature=0.0,
                verbose=None,
            )
        except Exception as exc:
            raise EngineUnavailable(f"Whisper transcription failed ({self.model_id}): {exc}") from exc
        words = []
        for seg in out.get("segments", []) or []:
            for w in seg.get("words", []) or []:
                words.append({"word": w.get("word", ""), "start": w.get("start"), "end": w.get("end"), "p": w.get("probability")})
        return {"text": (out.get("text") or "").strip(), "words": words, "engine": self.name, "model": self.model_id, "seconds": time.time() - t0}


# ============================================================================== MOCK


class MockEngine:
    """Deterministic synthetic audio for tests. NOT speech and NOT model inference.

    ``faults`` maps a segment sid, or ``"text:<substring>"``, to a list of fault names
    consumed one per attempt: ok | drop | repeat | babble | silence | clip | quiet | error."""

    name = "mock"
    SAMPLE_RATE = 24000

    def __init__(self, faults: dict | None = None):
        self.faults: dict[str, list[str]] = {k: list(v) for k, v in (faults or {}).items()}
        env = os.environ.get("STUDIO_MOCK_FAULTS")
        if env and Path(env).exists():
            for k, v in json.loads(Path(env).read_text("utf-8")).items():
                self.faults.setdefault(k, list(v))
        self.calls = 0

    def versions(self) -> dict:
        return {"engine": "mock", "python": platform.python_version()}

    def _fault(self, sid: str | None, text: str) -> str:
        for key in ([sid] if sid else []) + [k for k in self.faults if k.startswith("text:") and k[5:] in text]:
            seq = self.faults.get(key)
            if seq:
                return seq.pop(0)
        return "ok"

    def synthesize(self, voice: VoiceSpec, text: str, params: SynthParams, sid: str | None = None) -> SynthResult:
        self.calls += 1
        fault = self._fault(sid, text)
        if fault == "error":
            raise RuntimeError("mock engine: injected synthesis failure")
        words = text.split()
        spoken = list(words)
        if fault == "drop" and len(words) > 6:
            mid = len(words) // 2
            spoken = words[: mid - 2] + words[mid + 2:]
        elif fault == "repeat" and len(words) > 4:
            spoken = words[:4] + words[1:4] + words[4:]
        elif fault == "babble":
            spoken = words + ["and", "then", "something", "else", "entirely"]
        sr = self.SAMPLE_RATE
        rng = np.random.default_rng(params.seed or 1)
        voice_key = (voice.speaker or voice.instruct or voice.ref_text or voice.kind or "v")
        base_f = 110 + int(hashlib.md5(voice_key.encode()).hexdigest(), 16) % 90
        pieces = []
        for w in spoken:
            d = 0.12 + 0.035 * len(w)
            t = np.arange(int(sr * d)) / sr
            f = base_f * (1 + (int(hashlib.md5(w.encode()).hexdigest(), 16) % 7) / 10)
            env = np.sin(np.pi * t / d) ** 0.6
            tone = 0.22 * env * (np.sin(2 * np.pi * f * t) + 0.35 * np.sin(4 * np.pi * f * t) + 0.12 * np.sin(6 * np.pi * f * t))
            pieces.append(tone + rng.standard_normal(t.size) * 0.002)
            pieces.append(np.zeros(int(sr * 0.07)))
        audio = np.concatenate([np.zeros(int(sr * 0.08))] + pieces + [np.zeros(int(sr * 0.1))]).astype(np.float32)
        if fault == "silence":
            mid = audio.size // 2
            audio = np.concatenate([audio[:mid], np.zeros(int(sr * 3.0), dtype=np.float32), audio[mid:]])
        elif fault == "clip":
            audio = np.clip(audio * 12.0, -1.0, 1.0)
        elif fault == "quiet":
            audio = audio * 0.002
        return SynthResult(audio=audio, sample_rate=sr, engine=self.name, model_id=f"mock:{voice.model_id}",
                           seconds=0.0, rtf=None, peak_mem_gb=None,
                           meta={"mock_transcript": " ".join(spoken), "mock_fault": fault, "label": "synthetic test tones - not speech"})


class MockASR:
    """Returns the transcript recorded by MockEngine in the take's provenance sidecar."""

    name = "mock-asr"

    def __init__(self):
        self.model_id = "mock"

    def transcribe(self, wav_path: str | Path, language: str = "en") -> dict:
        side = Path(str(wav_path) + ".json")
        text = ""
        if side.exists():
            meta = json.loads(side.read_text("utf-8"))
            text = meta.get("meta", {}).get("mock_transcript", "")
            if meta.get("meta", {}).get("mock_fault") == "quiet":
                text = ""
        words, t = [], 0.08
        for w in text.split():
            d = 0.12 + 0.035 * len(w)
            words.append({"word": " " + w, "start": round(t, 2), "end": round(t + d, 2)})
            t += d + 0.07
        return {"text": text, "words": words, "engine": self.name, "model": "mock", "seconds": 0.0}


def create_tts(ctx) -> object:
    s = ctx.settings()
    if ctx.engine_mode == "mock":
        return MockEngine()
    return MlxQwenEngine(max_warm=s.models.max_warm_models, allow_downloads=s.models.allow_downloads)


def create_asr(ctx) -> object:
    s = ctx.settings()
    if ctx.engine_mode == "mock":
        return MockASR()
    return MlxWhisperASR(s.models.asr_model, allow_downloads=s.models.allow_downloads)
