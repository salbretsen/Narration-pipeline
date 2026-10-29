"""Update building blocks: signatures, release selection, hardened network and archive handling, release store, runtime provisioning,
platform services, provider/storage interfaces. All local; no network, no macOS."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import plistlib
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from studio.interfaces import (ASRProvider, InferenceProvider, ModelRegistry, PlatformService, QueueScheduler, ReleaseRepository, ServiceSpec,
                               StorageManager, TTSProvider, UpdateManager)
from studio.update import archive as AR
from studio.update.layout import Layout
from studio.update.manifest import Index, ManifestError, parse_version, select_update
from studio.update.net import Fetcher, NetError, validate_repository
from studio.update.repository import GitHubReleasesRepository, HttpsRepository, make_repository
from studio.update.runtime import RuntimeError_, ensure_runtime, lock_id
from studio.update.store import ReleaseStore
from studio.update.trust import TrustError, TrustStore, canonical, sign_payload
from update_fixtures import BASE, FakeSite, entry, file_member, index_bytes, new_key, pack, tar_with, trust_for, write_tree

# ------------------------------------------------------------------------------------------------ signatures


def test_signature_roundtrip_and_tamper_detection():
    key, pub = new_key()
    ts = trust_for(pub)
    raw = index_bytes(key, [])
    assert ts.verify_envelope(raw)["channel"] == "stable"
    env = json.loads(raw)
    env["signed"]["releases"].append({"version": "9.9.9"})            # attacker edits the index
    with pytest.raises(TrustError, match="signature is invalid"):
        ts.verify_envelope(json.dumps(env).encode())
    env = json.loads(raw)
    env["signed"]["sequence"] = 99
    with pytest.raises(TrustError):
        ts.verify_envelope(json.dumps(env).encode())


def test_signature_from_unpinned_or_revoked_key_rejected():
    key, pub = new_key()
    other, _ = new_key()
    ts = trust_for(pub)
    with pytest.raises(TrustError):
        ts.verify_envelope(index_bytes(other, []))
    ts.revoked = {next(iter(ts.keys))}
    with pytest.raises(TrustError):
        ts.verify_envelope(index_bytes(key, []))


def test_revoked_keyids_inside_signed_index_are_honoured():
    k1, p1 = new_key()
    k2, p2 = new_key()
    ts = TrustStore.from_public_b64(p1, p2)
    kid1 = [k for k in ts.keys][0]
    # signed by k2 and revoking k1: a later index signed with k1 must no longer verify only if the TrustStore remembers; here the
    # revocation applies to the very envelope that carries it
    raw = index_bytes(k1, [], revoked_keyids=[kid1])
    with pytest.raises(TrustError):
        ts.verify_envelope(raw)


def test_no_pinned_key_means_updates_are_disabled_not_unsigned():
    key, _ = new_key()
    with pytest.raises(TrustError, match="No release-signing key is pinned"):
        TrustStore().verify_envelope(index_bytes(key, []))
    # the repository's shipped key file is empty: an honest "not configured" state, never a silent pass
    pinned = TrustStore.load_pinned()
    assert pinned.configured and list(pinned.keys) == ["23bb536587702e29"]      # the owner's production PUBLIC key (private half is never in the repo)
    # the unsigned development mode must be opted into explicitly
    assert TrustStore(allow_unsigned=True).verify_envelope(index_bytes(key, []))["schema"] == 1


def test_canonical_json_is_order_independent():
    assert canonical({"b": 1, "a": [1, 2]}) == canonical({"a": [1, 2], "b": 1})


# ----------------------------------------------------------------------------------------------- selection


def _index(releases, **kw):
    key, _ = new_key()
    return Index.from_signed(json.loads(index_bytes(key, releases, **kw))["signed"])


def _rel(v, **over):
    return entry(v, Path("a.tar.gz"), "a" * 64, 1000, **over)


def sel(index, installed="0.5.0", channel="stable", os_version="15.1", schema=2, arch="arm64"):
    return select_update(index, installed, channel, "macos", arch, os_version, schema)


def test_semver_ordering():
    assert parse_version("0.10.0") > parse_version("0.9.9")
    assert parse_version("1.0.0") > parse_version("1.0.0-rc.1") > parse_version("1.0.0-beta.2") > parse_version("1.0.0-beta.1")
    with pytest.raises(ManifestError):
        parse_version("1.0")


def test_selection_states():
    idx = _index([_rel("0.5.0"), _rel("0.5.1")])
    assert sel(idx, "0.5.1").state == "up_to_date"
    assert sel(idx, "0.5.0").state == "available" and sel(idx, "0.5.0").release.version == "0.5.1"
    assert sel(idx, "0.5.0", arch="x86_64").state == "none_for_platform"
    assert sel(_index([_rel("0.5.1", min_os="16.0")]), os_version="15.1").state == "incompatible"
    assert "downgrade" in sel(_index([_rel("0.5.1", schema_version=1)]), schema=2).reason
    assert sel(_index([_rel("0.5.1", min_compatible_version="0.5.0")]), "0.4.0").state == "incompatible"


def test_stepping_stone_is_offered_when_newest_needs_intermediate():
    idx = _index([_rel("0.6.0", min_compatible_version="0.5.5"), _rel("0.5.5", min_compatible_version="0.4.0")])
    s = sel(idx, "0.4.0")
    assert s.state == "available" and s.release.version == "0.5.5" and s.newer_exists


def test_revoked_versions_and_channels():
    idx = _index([_rel("0.5.1")], revoked_versions=["0.5.1"])
    assert sel(idx).state == "none_for_platform"
    idx = _index([_rel("0.6.0-beta.1", channel="beta"), _rel("0.5.1")])
    assert sel(idx, channel="stable").release.version == "0.5.1"
    assert sel(idx, channel="beta").release.version == "0.6.0-beta.1"


def test_expired_index_and_bad_fields_rejected():
    key, _ = new_key()
    with pytest.raises(ManifestError, match="expired"):
        Index.from_signed(json.loads(index_bytes(key, [], expires_in_days=-1))["signed"])
    with pytest.raises(ManifestError):
        Index.from_signed(json.loads(index_bytes(key, [_rel("0.5.1", sha256="zz")]))["signed"])
    with pytest.raises(ManifestError):
        Index.from_signed(json.loads(index_bytes(key, [_rel("0.5.1", url="http://github.com/x")]))["signed"])


# ---------------------------------------------------------------------------------------------------- net


def test_repository_must_be_trusted():
    assert validate_repository("github:salbretsen/Narration-pipeline")
    for bad in ("http://github.com/x/y", "https://evil.example/releases", "file:///etc/passwd", "github:../../x", "https://user:pw@github.com/x", ""):
        with pytest.raises(ValueError):
            validate_repository(bad)
    from studio.config import UpdateSettings

    with pytest.raises(ValueError):
        UpdateSettings(repository="https://evil.example/")


def test_fetcher_enforces_https_hosts_and_redirects():
    site = FakeSite()
    f = Fetcher(transport=site.transport())
    with pytest.raises(NetError, match="non-HTTPS"):
        f.get_bytes("http://github.com/a")
    with pytest.raises(NetError, match="not a trusted"):
        f.get_bytes("https://evil.example/a")
    site.files["https://github.com/ok"] = b"hello"
    site.redirects["https://github.com/redir"] = "https://objects.githubusercontent.com/ok"
    site.files["https://objects.githubusercontent.com/ok"] = b"cdn"
    assert f.get_bytes("https://github.com/ok") == b"hello"
    assert f.get_bytes("https://github.com/redir") == b"cdn"
    site.redirects["https://github.com/bad"] = "https://evil.example/payload"
    with pytest.raises(NetError, match="not a trusted"):
        f.get_bytes("https://github.com/bad")
    site.redirects["https://github.com/dow"] = "http://objects.githubusercontent.com/ok"
    with pytest.raises(NetError, match="non-HTTPS"):
        f.get_bytes("https://github.com/dow")
    site.redirects["https://github.com/loop"] = "https://github.com/loop"
    with pytest.raises(NetError, match="Too many redirects"):
        f.get_bytes("https://github.com/loop")


def test_fetcher_size_limits_and_offline(tmp_path):
    site = FakeSite()
    f = Fetcher(transport=site.transport())
    site.files["https://github.com/big"] = b"x" * 5000
    with pytest.raises(NetError, match="larger than"):
        f.get_bytes("https://github.com/big", max_bytes=1000)
    with pytest.raises(NetError, match="unacceptable"):
        f.download("https://github.com/big", tmp_path / "a", expected_size=10 << 30)
    # server sends more than the signed size
    with pytest.raises(NetError, match="exceeded|larger"):
        f.download("https://github.com/big", tmp_path / "a", expected_size=4000)
    assert not (tmp_path / "a").exists()
    site.offline = True
    with pytest.raises(NetError) as ei:
        f.get_bytes("https://github.com/big")
    assert ei.value.offline


def test_interrupted_download_resumes_with_range(tmp_path):
    site = FakeSite()
    data = os.urandom(300_000)
    url = "https://github.com/dl/release.tar.gz"
    site.files[url] = data
    site.drop_after = 100_000
    f = Fetcher(transport=site.transport())
    dest = tmp_path / "x.tar.gz"
    with pytest.raises(NetError) as ei:
        f.download(url, dest, len(data))
    assert ei.value.offline and not dest.exists()
    part = dest.with_name(dest.name + ".part")
    assert 0 < part.stat().st_size < len(data)
    f.download(url, dest, len(data))
    assert dest.read_bytes() == data and not part.exists()
    assert any(r for _, r in site.requests if r and r.startswith("bytes=") and not r.startswith("bytes=0-"))


def test_repository_artifact_host_restrictions():
    site = FakeSite()
    repo = GitHubReleasesRepository("o", "r", Fetcher(transport=site.transport()))
    with pytest.raises(NetError, match="not hosted on this project"):
        repo.download("https://github.com/someone-else/repo/releases/download/v1/x.tar.gz", Path("x"), 10)
    h = HttpsRepository("https://example.invalid/updates", Fetcher(allowed_hosts={"example.invalid"}, transport=site.transport()))
    with pytest.raises(NetError, match="not on the update host"):
        h.download("https://other.invalid/x.tar.gz", Path("x"), 10)
    assert isinstance(make_repository("github:o/r"), GitHubReleasesRepository)


# -------------------------------------------------------------------------------------------------- archive


def test_safe_extract_happy_path_and_manifest(tmp_path):
    top = write_tree(tmp_path / "src", "0.5.1", extra_files={"bin/run": b"#!/bin/sh\n"})
    arc, _, _ = pack(top, tmp_path / "out")
    dest = tmp_path / "rel"
    assert AR.safe_extract(arc, dest, expected_top="audiobook-studio-0.5.1") == "audiobook-studio-0.5.1"
    assert AR.verify_tree_manifest(dest) >= 3
    assert (dest / "studio" / "__init__.py").exists()


@pytest.mark.parametrize("bad", ["../evil.txt", "audiobook-studio-1/../../evil", "/abs/evil", "audiobook-studio-1\\..\\evil", "C:/evil"])
def test_archive_path_traversal_rejected(tmp_path, bad):
    arc = tar_with([file_member(bad)], tmp_path / "a.tar.gz")
    with pytest.raises(AR.ArchiveError):
        AR.safe_extract(arc, tmp_path / "d")
    assert not (tmp_path / "d").exists() and not (tmp_path / "evil.txt").exists()


def test_archive_rejects_links_devices_dupes_multi_top_and_non_gzip(tmp_path):
    sym = tarfile.TarInfo("audiobook-studio-1/link")
    sym.type, sym.linkname = tarfile.SYMTYPE, "/etc/passwd"
    with pytest.raises(AR.ArchiveError, match="Links"):
        AR.safe_extract(tar_with([(sym, None)], tmp_path / "a.tar.gz"), tmp_path / "d1")
    hard = tarfile.TarInfo("audiobook-studio-1/h")
    hard.type, hard.linkname = tarfile.LNKTYPE, "audiobook-studio-1/x"
    with pytest.raises(AR.ArchiveError):
        AR.safe_extract(tar_with([(hard, None)], tmp_path / "b.tar.gz"), tmp_path / "d2")
    fifo = tarfile.TarInfo("audiobook-studio-1/f")
    fifo.type = tarfile.FIFOTYPE
    with pytest.raises(AR.ArchiveError, match="Unsupported"):
        AR.safe_extract(tar_with([(fifo, None)], tmp_path / "c.tar.gz"), tmp_path / "d3")
    with pytest.raises(AR.ArchiveError, match="Duplicate"):
        AR.safe_extract(tar_with([file_member("t/a"), file_member("t/a")], tmp_path / "e.tar.gz"), tmp_path / "d4")
    with pytest.raises(AR.ArchiveError, match="more than one top-level"):
        AR.safe_extract(tar_with([file_member("t/a"), file_member("u/b")], tmp_path / "f.tar.gz"), tmp_path / "d5")
    (tmp_path / "plain.tar.gz").write_bytes(b"PK\x03\x04 not gzip")
    with pytest.raises(AR.ArchiveError, match="not a gzip"):
        AR.safe_extract(tmp_path / "plain.tar.gz", tmp_path / "d6")
    with pytest.raises(AR.ArchiveError, match="Unexpected top-level"):
        AR.safe_extract(tar_with([file_member("t/a")], tmp_path / "g.tar.gz"), tmp_path / "d7", expected_top="audiobook-studio-9")


def test_archive_bomb_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(AR, "MAX_UNPACKED", 10_000)
    arc = tar_with([file_member("t/a", b"\0" * 6000), file_member("t/b", b"\0" * 6000)], tmp_path / "bomb.tar.gz")
    assert arc.stat().st_size < 1000                       # tiny on disk ...
    with pytest.raises(AR.ArchiveError, match="expands beyond"):
        AR.safe_extract(arc, tmp_path / "d")                # ... refused when it would expand
    assert not (tmp_path / "d").exists()


def test_tree_manifest_detects_modified_and_added_files(tmp_path):
    top = write_tree(tmp_path / "src", "0.5.1")
    (top / "studio" / "__init__.py").write_text('__version__ = "0.5.1"\nimport os; os.system("x")\n', "utf-8")
    with pytest.raises(AR.ArchiveError, match="does not match"):
        AR.verify_tree_manifest(top)
    top = write_tree(tmp_path / "src2", "0.5.1")
    (top / "studio" / "backdoor.py").write_text("x=1\n", "utf-8")
    with pytest.raises(AR.ArchiveError, match="not listed"):
        AR.verify_tree_manifest(top)


# ---------------------------------------------------------------------------------------------------- store


def test_release_store_pointer_rollback_and_prune(tmp_path):
    lay = Layout(tmp_path / "root", tmp_path / "logs")
    lay.ensure()
    store = ReleaseStore(lay)
    for v in ("0.5.0", "0.5.1", "0.5.2", "0.5.3"):
        write_tree(lay.releases, v)          # writes audiobook-studio-<v>; rename to bare version dir
        os.replace(lay.releases / f"audiobook-studio-{v}", lay.releases / v)
    assert store.installed() == ["0.5.0", "0.5.1", "0.5.2", "0.5.3"]
    assert store.activate("0.5.0") is None and store.current() == "0.5.0"
    assert store.activate("0.5.1") == "0.5.0" and store.previous() == "0.5.0"
    store.activate("0.5.3")
    assert store.previous() == "0.5.1" and lay.current_version() == "0.5.3"
    with pytest.raises(ValueError):
        store.remove("0.5.3")
    gone = store.prune(keep=2)
    assert set(store.installed()) == {"0.5.1", "0.5.3"} and "0.5.0" in gone
    with pytest.raises(FileNotFoundError):
        store.activate("9.9.9")
    with pytest.raises(ValueError):
        lay.release_dir("../etc")
    if (lay.current_link).is_symlink():
        assert Path(os.readlink(lay.current_link)).name == "0.5.3"


# --------------------------------------------------------------------------------------------------- runtime


def test_runtime_is_built_with_wheels_only_and_bound_to_signed_lock(tmp_path):
    lay = Layout(tmp_path / "root", tmp_path / "logs")
    lay.ensure()
    tree = tmp_path / "rel"
    tree.mkdir()
    lock = tree / "requirements.lock"
    lock.write_text("numpy==2.0.0\n")
    rid = lock_id(lock)
    calls = []

    def runner(argv, env):
        calls.append((argv, env))
        if "venv" in argv:
            py = Path(argv[-1]) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_text("")
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    with pytest.raises(RuntimeError_, match="uv is missing"):
        ensure_runtime(lay, tree, rid, runner=runner)
    (lay.tools_bin / "uv").write_text("")
    with pytest.raises(RuntimeError_, match="does not match"):
        ensure_runtime(lay, tree, "0" * 16, runner=runner)
    py = ensure_runtime(lay, tree, rid, runner=runner)
    assert py.exists() and (lay.runtimes / rid).exists()
    flat = " ".join(" ".join(a) for a, _ in calls)
    assert "--only-binary :all:" in flat and "brew" not in flat and "xcode" not in flat.lower()
    assert all(e.get("UV_PYTHON_PREFERENCE") == "only-managed" for _, e in calls)

    def failing(argv, env):
        return type("R", (), {"returncode": 1, "stdout": "", "stderr": "error: no matching wheel for foo"})()

    (lay.runtimes / rid).rename(lay.runtimes / "gone")
    with pytest.raises(RuntimeError_, match="packaging defect"):
        ensure_runtime(lay, tree, rid, runner=failing)
    assert not (lay.runtimes / f".build-{rid}").exists()


# ------------------------------------------------------------------------------------------------ platforms


class Rec:
    """Fake launchctl."""

    def __init__(self, loaded=True, running=True):
        self.calls, self.loaded, self.running = [], loaded, running

    def __call__(self, argv):
        self.calls.append(argv)
        out, rc = "", 0
        if argv[1] == "print":
            rc = 0 if (self.loaded or argv[2].startswith("gui/")) and (self.loaded or len(argv[2].split("/")) == 2) else 113
            out = "state = running\n" if self.running else "state = waiting\n"
        return type("R", (), {"returncode": rc, "stdout": out, "stderr": ""})()


def test_launchd_agent_is_per_user_and_safe(tmp_path):
    from studio.platform_services import MacPlatformService, build_plist

    spec = ServiceSpec("com.audiobookstudio.app", ["/Users/u/Library/Application Support/Audiobook Studio/app/runtimes/r/venv/bin/python", "-m", "studio", "serve"],
                       "/Users/u/Library/Application Support/Audiobook Studio/app/current", {"AUDIOBOOK_STUDIO_HOME": "/x"}, "/Users/u/Library/Logs/Audiobook Studio/launchd.out.log", 7861)
    p = build_plist(spec)
    assert p["RunAtLoad"] is True and p["KeepAlive"] == {"SuccessfulExit": False} and p["ThrottleInterval"] == 10
    assert p["ProgramArguments"][0].startswith("/") and p["WorkingDirectory"].startswith("/")   # absolute paths only
    assert "UserName" not in p and "GroupName" not in p        # never a root/system daemon
    rec = Rec()
    svc = MacPlatformService(runner=rec, agents_dir=tmp_path / "LaunchAgents", uid=501)
    svc.install_service(spec)
    written = plistlib.loads((tmp_path / "LaunchAgents" / "com.audiobookstudio.app.plist").read_bytes())
    assert written["ProgramArguments"][0].endswith("/bin/python") and "Application Support" in written["WorkingDirectory"]
    flat = [" ".join(c) for c in rec.calls]
    assert any(c.startswith("/bin/launchctl bootstrap gui/501 ") for c in flat)
    assert not any("sudo" in c or "/Library/LaunchDaemons" in c or "system" in c.split() for c in flat)
    rec.calls.clear()
    svc.stop(spec.label)
    svc.restart(spec.label)
    svc.start(spec.label)
    flat = [" ".join(c) for c in rec.calls]
    assert "/bin/launchctl bootout gui/501/com.audiobookstudio.app" in flat
    assert "/bin/launchctl kickstart -k gui/501/com.audiobookstudio.app" in flat
    assert svc.is_running(spec.label) is True
    svc.uninstall_service(spec.label)
    assert not (tmp_path / "LaunchAgents" / "com.audiobookstudio.app.plist").exists()


def test_launch_detached_uses_a_separate_one_shot_job(tmp_path):
    from studio.platform_services import MacPlatformService

    rec = Rec(loaded=False)
    svc = MacPlatformService(runner=rec, agents_dir=tmp_path / "la", uid=501)
    svc.launch_detached("com.audiobookstudio.updater", ["/py", "-m", "studio.update.helper"], "/cwd", {"A": "1"}, str(tmp_path / "u.log"), tmp_path / "pl")
    pl = plistlib.loads((tmp_path / "pl" / "com.audiobookstudio.updater.plist").read_bytes())
    assert pl["KeepAlive"] is False and pl["AbandonProcessGroup"] is True and pl["ProgramArguments"][0] == "/py"


def test_process_platform_service_lifecycle(tmp_path):
    import sys

    from studio.platform_services import ProcessPlatformService

    svc = ProcessPlatformService(tmp_path / "svc")
    spec = ServiceSpec("t", [sys.executable, "-c", "import time; time.sleep(60)"], str(tmp_path), {}, str(tmp_path / "t.log"), 1)
    svc.install_service(spec)
    assert not svc.is_running("t")
    svc.start("t")
    svc.start("t")                                   # idempotent: no duplicate process
    assert svc.is_running("t")
    pid = int((tmp_path / "svc" / "t.pid").read_text())
    svc.restart("t")
    assert svc.is_running("t") and int((tmp_path / "svc" / "t.pid").read_text()) != pid
    svc.stop("t")
    assert not svc.is_running("t")


# ------------------------------------------------------------------------------- interfaces and providers


def test_interfaces_are_real_contracts():
    for abc in (PlatformService, TTSProvider, ASRProvider, InferenceProvider, ModelRegistry, ReleaseRepository, UpdateManager, StorageManager, QueueScheduler):
        with pytest.raises(TypeError):
            abc()                                    # abstract: cannot be instantiated
    from studio.platform_services import MacPlatformService, ProcessPlatformService
    from studio.scheduler import FileQueueScheduler
    from studio.update.manager import ReleaseUpdateManager

    assert issubclass(MacPlatformService, PlatformService) and issubclass(ProcessPlatformService, PlatformService)
    assert issubclass(FileQueueScheduler, QueueScheduler) and issubclass(ReleaseUpdateManager, UpdateManager)
    assert issubclass(GitHubReleasesRepository, ReleaseRepository) and issubclass(HttpsRepository, ReleaseRepository)


def test_provider_registry_and_storage_manager(ctx, tmp_path):
    from studio.engines import MockASR, MockEngine
    from studio.providers import MockASRProvider, MockTTSProvider, SettingsModelRegistry, StudioStorageManager, usable_providers

    reg = SettingsModelRegistry(ctx)
    roles = {m.role for m in reg.list_models()}
    assert roles == {"tts_custom_voice", "tts_clone", "tts_voice_design", "asr"}
    assert reg.resolve("asr").provider_id == "mock-asr" and reg.resolve("tts_clone").provider_id == "mock-tts"   # engine_mode is mock in tests
    with pytest.raises(KeyError):
        reg.resolve("nope")
    assert "mock-tts" in usable_providers("tts") and "mock-asr" in usable_providers("asr")
    tts = MockTTSProvider(MockEngine())
    assert tts.availability() == (True, "") and tts.capabilities()["synthetic_tones"]
    assert MockASRProvider(MockASR()).capabilities()["synthetic"]
    sm = StudioStorageManager(ctx)
    ok, why = sm.check_available(ctx.library_root())
    assert sm.model_cache().name == "huggingface" or ctx.settings().models.cache_dir
    ok, why = sm.check_available(Path("/Volumes/AudioSSD/books"))
    assert ok is False and "AudioSSD" in why and "not mounted" in why
    assert sm.free_bytes(tmp_path / "does" / "not" / "exist") > 0


def test_model_cache_setting_is_applied_outside_releases(ctx, tmp_path, monkeypatch):
    monkeypatch.delenv("HF_HOME", raising=False)
    s = ctx.settings().model_copy(deep=True)
    s.models.cache_dir = str(tmp_path / "SSD" / "hf cache")
    ctx.save_settings(s)
    assert ctx.apply_model_cache_env() == str(tmp_path / "SSD" / "hf cache")


def test_building_the_app_never_writes_outside_its_own_home(ctx, tmp_path, monkeypatch):
    """Constructing the web app (which wires the update manager and platform service) must not create folders in the user's real home."""
    from studio.web.app import create_app

    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    for var in ("HOME", "USERPROFILE", "XDG_DATA_HOME"):
        monkeypatch.setenv(var, str(fake_home / ("xdg" if var == "XDG_DATA_HOME" else "")))
    monkeypatch.delenv("AUDIOBOOK_STUDIO_ROOT", raising=False)
    create_app(ctx, start_worker=False)
    assert not list(fake_home.rglob("*")) or all(p.name in ("xdg",) for p in fake_home.rglob("*"))
