"""Real-Mac drain test: queue a real narration job (MLX), request an update mid-job, and prove the queue is drained at a segment boundary,
the service is replaced, the job resumes by itself, and completed segments are NOT regenerated.

    <venv>/bin/python mac_drain_test.py <install-root> <dir-with-signed-index+archive> <admin-password-file>
"""
import sys
import time
from pathlib import Path

import httpx

from studio.context import Context
from studio.interfaces import ReleaseRepository
from studio.platform_services import MacPlatformService
from studio.smoke import SMOKE_TEXT
from studio.update.launch import make_launcher
from studio.update.layout import Layout
from studio.update.manager import ReleaseUpdateManager
from studio.update.store import ReleaseStore
from studio.update.trust import TrustStore

root, rel_dir, pwfile = (Path(a).expanduser() for a in sys.argv[1:4])
layout = Layout.discover(root)
ctx = Context(layout.state, migrate=False)
port = ctx.settings().server.port
base = f"http://127.0.0.1:{port}"
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)
    if not cond:
        fails.append(name)


class LocalRepo(ReleaseRepository):
    name = "local-test"

    def __init__(self, d):
        self.d = Path(d)

    def index_url(self, channel):
        return str(self.d / f"release-index-{channel}.json")

    def fetch_index(self, channel):
        return Path(self.index_url(channel)).read_bytes()

    def download(self, url, dest, size, progress=None, cancelled=None):
        import shutil

        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self.d / url.rsplit("/", 1)[-1], dest)
        return dest


pw = [ln.split(": ", 1)[1].strip() for ln in pwfile.read_text().splitlines() if ln.startswith("password:")][0]
c = httpx.Client(base_url=base, headers={"X-Studio-Request": "1"}, timeout=30)
r = c.post("/api/auth/login", json={"username": "admin", "password": pw})
check("sign in with the installer-created admin account", r.status_code == 200, str(r.status_code))
voices = c.get("/api/voices").json()["voices"]
ryan = next(v for v in voices if v["name"] == "Ryan")
prev = c.post("/api/imports", files={"file": ("drain.txt", (SMOKE_TEXT * 2).encode(), "text/plain")}).json()
book = c.post(f"/api/imports/{prev['token']}/confirm", json={"title": "Drain Test " + time.strftime("%H%M%S"), "voice_id": ryan["id"], "queue": True}).json()
bid = book["id"]


def seg_counts():
    with ctx.db.session() as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM segments WHERE book_id=? GROUP BY status", (bid,))}


def attempts_done():
    with ctx.db.session() as conn:
        return conn.execute("SELECT COUNT(*) FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=? AND a.status='done'", (bid,)).fetchone()[0]


end = time.time() + 180
while time.time() < end and attempts_done() < 3:
    time.sleep(1)
before = attempts_done()
check("real narration is under way (>=3 segments done)", before >= 3, str(before))

m = ReleaseUpdateManager(ctx, layout, MacPlatformService(), repository=LocalRepo(rel_dir), trust=TrustStore.load_pinned())
m.launch_helper = make_launcher(layout, MacPlatformService())
start_version = ReleaseStore(layout).current()
st = m.check()
check("update offered", st["state"] == "available", st.get("error") or st["state"])
target = st["latest"]["version"]
m.download(background=False)
check("update staged while the job runs", m.status()["state"] == "staged")
t_req = time.time()
m.request_install("now", target)
seen_hold = False
seen_states = set()
end = time.time() + 300
while time.time() < end:
    s = m.status()
    seen_states.add(s["state"])
    if (ctx.run_dir / "dispatch.hold").exists():
        seen_hold = True
    if s["state"] in ("succeeded", "failed", "rolled_back"):
        break
    time.sleep(0.5)
final = m.status()
check("updater finished", final["state"] == "succeeded", f"{final['state']}: {final.get('message')}")
check("queue was held during the switch", seen_hold, ",".join(sorted(seen_states)))
check("version switched", ReleaseStore(layout).current() == target and ReleaseStore(layout).previous() == start_version)
check("service serves the new version", httpx.get(base + "/healthz", timeout=5).json().get("version") == target)
done_at_restart = attempts_done()
check("segments finished before the stop were kept", done_at_restart >= before, f"{before} -> {done_at_restart}")

# the job must pick itself up again and finish, without redoing finished segments
end = time.time() + 600
while time.time() < end:
    cts = seg_counts()
    if cts and set(cts) <= {"passed", "approved", "flagged"} and cts.get("passed", 0) + cts.get("flagged", 0) + cts.get("approved", 0) == sum(cts.values()):
        break
    time.sleep(3)
cts = seg_counts()
total = sum(cts.values())
check("job resumed by itself and every segment passed QA", cts.get("passed", 0) == total and total > 0, str(cts))
with ctx.db.session() as conn:
    dup = conn.execute("""SELECT COUNT(*) FROM (SELECT segment_id, COUNT(*) n FROM attempts WHERE status='done' GROUP BY segment_id HAVING n > 1)
                          JOIN segments s ON s.id=segment_id WHERE s.book_id=?""", (bid,)).fetchone()[0]
check("no completed segment was regenerated after the update", dup == 0, f"{dup} segment(s) with more than one finished attempt")
print("\nRESULT:", "ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
