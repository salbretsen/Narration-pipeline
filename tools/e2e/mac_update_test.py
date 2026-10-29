"""Real-Mac update test: signed releases served from a LOCAL directory (no GitHub), verified against the PINNED production public key,
installed through the real launchd handoff (one-shot updater job), with a tampered archive, a good update and a broken one that must roll back.

    <venv>/bin/python mac_update_test.py <install-root> <dir-with-good-index+archives> <dir-with-bad-index+archive>

Needs the signed files built on the dev machine (the private key never leaves it). Prints PASS/FAIL per check; exit 1 on any failure.
"""
import json
import shutil
import sys
import time
from pathlib import Path

import httpx

from studio.context import Context
from studio.interfaces import ReleaseRepository
from studio.platform_services import MacPlatformService
from studio.update.launch import make_launcher
from studio.update.layout import Layout
from studio.update.manager import ReleaseUpdateManager
from studio.update.store import ReleaseStore
from studio.update.trust import TrustStore

root, good_dir, bad_dir = (Path(a).expanduser() for a in sys.argv[1:4])
layout = Layout.discover(root)
ctx = Context(layout.state, migrate=False)
ps = MacPlatformService()
fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail else ""), flush=True)
    if not cond:
        fails.append(name)


class LocalRepo(ReleaseRepository):
    """Serves a signed index and its archives from a directory. Authenticity is still decided only by the pinned key."""
    name = "local-test"

    def __init__(self, d):
        self.d = Path(d)

    def index_url(self, channel):
        return str(self.d / f"release-index-{channel}.json")

    def fetch_index(self, channel):
        return Path(self.index_url(channel)).read_bytes()

    def download(self, url, dest, size, progress=None, cancelled=None):
        src = self.d / url.rsplit("/", 1)[-1]
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        return dest


def health():
    try:
        return httpx.get(f"http://127.0.0.1:{ctx.settings().server.port}/healthz", timeout=3).json()
    except Exception:
        return None


def make(repo_dir):
    m = ReleaseUpdateManager(ctx, layout, ps, repository=LocalRepo(repo_dir), trust=TrustStore.load_pinned())
    m.launch_helper = make_launcher(layout, ps)
    return m


def wait_for(pred, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(2)
    return None


store = ReleaseStore(layout)
ps.start("com.audiobookstudio.app")
check("service healthy before the update", wait_for(lambda: (health() or {}).get("version") == "0.5.2", 90))
with ctx.db.tx() as conn:
    ctx.event(conn, "canary", "must survive both updates")

# 1. the production key is pinned and a tampered archive is refused
check("a production signing key is pinned", TrustStore.load_pinned().configured)
tamper = Path("/tmp/tamper-test")
shutil.rmtree(tamper, ignore_errors=True)
shutil.copytree(good_dir, tamper)
arc = next(tamper.glob("*.tar.gz"))
b = bytearray(arc.read_bytes())
b[len(b) // 2] ^= 0xFF
arc.write_bytes(bytes(b))
m = make(tamper)
st = m.check()
check("tampered archive: index itself still verifies", st["state"] == "available", st["state"])
m.download(background=False)
st = m.status()
check("tampered archive refused", st["state"] == "failed" and "does not match" in (st["error"] or ""), st.get("error"))
check("nothing staged from the tampered archive", not layout.release_dir("0.5.3").exists())

# 2. an index signed by a different key is refused
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import base64
from studio.update.trust import sign_payload
env = json.loads((good_dir / "release-index-stable.json").read_text())
forged = Path("/tmp/forged-test")
shutil.rmtree(forged, ignore_errors=True)
shutil.copytree(good_dir, forged)
(forged / "release-index-stable.json").write_bytes(sign_payload(env["signed"], Ed25519PrivateKey.generate()))
st = make(forged).check()
check("index signed by an unknown key refused", st["state"] == "failed" and st["error_kind"] == "untrusted", st.get("error"))

# 3. the good update through the real launchd handoff
m = make(good_dir)
st = m.check()
check("good update offered", st["state"] == "available" and st["latest"]["version"] == "0.5.3", str(st.get("latest", {}).get("version")))
m.download(background=False)
st = m.status()
check("good update downloaded, verified and staged", st["state"] == "staged", st.get("error"))
check("staging did not switch anything", store.current() == "0.5.2" and (health() or {}).get("version") == "0.5.2")
m.request_install("now", "0.5.3")
done = wait_for(lambda: m.status()["state"] in ("succeeded", "failed", "rolled_back") and m.status()["state"], 240)
st = m.status()
check("updater finished successfully", done == "succeeded", f"{done}: {st.get('message')}")
check("new version is serving", wait_for(lambda: (health() or {}).get("version") == "0.5.3", 60) is not None)
check("pointer switched, previous kept", store.current() == "0.5.3" and store.previous() == "0.5.2", f"{store.current()}/{store.previous()}")
with ctx.db.session() as conn:
    check("database content survived", conn.execute("SELECT COUNT(*) FROM events WHERE kind='canary'").fetchone()[0] == 1)
check("database backup taken", bool(list((ctx.backups_dir / "update").glob("studio-*.db"))))
check("queue hold released", not (ctx.run_dir / "dispatch.hold").exists())

# 4. a release that installs but whose service dies must roll back
m = make(bad_dir)
st = m.check()
check("broken release offered", st["state"] == "available" and st["latest"]["version"] == "0.5.4", str(st.get("latest", {}).get("version")))
m.download(background=False)
m.request_install("now", "0.5.4")
done = wait_for(lambda: m.status()["state"] in ("succeeded", "failed", "rolled_back") and m.status()["state"], 300)
st = m.status()
check("broken release rolled back", done == "rolled_back", f"{done}: {st.get('message')}")
check("previous version serving again", wait_for(lambda: (health() or {}).get("version") == "0.5.3", 90) is not None)
check("pointer restored", store.current() == "0.5.3", store.current())
with ctx.db.session() as conn:
    check("database still intact after rollback", conn.execute("SELECT COUNT(*) FROM events WHERE kind='canary'").fetchone()[0] == 1)
hist = m.status_file.read_history(10)
check("history records the failure and the success", any(h["event"] == "install" and h["ok"] for h in hist) and any(h["event"] == "install" and not h["ok"] for h in hist))
print("\nRESULT:", "ALL PASSED" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
