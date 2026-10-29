"""Unit tests: acoustic QA, WAV I/O, Range parsing, path validation, settings."""
from __future__ import annotations

import os
import sys

import numpy as np
import pytest

from studio import audio as A
from studio.config import Settings
from studio.storage import validate_dir
from studio.web.media import RangeNotSatisfiable, parse_range

QA = Settings().qa.model_dump()
SR = 24000


def speechlike(seconds=3.0, amp=0.2):
    t = np.arange(int(SR * seconds)) / SR
    env = (np.sin(2 * np.pi * 2.5 * t) > -0.3).astype(np.float32)
    return (amp * env * np.sin(2 * np.pi * 170 * t)).astype(np.float32)


def test_clean_take_has_no_errors():
    r = A.analyze(speechlike(), SR, 3.0, QA)
    assert not [i for i in r["issues"] if i["severity"] == "error"], r


def test_detects_empty_silent_clipping_dead_air_and_duration():
    assert any(i["kind"] == "empty" for i in A.analyze(np.zeros(0), SR, 1, QA)["issues"])
    assert any(i["kind"] == "silent" for i in A.analyze(np.zeros(SR * 2), SR, 2, QA)["issues"])
    clipped = np.clip(speechlike() * 20, -1, 1)
    assert any(i["kind"] == "clipping" and i["severity"] == "error" for i in A.analyze(clipped, SR, 3, QA)["issues"])
    gap = np.concatenate([speechlike(1.5), np.zeros(SR * 3, dtype=np.float32), speechlike(1.5)])
    dead = [i for i in A.analyze(gap, SR, 3, QA)["issues"] if i["kind"] == "dead_air"]
    assert dead and 1.0 < dead[0]["at_s"] < 2.2
    assert any(i["kind"] == "too_short" for i in A.analyze(speechlike(1.0), SR, 10.0, QA)["issues"])
    assert any(i["kind"] == "too_long" for i in A.analyze(speechlike(9.0), SR, 2.0, QA)["issues"])
    quiet = speechlike(amp=0.001)
    assert any(i["kind"] in ("too_quiet", "silent") for i in A.analyze(quiet, SR, 3.0, QA)["issues"])
    nan = speechlike()
    nan[100] = np.nan
    assert any(i["kind"] == "corrupt" for i in A.analyze(nan, SR, 3.0, QA)["issues"])


def test_wav_roundtrip_and_trim(tmp_path):
    a = np.concatenate([np.zeros(SR), speechlike(1.0), np.zeros(SR)])
    p = A.write_wav(tmp_path / "x.wav", a, SR)
    b, sr = A.read_wav(p)
    assert sr == SR and b.size == a.size and np.max(np.abs(a - b)) < 1e-3
    t = A.trim_silence(b, SR, keep_ms=50)
    assert 0.9 * SR < t.size < 1.3 * SR
    assert not list(tmp_path.glob("*.part*"))  # atomic write leaves no temp files


def test_sniff_audio():
    from conftest import make_wav_bytes

    assert A.sniff_audio(make_wav_bytes(1), ".wav")
    assert not A.sniff_audio(b"hello world, not audio", ".wav")
    assert A.sniff_audio(b"fLaC" + b"\0" * 20, ".flac")
    assert A.sniff_audio(b"\0\0\0\x20ftypM4A " + b"\0" * 8, ".m4a")


@pytest.mark.parametrize("hdr,expect", [
    (None, None), ("bytes=0-99", (0, 99)), ("bytes=100-", (100, 999)), ("bytes=-100", (900, 999)),
    ("bytes=0-5000", (0, 999)), ("items=0-1", None), ("bytes=5-1,7-9", "416"), ("bytes=2000-", "416"), ("bytes=-0", "416"),
])
def test_parse_range(hdr, expect):
    if expect == "416":
        with pytest.raises(RangeNotSatisfiable):
            parse_range(hdr, 1000)
    else:
        assert parse_range(hdr, 1000) == expect


def test_validate_dir(tmp_path):
    ok = validate_dir(str(tmp_path / "lib"), None)
    assert ok["ok"] and not ok["exists"] and ok["writable"]
    created = validate_dir(str(tmp_path / "lib2"), None, create=True)
    assert created["ok"] and created["exists"] and os.path.isdir(tmp_path / "lib2")
    assert not validate_dir("relative/path", None)["ok"]
    assert not validate_dir(str(tmp_path / ".." / "x"), None)["ok"]
    assert not validate_dir("", None)["ok"]
    assert not validate_dir("/tmp/a\x00b", None)["ok"]
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert not validate_dir(str(f), None)["ok"]
    if sys.platform != "win32":
        assert not validate_dir("/etc/studio", None)["ok"]
        assert not validate_dir("/", None)["ok"]
        assert not validate_dir("/Volumes/DefinitelyNotMountedDrive123/Library", None)["ok"]


def test_settings_validation():
    with pytest.raises(Exception):
        Settings.model_validate({"chunking": {"min_chars": 500, "max_chars": 300}})
    with pytest.raises(Exception):
        Settings.model_validate({"server": {"host": "0.0.0.0"}})  # remote binding requires explicit opt-in
    with pytest.raises(Exception):
        Settings.model_validate({"models": {"asr_model": "not a repo id"}})
    s = Settings.model_validate({"qa": {"max_attempts": 5}, "unknown_key": 1})
    assert s.qa.max_attempts == 5 and s.chunking.max_chars == 400
