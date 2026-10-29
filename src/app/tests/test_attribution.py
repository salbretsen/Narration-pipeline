"""Audit and review records are attributed to the signed-in user (C1)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import make_text
from studio import services as S
from studio.db import rows
from studio.web.app import create_app
from studio.web.auth import AuthStore

PW = "correct-horse-battery"


@pytest.fixture
def secured(ctx):
    store = AuthStore(ctx.config_dir / "auth.json")
    store.set_user("boss", PW, "admin")
    store.set_user("clerk", PW + "2", "operator")
    app = create_app(ctx, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:7861", headers={"X-Studio-Request": "1"}) as c:
        yield c


def _book(c):
    v = next(v for v in c.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
    p = c.post("/api/imports", files={"file": ("b.txt", make_text(1).encode(), "text/plain")}).json()
    return c.post(f"/api/imports/{p['token']}/confirm", json={"title": p["title"], "voice_id": v["id"], "queue": False}).json()


def _actors(ctx, table, action):
    with ctx.db.session() as conn:
        return {r["actor"] for r in rows(conn, f"SELECT actor FROM {table} WHERE action=?", (action,))}


def test_actions_attributed_to_signed_in_user(secured, ctx):
    secured.post("/api/auth/login", json={"username": "clerk", "password": PW + "2"})
    b = _book(secured)
    seg = secured.get(f"/api/books/{b['id']}/segments").json()["items"][0]
    assert secured.post(f"/api/segments/{seg['id']}/note", json={"note": "check pronunciation"}).status_code == 200
    assert secured.patch(f"/api/books/{b['id']}", json={"author": "Someone"}).status_code == 200
    assert secured.post(f"/api/books/{b['id']}/notes", json={"text": "Eleanor is the keeper"}).status_code == 200
    assert _actors(ctx, "review_actions", "note") == {"clerk"}
    assert _actors(ctx, "audit_log", "note") == {"clerk"}
    assert _actors(ctx, "audit_log", "metadata") == {"clerk"}
    assert _actors(ctx, "audit_log", "create") == {"clerk"}
    # a different user afterwards is recorded as that user
    secured.cookies.clear()
    secured.post("/api/auth/login", json={"username": "boss", "password": PW})
    secured.post(f"/api/segments/{seg['id']}/note", json={"note": "second look"})
    assert _actors(ctx, "review_actions", "note") == {"clerk", "boss"}
    d = secured.get(f"/api/segments/{seg['id']}").json()
    assert {r["actor"] for r in d["reviews"]} == {"clerk", "boss"}


def test_without_a_signed_in_user_the_actor_stays_operator(ctx, client):
    b = _book(client)
    seg = client.get(f"/api/books/{b['id']}/segments").json()["items"][0]
    client.post(f"/api/segments/{seg['id']}/note", json={"note": "local"})
    S.set_note(ctx, seg["id"], "direct service call")
    assert _actors(ctx, "review_actions", "note") == {"operator"}
