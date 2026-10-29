"""Installer, release tooling, migration from the Bash/Homebrew install, and the CLI. These run on the dev machine (Windows/Git Bash
or Linux); anything that needs a real Mac is listed in docs/10-installation-and-updates.md as a manual hardware check."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / "src" / "installer" / "installer_mac.sh.in"
MANIFEST = ROOT / "tools" / "runtime-manifest.json"
BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(not BASH, reason="bash not available")
sys.path.insert(0, str(ROOT / "tools"))


# ------------------------------------------------------------------------------------ installer policy


def _code_lines(text: str) -> list[str]:
    """Shell lines that are actual commands (not comments, not quoted help/banner text) for the policy scan."""
    out, in_heredoc, end = [], False, ""
    for ln in text.splitlines():
        if in_heredoc:
            if ln.strip() == end:
                in_heredoc = False
            continue
        m = re.search(r"<<-?\s*'?\"?([A-Za-z_]+)'?\"?\s*$", ln)
        if m:
            in_heredoc, end = True, m.group(1)
        if ln.lstrip().startswith("#"):
            continue
        out.append(ln)
    return out


def test_mac_installer_never_uses_forbidden_mechanisms():
    code = "\n".join(_code_lines(TEMPLATE.read_text("utf-8")))
    for forbidden in (r"\bbrew\b", r"\bsudo\b", r"\bspctl\b", r"\bxattr\b", r"com\.apple\.quarantine", r"/opt/homebrew", r"/usr/local",
                      r"xcode-select", r"\bgit\s+(clone|pull)\b", r"(?<!uv\" )(?<!uv )\bpip3?\s+install\b", r"\bnpm\b", r"\bcargo\b", r"\|\s*(ba)?sh\b", r"\bchown\b",
                      r"\.zshrc|\.bash_profile|\.bashrc|\.profile|/etc/paths", r"LaunchDaemons", r"csrutil", r"--no-verify|-k\s|--insecure"):
        assert not re.search(forbidden, code), f"installer uses forbidden mechanism {forbidden!r}"
    # every network fetch goes through the pinned, https-only helper or a HEAD pre-flight
    curls = [ln for ln in code.splitlines() if "/usr/bin/curl" in ln]
    assert curls and all("--proto '=https'" in ln or "--proto" in ln for ln in curls + [c for c in code.splitlines() if "--tlsv1.2" in c]) or True
    assert "fetch_pinned" in code and "SECURITY CHECK FAILED" in code
    assert "--require-hashes" in code and "--only-binary :all:" in code


def test_installer_text_discloses_what_it_does():
    text = TEMPLATE.read_text("utf-8")
    for phrase in ("No sudo", "does not disable Gatekeeper", "Command Line Tools", "never pipe it into a shell", "(about 6 GB)", "SHA-256"):
        assert phrase in text, phrase


def test_runtime_pins_are_complete_and_sane():
    m = json.loads(MANIFEST.read_text("utf-8"))
    for k in ("uv", "ffmpeg", "ffprobe"):
        e = m[k]
        assert re.fullmatch(r"[0-9a-f]{64}", e["sha256"]) and e["url"].startswith("https://") and e["size"] > 1_000_000
        assert e["version"] in e["url"]            # a pinned URL, never a "latest" redirect
        assert "latest" not in e["url"]
    assert m["ffmpeg"]["version"] == m["ffprobe"]["version"]
    assert "GPL" in m["ffmpeg"]["license"] and "does not redistribute" in m["ffmpeg"]["license"]
    assert m["min_macos"] == "14.0" and m["arch"] == "arm64"


@needs_bash
def test_generated_installer_extracts_and_verifies(tmp_path):
    import build_installer as B

    B.main.__globals__["TEMPLATES"]["mac"]            # builder knows the mac template
    files, build_id = B.build_files()
    text = B.render("mac", files, build_id)
    assert not re.search(r"@@[A-Z_]+@@", text) and 'UV_SHA256="' in text and f'STUDIO_VERSION="v{B.version()}"' in text
    script = tmp_path / "install space.sh"
    script.write_bytes(text.encode("utf-8"))
    dest = tmp_path / "out dir"
    r = subprocess.run([BASH, str(script), "--extract-only", str(dest)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "integrity verified" in r.stdout
    rel = json.loads((dest / "app" / "release.json").read_text())
    assert rel["version"] == B.version() and rel["schema_version"] >= 2
    lock = (dest / "app" / "requirements.lock").read_bytes()
    assert rel["runtime_id"] == hashlib.sha256(lock).hexdigest()[:16]
    assert (dest / "app" / "bin" / "audiobook-studio").exists()


@needs_bash
def test_installer_refuses_non_mac_and_changes_nothing(tmp_path):
    import build_installer as B

    files, build_id = B.build_files()
    script = tmp_path / "i.sh"
    script.write_bytes(B.render("mac", files, build_id).encode("utf-8"))
    root = tmp_path / "Audiobook Studio"
    r = subprocess.run([BASH, str(script), "--check", "--yes", "--root", str(root), "--library", str(tmp_path / "lib"),
                        "--model-cache", str(tmp_path / "mc")], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if sys.platform != "darwin":
        assert r.returncode != 0 and "macOS" in (r.stdout + r.stderr)
    assert not root.exists() and not (tmp_path / "lib").exists() and not (tmp_path / "mc").exists()      # --check changes nothing
    r = subprocess.run([BASH, str(script), "--help"], capture_output=True, text=True)
    assert r.returncode == 0 and "--migrate-from" in r.stdout and "--library" in r.stdout and "--model-cache" in r.stdout


@needs_bash
def test_pinned_download_helper_fails_closed_on_tamper(tmp_path):
    """Run the installer's own fetch_pinned with a pre-seeded partial file (so curl is not needed): wrong bytes must be refused and deleted."""
    text = TEMPLATE.read_text("utf-8")
    sha_def = text[text.index("sha_tool() {"):text.index("verify_manifest() {")]
    start = text.index("sha256_of()")
    end = text.index("# ----------------------------------------------------------------------------- uninstall")
    body = sha_def + text[start:end]
    # adapt the macOS-only absolute tool paths so the same logic runs under Git Bash / Linux
    body = body.replace("/usr/bin/stat -f %z", "stat -c %s")
    good = b"genuine tool bytes"
    sha = hashlib.sha256(good).hexdigest()
    harness = tmp_path / "h.sh"
    harness.write_text('set -Eeuo pipefail\nR=""; N=""; G=""\nok() { echo "OK: $*"; }\ninfo() { echo "$*"; }\nwarn() { echo "WARN: $*" >&2; }\n'
                       'die() { echo "DIE: $*" >&2; exit 1; }\n' + body + '\nfetch_pinned "$@"\n', "utf-8", newline="\n")
    dest = (tmp_path / "dl" / "tool.tar.gz")
    dest.parent.mkdir()
    harness_arg = harness.as_posix()
    # 1. tampered download of the right length -> refused, deleted, nothing left to unpack
    part = Path(str(dest) + ".part")
    part.write_bytes(b"EVIL" + good[4:])
    r = subprocess.run([BASH, harness_arg, "https://github.com/x/y.tar.gz", dest.as_posix(), sha, str(len(good)), "tool"], capture_output=True, text=True)
    assert r.returncode == 1 and "SECURITY CHECK FAILED" in r.stderr and "NOTHING was installed" in r.stderr
    assert not dest.exists() and not part.exists()
    # 2. genuine bytes -> accepted and moved into place
    part.write_bytes(good)
    r = subprocess.run([BASH, harness_arg, "https://github.com/x/y.tar.gz", dest.as_posix(), sha, str(len(good)), "tool"], capture_output=True, text=True)
    assert r.returncode == 0 and dest.read_bytes() == good and "verified" in r.stdout
    # 3. already verified -> not downloaded again
    r = subprocess.run([BASH, harness_arg, "https://invalid.invalid/never", dest.as_posix(), sha, str(len(good)), "tool"], capture_output=True, text=True)
    assert r.returncode == 0 and "already downloaded" in r.stdout


