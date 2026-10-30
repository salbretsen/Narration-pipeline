"""Review summary reports (JSON + self-contained, escaped HTML)."""
from __future__ import annotations

import html
import json
import time
from pathlib import Path

from . import __version__
from .assembly import active_release, chapter_readiness
from .db import row, rows
from .pipeline import book_dirs
from .util import atomic_write_text, dumps, loads, now, title_filename

QA_LIMITATIONS = [
    "Automatic QA compares an independent local Whisper transcript with the intended text. Agreement is evidence, not proof:",
    "Whisper can mis-hear names and rare words (false flags) and can 'autocorrect' a mispronounced word into the right spelling (missed errors).",
    "ASR cannot judge pronunciation of names, emphasis, emotional delivery, pacing or character voices.",
    "Acoustic checks detect clipping, dead air, silence, abnormal level and duration - not subtle artefacts such as metallic timbre or breaths.",
    "A full human listen is strongly recommended before commercial release. Distributor (e.g. ACX/Audible) technical and content requirements must be verified separately.",
]


def build_report(ctx, book_id: int) -> dict:
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        if not book:
            raise ValueError("book not found")
        voice = row(conn, "SELECT * FROM voices WHERE id=?", (book["voice_id"],)) or {}
        vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (book["voice_version_id"],)) or {}
        manuscript = row(conn, "SELECT original_name, sha256, size_bytes, format FROM manuscripts WHERE book_id=? ORDER BY id DESC LIMIT 1", (book_id,)) or {}
        readiness = {r["chapter"]["id"]: r for r in chapter_readiness(conn, book, ctx)}
        chapters = rows(conn, "SELECT * FROM chapters WHERE book_id=? ORDER BY idx", (book_id,))
        counts = {r["status"]: r["n"] for r in rows(conn, "SELECT status, COUNT(*) n FROM segments WHERE book_id=? GROUP BY status", (book_id,))}
        release = active_release(conn, book_id)
        rel_audit = row(conn, "SELECT actor FROM audit_log WHERE entity='book' AND entity_id=? AND action='release_approved' ORDER BY id DESC LIMIT 1", (str(book_id),)) if release else None
        segs = rows(conn, """SELECT s.*, c.idx AS chapter_idx, c.title AS chapter_title FROM segments s JOIN chapters c ON c.id=s.chapter_id
                             WHERE s.book_id=? ORDER BY c.idx, s.idx""", (book_id,))
        attempts_by_seg: dict[int, list] = {}
        for a in rows(conn, """SELECT a.id, a.segment_id, a.attempt_no, a.round, a.status, a.wer, a.score, a.passed, a.issues_json, a.duration_s,
                                      a.model_id, a.engine, a.versions_json, a.rtf FROM attempts a JOIN segments s ON s.id=a.segment_id
                               WHERE s.book_id=? ORDER BY a.id""", (book_id,)):
            attempts_by_seg.setdefault(a["segment_id"], []).append(a)
        reviews = rows(conn, """SELECT r.*, s.sid FROM review_actions r JOIN segments s ON s.id=r.segment_id WHERE s.book_id=? ORDER BY r.id""", (book_id,))

    # book-level timeline from chapter renders
    offsets, book_t = {}, 0.0
    chapter_rows = []
    for ch in chapters:
        rd = readiness.get(ch["id"], {})
        wav = (rd.get("renders") or {}).get("wav")
        seg_map = {m["sid"]: m for m in loads(wav["segment_map_json"], [])} if wav else {}
        offsets[ch["id"]] = (book_t, seg_map)
        chapter_rows.append({"index": ch["idx"], "title": ch["title"], "segments": ch["segment_count"], "words": ch["word_count"],
                             "duration_s": wav["duration_s"] if wav else None, "book_start_s": round(book_t, 3) if wav else None,
                             "render_current": rd.get("render_current", False), "missing_audio": len(rd.get("problems", []))})
        if wav:
            book_t += wav["duration_s"]

    findings = []
    engines, models = set(), set()
    for s in segs:
        atts = attempts_by_seg.get(s["id"], [])
        for a in atts:
            engines.add(a["engine"])
            if a["model_id"]:
                models.add(a["model_id"])
        sel = next((a for a in atts if a["id"] == s["selected_attempt_id"]), None)
        issues = loads(sel["issues_json"], []) if sel else []
        notable = [i for i in issues if i.get("severity") in ("error", "warning")]
        if s["status"] in ("flagged", "pending", "synthesizing") or notable or s["note"] or s["override_text"]:
            ch_start, seg_map = offsets.get(s["chapter_id"], (None, {}))
            m = seg_map.get(s["sid"])
            findings.append({
                "sid": s["sid"], "chapter": s["chapter_idx"], "chapter_title": s["chapter_title"], "status": s["status"],
                "chapter_offset_s": m["start_s"] if m else None,
                "book_offset_s": round(ch_start + m["start_s"], 3) if (m and ch_start is not None) else None,
                "wer": sel["wer"] if sel else None, "attempts": len(atts), "flag_reason": s["flag_reason"], "note": s["note"],
                "override_text": s["override_text"], "text": s["original_text"],
                "issues": [{"severity": i["severity"], "kind": i["kind"], "message": i["message"], "at_s": i.get("at_s")} for i in notable],
            })
    total = sum(counts.values())
    wers = [a["wer"] for s in segs for a in attempts_by_seg.get(s["id"], []) if a["id"] == s["selected_attempt_id"] and a["wer"] is not None]
    return {
        "report_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "studio_version": __version__,
        "book": {"id": book["id"], "uid": book["uid"], "title": book["title"], "author": book["author"], "narrator_credit": book["narrator_credit"],
                 "state": book["state"], "manuscript": manuscript},
        "narrator": {"voice": voice.get("name"), "kind": voice.get("kind"), "voice_version_id": vv.get("id"), "model_id": vv.get("model_id"),
                     "speaker": vv.get("speaker"), "instruct": vv.get("instruct"), "consent": loads(vv.get("consent_json"), None)},
        "provenance": {"engines": sorted(engines), "tts_models": sorted(models), "asr_model": loads(book["settings_json"], {}).get("models", {}).get("asr_model"),
                       "synth_fingerprint": book["synth_fingerprint"], "settings_snapshot": loads(book["settings_json"], {})},
        "counts": {"segments": total, **counts, "findings": len(findings)},
        "mean_wer": round(sum(wers) / len(wers), 4) if wers else None,
        "audio_duration_s": round(book_t, 2),
        "release": {"approved": bool(release), "approved_by": release["approved_by"] if release else None,
                    "approved_by_account": rel_audit["actor"] if rel_audit else None,
                    "approved_at": release["approved_at"] if release else None, "listen_scope": release["listen_scope"] if release else None},
        "chapters": chapter_rows,
        "findings": findings,
        "review_actions": [{"sid": r["sid"], "action": r["action"], "actor": r["actor"], "detail": loads(r["detail_json"], {}), "at": r["created_at"]} for r in reviews],
        "qa_limitations": QA_LIMITATIONS,
    }


