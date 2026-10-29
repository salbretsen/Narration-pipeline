"""Chapter mastering and book export (WAV master, MP3, chapter-marked M4B, ZIP).

Assembly never hides gaps: a chapter with any segment lacking a usable selected take, or
with takes rendered under different voice/model settings, is *blocked* with an explicit
list instead of being silently shortened.
"""
from __future__ import annotations

import json
import re
import time
import wave
import zipfile
from pathlib import Path

import numpy as np

from . import audio as A
from .db import row, rows
from .pipeline import book_dirs, check_storage, resolve_take
from .util import dumps, loads, now, short_hash, temp_sibling, title_filename


class AssemblyBlocked(RuntimeError):
    def __init__(self, message: str, details: list[dict] | None = None):
        super().__init__(message)
        self.details = details or []


def active_release(conn, book_id: int) -> dict | None:
    return row(conn, "SELECT * FROM releases WHERE book_id=? AND revoked_at IS NULL ORDER BY id DESC LIMIT 1", (book_id,))


def _chapter_takes(conn, book: dict, chapter_id: int):
    segs = rows(conn, "SELECT * FROM segments WHERE chapter_id=? ORDER BY idx", (chapter_id,))
    out, problems = [], []
    for s in segs:
        if not s["selected_attempt_id"]:
            problems.append({"sid": s["sid"], "problem": "no audio" if s["status"] == "pending" else f"no usable take ({s['status']})"})
            continue
        att = row(conn, "SELECT * FROM attempts WHERE id=?", (s["selected_attempt_id"],))
        path = resolve_take(book, att) if att else None
        if not att or path is None:
            problems.append({"sid": s["sid"], "problem": "selected take file is missing"})
            continue
        if att["fingerprint"] != book["synth_fingerprint"]:
            problems.append({"sid": s["sid"], "problem": "take was rendered with different narrator/model settings"})
            continue
        out.append((s, att, path))
    return segs, out, problems


def takes_digest(conn, book: dict, chapter_id: int) -> str:
    sel = rows(conn, "SELECT sid, selected_attempt_id FROM segments WHERE chapter_id=? ORDER BY idx", (chapter_id,))
    snap = loads(book["settings_json"], {})
    payload = {"sel": [(r["sid"], r["selected_attempt_id"]) for r in sel], "pauses": snap.get("pauses"), "export": snap.get("export"),
               "meta": [book["title"], book["author"]]}
    return short_hash(dumps(payload), 16)


def chapter_readiness(conn, book: dict) -> list[dict]:
    out = []
    for ch in rows(conn, "SELECT * FROM chapters WHERE book_id=? ORDER BY idx", (book["id"],)):
        segs, takes, problems = _chapter_takes(conn, book, ch["id"])
        renders = {r["kind"]: r for r in rows(conn, "SELECT * FROM chapter_renders WHERE chapter_id=?", (ch["id"],))}
        digest = takes_digest(conn, book, ch["id"])
        current = bool(renders.get("wav") and renders.get("mp3") and renders["wav"]["takes_digest"] == digest
                       and Path(renders["wav"]["path"]).exists() and Path(renders["mp3"]["path"]).exists())
        out.append({"chapter": ch, "segments": len(segs), "ready_takes": len(takes), "problems": problems,
                    "renders": renders, "render_current": current, "digest": digest})
    return out