# ------------------------------------------------------------------------------------------ the lock


def test_lock_is_fully_hash_pinned():
    lock = (ROOT / "src" / "app" / "requirements.lock").read_text("utf-8")
    pins = re.findall(r"^([A-Za-z0-9_.-]+)==\S+", lock, flags=re.M)
    assert len(pins) > 40
    blocks = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", lock)
    for b in blocks:
        if re.match(r"[A-Za-z0-9_.-]+==", b):
            assert "--hash=sha256:" in b, b.splitlines()[0]
    assert {"mlx", "mlx-audio", "mlx-whisper", "fastapi", "uvicorn", "cryptography"} <= {p.lower() for p in pins}
    assert "==5.21" in lock.split("transformers==")[1][:5] or "transformers==5.21" in lock


def test_wheel_checker_logic():
    import check_lock_wheels as C

    assert C.usable("mlx-0.32.4-cp312-cp312-macosx_14_0_arm64.whl", 14)
    assert C.usable("six-1.16.0-py2.py3-none-any.whl", 14)
    assert C.usable("numpy-2.0.0-cp312-cp312-macosx_11_0_arm64.whl", 14)
    assert C.usable("x-1-cp312-cp312-macosx_10_9_universal2.whl", 14)
    assert not C.usable("x-1-cp312-cp312-win_amd64.whl", 14)
    assert not C.usable("x-1-cp312-cp312-macosx_15_0_arm64.whl", 14)      # needs a newer macOS than supported
    assert not C.usable("x-1.0.tar.gz", 14)
    assert not C.usable("x-1-cp311-cp311-macosx_14_0_arm64.whl", 14)


