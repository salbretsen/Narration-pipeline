"""Private Python environments. A release whose locked dependency set differs from the active one names a ``runtime_id``
(the first 16 hex of the SHA-256 of its ``requirements.lock``, signed into the release index). The updater builds that
environment with the app-owned ``uv`` and ONLY prebuilt wheels (``--only-binary :all:``): if a dependency would need a
compiler, the install fails with an actionable message instead of reaching for Xcode/Command Line Tools or Homebrew.
The user's system Python is never used or modified."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from .layout import Layout

Runner = Callable[[list[str], dict], "subprocess.CompletedProcess[str]"]


class RuntimeError_(Exception):
    pass


def lock_id(lock_file: Path) -> str:
    return hashlib.sha256(Path(lock_file).read_bytes()).hexdigest()[:16]


def python_in(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def runtime_python(layout: Layout, runtime_id: str) -> Path:
    return python_in(layout.runtimes / runtime_id / "venv")


def uv_env(layout: Layout) -> dict[str, str]:
    app = layout.app
    return {"UV_PYTHON_INSTALL_DIR": str(app / "python"), "UV_CACHE_DIR": str(app / "uv-cache"), "UV_PYTHON_PREFERENCE": "only-managed",
            "UV_NO_CONFIG": "1", "UV_NO_PROGRESS": "1", "UV_PYTHON_DOWNLOADS": "manual", "PIP_REQUIRE_VIRTUALENV": "0"}


def _default_runner(argv: list[str], env: dict) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, env={**os.environ, **env}, timeout=3600)


def runtime_ready(layout: Layout, runtime_id: str) -> bool:
    return (layout.runtimes / runtime_id / ".complete").exists() and runtime_python(layout, runtime_id).exists()


def ensure_runtime(layout: Layout, release_dir: Path, runtime_id: str, python_version: str = "3.12", runner: Runner | None = None,
                   progress: Callable[[str], None] | None = None) -> Path:
    """Return the interpreter for ``runtime_id``, building the environment if it does not exist yet. It is built in place (a venv records
    its own absolute path) and only marked ``.complete`` after every step succeeded, so a half-built environment is never used."""
    if not runtime_id:
        raise RuntimeError_("The release does not name a runtime.")
    if runtime_ready(layout, runtime_id):
        return runtime_python(layout, runtime_id)
    lock = Path(release_dir) / "requirements.lock"
    if not lock.exists():
        raise RuntimeError_("The release needs a new Python environment but ships no requirements.lock.")
    if lock_id(lock) != runtime_id:
        raise RuntimeError_("requirements.lock does not match the runtime id signed into the release; refusing to build it.")
    uv = layout.tools_bin / "uv"
    if not uv.exists():
        raise RuntimeError_("The app-owned uv is missing; re-run the installer to repair the private runtime.")
    run = runner or _default_runner
    env = uv_env(layout)
    target = layout.runtimes / runtime_id
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True)
    venv = target / "venv"
    steps = [("Creating the private Python environment", [str(uv), "venv", "--python", python_version, str(venv)]),
             ("Installing locked dependencies (prebuilt wheels only)",
              [str(uv), "pip", "install", "--python", str(python_in(venv)), "--require-hashes", "--only-binary", ":all:", "-r", str(lock)])]
    for label, argv in steps:
        if progress:
            progress(label)
        r = run(argv, env)
        if r.returncode != 0:
            shutil.rmtree(target, ignore_errors=True)
            tail = (r.stderr or r.stdout or "").strip().splitlines()[-6:]
            raise RuntimeError_(f"{label} failed: " + " | ".join(tail) +
                                " (If a package has no prebuilt wheel this is a packaging defect; it is never fixed by installing developer tools.)")
    (target / ".complete").write_text("ok")
    return runtime_python(layout, runtime_id)
