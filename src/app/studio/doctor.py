"""Environment diagnostics shared by `studio doctor` and the About page."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .audio import ffmpeg_version
from .db import LATEST_VERSION
from .engines import package_version
from .storage import validate_dir

MODEL_LICENSES = [
    {"component": "Qwen3-TTS (CustomVoice / Base / VoiceDesign)", "source": "Qwen team, Alibaba Cloud; MLX conversions by mlx-community",
     "license": "Apache-2.0 (verify on the model card before commercial use)", "url": "https://huggingface.co/collections/Qwen/qwen3-tts"},
    {"component": "Whisper large-v3-turbo (MLX)", "source": "OpenAI Whisper; MLX conversion by mlx-community", "license": "MIT",
     "url": "https://huggingface.co/mlx-community/whisper-large-v3-turbo"},
    {"component": "mlx-audio", "source": "Prince Canuma / Blaizzy", "license": "MIT", "url": "https://github.com/Blaizzy/mlx-audio"},
    {"component": "MLX / mlx-whisper", "source": "Apple ml-explore", "license": "MIT", "url": "https://github.com/ml-explore"},
    {"component": "FFmpeg / FFprobe (app-owned static build, downloaded by the installer, not bundled)", "source": "FFmpeg project; build from the host pinned in tools/runtime-manifest.json",
     "license": "LGPL/GPL depending on the pinned build (see docs/10-installation-and-updates.md)", "url": "https://ffmpeg.org/legal.html"},
]


def _check(name: str, status: str, detail: str) -> dict:
    return {"name": name, "status": status, "detail": detail}


def hf_cached(model_id: str) -> bool | None:
    try:
        from huggingface_hub import try_to_load_from_cache  # type: ignore
    except Exception:
        return None
    try:
        res = try_to_load_from_cache(model_id, "config.json")
        return isinstance(res, str)
    except Exception:
        return None


def run_checks(ctx, deep: bool = False) -> list[dict]:
    s = ctx.settings()
    out = []
    py = platform.python_version()
    out.append(_check("Python", "ok" if sys.version_info[:2] >= (3, 11) else "fail", f"{py} ({sys.executable})"))
    mac = sys.platform == "darwin"
    arm = platform.machine() == "arm64"
    out.append(_check("Platform", "ok" if (mac and arm) else "warn", f"{platform.platform()} - {'Apple Silicon' if mac and arm else 'not Apple Silicon: MLX inference unavailable, mock engine only'}"))
    if mac:
        try:
            mem = int(subprocess.run(["/usr/sbin/sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout.strip() or 0)
            chip = subprocess.run(["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=5).stdout.strip()
            out.append(_check("Hardware", "ok" if mem >= 32 * 1024**3 else "warn", f"{chip}, {mem / 1024**3:.0f} GB unified memory"))
        except Exception:
            pass
    ff = ffmpeg_version()
    out.append(_check("FFmpeg", "ok" if ff != "not found" else "fail", ff))
    try:
        from .audio import ffprobe_bin

        out.append(_check("ffprobe", "ok", ffprobe_bin()))
    except Exception as exc:
        out.append(_check("ffprobe", "fail", str(exc)))
    for pkg in ("mlx", "mlx-audio", "mlx-whisper", "fastapi", "uvicorn", "numpy", "huggingface-hub"):
        v = package_version(pkg)
        needed = pkg in ("fastapi", "uvicorn", "numpy") or ctx.engine_mode == "mlx"
        out.append(_check(f"package {pkg}", "ok" if v else ("fail" if needed else "warn"), v or "not installed"))
    out.append(_check("Engine mode", "ok" if ctx.engine_mode == "mlx" else "warn",
                      "mlx (real local inference)" if ctx.engine_mode == "mlx" else "mock - synthetic test tones, NOT narration"))
    if ctx.engine_mode == "mlx":
        try:
            import mlx.core as mx  # type: ignore
            dev = str(mx.default_device())
            try:
                metal = bool(mx.metal.is_available())
            except Exception:
                metal = None
            out.append(_check("MLX device", "ok" if metal is not False else "fail", f"{dev} (Metal available: {metal})"))
        except Exception as exc:
            out.append(_check("MLX device", "fail", f"import failed: {exc}"))
    for label, mid in (("CustomVoice model", s.models.custom_voice_model), ("Base (clone) model", s.models.base_model),
                       ("VoiceDesign model", s.models.voice_design_model), ("ASR model", s.models.asr_model)):
        cached = hf_cached(mid)
        status = "ok" if cached else ("warn" if cached is False else "warn")
        detail = f"{mid}: {'cached locally' if cached else 'not downloaded yet (downloads on first use)' if s.models.allow_downloads else 'NOT cached and downloads are disabled'}"
        if cached is False and not s.models.allow_downloads:
            status = "fail"
        out.append(_check(label, status, detail))
    out.append(_check("Database", "ok" if ctx.db.schema_version() == LATEST_VERSION else "fail", f"{ctx.db.path} schema v{ctx.db.schema_version()} (latest {LATEST_VERSION})"))
    lib = validate_dir(str(ctx.library_root(s)), ctx.home, create=False, measure=False)
    free = f", {lib['free_bytes'] / 1024**3:.1f} GB free" if lib.get("free_bytes") else ""
    out.append(_check("Library storage", "ok" if lib["ok"] else "fail", f"{lib.get('resolved') or lib['input']}{free} {' '.join(lib['errors'])}"))
    out.append(_check("Network binding", "ok" if s.server.host in ("127.0.0.1", "localhost", "::1") else "warn",
                      f"{s.server.host}:{s.server.port}" + ("" if s.server.host in ("127.0.0.1", "localhost", "::1") else " - reachable from other machines!")))
    out.append(_check("Telemetry", "ok" if os.environ.get("HF_HUB_DISABLE_TELEMETRY") == "1" else "warn",
                      "HF_HUB_DISABLE_TELEMETRY=1" if os.environ.get("HF_HUB_DISABLE_TELEMETRY") == "1" else "set HF_HUB_DISABLE_TELEMETRY=1 (the launchers do this)"))
    out.append(_check("Studio version", "ok", __version__))
    out.extend(install_checks(ctx))
    return out


def install_checks(ctx) -> list[dict]:
    """Checks for the managed (launchd, versioned-release) installation. Legacy and development copies report that plainly."""
    from .lifecycle_cli import label_of, port_free
    from .providers import StudioStorageManager
    from .update.layout import Layout
    from .update.store import ReleaseStore
    from .update.trust import TrustStore

    lay = Layout.discover()
    out = []
    if not lay.managed:
        out.append(_check("Installation", "warn", "legacy or development copy (no managed release pointer); self-update is unavailable. Run install_audiobook_studio_mac.sh to migrate."))
    else:
        store = ReleaseStore(lay)
        cur = store.current()
        out.append(_check("Installation", "ok" if cur and store.has(cur) else "fail", f"active release {cur}, previous {store.previous() or '-'}, root {lay.root}"))
        py = lay.service_info().get("python")
        out.append(_check("Private Python", "ok" if py and Path(py).exists() else "fail", py or "service.json has no interpreter"))
        for t in ("ffmpeg", "ffprobe", "uv"):
            p = lay.tools_bin / t
            out.append(_check(f"App-owned {t}", "ok" if p.exists() else "warn", str(p) if p.exists() else "missing (re-run the installer to repair)"))
        try:
            from .platform_services import default_platform

            running = default_platform(lay.state).is_running(label_of(lay))
            out.append(_check("Background service", "ok" if running else "warn", f"{label_of(lay)} {'running' if running else 'not running (audiobook-studio start)'}"))
        except Exception as exc:
            out.append(_check("Background service", "warn", f"could not query the service manager ({type(exc).__name__})"))
        if (lay.update_dir / "REPAIR.txt").exists():
            out.append(_check("Update repair note", "fail", f"read {lay.update_dir / 'REPAIR.txt'}"))
    s = ctx.settings()
    out.append(_check("Update signing key", "ok" if TrustStore.load_pinned().configured else "warn",
                      "a release-signing key is pinned" if TrustStore.load_pinned().configured else "none pinned: update checks are disabled until the owner publishes one"))
    sm = StudioStorageManager(ctx)
    ok, why = sm.check_available(ctx.library_root(s))
    if not ok and "does not exist" in why:
        ok = True  # not created yet: it is made on the first import
    out.append(_check("Storage mount", "ok" if ok else "warn", str(ctx.library_root(s)) if ok else why))
    cache = sm.model_cache()
    out.append(_check("Model cache", "ok", f"{cache} ({sm.free_bytes(cache) / 1024**3:.0f} GB free; separate from the application releases)"))
    return out
