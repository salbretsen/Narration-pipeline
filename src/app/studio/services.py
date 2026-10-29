"""Service layer shared by the web API, the CLI and the smoke test.

Every state change goes through here, inside a transaction, with an audit trail.
"""
from __future__ import annotations

import os
import re
import statistics
import time
from pathlib import Path

from . import audio as A
from .assembly import active_release, chapter_readiness
from .chunker import build_segments
from .narrative import cast as N_cast
from .narrative import store as N_store
from .config import (CATEGORY_LABELS, ENGLISH_SPEAKERS, PRESET_SPEAKERS, STORAGE_CATEGORIES, Settings, book_snapshot,
                     merge_patch, synth_fingerprint)
from .db import row, rows, scalar
from .normalize import to_speech
from .parsing import ManuscriptError, ParseResult, detect_format, parse_manuscript
from .pipeline import book_dirs, resolve_take
from .storage import validate_dir
from .util import atomic_write_bytes, dumps, loads, new_uid, now, safe_filename, sha256_bytes, word_count
from .worker import refresh_book_state


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400, details=None):
        super().__init__(message)
        self.status = status
        self.details = details


class NeedsConfirmation(ServiceError):
    def __init__(self, message: str, details=None):
        super().__init__(message, 409, details)


DEFAULT_PREVIEW_TEXT = (
    "Chapter One. The rain had stopped by the time Eleanor reached the harbour, and the lamps along the quay "
    "were reflected in long, trembling lines on the black water."
)
DEFAULT_NARRATION_INSTRUCT = "Calm, clear, warm audiobook narration with natural pacing."


# ======================================================================= voices


def ensure_builtin_voices(ctx) -> None:
    s = ctx.settings()
    with ctx.db.tx() as conn:
        for spk in ENGLISH_SPEAKERS:
            if row(conn, "SELECT id FROM voices WHERE builtin=1 AND name=?", (spk,)):
                continue
            vid = conn.execute("INSERT INTO voices(uid, name, kind, description, builtin, created_at, updated_at) VALUES (?,?,?,?,1,?,?)",
                               (new_uid("voice"), spk, "preset", f"Qwen3-TTS CustomVoice preset speaker '{spk}'. {PRESET_SPEAKERS[spk]}.", now(), now())).lastrowid
            vvid = conn.execute("""INSERT INTO voice_versions(voice_id, version, model_id, speaker, language, instruct, consent_json, created_at)
                                   VALUES (?,1,?,?,?,?,?,?)""",
                                (vid, s.models.custom_voice_model, spk, s.models.language, DEFAULT_NARRATION_INSTRUCT,
                                 dumps({"type": "built-in synthetic speaker", "note": "No human voice recording involved."}), now())).lastrowid
            conn.execute("UPDATE voices SET current_version_id=? WHERE id=?", (vvid, vid))


def _voice_dict(conn, v: dict) -> dict:
    vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (v["current_version_id"],)) or {}
    used = scalar(conn, "SELECT COUNT(*) FROM books WHERE voice_id=? AND archived=0", (v["id"],), 0)
    return {
        "id": v["id"], "uid": v["uid"], "name": v["name"], "kind": v["kind"], "description": v["description"], "builtin": bool(v["builtin"]),
        "archived": bool(v["archived"]), "created_at": v["created_at"], "books_using": used,
        "version": {"id": vv.get("id"), "version": vv.get("version"), "model_id": vv.get("model_id"), "speaker": vv.get("speaker"),
                    "language": vv.get("language"), "instruct": vv.get("instruct"), "ref_text": vv.get("ref_text"),
                    "ref_duration_s": vv.get("ref_duration_s"), "has_reference": bool(vv.get("ref_audio_path")),
                    "consent": loads(vv.get("consent_json"), None), "created_at": vv.get("created_at")},
    }


def list_voices(ctx, include_archived: bool = False) -> list[dict]:
    with ctx.db.session() as conn:
        vs = rows(conn, "SELECT * FROM voices " + ("" if include_archived else "WHERE archived=0 ") + "ORDER BY builtin DESC, name")
        return [_voice_dict(conn, v) for v in vs]


def get_voice(ctx, voice_id: int) -> dict:
    with ctx.db.session() as conn:
        v = row(conn, "SELECT * FROM voices WHERE id=?", (voice_id,))
        if not v:
            raise ServiceError("Voice not found", 404)
        d = _voice_dict(conn, v)
        d["versions"] = rows(conn, "SELECT id, version, model_id, speaker, instruct, ref_text, created_at FROM voice_versions WHERE voice_id=? ORDER BY version DESC", (voice_id,))
        return d


def _check_name(conn, name: str, exclude_id: int | None = None) -> str:
    name = " ".join((name or "").split())
    if not 1 <= len(name) <= 80:
        raise ServiceError("Voice name must be 1-80 characters.", 422)
    dupe = row(conn, "SELECT id FROM voices WHERE lower(name)=lower(?) AND archived=0" + (" AND id<>?" if exclude_id else ""),
               (name, exclude_id) if exclude_id else (name,))
    if dupe:
        raise ServiceError(f"A voice named '{name}' already exists.", 409)
    return name


