"""Manual rollback (A4) and post-install housekeeping (A6). Mock platform only; nothing here ran on a Mac."""
from __future__ import annotations

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from studio.update import housekeeping
from studio.update.store import ReleaseStore
from studio.update.manager import UpdateError
from test_update_flow import CUR, applier, env, stage  # noqa: F401  (env is a fixture)
from update_fixtures import write_tree


def _install_new(env):
    stage(env)
    (env.layout.update_dir / "request.json").write_text(json.dumps({"version": "0.5.1", "mode": "now"}))
    assert applier(env).run("0.5.1", "now")


def test_no_rollback_without_previous(env):
    assert env.mgr.can_rollback() is None
    assert env.mgr.status()["can_rollback"] is None
    with pytest.raises(UpdateError):
        env.mgr.request_rollback("now", CUR)


def test_manual_rollback_switches_back_and_keeps_data(env, ctx):
    _install_new(env)
    mgr = env.mgr
    assert mgr.can_rollback() == CUR
    with pytest.raises(UpdateError) as e:
        mgr.request_rollback("now", "9.9.9")                      # wrong confirmation
    assert e.value.status == 422
    mgr.request_rollback("now", CUR)
    req = json.loads((env.layout.update_dir / "request.json").read_text())
    assert req["rollback"] is True and req["version"] == CUR
    with pytest.raises(UpdateError):
        mgr.request_rollback("now", CUR)                          # already pending
    env.platform.log.clear()
    assert applier(env).run(CUR, "now", rollback=True)
    store = ReleaseStore(env.layout)
    assert store.current() == CUR and store.previous() == "0.5.1"
    assert env.platform.log == ["stop", f"start:{CUR}"]
    st = mgr.status()
    assert st["state"] == "succeeded" and "Rolled back to" in st["message"]
    assert mgr.status_file.read_history()[0]["event"] == "rollback"
    assert not (env.layout.update_dir / "request.json").exists()


def test_rollback_refused_when_previous_cannot_open_the_database(env, ctx):
    _install_new(env)
    rel = env.layout.release_dir(CUR) / "release.json"
    rel.write_text(json.dumps({"version": CUR, "schema_version": 1, "runtime_id": ""}))
    assert ctx.db.schema_version() > 1
    ok = applier(env).run(CUR, "now", rollback=True)
    assert not ok
    st = env.mgr.status()
    assert st["state"] == "failed" and "older database layout" in st["error"]
    assert ReleaseStore(env.layout).current() == "0.5.1"            # nothing changed


def test_failed_rollback_reverts_to_the_newer_release(env):
    _install_new(env)
    env.platform.broken.add(CUR)                                     # the old version will not start
    ok = applier(env).run(CUR, "now", rollback=True)
    assert not ok
    assert ReleaseStore(env.layout).current() == "0.5.1"
    st = env.mgr.status()
    assert st["state"] == "rolled_back" and "Rollback to" in st["message"]


def test_rollback_api_is_admin_only_and_needs_confirm(env, ctx):
    from studio.web.app import create_app
    from studio.web.auth import AuthStore

    _install_new(env)
    store = AuthStore(ctx.config_dir / "auth.json")
    store.set_user("boss", "correct-horse-battery", "admin")
    store.set_user("clerk", "correct-horse-battery2", "operator")
    app = create_app(ctx, start_worker=False, update_manager=env.mgr)
    with TestClient(app, base_url="http://127.0.0.1:7861", headers={"X-Studio-Request": "1"}) as c:
        c.post("/api/auth/login", json={"username": "clerk", "password": "correct-horse-battery2"})
        assert c.post("/api/update/rollback", json={"version": CUR, "confirm": True}).status_code == 403
        c.post("/api/auth/logout")
        c.post("/api/auth/login", json={"username": "boss", "password": "correct-horse-battery"})
        assert c.get("/api/update/status").json()["can_rollback"] == CUR
        assert c.post("/api/update/rollback", json={"version": CUR}).status_code == 422
        assert c.post("/api/update/rollback", json={"version": "0.0.1", "confirm": True}).status_code == 422
        r = c.post("/api/update/rollback", json={"version": CUR, "confirm": True})
        assert r.status_code == 200 and r.json()["state"] == "waiting"


# --------------------------------------------------------------------------- housekeeping


def _rt(layout, rid, age=7200):
    d = layout.runtimes / rid
    (d / "venv").mkdir(parents=True)
    (d / ".complete").write_text("ok")
    t = time.time() - age
    os.utime(d, (t, t))
    return d


def test_housekeeping_prunes_only_unreferenced_old_runtimes(env):
    lay = env.layout
    keep_id, old_id, fresh_id = "a" * 16, "b" * 16, "c" * 16
    top = write_tree(env.tmp / "rt", "0.5.0", runtime_id=keep_id)
    (lay.release_dir(CUR) / "release.json").write_text((top / "release.json").read_text())
    keep, old, fresh = _rt(lay, keep_id), _rt(lay, old_id), _rt(lay, fresh_id, age=10)
    (lay.runtimes / "notes.txt").write_text("not a runtime")
    gone = housekeeping.prune_runtimes(lay)
    assert gone == [old_id]
    assert keep.exists() and fresh.exists() and not old.exists() and (lay.runtimes / "notes.txt").exists()


def test_housekeeping_keeps_previous_releases_runtime(env):
    _install_new(env)
    lay = env.layout
    rid = "d" * 16
    (lay.release_dir(CUR) / "release.json").write_text(json.dumps({"version": CUR, "schema_version": 2, "runtime_id": rid}))
    d = _rt(lay, rid)
    housekeeping.run(lay)
    assert d.exists()                                                # the previous release still needs it


def test_install_cleans_downloads_but_never_user_data(env, ctx):
    lay = env.layout
    dl = lay.app / "downloads"
    dl.mkdir()
    (dl / "uv-0.1.tar.gz").write_bytes(b"x")
    (dl / "ffmpeg-1.zip").write_bytes(b"x")
    (dl / "ffmpeg-2.zip.part").write_bytes(b"x")
    (dl / "README.keep").write_text("someone put this here")
    (lay.tools_bin / "uv").write_text("tool")
    canary = ctx.library_root() / "books" / "keep.wav"
    canary.parent.mkdir(parents=True, exist_ok=True)
    canary.write_bytes(b"audio")
    _install_new(env)
    assert [p.name for p in dl.iterdir()] == ["README.keep"]
    assert (lay.tools_bin / "uv").exists() and canary.read_bytes() == b"audio"
    assert ReleaseStore(env.layout).installed() == [CUR, "0.5.1"]


def test_housekeeping_does_nothing_without_an_active_release(env):
    env.layout.pointer_file.unlink()
    d = _rt(env.layout, "e" * 16)
    assert housekeeping.prune_runtimes(env.layout) == [] and d.exists()
