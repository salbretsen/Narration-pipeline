"""Validated application settings, persisted as JSON under <home>/config/settings.json.

Settings are global defaults. Each book stores a snapshot at import time so that later
changes never silently mix voices, models or QA rules inside an existing book.
"""
from __future__ import annotations

import ipaddress
import json
import platform
import re
import sys
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .util import atomic_write_text, dumps, short_hash

STORAGE_CATEGORIES = (
    "manuscripts",
    "references",
    "segments",
    "alternates",
    "reports",
    "masters",
    "exports",
)

CATEGORY_LABELS = {
    "manuscripts": "Original manuscripts (immutable copies)",
    "references": "Narrator reference clips",
    "segments": "Selected segment takes",
    "alternates": "Rejected / alternate takes",
    "reports": "QA reports",
    "masters": "Mastered chapter WAV/MP3",
    "exports": "Exports (M4B, ZIP)",
}

PRESET_SPEAKERS = {
    # Qwen3-TTS CustomVoice speakers documented by mlx-audio. Ryan and Aiden are the native
    # English speakers; the others are native Chinese speakers that can also read English.
    "Ryan": "Native English male - warm, articulate",
    "Aiden": "Native English male - bright, youthful",
    "Vivian": "Chinese female (non-native English)",
    "Serena": "Chinese female (non-native English)",
    "Uncle_Fu": "Chinese male, mature (non-native English)",
    "Dylan": "Chinese male, Beijing dialect (non-native English)",
    "Eric": "Chinese male, Sichuan dialect (non-native English)",
}
ENGLISH_SPEAKERS = ("Ryan", "Aiden")

_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+$")


def default_engine() -> str:
    if sys.platform == "darwin" and platform.machine() == "arm64":
        return "mlx"
    return "mock"


def _check_model_id(value: str) -> str:
    value = (value or "").strip()
    if _MODEL_ID_RE.match(value):
        return value
    if value.startswith("/") and Path(value).is_absolute():
        return value  # local model directory
    raise ValueError("must be a Hugging Face repo id (owner/name) or an absolute local path")


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=True)