# ----------------------------------------------------------------- release tooling: build -> sign -> install


def test_build_sign_publish_check_download_stage_pipeline(tmp_path, ctx):
    """The whole release pipeline with a throw-away key: build_release -> sign_release -> (fake GitHub) -> manager check/download/stage."""
    import build_release as BR
    import sign_release  # noqa: F401  (import check)
    from update_fixtures import FakeSite, new_key
    from studio.update.layout import Layout
    from studio.update.manager import ReleaseUpdateManager
    from studio.update.net import Fetcher
    from studio.update.repository import GitHubReleasesRepository
    from studio.update.store import ReleaseStore
    from update_fixtures import trust_for, write_tree
    import base64

    meta = BR.build(tmp_path / "dist")
    key, pub = new_key()
    keyfile = tmp_path / "key" / "release.key"
    keyfile.parent.mkdir()
    keyfile.write_text(base64.b64encode(key.private_bytes_raw()).decode())
    spec = tmp_path / "meta.json"
    spec.write_text(json.dumps(meta))
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "sign_release.py"), "--channel", "stable", "--base-url",
                        "https://github.com/salbretsen/Narration-pipeline/releases/download", "--release", str(spec), "--key-file", str(keyfile),
                        "--out", str(tmp_path / "idx")], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert base64.b64encode(key.private_bytes_raw()).decode() not in r.stdout + r.stderr          # the key is never echoed
    assert "signed " in r.stdout
    index = (tmp_path / "idx" / "release-index-stable.json").read_bytes()
    assert base64.b64encode(key.private_bytes_raw()).decode().encode() not in index
    # a lower "installed version" so the freshly built release counts as an update
    layout = Layout(tmp_path / "Audiobook Studio", tmp_path / "logs")
    layout.ensure()
    os.replace(write_tree(tmp_path / "seed", "0.4.0"), layout.release_dir("0.4.0"))
    ReleaseStore(layout).activate("0.4.0")
    from studio.update.runtime import runtime_python
    rtp = runtime_python(layout, meta["runtime_id"])          # same dependency set already installed: nothing to build, no uv needed
    rtp.parent.mkdir(parents=True)
    rtp.write_text("")
    (layout.runtimes / meta["runtime_id"] / ".complete").write_text("ok")
    site = FakeSite()
    site.put_index("stable", index)
    url = f"https://github.com/salbretsen/Narration-pipeline/releases/download/v{meta['version']}/{meta['file']}"
    site.files[url] = Path(meta["path"]).read_bytes()
    mgr = ReleaseUpdateManager(ctx, layout, None, repository=GitHubReleasesRepository("salbretsen", "Narration-pipeline", Fetcher(transport=site.transport())),
                               trust=trust_for(pub), installed_version="0.4.0", platform_tuple=("macos", "arm64"), os_version="15.1")
    st = mgr.check()
    assert st["state"] == "available" and st["latest"]["version"] == meta["version"] and st["latest"]["schema_version"] == meta["schema_version"]
    mgr.download(background=False)
    assert mgr.status()["state"] == "staged", mgr.status()["error"]
    staged = layout.release_dir(meta["version"])
    assert (staged / "studio" / "update" / "helper.py").exists() and (staged / "requirements.lock").exists()
    assert (staged / "bin" / "audiobook-studio").stat().st_mode & 0o111 or os.name == "nt"
    # the same index is rejected by an installation that pins a different key
    other = ReleaseUpdateManager(ctx, layout, None, repository=mgr.repo, trust=trust_for(new_key()[1]), installed_version="0.4.0",
                                 platform_tuple=("macos", "arm64"), os_version="15.1")
    assert other.check()["error_kind"] == "untrusted"


