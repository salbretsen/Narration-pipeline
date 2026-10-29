"""Manuscript parsing for .txt, .md, .docx and .epub.

Every parser produces chapters made of blocks. A block is either read aloud (heading,
paragraph) or explicitly *not read* (image, table, footnote, navigation, code...) with a
reason, so the import preview can show exactly what will and will not be narrated and no
text disappears silently.
"""
from __future__ import annotations

import posixpath
import re
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from .util import word_count

SUPPORTED_EXTENSIONS = {".txt": "txt", ".md": "md", ".markdown": "md", ".docx": "docx", ".epub": "epub"}


class ManuscriptError(ValueError):
    """The manuscript cannot be imported (malformed, encrypted, empty, unsupported)."""


@dataclass
class Block:
    kind: str  # heading | paragraph | scene_break | skipped
    text: str
    read: bool = True
    reason: str = ""
    level: int = 0


@dataclass
class Chapter:
    title: str
    source: str = ""
    blocks: list[Block] = field(default_factory=list)
    include: bool = True
    include_reason: str = ""

    def readable(self) -> list[Block]:
        return [b for b in self.blocks if b.read and b.kind in ("heading", "paragraph", "scene_break")]

    def words(self) -> int:
        return sum(word_count(b.text) for b in self.readable() if b.kind != "scene_break")

    def skipped(self) -> list[Block]:
        return [b for b in self.blocks if not b.read]


@dataclass
class ParseResult:
    format: str
    title: str = ""
    author: str = ""
    language: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def detect_format(filename: str) -> str:
    ext = posixpath.splitext((filename or "").lower())[1]
    fmt = SUPPORTED_EXTENSIONS.get(ext)
    if not fmt:
        raise ManuscriptError(f"Unsupported file type '{ext or '(none)'}'. Use .epub, .docx, .txt or .md.")
    return fmt


