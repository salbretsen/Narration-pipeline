"""PlatformService implementations.

MacPlatformService  per-user launchd LaunchAgent (gui/<uid> domain). No sudo, no daemon, no /Library, no shell profiles.
ProcessPlatformService  plain detached processes with pid files: used on a dev machine, in tests, and as the seed for Linux ports."""
from __future__ import annotations

import json
import os
import platform as _platform
import plistlib
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

from .interfaces import PlatformService, ServiceSpec

Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _run(argv: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def build_plist(spec: ServiceSpec) -> dict:
    """The LaunchAgent: starts at login, restarts only after a crash/failure (not after a clean stop), throttled to one restart per 10 s,
    one instance per label (launchd guarantees it), 45 s to shut down so an in-flight segment can finish."""
    return {
        "Label": spec.label,
        "ProgramArguments": list(spec.program),
        "WorkingDirectory": spec.working_dir,
        "EnvironmentVariables": dict(spec.env),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ExitTimeOut": 45,
        "ProcessType": "Interactive",
        "StandardOutPath": spec.stdout_log,
        "StandardErrorPath": spec.stdout_log,
    }


class MacPlatformService(PlatformService):
    name = "macos-launchd"

    def __init__(self, runner: Runner | None = None, agents_dir: Path | None = None, uid: int | None = None):
        self.run = runner or _run
        self.agents_dir = agents_dir or Path.home() / "Library" / "LaunchAgents"
        self.uid = os.getuid() if uid is None and hasattr(os, "getuid") else (uid or 501)

    def os_version(self) -> str:
        return _platform.mac_ver()[0] or "0"

    def architecture(self) -> str:
        m = _platform.machine()
        if sys.platform == "darwin" and m == "x86_64":
            r = self.run(["/usr/sbin/sysctl", "-n", "sysctl.proc_translated"])
            if r.stdout.strip() == "1":
                return "arm64-rosetta"
        return m

    def plist_path(self, label: str) -> Path:
        return self.agents_dir / f"{label}.plist"

    def _domain(self) -> str:
        return f"gui/{self.uid}" if self.run(["/bin/launchctl", "print", f"gui/{self.uid}"]).returncode == 0 else f"user/{self.uid}"

    def _loaded(self, label: str) -> bool:
        return self.run(["/bin/launchctl", "print", f"{self._domain()}/{label}"]).returncode == 0

    def install_service(self, spec: ServiceSpec) -> None:
        self.agents_dir.mkdir(parents=True, exist_ok=True)
        path = self.plist_path(spec.label)
        tmp = path.with_suffix(".plist.tmp")
        tmp.write_bytes(plistlib.dumps(build_plist(spec)))
        os.replace(tmp, path)
        if self._loaded(spec.label):
            self.run(["/bin/launchctl", "bootout", f"{self._domain()}/{spec.label}"])
        r = self.run(["/bin/launchctl", "bootstrap", self._domain(), str(path)])
        if r.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}")
        self.run(["/bin/launchctl", "enable", f"{self._domain()}/{spec.label}"])

    def uninstall_service(self, label: str) -> None:
        if self._loaded(label):
            self.run(["/bin/launchctl", "bootout", f"{self._domain()}/{label}"])
        try:
            self.plist_path(label).unlink()
        except FileNotFoundError:
            pass

    def _bootstrap(self, label: str) -> None:
        r = self.run(["/bin/launchctl", "bootstrap", self._domain(), str(self.plist_path(label))])
        if r.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}")

    def start(self, label: str) -> None:
        if not self._loaded(label):
            self._bootstrap(label)
        r = self.run(["/bin/launchctl", "kickstart", f"{self._domain()}/{label}"])
        if r.returncode != 0 and not self._loaded(label):  # the job went away between the check and the kickstart: load it again
            self._bootstrap(label)

    def stop(self, label: str, timeout: float = 60.0, poll: float = 0.25) -> None:
        # bootout stops the process and keeps KeepAlive from restarting it until start(). It can return while the job is still being torn
        # down (the server finishing a segment); a start() in that window saw it "loaded", only kickstarted a job that was going away, and
        # nothing ran until the update rolled back (the 0.8.1 -> 0.8.2 update on the Mac, 2026-10-29). So wait until it is really gone.
        if self._loaded(label):
            self.run(["/bin/launchctl", "bootout", f"{self._domain()}/{label}"])
            deadline = time.monotonic() + timeout
            while self._loaded(label) and time.monotonic() < deadline:
                time.sleep(poll)

    def restart(self, label: str) -> None:
        if self._loaded(label):
            self.run(["/bin/launchctl", "kickstart", "-k", f"{self._domain()}/{label}"])
        else:
            self.start(label)

    def is_running(self, label: str) -> bool:
        r = self.run(["/bin/launchctl", "print", f"{self._domain()}/{label}"])
        return r.returncode == 0 and "state = running" in r.stdout

    def launch_detached(self, label: str, argv: list[str], cwd: str, env: dict[str, str], log: str, plist_dir: Path) -> None:
        """Run a one-shot job (the updater) in its own launchd job, outside the service's process group, so restarting the
        service cannot kill it."""
        plist_dir.mkdir(parents=True, exist_ok=True)
        path = plist_dir / f"{label}.plist"
        path.write_bytes(plistlib.dumps({"Label": label, "ProgramArguments": argv, "WorkingDirectory": cwd, "EnvironmentVariables": env,
                                         "RunAtLoad": True, "KeepAlive": False, "StandardOutPath": log, "StandardErrorPath": log,
                                         "ProcessType": "Background", "AbandonProcessGroup": True}))
        if self._loaded(label):
            self.run(["/bin/launchctl", "bootout", f"{self._domain()}/{label}"])
        r = self.run(["/bin/launchctl", "bootstrap", self._domain(), str(path)])
        if r.returncode != 0:
            raise RuntimeError(f"could not start the updater job: {r.stderr.strip() or r.stdout.strip()}")


