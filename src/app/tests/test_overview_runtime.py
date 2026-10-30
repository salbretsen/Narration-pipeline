"""P2-B/C: the Overview and Library pages render `runtime` from each book summary; check the real server field reaches /api/books and
/api/overview. Formatting (js/runtime.js) is covered by tests/js/runtime_fmt.test.mjs via tests/test_runtime.py; real browser behaviour is
tools/e2e/polish_overview.py. MOCK engine only."""
from __future__ import annotations

from conftest import make_text

STATES = {"running", "queued", "finished", "idle"}
KEYS = {"state", "processing_s", "remaining_s", "finish_at", "starts_at", "total_est_s", "estimated", "basis"}


def test_runtime_field_reaches_books_and_overview(client):
    r = client.post("/api/imports", files={"file": ("A.txt", make_text(2, title="Rt").encode(), "text/plain")})
    voice = next(v for v in client.get("/api/voices").json()["voices"] if v["name"] == "Ryan")
    assert client.post(f"/api/imports/{r.json()['token']}/confirm", json={"title": "Rt", "voice_id": voice["id"], "queue": False}).status_code == 200
    for rt in (client.get("/api/books").json()["books"][0]["runtime"], client.get("/api/overview").json()["recent"][0]["runtime"]):
        assert set(rt) == KEYS and rt["state"] in STATES