class StorageSettings(_M):
    library_root: str = ""  # empty -> <home>/library
    manuscripts_dir: str = ""
    references_dir: str = ""
    segments_dir: str = ""
    alternates_dir: str = ""
    reports_dir: str = ""
    masters_dir: str = ""
    exports_dir: str = ""

    @field_validator("*")
    @classmethod
    def no_nul(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("path contains a NUL byte")
        return v.strip()


class ChunkSettings(_M):
    min_chars: int = Field(150, ge=40, le=600)
    max_chars: int = Field(400, ge=80, le=1200)
    merge_short_paragraphs: bool = True
    speak_chapter_titles: bool = True
    normalize_for_speech: bool = True

    @model_validator(mode="after")
    def check_order(self):
        if self.min_chars >= self.max_chars:
            raise ValueError("min_chars must be smaller than max_chars")
        return self


class QASettings(_M):
    enable_asr: bool = True
    wer_threshold: float = Field(0.12, ge=0.0, le=1.0)
    max_attempts: int = Field(3, ge=1, le=10)
    max_dropped_words: int = Field(2, ge=0, le=20)
    max_inserted_words: int = Field(2, ge=0, le=20)
    dead_air_seconds: float = Field(1.6, ge=0.3, le=10.0)
    clip_ratio_limit: float = Field(0.001, ge=0.0, le=0.1)
    min_rms_dbfs: float = Field(-42.0, ge=-80.0, le=-10.0)
    max_rms_dbfs: float = Field(-8.0, ge=-30.0, le=0.0)
    duration_ratio_min: float = Field(0.5, ge=0.1, le=1.0)
    duration_ratio_max: float = Field(2.0, ge=1.0, le=5.0)
    words_per_minute: float = Field(155.0, ge=60.0, le=300.0)

    @model_validator(mode="after")
    def check_order(self):
        if self.min_rms_dbfs >= self.max_rms_dbfs:
            raise ValueError("min_rms_dbfs must be below max_rms_dbfs")
        return self


class PauseSettings(_M):
    sentence_ms: int = Field(300, ge=0, le=3000)
    paragraph_ms: int = Field(800, ge=0, le=5000)
    scene_break_ms: int = Field(1800, ge=0, le=8000)
    after_heading_ms: int = Field(1500, ge=0, le=8000)
    chapter_head_ms: int = Field(800, ge=0, le=5000)
    chapter_tail_ms: int = Field(2500, ge=0, le=10000)
    room_tone_dbfs: float = Field(-72.0, ge=-120.0, le=-50.0)
    trim_silence: bool = True


class ModelSettings(_M):
    engine: Literal["mlx", "mock"] = Field(default_factory=default_engine)
    custom_voice_model: str = "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-bf16"
    base_model: str = "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16"
    voice_design_model: str = "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-bf16"
    asr_model: str = "mlx-community/whisper-large-v3-turbo"
    language: str = Field("English", min_length=2, max_length=32)
    temperature: Optional[float] = Field(None, ge=0.05, le=1.5)
    top_p: Optional[float] = Field(None, ge=0.05, le=1.0)
    repetition_penalty: Optional[float] = Field(None, ge=1.0, le=2.0)
    retry_temperatures: list[float] = Field(default_factory=lambda: [0.7, 0.55])
    max_warm_models: int = Field(2, ge=1, le=4)
    allow_downloads: bool = True

    @field_validator("custom_voice_model", "base_model", "voice_design_model", "asr_model")
    @classmethod
    def check_model_ids(cls, v: str) -> str:
        return _check_model_id(v)

    @field_validator("retry_temperatures")
    @classmethod
    def check_temps(cls, v: list[float]) -> list[float]:
        if len(v) > 9:
            raise ValueError("at most 9 retry temperatures")
        for t in v:
            if not 0.05 <= float(t) <= 1.5:
                raise ValueError("retry temperatures must be between 0.05 and 1.5")
        return [float(t) for t in v]

    @field_validator("language")
    @classmethod
    def check_lang(cls, v: str) -> str:
        if not re.match(r"^[A-Za-z][A-Za-z _-]+$", v):
            raise ValueError("language must be a plain language name such as English")
        return v.strip()


class ExportSettings(_M):
    sample_rate: Literal[22050, 44100, 48000] = 44100
    mp3_bitrate_kbps: Literal[128, 160, 192, 256, 320] = 192
    m4b_bitrate_kbps: Literal[64, 96, 128, 160] = 96
    loudness_normalize: bool = True
    target_lufs: float = Field(-19.0, ge=-30.0, le=-12.0)
    true_peak_db: float = Field(-3.0, ge=-9.0, le=-0.5)
    auto_assemble_drafts: bool = True


def is_loopback(host: str) -> bool:
    if host in ("localhost",):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


class ServerSettings(_M):
    host: str = "127.0.0.1"
    port: int = Field(7861, ge=1024, le=65535)
    allow_remote: bool = False
    https_enabled: bool = False
    https_port: int = Field(8443, ge=1024, le=65535)
    allowed_hosts: list[str] = Field(default_factory=list, description="extra Host header names accepted")
    max_manuscript_mb: int = Field(100, ge=1, le=2000)
    max_reference_mb: int = Field(25, ge=1, le=200)

    @model_validator(mode="after")
    def check_https(self):
        if self.https_enabled and self.https_port == self.port:
            raise ValueError("https_port must differ from port")
        return self

    @model_validator(mode="after")
    def check_bind(self):
        if not is_loopback(self.host) and not self.allow_remote:
            raise ValueError(
                "binding to a non-loopback address exposes an unauthenticated service; "
                "set allow_remote=true explicitly if you really want that"
            )
        return self


class WorkerSettings(_M):
    gpu_workers: int = Field(1, ge=1, le=1, description="MLX inference runs serially")
    autostart: bool = True
    prevent_sleep: bool = True
    max_consecutive_errors: int = Field(5, ge=1, le=50)


class Settings(_M):
    version: int = 1
    storage: StorageSettings = Field(default_factory=StorageSettings)
    chunking: ChunkSettings = Field(default_factory=ChunkSettings)
    qa: QASettings = Field(default_factory=QASettings)
    pauses: PauseSettings = Field(default_factory=PauseSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    export: ExportSettings = Field(default_factory=ExportSettings)
    server: ServerSettings = Field(default_factory=ServerSettings)
    worker: WorkerSettings = Field(default_factory=WorkerSettings)


def load_settings(path: Path) -> Settings:
    """Load settings, merging defaults for any keys missing from older files."""
    if not path.exists():
        return Settings()
    raw = json.loads(path.read_text("utf-8") or "{}")
    if not isinstance(raw, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return Settings.model_validate(raw)


def save_settings(path: Path, settings: Settings) -> None:
    atomic_write_text(path, json.dumps(settings.model_dump(mode="json"), indent=2) + "\n")


def merge_patch(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_patch(out[k], v)
        else:
            out[k] = v
    return out


def book_snapshot(settings: Settings) -> dict:
    """The subset of settings that is frozen per book."""
    data = settings.model_dump(mode="json")
    models = dict(data["models"])
    models.pop("max_warm_models", None)
    models.pop("allow_downloads", None)
    return {
        "chunking": data["chunking"],
        "qa": data["qa"],
        "pauses": data["pauses"],
        "models": models,
        "export": data["export"],
    }


def synth_fingerprint(snapshot: dict, voice_version: dict | None, instructions: str | None) -> str:
    """Hash of everything that changes how a segment *sounds*. Takes with a different
    fingerprint must never be assembled into the same book."""
    models = snapshot.get("models", {})
    vv = voice_version or {}
    payload = {
        "engine": models.get("engine"),
        "language": models.get("language"),
        "temperature": models.get("temperature"),
        "top_p": models.get("top_p"),
        "repetition_penalty": models.get("repetition_penalty"),
        "voice_version_id": vv.get("id"),
        "voice_model": vv.get("model_id"),
        "speaker": vv.get("speaker"),
        "voice_instruct": vv.get("instruct"),
        "ref_sha256": vv.get("ref_sha256"),
        "instructions": (instructions or "").strip(),
    }
    return short_hash(dumps(payload), 16)
