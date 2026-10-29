"""Command-line lifecycle: ``version``, ``start|stop|restart|status|logs``, ``service install|uninstall``, ``update``, ``migrate-legacy``.
These talk to the per-user service through the platform layer; they never need sudo."""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

from . import VERSION_LABEL, __version__
from .interfaces import ServiceSpec
from .update.layout import Layout

DEFAULT_LABEL = "com.audiobookstudio.app"


def _layout() -> Layout:
    return Layout.discover()


def _platform(layout: Layout):
    from .platform_services import default_platform

    return default_platform(layout.state)


def service_spec(layout: Layout, port: int, host: str = "127.0.0.1") -> ServiceSpec:
    """The LaunchAgent description, built only from the app-controlled manifest (``app/service.json``) and fixed paths."""
    info = layout.service_info()
    py = info.get("python") or sys.executable
    env = {"AUDIOBOOK_STUDIO_HOME": str(layout.state), "AUDIOBOOK_STUDIO_ROOT": str(layout.root), "AUDIOBOOK_STUDIO_LOGS": str(layout.logs),
           "HF_HOME": str(layout.models / "huggingface"), "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1", "TOKENIZERS_PARALLELISM": "false",
           "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(Path.home()),
           "PATH": os.pathsep.join([str(layout.tools_bin), "/usr/bin", "/bin", "/usr/sbin", "/sbin"])}
    env.update({k: v for k, v in (info.get("env") or {}).items() if isinstance(k, str) and isinstance(v, str)})
    return ServiceSpec(label=info.get("label") or DEFAULT_LABEL, program=[str(layout.root / "audiobook-studio"), "serve"],
                       working_dir=str(layout.current_link), env=env, stdout_log=str(layout.logs / "launchd.out.log"), port=port, host=host)


def label_of(layout: Layout) -> str:
    return layout.service_info().get("label") or DEFAULT_LABEL


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def find_free_port(preferred: int, host: str = "127.0.0.1", span: int = 40) -> int:
    for p in range(preferred, min(preferred + span, 65535)):
        if port_free(host, p):
            return p
    raise RuntimeError(f"No free port found in {preferred}-{preferred + span}.")


def cmd_version(a):
    from .context import Context

    layout = _layout()
    ctx = Context(migrate=False)
    build = ""
    try:
        build = (ctx.app_dir / "BUILD").read_text("utf-8").strip()
    except OSError:
        pass
    print(f"Audiobook Studio {VERSION_LABEL}" + (f" (build {build})" if build else ""))
    print(f"  channel       {ctx.settings().updates.channel}")
    print(f"  managed       {'yes' if layout.managed else 'no (legacy or development install)'}")
    print(f"  active release {layout.current_version() or '-'}   previous {layout.read_pointer().get('previous') or '-'}")
    print(f"  python        {sys.version.split()[0]} at {sys.executable}")
    print(f"  data          {ctx.home}")
    print(f"  database schema {ctx.db.schema_version()}")


def cmd_service(a):
    layout = _layout()
    ps = _platform(layout)
    label = label_of(layout)
    act = a.action
    if act == "install":
        from .context import Context

        ctx = Context(migrate=False)
        s = ctx.settings().server
        spec = service_spec(layout, s.port, s.host)
        layout.logs.mkdir(parents=True, exist_ok=True)
        ps.install_service(spec)
        print(f"Service '{spec.label}' installed for this user and started. Logs: {spec.stdout_log}")
    elif act == "uninstall":
        ps.uninstall_service(label)
        print("Service removed. Your books, settings, models and the application files were not touched.")
    elif act == "start":
        ps.start(label)
        print("Started.")
    elif act == "stop":
        ps.stop(label)
        print("Stopped. (In-flight work resumes from the last finished segment when you start again.)")
    elif act == "restart":
        ps.restart(label)
        print("Restarted.")
    elif act == "status":
        running = ps.is_running(label)
        print(f"service {label}: {'running' if running else 'not running'}")
        sys.exit(0 if running else 3)


