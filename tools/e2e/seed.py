import sys, time, httpx
sys.path.insert(0, sys.argv[1])  # tests dir for fixture builders
sys.path.insert(0, sys.argv[2])  # app dir
from conftest import make_epub, make_text, CHAPTERS
from studio.smoke import SMOKE_TEXT
B = "http://127.0.0.1:7871"
c = httpx.Client(base_url=B, headers={"X-Studio-Request": "1"}, timeout=60)
for _ in range(60):
    try:
        if c.get("/api/worker").status_code == 200: break
    except Exception: time.sleep(1)
def wait_idle(t=120):
    t0=time.time()
    while time.time()-t0<t:
        q=c.get("/api/queue").json()
        act=[j for j in q["jobs"] if j["status"] in ("queued","running")]
        util=[u for u in q["utility"] if u["status"] in ("queued","running")]
        if not act and not util: return
        time.sleep(1)
voices = {v["name"]: v for v in c.get("/api/voices").json()["voices"]}
def imp(name, data, title, author, voice, queue=True):
    p = c.post("/api/imports", files={"file": (name, data)}).json()
    b = c.post(f"/api/imports/{p['token']}/confirm", json={"title": title, "author": author, "voice_id": voices[voice]["id"], "queue": queue,
        "instructions": "Warm, unhurried literary narration."}).json()
    return b
b1 = imp("gull-point.txt", ("The Lighthouse at Gull Point\n\n" + SMOKE_TEXT).encode(), "The Lighthouse at Gull Point", "M. Hale", "Ryan")
b2 = imp("harbour-lights.epub", make_epub(CHAPTERS, title="Harbour Lights"), "Harbour Lights", "Ann Writer", "Aiden")
wait_idle()
b3 = imp("winter-orchard.txt", make_text(6, title="The Winter Orchard").encode(), "The Winter Orchard", "T. Marsh", "Ryan")
b4 = imp("quiet-river.txt", make_text(9, title="A Quiet River").encode(), "A Quiet River", "E. Lowe", "Aiden", queue=False)
# hold book 3 so the queue shows a paused job, then queue book 4 behind it
q = c.get("/api/queue").json()["jobs"]
j3 = next(j for j in q if j["book_id"] == b3["id"] and j["status"] in ("queued","running"))
c.post(f"/api/jobs/{j3['id']}/pause")
c.post("/api/queue", json={"book_id": b4["id"]})
wait_idle(30)
c.post(f"/api/books/{b1['id']}/report")
# approve one flagged segment with a note to show review state
fl = c.get(f"/api/books/{b1['id']}/segments?filter=flagged").json()["items"]
print("flagged in b1:", [(s["id"], s["sid"]) for s in fl])
print(c.get("/api/overview").json()["cards"])
print("books", b1["id"], b2["id"], b3["id"], b4["id"])