def create_preset_voice(ctx, name: str, speaker: str, instruct: str, language: str | None = None) -> dict:
    if speaker not in PRESET_SPEAKERS:
        raise ServiceError(f"Unsupported speaker '{speaker}'. Supported: {', '.join(PRESET_SPEAKERS)}", 422)
    s = ctx.settings()
    with ctx.db.tx() as conn:
        name = _check_name(conn, name)
        vid = conn.execute("INSERT INTO voices(uid, name, kind, description, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                           (new_uid("voice"), name, "preset", f"Preset speaker {speaker}", now(), now())).lastrowid
        vvid = conn.execute("INSERT INTO voice_versions(voice_id, version, model_id, speaker, language, instruct, consent_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
                            (vid, 1, s.models.custom_voice_model, speaker, language or s.models.language, (instruct or "").strip() or None,
                             dumps({"type": "built-in synthetic speaker"}), now())).lastrowid
        conn.execute("UPDATE voices SET current_version_id=? WHERE id=?", (vvid, vid))
        ctx.audit(conn, "voice", vid, "create_preset", {"speaker": speaker})
    return get_voice(ctx, vid)


def create_design_voice(ctx, name: str, description: str, language: str | None = None) -> dict:
    description = (description or "").strip()
    if not 10 <= len(description) <= 1000:
        raise ServiceError("Describe the voice in 10-1000 characters (age, gender, timbre, accent, pace, mood).", 422)
    s = ctx.settings()
    with ctx.db.tx() as conn:
        name = _check_name(conn, name)
        vid = conn.execute("INSERT INTO voices(uid, name, kind, description, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                           (new_uid("voice"), name, "design", description[:300], now(), now())).lastrowid
        vvid = conn.execute("INSERT INTO voice_versions(voice_id, version, model_id, language, instruct, consent_json, created_at) VALUES (?,?,?,?,?,?,?)",
                            (vid, 1, s.models.voice_design_model, language or s.models.language, description,
                             dumps({"type": "designed synthetic voice", "note": "Generated from a text description; no human recording."}), now())).lastrowid
        conn.execute("UPDATE voices SET current_version_id=? WHERE id=?", (vvid, vid))
        ctx.audit(conn, "voice", vid, "create_design", {"description": description})
    return get_voice(ctx, vid)


def create_clone_voice(ctx, name: str, filename: str, data: bytes, ref_text: str, consent: dict) -> dict:
    """Validate and store an authorised reference recording for Qwen3-TTS Base cloning."""
    if not consent.get("owner_attest"):
        raise ServiceError("You must confirm that you own this voice or have the speaker's written permission to clone it.", 422)
    rights_holder = " ".join(str(consent.get("rights_holder") or "").split())
    if not 2 <= len(rights_holder) <= 120:
        raise ServiceError("Enter the name of the speaker / rights holder (2-120 characters).", 422)
    ref_text = " ".join((ref_text or "").split())
    if not 5 <= len(ref_text) <= 1000:
        raise ServiceError("Enter the exact transcript of the reference clip (5-1000 characters).", 422)
    s = ctx.settings()
    ext = os.path.splitext((filename or "").lower())[1]
    if ext not in A.AUDIO_UPLOAD_EXTS:
        raise ServiceError("Reference audio must be WAV, MP3, M4A or FLAC.", 422)
    if len(data) > s.server.max_reference_mb * 1024 * 1024:
        raise ServiceError(f"Reference audio is larger than {s.server.max_reference_mb} MB.", 413)
    if not A.sniff_audio(data, ext):
        raise ServiceError(f"The file content does not match a {ext} audio file.", 422)
    uid = new_uid("voice")
    ref_dir = ctx.category_dir("references", s) / uid
    try:
        ref_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ServiceError(f"Reference storage is unavailable: {exc}", 503)
    original = ref_dir / ("original" + ext)
    atomic_write_bytes(original, data)
    ref_wav = ref_dir / "reference-24k.wav"
    try:
        info = A.convert_reference(original, ref_wav)
    except A.AudioValidationError as exc:
        original.unlink(missing_ok=True)
        try:
            ref_dir.rmdir()
        except OSError:
            pass
        raise ServiceError(str(exc), 422)
    words = word_count(ref_text)
    if info["duration_s"] and words / info["duration_s"] > 5.5:
        info["warnings"].append("The transcript looks long for this clip; it must match the audio exactly.")
    consent_rec = {"owner_attest": True, "rights_holder": rights_holder, "statement": str(consent.get("statement") or "")[:1000],
                   "attested_at": now(), "original_filename": safe_filename(filename), "sha256": sha256_bytes(data)}
    with ctx.db.tx() as conn:
        name = _check_name(conn, name)
        vid = conn.execute("INSERT INTO voices(uid, name, kind, description, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                           (uid, name, "clone", f"Cloned from an authorised reference of {rights_holder}", now(), now())).lastrowid
        vvid = conn.execute("""INSERT INTO voice_versions(voice_id, version, model_id, language, ref_audio_path, ref_original_path, ref_text,
                                   ref_sha256, ref_duration_s, consent_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (vid, 1, s.models.base_model, s.models.language, str(ref_wav), str(original), ref_text, sha256_bytes(data),
                             info["duration_s"], dumps(consent_rec), now())).lastrowid
        conn.execute("UPDATE voices SET current_version_id=? WHERE id=?", (vvid, vid))
        ctx.audit(conn, "voice", vid, "create_clone", {"rights_holder": rights_holder, "duration_s": info["duration_s"], "sha256": consent_rec["sha256"]})
        ctx.event(conn, "voice", f"Cloned voice '{name}' added (reference {info['duration_s']}s, consent recorded).")
    out = get_voice(ctx, vid)
    out["warnings"] = info["warnings"]
    return out


def update_voice(ctx, voice_id: int, instruct: str | None = None, speaker: str | None = None, name: str | None = None, description: str | None = None) -> dict:
    """Voice versions are immutable: a sound-affecting change creates a new version. Books keep
    the version they were created with until the operator explicitly switches them (which
    invalidates their audio)."""
    with ctx.db.tx() as conn:
        v = row(conn, "SELECT * FROM voices WHERE id=?", (voice_id,))
        if not v:
            raise ServiceError("Voice not found", 404)
        if name is not None and name.strip() != v["name"]:
            conn.execute("UPDATE voices SET name=?, updated_at=? WHERE id=?", (_check_name(conn, name, voice_id), now(), voice_id))
        if description is not None:
            conn.execute("UPDATE voices SET description=?, updated_at=? WHERE id=?", (description.strip()[:300], now(), voice_id))
        cur = row(conn, "SELECT * FROM voice_versions WHERE id=?", (v["current_version_id"],))
        new_instruct = cur["instruct"] if instruct is None else (instruct.strip() or None)
        new_speaker = cur["speaker"] if speaker is None else speaker
        if v["kind"] == "preset" and new_speaker not in PRESET_SPEAKERS:
            raise ServiceError("Unsupported speaker", 422)
        if v["kind"] == "design" and not (new_instruct and len(new_instruct) >= 10):
            raise ServiceError("A designed voice needs a description of at least 10 characters.", 422)
        if v["kind"] == "clone" and speaker is not None:
            raise ServiceError("Cloned voices have no speaker setting (Base models never take one).", 422)
        if (new_instruct, new_speaker) != (cur["instruct"], cur["speaker"]):
            ver = scalar(conn, "SELECT MAX(version) FROM voice_versions WHERE voice_id=?", (voice_id,), 0) + 1
            vvid = conn.execute("""INSERT INTO voice_versions(voice_id, version, model_id, speaker, language, instruct, ref_audio_path, ref_original_path,
                                       ref_text, ref_sha256, ref_duration_s, consent_json, created_at)
                                   SELECT voice_id, ?, model_id, ?, language, ?, ref_audio_path, ref_original_path, ref_text, ref_sha256, ref_duration_s,
                                          consent_json, ? FROM voice_versions WHERE id=?""",
                                (ver, new_speaker, new_instruct, now(), cur["id"])).lastrowid
            conn.execute("UPDATE voices SET current_version_id=?, updated_at=? WHERE id=?", (vvid, now(), voice_id))
            ctx.audit(conn, "voice", voice_id, "new_version", {"version": ver, "instruct": new_instruct, "speaker": new_speaker})
    out = get_voice(ctx, voice_id)
    with ctx.db.session() as conn:
        out["books_on_older_version"] = rows(conn, "SELECT id, title FROM books WHERE voice_id=? AND voice_version_id<>? AND archived=0",
                                            (voice_id, out["version"]["id"]))
    return out


def archive_voice(ctx, voice_id: int) -> None:
    with ctx.db.tx() as conn:
        v = row(conn, "SELECT * FROM voices WHERE id=?", (voice_id,))
        if not v:
            raise ServiceError("Voice not found", 404)
        if v["builtin"]:
            raise ServiceError("Built-in voices cannot be archived.", 422)
        conn.execute("UPDATE voices SET archived=1, updated_at=? WHERE id=?", (now(), voice_id))
        ctx.audit(conn, "voice", voice_id, "archive")


def enqueue_preview(ctx, voice_id: int, text: str, instruct: str | None = None, verify: bool = False, seed: int | None = None) -> dict:
    text = " ".join((text or "").split())
    if not 3 <= len(text) <= 600:
        raise ServiceError("Preview text must be 3-600 characters.", 422)
    with ctx.db.tx() as conn:
        v = row(conn, "SELECT * FROM voices WHERE id=? AND archived=0", (voice_id,))
        if not v:
            raise ServiceError("Voice not found", 404)
        jid = conn.execute("INSERT INTO jobs(kind, payload_json, status, position, created_at, updated_at) VALUES ('preview',?, 'queued', 0, ?, ?)",
                           (dumps({"voice_id": voice_id, "voice_version_id": v["current_version_id"], "text": text,
                                   "instruct": (instruct or "").strip() or None, "verify": bool(verify), "seed": seed or 1234}), now(), now())).lastrowid
    return get_job(ctx, jid)


def promote_preview_to_clone(ctx, job_id: int, name: str) -> dict:
    """Turn a designed-voice preview into a stable cloned narrator (recommended for long books:
    VoiceDesign re-creates the voice on every call, so it can drift between segments)."""
    with ctx.db.session() as conn:
        j = row(conn, "SELECT * FROM jobs WHERE id=? AND kind='preview' AND status='completed'", (job_id,))
        if not j:
            raise ServiceError("Completed preview not found", 404)
        p, r = loads(j["payload_json"], {}), loads(j["result_json"], {})
        v = row(conn, "SELECT * FROM voices WHERE id=?", (p.get("voice_id"),))
    if not v or v["kind"] != "design":
        raise ServiceError("Only previews of designed voices can be promoted to a stable narrator.", 422)
    path = Path(r.get("path", ""))
    if not path.exists():
        raise ServiceError("The preview audio file no longer exists; generate it again.", 410)
    data = path.read_bytes()
    out = create_clone_voice(ctx, name, path.name, data, p["text"],
                             {"owner_attest": True, "rights_holder": f"Synthetic voice designed in this studio ('{v['name']}')",
                              "statement": f"Reference generated locally by Qwen3-TTS VoiceDesign (preview job {job_id}); no human voice involved."})
    return out


# ============================================================= reference-voice lab


def recordings_dir(ctx) -> Path:
    d = ctx.data_dir / "recordings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _rec_meta_path(ctx, rid: str) -> Path:
    if not re.fullmatch(r"rec_[0-9a-f]{8}", rid or ""):
        raise ServiceError("Recording not found", 404)
    return recordings_dir(ctx) / rid / "meta.json"


def get_recording(ctx, rid: str) -> dict:
    p = _rec_meta_path(ctx, rid)
    if not p.exists():
        raise ServiceError("Recording not found", 404)
    return loads(p.read_text("utf-8"), {})


def list_recordings(ctx) -> list[dict]:
    import shutil as _sh

    out, cutoff = [], now() - 14 * 86400
    for d in recordings_dir(ctx).iterdir():
        m = d / "meta.json"
        if not m.exists():
            continue
        meta = loads(m.read_text("utf-8"), {})
        if meta.get("created_at", now()) < cutoff:  # drafts expire after 14 days; created narrators keep their own copy
            _sh.rmtree(d, ignore_errors=True)
            continue
        out.append(meta)
    return sorted(out, key=lambda r: r.get("created_at", 0), reverse=True)


def create_recording(ctx, filename: str, data: bytes, script_id: str, ref_text: str, label: str = "") -> dict:
    """Store a studio recording as a draft reference: validate, analyse, convert. Nothing becomes a narrator yet."""
    from . import reference as R

    s = ctx.settings()
    ext = os.path.splitext((filename or "").lower())[1]
    if ext not in A.AUDIO_UPLOAD_EXTS:
        raise ServiceError("Recording must be WAV, MP3, M4A or FLAC audio.", 422)
    if len(data) > s.server.max_reference_mb * 1024 * 1024:
        raise ServiceError(f"Recording is larger than {s.server.max_reference_mb} MB.", 413)
    if not A.sniff_audio(data, ext):
        raise ServiceError(f"The data is not a valid {ext} audio file.", 422)
    ref_text = " ".join((ref_text or "").split())
    if not 5 <= len(ref_text) <= 1000:
        raise ServiceError("The transcript must be the exact words spoken (5-1000 characters).", 422)
    rid = new_uid("rec").replace("rec_", "rec_")
    d = recordings_dir(ctx) / rid
    d.mkdir(parents=True)
    try:
        orig = d / ("original" + ext)
        atomic_write_bytes(orig, data)
        try:
            analysis = R.analyze_recording(orig)
        except A.AudioToolError as exc:
            raise ServiceError(f"That audio could not be decoded: {exc}", 422)
        quality = R.quality_report(analysis["metrics"])
        try:
            conv = A.convert_reference(orig, d / "reference-24k.wav")
        except A.AudioValidationError as exc:
            raise ServiceError(str(exc), 422, {"analysis": analysis["metrics"], "quality": quality})
        script = R.script_by_id(script_id)
        meta = {"id": rid, "created_at": now(), "script_id": script_id if script else None, "script_title": script["title"] if script else "Own text",
                "ref_text": ref_text, "label": (label or "").strip()[:80], "duration_s": conv["duration_s"], "metrics": analysis["metrics"],
                "issues": analysis["issues"], "quality": quality, "sha256": sha256_bytes(data), "filename": "original" + ext,
                "convert_warnings": conv["warnings"]}
        atomic_write_bytes(d / "meta.json", dumps(meta).encode())
    except BaseException:
        import shutil as _sh

        _sh.rmtree(d, ignore_errors=True)
        raise
    with ctx.db.tx() as conn:
        ctx.audit(conn, "recording", rid, "created", {"script": script_id, "duration_s": conv["duration_s"], "verdict": quality["verdict"]})
    return meta


def delete_recording(ctx, rid: str) -> None:
    import shutil as _sh

    get_recording(ctx, rid)
    _sh.rmtree(recordings_dir(ctx) / rid, ignore_errors=True)
    with ctx.db.tx() as conn:
        ctx.audit(conn, "recording", rid, "deleted")


def update_recording_text(ctx, rid: str, ref_text: str) -> dict:
    meta = get_recording(ctx, rid)
    ref_text = " ".join((ref_text or "").split())
    if not 5 <= len(ref_text) <= 1000:
        raise ServiceError("The transcript must be 5-1000 characters.", 422)
    meta["ref_text"] = ref_text
    atomic_write_bytes(_rec_meta_path(ctx, rid), dumps(meta).encode())
    return meta


def enqueue_recording_job(ctx, rid: str, mode: str, text: str | None = None) -> dict:
    """mode 'check': transcribe the take with Whisper and compare with its transcript.
    mode 'audition': synthesize a test passage with the Base model using this take as the reference."""
    from . import reference as R

    get_recording(ctx, rid)
    if mode not in ("check", "audition"):
        raise ServiceError("mode must be check or audition", 422)
    text = " ".join((text or R.AUDITION_TEXT).split())
    if mode == "audition" and not 3 <= len(text) <= 600:
        raise ServiceError("Audition text must be 3-600 characters.", 422)
    with ctx.db.tx() as conn:
        jid = conn.execute("INSERT INTO jobs(kind, payload_json, status, position, created_at, updated_at) VALUES ('preview',?, 'queued', 0, ?, ?)",
                           (dumps({"recording_id": rid, "mode": mode, "text": text}), now(), now())).lastrowid
    return get_job(ctx, jid)


def create_voice_from_recording(ctx, rid: str, name: str, consent: dict) -> dict:
    meta = get_recording(ctx, rid)
    if meta["quality"]["verdict"] == "poor":
        raise ServiceError("This take was rated poor quality. Re-record it before creating a narrator.", 422, {"quality": meta["quality"]})
    d = recordings_dir(ctx) / rid
    data = (d / meta["filename"]).read_bytes()
    statement = (consent.get("statement") or "").strip()
    consent = {**consent, "statement": (statement + " " if statement else "") + f"Recorded in the studio reference lab ({meta['script_title']}, recording {rid})."}
    return create_clone_voice(ctx, name, meta["filename"], data, meta["ref_text"], consent)


# ====================================================================== imports


def _parse_preview(res: ParseResult, settings: Settings) -> dict:
    ch_settings = settings.chunking
    chapters = []
    tw = ts = 0
    for i, ch in enumerate(res.chapters):
        segs = build_segments(ch.readable(), ch_settings.min_chars, ch_settings.max_chars, ch_settings.merge_short_paragraphs)
        n_segs = len(segs) + (1 if ch_settings.speak_chapter_titles and segs else 0)
        words = ch.words()
        if ch.include:
            tw += words
            ts += n_segs
        paras = [b.text for b in ch.readable() if b.kind == "paragraph"]
        chapters.append({
            "index": i, "title": ch.title, "source": ch.source, "include": ch.include, "include_reason": ch.include_reason,
            "words": words, "segments": n_segs, "est_minutes": round(words / settings.qa.words_per_minute, 1),
            "paragraphs": len(paras), "sample": (" ".join(paras))[:400],
            "headings": [b.text for b in ch.readable() if b.kind == "heading"][:10],
            "skipped": [{"reason": b.reason, "text": b.text[:200]} for b in ch.skipped()][:50],
            "skipped_count": len(ch.skipped()),
        })
    return {"title": res.title, "author": res.author, "language": res.language, "warnings": res.warnings, "chapters": chapters,
            "totals": {"chapters": len(chapters), "included_chapters": sum(1 for c in chapters if c["include"]), "words": tw,
                       "segments": ts, "est_hours": round(tw / settings.qa.words_per_minute / 60, 2),
                       "not_read": sum(c["skipped_count"] for c in chapters)}}


def create_import(ctx, filename: str, data: bytes) -> dict:
    s = ctx.settings()
    if len(data) > s.server.max_manuscript_mb * 1024 * 1024:
        raise ServiceError(f"Manuscript is larger than {s.server.max_manuscript_mb} MB.", 413)
    try:
        fmt = detect_format(filename)
        res = parse_manuscript(filename, data)
    except ManuscriptError as exc:
        raise ServiceError(str(exc), 422)
    except Exception as exc:  # malformed input must never crash the server
        raise ServiceError(f"Could not read this manuscript: {type(exc).__name__}: {exc}", 422)
    token = new_uid("imp")
    safe = safe_filename(filename, default=f"manuscript.{fmt}")
    tmp_dir = ctx.imports_dir() / token
    atomic_write_bytes(tmp_dir / safe, data)
    digest = sha256_bytes(data)
    preview = _parse_preview(res, s)
    preview.update({"token": token, "filename": safe, "format": fmt, "sha256": digest, "size_bytes": len(data)})
    if not preview["title"]:
        preview["title"] = os.path.splitext(safe)[0].replace("_", " ")
    with ctx.db.tx() as conn:
        dupe = row(conn, "SELECT b.id, b.title FROM manuscripts m JOIN books b ON b.id=m.book_id WHERE m.sha256=? AND b.archived=0", (digest,))
        if dupe:
            preview["warnings"].insert(0, f"This exact file was already imported as '{dupe['title']}' (book #{dupe['id']}). Importing again creates a separate project.")
            preview["duplicate_of"] = dupe["id"]
        conn.execute("INSERT INTO imports(token, original_name, stored_path, sha256, format, preview_json, created_at) VALUES (?,?,?,?,?,?,?)",
                     (token, safe, str(tmp_dir / safe), digest, fmt, dumps(preview), now()))
        ctx.event(conn, "upload", f"Uploaded '{safe}' for preview ({preview['totals']['words']} words, {preview['totals']['chapters']} chapters)")
    _cleanup_imports(ctx)
    return preview


def _cleanup_imports(ctx, max_age_s: float = 86400 * 2) -> None:
    cutoff = now() - max_age_s
    with ctx.db.tx() as conn:
        old = rows(conn, "SELECT * FROM imports WHERE consumed_book_id IS NULL AND created_at < ?", (cutoff,))
        for imp in old:
            p = Path(imp["stored_path"])
            p.unlink(missing_ok=True)
            try:
                p.parent.rmdir()
            except OSError:
                pass
            conn.execute("DELETE FROM imports WHERE id=?", (imp["id"],))


def get_import(ctx, token: str) -> dict:
    with ctx.db.session() as conn:
        imp = row(conn, "SELECT * FROM imports WHERE token=?", (token,))
    if not imp:
        raise ServiceError("Import not found or expired; upload the file again.", 404)
    return imp


def _book_storage(ctx, uid: str) -> dict:
    s = ctx.settings()
    out = {}
    for c in STORAGE_CATEGORIES:
        if c == "references":
            continue
        out[c] = str((ctx.category_dir(c, s) / uid).resolve())
    return out


def confirm_import(ctx, token: str, meta: dict) -> dict:
    imp = get_import(ctx, token)
    if imp["consumed_book_id"]:
        return get_book(ctx, imp["consumed_book_id"])  # idempotent
    s = ctx.settings()
    preview = loads(imp["preview_json"], {})
    data = Path(imp["stored_path"]).read_bytes()
    res = parse_manuscript(imp["original_name"], data)  # deterministic re-parse of the exact bytes
    include = meta.get("include")
    if include is None:
        include = [i for i, c in enumerate(res.chapters) if c.include]
    include = sorted({int(i) for i in include if 0 <= int(i) < len(res.chapters)})
    if not include:
        raise ServiceError("Select at least one chapter to narrate.", 422)
    title = " ".join(str(meta.get("title") or preview.get("title") or "Untitled").split())[:300]
    with ctx.db.session() as conn:
        voice = row(conn, "SELECT * FROM voices WHERE id=? AND archived=0", (meta.get("voice_id"),))
        if not voice:
            raise ServiceError("Choose an approved narrator voice.", 422)
        vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (voice["current_version_id"],))
    uid = new_uid("bk")
    storage = _book_storage(ctx, uid)
    try:
        for d in storage.values():
            Path(d).mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ServiceError(f"Storage location is unavailable: {exc}. Check Settings & Storage.", 503)
    stored = Path(storage["manuscripts"]) / imp["original_name"]
    atomic_write_bytes(stored, data)
    try:
        os.chmod(stored, 0o444)  # immutable original
    except OSError:
        pass
    snap = book_snapshot(s)
    instructions = str(meta.get("instructions") or "").strip()[:1000]
    fp = synth_fingerprint(snap, vv, instructions)
    ch_s = s.chunking
    ns = s.narrative
    semantic = ns.semantic_segmentation
    nar_plans, nar_cast, nar_seconds = (N_store.prepare_import(res, include, s, ns) if semantic else ([], None, 0.0))
    with ctx.db.tx() as conn:
        bid = conn.execute(
            """INSERT INTO books(uid, title, author, narrator_credit, publisher, year, description, language, genre, voice_id, voice_version_id,
                   instructions, settings_json, synth_fingerprint, storage_json, state, narrative_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'imported', ?, ?, ?)""",
            (uid, title, str(meta.get("author") or preview.get("author") or "")[:300], str(meta.get("narrator_credit") or voice["name"])[:200],
             str(meta.get("publisher") or "")[:200], str(meta.get("year") or "")[:20], str(meta.get("description") or "")[:4000],
             str(meta.get("language") or s.models.language)[:40], str(meta.get("genre") or "Audiobook")[:60], voice["id"], vv["id"],
             instructions, dumps(snap), fp, dumps(storage), dumps({"semantic_segmentation": True} if semantic else {}), now(), now()),
        ).lastrowid
        manifest = {"format": res.format, "warnings": res.warnings, "chapters": []}
        total_segments = 0
        if semantic:
            char_ids = N_cast.sync_cast(conn, bid, nar_cast, 1)
            narrator_id = N_cast.ensure_narrator(conn, bid)
            book_row = {"id": bid, "uid": uid, "narrative_json": dumps({"semantic_segmentation": True})}
        for out_idx, ci in enumerate(include, start=1):
            ch = res.chapters[ci]
            words = ch.words()
            if semantic:
                plan = nar_plans[out_idx - 1]
                chid = conn.execute("INSERT INTO chapters(book_id, idx, title, source_ref, word_count, segment_count, est_duration_s) VALUES (?,?,?,?,?,?,?)",
                                    (bid, out_idx, ch.title, ch.source, words, 0, words * 60.0 / s.qa.words_per_minute)).lastrowid
                N_store.persist_structure(conn, bid, uid, chid, out_idx, plan.struct, char_ids, narrator_id)
                total_segments += N_store.resegment_chapter(ctx, conn, book_row, {"id": chid, "idx": out_idx}, ns, s)["segments"]
                continue
            segs = build_segments(ch.readable(), ch_s.min_chars, ch_s.max_chars, ch_s.merge_short_paragraphs)
            auto_title = ch.title == "Opening" or bool(re.fullmatch(r"Section \d+", ch.title))
            if ch_s.speak_chapter_titles and ch.title and not auto_title:
                from .chunker import Segment
                segs.insert(0, Segment(ch.title, "heading", -1, "heading"))
            chid = conn.execute("INSERT INTO chapters(book_id, idx, title, source_ref, word_count, segment_count, est_duration_s) VALUES (?,?,?,?,?,?,?)",
                                (bid, out_idx, ch.title, ch.source, words, len(segs), words * 60.0 / s.qa.words_per_minute)).lastrowid
            for n, sg in enumerate(segs, start=1):
                narr = to_speech(sg.text) if ch_s.normalize_for_speech else sg.text
                conn.execute("""INSERT INTO segments(sid, book_id, chapter_id, idx, para_idx, kind, pause_kind, original_text, narration_text, status, updated_at)
                                VALUES (?,?,?,?,?,?,?,?,?, 'pending', ?)""",
                             (f"{uid}-c{out_idx:03d}-s{n:04d}", bid, chid, n, sg.para_idx, sg.kind, sg.pause_kind, sg.text, narr or sg.text, now()))
            total_segments += len(segs)
        if semantic:
            review = scalar(conn, "SELECT COUNT(*) FROM nar_spans WHERE book_id=? AND needs_review=1", (bid,), 0)
            N_store.set_stage(conn, bid, "structure", "done", {"seconds": round(nar_seconds, 3), "chapters": len(nar_plans), "review": review,
                                                                "spans": sum(len(p.struct.spans) for p in nar_plans)})
            N_store.set_stage(conn, bid, "characters", "done", {"characters": len(char_ids) - 1})
            N_store.set_stage(conn, bid, "segmentation", "done", {"segments": total_segments})
        for i, ch in enumerate(res.chapters):
            manifest["chapters"].append({"index": i, "title": ch.title, "source": ch.source, "narrated": i in include,
                                         "excluded_reason": None if i in include else (ch.include_reason or "Excluded by operator"),
                                         "words": ch.words(), "not_read": [{"reason": b.reason, "text": b.text[:300]} for b in ch.skipped()]})
        conn.execute("INSERT INTO manuscripts(book_id, original_name, stored_path, sha256, size_bytes, format, parse_report_json, imported_at) VALUES (?,?,?,?,?,?,?,?)",
                     (bid, imp["original_name"], str(stored), imp["sha256"], len(data), res.format, dumps(manifest), now()))
        conn.execute("UPDATE imports SET consumed_book_id=? WHERE id=?", (bid, imp["id"]))
        ctx.audit(conn, "book", bid, "import", {"file": imp["original_name"], "sha256": imp["sha256"], "chapters": len(include), "segments": total_segments, "voice_version": vv["id"]})
        ctx.event(conn, "import", f"Imported '{title}': {len(include)} chapters, {total_segments} segments", bid)
    Path(imp["stored_path"]).unlink(missing_ok=True)
    try:
        Path(imp["stored_path"]).parent.rmdir()
    except OSError:
        pass
    if meta.get("queue"):
        enqueue_book(ctx, bid)
    return get_book(ctx, bid)


# ======================================================================== books


def _book_stats(conn, book_ids: list[int] | None = None) -> dict[int, dict]:
    q = "SELECT book_id, status, COUNT(*) n FROM segments" + (f" WHERE book_id IN ({','.join('?' * len(book_ids))})" if book_ids else "") + " GROUP BY book_id, status"
    stats: dict[int, dict] = {}
    for r in rows(conn, q, book_ids or ()):
        stats.setdefault(r["book_id"], {})[r["status"]] = r["n"]
    durs = rows(conn, """SELECT s.book_id, SUM(a.duration_s) d FROM segments s JOIN attempts a ON a.id = s.selected_attempt_id
                         GROUP BY s.book_id""")
    for d in durs:
        stats.setdefault(d["book_id"], {})["_audio_s"] = d["d"] or 0.0
    return stats


def _book_summary(conn, b: dict, st: dict) -> dict:
    total = sum(v for k, v in st.items() if not k.startswith("_"))
    produced = total - st.get("pending", 0) - st.get("synthesizing", 0)
    job = row(conn, "SELECT id, status, paused_reason, error, current_segment_sid, retries FROM jobs WHERE book_id=? AND kind='book' ORDER BY id DESC LIMIT 1", (b["id"],))
    voice = row(conn, "SELECT name, kind FROM voices WHERE id=?", (b["voice_id"],)) or {}
    m = row(conn, "SELECT original_name, format FROM manuscripts WHERE book_id=? ORDER BY id DESC LIMIT 1", (b["id"],)) or {}
    est = scalar(conn, "SELECT SUM(est_duration_s) FROM chapters WHERE book_id=?", (b["id"],), 0.0)
    cur_ch = None
    if job and job["current_segment_sid"]:
        cur_ch = row(conn, "SELECT c.idx, c.title FROM segments s JOIN chapters c ON c.id=s.chapter_id WHERE s.sid=?", (job["current_segment_sid"],))
    retries = scalar(conn, "SELECT COUNT(*) FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=? AND a.attempt_no>1", (b["id"],), 0)
    return {
        "id": b["id"], "uid": b["uid"], "title": b["title"], "author": b["author"], "state": b["state"], "last_error": b["last_error"],
        "created_at": b["created_at"], "voice": voice.get("name"), "voice_kind": voice.get("kind"), "source": m.get("original_name"), "format": m.get("format"),
        "segments": total, "produced": produced, "passed": st.get("passed", 0), "flagged": st.get("flagged", 0), "approved": st.get("approved", 0),
        "pending": st.get("pending", 0) + st.get("synthesizing", 0), "progress": round(produced / total, 4) if total else 0.0,
        "est_duration_s": est, "audio_duration_s": round(st.get("_audio_s", 0.0), 1), "retries": retries,
        "job": job, "current_chapter": cur_ch, "released": b["state"] == "approved",
    }


def list_books(ctx, include_archived: bool = False) -> list[dict]:
    with ctx.db.session() as conn:
        books = rows(conn, "SELECT * FROM books " + ("" if include_archived else "WHERE archived=0 ") + "ORDER BY created_at DESC")
        stats = _book_stats(conn)
        return [_book_summary(conn, b, stats.get(b["id"], {})) for b in books]


def get_book(ctx, book_id: int) -> dict:
    with ctx.db.session() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        if not b:
            raise ServiceError("Book not found", 404)
        out = _book_summary(conn, b, _book_stats(conn, [book_id]).get(book_id, {}))
        out.update({k: b[k] for k in ("narrator_credit", "publisher", "year", "description", "language", "genre", "instructions", "synth_fingerprint", "archived")})
        out["has_cover"] = bool(b["cover_path"] and Path(b["cover_path"]).exists())
        out["settings"] = loads(b["settings_json"], {})
        out["storage"] = loads(b["storage_json"], {})
        vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (b["voice_version_id"],)) or {}
        v = row(conn, "SELECT * FROM voices WHERE id=?", (b["voice_id"],)) or {}
        out["voice_detail"] = {"id": v.get("id"), "name": v.get("name"), "kind": v.get("kind"), "version": vv.get("version"), "version_id": vv.get("id"),
                               "current_version_id": v.get("current_version_id"), "model_id": vv.get("model_id"), "speaker": vv.get("speaker"),
                               "instruct": vv.get("instruct")}
        out["voice_outdated"] = bool(v and v.get("current_version_id") != b["voice_version_id"])
        ready = chapter_readiness(conn, b)
        out["chapters"] = []
        for r in ready:
            ch = r["chapter"]
            st = {x["status"]: x["n"] for x in rows(conn, "SELECT status, COUNT(*) n FROM segments WHERE chapter_id=? GROUP BY status", (ch["id"],))}
            out["chapters"].append({"id": ch["id"], "index": ch["idx"], "title": ch["title"], "words": ch["word_count"], "segments": ch["segment_count"],
                                    "est_duration_s": ch["est_duration_s"], "status_counts": st, "missing_audio": len(r["problems"]),
                                    "render_current": r["render_current"],
                                    "renders": {k: {"id": v_["id"], "duration_s": v_["duration_s"], "draft": bool(v_["draft"]), "created_at": v_["created_at"],
                                                    "current": r["render_current"]} for k, v_ in r["renders"].items()}})
        m = row(conn, "SELECT * FROM manuscripts WHERE book_id=? ORDER BY id DESC LIMIT 1", (book_id,)) or {}
        out["manuscript"] = {"name": m.get("original_name"), "sha256": m.get("sha256"), "size_bytes": m.get("size_bytes"), "format": m.get("format"),
                             "manifest": loads(m.get("parse_report_json"), {})}
        out["exports"] = rows(conn, "SELECT id, kind, label, draft, size_bytes, duration_s, created_at FROM exports WHERE book_id=? ORDER BY id DESC LIMIT 50", (book_id,))
        out["release"] = active_release(conn, book_id)
        out["release_check"] = release_check_conn(conn, b)
        out["logs"] = rows(conn, "SELECT id, level, message, created_at FROM job_logs WHERE book_id=? AND level<>'debug' ORDER BY id DESC LIMIT 40", (book_id,))
        out["utility_jobs"] = rows(conn, "SELECT id, kind, status, error, result_json, created_at, finished_at FROM jobs WHERE book_id=? AND kind IN ('assemble','export','analyze') ORDER BY id DESC LIMIT 10", (book_id,))
    out["narrative"] = N_store.narrative_status(ctx, book_id)
    return out


def update_book_meta(ctx, book_id: int, patch: dict) -> dict:
    allowed = {"title": 300, "author": 300, "narrator_credit": 200, "publisher": 200, "year": 20, "description": 4000, "genre": 60, "language": 40}
    sets, vals = [], []
    for k, v in patch.items():
        if k in allowed and v is not None:
            v = str(v).strip()[: allowed[k]]
            if k == "title" and not v:
                raise ServiceError("Title cannot be empty", 422)
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return get_book(ctx, book_id)
    with ctx.db.tx() as conn:
        if not row(conn, "SELECT id FROM books WHERE id=?", (book_id,)):
            raise ServiceError("Book not found", 404)
        conn.execute(f"UPDATE books SET {', '.join(sets)}, updated_at=? WHERE id=?", (*vals, now(), book_id))
        ctx.audit(conn, "book", book_id, "metadata", {k: patch[k] for k in patch if k in allowed})
    return get_book(ctx, book_id)


def set_cover(ctx, book_id: int, filename: str, data: bytes) -> dict:
    ext = os.path.splitext((filename or "").lower())[1]
    if ext not in (".jpg", ".jpeg", ".png"):
        raise ServiceError("Cover must be a JPEG or PNG image.", 422)
    if len(data) > 10 * 1024 * 1024:
        raise ServiceError("Cover image must be under 10 MB.", 413)
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
        raise ServiceError("File content is not a JPEG or PNG image.", 422)
    with ctx.db.session() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
    if not b:
        raise ServiceError("Book not found", 404)
    dest = book_dirs(b)["manuscripts"] / ("cover.png" if data[:4] == b"\x89PNG" else "cover.jpg")
    atomic_write_bytes(dest, data)
    with ctx.db.tx() as conn:
        conn.execute("UPDATE books SET cover_path=?, updated_at=? WHERE id=?", (str(dest), now(), book_id))
        ctx.audit(conn, "book", book_id, "cover", {"bytes": len(data)})
    return get_book(ctx, book_id)


def _invalidate_audio(conn, ctx, book_id: int, reason: str) -> int:
    n = scalar(conn, "SELECT COUNT(*) FROM segments WHERE book_id=? AND status<>'pending'", (book_id,), 0)
    conn.execute("""UPDATE segments SET round=round+1, attempts_used=0, status='pending', selected_attempt_id=NULL, flag_reason=NULL, updated_at=?
                    WHERE book_id=?""", (now(), book_id))
    conn.execute("DELETE FROM chapter_renders WHERE chapter_id IN (SELECT id FROM chapters WHERE book_id=?)", (book_id,))
    _revoke(conn, ctx, book_id, reason)
    return n


def _revoke(conn, ctx, book_id: int, reason: str) -> None:
    rel = active_release(conn, book_id)
    if rel:
        conn.execute("UPDATE releases SET revoked_at=?, revoked_reason=? WHERE id=?", (now(), reason[:500], rel["id"]))
        ctx.audit(conn, "book", book_id, "release_revoked", {"reason": reason})
        ctx.event(conn, "release", f"Release approval revoked: {reason}", book_id, "warning")


def change_narrator(ctx, book_id: int, voice_id: int, instructions: str | None, confirm_invalidate: bool) -> dict:
    with ctx.db.session() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        v = row(conn, "SELECT * FROM voices WHERE id=? AND archived=0", (voice_id,))
        if not b or not v:
            raise ServiceError("Book or voice not found", 404)
        vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (v["current_version_id"],))
        running = row(conn, "SELECT id FROM jobs WHERE book_id=? AND kind='book' AND status='running'", (book_id,))
    instr = b["instructions"] if instructions is None else instructions.strip()[:1000]
    fp = synth_fingerprint(loads(b["settings_json"], {}), vv, instr)
    if fp == b["synth_fingerprint"]:
        return get_book(ctx, book_id)
    if running:
        raise ServiceError("Pause the running job before changing the narrator.", 409)
    with ctx.db.tx() as conn:
        rendered = scalar(conn, "SELECT COUNT(*) FROM segments WHERE book_id=? AND status<>'pending'", (book_id,), 0)
        if rendered and not confirm_invalidate:
            raise NeedsConfirmation(f"{rendered} segments were rendered with the current narrator settings. Changing them marks every segment "
                                    "for regeneration (old takes are kept as history, never mixed into the book).", {"rendered_segments": rendered})
        n = _invalidate_audio(conn, ctx, book_id, "narrator settings changed")
        conn.execute("UPDATE books SET voice_id=?, voice_version_id=?, instructions=?, synth_fingerprint=?, narrator_credit=CASE WHEN narrator_credit='' THEN ? ELSE narrator_credit END, updated_at=? WHERE id=?",
                     (v["id"], vv["id"], instr, fp, v["name"], now(), book_id))
        ctx.audit(conn, "book", book_id, "narrator_changed", {"voice_id": v["id"], "voice_version_id": vv["id"], "instructions": instr, "invalidated_segments": n})
        ctx.event(conn, "narrator", f"Narrator changed to '{v['name']}' v{vv['version']}; {n} segments marked for regeneration.", book_id, "warning" if n else "info")
        refresh_book_state(conn, book_id)
    return get_book(ctx, book_id)


def resnapshot_settings(ctx, book_id: int, confirm_invalidate: bool) -> dict:
    """Apply current global settings to an existing book. Changes that affect the sound of
    the narration require confirmation and regenerate every segment; QA, pause and export
    changes apply to future checks/assembly only. Chunk size changes never re-split an
    existing book (segment IDs stay stable)."""
    s = ctx.settings()
    snap = book_snapshot(s)
    with ctx.db.session() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        if not b:
            raise ServiceError("Book not found", 404)
        vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (b["voice_version_id"],))
    old = loads(b["settings_json"], {})
    snap["chunking"] = old.get("chunking", snap["chunking"])
    fp = synth_fingerprint(snap, vv, b["instructions"])
    with ctx.db.tx() as conn:
        if fp != b["synth_fingerprint"]:
            rendered = scalar(conn, "SELECT COUNT(*) FROM segments WHERE book_id=? AND status<>'pending'", (book_id,), 0)
            if rendered and not confirm_invalidate:
                raise NeedsConfirmation(f"The new model/voice settings change how narration sounds; {rendered} rendered segments would be regenerated.",
                                        {"rendered_segments": rendered})
            _invalidate_audio(conn, ctx, book_id, "synthesis settings changed")
        conn.execute("UPDATE books SET settings_json=?, synth_fingerprint=?, updated_at=? WHERE id=?", (dumps(snap), fp, now(), book_id))
        ctx.audit(conn, "book", book_id, "settings_resnapshot", {"fingerprint_changed": fp != b["synth_fingerprint"]})
        refresh_book_state(conn, book_id)
    return get_book(ctx, book_id)


def archive_book(ctx, book_id: int, archived: bool = True) -> dict:
    with ctx.db.tx() as conn:
        if archived and row(conn, "SELECT id FROM jobs WHERE book_id=? AND status IN ('queued','running','paused')", (book_id,)):
            raise ServiceError("Cancel or finish the book's queue job first.", 409)
        conn.execute("UPDATE books SET archived=?, updated_at=? WHERE id=?", (1 if archived else 0, now(), book_id))
        ctx.audit(conn, "book", book_id, "archive" if archived else "unarchive")
    return get_book(ctx, book_id)


# ======================================================================== queue


def get_job(ctx, job_id: int) -> dict:
    with ctx.db.session() as conn:
        j = row(conn, "SELECT * FROM jobs WHERE id=?", (job_id,))
    if not j:
        raise ServiceError("Job not found", 404)
    j["payload"] = loads(j.pop("payload_json"), {})
    j["result"] = loads(j.pop("result_json"), None)
    return j


def enqueue_book(ctx, book_id: int, front: bool = False) -> dict:
    """Idempotent: returns the existing active job instead of creating a duplicate."""
    with ctx.db.tx() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=? AND archived=0", (book_id,))
        if not b:
            raise ServiceError("Book not found", 404)
        existing = row(conn, "SELECT id FROM jobs WHERE book_id=? AND kind='book' AND status IN ('queued','running','paused')", (book_id,))
        if existing:
            if front:
                conn.execute("UPDATE jobs SET position=?, updated_at=? WHERE id=? AND status<>'running'",
                             (scalar(conn, "SELECT MIN(position) FROM jobs WHERE kind='book'", (), 0) - 1, now(), existing["id"]))
            jid, created = existing["id"], False
        else:
            pending = scalar(conn, "SELECT COUNT(*) FROM segments WHERE book_id=? AND status IN ('pending','synthesizing')", (book_id,), 0)
            if not pending:
                raise ServiceError("Nothing to generate: every segment already has audio. Use Regenerate on specific segments.", 409)
            if front:
                pos = scalar(conn, "SELECT MIN(position) FROM jobs WHERE kind='book' AND status IN ('queued','paused','running')", (), 1) - 1
            else:
                pos = scalar(conn, "SELECT MAX(position) FROM jobs WHERE kind='book'", (), 0) + 1
            jid = conn.execute("INSERT INTO jobs(kind, book_id, status, position, created_at, updated_at) VALUES ('book', ?, 'queued', ?, ?, ?)",
                               (book_id, pos, now(), now())).lastrowid
            created = True
            ctx.job_log(conn, jid, book_id, f"Queued ({pending} segments to render)")
            ctx.event(conn, "queue", f"'{b['title']}' added to the queue", book_id)
        refresh_book_state(conn, book_id)
    j = get_job(ctx, jid)
    j["created"] = created
    return j


def list_queue(ctx) -> dict:
    with ctx.db.session() as conn:
        jobs = rows(conn, """SELECT j.*, b.title, b.uid FROM jobs j LEFT JOIN books b ON b.id=j.book_id
                             WHERE j.kind='book' AND (j.status IN ('queued','running','paused') OR j.updated_at > ?)
                             ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 WHEN 'paused' THEN 2 ELSE 3 END, j.position, j.id""",
                    (now() - 7 * 86400,))
        utility = rows(conn, """SELECT j.id, j.kind, j.status, j.error, j.book_id, b.title, j.created_at, j.finished_at, j.payload_json
                                FROM jobs j LEFT JOIN books b ON b.id=j.book_id WHERE j.kind IN ('assemble','export','preview','analyze')
                                ORDER BY j.id DESC LIMIT 15""")
        per_seg = _segment_seconds(conn)
        stats = _book_stats(conn, [j["book_id"] for j in jobs if j["book_id"]]) if jobs else {}
        out = []
        cumulative = 0.0
        for j in jobs:
            st = stats.get(j["book_id"], {})
            total = sum(v for k, v in st.items() if not k.startswith("_"))
            remaining = st.get("pending", 0) + st.get("synthesizing", 0)
            eta = None
            if per_seg["samples"] >= 20 and j["status"] in ("queued", "running"):
                eta = remaining * per_seg["median_s"]
                cumulative += eta
            last_log = row(conn, "SELECT message, level, created_at FROM job_logs WHERE job_id=? AND level<>'debug' ORDER BY id DESC LIMIT 1", (j["id"],))
            retries = scalar(conn, "SELECT COUNT(*) FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=? AND a.attempt_no>1", (j["book_id"],), 0)
            out.append({"id": j["id"], "book_id": j["book_id"], "title": j["title"], "status": j["status"], "position": j["position"],
                        "paused_reason": j["paused_reason"], "error": j["error"], "retries": j["retries"], "segment_retries": retries,
                        "current_segment_sid": j["current_segment_sid"], "created_at": j["created_at"], "started_at": j["started_at"],
                        "finished_at": j["finished_at"], "segments": total, "remaining": remaining, "flagged": st.get("flagged", 0),
                        "progress": round((total - remaining) / total, 4) if total else 0, "eta_s": eta,
                        "eta_finish_s": cumulative if eta is not None else None, "pause_requested": bool(j["pause_requested"]),
                        "cancel_requested": bool(j["cancel_requested"]), "last_log": last_log})
        for u in utility:
            u["payload"] = loads(u.pop("payload_json"), {})
        return {"jobs": out, "utility": utility, "throughput": per_seg}


def _segment_seconds(conn) -> dict:
    data = rows(conn, """SELECT a.segment_id, SUM(a.finished_at - a.created_at) t FROM attempts a
                         WHERE a.status IN ('done','error') AND a.finished_at IS NOT NULL GROUP BY a.segment_id ORDER BY MAX(a.id) DESC LIMIT 200""")
    vals = [d["t"] for d in data if d["t"] is not None and d["t"] >= 0]
    if not vals:
        return {"samples": 0, "median_s": None}
    return {"samples": len(vals), "median_s": round(statistics.median(vals), 2)}


def reorder_queue(ctx, job_ids: list[int]) -> dict:
    with ctx.db.tx() as conn:
        movable = {r["id"] for r in rows(conn, "SELECT id FROM jobs WHERE kind='book' AND status IN ('queued','paused')")}
        ids = [int(i) for i in job_ids if int(i) in movable]
        if len(set(ids)) != len(ids):
            raise ServiceError("Duplicate job ids in order", 422)
        rest = [r["id"] for r in rows(conn, "SELECT id FROM jobs WHERE kind='book' AND status IN ('queued','paused') ORDER BY position, id") if r["id"] not in ids]
        base = scalar(conn, "SELECT MIN(position) FROM jobs WHERE kind='book' AND status='running'", (), None)
        start = (base + 1) if base is not None else 1
        for k, jid in enumerate(ids + rest):
            conn.execute("UPDATE jobs SET position=?, updated_at=? WHERE id=?", (start + k, now(), jid))
        ctx.audit(conn, "queue", None, "reorder", {"order": ids + rest})
    return list_queue(ctx)


def move_job(ctx, job_id: int, direction: str) -> dict:
    with ctx.db.session() as conn:
        order = [r["id"] for r in rows(conn, "SELECT id FROM jobs WHERE kind='book' AND status IN ('queued','paused') ORDER BY position, id")]
    if job_id not in order:
        raise ServiceError("Only queued or paused jobs can be moved", 409)
    i = order.index(job_id)
    order.pop(i)
    if direction == "up":
        order.insert(max(0, i - 1), job_id)
    elif direction == "down":
        order.insert(min(len(order), i + 1), job_id)
    elif direction == "top":
        order.insert(0, job_id)
    elif direction == "bottom":
        order.append(job_id)
    else:
        raise ServiceError("direction must be up, down, top or bottom", 422)
    return reorder_queue(ctx, order)


def job_action(ctx, job_id: int, action: str) -> dict:
    with ctx.db.tx() as conn:
        j = row(conn, "SELECT * FROM jobs WHERE id=?", (job_id,))
        if not j:
            raise ServiceError("Job not found", 404)
        st = j["status"]
        if action == "pause":
            if st == "queued":
                conn.execute("UPDATE jobs SET status='paused', paused_reason='operator', updated_at=? WHERE id=?", (now(), job_id))
            elif st == "running":
                conn.execute("UPDATE jobs SET pause_requested=1, updated_at=? WHERE id=?", (now(), job_id))
            elif st != "paused":
                raise ServiceError(f"Cannot pause a {st} job", 409)
        elif action == "resume":
            if st == "paused":
                conn.execute("UPDATE jobs SET status='queued', paused_reason=NULL, pause_requested=0, updated_at=? WHERE id=?", (now(), job_id))
            elif st == "running" and j["pause_requested"]:
                conn.execute("UPDATE jobs SET pause_requested=0, updated_at=? WHERE id=?", (now(), job_id))
            elif st not in ("queued", "running"):
                raise ServiceError(f"Cannot resume a {st} job", 409)
        elif action == "retry":
            if st != "failed":
                raise ServiceError("Only failed jobs can be retried", 409)
            if j["kind"] == "book":
                if row(conn, "SELECT id FROM jobs WHERE book_id=? AND kind='book' AND status IN ('queued','running','paused') AND id<>?", (j["book_id"], job_id)):
                    raise ServiceError("This book already has an active job", 409)
            conn.execute("UPDATE jobs SET status='queued', error=NULL, retries=retries+1, consecutive_errors=0, finished_at=NULL, updated_at=? WHERE id=?", (now(), job_id))
            if j["book_id"]:
                conn.execute("UPDATE books SET last_error=NULL WHERE id=?", (j["book_id"],))
        elif action == "cancel":
            if st in ("queued", "paused"):
                conn.execute("UPDATE jobs SET status='cancelled', finished_at=?, updated_at=? WHERE id=?", (now(), now(), job_id))
            elif st == "running":
                conn.execute("UPDATE jobs SET cancel_requested=1, updated_at=? WHERE id=?", (now(), job_id))
            else:
                raise ServiceError(f"Cannot cancel a {st} job", 409)
        else:
            raise ServiceError("Unknown action", 422)
        ctx.job_log(conn, job_id, j["book_id"], f"Operator action: {action}")
        ctx.audit(conn, "job", job_id, action)
        if j["book_id"] and j["kind"] == "book":
            refresh_book_state(conn, j["book_id"])
    return get_job(ctx, job_id)


def job_logs(ctx, job_id: int, limit: int = 300) -> list[dict]:
    with ctx.db.session() as conn:
        return rows(conn, "SELECT id, level, message, created_at FROM job_logs WHERE job_id=? ORDER BY id DESC LIMIT ?", (job_id, limit))


def preview_segment(ctx, segment_id: int, seed: int | None = None) -> dict:
    """Queue a preview of one segment rendered with its own voice and delivery (not saved as a take)."""
    with ctx.db.tx() as conn:
        seg = row(conn, "SELECT id, book_id FROM segments WHERE id=?", (segment_id,))
        if not seg:
            raise ServiceError("Segment not found", 404)
        jid = conn.execute("INSERT INTO jobs(kind, book_id, payload_json, status, position, created_at, updated_at) VALUES ('preview',?,?, 'queued', 0, ?, ?)",
                           (seg["book_id"], dumps({"segment_id": segment_id, "seed": seed if seed is not None else 1234}), now(), now())).lastrowid
    return get_job(ctx, jid)


def enqueue_utility(ctx, book_id: int, kind: str, payload: dict | None = None) -> dict:
    if kind not in ("assemble", "export"):
        raise ServiceError("unknown job kind", 422)
    with ctx.db.tx() as conn:
        if not row(conn, "SELECT id FROM books WHERE id=?", (book_id,)):
            raise ServiceError("Book not found", 404)
        dup = row(conn, "SELECT id FROM jobs WHERE book_id=? AND kind=? AND status IN ('queued','running') AND payload_json=?", (book_id, kind, dumps(payload or {})))
        if dup:
            jid = dup["id"]
        else:
            jid = conn.execute("INSERT INTO jobs(kind, book_id, payload_json, status, position, created_at, updated_at) VALUES (?,?,?, 'queued', 0, ?, ?)",
                               (kind, book_id, dumps(payload or {}), now(), now())).lastrowid
            ctx.job_log(conn, jid, book_id, f"{kind} requested {payload or ''}")
    return get_job(ctx, jid)


# ===================================================================== segments

FILTERS = {
    "all": "1=1",
    "issues": "s.status='flagged' OR (s.status IN ('passed','approved') AND EXISTS (SELECT 1 FROM attempts a WHERE a.id=s.selected_attempt_id AND a.issues_json LIKE '%\"severity\":\"warning\"%'))",
    "flagged": "s.status='flagged'",
    "generated": "s.status IN ('passed','flagged','approved')",
    "passed": "s.status='passed'",
    "approved": "s.status='approved'",
    "pending": "s.status IN ('pending','synthesizing')",
    "overridden": "s.override_text IS NOT NULL",
    "speaker_review": "s.needs_review=1",
    "direction_review": "EXISTS (SELECT 1 FROM directions d WHERE d.segment_id=s.id AND (d.needs_review=1 OR d.stale=1))",
    "noted": "s.note IS NOT NULL AND s.note<>''",
}


def list_segments(ctx, book_id: int, chapter_id: int | None, flt: str, after: int | None, limit: int, q: str | None = None) -> dict:
    if flt not in FILTERS:
        raise ServiceError(f"Unknown filter. Use one of: {', '.join(FILTERS)}", 422)
    limit = max(1, min(200, limit))
    where = [f"s.book_id=?", f"({FILTERS[flt]})"]
    params: list = [book_id]
    if chapter_id:
        where.append("s.chapter_id=?")
        params.append(chapter_id)
    if q:
        where.append("(s.original_text LIKE ? OR s.sid LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    with ctx.db.session() as conn:
        total = scalar(conn, f"SELECT COUNT(*) FROM segments s WHERE {' AND '.join(where)}", params, 0)
        page_where = where + (["s.id > ?"] if after else [])
        items = rows(conn, f"""SELECT s.id, s.sid, s.chapter_id, s.idx, s.kind, s.status, s.original_text, s.override_text, s.attempts_used, s.round,
                                     s.flag_reason, s.note, s.character_id, s.needs_review, s.span_kind, s.scene_idx, s.src_start, a.wer, a.duration_s, a.id AS attempt_id, c.idx AS chapter_idx
                              FROM segments s JOIN chapters c ON c.id=s.chapter_id LEFT JOIN attempts a ON a.id=s.selected_attempt_id
                              WHERE {' AND '.join(page_where)} ORDER BY s.id LIMIT ?""", params + ([after] if after else []) + [limit + 1])
    more = len(items) > limit
    items = items[:limit]
    return {"items": items, "total": total, "next": items[-1]["id"] if more and items else None}


def segment_detail(ctx, segment_id: int) -> dict:
    with ctx.db.session() as conn:
        s = row(conn, "SELECT * FROM segments WHERE id=?", (segment_id,))
        if not s:
            raise ServiceError("Segment not found", 404)
        b = row(conn, "SELECT * FROM books WHERE id=?", (s["book_id"],))
        ch = row(conn, "SELECT id, idx, title FROM chapters WHERE id=?", (s["chapter_id"],))
        atts = rows(conn, "SELECT * FROM attempts WHERE segment_id=? ORDER BY id DESC", (segment_id,))
        reviews = rows(conn, "SELECT action, detail_json, actor, created_at FROM review_actions WHERE segment_id=? ORDER BY id DESC", (segment_id,))
        prev_id = scalar(conn, "SELECT id FROM segments WHERE book_id=? AND id<? ORDER BY id DESC LIMIT 1", (s["book_id"], segment_id))
        next_id = scalar(conn, "SELECT id FROM segments WHERE book_id=? AND id>? ORDER BY id LIMIT 1", (s["book_id"], segment_id))
        render = row(conn, "SELECT segment_map_json FROM chapter_renders WHERE chapter_id=? AND kind='wav'", (s["chapter_id"],))
    timing = None
    if render:
        timing = next((m for m in loads(render["segment_map_json"], []) if m["sid"] == s["sid"]), None)
    out_atts = []
    for a in atts:
        asr = loads(a["asr_json"], {}) or {}
        out_atts.append({
            "id": a["id"], "round": a["round"], "attempt_no": a["attempt_no"], "status": a["status"], "engine": a["engine"], "model_id": a["model_id"],
            "seed": a["seed"], "temperature": a["temperature"], "text_spoken": a["text_spoken"], "duration_s": a["duration_s"], "wer": a["wer"],
            "wer_raw": asr.get("wer_raw"), "asr_text": a["asr_text"], "diff": asr.get("diff", []), "counts": asr.get("counts"),
            "issues": loads(a["issues_json"], []), "acoustics": loads(a["acoustics_json"], {}), "score": a["score"], "passed": a["passed"],
            "rtf": a["rtf"], "peak_mem_gb": a["peak_mem_gb"], "instruct_used": a["instruct_used"], "gen_key": a["gen_key"],
            "delivery_applied": a["delivery_applied"], "asr_skipped": bool(a["asr_skipped"]), "voice_id_used": a["voice_id_used"],
            "error": a["error"], "selected": a["id"] == s["selected_attempt_id"], "created_at": a["created_at"],
            "playable": bool(a["wav_path"]) and resolve_take(b, a) is not None, "fingerprint_ok": a["fingerprint"] == b["synth_fingerprint"],
            "versions": loads(a["versions_json"], {}),
        })
    return {"segment": s, "chapter": ch, "book": {"id": b["id"], "title": b["title"], "uid": b["uid"]}, "attempts": out_atts,
            "reviews": [{**r, "detail": loads(r.pop("detail_json"), {})} for r in reviews], "prev_id": prev_id, "next_id": next_id,
            "chapter_timing": timing, "max_attempts": loads(b["settings_json"], {}).get("qa", {}).get("max_attempts", 3)}


def _seg_and_book(conn, segment_id: int):
    s = row(conn, "SELECT * FROM segments WHERE id=?", (segment_id,))
    if not s:
        raise ServiceError("Segment not found", 404)
    return s, row(conn, "SELECT * FROM books WHERE id=?", (s["book_id"],))


def _review(conn, ctx, seg: dict, action: str, detail: dict | None = None, actor: str = "operator") -> None:
    conn.execute("INSERT INTO review_actions(segment_id, action, detail_json, actor, created_at) VALUES (?,?,?,?,?)",
                 (seg["id"], action, dumps(detail or {}), actor, now()))
    ctx.audit(conn, "segment", seg["sid"], action, detail, actor)


def approve_segment(ctx, segment_id: int, note: str | None = None, actor: str = "operator") -> dict:
    with ctx.db.tx() as conn:
        s, b = _seg_and_book(conn, segment_id)
        if s["status"] == "approved":
            return {"ok": True, "status": "approved", "segment_id": segment_id}
        if not s["selected_attempt_id"] or s["status"] not in ("passed", "flagged"):
            raise ServiceError("Only a segment with a selected, rendered take can be approved.", 409)
        att = row(conn, "SELECT * FROM attempts WHERE id=?", (s["selected_attempt_id"],))
        if att["fingerprint"] != b["synth_fingerprint"]:
            raise ServiceError("The selected take uses outdated narrator settings; regenerate instead.", 409)
        if att["gen_key"] and att["gen_key"] != N_store.plan_for_segment(conn, ctx, s, b).gen_key:
            raise ServiceError("The selected take no longer matches this segment's voice or delivery; regenerate instead.", 409)
        conn.execute("UPDATE segments SET status='approved', note=COALESCE(?, note), updated_at=? WHERE id=?", ((note or None), now(), segment_id))
        _review(conn, ctx, s, "approve", {"attempt_id": s["selected_attempt_id"], "previous_status": s["status"], "note": note}, actor)
        refresh_book_state(conn, s["book_id"])
    return {"ok": True, "status": "approved", "segment_id": segment_id}


def regenerate_segment(ctx, segment_id: int, note: str | None = None) -> dict:
    with ctx.db.tx() as conn:
        s, b = _seg_and_book(conn, segment_id)
        if s["status"] in ("pending", "synthesizing"):
            return {"ok": True, "status": s["status"], "segment_id": segment_id, "already_pending": True}
        conn.execute("UPDATE segments SET round=round+1, attempts_used=0, status='pending', selected_attempt_id=NULL, flag_reason=NULL, updated_at=? WHERE id=?",
                     (now(), segment_id))
        conn.execute("DELETE FROM chapter_renders WHERE chapter_id=?", (s["chapter_id"],))
        _review(conn, ctx, s, "regenerate", {"previous_status": s["status"], "previous_attempt": s["selected_attempt_id"], "note": note})
        _revoke(conn, ctx, s["book_id"], f"segment {s['sid']} regenerated")
    enqueue_book(ctx, s["book_id"], front=True)
    return {"ok": True, "status": "pending", "segment_id": segment_id}


def set_override(ctx, segment_id: int, text: str | None, regenerate: bool = True) -> dict:
    text = " ".join((text or "").split()) or None
    if text and len(text) > 2000:
        raise ServiceError("Spoken rendition must be under 2000 characters.", 422)
    with ctx.db.tx() as conn:
        s, _b = _seg_and_book(conn, segment_id)
        conn.execute("UPDATE segments SET override_text=?, updated_at=? WHERE id=?", (text, now(), segment_id))
        _review(conn, ctx, s, "override", {"old": s["override_text"], "new": text, "original_unchanged": True})
    if regenerate:
        return regenerate_segment(ctx, segment_id, note="spoken rendition edited")
    return {"ok": True, "segment_id": segment_id}


def select_take(ctx, segment_id: int, attempt_id: int) -> dict:
    with ctx.db.tx() as conn:
        s, b = _seg_and_book(conn, segment_id)
        a = row(conn, "SELECT * FROM attempts WHERE id=? AND segment_id=?", (attempt_id, segment_id))
        if not a:
            raise ServiceError("That take does not belong to this segment.", 404)
        if a["status"] != "done" or resolve_take(b, a) is None:
            raise ServiceError("That take has no playable audio.", 409)
        if a["fingerprint"] != b["synth_fingerprint"]:
            raise ServiceError("That take was rendered with different narrator settings and cannot be used in this book.", 409)
        if a["gen_key"] and a["gen_key"] != N_store.plan_for_segment(conn, ctx, s, b).gen_key:
            raise ServiceError("That take was rendered with a different voice, delivery or spoken text than this segment now uses; regenerate instead.", 409)
        if s["status"] in ("pending", "synthesizing"):
            raise ServiceError("Wait for the segment to finish generating.", 409)
        status = "passed" if a["passed"] else "flagged"
        reason = None if a["passed"] else f"Operator selected take {a['round']}.{a['attempt_no']}, which did not pass automatic QA - approve after listening."
        conn.execute("UPDATE segments SET selected_attempt_id=?, status=?, flag_reason=?, updated_at=? WHERE id=?", (attempt_id, status, reason, now(), segment_id))
        from .pipeline import place_takes
        place_takes(conn, b, segment_id)
        conn.execute("DELETE FROM chapter_renders WHERE chapter_id=?", (s["chapter_id"],))
        _review(conn, ctx, s, "select_take", {"attempt_id": attempt_id, "previous": s["selected_attempt_id"]})
        _revoke(conn, ctx, s["book_id"], f"take changed for {s['sid']}")
        refresh_book_state(conn, s["book_id"])
    return {"ok": True, "status": status, "segment_id": segment_id}


def set_note(ctx, segment_id: int, note: str | None) -> dict:
    note = (note or "").strip()[:2000] or None
    with ctx.db.tx() as conn:
        s, _ = _seg_and_book(conn, segment_id)
        conn.execute("UPDATE segments SET note=?, updated_at=? WHERE id=?", (note, now(), segment_id))
        _review(conn, ctx, s, "note", {"note": note})
    return {"ok": True, "segment_id": segment_id}


# ====================================================================== release


def release_check_conn(conn, b: dict) -> dict:
    counts = {r["status"]: r["n"] for r in rows(conn, "SELECT status, COUNT(*) n FROM segments WHERE book_id=? GROUP BY status", (b["id"],))}
    blockers = []
    pend = counts.get("pending", 0) + counts.get("synthesizing", 0)
    if pend:
        blockers.append(f"{pend} segment(s) have no audio yet.")
    if counts.get("flagged", 0):
        blockers.append(f"{counts['flagged']} segment(s) are flagged by automatic QA - approve, fix or regenerate them.")
    ready = chapter_readiness(conn, b)
    missing = sum(len(r["problems"]) for r in ready)
    if missing and not pend:
        blockers.append(f"{missing} segment(s) have missing or mismatched take files.")
    stale = [r["chapter"]["idx"] for r in ready if not r["render_current"]]
    if stale:
        blockers.append(f"Chapter masters are missing or outdated for chapter(s) {', '.join(map(str, stale[:20]))} - run Assemble.")
    return {"ready": not blockers, "blockers": blockers, "counts": counts}


def approve_release(ctx, book_id: int, approved_by: str, listen_scope: str, attest: bool, notes: str = "") -> dict:
    approved_by = " ".join((approved_by or "").split())
    if not 2 <= len(approved_by) <= 120:
        raise ServiceError("Enter the name of the person approving the release.", 422)
    if listen_scope not in ("full", "partial"):
        raise ServiceError("listen_scope must be 'full' or 'partial'", 422)
    if not attest:
        raise ServiceError("Confirm the release statement.", 422)
    with ctx.db.tx() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        if not b:
            raise ServiceError("Book not found", 404)
        if active_release(conn, book_id):
            raise ServiceError("This book is already approved.", 409)
        chk = release_check_conn(conn, b)
        if not chk["ready"]:
            raise ServiceError("Release is blocked: " + " ".join(chk["blockers"]), 409, chk)
        statement = ("I have reviewed this audiobook, confirm the rights to the manuscript and narrator voice, and approve these files as "
                     "publication candidates. Automatic QA is not a substitute for listening.")
        conn.execute("INSERT INTO releases(book_id, approved_by, listen_scope, statement, checklist_json, approved_at) VALUES (?,?,?,?,?,?)",
                     (book_id, approved_by, listen_scope, statement, dumps({"notes": notes[:2000], "counts": chk["counts"]}), now()))
        conn.execute("UPDATE chapter_renders SET draft=0 WHERE chapter_id IN (SELECT id FROM chapters WHERE book_id=?)", (book_id,))
        ctx.audit(conn, "book", book_id, "release_approved", {"by": approved_by, "listen_scope": listen_scope}, approved_by)
        ctx.event(conn, "release", f"'{b['title']}' approved for publication by {approved_by} ({listen_scope} listen). Export again to get final (non-draft) files.", book_id)
        refresh_book_state(conn, book_id)
    return get_book(ctx, book_id)


def revoke_release(ctx, book_id: int, reason: str) -> dict:
    with ctx.db.tx() as conn:
        _revoke(conn, ctx, book_id, reason or "revoked by operator")
        refresh_book_state(conn, book_id)
    return get_book(ctx, book_id)


# ===================================================================== overview


def overview(ctx) -> dict:
    with ctx.db.session() as conn:
        books = rows(conn, "SELECT * FROM books WHERE archived=0 ORDER BY updated_at DESC")
        stats = _book_stats(conn)
        jobs = {r["status"]: r["n"] for r in rows(conn, "SELECT status, COUNT(*) n FROM jobs WHERE kind='book' GROUP BY status")}
        seg_counts = {r["status"]: r["n"] for r in rows(conn, "SELECT s.status, COUNT(*) n FROM segments s JOIN books b ON b.id=s.book_id WHERE b.archived=0 GROUP BY s.status")}
        audio_s = scalar(conn, """SELECT SUM(cr.duration_s) FROM chapter_renders cr JOIN chapters c ON c.id=cr.chapter_id JOIN books b ON b.id=c.book_id
                                  WHERE cr.kind='wav' AND b.archived=0""", (), 0.0)
        draft_s = scalar(conn, """SELECT SUM(a.duration_s) FROM segments s JOIN attempts a ON a.id=s.selected_attempt_id JOIN books b ON b.id=s.book_id WHERE b.archived=0""", (), 0.0)
        approved = sum(1 for b in books if b["state"] == "approved")
        flags = rows(conn, """SELECT s.id, s.sid, s.flag_reason, s.book_id, b.title, c.idx AS chapter_idx FROM segments s JOIN books b ON b.id=s.book_id
                              JOIN chapters c ON c.id=s.chapter_id WHERE s.status='flagged' AND b.archived=0 ORDER BY s.updated_at DESC LIMIT 25""")
        events = rows(conn, "SELECT id, kind, book_id, message, level, created_at FROM events ORDER BY id DESC LIMIT 25")
        recent = [_book_summary(conn, b, stats.get(b["id"], {})) for b in books[:12]]
        worker = row(conn, "SELECT * FROM worker_state WHERE id=1")
        failed_jobs = rows(conn, "SELECT j.id, j.book_id, j.error, b.title FROM jobs j JOIN books b ON b.id=j.book_id WHERE j.status='failed' AND j.kind='book' ORDER BY j.id DESC LIMIT 10")
    total = sum(seg_counts.values())
    produced = total - seg_counts.get("pending", 0) - seg_counts.get("synthesizing", 0)
    return {
        "cards": {"books": len(books), "queued": jobs.get("queued", 0), "running": jobs.get("running", 0), "paused": jobs.get("paused", 0),
                  "completed": jobs.get("completed", 0), "failed": jobs.get("failed", 0), "segments_total": total, "segments_produced": produced,
                  "qa_flags": seg_counts.get("flagged", 0), "approved_projects": approved, "mastered_audio_hours": round((audio_s or 0) / 3600, 2),
                  "draft_audio_hours": round((draft_s or 0) / 3600, 2)},
        "segment_states": {"draft_rendered": produced, "qa_passed": seg_counts.get("passed", 0), "needs_review": seg_counts.get("flagged", 0),
                           "approved_segments": seg_counts.get("approved", 0), "pending": seg_counts.get("pending", 0) + seg_counts.get("synthesizing", 0)},
        "recent": recent, "flags": flags, "events": events, "failed_jobs": failed_jobs,
        "worker": worker_status_from_row(ctx, worker),
    }


def worker_status_from_row(ctx, w: dict | None) -> dict:
    w = w or {}
    hb = w.get("heartbeat_at")
    alive = bool(w.get("owner")) and hb is not None and now() - hb < 30
    return {"online": alive, "state": w.get("state") if alive else "offline", "message": w.get("message"), "pid": w.get("pid"),
            "heartbeat_age_s": round(now() - hb, 1) if hb else None, "current_job_id": w.get("current_job_id") if alive else None,
            "current_segment_sid": w.get("current_segment_sid") if alive else None, "engine": ctx.engine_mode}


def state_digest(ctx) -> dict:
    """Cheap snapshot used by the SSE stream to detect changes."""
    with ctx.db.session() as conn:
        ev = scalar(conn, "SELECT MAX(id) FROM events", (), 0)
        seg = scalar(conn, "SELECT MAX(updated_at) FROM segments", (), 0)
        job = scalar(conn, "SELECT MAX(updated_at) FROM jobs", (), 0)
        att = scalar(conn, "SELECT MAX(id) FROM attempts", (), 0)
        w = row(conn, "SELECT * FROM worker_state WHERE id=1")
        active = rows(conn, """SELECT j.id, j.book_id, j.status, j.current_segment_sid, b.title FROM jobs j LEFT JOIN books b ON b.id=j.book_id
                               WHERE j.status IN ('running') ORDER BY j.id""")
        progress = {}
        for a in active:
            if a["book_id"]:
                st = {r["status"]: r["n"] for r in rows(conn, "SELECT status, COUNT(*) n FROM segments WHERE book_id=? GROUP BY status", (a["book_id"],))}
                total = sum(st.values())
                done = total - st.get("pending", 0) - st.get("synthesizing", 0)
                progress[a["book_id"]] = {"done": done, "total": total, "flagged": st.get("flagged", 0)}
    ws = worker_status_from_row(ctx, w)
    return {"events": ev, "segments": seg, "jobs": job, "attempts": att, "worker": ws, "active": active, "progress": progress,
            "worker_message": ws.get("message")}


# ===================================================================== settings


def get_settings(ctx) -> dict:
    s = ctx.settings()
    return {"settings": s.model_dump(mode="json"), "categories": {c: CATEGORY_LABELS[c] for c in STORAGE_CATEGORIES},
            "speakers": PRESET_SPEAKERS, "english_speakers": list(ENGLISH_SPEAKERS), "engine_mode": ctx.engine_mode,
            "effective_paths": {c: str(ctx.category_dir(c, s)) for c in STORAGE_CATEGORIES}, "library_root": str(ctx.library_root(s))}


def update_settings(ctx, patch: dict) -> dict:
    cur = ctx.settings().model_dump(mode="json")
    merged = merge_patch(cur, patch or {})
    try:
        new = Settings.model_validate(merged)
    except Exception as exc:
        raise ServiceError(f"Invalid settings: {exc}", 422)
    errors = {}
    for field in ["library_root"] + [f"{c}_dir" for c in STORAGE_CATEGORIES]:
        val = getattr(new.storage, field)
        if val and val != getattr(ctx.settings().storage, field):
            v = validate_dir(val, ctx.home, create=True, measure=False)
            if not v["ok"]:
                errors[field] = v["errors"]
            else:
                setattr(new.storage, field, v["resolved"])
    if errors:
        raise ServiceError("Some storage paths are invalid: " + "; ".join(f"{k}: {' '.join(v)}" for k, v in errors.items()), 422, errors)
    if new.server.host != ctx.settings().server.host or new.server.port != ctx.settings().server.port:
        pass  # takes effect after restart; surfaced in the UI
    ctx.save_settings(new)
    with ctx.db.tx() as conn:
        ctx.audit(conn, "settings", None, "update", patch)
        ctx.event(conn, "settings", "Settings saved. They apply to books imported from now on; existing books keep their snapshot.")
    return get_settings(ctx)


def wait_for_job(ctx, job_id: int, timeout: float = 600.0, poll: float = 0.5) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = get_job(ctx, job_id)
        if j["status"] in ("completed", "failed", "cancelled"):
            return j
        time.sleep(poll)
    raise ServiceError("Timed out waiting for job", 504)