def cmd_logs(a):
    layout = _layout()
    files = [layout.logs / n for n in ("launchd.out.log", "server.log", "worker.log", "updater.log")]
    for f in files:
        if not f.exists():
            continue
        lines = f.read_text("utf-8", errors="replace").splitlines()[-a.lines:]
        print(f"==> {f} <==")
        print("\n".join(lines))
    if a.follow:
        target = layout.logs / "server.log"
        pos = target.stat().st_size if target.exists() else 0
        try:
            while True:
                time.sleep(1)
                if target.exists() and target.stat().st_size > pos:
                    with open(target, "rb") as fh:
                        fh.seek(pos)
                        sys.stdout.write(fh.read().decode("utf-8", errors="replace"))
                        sys.stdout.flush()
                        pos = target.stat().st_size
        except KeyboardInterrupt:
            pass


def _manager():
    from .context import Context
    from .update.manager import ReleaseUpdateManager

    layout = _layout()
    ctx = Context(migrate=False)
    ps = _platform(layout)
    return ReleaseUpdateManager(ctx, layout, ps), ctx, layout, ps


def cmd_update(a):
    from .update.helper import Applier, ApplyError
    from .update.manager import UpdateError

    mgr, ctx, layout, ps = _manager()

    def show(st):
        print(f"installed {st['installed_version']} ({st['channel']} channel) - {st['state']}: {st['message']}")
        if st.get("latest"):
            print(f"  latest compatible: {st['latest']['version']} (published {st['latest']['published']}, {st['latest']['size'] // (1 << 20)} MB)")

    if a.status:
        show(mgr.status())
        return
    if a.rollback:
        prev = mgr.can_rollback()
        if not layout.managed:
            sys.exit(mgr.status()["not_managed_reason"])
        if not prev:
            sys.exit("There is no previous version kept on this Mac to roll back to.")
        if not a.yes:
            if not sys.stdin.isatty():
                sys.exit("Refusing to roll back without --yes when not run interactively.")
            if input(f"Roll back from {mgr.installed} to {prev}? [y/N] ").strip().lower() not in ("y", "yes"):
                sys.exit("Cancelled.")
        layout.update_dir.mkdir(parents=True, exist_ok=True)
        (layout.update_dir / "request.json").write_text(json.dumps({"version": prev, "mode": a.when, "requested_at": time.time(), "rollback": True}))
        try:
            ok = Applier(layout, ctx, ps).run(prev, a.when, rollback=True)
        except ApplyError as exc:
            sys.exit(str(exc))
        show(mgr.status())
        sys.exit(0 if ok else 1)
    st = mgr.check()
    show(st)
    if st["state"] in ("failed", "offline", "incompatible", "disabled"):
        sys.exit(1)
    if not a.install or st["state"] != "available":
        return
    if not layout.managed:
        sys.exit(st["not_managed_reason"])
    if not a.yes:
        if not sys.stdin.isatty():
            sys.exit("Refusing to install without --yes when not run interactively.")
        if input(f"Install {st['latest']['version']} now? [y/N] ").strip().lower() not in ("y", "yes"):
            sys.exit("Cancelled.")
    try:
        mgr.download(background=False)
    except UpdateError as exc:
        sys.exit(str(exc))
    st = mgr.status()
    show(st)
    if st["state"] != "staged":
        sys.exit(1)
    (layout.update_dir).mkdir(parents=True, exist_ok=True)
    (layout.update_dir / "request.json").write_text(json.dumps({"version": st["staged_version"], "mode": a.when, "requested_at": time.time()}))
    try:
        ok = Applier(layout, ctx, ps).run(st["staged_version"], a.when)
    except ApplyError as exc:
        sys.exit(str(exc))
    show(mgr.status())
    sys.exit(0 if ok else 1)


def _write_manifest(layout: Layout) -> None:
    from .util import atomic_write_text

    info = layout.service_info()
    info.update({"label": info.get("label") or DEFAULT_LABEL, "python": str(layout.app / "venv" / "bin" / "python"), "schema": 1})
    atomic_write_text(layout.service_manifest, json.dumps(info, indent=2) + chr(10))