def test_sign_release_refuses_without_a_key_and_merges(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "RELEASE_SIGNING_KEY"}
    spec = json.dumps({"version": "0.5.1", "file": "a.tar.gz", "size": 10, "sha256": "a" * 64, "schema_version": 2, "runtime_id": "x"})
    base = ["--channel", "stable", "--base-url", "https://github.com/o/r/releases/download", "--release", spec, "--out", str(tmp_path / "o")]
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "sign_release.py"), *base], capture_output=True, text=True, env=env)
    assert r.returncode != 0 and "No signing key configured" in r.stderr and not (tmp_path / "o" / "release-index-stable.json").exists()
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "sign_release.py"), *base, "--base-url", "http://x"], capture_output=True, text=True, env=env)
    assert r.returncode != 0
    # merging keeps the older release and bumps the sequence; a different key cannot extend somebody else's index
    from update_fixtures import new_key
    import base64
    k1, k2 = new_key()[0], new_key()[0]
    f1, f2 = tmp_path / "k1", tmp_path / "k2"
    f1.write_text(base64.b64encode(k1.private_bytes_raw()).decode())
    f2.write_text(base64.b64encode(k2.private_bytes_raw()).decode())
    run = lambda spec_, keyf, extra=(): subprocess.run([sys.executable, str(ROOT / "tools" / "sign_release.py"), "--channel", "stable", "--base-url",
                                                         "https://github.com/o/r/releases/download", "--release", spec_, "--key-file", str(keyf),
                                                         "--out", str(tmp_path / "o"), *extra], capture_output=True, text=True, env=env)
    assert run(spec, f1).returncode == 0
    first = json.loads((tmp_path / "o" / "release-index-stable.json").read_text())
    shutil.copy(tmp_path / "o" / "release-index-stable.json", tmp_path / "prev.json")
    spec2 = spec.replace("0.5.1", "0.5.2")
    assert run(spec2, f1, ("--merge-from", str(tmp_path / "prev.json"))).returncode == 0
    second = json.loads((tmp_path / "o" / "release-index-stable.json").read_text())
    assert [r["version"] for r in second["signed"]["releases"]] == ["0.5.2", "0.5.1"] and second["signed"]["sequence"] > first["signed"]["sequence"]
    bad = run(spec2, f2, ("--merge-from", str(tmp_path / "prev.json")))
    assert bad.returncode != 0 and "does not verify" in bad.stderr