def _fmt_t(s) -> str:
    if s is None:
        return "-"
    s = float(s)
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


def _fmt_pct(v, digits: int = 1) -> str:
    return "" if v is None else f"{v * 100:.{digits}f}%"


def render_html(rep: dict) -> str:
    e = html.escape
    b = rep["book"]
    rows_html = []
    for f in rep["findings"]:
        issues = "<br>".join(f"<span class='sev-{e(i['severity'])}'>{e(i['severity'])}</span> {e(i['message'])}" + (f" <small>@{i['at_s']}s</small>" if i.get("at_s") is not None else "") for i in f["issues"])
        rows_html.append(
            f"<tr><td><code>{e(f['sid'])}</code></td><td>{f['chapter']}</td><td>{_fmt_t(f['chapter_offset_s'])}</td><td>{_fmt_t(f['book_offset_s'])}</td>"
            f"<td>{e(f['status'])}</td><td>{_fmt_pct(f['wer'])}</td><td>{f['attempts']}</td>"
            f"<td>{e(f['text'][:240])}{'&hellip;' if len(f['text']) > 240 else ''}{('<div class=ovr>Spoken as: ' + e(f['override_text']) + '</div>') if f['override_text'] else ''}</td>"
            f"<td>{issues or e(f['flag_reason'] or '')}{('<div class=note>Note: ' + e(f['note']) + '</div>') if f['note'] else ''}</td></tr>"
        )
    ch_html = "".join(
        f"<tr><td>{c['index']}</td><td>{e(c['title'])}</td><td>{c['segments']}</td><td>{c['words']}</td><td>{_fmt_t(c['duration_s'])}</td><td>{_fmt_t(c['book_start_s'])}</td><td>{c['missing_audio']}</td></tr>"
        for c in rep["chapters"])
    counts = rep["counts"]
    rel = rep["release"]
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>QA report - {e(b['title'])}</title>
<style>body{{font:14px/1.5 -apple-system,system-ui,sans-serif;margin:2rem;color:#1d2433;background:#fff}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}
th,td{{border:1px solid #d5dae3;padding:.4rem .5rem;vertical-align:top;text-align:left}}th{{background:#f1f3f7}}code{{font-size:12px}}
.sev-error{{color:#a3121f;font-weight:600}}.sev-warning{{color:#8a5a00;font-weight:600}}.ovr,.note{{color:#38506e;font-style:italic}}
.box{{border:1px solid #d5dae3;border-radius:8px;padding:1rem;margin:1rem 0;background:#fafbfc}}</style></head><body>
<h1>QA report: {e(b['title'])}</h1>
<p>Author: {e(b['author'] or '-')} &middot; Narrator: {e(rep['narrator'].get('voice') or '-')} ({e(rep['narrator'].get('kind') or '')}) &middot; Generated {e(rep['generated_at'])} &middot; Studio {e(rep['studio_version'])}</p>
<div class="box"><strong>Status:</strong> {counts.get('segments', 0)} segments &middot; {counts.get('passed', 0)} passed QA &middot; {counts.get('flagged', 0)} flagged &middot;
{counts.get('approved', 0)} approved by reviewer &middot; {counts.get('pending', 0) + counts.get('synthesizing', 0)} not yet rendered &middot; mean WER {_fmt_pct(rep['mean_wer'], 2)}
&middot; audio {_fmt_t(rep['audio_duration_s'])}<br><strong>Release:</strong> {('Approved by ' + e(rel['approved_by']) + ' (' + e(rel['listen_scope'] or '') + ')') if rel['approved'] else 'NOT approved - draft'}</div>
<h2>Provenance</h2><p>Engines: {e(', '.join(rep['provenance']['engines']) or '-')} &middot; TTS models: {e(', '.join(rep['provenance']['tts_models']) or '-')} &middot; ASR: {e(rep['provenance']['asr_model'] or '-')}
&middot; Manuscript SHA-256: <code>{e(str(b['manuscript'].get('sha256', '-')))}</code></p>
<h2>Chapters</h2><table><thead><tr><th>#</th><th>Title</th><th>Segments</th><th>Words</th><th>Duration</th><th>Starts at</th><th>Missing audio</th></tr></thead><tbody>{ch_html}</tbody></table>
<h2>Findings ({len(rep['findings'])})</h2><table><thead><tr><th>Segment</th><th>Ch.</th><th>Ch. time</th><th>Book time</th><th>Status</th><th>WER</th><th>Attempts</th><th>Text</th><th>Issues</th></tr></thead>
<tbody>{''.join(rows_html) or '<tr><td colspan=9>No findings.</td></tr>'}</tbody></table>
<h2>QA limitations</h2><ul>{''.join(f'<li>{e(x)}</li>' for x in rep['qa_limitations'])}</ul></body></html>"""


def write_report(ctx, book_id: int) -> dict:
    rep = build_report(ctx, book_id)
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
    out_dir = book_dirs(book)["reports"]
    out_dir.mkdir(parents=True, exist_ok=True)
    base = title_filename(book["title"], "QA report") + time.strftime(" - %Y%m%d-%H%M%S")
    jp, hp = out_dir / f"{base}.json", out_dir / f"{base}.html"
    atomic_write_text(jp, json.dumps(rep, ensure_ascii=False, indent=2))
    atomic_write_text(hp, render_html(rep))
    draft = not rep["release"]["approved"]
    ids = {}
    with ctx.db.tx() as conn:
        for kind, p in (("report_json", jp), ("report_html", hp)):
            cur = conn.execute("INSERT INTO exports(book_id, kind, label, path, draft, size_bytes, created_at) VALUES (?,?,?,?,?,?,?)",
                               (book_id, kind, "QA report (" + kind.split("_")[1].upper() + ")", str(p), 1 if draft else 0, p.stat().st_size, now()))
            ids[kind] = cur.lastrowid
    return {"json": str(jp), "html": str(hp), "export_ids": ids, "findings": len(rep["findings"])}
