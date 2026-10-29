"""Release notes come from CHANGELOG.md (tools/release_notes.py), and every release must have an entry."""
from __future__ import annotations

import importlib.util
from pathlib import Path

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
