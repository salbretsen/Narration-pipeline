"""Command line interface. The web UI is the primary interface; the CLI exists for automation,
diagnostics and headless use."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__


def _ctx():
    from .context import Context, setup_logging

    ctx = Context()
    return ctx


def _book_id(ctx, ref: str) -> int:
    from .db import row

    with ctx.db.session() as conn:
        b = row(conn, "SELECT id FROM books WHERE uid=? OR CAST(id AS TEXT)=?", (ref, ref))
    if not b:
        sys.exit(f"No book matches '{ref}' (use the id or uid from `studio list`).")
    return b["id"]


def cmd_serve(a):
    import uvicorn

    from .config import is_loopback
    from .context import Context

    ctx = Context()
    s = ctx.settings()
    host = a.host or s.server.host
    port = a.port or s.server.port
    if not is_loopback(host) and not (s.server.allow_remote and a.allow_remote):
        sys.exit("Refusing to bind to a non-loopback address. This studio has no authentication. "
                 "If you really need it, set server.allow_remote=true in Settings AND pass --allow-remote.")
    from .web.app import create_app

    app = create_app(ctx)
    app.state.port = port
    pidfile = ctx.run_dir / "server.pid"
    pidfile.write_text(str(os.getpid()))
    import atexit

    atexit.register(lambda: pidfile.unlink(missing_ok=True) if pidfile.exists() and pidfile.read_text().strip() == str(os.getpid()) else None)
    print(f"Audiobook Studio {__version__} on http://{'127.0.0.1' if host == '127.0.0.1' else host}:{port}  (engine: {ctx.engine_mode})", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False, timeout_graceful_shutdown=30)


def cmd_worker(a):
    from .context import Context, setup_logging
    from .worker import main

    ctx = Context()
    setup_logging(ctx, "worker")
    main(ctx)


def cmd_migrate(a):
    from .context import Context

    ctx = Context(migrate=False)
    before, after = ctx.db.migrate(ctx.backups_dir / "db")
    if not ctx.settings_path.exists():
        from .config import Settings, save_settings

        save_settings(ctx.settings_path, Settings())
    else:
        # rewrite with any new default keys, preserving every existing value
        ctx.save_settings(ctx.settings())
    from .services import ensure_builtin_voices

    ensure_builtin_voices(ctx)
    print(f"database schema v{before} -> v{after}; settings OK ({ctx.settings_path})")


def cmd_doctor(a):
    from .doctor import run_checks

    ctx = _ctx()
    checks = run_checks(ctx)
    if a.json:
        print(json.dumps(checks, indent=2))
    else:
        icons = {"ok": "OK  ", "warn": "WARN", "fail": "FAIL"}
        for c in checks:
            print(f"[{icons[c['status']]}] {c['name']}: {c['detail']}")
    if any(c["status"] == "fail" for c in checks):
        sys.exit(1)


def cmd_test(a):
    import subprocess

    tests = Path(__file__).resolve().parent.parent / "tests"
    env = dict(os.environ, STUDIO_ENGINE="mock", STUDIO_NO_WORKER="1")
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", "-q", str(tests), *a.pytest_args], env=env))


def cmd_import(a):
    from . import services as S

    ctx = _ctx()
    S.ensure_builtin_voices(ctx)
    data = Path(a.file).read_bytes()
    prev = S.create_import(ctx, Path(a.file).name, data)
    print(f"Preview: {prev['totals']['chapters']} chapters, {prev['totals']['words']} words, {prev['totals']['segments']} segments")
    for w in prev["warnings"]:
        print("  warning:", w)
    voices = S.list_voices(ctx)
    voice = next((v for v in voices if v["name"].lower() == (a.voice or "Ryan").lower()), None)
    if not voice:
        sys.exit(f"Voice '{a.voice}' not found. Available: {', '.join(v['name'] for v in voices)}")
    book = S.confirm_import(ctx, prev["token"], {"title": a.title or prev["title"], "author": a.author or prev.get("author") or "",
                                                 "voice_id": voice["id"], "instructions": a.instructions or "", "queue": a.queue})
    print(f"Imported book #{book['id']} ({book['uid']}): {book['title']} - {book['segments']} segments" + (" - queued" if a.queue else ""))


def cmd_list(a):
    from . import services as S

    ctx = _ctx()
    for b in S.list_books(ctx):
        print(f"#{b['id']:<4} {b['uid']:<14} {b['state']:<13} {b['produced']:>5}/{b['segments']:<5} flagged={b['flagged']:<4} {b['title']}")


def cmd_run(a):
    from . import services as S
    from .context import setup_logging
    from .worker import Worker

    ctx = _ctx()
    setup_logging(ctx, "worker")
    if a.book:
        S.enqueue_book(ctx, _book_id(ctx, a.book))
    w = Worker(ctx)
    if not w.acquire_lease():
        sys.exit("Another worker is running (probably the web studio). Use the web UI queue instead.")
    try:
        w.recover()
        n = w.run_until_idle()
        print(f"Processed {n} steps; queue is idle.")
    finally:
        w.release_lease()


def cmd_resume(a):
    from . import services as S

    ctx = _ctx()
    bid = _book_id(ctx, a.book)
    with ctx.db.session() as conn:
        from .db import row

        j = row(conn, "SELECT id, status FROM jobs WHERE book_id=? AND kind='book' ORDER BY id DESC LIMIT 1", (bid,))
    if j and j["status"] == "paused":
        S.job_action(ctx, j["id"], "resume")
    elif j and j["status"] == "failed":
        S.job_action(ctx, j["id"], "retry")
    else:
        S.enqueue_book(ctx, bid)
    print("Queued. The worker (web studio or `studio run`) will continue it.")


def cmd_issues(a):
    from . import services as S

    ctx = _ctx()
    bid = _book_id(ctx, a.book)
    res = S.list_segments(ctx, bid, None, "flagged", None, 200)
    print(f"{res['total']} flagged segment(s)")
    for s in res["items"]:
        print(f"  {s['sid']}  attempts={s['attempts_used']}  {s['flag_reason'] or ''}")


def cmd_approve(a):
    from . import services as S
    from .db import row

    ctx = _ctx()
    if a.release:
        bid = _book_id(ctx, a.target)
        if not a.by:
            sys.exit("--by NAME is required for a release approval")
        S.approve_release(ctx, bid, a.by, "full" if a.full_listen else "partial", True, a.note or "")
        print("Release approved.")
        return
    with ctx.db.session() as conn:
        s = row(conn, "SELECT id FROM segments WHERE sid=?", (a.target,))
    if not s:
        sys.exit("Segment not found")
    S.approve_segment(ctx, s["id"], a.note, actor="cli")
    print("Segment approved.")


def cmd_report(a):
    from .reports import write_report

    ctx = _ctx()
    r = write_report(ctx, _book_id(ctx, a.book))
    print(f"HTML: {r['html']}\nJSON: {r['json']}\nFindings: {r['findings']}")


def cmd_export(a):
    from .assembly import AssemblyBlocked, export_book

    ctx = _ctx()
    bid = _book_id(ctx, a.book)
    chapter_id = None
    if a.kind == "chapter_m4b":
        from .db import row

        with ctx.db.session() as conn:
            c = row(conn, "SELECT id FROM chapters WHERE book_id=? AND idx=?", (bid, a.chapter))
        if not c:
            sys.exit("Chapter not found")
        chapter_id = c["id"]
    try:
        r = export_book(ctx, bid, a.kind, chapter_id, progress=print)
    except AssemblyBlocked as exc:
        print(f"BLOCKED: {exc}")
        for d in exc.details[:30]:
            print("  ", d)
        sys.exit(2)
    print(f"{'DRAFT ' if r['draft'] else ''}export written: {r['path']} ({r['size_bytes'] / 1e6:.1f} MB)")


def cmd_smoke(a):
    from .smoke import run_smoke

    sys.exit(run_smoke(a))


def main(argv=None):
    p = argparse.ArgumentParser(prog="studio", description=f"Audiobook Studio {__version__} (web UI is the primary interface: ./start-studio)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("serve", help="run the web studio")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.add_argument("--allow-remote", action="store_true")
    sp.set_defaults(func=cmd_serve)
    sub.add_parser("worker", help="run the GPU worker in the foreground").set_defaults(func=cmd_worker)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    sp = sub.add_parser("doctor", help="check the environment")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_doctor)
    sp = sub.add_parser("test", help="run the automated (mock-engine) test suite")
    sp.add_argument("pytest_args", nargs="*")
    sp.set_defaults(func=cmd_test)
    sp = sub.add_parser("import", help="import a manuscript")
    sp.add_argument("file")
    sp.add_argument("--title")
    sp.add_argument("--author")
    sp.add_argument("--voice", default="Ryan")
    sp.add_argument("--instructions")
    sp.add_argument("--queue", action="store_true")
    sp.set_defaults(func=cmd_import)
    sub.add_parser("list", help="list books").set_defaults(func=cmd_list)
    sp = sub.add_parser("run", help="process the queue in the foreground until idle")
    sp.add_argument("--book", help="enqueue this book first")
    sp.set_defaults(func=cmd_run)
    sp = sub.add_parser("resume", help="resume/retry/enqueue a book")
    sp.add_argument("book")
    sp.set_defaults(func=cmd_resume)
    sp = sub.add_parser("issues", help="list flagged segments of a book")
    sp.add_argument("book")
    sp.set_defaults(func=cmd_issues)
    sp = sub.add_parser("approve", help="approve a segment (sid) or, with --release, a whole book")
    sp.add_argument("target")
    sp.add_argument("--release", action="store_true")
    sp.add_argument("--by")
    sp.add_argument("--full-listen", action="store_true")
    sp.add_argument("--note")
    sp.set_defaults(func=cmd_approve)
    sp = sub.add_parser("report", help="write the QA report (HTML + JSON)")
    sp.add_argument("book")
    sp.set_defaults(func=cmd_report)
    sp = sub.add_parser("export", help="export M4B / chapter M4B / MP3 zip")
    sp.add_argument("book")
    sp.add_argument("--kind", choices=["m4b", "chapter_m4b", "mp3_zip"], default="m4b")
    sp.add_argument("--chapter", type=int, default=1)
    sp.set_defaults(func=cmd_export)
    sp = sub.add_parser("smoke", help="real-hardware smoke test (Apple Silicon, MLX)")
    sp.add_argument("--ref-audio")
    sp.add_argument("--ref-text")
    sp.add_argument("--skip-design", action="store_true")
    sp.add_argument("--keep", action="store_true", help="keep the smoke-test books in the library")
    sp.set_defaults(func=cmd_smoke)
    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