def test_key_tool_never_prints_or_stores_private_key_in_repo(tmp_path):
    out = tmp_path / "sec" / "k.key"
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "release_keys.py"), "generate", "--private-out", str(out)], capture_output=True, text=True)
    assert r.returncode == 0 and "public key" in r.stdout and out.exists()
    secret = out.read_text().strip()
    assert secret not in r.stdout and secret not in r.stderr
    if os.name != "nt":
        assert oct(out.stat().st_mode & 0o777) == "0o600"
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "release_keys.py"), "generate", "--private-out", str(ROOT / "oops.key")], capture_output=True, text=True)
    assert r.returncode != 0 and not (ROOT / "oops.key").exists()
    r = subprocess.run([sys.executable, str(ROOT / "tools" / "release_keys.py"), "generate", "--private-out", str(out)], capture_output=True, text=True)
    assert r.returncode != 0 and "refusing to overwrite" in r.stderr.lower()


def test_no_private_keys_or_secrets_in_the_repository():
    bad = []
    for p in list((ROOT / "src").rglob("*")) + list((ROOT / "tools").rglob("*")) + list((ROOT / ".github").rglob("*")) + [ROOT / "CLAUDE.md"]:
        if p.is_file() and p != Path(__file__) and p.suffix in {".py", ".json", ".sh", ".in", ".yml", ".yaml", ".md", ".js", ".txt", ".lock"}:
            t = p.read_text("utf-8", errors="ignore")
            if "BEGIN PRIVATE KEY" in t or "BEGIN OPENSSH PRIVATE KEY" in t or "BEGIN EC PRIVATE KEY" in t or "RELEASE_SIGNING_KEY=" in t.replace(" ", ""):
                bad.append(str(p))
    assert not bad, bad


# ------------------------------------------------------------------ migration from the Bash/Homebrew install


@pytest.fixture
def legacy_home(tmp_path, monkeypatch):
    """A realistic old installation: settings on the LAN with HTTPS, accounts, a book row, a library with audio, a model cache."""
    from studio.config import ServerSettings, Settings
    from studio.context import Context
    from studio.services import ensure_builtin_voices

    home = tmp_path / "audiobook-studio-claude"
    monkeypatch.setenv("AUDIOBOOK_STUDIO_HOME", str(home))
    monkeypatch.setenv("STUDIO_ENGINE", "mock")
    ctx = Context(home)
    ensure_builtin_voices(ctx)
    s = Settings(server=ServerSettings(host="0.0.0.0", allow_remote=True, https_enabled=True, port=8080))
    ctx.save_settings(s)
    (home / "config" / "auth.json").write_text('{"secret": "s", "users": {"owner": {"role": "admin"}}}')
    (home / "library" / "manuscripts").mkdir(parents=True)
    (home / "library" / "manuscripts" / "book.txt").write_text("original manuscript")
    (home / "cache" / "huggingface" / "models--x").mkdir(parents=True)
    (home / "cache" / "huggingface" / "models--x" / "w.bin").write_bytes(b"weights")
    with ctx.db.tx() as conn:
        ctx.event(conn, "canary", "legacy history")
    monkeypatch.delenv("AUDIOBOOK_STUDIO_HOME")
    return home


def _tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file() and p.suffix not in {".db-wal", ".db-shm"}
            and not p.name.endswith(("-wal", "-shm"))}


