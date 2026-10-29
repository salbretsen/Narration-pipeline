"""Platform and provider boundaries. Nothing outside the implementing modules should import launchd, MLX, GitHub or a
specific storage layout directly; they depend on these contracts, which makes Windows/Linux/container ports and
remote inference endpoints additions rather than rewrites.

Implemented now:  MacPlatformService / ProcessPlatformService (platform_services.py), FileQueueScheduler (scheduler.py),
                  SettingsModelRegistry + MLX/mock providers (providers.py), GitHubReleasesRepository / HttpsRepository
                  (update/repository.py), StudioStorageManager (providers.py), ReleaseUpdateManager (update/manager.py).
Designed only:    CUDA/CPU providers, remote HTTP TTS/ASR providers, containers (image-tag upgrades, no self-update)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass(frozen=True)
class ServiceSpec:
    """Everything needed to run the web service, as absolute paths taken from the app-controlled manifest."""
    label: str
    program: list[str]
    working_dir: str
    env: dict[str, str]
    stdout_log: str
    port: int
    host: str = "127.0.0.1"


class PlatformService(ABC):
    """Operating-system integration: background service lifecycle and machine facts."""

    name: str = "abstract"

    @abstractmethod
    def os_version(self) -> str: ...

    @abstractmethod
    def architecture(self) -> str: ...

    @abstractmethod
    def install_service(self, spec: ServiceSpec) -> None: ...

    @abstractmethod
    def uninstall_service(self, label: str) -> None: ...

    @abstractmethod
    def start(self, label: str) -> None: ...

    @abstractmethod
    def stop(self, label: str) -> None: ...

    @abstractmethod
    def restart(self, label: str) -> None: ...

    @abstractmethod
    def is_running(self, label: str) -> bool: ...

    @abstractmethod
    def launch_detached(self, label: str, argv: list[str], cwd: str, env: dict[str, str], log: str, plist_dir: Path) -> None:
        """Run a one-shot job outside the service's process group (the updater)."""


@dataclass(frozen=True)
class ModelDescriptor:
    role: str                    # tts_custom_voice | tts_clone | tts_voice_design | asr | planner
    model_id: str
    provider_id: str
    features: tuple[str, ...] = ()
    revision: Optional[str] = None


class ModelRegistry(ABC):
    """Which models exist for which role, independent of the code release and of where weights are cached."""

    @abstractmethod
    def list_models(self, role: Optional[str] = None) -> list[ModelDescriptor]: ...

    @abstractmethod
    def resolve(self, role: str) -> ModelDescriptor: ...


class InferenceProvider(ABC):
    provider_id: str = "abstract"
    kind: str = "abstract"        # local-mlx | local-cuda | local-cpu | remote-http | mock

    @abstractmethod
    def capabilities(self) -> dict[str, Any]: ...

    @abstractmethod
    def availability(self) -> tuple[bool, str]:
        """(usable, reason when not)."""


class TTSProvider(InferenceProvider):
    @abstractmethod
    def synthesize(self, voice, text: str, params, sid: Optional[str] = None): ...


class ASRProvider(InferenceProvider):
    @abstractmethod
    def transcribe(self, wav_path: str | Path, language: str = "en") -> dict: ...


class ReleaseRepository(ABC):
    """Where signed release metadata and artifacts come from (GitHub Releases now, a first-party HTTPS host later).
    Implementations only *locate and fetch*; authenticity is decided by the pinned-key TrustStore, never by the host."""

    name: str = "abstract"

    @abstractmethod
    def index_url(self, channel: str) -> str: ...

    @abstractmethod
    def fetch_index(self, channel: str) -> bytes: ...

    @abstractmethod
    def download(self, url: str, dest: Path, size: int, progress=None, cancelled=None) -> Path: ...


class UpdateManager(ABC):
    @abstractmethod
    def check(self) -> dict: ...

    @abstractmethod
    def download(self) -> dict: ...

    @abstractmethod
    def status(self) -> dict: ...


class StorageManager(ABC):
    @abstractmethod
    def library_root(self) -> Path: ...

    @abstractmethod
    def model_cache(self) -> Path: ...

    @abstractmethod
    def check_available(self, path: Path) -> tuple[bool, str]: ...

    @abstractmethod
    def free_bytes(self, path: Path) -> int: ...


@dataclass
class HoldRequest:
    mode: str                    # "now": no new work; "after_job": running jobs may finish, nothing new starts
    reason: str = "update"
    since: float = 0.0
    extra: dict = field(default_factory=dict)


class QueueScheduler(ABC):
    @abstractmethod
    def hold(self, mode: str, reason: str = "update") -> None: ...

    @abstractmethod
    def release(self) -> None: ...

    @abstractmethod
    def current_hold(self) -> Optional[HoldRequest]: ...

    @abstractmethod
    def allows(self, job: dict) -> bool:
        """May the worker run (or continue) this job right now?"""

    @abstractmethod
    def drained(self) -> bool:
        """True when no work is in flight, so the service may be stopped without losing a partial segment."""
