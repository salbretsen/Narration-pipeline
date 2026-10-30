"""Release notes come from CHANGELOG.md (tools/release_notes.py), and every release must have an entry."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from studio import __version__

_spec = importlib.util.spec_from_file_location("release_notes", Path(__file__).resolve().parents[3] / "tools" / "release_notes.py")
rn = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rn)

SAMPLE = "# Changelog\n\nintro\n\n## v0.7.0\n- new thing\n\n## v0.6.1\n- fix\n- more\n\n## v0.6.0\n- feature\n"


def test_section_extraction_and_fallback():
    assert rn.section(SAMPLE, "v0.6.1") == "- fix\n- more\n"
    assert rn.section(SAMPLE, "0.7.0") == "- new thing\n"
    assert rn.section(SAMPLE, "v0.7.0-beta.2") == "- new thing\n"   # beta tags fall back to the release section
    assert rn.section(SAMPLE, "v9.9.9") is None
    assert rn.section(SAMPLE, "v0.6") is None                       # no prefix matching


def test_every_changelog_version_has_notes_and_current_version_is_covered():
    text = rn.CHANGELOG.read_text("utf-8")
    assert rn.section(text, f"v{__version__}"), "add a CHANGELOG.md section for the current version before tagging it"
    import re
    for v in re.findall(r"^## (v\d+\.\d+\.\d+)\s*$", text, re.M):
        assert rn.section(text, v), f"empty CHANGELOG section for {v}"


GAP = ("# Changelog\n\n## v0.8.2\n- notes fix\n\n## v0.8.1\n- license\n\n## v0.8.0\n- polish\n\n## v0.7.0\n- versions\n\n"
       "## v0.6.2\n- notes from changelog\n")


def test_since_includes_every_unpublished_version_newest_first():
    out = rn.notes(GAP, "v0.8.1", "v0.6.2")
    assert out.startswith("This release also contains v0.8.0 and v0.7.0, which were not published separately")
    assert out.index("## v0.8.1\n- license") < out.index("## v0.8.0\n- polish") < out.index("## v0.7.0\n- versions")
    assert "notes from changelog" not in out and "notes fix" not in out
    assert rn.notes(GAP, "v0.8.2", "v0.8.1") == "- notes fix\n"            # nothing skipped: same text as before
    assert rn.notes(GAP, "v0.8.2") == "- notes fix\n"
    assert "contains v0.8.1, which was" in rn.notes(GAP, "v0.8.2", "v0.8.0")
    assert rn.notes(GAP, "v9.9.9", "v0.6.2") is None


def test_previous_published_release_follows_the_channel():
    rels = [{"tagName": "v0.8.1", "isPrerelease": False}, {"tagName": "v0.6.2", "isPrerelease": False},
            {"tagName": "v0.8.0", "isPrerelease": True},                   # an unsigned build: not a published stable release
            {"tagName": "v0.9.0-beta.1", "isPrerelease": True}, {"tagName": "index-stable", "isPrerelease": True}]
    assert rn.previous_published("v0.8.2", rels) == "v0.8.1"
    assert rn.previous_published("v0.8.1", rels) == "v0.6.2"
    assert rn.previous_published("v0.6.2", rels) is None
    assert rn.previous_published("v0.9.0-beta.2", rels) == "v0.9.0-beta.1"
    assert rn.previous_published("v0.9.0", rels) == "v0.8.1"


def test_signed_index_notes_are_filled_from_the_changelog(monkeypatch, tmp_path):
    pytest.importorskip("cryptography")
    spec = importlib.util.spec_from_file_location("sign_release", Path(__file__).resolve().parents[3] / "tools" / "sign_release.py")
    sr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sr)
    (tmp_path / "CHANGELOG.md").write_text(GAP, "utf-8")
    monkeypatch.setattr(rn, "CHANGELOG", tmp_path / "CHANGELOG.md")
    import sys
    monkeypatch.setitem(sys.modules, "release_notes", rn)
    ordered = [{"version": "0.8.2", "notes": ""}, {"version": "0.8.1", "notes": ""}, {"version": "0.6.2", "notes": "kept?"},
               {"version": "0.5.0", "notes": "no section: kept"}]
    sr.fill_notes(ordered)
    assert ordered[0]["notes"] == "- notes fix\n"
    assert "v0.8.0 and v0.7.0" in ordered[1]["notes"] and "## v0.7.0" in ordered[1]["notes"]
    assert ordered[2]["notes"] == "- notes from changelog\n" and ordered[3]["notes"] == "no section: kept"
