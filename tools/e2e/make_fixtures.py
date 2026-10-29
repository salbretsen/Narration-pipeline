"""Create the fixture files the e2e scripts need: smoke_text.txt and fake-mic.wav.

    python tools/e2e/make_fixtures.py OUTDIR
"""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root / "src" / "app"))
sys.path.insert(0, str(root / "src" / "app" / "tests"))
from conftest import make_speechlike_wav  # noqa: E402
from studio.smoke import SMOKE_TEXT  # noqa: E402

out = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
out.mkdir(parents=True, exist_ok=True)
(out / "smoke_text.txt").write_text(SMOKE_TEXT, encoding="utf-8")
(out / "fake-mic.wav").write_bytes(make_speechlike_wav(30.0, sr=48000, amp=0.2, noise_db=-68.0))
print("wrote", out / "smoke_text.txt", "and", out / "fake-mic.wav")
