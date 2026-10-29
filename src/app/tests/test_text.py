"""Unit tests: parsing, chunking, normalisation and alignment."""
from __future__ import annotations

import random
import re

import pytest

from conftest import CHAPTERS, SENTENCES, make_docx, make_epub, make_text
from studio.align import evaluate
from studio.chunker import build_segments, chunk_paragraph, normalize_ws, split_sentences
from studio.normalize import compare_tokens, int_to_words, ordinal_words, to_speech, year_to_words
from studio.parsing import ManuscriptError, parse_manuscript

# ------------------------------------------------------------------ parsing


def test_txt_chapters_title_and_scene_breaks():
    r = parse_manuscript("book.txt", make_text(3).encode())
    assert r.title == "A Test Book"
    assert [c.title for c in r.chapters] == ["Chapter 1", "Chapter 2", "Chapter 3"]
    assert all(c.include for c in r.chapters)
    kinds = [b.kind for b in r.chapters[0].blocks]
    assert "scene_break" in kinds
    # the book title is kept in the manifest as not-read, not silently dropped
    assert any(b.reason.startswith("book title") for b in r.chapters[0].skipped())


def test_txt_without_headings_warns_single_chapter():
    r = parse_manuscript("x.txt", ("Just text. " * 30).encode())
    assert len(r.chapters) == 1
    assert any("No chapter headings" in w for w in r.warnings)


def test_txt_cp1252_fallback_and_binary_rejected():
    r = parse_manuscript("x.txt", "Caf\xe9 au lait was served at nine. ".encode("cp1252") * 5)
    assert any("Windows-1252" in w for w in r.warnings)
    with pytest.raises(ManuscriptError):
        parse_manuscript("x.txt", b"\x00\x01\x02binary" * 100)


def test_markdown_structure():
    md = "---\ntitle: MD Book\nauthor: Someone\n---\n# MD Book\n\n## One\n\nHello **world**, this is [a link](http://x).\n\n![map](m.png)\n\n```\ncode()\n```\n\n## Two\n\n| a | b |\n\nText two is here.\n"
    r = parse_manuscript("b.md", md.encode())
    assert r.title == "MD Book" and r.author == "Someone"
    assert [c.title for c in r.chapters] == ["One", "Two"]
    one = r.chapters[0]
    assert one.readable()[0].text == "Hello world, this is a link."
    reasons = {b.reason for c in r.chapters for b in c.skipped()}
    assert {"image", "code block", "table row"} <= reasons


def test_docx_headings_tables_footnotes():
    data = make_docx([("Chapter One", ["First paragraph text here."]), ("Chapter Two", ["Second paragraph."])])
    r = parse_manuscript("b.docx", data)
    assert r.title == "Docx Book" and r.author == "Jane Author"
    assert [c.title for c in r.chapters] == ["Chapter One", "Chapter Two"]
    assert any(b.reason == "table" for b in r.chapters[0].skipped())
    assert any("footnote" in w for w in r.warnings)


def test_epub_spine_order_nav_nonlinear_and_notes():
    r = parse_manuscript("b.epub", make_epub(CHAPTERS))
    titles = [c.title for c in r.chapters]
    # nav doc excluded but listed; spine order (not manifest order); notes are non-linear
    included = [c.title for c in r.chapters if c.include]
    assert included == ["The Harbour", "The Orchard", "The Storm"]
    assert "Notes" in titles and not next(c for c in r.chapters if c.title == "Notes").include
    harbour = next(c for c in r.chapters if c.title == "The Harbour")
    text = " ".join(b.text for b in harbour.readable())
    assert "Footnote text" not in text  # footnote body not read
    assert not re.search(r"quay\.?\s*1\b", text)  # noteref marker not read
    reasons = {b.reason for b in harbour.skipped()}
    assert "image" in reasons and "footnote/endnote" in reasons
    assert r.title == "Epub Book" and r.author == "Ann Writer"


