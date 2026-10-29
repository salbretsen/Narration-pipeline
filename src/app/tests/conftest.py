"""Shared fixtures. All tests use the MOCK engine (synthetic test tones) - never MLX inference.
FFmpeg-dependent tests are skipped when ffmpeg is not installed."""
from __future__ import annotations

import io
import shutil
import sys
import wave
import zipfile
from pathlib import Path

import numpy as np
import pytest

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "studio-home"
    monkeypatch.setenv("AUDIOBOOK_STUDIO_HOME", str(h))
    monkeypatch.setenv("STUDIO_ENGINE", "mock")
    monkeypatch.setenv("STUDIO_NO_WORKER", "1")
    monkeypatch.delenv("STUDIO_MOCK_FAULTS", raising=False)
    return h


@pytest.fixture
def ctx(home):
    from studio.context import Context
    from studio.services import ensure_builtin_voices

    c = Context(home)
    ensure_builtin_voices(c)
    return c


@pytest.fixture
def client(ctx):
    from fastapi.testclient import TestClient

    from studio.web.app import create_app

    app = create_app(ctx, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:7861", headers={"X-Studio-Request": "1"}) as c:
        yield c


@pytest.fixture
def make_worker(ctx):
    from studio.engines import MockASR, MockEngine
    from studio.worker import Worker

    made = []

    def make(faults=None, take_over=False):
        if take_over:
            with ctx.db.tx() as conn:
                conn.execute("UPDATE worker_state SET heartbeat_at=0 WHERE id=1")
        w = Worker(ctx, tts=MockEngine(faults or {}), asr=MockASR())
        assert w.acquire_lease(), "could not acquire the worker lease"
        made.append(w)
        return w

    return make


# ------------------------------------------------------------------ fixtures

SENTENCES = [
    "The harbour lights trembled on the black water while the last ferry pulled away from the quay.",
    "Eleanor counted the steps to the lighthouse door and found, as always, that there were ninety-two.",
    "Nobody in the village remembered who had planted the orchard, but everyone remembered the storm of 1987.",
    "She folded the letter twice, slid it into her coat, and walked out into the rain without an umbrella.",
    "The kettle sang, the clock ticked, and somewhere upstairs a door closed very softly.",
    "It cost $12.50 to cross the bridge, which Thomas considered an outrage and a personal insult.",
    "By the 3rd of March the ice had gone from the river and the herons had come back.",
    "“We should leave before dark,” said Mr. Hale, though he made no move towards the door.",
]


def make_text(n_chapters: int = 2, paras: int = 3, marker: str | None = None, title: str = "A Test Book") -> str:
    out = [title, ""]
    k = 0
    for c in range(1, n_chapters + 1):
        out += [f"Chapter {c}", ""]
        for p in range(paras):
            sents = [SENTENCES[(k + i) % len(SENTENCES)] for i in range(3)]
            k += 3
            if marker and c == n_chapters and p == paras - 1:
                sents.append(f"The ship was bound for {marker} and would not return before winter.")
            out += [" ".join(sents), ""]
        out += ["* * *", "", "Short reply.", "", "Another brief line of dialogue.", ""]
    return "\n".join(out)


def make_docx(chapters: list[tuple[str, list[str]]], title: str = "Docx Book") -> bytes:
    W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = [f'<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>{title}</w:t></w:r></w:p>']
    for t, paras in chapters:
        body.append(f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>{t}</w:t></w:r></w:p>')
        for p in paras:
            body.append(f'<w:p><w:r><w:t xml:space="preserve">{p}</w:t></w:r><w:r><w:footnoteReference w:id="1"/></w:r></w:p>')
        body.append("<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Table cell text</w:t></w:r></w:p></w:tc></w:tr></w:tbl>")
    doc = f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{"".join(body)}</w:body></w:document>'
    styles = (f'<?xml version="1.0"?><w:styles {W}><w:style w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>'
              f'<w:style w:styleId="Title"><w:name w:val="Title"/></w:style></w:styles>')
    core = ('<?xml version="1.0"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
            f'xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{title}</dc:title><dc:creator>Jane Author</dc:creator></cp:coreProperties>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", styles)
        z.writestr("docProps/core.xml", core)
    return buf.getvalue()


def make_epub(chapters: list[tuple[str, list[str]]], title: str = "Epub Book", reverse_manifest: bool = True, drm: bool = False) -> bytes:
    """Manifest order is deliberately reversed vs. the spine to prove spine order wins."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip")
        z.writestr("META-INF/container.xml", '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        if drm:
            z.writestr("META-INF/encryption.xml", '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container" xmlns:enc="http://www.w3.org/2001/04/xmlenc#">'
                       '<enc:EncryptedData><enc:EncryptionMethod Algorithm="http://www.w3.org/2001/04/xmlenc#aes128-cbc"/>'
                       '<enc:CipherData><enc:CipherReference URI="OEBPS/ch1.xhtml"/></enc:CipherData></enc:EncryptedData></encryption>')
        items, spine = [], []
        for i, (t, paras) in enumerate(chapters, start=1):
            body = "".join(f"<p>{p}<a epub:type=\"noteref\" href=\"#n{i}\">{i}</a></p>" for p in paras)
            x = (f'<?xml version="1.0" encoding="utf-8"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
                 f'<head><title>{t}</title><style>p{{margin:0}}</style></head><body><section><h1>{t}</h1>{body}'
                 f'<img src="x.png" alt="A map of the harbour"/><aside epub:type="footnote" id="n{i}"><p>Footnote text {i}.</p></aside></section></body></html>')
            z.writestr(f"OEBPS/ch{i}.xhtml", x)
            items.append(f'<item id="ch{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="ch{i}"/>')
        z.writestr("OEBPS/nav.xhtml", '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><body>'
                   '<nav epub:type="toc"><ol>' + "".join(f'<li><a href="ch{i}.xhtml">{t}</a></li>' for i, (t, _) in enumerate(chapters, 1)) + '</ol></nav></body></html>')
        z.writestr("OEBPS/notes.xhtml", '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Notes</h1><p>Auxiliary note content that is not linear.</p></body></html>')
        if reverse_manifest:
            items = list(reversed(items))
        opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
               f'<dc:title>{title}</dc:title><dc:creator>Ann Writer</dc:creator><dc:language>en</dc:language></metadata><manifest>'
               '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
               '<item id="notes" href="notes.xhtml" media-type="application/xhtml+xml"/>' + "".join(items) + "</manifest><spine>"
               '<itemref idref="nav"/>' + "".join(spine) + '<itemref idref="notes" linear="no"/></spine></package>')
        z.writestr("OEBPS/content.opf", opf)
    return buf.getvalue()


def make_wav_bytes(seconds: float = 5.0, sr: int = 24000, amp: float = 0.3, silent: bool = False) -> bytes:
    t = np.arange(int(seconds * sr)) / sr
    if silent:
        a = np.zeros_like(t)
    else:
        env = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t) ** 2
        a = amp * env * (np.sin(2 * np.pi * 180 * t) + 0.4 * np.sin(2 * np.pi * 360 * t))
    pcm = (np.clip(a, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


CHAPTERS = [
    ("The Harbour", [SENTENCES[0] + " " + SENTENCES[1], SENTENCES[2] + " " + SENTENCES[3]]),
    ("The Orchard", [SENTENCES[4] + " " + SENTENCES[5], SENTENCES[6]]),
    ("The Storm", [SENTENCES[7] + " " + SENTENCES[0]]),
]


def make_speechlike_wav(seconds: float = 10.0, sr: int = 24000, amp: float = 0.25, noise_db: float = -66.0, pauses: bool = True) -> bytes:
    """Voice-like bursts separated by short pauses, half a second of lead-in/tail, and a background noise floor."""
    rng = np.random.default_rng(7)
    n = int(seconds * sr)
    t = np.arange(n) / sr
    a = np.zeros(n)
    voiced = (t > 0.5) & (t < seconds - 0.5)
    if pauses:
        voiced &= (np.sin(2 * np.pi * 0.45 * t) > -0.55)
    env = (0.55 + 0.45 * np.sin(2 * np.pi * 3.2 * t) ** 2) * voiced
    a += amp * env * (np.sin(2 * np.pi * 150 * t) + 0.4 * np.sin(2 * np.pi * 300 * t) + 0.2 * np.sin(2 * np.pi * 450 * t))
    a += rng.standard_normal(n) * (10 ** (noise_db / 20))
    pcm = (np.clip(a, -1, 1) * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()