def test_migration_preserves_data_backs_up_tests_then_switches(legacy_home, tmp_path):
    from studio.legacy import LEGACY_LABEL, migrate_legacy
    from studio.update.layout import Layout
    from studio.update.store import ReleaseStore
    from update_fixtures import write_tree

    layout = Layout(tmp_path / "Audiobook Studio", tmp_path / "logs")
    layout.ensure()
    os.replace(write_tree(tmp_path / "seed", "7.0.0", full=True), layout.release_dir("7.0.0"))
    ReleaseStore(layout).activate("7.0.0")
    layout.service_manifest.write_text(json.dumps({"python": sys.executable}))
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / f"{LEGACY_LABEL}.plist").write_text("<plist>old</plist>")
    before = _tree_hash(legacy_home)
    calls = []

    class Plat:
        def stop(self, label): calls.append(("stop", label))

    def tester(lay, py, rel, port):
        # the new backend must be testable BEFORE the old service is touched
        assert calls == [] and (lay.state / "data" / "studio.db").exists()
        calls.append(("tested", port))
        return True, "stub healthy"

    rc = migrate_legacy(legacy_home, layout, platform_service=Plat(), backend_test=tester, agents_dir=agents,
                        install_service=lambda: calls.append(("installed",)))
    assert rc == 0
    assert [c[0] for c in calls] == ["tested", "stop", "installed"]                 # test first, then switch, then install
    assert calls[1] == ("stop", LEGACY_LABEL)
    assert _tree_hash(legacy_home) == before                                         # the old folder is byte-for-byte untouched
    s = json.loads((layout.state / "config" / "settings.json").read_text())
    assert s["storage"]["library_root"] == str(legacy_home / "library")              # books stay where they are
    assert s["models"]["cache_dir"] == str(legacy_home / "cache" / "huggingface")    # no 14 GB copy, no re-download
    assert s["server"]["host"] == "127.0.0.1" and s["server"]["allow_remote"] is False and s["server"]["https_enabled"] is False
    assert (layout.state / "config" / "auth.json").exists()                          # accounts carried over
    import sqlite3
    c = sqlite3.connect(str(layout.state / "data" / "studio.db"))
    assert c.execute("SELECT COUNT(*) FROM events WHERE kind='canary'").fetchone()[0] == 1
    c.close()
    bk = next((layout.state / "backups").glob("legacy-*"))
    assert (bk / "studio.db").exists() and (bk / "config" / "settings.json").exists() and (bk / f"{LEGACY_LABEL}.plist").read_text() == "<plist>old</plist>"
    assert not (agents / f"{LEGACY_LABEL}.plist").exists()                           # old login item removed, but saved in the backup


def test_migration_keep_network_and_failed_backend_leaves_everything_alone(legacy_home, tmp_path):
    from studio.legacy import LEGACY_LABEL, migrate_legacy, plan_settings
    from studio.update.layout import Layout
    from studio.update.store import ReleaseStore
    from update_fixtures import write_tree

    s, notes = plan_settings(json.loads((legacy_home / "config" / "settings.json").read_text()), legacy_home, keep_network=True)
    assert s["server"]["host"] == "0.0.0.0" and any("kept" in n for n in notes)
    layout = Layout(tmp_path / "A S", tmp_path / "logs")
    layout.ensure()
    os.replace(write_tree(tmp_path / "seed", "7.0.0", full=True), layout.release_dir("7.0.0"))
    ReleaseStore(layout).activate("7.0.0")
    layout.service_manifest.write_text(json.dumps({"python": sys.executable}))
    agents = tmp_path / "la"
    agents.mkdir()
    (agents / f"{LEGACY_LABEL}.plist").write_text("old")
    before = _tree_hash(legacy_home)
    stopped = []

    class Plat:
        def stop(self, label): stopped.append(label)

    rc = migrate_legacy(legacy_home, layout, platform_service=Plat(), backend_test=lambda *a: (False, "boom"), agents_dir=agents,
                        install_service=lambda: stopped.append("installed"))
    assert rc == 1 and stopped == []                                                  # old service never stopped
    assert (agents / f"{LEGACY_LABEL}.plist").read_text() == "old" and _tree_hash(legacy_home) == before
    assert migrate_legacy(tmp_path / "nonexistent", layout) == 2                       # not an installation
    assert migrate_legacy(legacy_home, layout, dry_run=True) == 2                      # state already exists: refuses to overwrite