def test_epub_drm_and_corrupt_rejected():
    with pytest.raises(ManuscriptError, match="DRM|encrypted"):
        parse_manuscript("b.epub", make_epub(CHAPTERS, drm=True))
    with pytest.raises(ManuscriptError):
        parse_manuscript("b.epub", b"PK\x03\x04garbage")
    with pytest.raises(ManuscriptError):
        parse_manuscript("b.epub", b"not a zip at all")
    with pytest.raises(ManuscriptError):
        parse_manuscript("b.pdf", b"%PDF")
    with pytest.raises(ManuscriptError):
        parse_manuscript("b.txt", b"")


# ------------------------------------------------------------------ chunking


def test_split_sentences_respects_abbreviations():
    s = split_sentences("Mr. Hale met Dr. Smith at 5 p.m. on Friday. They spoke. J. R. Tolkien wrote it!")
    assert s == ["Mr. Hale met Dr. Smith at 5 p.m. on Friday.", "They spoke.", "J. R. Tolkien wrote it!"]


@pytest.mark.parametrize("seed", range(25))
def test_chunk_invariants_random(seed):
    rnd = random.Random(seed)
    words = []
    for _ in range(rnd.randint(5, 400)):
        w = rnd.choice(SENTENCES).split()
        words.extend(w[: rnd.randint(1, len(w))])
        if rnd.random() < 0.05:
            words.append("x" * rnd.randint(1, 30))
    para = " ".join(words)
    chunks = chunk_paragraph(para, 150, 400)
    assert " ".join(chunks) == normalize_ws(para), "text must be preserved exactly"
    for c in chunks:
        assert len(c) <= 400 or " " not in c
        assert c == c.strip() and c
    original_words = normalize_ws(para).split(" ")
    assert sum(len(c.split(" ")) for c in chunks) == len(original_words)  # no word split


def test_build_segments_merges_short_and_marks_pauses():
    from studio.parsing import Block

    blocks = [Block("heading", "Part One", level=2), Block("paragraph", "Yes."), Block("paragraph", "No."),
              Block("paragraph", SENTENCES[0] * 3), Block("scene_break", ""), Block("paragraph", "End.")]
    segs = build_segments(blocks, 150, 400, True)
    assert segs[0].kind == "heading" and segs[0].pause_kind == "heading"
    assert segs[1].text == "Yes. No."
    assert any(s.pause_kind == "scene" for s in segs)
    assert segs[-1].pause_kind == "chapter_end"
    joined = " ".join(s.text for s in segs if s.kind == "body")
    assert joined == normalize_ws("Yes. No. " + SENTENCES[0] * 3 + " End.")


# ------------------------------------------------------------- normalisation


def test_number_words():
    assert int_to_words(1250) == "one thousand two hundred fifty"
    assert ordinal_words(21) == "twenty-first"
    assert ordinal_words(12) == "twelfth"
    assert year_to_words(1987) == "nineteen eighty-seven"
    assert year_to_words(2005) == "two thousand five"
    assert year_to_words(1905) == "nineteen oh five"


def test_to_speech_keeps_meaning():
    s = to_speech("Dr. Smith paid $12.50 on the 3rd of May, 1999 at 3:30 & left. See e.g. Chapter IV [12].")
    assert "Doctor Smith" in s and "twelve dollars and fifty cents" in s and "third" in s
    assert "nineteen ninety-nine" in s and "three thirty" in s and " and left" in s
    assert "for example" in s and "Chapter four" in s and "[12]" not in s


@pytest.mark.parametrize("a,b", [
    ("In 1999 he paid $1,250.50.", "In nineteen ninety-nine he paid one thousand two hundred fifty dollars and fifty cents."),
    ("He won't go; it's late!", "he will not go it is late"),
    ("Mr. Hale, the grey-haired man.", "Mister Hale the gray haired man"),
    ("On the 21st of March", "on the twenty-first of march"),
    ("The 1990s were 50% better", "the nineteen nineties were fifty percent better"),
    ("At 3:30 we left", "at three thirty we left"),
])
def test_compare_equivalences(a, b):
    assert compare_tokens(a) == compare_tokens(b)