def parse_manuscript(filename: str, data: bytes) -> ParseResult:
    fmt = detect_format(filename)
    if not data:
        raise ManuscriptError("The file is empty.")
    if fmt in ("docx", "epub"):
        if not data.startswith(b"PK"):
            raise ManuscriptError(f"This does not look like a valid .{fmt} file (not a ZIP container).")
        try:
            zf = zipfile.ZipFile(BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise ManuscriptError(f"Corrupt .{fmt} file: {exc}") from exc
        with zf:
            bad = zf.testzip()
            if bad:
                raise ManuscriptError(f"Corrupt .{fmt} file: damaged member {bad}")
            result = _parse_docx(zf) if fmt == "docx" else _parse_epub(zf)
    else:
        text = _decode_text(data, result_warnings := [])
        result = _parse_markdown(text) if fmt == "md" else _parse_plaintext(text)
        result.warnings[:0] = result_warnings
    result.format = fmt
    _finalize(result)
    return result


# --------------------------------------------------------------------------- common


def clean_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = text.replace("­", "")  # soft hyphen
    text = re.sub(r"[​‌‍﻿]", "", text)
    text = text.replace(" ", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    return text.strip()


def _decode_text(data: bytes, warnings: list[str]) -> str:
    if b"\x00" in data[:4096] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ManuscriptError("This file contains binary data and is not a plain-text manuscript.")
    for enc in ("utf-8-sig", "utf-16"):
        try:
            if enc == "utf-16" and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    warnings.append("File is not valid UTF-8; decoded as Windows-1252. Check accented characters in the preview.")
    return data.decode("cp1252", errors="replace")


_NUM_WORD = (
    r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|"
    r"sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)"
)
HEADING_RE = re.compile(
    r"^(?:(?:chapter|part|book|section)\s+(?:\d+|[ivxlcdm]+\b|" + _NUM_WORD + r"(?:[-\s]" + _NUM_WORD + r")*)\b"
    r"|prologue|epilogue|introduction|preface|foreword|afterword|interlude|acknowledge?ments|"
    r"author'?s note|about the author|appendix(?:\s+\w+)?)\b[\s:.\-–—]*[^\n]{0,90}$",
    re.IGNORECASE,
)
SCENE_BREAK_RE = re.compile(r"^\s*(?:(?:[*#~•·⁂❦]\s*){1,7}|-{3,}|_{3,}|={3,})\s*$")


def _is_heading_line(line: str) -> bool:
    line = line.strip()
    if not line or len(line) > 100:
        return False
    if line.endswith((",", ";")):
        return False
    return bool(HEADING_RE.match(line))


def _build_chapters(blocks: list[Block], chapter_level: int | None, default_title: str, source: str) -> list[Chapter]:
    """Split a flat block list into chapters at headings of ``chapter_level``."""
    chapters: list[Chapter] = []
    current: Chapter | None = None
    for b in blocks:
        if b.kind == "heading" and chapter_level is not None and b.level == chapter_level and b.read:
            current = Chapter(title=b.text, source=source)
            chapters.append(current)
            continue
        if current is None:
            current = Chapter(title=default_title, source=source)
            chapters.append(current)
        current.blocks.append(b)
    return chapters


def _finalize(result: ParseResult) -> None:
    # a lead-in "chapter" holding only non-narrated blocks (e.g. the book title) is folded
    # into the next chapter so its manifest entries are kept without an empty chapter
    merged: list[Chapter] = []
    carry: list[Block] = []
    for i, ch in enumerate(result.chapters):
        if carry:
            ch.blocks[:0] = carry
            carry = []
        if i + 1 < len(result.chapters) and ch.blocks and not ch.readable() and ch.source == result.chapters[i + 1].source:
            carry = ch.blocks
            continue
        merged.append(ch)
    result.chapters = merged
    chapters = []
    for ch in result.chapters:
        # trim leading/trailing scene breaks, drop chapters with no content at all
        while ch.blocks and ch.blocks[0].kind == "scene_break":
            ch.blocks.pop(0)
        while ch.blocks and ch.blocks[-1].kind == "scene_break":
            ch.blocks.pop()
        if not ch.blocks and not ch.title:
            continue
        if ch.words() == 0 and ch.include:
            ch.include = False
            ch.include_reason = ch.include_reason or "No readable text"
        chapters.append(ch)
    # an untitled lead-in chapter before the first real heading
    for i, ch in enumerate(chapters, start=1):
        if not ch.title.strip():
            ch.title = f"Section {i}"
    result.chapters = chapters
    if not any(c.include for c in chapters):
        raise ManuscriptError("No readable text was found in this manuscript.")
    total = sum(c.words() for c in chapters if c.include)
    if total < 5:
        raise ManuscriptError("The manuscript contains almost no readable text.")
    skipped = sum(len(c.skipped()) for c in chapters)
    if skipped:
        result.warnings.append(f"{skipped} non-narrated element(s) (images, tables, notes, code...) are listed per chapter.")


# ------------------------------------------------------------------ txt / markdown


def _paragraph_blocks(lines: list[str]) -> list[list[str]]:
    """Group lines into paragraphs. Blank lines separate paragraphs; if the text has no blank
    lines at all, every line is its own paragraph."""
    has_blank = any(not ln.strip() for ln in lines)
    groups: list[list[str]] = []
    cur: list[str] = []
    for ln in lines:
        if not ln.strip():
            if cur:
                groups.append(cur)
                cur = []
            continue
        if not has_blank:
            groups.append([ln])
            continue
        cur.append(ln)
    if cur:
        groups.append(cur)
    return groups


def _parse_plaintext(text: str) -> ParseResult:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[Block] = []
    for group in _paragraph_blocks(lines):
        first = group[0].strip()
        if len(group) == 1 and SCENE_BREAK_RE.match(first):
            blocks.append(Block("scene_break", "", read=True, reason="scene break (pause)"))
            continue
        if _is_heading_line(first):
            blocks.append(Block("heading", clean_text(first), level=1))
            rest = clean_text(" ".join(group[1:]))
            if rest:
                blocks.append(Block("paragraph", rest))
            continue
        para = clean_text(" ".join(group))
        if para:
            blocks.append(Block("paragraph", para))
    res = ParseResult(format="txt")
    # a short first line followed directly by a chapter heading is the book title
    if (len(blocks) >= 2 and blocks[0].kind == "paragraph" and blocks[1].kind == "heading"
            and len(blocks[0].text) <= 80 and not blocks[0].text.endswith((".", "!", "?", ","))):
        res.title = blocks[0].text
        blocks[0] = Block("skipped", blocks[0].text, read=False, reason="book title (used as metadata)")
    has_headings = any(b.kind == "heading" for b in blocks)
    if not has_headings:
        res.warnings.append("No chapter headings detected; the whole text is one chapter. Add lines like 'Chapter 1' to split it.")
    res.chapters = _build_chapters(blocks, 1 if has_headings else None, "", "text")
    if has_headings and res.chapters and res.chapters[0].title == "":
        res.chapters[0].title = "Opening"
    return res


_MD_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_MD_LIST = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_MD_HR = re.compile(r"^\s*(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})\s*$")


def _strip_inline_md(text: str) -> str:
    text = _MD_LINK.sub(r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<![\w*])([*_])(?!\s)(.+?)(?<!\s)\1(?![\w*])", r"\2", text)
    text = re.sub(r"<!--.*?-->", "", text)
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)
    text = re.sub(r"\[\^[^\]]+\]", "", text)  # footnote markers
    return text


def _parse_markdown(text: str) -> ParseResult:
    res = ParseResult(format="md")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    # YAML front matter
    if lines and lines[0].strip() == "---":
        for i in range(1, min(len(lines), 200)):
            if lines[i].strip() in ("---", "..."):
                for fm in lines[1:i]:
                    m = re.match(r"^\s*(title|author)\s*:\s*['\"]?(.*?)['\"]?\s*$", fm, re.I)
                    if m:
                        setattr(res, m.group(1).lower(), m.group(2))
                lines = lines[i + 1 :]
                break
    blocks: list[Block] = []
    para: list[str] = []
    in_code = False
    code_buf: list[str] = []

    def flush():
        if para:
            t = clean_text(_strip_inline_md(" ".join(para)))
            if t:
                blocks.append(Block("paragraph", t))
            para.clear()

    for ln in lines:
        if ln.strip().startswith(("```", "~~~")):
            if in_code:
                blocks.append(Block("skipped", " ".join(code_buf)[:300], read=False, reason="code block"))
                code_buf = []
            else:
                flush()
            in_code = not in_code
            continue
        if in_code:
            code_buf.append(ln.strip())
            continue
        stripped = ln.strip()
        if not stripped:
            flush()
            continue
        if re.match(r"^\[\^[^\]]+\]:", stripped):
            flush()
            blocks.append(Block("skipped", stripped[:300], read=False, reason="footnote"))
            continue
        m = _MD_HEADING.match(stripped)
        if m:
            flush()
            blocks.append(Block("heading", clean_text(_strip_inline_md(m.group(2))), level=len(m.group(1))))
            continue
        if _MD_HR.match(stripped) or SCENE_BREAK_RE.match(stripped):
            flush()
            blocks.append(Block("scene_break", "", reason="scene break (pause)"))
            continue
        if stripped.startswith("|"):
            flush()
            blocks.append(Block("skipped", stripped[:300], read=False, reason="table row"))
            continue
        for img in _MD_IMAGE.finditer(stripped):
            blocks.append(Block("skipped", img.group(1) or "(image)", read=False, reason="image"))
        stripped = _MD_IMAGE.sub("", stripped)
        stripped = re.sub(r"^>\s?", "", stripped)
        if _MD_LIST.match(stripped):
            flush()
            stripped = _MD_LIST.sub("", stripped)
        if stripped.strip():
            para.append(stripped)
    flush()
    if in_code and code_buf:
        blocks.append(Block("skipped", " ".join(code_buf)[:300], read=False, reason="code block"))
        res.warnings.append("Unterminated code block at end of file was not read.")

    levels = sorted({b.level for b in blocks if b.kind == "heading"})
    chapter_level = None
    if levels:
        counts = {lv: sum(1 for b in blocks if b.kind == "heading" and b.level == lv) for lv in levels}
        chapter_level = levels[0]
        if counts[levels[0]] == 1 and len(levels) > 1 and counts[levels[1]] >= 2:
            # a single top-level heading is the book title, the next level are chapters
            top = next(b for b in blocks if b.kind == "heading" and b.level == levels[0])
            if not res.title:
                res.title = top.text
            top.read = False
            top.kind = "skipped"
            top.reason = "book title (used as metadata)"
            chapter_level = levels[1]
    else:
        res.warnings.append("No Markdown headings found; the whole text is one chapter.")
    res.chapters = _build_chapters(blocks, chapter_level, "", "markdown")
    if res.chapters and not res.chapters[0].title:
        res.chapters[0].title = "Opening"
    return res


# --------------------------------------------------------------------------- docx

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _parse_docx(zf: zipfile.ZipFile) -> ParseResult:
    res = ParseResult(format="docx")
    try:
        doc_xml = zf.read("word/document.xml")
    except KeyError as exc:
        raise ManuscriptError("Invalid .docx: word/document.xml is missing.") from exc
    try:
        root = ET.fromstring(doc_xml)
    except ET.ParseError as exc:
        raise ManuscriptError(f"Invalid .docx XML: {exc}") from exc

    style_names: dict[str, str] = {}
    try:
        sroot = ET.fromstring(zf.read("word/styles.xml"))
        for st in sroot.iter(f"{W}style"):
            sid = st.get(f"{W}styleId") or ""
            name_el = st.find(f"{W}name")
            if name_el is not None:
                style_names[sid] = (name_el.get(f"{W}val") or "").lower()
    except (KeyError, ET.ParseError):
        pass
    try:
        core = ET.fromstring(zf.read("docProps/core.xml"))
        for el in core:
            tag = el.tag.split("}")[-1]
            if tag == "title" and el.text:
                res.title = el.text.strip()
            elif tag == "creator" and el.text:
                res.author = el.text.strip()
            elif tag == "language" and el.text:
                res.language = el.text.strip()
    except (KeyError, ET.ParseError):
        pass

    body = root.find(f"{W}body")
    if body is None:
        raise ManuscriptError("Invalid .docx: document body missing.")
    blocks: list[Block] = []
    footnote_refs = 0

    def heading_level(p) -> int:
        ppr = p.find(f"{W}pPr")
        if ppr is None:
            return 0
        ps = ppr.find(f"{W}pStyle")
        if ps is None:
            ol = ppr.find(f"{W}outlineLvl")
            if ol is not None:
                try:
                    return int(ol.get(f"{W}val", "9")) + 1
                except ValueError:
                    return 0
            return 0
        sid = ps.get(f"{W}val") or ""
        name = style_names.get(sid, sid).lower().replace(" ", "")
        if name in ("title", "subtitle"):
            return -1  # document title: metadata, not a chapter
        m = re.match(r"^(?:heading|berschrift|titre|titolo|encabezado|kop)(\d)$", name) or re.match(r"^heading(\d)$", sid.lower())
        return int(m.group(1)) if m else 0

    for child in body:
        tag = child.tag
        if tag == f"{W}tbl":
            txt = clean_text(" ".join(t.text or "" for t in child.iter(f"{W}t")))
            blocks.append(Block("skipped", txt[:300] or "(table)", read=False, reason="table"))
            continue
        if tag != f"{W}p":
            continue
        parts: list[str] = []
        has_image = False
        for el in child.iter():
            t = el.tag
            if t == f"{W}t":
                parts.append(el.text or "")
            elif t == f"{W}tab":
                parts.append(" ")
            elif t in (f"{W}br", f"{W}cr"):
                parts.append(" ")
            elif t in (f"{W}footnoteReference", f"{W}endnoteReference"):
                footnote_refs += 1
            elif t.endswith("}drawing") or t.endswith("}pict"):
                has_image = True
        if has_image:
            blocks.append(Block("skipped", "(image)", read=False, reason="image"))
        text = clean_text("".join(parts))
        if not text:
            continue
        lvl = heading_level(child)
        if lvl == -1:
            res.title = res.title or text
            blocks.append(Block("skipped", text, read=False, reason="book title (used as metadata)"))
        elif lvl:
            blocks.append(Block("heading", text, level=lvl))
        elif SCENE_BREAK_RE.match(text):
            blocks.append(Block("scene_break", "", reason="scene break (pause)"))
        else:
            blocks.append(Block("paragraph", text))
    if footnote_refs:
        res.warnings.append(f"{footnote_refs} footnote/endnote reference(s): note text is not narrated.")

    levels = sorted({b.level for b in blocks if b.kind == "heading"})
    chapter_level = None
    if levels:
        counts = {lv: sum(1 for b in blocks if b.kind == "heading" and b.level == lv) for lv in levels}
        chapter_level = levels[0]
        if counts[levels[0]] == 1 and len(levels) > 1 and counts[levels[1]] >= 2:
            top = next(b for b in blocks if b.kind == "heading" and b.level == levels[0])
            res.title = res.title or top.text
            top.kind, top.read, top.reason = "skipped", False, "book title (used as metadata)"
            chapter_level = levels[1]
    else:
        # fall back to textual chapter headings in normal paragraphs
        for b in blocks:
            if b.kind == "paragraph" and _is_heading_line(b.text):
                b.kind, b.level = "heading", 1
        if any(b.kind == "heading" for b in blocks):
            chapter_level = 1
            res.warnings.append("No Word heading styles found; chapters were detected from 'Chapter N' lines.")
        else:
            res.warnings.append("No headings found; the whole document is one chapter. Use Heading 1 for chapters.")
    res.chapters = _build_chapters(blocks, chapter_level, "", "docx")
    if res.chapters and not res.chapters[0].title:
        res.chapters[0].title = "Opening"
    return res


# --------------------------------------------------------------------------- epub

_FONT_OBFUSCATION = {"http://www.idpf.org/2008/embedding", "http://ns.adobe.com/pdf/enc#RC"}
_BLOCK_TAGS = {"p", "div", "li", "blockquote", "dd", "dt", "pre", "figcaption", "caption", "section", "article", "header", "footer", "body", "td", "th"}
_HEADING_TAGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_SKIP_CONTENT = {"script": "script", "style": "style", "head": "", "svg": "image", "math": "math formula", "object": "embedded object", "audio": "audio", "video": "video"}
_NOTE_TYPES = {"footnote", "endnote", "rearnote", "note", "doc-footnote", "doc-endnote", "doc-endnotes", "footnotes", "endnotes", "rearnotes"}
_NOTEREF_TYPES = {"noteref", "doc-noteref"}
_EXCLUDE_DOC_TYPES = {"toc": "table of contents", "landmarks": "navigation", "cover": "cover", "copyright-page": "copyright page", "titlepage": "title page", "index": "index", "loi": "list of illustrations", "lot": "list of tables"}


def _types(attrs: dict) -> set[str]:
    vals = (attrs.get("epub:type") or "") + " " + (attrs.get("role") or "")
    return {v.strip().lower() for v in vals.split() if v.strip()}


_VOID = {"br", "img", "hr", "meta", "link", "input", "wbr", "col", "area", "base", "source", "param", "embed", "track"}
_NOTE_MARK = re.compile(r"^\s*[\[(]?(?:\d{1,3}|[ivx]{1,4}|[*†‡§])[\])]?\s*$")


class _XHTMLExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.buf: list[str] = []
        self.skip_stack: list[tuple[str, str, list[str]]] = []  # (tag, reason, captured text)
        self.heading: int = 0
        self.doc_types: set[str] = set()
        self.noteref_depth = 0
        self.sup_depth = 0
        self.frag_link_depth = 0
        self.skipped_markers = 0

    def _flush(self):
        text = clean_text("".join(self.buf))
        self.buf = []
        if not text:
            return
        if self.heading:
            self.blocks.append(Block("heading", text, level=self.heading))
        elif SCENE_BREAK_RE.match(text):
            self.blocks.append(Block("scene_break", "", reason="scene break (pause)"))
        else:
            self.blocks.append(Block("paragraph", text))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in _VOID:
            self.handle_endtag(tag)

    def handle_starttag(self, tag, attrs_list):
        attrs = {k.lower(): (v or "") for k, v in attrs_list}
        tag = tag.lower()
        types = _types(attrs)
        if tag in ("body", "section") and types & set(_EXCLUDE_DOC_TYPES):
            self.doc_types |= types
        if self.skip_stack:
            if tag not in _VOID:
                self.skip_stack.append((tag, "", []))
            return
        if tag in ("br", "wbr"):
            self.buf.append(" ")
            return
        if tag == "hr":
            self._flush()
            self.blocks.append(Block("scene_break", "", reason="scene break (pause)"))
            return
        if tag == "img":
            alt = attrs.get("alt", "").strip()
            self.blocks.append(Block("skipped", alt or "(image)", read=False, reason="image"))
            return
        if tag in _VOID:
            return
        is_note = tag in ("aside", "section", "div", "ol", "ul", "p", "li") and bool(types & _NOTE_TYPES)
        if tag in _SKIP_CONTENT or tag in ("table", "nav") or is_note:
            self._flush()
            if tag in _SKIP_CONTENT:
                reason = _SKIP_CONTENT[tag]
            elif tag == "table":
                reason = "table"
            elif tag == "nav":
                reason = "navigation"
            else:
                reason = "footnote/endnote"
            self.skip_stack.append((tag, reason, []))
            return
        if tag == "a":
            if types & _NOTEREF_TYPES:
                self.noteref_depth += 1
                self.skipped_markers += 1
            elif attrs.get("href", "").find("#") >= 0:
                self.frag_link_depth += 1
            return
        if tag == "sup":
            self.sup_depth += 1
            return
        if tag in _HEADING_TAGS:
            self._flush()
            self.heading = _HEADING_TAGS[tag]
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip_stack:
            _otag, reason, text = self.skip_stack.pop()
            if self.skip_stack:
                self.skip_stack[0][2].extend(text)
            elif reason:
                t = clean_text(" ".join(text))
                self.blocks.append(Block("skipped", t[:300] or f"({reason})", read=False, reason=reason))
            return
        if tag == "a":
            if self.noteref_depth:
                self.noteref_depth -= 1
            elif self.frag_link_depth:
                self.frag_link_depth -= 1
            return
        if tag == "sup":
            self.sup_depth = max(0, self.sup_depth - 1)
            return
        if tag in _HEADING_TAGS:
            self._flush()
            self.heading = 0
            return
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self.skip_stack:
            self.skip_stack[-1][2].append(data)
            return
        if self.noteref_depth:
            return
        if self.sup_depth and self.frag_link_depth and _NOTE_MARK.match(data):
            self.skipped_markers += 1
            return
        self.buf.append(data)

    def close(self):
        super().close()
        self._flush()


def _zip_read(zf: zipfile.ZipFile, name: str) -> bytes:
    try:
        return zf.read(name)
    except KeyError:
        # tolerate case differences produced by some tools
        lower = {n.lower(): n for n in zf.namelist()}
        if name.lower() in lower:
            return zf.read(lower[name.lower()])
        raise


def _parse_epub(zf: zipfile.ZipFile) -> ParseResult:
    res = ParseResult(format="epub")
    names = set(zf.namelist())
    if "META-INF/rights.xml" in names:
        raise ManuscriptError("This EPUB is DRM-protected (rights.xml present). Import a DRM-free file you own.")
    if "META-INF/encryption.xml" in names:
        try:
            enc = ET.fromstring(zf.read("META-INF/encryption.xml"))
            for ed in enc.iter():
                if ed.tag.endswith("EncryptionMethod"):
                    if (ed.get("Algorithm") or "") not in _FONT_OBFUSCATION:
                        raise ManuscriptError("This EPUB is encrypted (DRM). Import a DRM-free file you own.")
        except ET.ParseError as exc:
            raise ManuscriptError("This EPUB has an unreadable encryption manifest.") from exc
    try:
        container = ET.fromstring(_zip_read(zf, "META-INF/container.xml"))
    except (KeyError, ET.ParseError) as exc:
        raise ManuscriptError("Invalid EPUB: META-INF/container.xml missing or malformed.") from exc
    rootfile = next((el.get("full-path") for el in container.iter() if el.tag.endswith("rootfile")), None)
    if not rootfile:
        raise ManuscriptError("Invalid EPUB: no package document (OPF) declared.")
    try:
        opf = ET.fromstring(_zip_read(zf, rootfile))
    except (KeyError, ET.ParseError) as exc:
        raise ManuscriptError(f"Invalid EPUB: package document {rootfile} missing or malformed.") from exc
    opf_dir = posixpath.dirname(rootfile)

    for el in opf.iter():
        tag = el.tag.split("}")[-1]
        if tag == "title" and el.text and not res.title:
            res.title = el.text.strip()
        elif tag == "creator" and el.text and not res.author:
            res.author = el.text.strip()
        elif tag == "language" and el.text and not res.language:
            res.language = el.text.strip()

    manifest: dict[str, dict] = {}
    spine: list[tuple[str, bool]] = []
    toc_id = None
    for el in opf.iter():
        tag = el.tag.split("}")[-1]
        if tag == "item":
            href = unquote(el.get("href") or "")
            manifest[el.get("id") or ""] = {
                "href": posixpath.normpath(posixpath.join(opf_dir, href)) if href else "",
                "type": el.get("media-type") or "",
                "props": set((el.get("properties") or "").split()),
            }
        elif tag == "spine":
            toc_id = el.get("toc")
        elif tag == "itemref":
            spine.append((el.get("idref") or "", (el.get("linear") or "yes").lower() != "no"))
    if not spine:
        raise ManuscriptError("Invalid EPUB: the spine (reading order) is empty.")

    labels = _epub_toc_labels(zf, manifest, toc_id)
    chapters: list[Chapter] = []
    for order, (idref, linear) in enumerate(spine, start=1):
        item = manifest.get(idref)
        if not item:
            res.warnings.append(f"Spine item '{idref}' is not in the manifest and was skipped.")
            continue
        href = item["href"]
        if "html" not in item["type"] and not href.lower().endswith((".xhtml", ".html", ".htm")):
            continue
        try:
            raw = _zip_read(zf, href)
        except KeyError:
            res.warnings.append(f"Spine document {href} is missing from the EPUB.")
            continue
        ex = _XHTMLExtractor()
        try:
            ex.feed(raw.decode("utf-8", errors="replace"))
            ex.close()
        except Exception as exc:  # malformed markup: keep going with what we have
            res.warnings.append(f"{href}: markup error ({exc}); content may be incomplete.")
        blocks = ex.blocks
        if ex.skipped_markers:
            res.warnings.append(f"{href}: {ex.skipped_markers} footnote reference marker(s) are not narrated.")
        label = labels.get(href, "")
        heads = [b for b in blocks if b.kind == "heading"]
        top = min((b.level for b in heads), default=None)
        doc_chapters: list[Chapter]
        if top is not None and sum(1 for b in heads if b.level == top) >= 2:
            doc_chapters = _build_chapters(blocks, top, label, href)
        else:
            title = label
            if heads and blocks and blocks[0].kind == "heading":
                title = blocks[0].text
                blocks = blocks[1:]
            elif heads and not title:
                title = heads[0].text
            doc_chapters = [Chapter(title=title, source=href, blocks=blocks)]
        for ch in doc_chapters:
            ch.source = href
            if "nav" in item["props"]:
                ch.include, ch.include_reason = False, "EPUB navigation document"
            elif not linear:
                ch.include, ch.include_reason = False, "Marked non-linear in the EPUB spine (auxiliary content)"
            else:
                doc_type = next((t for t in ex.doc_types if t in _EXCLUDE_DOC_TYPES), None)
                if doc_type:
                    ch.include, ch.include_reason = False, f"Looks like a {_EXCLUDE_DOC_TYPES[doc_type]}"
            if not ch.title:
                ch.title = f"Section {order}"
            chapters.append(ch)
    res.chapters = chapters
    if not res.title:
        res.warnings.append("EPUB has no dc:title metadata.")
    return res


def _epub_toc_labels(zf: zipfile.ZipFile, manifest: dict, toc_id: str | None) -> dict[str, str]:
    labels: dict[str, str] = {}
    nav = next((m for m in manifest.values() if "nav" in m["props"]), None)
    if nav:
        try:
            class NavParser(HTMLParser):
                def __init__(self):
                    super().__init__(convert_charrefs=True)
                    self.href = None
                    self.text: list[str] = []

                def handle_starttag(self, tag, attrs):
                    if tag == "a":
                        self.href = dict(attrs).get("href")
                        self.text = []

                def handle_data(self, data):
                    if self.href is not None:
                        self.text.append(data)

                def handle_endtag(self, tag):
                    if tag == "a" and self.href:
                        target = posixpath.normpath(posixpath.join(posixpath.dirname(nav["href"]), unquote(self.href.split("#")[0])))
                        labels.setdefault(target, clean_text("".join(self.text)))
                        self.href = None

            p = NavParser()
            p.feed(_zip_read(zf, nav["href"]).decode("utf-8", errors="replace"))
        except Exception:
            pass
    ncx = manifest.get(toc_id or "") or next((m for m in manifest.values() if m["type"] == "application/x-dtbncx+xml"), None)
    if ncx and not labels:
        try:
            root = ET.fromstring(_zip_read(zf, ncx["href"]))
            for np_ in root.iter():
                if not np_.tag.endswith("navPoint"):
                    continue
                text_el = next((e for e in np_.iter() if e.tag.endswith("text")), None)
                content = next((e for e in np_ if e.tag.endswith("content")), None)
                if text_el is not None and content is not None:
                    src = unquote((content.get("src") or "").split("#")[0])
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(ncx["href"]), src))
                    labels.setdefault(target, clean_text(text_el.text or ""))
        except Exception:
            pass
    return labels
