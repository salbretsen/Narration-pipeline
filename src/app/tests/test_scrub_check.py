"""tools/scrub_check.py detects private network details, personal paths and private keys."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location("scrub_check", Path(__file__).resolve().parents[3] / "tools" / "scrub_check.py")
sc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sc)


# Built from pieces so the repo-wide key-material test does not trip over this file.
DASHES = "-" * 5


def kinds(text):
    return [k for _n, _l, k, _t in sc.scan_text(text)]


def test_detects_private_ips():
    for ip in ("192.168.7.42", "10.4.5.6", "172.16.0.9", "172.31.255.1"):
        assert kinds(f"ssh admin@{ip}") == ["private IPv4"], ip


def test_ignores_public_loopback_and_placeholders():
    for t in ("127.0.0.1:7861", "0.0.0.0", "172.32.0.1", "172.15.0.1", "8.8.8.8", "192.168.1.1", "version 110.1.2.3.4"):
        assert kinds(t) == [], t


def test_detects_real_hostnames_but_not_examples():
    assert kinds("open http://scotts-macbook.local:8443") == ["private hostname"]
    assert kinds("nas.home.lan") == ["private hostname"]
    assert kinds("http://example.lan and your-mac.local") == []


def test_detects_home_paths_but_not_placeholders():
    assert kinds(r"C:\Users\jsmith\Documents") == ["home path"]
    assert kinds("C:/Users/jsmith/x") == ["home path"]
    assert kinds("cd /Users/jsmith/audiobook") == ["home path"]
    assert kinds("/home/bob/.ssh") == ["home path"]
    assert kinds(r"C:\Users\<user>\x /Users/<user>/y ~/Users") == []


def test_detects_private_key_blocks():
    assert kinds(DASHES + "BEGIN " + "PRIVATE KEY" + DASHES) == ["private key block"]
    assert kinds(DASHES + "BEGIN OPENSSH " + "PRIVATE KEY" + DASHES) == ["private key block"]
    assert kinds(DASHES + "BEGIN PUBLIC KEY" + DASHES) == []


def test_reports_line_numbers():
    assert sc.scan_text("ok\nok\nhost 10.1.1.1", "f.txt") == [("f.txt", 3, "private IPv4", "10.1.1.1")]
