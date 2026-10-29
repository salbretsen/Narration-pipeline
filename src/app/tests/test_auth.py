"""Authentication, roles, LAN host handling and login rate limiting."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from studio.web.app import create_app
from studio.web.auth import AuthStore, is_admin_only

PW = "correct-horse-battery"


@pytest.fixture
def secured(ctx):
    store = AuthStore(ctx.config_dir / "auth.json")
    store.set_user("boss", PW, "admin")
    store.set_user("clerk", PW + "2", "operator")
    app = create_app(ctx, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:7861", headers={"X-Studio-Request": "1"}) as c:
        yield c


def login(c, user="boss", pw=PW):
    return c.post("/api/auth/login", json={"username": user, "password": pw})


def test_unauthenticated_requests_blocked_but_shell_public(secured):
    assert secured.get("/").status_code == 200 and secured.get("/static/app.css").status_code == 200
    st = secured.get("/api/auth/status").json()
    assert st["auth_required"] is True and st["user"] is None
    for path in ("/api/overview", "/api/books", "/api/settings", "/api/events?once=1", "/media/attempt/1", "/api/queue"):
        assert secured.get(path).status_code == 401, path
    assert secured.post("/api/queue", json={"book_id": 1}).status_code == 401
    assert secured.put("/api/settings", json={}).status_code == 401


def test_login_session_cookie_and_logout(secured):
    assert login(secured, pw="wrong-password-here").status_code == 401
    assert login(secured, user="nobody").status_code == 401
    r = login(secured)
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert secured.get("/api/overview").status_code == 200
    assert secured.get("/api/auth/status").json()["user"] == {"name": "boss", "role": "admin"}
    secured.post("/api/auth/logout")
    secured.cookies.clear()
    assert secured.get("/api/overview").status_code == 401


def test_forged_and_tampered_tokens_rejected(secured):
    login(secured)
    good = secured.cookies.get("studio_session")
    secured.cookies.clear()
    for bad in (good[:-3] + "abc", "x." + good.split(".")[1], "garbage", good.split(".")[0] + "."):
        secured.cookies.set("studio_session", bad)
        assert secured.get("/api/overview").status_code == 401


def test_operator_cannot_use_admin_actions(secured):
    login(secured, "clerk", PW + "2")
    assert secured.get("/api/overview").status_code == 200
    assert secured.put("/api/settings", json={"qa": {"max_attempts": 2}}).status_code == 403
    assert secured.get("/api/storage").status_code == 403
    assert secured.post("/api/storage/validate", json={"path": "/tmp"}).status_code == 403
    assert secured.post("/api/storage/choose-folder").status_code == 403
    assert secured.post("/api/worker/restart").status_code == 403
    assert secured.post("/api/books/1/move", json={"new_root": "/tmp/x"}).status_code == 403
    assert secured.post("/api/auth/users", json={"username": "evil", "password": "x" * 12, "confirm": "x" * 12, "role": "admin"}).status_code == 403
    secured.cookies.clear()
    login(secured)
    assert secured.put("/api/settings", json={"qa": {"max_attempts": 2}}).status_code == 200
    assert secured.get("/api/storage").status_code == 200
    assert secured.post("/api/auth/users", json={"username": "newbie", "password": "twelve-chars-ok", "confirm": "twelve-chars-ok", "role": "operator"}).status_code == 200
    assert secured.post("/api/auth/users", json={"username": "typo", "password": "twelve-chars-ok", "confirm": "twelve-chars-ko", "role": "operator"}).status_code == 422


def test_password_change_invalidates_old_sessions(secured, ctx):
    login(secured)
    old = secured.cookies.get("studio_session")
    r = secured.post("/api/auth/password", json={"current": PW, "new": "a-brand-new-password", "confirm": "a-brand-new-passw0rd"})
    assert r.status_code == 422 and "do not match" in r.json()["error"]
    assert login(secured).status_code == 200  # a mistyped confirmation changed nothing: the old password still works
    secured.cookies.set("studio_session", old)
    r = secured.post("/api/auth/password", json={"current": PW, "new": "a-brand-new-password", "confirm": "a-brand-new-password"})
    assert r.status_code == 200
    secured.cookies.clear()
    secured.cookies.set("studio_session", old)
    assert secured.get("/api/overview").status_code == 401
    assert login(secured, pw="a-brand-new-password").status_code == 200
    assert secured.post("/api/auth/password", json={"current": "nope", "new": "another-long-password", "confirm": "another-long-password"}).status_code == 403


def test_login_rate_limit(secured):
    codes = [login(secured, pw=f"bad-password-{i}").status_code for i in range(8)]
    assert codes[:6] == [401] * 6 and codes[6] == 429
    assert login(secured).status_code == 429  # even the right password waits out the lockout


def test_host_header_allowlist_and_lan_names(secured, ctx):
    login(secured)
    assert secured.get("/api/overview", headers={"Host": "evil.example:7861"}).status_code == 421
    assert secured.get("/api/overview", headers={"Host": "127.0.0.1:9999"}).status_code == 421  # wrong port
    assert secured.get("/api/overview", headers={"Host": "localhost:7861"}).status_code == 200
    s = ctx.settings().model_copy(deep=True)
    s.server.allowed_hosts = ["studio.example.lan"]
    ctx.save_settings(s)
    assert secured.get("/api/overview", headers={"Host": "studio.example.lan:7861"}).status_code == 200


def test_csrf_still_enforced_when_authenticated(secured):
    login(secured)
    assert secured.post("/api/queue", json={"book_id": 1}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert secured.post("/api/queue", json={"book_id": 1}, headers={"X-Studio-Request": ""}).status_code == 403
    assert "access-control-allow-origin" not in secured.get("/api/overview", headers={"Origin": "http://evil.example"}).headers


def test_network_bind_requires_a_user_account(ctx):
    s = ctx.settings().model_copy(deep=True)
    s.server.allow_remote = True
    s.server.host = "0.0.0.0"
    ctx.save_settings(s)
    app = create_app(ctx, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:7861", headers={"X-Studio-Request": "1"}) as c:
        assert c.get("/api/overview").status_code == 503  # no users: refuse to serve on a network bind
        AuthStore(ctx.config_dir / "auth.json").set_user("boss", PW, "admin")
        assert c.get("/api/overview").status_code == 401


def test_temporary_password_forces_change(ctx):
    AuthStore(ctx.config_dir / "auth.json").set_user("admin", "admin", "admin", temporary=True)
    app = create_app(ctx, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:7861", headers={"X-Studio-Request": "1"}) as c:
        assert login(c, "admin", "admin").json()["user"]["must_change"] is True
        r = c.get("/api/overview")
        assert r.status_code == 403 and r.json()["must_change_password"] is True
        assert c.get("/api/auth/status").json()["user"]["must_change"] is True and c.get("/").status_code == 200
        assert c.post("/api/auth/password", json={"new": "admin", "confirm": "admin"}).status_code == 422   # too short
        assert c.post("/api/auth/password", json={"new": "a-fresh-password", "confirm": "a-fresh-password"}).status_code == 200
        assert c.get("/api/overview").status_code == 200 and "must_change" not in c.get("/api/auth/status").json()["user"]
        c.cookies.clear()
        assert login(c, "admin", "admin").status_code == 401 and login(c, "admin", "a-fresh-password").status_code == 200
        # outside the temporary state the current password is still required
        assert c.post("/api/auth/password", json={"new": "yet-another-pass", "confirm": "yet-another-pass"}).status_code == 403


def test_auth_store_rules(ctx):
    st = AuthStore(ctx.config_dir / "auth.json")
    with pytest.raises(ValueError):
        st.set_user("a", PW, "admin")
    with pytest.raises(ValueError):
        st.set_user("ok-name", "short", "admin")
    st.set_user("only", PW, "admin")
    with pytest.raises(ValueError):
        st.set_user("only", PW, "operator")  # cannot demote the last admin
    st.set_user("second", PW, "admin")
    tok = st.make_token({"name": "second", "role": "admin"})
    st.remove_user("second")
    assert st.parse_token(tok) is None  # removing a user ends their sessions
    with pytest.raises(ValueError):
        st.remove_user("only")  # last administrator
    with pytest.raises(ValueError):
        st.remove_user("ghost")
    assert (ctx.config_dir / "auth.json").exists()
    assert is_admin_only("PUT", "/api/settings") and not is_admin_only("GET", "/api/settings")


def test_folder_browser_admin_only_lists_dirs_and_creates_folders(secured, tmp_path):
    (tmp_path / "Library").mkdir()
    (tmp_path / "Library" / "inner").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "file.txt").write_text("x")
    # operators cannot browse the host's filesystem
    login(secured, "clerk", PW + "2")
    assert secured.get("/api/storage/browse", params={"path": str(tmp_path)}).status_code == 403
    assert secured.post("/api/storage/mkdir", json={"parent": str(tmp_path), "name": "nope"}).status_code == 403
    secured.cookies.clear()
    assert secured.get("/api/storage/browse").status_code == 401
    login(secured)
    d = secured.get("/api/storage/browse", params={"path": str(tmp_path)}).json()
    names = [e["name"] for e in d["entries"]]
    assert [n for n in names if n != "studio-home"] == ["Library"]  # folders only: no files, no hidden entries
    assert d["writable"] and d["free_bytes"] > 0 and d["parent"]
    sub = secured.get("/api/storage/browse", params={"path": str(tmp_path / "Library")}).json()
    assert [e["name"] for e in sub["entries"]] == ["inner"]
    assert any(e["name"] == ".hidden" for e in secured.get("/api/storage/browse", params={"path": str(tmp_path), "hidden": "true"}).json()["entries"])
    # errors are clear, not 500s
    assert secured.get("/api/storage/browse", params={"path": "relative/dir"}).status_code == 422
    assert secured.get("/api/storage/browse", params={"path": str(tmp_path / "missing")}).status_code == 422
    assert secured.get("/api/storage/browse", params={"path": str(tmp_path / "file.txt")}).status_code == 422
    assert secured.get("/api/storage/browse", params={"path": "/Volumes/NotMountedXYZ"}).status_code == 422
    # create a folder, then use it as the library root over the "network" session
    r = secured.post("/api/storage/mkdir", json={"parent": str(tmp_path), "name": "New Library"})
    assert r.status_code == 200 and (tmp_path / "New Library").is_dir()
    assert secured.post("/api/storage/mkdir", json={"parent": str(tmp_path), "name": "New Library"}).status_code == 422  # exists
    for bad in ("../escape", "a/b", ".hidden", "x" * 90, ""):
        assert secured.post("/api/storage/mkdir", json={"parent": str(tmp_path), "name": bad}).status_code == 422, bad
    r = secured.put("/api/settings", json={"storage": {"library_root": r.json()["path"]}})
    assert r.status_code == 200 and r.json()["library_root"].endswith("New Library")
    # the native Finder picker is refused for remote clients (it would open on the Mac's own screen)
    assert secured.post("/api/storage/choose-folder", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 403
