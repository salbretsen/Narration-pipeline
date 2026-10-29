"""Concrete provider-side implementations of the contracts in interfaces.py.

Implemented: local MLX and mock TTS/ASR descriptors, a settings-backed ModelRegistry, a filesystem StorageManager.
Not implemented (by design, this phase): CUDA/CPU and remote-HTTP providers. They would implement TTSProvider/ASRProvider and be
added to ``PROVIDERS``; the UI shows a provider selector only once more than one usable provider exists."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from .interfaces import ASRProvider, InferenceProvider, ModelDescriptor, ModelRegistry, StorageManager, TTSProvider


class MlxTTSProvider(TTSProvider):
    provider_id, kind = "mlx-qwen3-tts", "local-mlx"

    def __init__(self, engine=None):
        self._engine = engine

    def capabilities(self) -> dict[str, Any]:
        return {"preset_voices": True, "voice_design": True, "voice_clone": True, "streaming": False, "platforms": ["macos-arm64"]}

    def availability(self) -> tuple[bool, str]:
        if sys.platform != "darwin":
            return False, "MLX needs an Apple Silicon Mac"
        try:
            import mlx.core  # noqa: F401
        except Exception as exc:
            return False, f"mlx is not importable ({type(exc).__name__})"
        return True, ""

    def synthesize(self, voice, text, params, sid: Optional[str] = None):
        return self._engine.synthesize(voice, text, params, sid)


class MlxWhisperProvider(ASRProvider):
    provider_id, kind = "mlx-whisper", "local-mlx"

    def __init__(self, asr=None):
        self._asr = asr

    def capabilities(self) -> dict[str, Any]:
        return {"word_timestamps": True, "languages": "multilingual", "platforms": ["macos-arm64"]}

    def availability(self) -> tuple[bool, str]:
        return MlxTTSProvider().availability()

    def transcribe(self, wav_path, language: str = "en") -> dict:
        return self._asr.transcribe(wav_path, language)


class MockTTSProvider(TTSProvider):
    provider_id, kind = "mock-tts", "mock"

    def __init__(self, engine=None):
        self._engine = engine

    def capabilities(self) -> dict[str, Any]:
        return {"preset_voices": True, "voice_design": True, "voice_clone": True, "synthetic_tones": True}

    def availability(self) -> tuple[bool, str]:
        return True, ""

    def synthesize(self, voice, text, params, sid: Optional[str] = None):
        return self._engine.synthesize(voice, text, params, sid)


class MockASRProvider(ASRProvider):
    provider_id, kind = "mock-asr", "mock"

    def __init__(self, asr=None):
        self._asr = asr

    def capabilities(self) -> dict[str, Any]:
        return {"synthetic": True}

    def availability(self) -> tuple[bool, str]:
        return True, ""

    def transcribe(self, wav_path, language: str = "en") -> dict:
        return self._asr.transcribe(wav_path, language)


PROVIDERS: dict[str, type[InferenceProvider]] = {c.provider_id: c for c in (MlxTTSProvider, MlxWhisperProvider, MockTTSProvider, MockASRProvider)}


def usable_providers(role: Optional[str] = None) -> list[str]:
    out = []
    for pid, cls in PROVIDERS.items():
        if role == "tts" and not issubclass(cls, TTSProvider) or role == "asr" and not issubclass(cls, ASRProvider):
            continue
        if cls().availability()[0]:
            out.append(pid)
    return out


class SettingsModelRegistry(ModelRegistry):
    """Models as configured in settings; revisions are recorded per generation elsewhere, so a code update never invalidates audio."""

    def __init__(self, ctx):
        self.ctx = ctx

    def _provider(self, asr: bool = False) -> str:
        if self.ctx.engine_mode == "mock":
            return "mock-asr" if asr else "mock-tts"
        return "mlx-whisper" if asr else "mlx-qwen3-tts"

    def list_models(self, role: Optional[str] = None) -> list[ModelDescriptor]:
        m = self.ctx.settings().models
        items = [ModelDescriptor("tts_custom_voice", m.custom_voice_model, self._provider(), ("preset_voices",)),
                 ModelDescriptor("tts_clone", m.base_model, self._provider(), ("voice_clone",)),
                 ModelDescriptor("tts_voice_design", m.voice_design_model, self._provider(), ("voice_design",)),
                 ModelDescriptor("asr", m.asr_model, self._provider(asr=True), ("word_timestamps",))]
        return [i for i in items if role is None or i.role == role]

    def resolve(self, role: str) -> ModelDescriptor:
        found = self.list_models(role)
        if not found:
            raise KeyError(f"no model registered for role {role!r}")
        return found[0]


class StudioStorageManager(StorageManager):
    """Library root, model cache and availability checks, all independent of the release directory."""

    def __init__(self, ctx):
        self.ctx = ctx

    def library_root(self) -> Path:
        return self.ctx.library_root()

    def model_cache(self) -> Path:
        d = self.ctx.settings().models.cache_dir or os.environ.get("HF_HOME") or str(self.ctx.home / "cache" / "huggingface")
        return Path(d).expanduser()

    def check_available(self, path: Path) -> tuple[bool, str]:
        p = Path(path).expanduser()
        if p.exists() and os.access(p, os.W_OK):
            return True, ""
        parts = p.parts
        if len(parts) > 2 and parts[1] == "Volumes" and not Path(*parts[:3]).exists():
            return False, f"The volume '{parts[2]}' is not mounted. Connect the disk; paused work resumes by itself."
        if p.exists():
            return False, f"{p} is not writable (check permissions or macOS privacy settings)."
        return False, f"{p} does not exist."

    def free_bytes(self, path: Path) -> int:
        p = Path(path).expanduser()
        while not p.exists() and p != p.parent:
            p = p.parent
        return shutil.disk_usage(p).free