class ProcessPlatformService(PlatformService):
    """Detached processes tracked by pid files in ``state_dir``; the spec is stored next to the pid so start/restart work after install."""

    name = "process"

    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)   # created lazily: merely constructing the service must not touch the disk

    def os_version(self) -> str:
        return _platform.mac_ver()[0] or _platform.release()

    def architecture(self) -> str:
        return _platform.machine()

    def _spec_path(self, label: str) -> Path:
        return self.dir / f"{label}.spec.json"

    def _pid_path(self, label: str) -> Path:
        return self.dir / f"{label}.pid"

    def install_service(self, spec: ServiceSpec) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._spec_path(spec.label).write_text(json.dumps({"program": spec.program, "cwd": spec.working_dir, "env": spec.env, "log": spec.stdout_log}))

    def uninstall_service(self, label: str) -> None:
        self.stop(label)
        self._spec_path(label).unlink(missing_ok=True)

    def _pid(self, label: str) -> int | None:
        try:
            pid = int(self._pid_path(label).read_text().strip())
        except (OSError, ValueError):
            return None
        return pid if _alive(pid) else None

    def start(self, label: str) -> None:
        if self._pid(label):
            return
        spec = json.loads(self._spec_path(label).read_text())
        env = {**os.environ, **spec["env"]}
        Path(spec["log"]).parent.mkdir(parents=True, exist_ok=True)
        log = open(spec["log"], "ab")
        kwargs = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt" else {"start_new_session": True}
        p = subprocess.Popen(spec["program"], cwd=spec["cwd"], env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL, **kwargs)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._pid_path(label).write_text(str(p.pid))

    def stop(self, label: str, timeout: float = 30.0) -> None:
        pid = self._pid(label)
        if not pid:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline and _alive(pid):
            time.sleep(0.1)
        if _alive(pid):
            try:
                os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass
        self._pid_path(label).unlink(missing_ok=True)

    def restart(self, label: str) -> None:
        self.stop(label)
        self.start(label)

    def is_running(self, label: str) -> bool:
        return self._pid(label) is not None

    def launch_detached(self, label: str, argv: list[str], cwd: str, env: dict[str, str], log: str, plist_dir: Path) -> None:
        Path(log).parent.mkdir(parents=True, exist_ok=True)
        kwargs = {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)} if os.name == "nt" else {"start_new_session": True}
        subprocess.Popen(argv, cwd=cwd, env={**os.environ, **env}, stdout=open(log, "ab"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def default_platform(state_dir: Path) -> PlatformService:
    return MacPlatformService() if sys.platform == "darwin" else ProcessPlatformService(state_dir)