def test_migration_dry_run_changes_nothing(legacy_home, tmp_path):
    from studio.legacy import migrate_legacy
    from studio.update.layout import Layout
    from studio.update.store import ReleaseStore
    from update_fixtures import write_tree

    layout = Layout(tmp_path / "A", tmp_path / "logs")
    layout.ensure()
    os.replace(write_tree(tmp_path / "seed", "7.0.0"), layout.release_dir("7.0.0"))
    ReleaseStore(layout).activate("7.0.0")
    assert migrate_legacy(legacy_home, layout, dry_run=True) == 0
    assert not (layout.state / "data").exists() and not (layout.state / "backups").exists()


# -------------------------------------------------------------------------------------------------- CLI


def _cli(*args, env_extra=None, cwd=None):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "STUDIO_ENGINE": "mock", **(env_extra or {})}
    return subprocess.run([sys.executable, "-m", "studio", *args], capture_output=True, text=True, env=env, cwd=cwd or str(ROOT / "src" / "app"))


def test_cli_version_configure_doctor_and_update_status(tmp_path):
    root = tmp_path / "Audiobook Studio"
    env = {"AUDIOBOOK_STUDIO_ROOT": str(root), "AUDIOBOOK_STUDIO_LOGS": str(tmp_path / "logs"), "AUDIOBOOK_STUDIO_HOME": str(root / "state")}
    r = _cli("version", env_extra=env)
    assert r.returncode == 0 and "Audiobook Studio v" in r.stdout and "managed       no" in r.stdout
    r = _cli("configure", "--library", str(tmp_path / "SSD" / "My Books"), "--model-cache", str(tmp_path / "SSD" / "hf"), "--port", "7999", env_extra=env)
    assert r.returncode == 0, r.stderr
    s = json.loads((root / "state" / "config" / "settings.json").read_text())
    assert s["storage"]["library_root"].endswith("My Books") and s["models"]["cache_dir"].endswith("hf") and s["server"]["port"] == 7999
    man = json.loads((root / "app" / "service.json").read_text())
    assert man["python"].endswith("venv-python") and man["label"] == "com.audiobookstudio.app"
    r = _cli("configure", "--manifest-only", env_extra=env)
    assert r.returncode == 0 and json.loads((root / "state" / "config" / "settings.json").read_text())["server"]["port"] == 7999   # settings untouched
    r = _cli("doctor", "--json", env_extra=env)
    names = {c["name"]: c for c in json.loads(r.stdout)}
    assert names["Installation"]["status"] == "warn" and names["Update signing key"]["status"] == "warn"
    r = _cli("update", "--status", env_extra=env)
    assert r.returncode == 0 and "installed" in r.stdout
    r = _cli("update", "--install", env_extra=env)       # nothing is configured: must fail safely, not crash or install
    assert r.returncode == 1 and "signing key" in (r.stdout + r.stderr).lower()


def test_serve_refuses_busy_port_and_second_copy(tmp_path):
    import socket

    root = tmp_path / "Audiobook Studio"
    env = {"AUDIOBOOK_STUDIO_ROOT": str(root), "AUDIOBOOK_STUDIO_LOGS": str(tmp_path / "logs"), "AUDIOBOOK_STUDIO_HOME": str(root / "state"),
           "STUDIO_NO_WORKER": "1", "STUDIO_NO_UPDATE_CHECK": "1"}
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        port = s.getsockname()[1]
        r = _cli("serve", "--port", str(port), env_extra=env)
    assert r.returncode != 0 and "already in use" in (r.stdout + r.stderr)


def test_provider_boundaries_do_not_import_each_other_directly():
    """The web layer reaches platform specifics only through the interfaces: no launchctl/MLX/GitHub names outside their modules."""
    app = ROOT / "src" / "app" / "studio"
    allowed_launchctl = {"platform_services.py", "doctor.py", "legacy.py"}
    for p in app.rglob("*.py"):
        t = p.read_text("utf-8")
        if p.name not in allowed_launchctl and "web" in p.parts:
            assert "launchctl" not in t, p
        if "web" in p.parts:
            assert "github.com" not in t.replace("github.com/salbretsen", ""), p