def cmd_configure(a):
    """Used by the installer: write the app-controlled service manifest and (optionally) the storage/port settings. Idempotent."""
    from .config import save_settings
    from .context import Context
    from .util import atomic_write_text

    layout = _layout()
    layout.ensure()
    if a.manifest_only:
        _write_manifest(layout)
        print("configured: service manifest only")
        return
    ctx = Context(layout.state)          # creates state dirs, runs migrations, writes default settings on first run
    s = ctx.settings().model_copy(deep=True)
    changed = []
    if a.library:
        s.storage.library_root = str(Path(a.library).expanduser())
        changed.append("library")
    if a.model_cache:
        s.models.cache_dir = str(Path(a.model_cache).expanduser())
        changed.append("model cache")
    if a.port:
        s.server.port = a.port
        changed.append("port")
    if a.lan:
        s.server.allow_remote, s.server.https_enabled, s.server.host = True, True, "0.0.0.0"   # validated per assignment: allow first
        changed.append("LAN serving (sign-in required)")
    elif a.local_only:
        s.server.host, s.server.allow_remote, s.server.https_enabled = "127.0.0.1", False, False
        changed.append("this Mac only")
    if changed:
        ctx.save_settings(s)
    _write_manifest(layout)
    print("configured: " + (", ".join(changed) if changed else "service manifest only"))


def cmd_migrate_legacy(a):
    from .legacy import migrate_legacy

    layout = _layout()
    ps = _platform(layout)

    def install():
        from .context import Context

        s = Context(layout.state, migrate=False).settings().server
        layout.logs.mkdir(parents=True, exist_ok=True)
        ps.install_service(service_spec(layout, s.port, s.host))
        print("New service installed and started.")

    sys.exit(migrate_legacy(Path(a.old_home).expanduser(), layout, dry_run=a.dry_run, keep_network=a.keep_network, platform_service=ps,
                            install_service=install))


def register(sub) -> None:
    sub.add_parser("version", help="show version, build, channel and install layout").set_defaults(func=cmd_version)
    for name, helptext in (("start", "start the background service"), ("stop", "stop the background service"),
                           ("restart", "restart the background service"), ("status", "show whether the service is running")):
        sp = sub.add_parser(name, help=helptext)
        sp.set_defaults(func=cmd_service, action=name)
    sp = sub.add_parser("service", help="install or remove the per-user background service")
    sp.add_argument("action", choices=["install", "uninstall", "start", "stop", "restart", "status"])
    sp.set_defaults(func=cmd_service)
    sp = sub.add_parser("logs", help="show recent log lines")
    sp.add_argument("-n", "--lines", type=int, default=60)
    sp.add_argument("-f", "--follow", action="store_true")
    sp.set_defaults(func=cmd_logs)
    sp = sub.add_parser("update", help="check for / install a signed update")
    sp.add_argument("--check", action="store_true", help="check only (default)")
    sp.add_argument("--install", action="store_true", help="download, verify and install the update")
    sp.add_argument("--status", action="store_true", help="show the last known status without contacting the server")
    sp.add_argument("--rollback", action="store_true", help="switch back to the previous version that is kept on this Mac")
    sp.add_argument("--when", choices=["now", "after_job"], default="after_job", help="install at the next segment boundary or after the current job")
    sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    sp.set_defaults(func=cmd_update)
    sp = sub.add_parser("configure", help="(installer) write the service manifest and storage/port settings")
    sp.add_argument("--library")
    sp.add_argument("--model-cache")
    sp.add_argument("--port", type=int)
    sp.add_argument("--lan", action="store_true", help="serve the LAN (HTTP + HTTPS); sign-in stays required")
    sp.add_argument("--local-only", action="store_true", help="serve this Mac only (127.0.0.1)")
    sp.add_argument("--manifest-only", action="store_true", help="write only app/service.json (touches no data)")
    sp.set_defaults(func=cmd_configure)
    sp = sub.add_parser("migrate-legacy", help="adopt a Bash/Homebrew-era installation (backs it up first, never deletes it)")
    sp.add_argument("old_home", help="the old install directory, usually ~/audiobook-studio-claude")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--keep-network", action="store_true", help="keep serving the LAN if the old install did (default: loopback only)")
    sp.set_defaults(func=cmd_migrate_legacy)
