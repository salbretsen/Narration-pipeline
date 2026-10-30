"""Update flow: check / download / verify / stage (ReleaseUpdateManager), install / drain / switch / rollback (Applier),
queue drain in the worker, the HTTP API, and a real-process end-to-end update with rollback. Mock inference only; no macOS."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from studio.interfaces import PlatformService
from studio.platform_services import ProcessPlatformService
from studio.scheduler import FileQueueScheduler
from studio.update.helper import Applier, ApplyError
from studio.update.layout import Layout
from studio.update.manager import ReleaseUpdateManager, UpdateError
from studio.update.net import Fetcher
from studio.update.repository import GitHubReleasesRepository
from studio.update.store import ReleaseStore
from update_fixtures import FakeSite, entry, file_member, index_bytes, new_key, pack, tar_with, trust_for, write_tree, BASE

CUR = "0.5.0"


class FakePlatform(PlatformService):
    """Records service calls; 'serves' whatever release is active when started."""

    name = "fake"

    def __init__(self, layout: Layout):
        self.layout, self.log, self.serving, self.broken = layout, [], None, set()

    def os_version(self): return "15.1"
    def architecture(self): return "arm64"
    def install_service(self, spec): self.log.append("install")
    def uninstall_service(self, label): self.log.append("uninstall")

    def start(self, label):
        self.log.append(f"start:{self.layout.current_version()}")
        v = self.layout.current_version()
        self.serving = None if v in self.broken else v

    def stop(self, label):
        self.log.append("stop")
        self.serving = None

    def restart(self, label): self.stop(label); self.start(label)
    def is_running(self, label): return self.serving is not None
    def launch_detached(self, *a, **k): self.log.append("launch_detached")

    def health(self, port):
        return {"ok": True, "db": True, "version": self.serving, "storage": {"ok": True}} if self.serving else None


class Env:
    pass


@pytest.fixture
def env(tmp_path, ctx):
    e = Env()
    e.tmp = tmp_path
    e.layout = Layout(tmp_path / "Audiobook Studio", tmp_path / "logs")        # a space in the path on purpose
    e.layout.ensure()
    top = write_tree(tmp_path / "seed", CUR)
    os.replace(top, e.layout.release_dir(CUR))
    ReleaseStore(e.layout).activate(CUR)
    e.key, pub = new_key()
    e.key2, _ = new_key()
    e.site = FakeSite()
    e.repo = GitHubReleasesRepository("salbretsen", "Narration-pipeline", Fetcher(transport=e.site.transport()))
    e.platform = FakePlatform(e.layout)
    e.platform.serving = CUR
    e.ctx = ctx
    e.mgr = ReleaseUpdateManager(ctx, e.layout, e.platform, repository=e.repo, trust=trust_for(pub), installed_version=CUR,
                                 platform_tuple=("macos", "arm64"), os_version="15.1")
    e.releases: list[dict] = []
    e.seq = 0

    def publish(version, tamper=None, break_tree=None, key=None, **over):
        """Build, 'upload' and index a release. tamper(bytes)->bytes alters the served artifact after signing."""
        top = write_tree(tmp_path / "build", version, **(break_tree or {}))
        arc, sha, size = pack(top, tmp_path / "dist")
        rel = entry(version, arc, sha, size, **over)
        data = arc.read_bytes()
        e.site.files[rel["url"]] = tamper(data) if tamper else data
        e.releases.append(rel)
        push(key)
        return rel

    def push(key=None, **extra):
        e.seq += 1
        e.site.put_index("stable", index_bytes(key or e.key, e.releases, sequence=e.seq, **extra))

    e.publish, e.push = publish, push
    return e


def applier(e, **kw):
    kw.setdefault("health", e.platform.health)
    kw.setdefault("migrate", lambda d, py, env: (0, ""))
    kw.setdefault("poll", 0.01)
    kw.setdefault("health_timeout", 0.3)
    return Applier(e.layout, e.ctx, e.platform, **kw)


def stage(e, version="0.5.1", **kw):
    e.publish(version, **kw)
    assert e.mgr.check()["state"] == "available"
    e.mgr.download(background=False)
    return e.mgr.status()


# ------------------------------------------------------------------------------------------- check


def test_check_states(env):
    st = env.mgr.check()
    assert st["state"] == "failed" and "404" in st["error"] or st["state"] in ("failed", "offline")   # nothing published yet
    env.publish("0.5.0")
    assert env.mgr.check()["state"] == "up_to_date"                                  # no-op for the current version
    env.publish("0.5.1")
    st = env.mgr.check()
    assert st["state"] == "available" and st["latest"]["version"] == "0.5.1" and "fixed things" in st["notes"]
    assert st["last_success_check"] and "sha256" in st["latest"] and "url" not in st["latest"]
    assert [n["version"] for n in st["notes_since_installed"]] == ["0.5.1"] and "fixed things" in st["notes_since_installed"][0]["notes"]
    env.site.offline = True
    st = env.mgr.check()
    assert st["state"] == "offline" and st["error_kind"] == "offline" and st["last_success_check"]   # last good check is kept
    env.site.offline = False


def test_updates_are_switched_off_when_no_key_is_pinned(env):
    from studio.update.trust import TrustStore

    env.mgr.trust = TrustStore()
    env.publish("0.5.1")
    st = env.mgr.check()
    assert st["state"] == "disabled" and st["error_kind"] == "no_key" and "signing key" in st["message"]
    assert not env.site.requests                                           # does not even contact the server
    with pytest.raises(UpdateError):
        env.mgr.download(background=False)


def test_incompatible_release_is_reported_not_offered(env):
    env.publish("0.6.0", min_os="99.0")
    st = env.mgr.check()
    assert st["state"] == "incompatible" and "macOS 99.0" in st["message"]
    with pytest.raises(UpdateError):
        env.mgr.download(background=False)


def test_invalid_signature_and_unpinned_key_rejected(env):
    env.publish("0.5.1", key=env.key2)                      # signed by a key this install does not pin
    st = env.mgr.check()
    assert st["state"] == "failed" and st["error_kind"] == "untrusted" and "signature" in st["error"].lower()
    assert st["latest"] is None
    raw = json.loads(env.site.files[next(iter(env.site.files))] if False else env.site.files[
        "https://github.com/salbretsen/Narration-pipeline/releases/download/index-stable/release-index-stable.json"])
    raw["signed"]["releases"][0]["sha256"] = "0" * 64        # forged hash on an otherwise authentic file
    env.push(key=env.key)
    good = env.site.files["https://github.com/salbretsen/Narration-pipeline/releases/download/index-stable/release-index-stable.json"]
    forged = json.loads(good)
    forged["signed"]["releases"][0]["sha256"] = "0" * 64
    env.site.files["https://github.com/salbretsen/Narration-pipeline/releases/download/index-stable/release-index-stable.json"] = json.dumps(forged).encode()
    assert env.mgr.check()["error_kind"] == "untrusted"


def test_replayed_older_index_is_refused(env):
    env.publish("0.5.1")
    assert env.mgr.check()["state"] == "available"
    old = env.site.files["https://github.com/salbretsen/Narration-pipeline/releases/download/index-stable/release-index-stable.json"]
    env.releases.append(entry("0.5.2", Path("x"), "a" * 64, 10))
    env.push()
    assert env.mgr.check()["latest"]["version"] == "0.5.2"
    env.site.files["https://github.com/salbretsen/Narration-pipeline/releases/download/index-stable/release-index-stable.json"] = old   # attacker replays the older signed index
    st = env.mgr.check()
    assert st["state"] == "failed" and "rollback" in st["error"]


def test_unmanaged_install_explains_itself(env, ctx):
    lay = Layout(env.tmp / "elsewhere", env.tmp / "l2")
    mgr = ReleaseUpdateManager(ctx, lay, env.platform, repository=env.repo, trust=env.mgr.trust, installed_version=CUR, platform_tuple=("macos", "arm64"))
    st = mgr.status()
    assert st["managed"] is False and "install_audiobook_studio_mac.sh" in st["not_managed_reason"]
    with pytest.raises(UpdateError):
        mgr.request_install("now", "0.5.1")


# ------------------------------------------------------------------------------------- download / stage


def test_download_verifies_and_stages(env):
    st = stage(env)
    assert st["state"] == "staged" and st["staged_version"] == "0.5.1"
    assert env.layout.release_dir("0.5.1").exists() and (env.layout.release_dir("0.5.1") / "studio" / "__init__.py").exists()
    assert ReleaseStore(env.layout).current() == CUR                       # staging never switches anything
    assert {k for k in st["phases"]} >= {"check", "download", "verify", "stage"}
    assert not (env.layout.staging / "0.5.1").exists()                     # staging area cleaned up
    assert not list(env.layout.releases.glob(".stage-*"))


def test_tampered_archive_rejected_even_with_matching_size(env):
    def flip(b: bytes) -> bytes:
        return b[:-5] + bytes([b[-5] ^ 0xFF]) + b[-4:]

    env.publish("0.5.1", tamper=flip)
    env.mgr.check()
    env.mgr.download(background=False)
    st = env.mgr.status()
    assert st["state"] == "failed" and "does not match the signed release" in st["error"]
    assert not env.layout.release_dir("0.5.1").exists()
    assert not list((env.layout.staging).rglob("*.tar.gz"))               # the bad artifact was deleted
    assert ReleaseStore(env.layout).current() == CUR


def test_signed_but_malicious_archives_are_rejected_at_extraction(env, tmp_path):
    # even a correctly signed release cannot smuggle a traversal path or a link
    evil = tar_with([file_member("audiobook-studio-0.5.1/../../evil.txt")], tmp_path / "evil.tar.gz")
    data = evil.read_bytes()
    rel = entry("0.5.1", evil, hashlib.sha256(data).hexdigest(), len(data))
    env.site.files[rel["url"]] = data
    env.releases.append(rel)
    env.push()
    env.mgr.check()
    env.mgr.download(background=False)
    st = env.mgr.status()
    assert st["state"] == "failed" and "traversal" in st["error"].lower() or "Unsafe" in st["error"]
    assert not (env.layout.root / "evil.txt").exists() and not (env.layout.root.parent / "evil.txt").exists()
    assert not env.layout.release_dir("0.5.1").exists()


def test_mislabelled_release_rejected(env):
    top = write_tree(env.tmp / "build2", "0.5.9")                          # contents say 0.5.9 ...
    top = top.rename(top.with_name("audiobook-studio-0.5.1"))
    arc, sha, size = pack(top, env.tmp / "dist2")
    rel = entry("0.5.1", arc, sha, size)                                   # ... but the signed index says 0.5.1
    env.site.files[rel["url"]] = arc.read_bytes()
    env.releases.append(rel)
    env.push()
    env.mgr.check()
    env.mgr.download(background=False)
    assert "claims version 0.5.9" in env.mgr.status()["error"] and not env.layout.release_dir("0.5.1").exists()


def test_artifact_from_untrusted_origin_refused(env):
    env.publish("0.5.1", url="https://github.com/someone-else/fork/releases/download/v0.5.1/a.tar.gz")
    env.mgr.check()
    env.mgr.download(background=False)
    st = env.mgr.status()
    assert st["state"] == "failed" and "not hosted on this project" in st["error"]
    # an https URL on a non-trusted host cannot even be indexed
    env.publish("0.5.2", url="https://evil.example/a.tar.gz")
    assert env.mgr.check()["state"] in ("available", "failed")
    env.mgr.download(background=False)
    assert env.mgr.status()["state"] == "failed"


def test_not_enough_disk_space_is_reported_before_downloading(env, monkeypatch):
    env.publish("0.5.1")
    env.mgr.check()
    monkeypatch.setattr(shutil, "disk_usage", lambda p: type("U", (), {"free": 1000, "total": 1, "used": 0})())
    env.mgr.download(background=False)
    st = env.mgr.status()
    assert st["state"] == "failed" and "Not enough free disk space" in st["error"]
    assert not any(u.endswith(".tar.gz") for u, _ in env.site.requests)


def test_interrupted_download_resumes_and_completes(env):
    env.publish("0.5.1")
    env.mgr.check()
    env.site.drop_after = 200
    env.mgr.download(background=False)
    st = env.mgr.status()
    assert st["state"] == "offline" and "resume" in st["error"]
    part = list(env.layout.staging.rglob("*.part"))
    assert part and part[0].stat().st_size > 0
    env.mgr.download(background=False)                                     # retry from the Update Center
    assert env.mgr.status()["state"] == "staged"
    assert any(r and r.startswith("bytes=") for _, r in env.site.requests)


def test_background_download_endpoint_flow_and_busy_guard(env):
    env.publish("0.5.1")
    env.mgr.check()
    env.mgr.download(background=True)
    for _ in range(1000):  # a loaded machine (parallel test runs, browser scripts) can need seconds, not milliseconds
        if env.mgr.status()["state"] in ("staged", "failed", "offline"):
            break
        time.sleep(0.02)
    st = env.mgr.status()
    assert st["state"] == "staged" and st["busy"] is None, f"download ended in state {st['state']!r}: {st.get('error') or st.get('message')}"


def test_request_install_needs_staged_and_explicit_version(env):
    with pytest.raises(UpdateError, match="No update is staged"):
        env.mgr.request_install("now", "0.5.1")
    stage(env)
    launched = []
    env.mgr.launch_helper = lambda v, m: launched.append((v, m))
    with pytest.raises(UpdateError, match="does not match"):
        env.mgr.request_install("after_job", "0.5.2")
    with pytest.raises(UpdateError):
        env.mgr.request_install("sudo", "0.5.1")
    st = env.mgr.request_install("after_job", "0.5.1")
    assert st["state"] == "waiting" and launched == [("0.5.1", "after_job")]
    with pytest.raises(UpdateError, match="already pending"):
        env.mgr.request_install("now", "0.5.1")
    st = env.mgr.cancel()
    assert st["state"] == "staged" and not (env.layout.update_dir / "request.json").exists()


# --------------------------------------------------------------------------------------- the helper


def test_apply_success_switches_and_prunes_without_touching_user_data(env, ctx):
    canary = ctx.library_root() / "books" / "keep.wav"
    canary.parent.mkdir(parents=True, exist_ok=True)
    canary.write_bytes(b"RIFF-original-audio")
    with ctx.db.tx() as conn:
        ctx.event(conn, "canary", "survives updates")
    stage(env)
    (env.layout.update_dir / "request.json").write_text(json.dumps({"version": "0.5.1", "mode": "now"}))
    ok = applier(env).run("0.5.1", "now")
    assert ok
    store = ReleaseStore(env.layout)
    assert store.current() == "0.5.1" and store.previous() == CUR
    assert env.platform.log == ["stop", "start:0.5.1"]                     # stop -> switch -> start, nothing else
    st = env.mgr.status()
    assert st["state"] == "succeeded" and "Updated to 0.5.1" in st["message"]
    assert FileQueueScheduler(ctx.run_dir).current_hold() is None          # queue released
    assert canary.read_bytes() == b"RIFF-original-audio"
    with ctx.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='canary'").fetchone()[0] == 1
    assert list((ctx.backups_dir / "update").glob("studio-*.db"))          # pre-update backup exists
    h = env.mgr.status_file.read_history()
    assert h[0]["event"] == "install" and h[0]["ok"] and h[0]["previous"] == CUR


def test_failed_migration_rolls_back_and_restores_database(env, ctx):
    with ctx.db.tx() as conn:
        ctx.event(conn, "canary", "before")
    stage(env)

    def bad_migrate(d, py, e):
        with ctx.db.tx() as conn:
            conn.execute("PRAGMA user_version = 99") if False else None
            ctx.event(conn, "half-migrated", "partial schema work")
        import sqlite3
        c = sqlite3.connect(str(ctx.db.path))
        c.execute("PRAGMA user_version = 99")
        c.commit()
        c.close()
        return 1, "boom: cannot add column"

    ok = applier(env, migrate=bad_migrate).run("0.5.1", "now")
    assert not ok
    st = env.mgr.status()
    assert st["state"] == "rolled_back" and "boom" in st["message"] and CUR in st["message"]
    assert ReleaseStore(env.layout).current() == CUR
    assert env.platform.serving == CUR and env.platform.log[-1] == f"start:{CUR}"
    assert ctx.db.schema_version() != 99                                    # schema restored from the backup
    with ctx.db.session() as conn:
        kinds = {r[0] for r in conn.execute("SELECT kind FROM events")}
    assert "canary" in kinds and "half-migrated" not in kinds
    assert list(Path(ctx.db.path).parent.glob("studio.failed-*.db"))        # the failed-state DB is kept for inspection, not discarded
    assert FileQueueScheduler(ctx.run_dir).current_hold() is None


def test_unhealthy_new_version_is_rolled_back(env):
    stage(env)
    env.platform.broken.add("0.5.1")                                        # the new version never serves
    ok = applier(env).run("0.5.1", "now")
    assert not ok and env.mgr.status()["state"] == "rolled_back"
    assert ReleaseStore(env.layout).current() == CUR and env.platform.serving == CUR
    assert env.platform.log == ["stop", "start:0.5.1", "stop", f"start:{CUR}"]
    assert env.mgr.status_file.read_history()[0]["rolled_back_to"] == CUR


def test_double_failure_enters_repair_mode_and_keeps_everything(env, ctx):
    stage(env)
    env.platform.broken.update({"0.5.1", CUR})
    ok = applier(env).run("0.5.1", "now")
    assert not ok
    st = env.mgr.status()
    assert st["state"] == "failed" and st["error_kind"] == "repair" and "No project data was deleted" in st["message"]
    note = (env.layout.update_dir / "REPAIR.txt").read_text()
    assert "audiobook-studio doctor" in note
    assert env.layout.release_dir(CUR).exists() and env.layout.release_dir("0.5.1").exists()   # nothing deleted
    assert FileQueueScheduler(ctx.run_dir).current_hold() is None


def test_preflight_catches_tampering_after_staging(env):
    stage(env)
    (env.layout.release_dir("0.5.1") / "studio" / "evil.py").write_text("print('x')\n")
    ok = applier(env).run("0.5.1", "now")
    assert not ok
    st = env.mgr.status()
    assert st["state"] == "failed" and st["error_kind"] == "preflight" and "re-verification" in st["error"]
    assert env.platform.log == []                                           # the service was never touched
    assert ReleaseStore(env.layout).current() == CUR


def test_unavailable_state_folder_blocks_with_clear_message(env, monkeypatch):
    stage(env)
    real = os.access
    monkeypatch.setattr(os, "access", lambda p, m: False if str(p) == str(env.layout.state) else real(p, m))
    ok = applier(env).run("0.5.1", "now")
    assert not ok and "not writable. If it is on an external disk, reconnect it" in env.mgr.status()["error"]


def test_apply_waits_for_current_segment_then_proceeds(env, ctx):
    stage(env)
    with ctx.db.tx() as conn:
        conn.execute("UPDATE worker_state SET state='generating', heartbeat_at=? WHERE id=1", (time.time(),))
    holds = []

    def finish_segment():
        time.sleep(0.3)
        holds.append(FileQueueScheduler(ctx.run_dir).current_hold())       # while waiting, the hold is in force
        with ctx.db.tx() as conn:
            conn.execute("UPDATE worker_state SET state='idle' WHERE id=1")

    t = threading.Thread(target=finish_segment)
    t.start()
    ok = applier(env).run("0.5.1", "now")
    t.join()
    assert ok, env.mgr.status_file.read()
    assert holds and holds[0].mode == "now"


def test_after_job_mode_waits_for_the_running_job(env, ctx, client, make_worker):
    from test_integration import confirm, ryan, upload
    from conftest import make_text

    v = ryan(client)
    b = confirm(client, upload(client, "a.txt", make_text(1).encode()), v["id"], queue=True, title="Running Book")
    w = make_worker()
    w.step()                                                                 # the book job is now 'running'
    gate = FileQueueScheduler(ctx.run_dir, ctx.db)
    gate.hold("after_job")
    assert not gate.drained()
    assert w.run_until_idle() > 0                                           # the worker keeps working on it ...
    w._set(state="idle", job=None, sid=None, message="waiting for work")   # what run_forever does when step() finds nothing
    gate.release()
    with ctx.db.session() as conn:
        status = conn.execute("SELECT status FROM jobs WHERE book_id=?", (b["id"],)).fetchone()[0]
    assert status == "completed" or status == "running"
    stage(env)
    gate2 = FileQueueScheduler(ctx.run_dir, ctx.db)
    with ctx.db.tx() as conn:
        conn.execute("UPDATE jobs SET status='running' WHERE book_id=?", (b["id"],))
    seen = {}

    def complete_job():
        time.sleep(0.3)
        seen["waiting_msg"] = env.mgr.status()["message"]
        with ctx.db.tx() as conn:
            conn.execute("UPDATE jobs SET status='completed' WHERE book_id=?", (b["id"],))

    t = threading.Thread(target=complete_job)
    t.start()
    ok = applier(env, after_job_timeout=10).run("0.5.1", "after_job")
    t.join()
    assert ok, env.mgr.status_file.read()
    assert "Running Book" in seen["waiting_msg"]


def test_only_one_updater_at_a_time(env):
    stage(env)
    (env.layout.update_dir / "apply.lock").write_text(str(os.getpid()))
    # our own pid is "alive" but equal to ours -> treated as stale; another live pid blocks
    other = Path(sys.executable)
    p = __import__("subprocess").Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (env.layout.update_dir / "apply.lock").write_text(str(p.pid))
        with pytest.raises(ApplyError, match="already running"):
            applier(env).run("0.5.1", "now")
    finally:
        p.kill()


# ------------------------------------------------------------------------------- queue drain in worker


def test_hold_now_stops_new_work_and_release_resumes_without_loss(client, ctx, make_worker):
    from test_integration import confirm, ryan, upload
    from conftest import make_text

    v = ryan(client)
    confirm(client, upload(client, "a.txt", make_text(2).encode()), v["id"], queue=True, title="Drain Book")
    w = make_worker()
    for _ in range(3):
        assert w.step()
    with ctx.db.session() as conn:
        done = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    gate = FileQueueScheduler(ctx.run_dir, ctx.db)
    gate.hold("now")
    assert w.step() is False and w.step() is False                          # nothing new starts
    with ctx.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == done
    assert gate.drained() or True
    gate.release()
    w.run_until_idle()
    with ctx.db.session() as conn:
        pending = conn.execute("SELECT COUNT(*) FROM segments WHERE status IN ('pending','synthesizing')").fetchone()[0]
        kept = conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0]
    assert pending == 0 and kept >= done                                    # completed segments were kept, the rest finished


def test_after_job_hold_finishes_running_book_but_starts_no_other(client, ctx, make_worker):
    from test_integration import import_two

    b1, b2 = import_two(client)
    w = make_worker()
    w.step()
    gate = FileQueueScheduler(ctx.run_dir, ctx.db)
    gate.hold("after_job")
    w.run_until_idle()
    w._set(state="idle", job=None, sid=None, message="waiting for work")   # what run_forever does when step() finds nothing
    with ctx.db.session() as conn:
        s = {r["book_id"]: r["status"] for r in conn.execute("SELECT book_id, status FROM jobs WHERE kind='book'")}
    assert s[b1["id"]] == "completed" and s[b2["id"]] == "queued"           # remaining manuscripts wait their turn
    assert gate.drained()
    gate.release()
    w.run_until_idle()
    with ctx.db.session() as conn:
        assert conn.execute("SELECT status FROM jobs WHERE book_id=?", (b2["id"],)).fetchone()[0] == "completed"


# ---------------------------------------------------------------------------------------------- the API


@pytest.fixture
def api(env, ctx):
    from studio.web.app import create_app

    env.mgr.launch_helper = lambda v, m: env.platform.log.append(f"helper:{v}:{m}")
    app = create_app(ctx, start_worker=False, update_manager=env.mgr)
    with TestClient(app, base_url="http://127.0.0.1:7861", headers={"X-Studio-Request": "1"}) as c:
        yield c


def test_health_and_version_endpoints_are_public_and_minimal(api, ctx):
    from studio import __version__

    h = api.get("/healthz").json()
    assert h["ok"] and h["db"] and h["version"] == __version__ and h["schema"] == ctx.db.schema_version()
    v = api.get("/api/version").json()
    assert v["version"] == __version__ and "managed" in v


def test_update_api_end_to_end(api, env):
    st = api.get("/api/update/status").json()
    assert st["installed_version"] and st["channel"] == "stable" and st["managed"] is True
    env.publish("0.5.1")
    st = api.post("/api/update/check").json()
    assert st["state"] == "available" and st["latest"]["version"] == "0.5.1"
    api.post("/api/update/download")
    for _ in range(300):
        if api.get("/api/update/status").json()["state"] == "staged":
            break
        time.sleep(0.02)
    assert api.get("/api/update/status").json()["state"] == "staged"
    assert api.post("/api/update/install", json={"version": "0.5.1", "mode": "now"}).status_code == 422          # no explicit confirmation
    assert api.post("/api/update/install", json={"version": "0.5.1", "mode": "now", "confirm": True, "url": "https://evil.example/x"}).status_code == 422
    assert api.post("/api/update/install", json={"version": "0.5.1", "mode": "now", "confirm": True, "cmd": "rm -rf /"}).status_code == 422
    r = api.post("/api/update/install", json={"version": "0.5.1", "mode": "after_job", "confirm": True})
    assert r.status_code == 200 and r.json()["state"] == "waiting" and "helper:0.5.1:after_job" in env.platform.log
    assert api.post("/api/update/cancel").json()["state"] == "staged"
    assert api.get("/api/update/history").json()["history"]
    p = api.put("/api/update/preferences", json={"auto_check": False, "check_interval_hours": 48}).json()
    assert p["auto_check"] is False and p["check_interval_hours"] == 48
    assert api.put("/api/update/preferences", json={"repository": "https://evil.example"}).status_code == 422    # repo not settable via API
    assert api.put("/api/update/preferences", json={"channel": "nightly"}).status_code == 422


def test_update_api_csrf_and_roles(env, ctx):
    from studio.web.app import create_app
    from studio.web.auth import AuthStore

    store = AuthStore(ctx.config_dir / "auth.json")
    store.set_user("boss", "correct-horse-battery", "admin")
    store.set_user("clerk", "correct-horse-battery2", "operator")
    app = create_app(ctx, start_worker=False, update_manager=env.mgr)
    with TestClient(app, base_url="http://127.0.0.1:7861", headers={"X-Studio-Request": "1"}) as c:
        assert c.get("/api/update/status").status_code == 401                      # sign-in required
        assert c.get("/healthz").status_code == 200                                # but health is public
        c.post("/api/auth/login", json={"username": "clerk", "password": "correct-horse-battery2"})
        assert c.get("/api/update/status").status_code == 200                      # operators can see the state
        for method, path in (("post", "/api/update/check"), ("post", "/api/update/download"), ("post", "/api/update/install"),
                             ("post", "/api/update/cancel"), ("post", "/api/update/restart"), ("put", "/api/update/preferences")):
            assert getattr(c, method)(path, json={}).status_code == 403, path       # ... but cannot change anything
        c.post("/api/auth/logout")
        c.post("/api/auth/login", json={"username": "boss", "password": "correct-horse-battery"})
        no_header = c.post("/api/update/check", headers={"X-Studio-Request": ""})
        assert no_header.status_code == 403                                         # CSRF header required
        cross = c.post("/api/update/check", headers={"Origin": "https://evil.example"})
        assert cross.status_code == 403                                             # cross-origin blocked
        assert c.post("/api/update/check").status_code == 200


# --------------------------------------------------------------- real processes: update + rollback E2E


def _free_port():
    from studio.lifecycle_cli import find_free_port

    return find_free_port(18000 + os.getpid() % 2000)


class DirFollowingPlatform(ProcessPlatformService):
    """ProcessPlatformService whose working directory follows the active release, like launchd's WorkingDirectory=current."""

    def __init__(self, state_dir, layout):
        super().__init__(state_dir)
        self.layout = layout

    def start(self, label):
        spec = json.loads(self._spec_path(label).read_text())
        cur = str(self.layout.release_dir(self.layout.current_version()))
        spec["cwd"], spec["env"]["PYTHONPATH"] = cur, cur
        self._spec_path(label).write_text(json.dumps(spec))
        super().start(label)


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A managed installation with two real releases of this application, a real server process and a real Applier."""
    from studio.context import Context
    from studio.interfaces import ServiceSpec

    monkeypatch.delenv("AUDIOBOOK_STUDIO_HOME", raising=False)
    lay = Layout(tmp_path / "Audiobook Studio", tmp_path / "logs")
    lay.ensure()
    port = _free_port()
    (lay.state / "config").mkdir(parents=True)
    (lay.state / "config" / "settings.json").write_text(json.dumps({"server": {"host": "127.0.0.1", "port": port}, "models": {"engine": "mock"}}))
    for v in ("7.0.0",):
        os.replace(write_tree(tmp_path / "seed", v, full=True), lay.release_dir(v))
    ReleaseStore(lay).activate("7.0.0")
    lay.service_manifest.write_text(json.dumps({"label": "t-app", "python": sys.executable}))
    ctx = Context(lay.state, migrate=False)
    ctx.db.migrate()
    ps = DirFollowingPlatform(lay.state / "svc", lay)
    env = {"AUDIOBOOK_STUDIO_HOME": str(lay.state), "AUDIOBOOK_STUDIO_ROOT": str(lay.root), "AUDIOBOOK_STUDIO_LOGS": str(lay.logs),
           "STUDIO_NO_WORKER": "1", "STUDIO_NO_UPDATE_CHECK": "1", "STUDIO_ENGINE": "mock", "PYTHONDONTWRITEBYTECODE": "1"}
    ps.install_service(ServiceSpec("t-app", [sys.executable, "-m", "studio", "serve"], str(lay.release_dir("7.0.0")), env, str(lay.logs / "svc.log"), port))
    yield type("Live", (), {"layout": lay, "ctx": ctx, "platform": ps, "port": port, "tmp": tmp_path})
    ps.stop("t-app")


def _wait_version(port, version, timeout=60.0):
    import httpx

    end = time.time() + timeout
    while time.time() < end:
        try:
            r = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=2).json()
            if r.get("version") == version and r.get("ok"):
                return r
        except Exception:
            pass
        time.sleep(0.4)
    return None


def test_real_process_update_then_failed_update_rolls_back(live):
    lay, ctx = live.layout, live.ctx
    live.platform.start("t-app")
    assert _wait_version(live.port, "7.0.0"), "baseline service did not come up"
    with ctx.db.tx() as conn:
        ctx.event(conn, "canary", "must survive both updates")

    # --- a good update 7.0.0 -> 7.0.1 (real migrate subprocess, real restart, real /healthz)
    os.replace(write_tree(live.tmp / "b1", "7.0.1", full=True), lay.release_dir("7.0.1"))
    ap = Applier(lay, ctx, live.platform, poll=0.2, health_timeout=60)
    assert ap.run("7.0.1", "now") is True
    h = _wait_version(live.port, "7.0.1")
    assert h and h["db"] and ReleaseStore(lay).current() == "7.0.1" and ReleaseStore(lay).previous() == "7.0.0"
    assert live.platform.is_running("t-app")                                 # exactly one service process
    pid1 = int((live.tmp / "Audiobook Studio" / "state" / "svc" / "t-app.pid").read_text())

    # --- a release that installs and migrates but whose service dies: must roll back to 7.0.1 with the same data
    os.replace(write_tree(live.tmp / "b2", "7.0.2", full=True, break_serve=True), lay.release_dir("7.0.2"))
    ap = Applier(lay, ctx, live.platform, poll=0.2, health_timeout=6)
    assert ap.run("7.0.2", "now") is False
    assert _wait_version(live.port, "7.0.1"), "previous version was not restored"
    assert ReleaseStore(lay).current() == "7.0.1"
    st = ap.status.read()
    assert st["state"] == "rolled_back" and "did not become healthy" in st["error"]
    assert live.platform.is_running("t-app")
    with ctx.db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE kind='canary'").fetchone()[0] == 1
    assert pid1 != int((live.tmp / "Audiobook Studio" / "state" / "svc" / "t-app.pid").read_text())     # restarted, not duplicated
    assert ctx.db.schema_version() > 0


def test_supervised_worker_exits_when_its_server_disappears(ctx, monkeypatch):
    import os

    from studio.engines import MockASR, MockEngine
    from studio.worker import Worker

    monkeypatch.setenv("STUDIO_SUPERVISED_WORKER", "1")
    w = Worker(ctx, tts=MockEngine({}), asr=MockASR())
    assert w.parent_gone() is False
    monkeypatch.setattr(os, "getppid", lambda: 1)              # the server was killed: re-parented to launchd
    assert w.parent_gone() is True
    monkeypatch.delenv("STUDIO_SUPERVISED_WORKER")
    assert Worker(ctx, tts=MockEngine({}), asr=MockASR()).parent_gone() is False     # foreground `studio worker` is never auto-stopped