def _loudnorm_filter(raw: Path, lufs: float, tp: float, sr_out: int, enabled: bool) -> str:
    resample = f"aresample={sr_out}"
    if not enabled:
        return resample
    base = f"loudnorm=I={lufs}:TP={tp}:LRA=11"
    try:
        proc = A.run_ffmpeg(["-i", str(raw), "-af", base + ":print_format=json", "-f", "null", "-"], timeout=1800)
        m = re.findall(r"\{[^{}]*\}", proc.stderr, re.S)
        stats = json.loads(m[-1]) if m else {}
        mi, mtp, mlra, mth, off = (stats.get(k) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset"))
        vals = [float(v) for v in (mi, mtp, mlra, mth, off)]
        if not all(np.isfinite(vals)):
            raise ValueError("non-finite loudness stats")
        return (f"{base}:measured_I={vals[0]}:measured_TP={vals[1]}:measured_LRA={vals[2]}:"
                f"measured_thresh={vals[3]}:offset={vals[4]}:linear=true,{resample}")
    except (A.AudioToolError, ValueError, TypeError):
        return f"{base},{resample}"  # single-pass fallback (very short audio)


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _tag_args(meta: dict) -> list[str]:
    args = []
    for k, v in meta.items():
        if v:
            args += ["-metadata", f"{k}={v}"]
    return args


def assemble_chapter(ctx, book_id: int, chapter_id: int) -> dict:
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        ch = row(conn, "SELECT * FROM chapters WHERE id=? AND book_id=?", (chapter_id, book_id))
        if not book or not ch:
            raise ValueError("chapter not found")
        segs, takes, problems = _chapter_takes(conn, book, chapter_id)
        if problems:
            raise AssemblyBlocked(f"Chapter {ch['idx']} '{ch['title']}' cannot be assembled: {len(problems)} segment(s) without usable audio.", problems)
        if not takes:
            raise AssemblyBlocked(f"Chapter {ch['idx']} has no segments.")
        digest = takes_digest(conn, book, chapter_id)
        draft = active_release(conn, book_id) is None
    check_storage(book, ("masters",))
    snap = loads(book["settings_json"], {})
    pauses, export = snap.get("pauses", {}), snap.get("export", {})
    sr = int(takes[0][1]["sample_rate"] or 24000)
    gaps = {"sentence": pauses.get("sentence_ms", 300), "paragraph": pauses.get("paragraph_ms", 800),
            "scene": pauses.get("scene_break_ms", 1800), "heading": pauses.get("after_heading_ms", 1500),
            "chapter_end": pauses.get("chapter_tail_ms", 2500)}
    tone_db = pauses.get("room_tone_dbfs", -72.0)
    pieces = [A.room_tone(int(sr * pauses.get("chapter_head_ms", 800) / 1000), tone_db, seed=1)]
    cursor = pieces[0].size
    seg_map = []
    for k, (s, att, path) in enumerate(takes):
        a, r = A.read_wav(path)
        if r != sr:
            a = A.resample_linear(a, r, sr)
        if pauses.get("trim_silence", True):
            a = A.trim_silence(a, sr, keep_ms=60)
        seg_map.append({"sid": s["sid"], "start_s": round(cursor / sr, 3), "end_s": round((cursor + a.size) / sr, 3)})
        pieces.append(a.astype(np.float32))
        cursor += a.size
        gap = A.room_tone(int(sr * gaps.get(s["pause_kind"], gaps["sentence"]) / 1000), tone_db, seed=k + 2)
        pieces.append(gap)
        cursor += gap.size
    raw_audio = np.concatenate(pieces)
    expected = raw_audio.size / sr

    dirs = book_dirs(book)
    out_dir = dirs["masters"]
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{ch['idx']:03d}-" + title_filename(ch["title"], maxlen=60).replace(" ", "_")
    wav_out, mp3_out = out_dir / f"{stem}.wav", out_dir / f"{stem}.mp3"
    raw_tmp = temp_sibling(out_dir / f"{stem}.raw.wav", ".wav")
    wav_tmp = temp_sibling(wav_out, ".wav")
    mp3_tmp = temp_sibling(mp3_out, ".mp3")
    sr_out = int(export.get("sample_rate", 44100))
    try:
        A.write_wav(raw_tmp, raw_audio, sr)
        af = _loudnorm_filter(raw_tmp, export.get("target_lufs", -19.0), export.get("true_peak_db", -3.0), sr_out, export.get("loudness_normalize", True))
        A.run_ffmpeg(["-i", str(raw_tmp), "-af", af, "-ac", "1", "-ar", str(sr_out), "-c:a", "pcm_s16le", "-f", "wav", str(wav_tmp)])
        tags = {"title": ch["title"], "artist": book["author"], "album": book["title"], "album_artist": book["author"],
                "composer": book["narrator_credit"], "track": str(ch["idx"]), "genre": book.get("genre") or "Audiobook",
                "comment": "DRAFT - not approved for release" if draft else "Approved for release"}
        A.run_ffmpeg(["-i", str(wav_tmp), "-c:a", "libmp3lame", "-b:a", f"{export.get('mp3_bitrate_kbps', 192)}k", "-ar", str(sr_out),
                      "-ac", "1", "-id3v2_version", "3", *_tag_args(tags), "-f", "mp3", str(mp3_tmp)])
        wav_probe, mp3_probe = A.probe_summary(wav_tmp), A.probe_summary(mp3_tmp)
        tol = 0.5 + expected * 0.01
        for name, p, codec in (("WAV master", wav_probe, "pcm_s16le"), ("MP3", mp3_probe, "mp3")):
            if p["codec"] != codec or p["sample_rate"] != sr_out or abs(p["duration_s"] - expected) > tol:
                raise A.AudioToolError(f"{name} validation failed: codec={p['codec']} rate={p['sample_rate']} duration={p['duration_s']:.2f}s expected {expected:.2f}s")
        wav_tmp.replace(wav_out)
        mp3_tmp.replace(mp3_out)
    finally:
        for t in (raw_tmp, wav_tmp, mp3_tmp):
            t.unlink(missing_ok=True)
    wav_dur = _wav_duration(wav_out)
    with ctx.db.tx() as conn:
        for kind, path, probe_ in (("wav", wav_out, wav_probe), ("mp3", mp3_out, mp3_probe)):
            conn.execute(
                """INSERT INTO chapter_renders(chapter_id, kind, path, duration_s, segment_map_json, takes_digest, draft, probe_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(chapter_id, kind) DO UPDATE SET path=excluded.path, duration_s=excluded.duration_s,
                     segment_map_json=excluded.segment_map_json, takes_digest=excluded.takes_digest, draft=excluded.draft,
                     probe_json=excluded.probe_json, created_at=excluded.created_at""",
                (chapter_id, kind, str(path), wav_dur if kind == "wav" else probe_["duration_s"], dumps(seg_map), digest, 1 if draft else 0, dumps(probe_), now()),
            )
    return {"chapter_id": chapter_id, "wav": str(wav_out), "mp3": str(mp3_out), "duration_s": wav_dur, "segments": len(takes), "draft": draft}


def assemble_book(ctx, book_id: int, only_ready: bool = True, progress=None) -> dict:
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        ready = chapter_readiness(conn, book)
    done, blocked, skipped = [], [], []
    for r in ready:
        ch = r["chapter"]
        if r["render_current"]:
            skipped.append(ch["id"])
            continue
        if r["problems"]:
            blocked.append({"chapter": ch["idx"], "title": ch["title"], "problems": r["problems"][:20], "count": len(r["problems"])})
            if not only_ready:
                raise AssemblyBlocked(f"Chapter {ch['idx']} is incomplete", r["problems"])
            continue
        if progress:
            progress(f"Assembling chapter {ch['idx']}: {ch['title']}")
        done.append(assemble_chapter(ctx, book_id, ch["id"]))
    return {"assembled": done, "blocked": blocked, "up_to_date": skipped}


def _ffmeta_escape(v: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", v or "")


def export_book(ctx, book_id: int, kind: str = "m4b", chapter_id: int | None = None, progress=None) -> dict:
    """Build an export. kind: m4b (whole book), chapter_m4b, mp3_zip."""
    if kind not in ("m4b", "chapter_m4b", "mp3_zip"):
        raise ValueError(f"unknown export kind {kind}")
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        ready = chapter_readiness(conn, book)
    if kind == "chapter_m4b":
        ready = [r for r in ready if r["chapter"]["id"] == chapter_id]
        if not ready:
            raise ValueError("chapter not found")
    blocked = [r for r in ready if r["problems"]]
    if blocked:
        details = [{"chapter": r["chapter"]["idx"], "title": r["chapter"]["title"], **p} for r in blocked for p in r["problems"][:50]]
        raise AssemblyBlocked(f"Export blocked: {sum(len(r['problems']) for r in blocked)} segment(s) in {len(blocked)} chapter(s) have no usable audio. "
                              "Nothing was omitted - generate or fix them first.", details)
    for r in ready:
        if not r["render_current"]:
            if progress:
                progress(f"Assembling chapter {r['chapter']['idx']}")
            assemble_chapter(ctx, book_id, r["chapter"]["id"])
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        draft = active_release(conn, book_id) is None
        chapters = []
        for r in ready:
            ch = r["chapter"]
            rend = {x["kind"]: x for x in rows(conn, "SELECT * FROM chapter_renders WHERE chapter_id=?", (ch["id"],))}
            chapters.append((ch, rend))
    check_storage(book, ("exports",))
    export = loads(book["settings_json"], {}).get("export", {})
    out_dir = book_dirs(book)["exports"]
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    label_parts = [book["title"], book["author"]]
    if kind == "chapter_m4b":
        label_parts.append(f"Chapter {chapters[0][0]['idx']}")
    if draft:
        label_parts.append("DRAFT")
    base = title_filename(*label_parts) + f" - {stamp}"

    if kind == "mp3_zip":
        out = out_dir / f"{base}.zip"
        tmp = temp_sibling(out, ".zip")
        try:
            with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as z:
                lines = ["#EXTM3U"]
                for ch, rend in chapters:
                    name = Path(rend["mp3"]["path"]).name
                    z.write(rend["mp3"]["path"], name)
                    lines += [f"#EXTINF:{int(rend['mp3']['duration_s'])},{ch['title']}", name]
                z.writestr("playlist.m3u", "\n".join(lines) + "\n")
                if draft:
                    z.writestr("DRAFT-NOT-APPROVED.txt", "These files are drafts. Human release approval has not been recorded.\n")
            tmp.replace(out)
        finally:
            tmp.unlink(missing_ok=True)
        info = {"duration_s": sum(r["mp3"]["duration_s"] for _, r in chapters), "files": len(chapters)}
    else:
        out = out_dir / f"{base}.m4b"
        list_tmp = temp_sibling(out_dir / "concat.txt", ".txt")
        meta_tmp = temp_sibling(out_dir / "meta.txt", ".txt")
        out_tmp = temp_sibling(out, ".m4b")
        try:
            starts, t = [], 0.0
            lines = []
            for ch, rend in chapters:
                p = rend["wav"]["path"]
                lines.append("file '" + p.replace("'", "'\\''") + "'")
                d = _wav_duration(Path(p))
                starts.append((ch["title"], t, t + d))
                t += d
            list_tmp.write_text("\n".join(lines) + "\n", "utf-8")
            meta = [";FFMETADATA1",
                    f"title={_ffmeta_escape(book['title'] + (' (DRAFT)' if draft else ''))}",
                    f"artist={_ffmeta_escape(book['author'])}", f"album={_ffmeta_escape(book['title'])}",
                    f"album_artist={_ffmeta_escape(book['author'])}", f"composer={_ffmeta_escape(book['narrator_credit'])}",
                    f"genre={_ffmeta_escape(book.get('genre') or 'Audiobook')}", f"date={_ffmeta_escape(book['year'])}",
                    f"publisher={_ffmeta_escape(book['publisher'])}",
                    f"description={_ffmeta_escape(book['description'])}",
                    f"comment={_ffmeta_escape('DRAFT - human release approval not recorded' if draft else 'Approved for release by operator')}"]
            for title, s, e in starts:
                meta += ["", "[CHAPTER]", "TIMEBASE=1/1000", f"START={int(round(s * 1000))}", f"END={int(round(e * 1000))}", f"title={_ffmeta_escape(title)}"]
            meta_tmp.write_text("\n".join(meta) + "\n", "utf-8")
            args = ["-f", "concat", "-safe", "0", "-i", str(list_tmp), "-i", str(meta_tmp)]
            cover = Path(book["cover_path"]) if book.get("cover_path") else None
            if cover and cover.exists():
                args += ["-i", str(cover), "-map", "0:a", "-map", "2:v", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
            else:
                args += ["-map", "0:a"]
            args += ["-map_metadata", "1", "-map_chapters", "1", "-c:a", "aac", "-b:a", f"{export.get('m4b_bitrate_kbps', 96)}k",
                     "-ar", str(export.get("sample_rate", 44100)), "-ac", "1", "-movflags", "+faststart", "-f", "mp4", str(out_tmp)]
            if progress:
                progress("Encoding AAC M4B")
            A.run_ffmpeg(args, timeout=4 * 3600)
            info = A.probe_summary(out_tmp)
            if info["codec"] != "aac":
                raise A.AudioToolError(f"M4B validation failed: codec {info['codec']}")
            if len(info["chapters"]) != len(starts):
                raise A.AudioToolError(f"M4B validation failed: {len(info['chapters'])} chapter markers, expected {len(starts)}")
            if abs(info["duration_s"] - t) > 1.5 + t * 0.005:
                raise A.AudioToolError(f"M4B validation failed: duration {info['duration_s']:.1f}s, expected {t:.1f}s")
            out_tmp.replace(out)
        finally:
            for x in (list_tmp, meta_tmp, out_tmp):
                x.unlink(missing_ok=True)
    size = out.stat().st_size
    label = {"m4b": "Audiobook M4B", "chapter_m4b": f"Chapter {chapters[0][0]['idx']} M4B", "mp3_zip": "Chapter MP3s (ZIP)"}[kind]
    with ctx.db.tx() as conn:
        cur = conn.execute("INSERT INTO exports(book_id, kind, label, path, draft, size_bytes, duration_s, probe_json, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                           (book_id, kind, label + (" (DRAFT)" if draft else ""), str(out), 1 if draft else 0, size, info.get("duration_s"), dumps(info), now()))
        ctx.event(conn, "export", f"{label}{' (draft)' if draft else ''} exported for '{book['title']}'", book_id)
    return {"export_id": cur.lastrowid, "path": str(out), "draft": draft, "size_bytes": size, "probe": info}