# ------------------------------------------------------------------ alignment


def test_alignment_clean_and_punctuation_insensitive():
    ev = evaluate("Hello, world! It's 1999.", "hello world it is nineteen ninety nine")
    assert ev["wer"] == 0 and not [i for i in ev["issues"] if i["severity"] == "error"]


def test_alignment_detects_dropped_run():
    ref = "the quick brown fox jumps over the lazy dog near the old stone bridge"
    hyp = "the quick brown fox near the old stone bridge"
    ev = evaluate(ref, hyp)
    kinds = {i["kind"] for i in ev["issues"] if i["severity"] == "error"}
    assert "dropped" in kinds and ev["counts"]["del"] == 5


def test_alignment_detects_repetition_and_hallucination():
    ref = "she walked into the room and sat down by the fire"
    ev = evaluate(ref, "she walked into the room into the room and sat down by the fire")
    assert any(i["kind"] == "repetition" and i["severity"] == "error" for i in ev["issues"])
    ev2 = evaluate(ref, ref + " thank you for watching")
    assert any(i["kind"] == "hallucination" for i in ev2["issues"])
    ev3 = evaluate(ref, "she walked into the room and sat")
    assert any(i["kind"] == "truncated" for i in ev3["issues"])


def test_alignment_soft_name_substitution_weighted():
    ev = evaluate("Katherine opened the theatre door", "Catherine opened the theater door")
    assert ev["wer"] < 0.12  # near-identical names are weighted lightly (false-positive control)
    assert ev["wer_raw"] > ev["wer"]


def test_alignment_locates_issue_with_word_times():
    words = [{"word": w, "start": i * 0.5, "end": i * 0.5 + 0.4} for i, w in enumerate("one two three seven eight".split())]
    ev = evaluate("one two three four five six seven eight", "one two three seven eight", words)
    drop = next(i for i in ev["issues"] if i["kind"] == "dropped")
    assert drop["at_s"] == 1.0


def test_short_segment_single_error_is_not_a_wer_flag(ctx):
    """One misheard word in a 5-word heading is 20% WER; it must not flag on WER alone,
    while two errors still do."""
    from studio import services as S
    from studio.db import row
    from studio.engines import MockASR, MockEngine
    from studio.worker import Worker

    class Mishear(MockASR):
        def __init__(self, heard):
            super().__init__()
            self.heard = heard

        def transcribe(self, wav_path, language="en"):
            out = super().transcribe(wav_path, language)
            if "second light" in out["text"].lower():
                out["text"] = self.heard
            return out

    v = S.list_voices(ctx)[0]
    results = {}
    for heard in ("Chapter Two: The Second Life", "Chapter Too: The Second Life"):
        text = "Chapter One\n\nChapter Two: The Second Light\n\n" + "A long enough body paragraph to read aloud here. " * 6
        prev = S.create_import(ctx, "b.txt", text.encode())
        b = S.confirm_import(ctx, prev["token"], {"title": "T" + heard[-9:], "voice_id": v["id"], "queue": True})
        w = Worker(ctx, tts=MockEngine(), asr=Mishear(heard))
        with ctx.db.tx() as conn:
            conn.execute("UPDATE worker_state SET heartbeat_at=0 WHERE id=1")
        assert w.acquire_lease()
        w.run_until_idle()
        with ctx.db.session() as conn:
            results[heard] = row(conn, "SELECT status FROM segments WHERE book_id=? AND original_text LIKE '%Second Light%'", (b["id"],))["status"]
    assert results["Chapter Two: The Second Life"] == "passed"  # one word of five: no WER flag
    assert results["Chapter Too: The Second Life"] == "flagged"  # two errors: flagged
