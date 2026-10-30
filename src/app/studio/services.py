"""Service layer shared by the web API, the CLI and the smoke test.

Every state change goes through here, inside a transaction, with an audit trail.
"""
from __future__ import annotations

import os
import random
import re
import secrets
import statistics
import time
import uuid
from pathlib import Path

from . import audio as A
from . import runtime as R
from .assembly import active_release, chapter_readiness
from .chunker import build_segments
from .narrative import ab as N_ab
from .narrative import cast as N_cast
from .narrative import store as N_store
from .context import resolve_actor
from .config import (CATEGORY_LABELS, ENGLISH_SPEAKERS, PRESET_SPEAKERS, STORAGE_CATEGORIES, Settings, book_snapshot,
                     merge_patch, synth_fingerprint)
from .db import row, rows, scalar
from .normalize import to_speech
from .parsing import ManuscriptError, ParseResult, detect_format, parse_manuscript
from .pipeline import book_dirs, resolve_take
from .secrets_store import SecretError
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
SYNTHETIC_HOLDER = "Synthetic voice designed in this studio"


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


def _source_ref(s: dict | None) -> dict | None:
    return {"id": s["id"], "name": s["name"], "archived": bool(s["archived"])} if s else None


def _voice_dict(conn, v: dict, vv: dict | None = None, used: int | None = None, designs: list[dict] | None = None) -> dict:
    """``vv`` / ``used`` / ``designs`` may be preloaded by a list call so that N voices cost a constant number of queries."""
    if vv is None:
        vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (v["current_version_id"],)) or {}
    if used is None:
        used = scalar(conn, "SELECT COUNT(*) FROM books WHERE voice_id=? AND archived=0", (v["id"],), 0)
    consent = loads(vv.get("consent_json"), None)
    out = {
        "id": v["id"], "uid": v["uid"], "name": v["name"], "kind": v["kind"], "description": v["description"], "builtin": bool(v["builtin"]),
        "archived": bool(v["archived"]), "created_at": v["created_at"], "books_using": used, "pace": float(v.get("pace") or 1.0),
        "version": {"id": vv.get("id"), "version": vv.get("version"), "model_id": vv.get("model_id"), "speaker": vv.get("speaker"),
                    "language": vv.get("language"), "instruct": vv.get("instruct"), "ref_text": vv.get("ref_text"),
                    "ref_duration_s": vv.get("ref_duration_s"), "has_reference": bool(vv.get("ref_audio_path")),
                    "consent": consent, "created_at": vv.get("created_at")},
        "derived_from": (consent or {}).get("derived_from") or None,
    }
    if v["kind"] == "clone":
        if designs is None:
            out["source_voice"] = _source_ref(clone_source_voice(conn, v))
        else:
            out["source_voice"] = _source_ref(_pick_source(consent or {}, designs))
    return out


def list_voices(ctx, include_archived: bool = False) -> list[dict]:
    """A constant number of queries however many voices exist (versions, book counts and clone sources are batched)."""
    with ctx.db.session() as conn:
        vs = rows(conn, "SELECT * FROM voices " + ("" if include_archived else "WHERE archived=0 ") + "ORDER BY builtin DESC, name")
        vvs = {r["id"]: r for r in rows(conn, "SELECT * FROM voice_versions WHERE id IN (SELECT current_version_id FROM voices)")}
        used = {r["voice_id"]: r["n"] for r in rows(conn, "SELECT voice_id, COUNT(*) AS n FROM books WHERE archived=0 GROUP BY voice_id")}
        designs = rows(conn, "SELECT id, name, archived FROM voices WHERE kind='design' ORDER BY archived, id DESC")
        return [_voice_dict(conn, v, vvs.get(v["current_version_id"]) or {}, used.get(v["id"], 0), designs) for v in vs]


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
                   "attested_at": now(), "original_filename": safe_filename(filename), "sha256": sha256_bytes(data),
                   **{k: consent[k] for k in ("source_voice_id", "source_voice_name") if consent.get(k) is not None}}
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
            if v["kind"] == "design" and description is None:  # a designed voice's card summary IS its description: keep it current
                conn.execute("UPDATE voices SET description=? WHERE id=?", (new_instruct[:300], voice_id))
            ctx.audit(conn, "voice", voice_id, "new_version", {"version": ver, "instruct": new_instruct, "speaker": new_speaker})
    out = get_voice(ctx, voice_id)
    with ctx.db.session() as conn:
        out["books_on_older_version"] = rows(conn, """SELECT b.id, b.title, vv.version AS voice_version FROM books b
                                                      LEFT JOIN voice_versions vv ON vv.id=b.voice_version_id
                                                      WHERE b.voice_id=? AND b.voice_version_id<>? AND b.archived=0""",
                                            (voice_id, out["version"]["id"]))
    return out


def _derive_voice(ctx, conn, src: dict, name: str, instruct: str | None, speaker: str | None, description: str | None,
                  derived: dict, action: str, actor: str) -> int:
    """Insert a NEW non-built-in voice (version 1) of the same kind/model/language as ``src``, starting from src's CURRENT
    version. ``instruct``/``speaker`` of None mean "copy"; an empty ``instruct`` clears a preset's delivery instruction."""
    cur = row(conn, "SELECT * FROM voice_versions WHERE id=?", (src["current_version_id"],))
    if not cur:
        raise ServiceError("Voice has no current version.", 409)
    new_instruct = cur["instruct"] if instruct is None else (instruct.strip() or None)
    new_speaker = cur["speaker"]
    if src["kind"] == "preset":
        new_speaker = cur["speaker"] if speaker is None else speaker
        if new_speaker not in PRESET_SPEAKERS:
            raise ServiceError(f"Unsupported speaker '{new_speaker}'. Supported: {', '.join(PRESET_SPEAKERS)}", 422)
    elif speaker is not None:
        raise ServiceError("Only preset voices have a speaker setting.", 422)
    if src["kind"] == "design" and not (new_instruct and 10 <= len(new_instruct) <= 1000):
        raise ServiceError("A designed voice needs a description of 10-1000 characters.", 422)
    name = _check_name(conn, name)
    old = loads(cur["consent_json"], {}) or {}
    consent = {k: old[k] for k in ("type", "note") if old.get(k)}
    consent.setdefault("type", "designed synthetic voice" if src["kind"] == "design" else "built-in synthetic speaker")
    consent["derived_from"] = derived
    if description is not None and description.strip():
        summary = description.strip()[:300]
    elif src["kind"] == "design":
        summary = new_instruct[:300]
    else:
        summary = f"Preset speaker {new_speaker}"
    vid = conn.execute("INSERT INTO voices(uid, name, kind, description, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                       (new_uid("voice"), name, src["kind"], summary, now(), now())).lastrowid
    vvid = conn.execute("INSERT INTO voice_versions(voice_id, version, model_id, speaker, language, instruct, consent_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (vid, 1, cur["model_id"], new_speaker, cur["language"], new_instruct, dumps(consent), now())).lastrowid
    conn.execute("UPDATE voices SET current_version_id=?, pace=? WHERE id=?", (vvid, float(src.get("pace") or 1.0), vid))
    ctx.audit(conn, "voice", vid, action, {"derived_from": derived, "speaker": new_speaker, "instruct": new_instruct}, actor)
    return vid


def duplicate_voice(ctx, voice_id: int, name: str, instruct: str | None = None, speaker: str | None = None,
                    description: str | None = None, actor: str = "operator") -> dict:
    """Copy a preset or designed voice (built-ins included) into a new, editable, non-built-in voice, optionally with a different
    delivery instruction / description / speaker. The original and its versions are not touched."""
    with ctx.db.tx() as conn:
        src = row(conn, "SELECT * FROM voices WHERE id=?", (voice_id,))
        if not src:
            raise ServiceError("Voice not found", 404)
        if src["kind"] == "clone":
            raise ServiceError("A cloned voice is defined by its reference clip and cannot be duplicated with new instructions. "
                               "Use 'Retune' on a clone made from a designed voice, or upload a new reference clip.", 422)
        cur = row(conn, "SELECT version FROM voice_versions WHERE id=?", (src["current_version_id"],)) or {}
        vid = _derive_voice(ctx, conn, src, name, instruct, speaker, description,
                            {"relation": "copy", "voice_id": src["id"], "voice_name": src["name"], "version": cur.get("version")}, "duplicate", actor)
    return get_voice(ctx, vid)


def retune_from_design(ctx, clone_voice_id: int, name: str, instruct: str | None = None, actor: str = "operator") -> dict:
    """A promoted clone cannot take instructions, so retuning means going back to the designed voice it came from: create a new
    designed voice from that source's current description (or ``instruct``), ready for new previews and another promotion."""
    with ctx.db.tx() as conn:
        c = row(conn, "SELECT * FROM voices WHERE id=?", (clone_voice_id,))
        if not c:
            raise ServiceError("Voice not found", 404)
        if c["kind"] != "clone":
            raise ServiceError("Retune applies to cloned voices made from a designed voice; use Duplicate for other voices.", 422)
        src = clone_source_voice(conn, c)
        if not src:
            raise ServiceError("No source design voice is recorded for this clone; duplicate a design voice manually.", 422)
        cur = row(conn, "SELECT version FROM voice_versions WHERE id=?", (src["current_version_id"],)) or {}
        vid = _derive_voice(ctx, conn, src, name, instruct, None, None,
                            {"relation": "retune", "voice_id": src["id"], "voice_name": src["name"], "version": cur.get("version"),
                             "source_voice_id": src["id"], "clone_voice_id": c["id"], "clone_name": c["name"]}, "retune", actor)
        source = _source_ref(src)
    out = get_voice(ctx, vid)
    out["source"] = source
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


def enqueue_preview(ctx, voice_id: int, text: str, instruct: str | None = None, verify: bool = False, seed: int | None = None,
                    extra: dict | None = None, allow_archived: bool = False) -> dict:
    text = " ".join((text or "").split())
    if not 3 <= len(text) <= 600:
        raise ServiceError("Preview text must be 3-600 characters.", 422)
    with ctx.db.tx() as conn:
        v = row(conn, "SELECT * FROM voices WHERE id=?" + ("" if allow_archived else " AND archived=0"), (voice_id,))
        if not v:
            raise ServiceError("Voice not found", 404)
        jid = conn.execute("INSERT INTO jobs(kind, payload_json, status, position, created_at, updated_at) VALUES ('preview',?, 'queued', 0, ?, ?)",
                           (dumps({"voice_id": voice_id, "voice_version_id": v["current_version_id"], "text": text,
                                   "instruct": (instruct or "").strip() or None, "verify": bool(verify), "seed": seed or 1234, **(extra or {})}),
                            now(), now())).lastrowid
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
                             {"owner_attest": True, "rights_holder": f"{SYNTHETIC_HOLDER} ('{v['name']}')",
                              "statement": f"Reference generated locally by Qwen3-TTS VoiceDesign (preview job {job_id}); no human voice involved.",
                              "source_voice_id": v["id"], "source_voice_name": v["name"]})
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


def compare_recordings(ctx, ids: list[str]) -> dict:
    """Compare 2-8 saved takes side by side (metrics, verdicts, advisory ranking)."""
    from . import reference as R

    ids = list(dict.fromkeys(ids))
    if not 2 <= len(ids) <= 8:
        raise ServiceError("Choose 2 to 8 takes to compare.", 422)
    return R.compare_takes([get_recording(ctx, i) for i in ids])


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
    meta.pop("asr_check", None)  # the check compared the old transcript
    atomic_write_bytes(_rec_meta_path(ctx, rid), dumps(meta).encode())
    return meta


def update_recording_meta(ctx, rid: str, **fields) -> dict:
    """Merge extra fields (Whisper check result, synthetic-candidate provenance, drift metrics) into a recording's meta.json."""
    meta = get_recording(ctx, rid)
    meta.update(fields)
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


# ================================================ emotion reference bank (cloned voices)

BANK_MAX_WER = 0.08


def _clone_row(conn, voice_id: int) -> dict:
    v = row(conn, "SELECT * FROM voices WHERE id=?", (voice_id,))
    if not v:
        raise ServiceError("Voice not found", 404)
    if v["kind"] != "clone":
        raise ServiceError("Only cloned voices have an emotion reference bank.", 422)
    return v


def _clone_consent(conn, v: dict) -> dict:
    vv = row(conn, "SELECT consent_json FROM voice_versions WHERE id=?", (v["current_version_id"],)) or {}
    return loads(vv.get("consent_json"), {})


def _is_synthetic_consent(consent: dict) -> bool:
    return str(consent.get("rights_holder") or "").startswith(SYNTHETIC_HOLDER)


def _holder_source_name(consent: dict) -> str | None:
    m = re.search(r"\('(.+)'\)\s*$", str(consent.get("rights_holder") or ""))
    return m.group(1) if m else None


def _pick_source(consent: dict, designs: list[dict]) -> dict | None:
    """In-memory twin of clone_source_voice for list calls. ``designs`` = all design voices ordered by (archived, id DESC)."""
    if not _is_synthetic_consent(consent):
        return None
    sid = consent.get("source_voice_id")
    if sid:
        hit = next((d for d in designs if d["id"] == sid), None)
        if hit:
            return hit
    nm = _holder_source_name(consent)
    return next((d for d in designs if d["name"] == nm), None) if nm else None


def clone_source_voice(conn, v: dict) -> dict | None:
    """The designed voice a clone was promoted from (archived ones included), or None for human-recorded clones.
    Clones promoted before v0.7.0 only name it inside ``rights_holder``."""
    consent = _clone_consent(conn, v)
    if not _is_synthetic_consent(consent):
        return None
    sv = None
    if consent.get("source_voice_id"):
        sv = row(conn, "SELECT * FROM voices WHERE id=? AND kind='design'", (consent["source_voice_id"],))
    if not sv:
        nm = _holder_source_name(consent)
        if nm:
            sv = row(conn, "SELECT * FROM voices WHERE name=? AND kind='design' ORDER BY archived, id DESC", (nm,))
    return sv


def _ref_dict(r: dict) -> dict:
    m = loads(r["metrics_json"], {})
    return {"id": r["id"], "bucket": r["bucket"], "ref_text": r["ref_text"], "duration_s": r["ref_duration_s"], "source": r["source"],
            "source_ref": r["source_ref"], "metrics": m, "created_at": r["created_at"], "mock": m.get("engine") == "mock"}


def list_refs(ctx, voice_id: int) -> dict:
    from .narrative import refbank as RB

    with ctx.db.session() as conn:
        v = _clone_row(conn, voice_id)
        have = {r["bucket"]: r for r in rows(conn, "SELECT * FROM voice_refs WHERE voice_id=?", (voice_id,))}
        src = clone_source_voice(conn, v)
        synthetic = _is_synthetic_consent(_clone_consent(conn, v))
    return {"voice_id": voice_id, "synthetic": synthetic,
            "source_voice": {"id": src["id"], "name": src["name"], "archived": bool(src["archived"])} if src else None,
            "buckets": [{"bucket": b, "label": info["label"], "emotion_text": info["emotion_text"], "script_id": info["script_id"],
                         "ref": _ref_dict(have[b]) if b in have else None} for b, info in RB.BUCKET_INFO.items()]}


def adopt_ref(ctx, voice_id: int, bucket: str, recording_id: str, owner_attest: bool = False, statement: str = "") -> dict:
    """Adopt a lab recording as the clone's reference clip for one emotion bucket (replacing any earlier clip there)."""
    from .narrative import refbank as RB
    from .narrative import voicecheck as VC

    if bucket not in RB.BUCKETS:
        raise ServiceError(f"bucket must be one of: {', '.join(RB.BUCKETS)}", 422)
    meta = get_recording(ctx, recording_id)
    chk = meta.get("asr_check") or {}
    if not chk:
        raise ServiceError("Run the Whisper check on this recording first: an emotional clip is only adopted when its transcript matches.", 422)
    if not chk.get("matches") or float(chk.get("wer", 1.0)) > BANK_MAX_WER:
        raise ServiceError(f"The Whisper check does not match the transcript (word error rate {float(chk.get('wer', 1.0)) * 100:.1f}%, "
                           f"at most {BANK_MAX_WER * 100:.0f}% allowed). Fix the transcript or re-record.", 422, {"asr_check": chk})
    if meta.get("source") != "synthetic" and (meta.get("quality") or {}).get("verdict") == "poor":  # room-noise checks describe microphones, not synthesis
        raise ServiceError("This take was rated poor quality; re-record it before adopting it.", 422, {"quality": meta.get("quality")})
    s = ctx.settings()
    with ctx.db.session() as conn:
        v = _clone_row(conn, voice_id)
        vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (v["current_version_id"],))
        consent_base = _clone_consent(conn, v)
        src = clone_source_voice(conn, v)
    synthetic_clone = _is_synthetic_consent(consent_base)
    statement = " ".join((statement or "").split())
    if synthetic_clone:
        if meta.get("source") != "synthetic":
            raise ServiceError("This clone was made from a designed (synthetic) voice, so its emotion clips must be generated by that voice "
                               "('Generate candidates'); a human recording cannot be adopted into it.", 422)
        if src is None or meta.get("source_voice_id") != src["id"]:
            raise ServiceError("This candidate was not generated by the designed voice this clone came from.", 422)
        consent = {"type": "synthetic", "source_voice_id": src["id"], "source_voice_name": src["name"],
                   "note": "Generated locally by the designed voice; no human voice involved."}
    else:
        if meta.get("source") == "synthetic":
            raise ServiceError("A synthetic candidate cannot be adopted into a clone of a human voice.", 422)
        if not owner_attest:
            raise ServiceError("Confirm that this recording is of the same speaker and that you own it or have their written permission.", 422)
        if len(statement) < 10:
            raise ServiceError("Add a short consent statement (at least 10 characters).", 422)
        consent = {"type": "human", "owner_attest": True, "statement": statement[:1000], "attested_at": now(), "rights_holder": consent_base.get("rights_holder")}
    rdir = recordings_dir(ctx) / recording_id
    clip_src, orig_src = rdir / "reference-24k.wav", rdir / meta.get("filename", "original.wav")
    if not clip_src.exists() or not orig_src.exists():
        raise ServiceError("The recording's audio files are missing.", 410)
    try:
        clip, clip_sr = A.read_wav(clip_src)
        base, base_sr = A.read_wav(Path(vv["ref_audio_path"]))
    except Exception as exc:
        raise ServiceError(f"Could not read the audio: {exc}", 422)
    adj, gain_db = VC.match_level(clip, clip_sr, base, base_sr)
    f0, f0_base = VC.f0_median(adj, clip_sr), VC.f0_median(base, base_sr)
    cen, cen_base = VC.spectral_centroid(adj, clip_sr), VC.spectral_centroid(base, base_sr)
    metrics = {"gain_db": round(gain_db, 2), "f0_hz": round(f0, 1) if f0 else None, "base_f0_hz": round(f0_base, 1) if f0_base else None,
               "f0_delta_hz": round(f0 - f0_base, 1) if f0 and f0_base else None, "centroid_hz": round(cen, 1), "base_centroid_hz": round(cen_base, 1),
               "centroid_delta_hz": round(cen - cen_base, 1), "wer": chk.get("wer"), "engine": meta.get("engine"), "script": meta.get("script_title")}
    if meta.get("engine") == "mock":
        metrics["note"] = "Mock engine: synthetic test tones, not speech."
    ref_dir = ctx.category_dir("references", s) / v["uid"] / "refs"
    try:
        ref_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ServiceError(f"Reference storage is unavailable: {exc}", 503)
    data = orig_src.read_bytes()
    ext = orig_src.suffix.lower() or ".wav"
    wav_dst, orig_dst = ref_dir / f"{bucket}.wav", ref_dir / f"{bucket}-original{ext}"
    atomic_write_bytes(orig_dst, data)
    A.write_wav(wav_dst, adj, clip_sr)
    with ctx.db.tx() as conn:
        old = row(conn, "SELECT * FROM voice_refs WHERE voice_id=? AND bucket=?", (voice_id, bucket))
        # never reuse an id: attempts.ref_id_used must keep pointing at one specific clip even after it was replaced or removed
        new_id = max(scalar(conn, "SELECT MAX(id) FROM voice_refs", (), 0) or 0, scalar(conn, "SELECT MAX(ref_id_used) FROM attempts", (), 0) or 0) + 1
        if old:
            conn.execute("DELETE FROM voice_refs WHERE id=?", (old["id"],))
        conn.execute(
            """INSERT INTO voice_refs(id, voice_id, bucket, ref_audio_path, ref_original_path, ref_text, ref_sha256, ref_duration_s, source, source_ref,
                   consent_json, metrics_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (new_id, voice_id, bucket, str(wav_dst), str(orig_dst), meta["ref_text"], sha256_bytes(data), round(adj.size / clip_sr, 2),
             "synthetic" if synthetic_clone else "recording", recording_id, dumps(consent), dumps(metrics), now()))
        ctx.audit(conn, "voice", voice_id, "adopt_ref", {"bucket": bucket, "recording_id": recording_id, "replaced": bool(old), "consent": consent.get("type")})
        ctx.event(conn, "voice", f"Reference clip '{bucket}' adopted for '{v['name']}'" + (" (mock engine: test tones)" if meta.get("engine") == "mock" else "") + ".")
    return list_refs(ctx, voice_id)


def remove_ref(ctx, voice_id: int, ref_id: int) -> dict:
    with ctx.db.tx() as conn:
        _clone_row(conn, voice_id)
        r = row(conn, "SELECT * FROM voice_refs WHERE id=? AND voice_id=?", (ref_id, voice_id))
        if not r:
            raise ServiceError("Reference clip not found", 404)
        conn.execute("DELETE FROM voice_refs WHERE id=?", (ref_id,))
        ctx.audit(conn, "voice", voice_id, "remove_ref", {"bucket": r["bucket"], "ref_id": ref_id})
    for p in (r["ref_audio_path"], r["ref_original_path"]):
        if p:
            Path(p).unlink(missing_ok=True)
    return list_refs(ctx, voice_id)


def generate_ref_candidates(ctx, voice_id: int, bucket: str, source_voice_id: int | None = None, n: int = 3) -> dict:
    """Ask the designed voice a synthetic clone came from for ``n`` takes in one emotional register. Each finished job registers
    a draft in the reference lab (with a Whisper check and its drift from the clone's base reference), ready to audition and adopt."""
    from . import reference as R
    from .narrative import refbank as RB

    if bucket not in RB.BUCKETS:
        raise ServiceError(f"bucket must be one of: {', '.join(RB.BUCKETS)}", 422)
    n = max(1, min(5, int(n or 3)))
    with ctx.db.session() as conn:
        v = _clone_row(conn, voice_id)
        src = clone_source_voice(conn, v)
        synthetic = _is_synthetic_consent(_clone_consent(conn, v))
        if src is None and synthetic and source_voice_id:
            src = row(conn, "SELECT * FROM voices WHERE id=? AND kind='design'", (source_voice_id,))
            if not src:
                raise ServiceError("source_voice_id is not a designed voice.", 422)
    if not synthetic:
        raise ServiceError("Candidates can only be generated for a clone made from a designed voice. For a human voice, record the clips in the "
                           "Recording lab and adopt them here.", 422)
    if src is None:
        raise ServiceError("The designed voice this clone came from could not be found; pass source_voice_id.", 422)
    info = RB.BUCKET_INFO[bucket]
    script = R.script_by_id(info["script_id"])
    jobs = []
    for i in range(n):
        jobs.append(enqueue_preview(
            ctx, src["id"], script["text"], f"Deliver the whole passage in a {info['emotion_text']} manner.", verify=True,
            seed=7001 + 101 * i + 13 * RB.BUCKETS.index(bucket), allow_archived=True,
            extra={"to_lab": True, "bucket": bucket, "script_id": script["id"], "target_voice_id": voice_id, "source_voice_id": src["id"], "candidate": i + 1}))
    return {"voice_id": voice_id, "bucket": bucket, "source_voice": {"id": src["id"], "name": src["name"], "archived": bool(src["archived"])}, "jobs": jobs}


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
            preview["warnings"].insert(0, f"This exact file was already imported as '{dupe['title']}' (book #{dupe['id']}). Importing again creates a separate project; "
                                          "to hear it in another voice, open that book and use 'Narrate with another voice' instead.")
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
        conn.execute("UPDATE books SET work_uid=uid WHERE id=?", (bid,))  # a fresh import starts its own work; further narrators join it (versions.py)
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


# Narrator versions (v0.7.0): books narrated from one manuscript share work_uid. Titles in queues, review and the dashboard are "<Title> - <Narrator>".
DISPLAY_TITLE_SQL = "b.title || COALESCE(' - ' || (SELECT v.name FROM voices v WHERE v.id=b.voice_id), '')"


def work_key(b: dict) -> str:
    return b.get("work_uid") or b["uid"]


def siblings(conn, b: dict) -> list[dict]:
    """Every version of this book's manuscript, oldest first (the book itself included; archived versions too)."""
    return rows(conn, "SELECT * FROM books WHERE COALESCE(work_uid, uid)=? ORDER BY created_at, id", (work_key(b),))


def display_title(title: str, narrator: str | None) -> str:
    return f"{title} - {narrator}" if narrator else title


def _book_stats(conn, book_ids: list[int] | None = None) -> dict[int, dict]:
    """book_id -> {segment status: count, "_audio_s": seconds of selected audio}. ``book_ids=None`` means every book; a list (even an empty one)
    limits BOTH queries to those books."""
    if book_ids is not None:
        book_ids = sorted({int(i) for i in book_ids})
        if not book_ids:
            return {}
    marks = ",".join("?" * len(book_ids)) if book_ids is not None else ""
    q = "SELECT book_id, status, COUNT(*) n FROM segments" + (f" WHERE book_id IN ({marks})" if book_ids is not None else "") + " GROUP BY book_id, status"
    stats: dict[int, dict] = {}
    for r in rows(conn, q, book_ids or ()):
        stats.setdefault(r["book_id"], {})[r["status"]] = r["n"]
    durs = rows(conn, """SELECT s.book_id, SUM(a.duration_s) d FROM segments s JOIN attempts a ON a.id = s.selected_attempt_id"""
                + (f" WHERE s.book_id IN ({marks})" if book_ids is not None else "") + " GROUP BY s.book_id", book_ids or ())
    for d in durs:
        stats.setdefault(d["book_id"], {})["_audio_s"] = d["d"] or 0.0
    return stats


def _progress(st: dict) -> float:
    total = sum(v for k, v in st.items() if not k.startswith("_"))
    return round((st.get("passed", 0) + st.get("flagged", 0) + st.get("approved", 0)) / total, 4) if total else 0.0


def _book_summary(conn, b: dict, st: dict, rt_ctx: dict | None | bool = None) -> dict:
    """``rt_ctx``: a runtime.runtime_context built once per request (built lazily here when a caller has none); ``False`` leaves the
    ``runtime`` field out (the light Review payload does not show processing time)."""
    if rt_ctx is None:
        rt_ctx = R.runtime_context(conn)
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
    versions = scalar(conn, "SELECT COUNT(*) FROM books WHERE COALESCE(work_uid, uid)=? AND archived=0", (work_key(b),), 1)
    return {
        "id": b["id"], "uid": b["uid"], "title": b["title"], "author": b["author"], "state": b["state"], "last_error": b["last_error"],
        "work_uid": work_key(b), "narrator": voice.get("name"), "display_title": display_title(b["title"], voice.get("name")), "version_count": versions,
        "created_at": b["created_at"], "voice": voice.get("name"), "voice_kind": voice.get("kind"), "source": m.get("original_name"), "format": m.get("format"),
        "segments": total, "produced": produced, "passed": st.get("passed", 0), "flagged": st.get("flagged", 0), "approved": st.get("approved", 0),
        "pending": st.get("pending", 0) + st.get("synthesizing", 0), "progress": round(produced / total, 4) if total else 0.0,
        "est_duration_s": est, "audio_duration_s": round(st.get("_audio_s", 0.0), 1), "retries": retries,
        "job": job, "current_chapter": cur_ch, "released": b["state"] == "approved",
        **({"runtime": R.book_runtime(conn, b, rt_ctx)} if rt_ctx is not False else {}),
    }


def list_books(ctx, include_archived: bool = False) -> list[dict]:
    with ctx.db.session() as conn:
        books = rows(conn, "SELECT * FROM books " + ("" if include_archived else "WHERE archived=0 ") + "ORDER BY created_at DESC")
        stats = _book_stats(conn)
        rt_ctx = R.runtime_context(conn, now(), ctx.settings())
        return [_book_summary(conn, b, stats.get(b["id"], {}), rt_ctx) for b in books]


def _cover_version(cover_path) -> int:
    """Integer mtime of the cover file, or 0 when there is none."""
    try:
        return int(Path(cover_path).stat().st_mtime) if cover_path else 0
    except OSError:
        return 0


def _chapter_status_counts(conn, book_id: int) -> dict[int, dict]:
    """chapter_id -> {segment status: count} for a whole book in one GROUP BY."""
    out: dict[int, dict] = {}
    for r in rows(conn, "SELECT chapter_id, status, COUNT(*) n FROM segments WHERE book_id=? GROUP BY chapter_id, status", (book_id,)):
        out.setdefault(r["chapter_id"], {})[r["status"]] = r["n"]
    return out


def _chapter_row(ch: dict, st: dict) -> dict:
    return {"id": ch["id"], "index": ch["idx"], "title": ch["title"], "words": ch["word_count"], "segments": ch["segment_count"],
            "title_overridden": ch["original_title"] is not None, "original_title": ch["original_title"] or ch["title"],
            "est_duration_s": ch["est_duration_s"], "status_counts": st}


def get_book_review(ctx, book_id: int) -> dict:
    """LIGHT book payload for the Review page (GET /api/books/{id}?view=review): the summary fields and the chapter list, with NO
    readiness / release check / exports / logs / utility jobs / narrative status / manuscript manifest. Key names equal get_book's.

    Keys: id, uid, title, author, state, last_error, work_uid, narrator, display_title, version_count, created_at, voice, voice_kind, source,
    format, segments, produced, passed, flagged, approved, pending, progress, est_duration_s, audio_duration_s, retries, job,
    current_chapter, released; and chapters = [{id, index, title, words, segments, title_overridden, original_title, est_duration_s,
    status_counts}]. The chapters carry no missing_audio / render_current / renders (those need the per-file readiness check)."""
    with ctx.db.session() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        if not b:
            raise ServiceError("Book not found", 404)
        out = _book_summary(conn, b, _book_stats(conn, [book_id]).get(book_id, {}), False)
        counts = _chapter_status_counts(conn, book_id)
        out["chapters"] = [_chapter_row(ch, counts.get(ch["id"], {})) for ch in rows(conn, "SELECT * FROM chapters WHERE book_id=? ORDER BY idx", (book_id,))]
    return out


def get_book(ctx, book_id: int) -> dict:
    """Full book page payload. Costs a constant number of queries (independent of the segment count): the per-segment take lookups and
    file checks are batched in assembly.chapter_readiness, which runs once and also feeds the release check."""
    with ctx.db.session() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        if not b:
            raise ServiceError("Book not found", 404)
        stats = _book_stats(conn, [book_id]).get(book_id, {})
        out = _book_summary(conn, b, stats, R.runtime_context(conn, now(), ctx.settings()))
        out.update({k: b[k] for k in ("narrator_credit", "publisher", "year", "description", "language", "genre", "instructions", "synth_fingerprint", "archived")})
        out["has_cover"] = bool(b["cover_path"] and Path(b["cover_path"]).exists())
        out["cover_version"] = _cover_version(b["cover_path"])  # cache-buster for /media/cover/<id>: changes only when the file does
        out["settings"] = loads(b["settings_json"], {})
        out["storage"] = loads(b["storage_json"], {})
        vv = row(conn, "SELECT * FROM voice_versions WHERE id=?", (b["voice_version_id"],)) or {}
        v = row(conn, "SELECT * FROM voices WHERE id=?", (b["voice_id"],)) or {}
        out["voice_detail"] = {"id": v.get("id"), "name": v.get("name"), "kind": v.get("kind"), "version": vv.get("version"), "version_id": vv.get("id"),
                               "current_version_id": v.get("current_version_id"), "model_id": vv.get("model_id"), "speaker": vv.get("speaker"),
                               "instruct": vv.get("instruct")}
        out["voice_outdated"] = bool(v and v.get("current_version_id") != b["voice_version_id"])
        sibs = rows(conn, """SELECT x.id, x.state, x.archived, x.created_at, (SELECT name FROM voices vo WHERE vo.id=x.voice_id) AS narrator
                             FROM books x WHERE COALESCE(x.work_uid, x.uid)=? ORDER BY x.created_at, x.id""", (work_key(b),))
        sib_stats = _book_stats(conn, [x["id"] for x in sibs]) if len(sibs) > 1 else {}
        out["versions"] = [{"id": x["id"], "narrator": x["narrator"], "state": x["state"],
                            "archived": bool(x["archived"]), "current": x["id"] == book_id, "created_at": x["created_at"],
                            "progress": _progress(sib_stats.get(x["id"], {}))} for x in sibs]
        ready = chapter_readiness(conn, b, ctx)  # once: feeds both the chapter rows and the release check below
        counts = _chapter_status_counts(conn, book_id)
        out["chapters"] = []
        for r in ready:
            ch = r["chapter"]
            out["chapters"].append({**_chapter_row(ch, counts.get(ch["id"], {})), "missing_audio": len(r["problems"]),
                                    "render_current": r["render_current"],
                                    "renders": {k: {"id": v_["id"], "duration_s": v_["duration_s"], "draft": bool(v_["draft"]), "created_at": v_["created_at"],
                                                    "current": r["render_current"]} for k, v_ in r["renders"].items()}})
        m = row(conn, "SELECT * FROM manuscripts WHERE book_id=? ORDER BY id DESC LIMIT 1", (book_id,)) or {}
        out["manuscript"] = {"name": m.get("original_name"), "sha256": m.get("sha256"), "size_bytes": m.get("size_bytes"), "format": m.get("format"),
                             "manifest": loads(m.get("parse_report_json"), {})}
        out["exports"] = rows(conn, "SELECT id, kind, label, draft, size_bytes, duration_s, created_at FROM exports WHERE book_id=? ORDER BY id DESC LIMIT 50", (book_id,))
        out["release"] = active_release(conn, book_id)
        out["release_check"] = release_check_conn(conn, b, ctx, ready=ready, counts={k: n for k, n in stats.items() if not k.startswith("_")})
        out["logs"] = rows(conn, "SELECT id, level, message, created_at FROM job_logs WHERE book_id=? AND level<>'debug' ORDER BY id DESC LIMIT 40", (book_id,))
        out["utility_jobs"] = rows(conn, "SELECT id, kind, status, error, result_json, created_at, finished_at FROM jobs WHERE book_id=? AND kind IN ('assemble','export','analyze') ORDER BY id DESC LIMIT 10", (book_id,))
        out["narrative"] = N_store.narrative_status(ctx, book_id, conn)
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


QUEUE_WINDOW_DAYS = 30        # finished book jobs older than this drop off the Queue page
QUEUE_PAGE_MAX = 50
EARLIER_RUNS_MAX = 20
_DONE_SQL = "('completed','failed','cancelled')"


def _page(offset, limit) -> tuple[int, int]:
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(limit if limit is not None else 10)
    except (TypeError, ValueError):
        limit = 10
    return offset, max(1, min(QUEUE_PAGE_MAX, limit))


def _queue_entry(j: dict, st: dict, rt: dict, retries: int, last_log, t: float) -> dict:
    total = sum(v for k, v in st.items() if not k.startswith("_"))
    remaining = st.get("pending", 0) + st.get("synthesizing", 0)
    # eta_s / eta_finish_s are the pre-v0.8.0 fields; they now come from the same estimate the runtime dict carries
    eta = eta_finish = None
    if j["status"] in ("queued", "running"):
        eta = rt["remaining_s"] if j["status"] == "running" else rt["total_est_s"]
        eta_finish = (rt["finish_at"] - t) if (eta is not None and rt["finish_at"] is not None) else None
    return {"id": j["id"], "book_id": j["book_id"], "title": j["title"], "status": j["status"], "position": j["position"],
            "paused_reason": j["paused_reason"], "error": j["error"], "retries": j["retries"], "segment_retries": retries,
            "current_segment_sid": j["current_segment_sid"], "created_at": j["created_at"], "started_at": j["started_at"],
            "finished_at": j["finished_at"], "segments": total, "remaining": remaining, "flagged": st.get("flagged", 0),
            "progress": round((total - remaining) / total, 4) if total else 0, "eta_s": eta, "eta_finish_s": eta_finish,
            "active_seconds": round(float(j["active_seconds"] or 0), 1), "runtime": rt,
            "pause_requested": bool(j["pause_requested"]), "cancel_requested": bool(j["cancel_requested"]), "last_log": last_log}


def list_queue(ctx, finished_offset: int = 0, finished_limit: int = 10, utility_offset: int = 0, utility_limit: int = 10) -> dict:
    """The Queue page: every active (queued/running/paused) book job, ONE "recently finished" entry per book (its latest finished run, with the
    book's earlier finished runs folded into ``earlier_runs``) and the assemble/export/preview/analyze jobs; the last two are paginated."""
    f_off, f_lim = _page(finished_offset, finished_limit)
    u_off, u_lim = _page(utility_offset, utility_limit)
    t = now()
    cut = t - QUEUE_WINDOW_DAYS * 86400
    fin_when = "COALESCE(j.finished_at, j.updated_at)"
    with ctx.db.session() as conn:
        active = rows(conn, f"""SELECT j.*, {DISPLAY_TITLE_SQL} AS title FROM jobs j LEFT JOIN books b ON b.id=j.book_id
                               WHERE j.kind='book' AND j.status IN ('queued','running','paused')
                               ORDER BY CASE j.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END, j.position, j.id""")
        latest = f"""SELECT j2.id FROM jobs j2 WHERE j2.book_id=j.book_id AND j2.kind='book' AND j2.status IN {_DONE_SQL}
                     AND COALESCE(j2.finished_at, j2.updated_at) > ? ORDER BY COALESCE(j2.finished_at, j2.updated_at) DESC, j2.id DESC LIMIT 1"""
        finished_total = scalar(conn, f"""SELECT COUNT(DISTINCT j.book_id) FROM jobs j WHERE j.kind='book' AND j.book_id IS NOT NULL
                                         AND j.status IN {_DONE_SQL} AND {fin_when} > ?""", (cut,), 0)
        finished = rows(conn, f"""SELECT j.*, {DISPLAY_TITLE_SQL} AS title FROM jobs j JOIN books b ON b.id=j.book_id
                                 WHERE j.kind='book' AND j.status IN {_DONE_SQL} AND {fin_when} > ? AND j.id = ({latest})
                                 ORDER BY {fin_when} DESC, j.id DESC LIMIT ? OFFSET ?""", (cut, cut, f_lim, f_off))
        earlier: dict[int, list[dict]] = {}
        if finished:
            marks = ",".join("?" * len(finished))
            for r in rows(conn, f"""SELECT j.id, j.book_id, j.status, j.created_at, j.started_at, j.finished_at, j.updated_at, j.active_seconds FROM jobs j
                                   WHERE j.kind='book' AND j.book_id IN ({marks}) AND j.status IN {_DONE_SQL} AND {fin_when} > ? AND j.id NOT IN ({marks})
                                   ORDER BY {fin_when} DESC, j.id DESC""", ([x["book_id"] for x in finished] + [cut] + [x["id"] for x in finished])):
                lst = earlier.setdefault(r["book_id"], [])
                if len(lst) < EARLIER_RUNS_MAX:
                    lst.append(r)
        utility_total = scalar(conn, "SELECT COUNT(*) FROM jobs WHERE kind IN ('assemble','export','preview','analyze')", (), 0)
        utility = rows(conn, f"""SELECT j.id, j.kind, j.status, j.error, j.book_id, {DISPLAY_TITLE_SQL} AS title, j.created_at, j.finished_at, j.payload_json
                                FROM jobs j LEFT JOIN books b ON b.id=j.book_id WHERE j.kind IN ('assemble','export','preview','analyze')
                                ORDER BY j.id DESC LIMIT ? OFFSET ?""", (u_lim, u_off))
        per_seg = _segment_seconds(conn)
        rt_ctx = R.runtime_context(conn, t, ctx.settings())
        book_ids = sorted({j["book_id"] for j in active + finished if j["book_id"]})
        stats = _book_stats(conn, book_ids) if book_ids else {}
        # how much each shown run actually rendered: distinct segments with an attempt that started during the run
        runs = [r for lst in earlier.values() for r in lst] + finished
        rendered: dict[int, int] = {}
        if runs:
            marks = ",".join("?" * len(runs))
            rendered = {r["id"]: r["n"] for r in rows(conn, f"""
                SELECT j.id, COUNT(DISTINCT a.segment_id) n FROM jobs j JOIN segments s ON s.book_id = j.book_id JOIN attempts a ON a.segment_id = s.id
                WHERE j.id IN ({marks}) AND a.created_at >= COALESCE(j.started_at, j.created_at) AND a.created_at <= COALESCE(j.finished_at, j.updated_at)
                GROUP BY j.id""", [r["id"] for r in runs])}

        def entry(j: dict) -> dict:
            st = stats.get(j["book_id"], {})
            last_log = row(conn, "SELECT message, level, created_at FROM job_logs WHERE job_id=? AND level<>'debug' ORDER BY id DESC LIMIT 1", (j["id"],))
            retries = scalar(conn, "SELECT COUNT(*) FROM attempts a JOIN segments s ON s.id=a.segment_id WHERE s.book_id=? AND a.attempt_no>1", (j["book_id"],), 0)
            return _queue_entry(j, st, R.job_runtime(conn, j, rt_ctx), retries, last_log, t)

        def run_summary(r: dict, total_segments: int) -> dict:
            n = rendered.get(r["id"], 0)
            secs = float(r["active_seconds"] or 0)
            if secs <= 0 and r["started_at"] and r["finished_at"] and r["finished_at"] >= r["started_at"]:
                secs = float(r["finished_at"] - r["started_at"])
            return {"id": r["id"], "status": r["status"], "finished_at": r["finished_at"] or r["updated_at"], "segments_rendered": n,
                    "active_seconds": round(secs, 1), "kind_hint": "full pass" if total_segments and n * 2 >= total_segments else "touch-up"}

        out = [entry(j) for j in active]
        done_entries = []
        for j in finished:
            e = entry(j)
            total = e["segments"]
            e["latest_run"] = run_summary(j, total)
            e["earlier_runs"] = [run_summary(r, total) for r in earlier.get(j["book_id"], [])]
            done_entries.append(e)
        for u in utility:
            u["payload"] = loads(u.pop("payload_json"), {})
    return {"jobs": out + done_entries, "active": out, "finished": done_entries, "finished_total": finished_total,
            "finished_offset": f_off, "finished_limit": f_lim, "window_days": QUEUE_WINDOW_DAYS,
            "utility": utility, "utility_total": utility_total, "utility_offset": u_off, "utility_limit": u_lim, "throughput": per_seg}


def _segment_seconds(conn) -> dict:
    return R.global_rate(conn)


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


# ======================================================================== blind A/B listening


def _ab_plans(conn, ctx, seg: dict, book: dict, comparison: str):
    """The two plans of a comparison for one segment (read-only). Refuses (409) when the comparison does not apply to the
    segment's voice kind or when both settings give the same audio, because such a pair would only ever tie."""
    c = N_ab.AB_COMPARISONS[comparison]
    try:
        pa = N_store.plan_for_segment(conn, ctx, seg, book, ns_patch=c["a_patch"])
        pb = N_store.plan_for_segment(conn, ctx, seg, book, ns_patch=c["b_patch"])
    except ValueError as exc:
        raise ServiceError(str(exc), 409)
    if pa.voice_kind not in c["kinds"]:
        raise ServiceError(f"\"{c['title']}\" applies to {c['applies']}; this segment is read by a {pa.voice_kind} voice. Try another segment.", 409)
    if pa.gen_key == pb.gen_key or (pa.text, pa.instruct, pa.ref_id, round(pa.pace, 3)) == (pb.text, pb.instruct, pb.ref_id, round(pb.pace, 3)):
        raise ServiceError("These two settings give the same audio for this segment; try another segment.", 409)
    return pa, pb


def _ab_comparison(name: str) -> dict:
    c = N_ab.AB_COMPARISONS.get(name)
    if not c:
        raise ServiceError(f"Unknown comparison '{name}'. Choose one of: {', '.join(N_ab.AB_COMPARISONS)}.", 422)
    return c


def enqueue_ab(ctx, segment_id: int, comparison: str, actor: str = "operator", swap: bool | None = None) -> dict:
    """Queue the two preview renders of a blind A/B round on one segment. Both use the same seed, so any difference comes from
    the settings alone. Which setting sits behind clip X is random and stored only server side (``swap`` is for tests)."""
    c = _ab_comparison(comparison)
    with ctx.db.tx() as conn:
        seg = row(conn, "SELECT * FROM segments WHERE id=?", (segment_id,))
        if not seg:
            raise ServiceError("Segment not found", 404)
        book = row(conn, "SELECT * FROM books WHERE id=?", (seg["book_id"],))
        pa, pb = _ab_plans(conn, ctx, seg, book, comparison)
        round_id = uuid.uuid4().hex
        seed = int(round_id[:7], 16)
        swap = (secrets.randbelow(2) == 1) if swap is None else bool(swap)
        side_of = {"x": "b" if swap else "a", "y": "a" if swap else "b"}
        jobs = []
        for slot in ("x", "y"):  # x is queued first, so the job order says nothing about which setting is which
            side = side_of[slot]
            payload = {"segment_id": segment_id, "seed": seed, "ns_patch": c[f"{side}_patch"],
                       "ab": {"round_id": round_id, "slot": slot, "side": side, "comparison": comparison, "gen_key": (pa if side == "a" else pb).gen_key}}
            jobs.append(conn.execute("INSERT INTO jobs(kind, book_id, payload_json, status, position, created_at, updated_at) VALUES ('preview',?,?, 'queued', 0, ?, ?)",
                                     (seg["book_id"], dumps(payload), now(), now())).lastrowid)
    return {"round_id": round_id, "jobs": jobs, "segment_id": segment_id, "book_id": seg["book_id"], "comparison": comparison}


def _ab_round_jobs(conn, round_id: str) -> list[dict]:
    if not re.fullmatch(r"[0-9a-f]{32}", round_id or ""):
        raise ServiceError("A/B round not found", 404)
    out = []
    for j in rows(conn, "SELECT * FROM jobs WHERE kind='preview' AND payload_json LIKE ? ORDER BY id", (f'%"round_id":"{round_id}"%',)):
        j["payload"] = loads(j["payload_json"], {})
        j["result"] = loads(j["result_json"], None)
        if (j["payload"].get("ab") or {}).get("round_id") == round_id:
            out.append(j)
    if len(out) != 2:
        raise ServiceError("A/B round not found", 404)
    return out


def _ab_labels(comparison: str) -> tuple[str, str]:
    c = N_ab.AB_COMPARISONS[comparison]
    return c["a_label"], c["b_label"]


def get_ab_round(ctx, round_id: str) -> dict:
    """The round as the listener sees it: two anonymous clips (x, y). Which setting is behind each is withheld until a vote is cast."""
    with ctx.db.session() as conn:
        jobs = _ab_round_jobs(conn, round_id)
        first = jobs[0]["payload"]["ab"]
        comparison = first["comparison"]
        voted = next((j["payload"]["ab"]["voted"] for j in jobs if j["payload"]["ab"].get("voted")), None)
        tally = N_ab.tally(conn, jobs[0]["book_id"], comparison)[comparison] if voted else None
    slots = {j["payload"]["ab"]["slot"]: j for j in jobs}
    states = [j["status"] for j in jobs]
    status = "failed" if any(s in ("failed", "cancelled") for s in states) else "ready" if all(s == "completed" for s in states) else "pending"
    engines = {(j["result"] or {}).get("engine") for j in jobs if j["result"]}
    out = {"round_id": round_id, "comparison": comparison, "title": N_ab.AB_COMPARISONS[comparison]["title"], "segment_id": jobs[0]["payload"].get("segment_id"),
           "book_id": jobs[0]["book_id"], "status": status, "engine": next(iter(engines)) if len(engines) == 1 else None, "mock": "mock" in engines,
           "sides": [{"side": s, "job_id": slots[s]["id"], "status": slots[s]["status"], "media_url": f"/media/preview/{slots[s]['id']}" if slots[s]["status"] == "completed" else None}
                     for s in ("x", "y")],
           "voted": bool(voted)}
    if status == "failed":
        out["error"] = next((j["error"] for j in jobs if j.get("error")), "A preview render failed.")
    if voted:
        a_label, b_label = _ab_labels(comparison)
        label = {"a": a_label, "b": b_label}
        out["revealed"] = {s: label[slots[s]["payload"]["ab"]["side"]] for s in ("x", "y")}
        out["vote"] = {k: voted[k] for k in ("choice", "winner", "other_narrator")}
        out["tally"] = tally
    return out


def vote_ab(ctx, round_id: str, choice: str, other_narrator: bool = False, actor: str = "operator") -> dict:
    """Record the vote of a finished round (one per round) and reveal which setting was behind each clip."""
    if choice not in ("x", "y", "same"):
        raise ServiceError("choice must be x, y or same", 422)
    with ctx.db.tx() as conn:
        jobs = _ab_round_jobs(conn, round_id)
        if any(j["payload"]["ab"].get("voted") for j in jobs):
            raise ServiceError("This round already has a vote.", 409)
        if any(j["status"] != "completed" for j in jobs):
            raise ServiceError("Both clips must finish rendering before you can vote.", 409)
        slots = {j["payload"]["ab"]["slot"]: j for j in jobs}
        sides = {j["payload"]["ab"]["side"]: j for j in jobs}
        ab = jobs[0]["payload"]["ab"]
        comparison = ab["comparison"]
        a_label, b_label = _ab_labels(comparison)
        winner = "same" if choice == "same" else slots[choice]["payload"]["ab"]["side"]
        voter = resolve_actor(actor)
        vote_id = conn.execute("""INSERT INTO ab_votes(book_id, segment_id, comparison, a_label, b_label, a_gen_key, b_gen_key, winner, other_narrator, actor, created_at)
                                  VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                               (jobs[0]["book_id"], jobs[0]["payload"]["segment_id"], comparison, a_label, b_label, sides["a"]["payload"]["ab"].get("gen_key"),
                                sides["b"]["payload"]["ab"].get("gen_key"), winner, 1 if other_narrator else 0, voter, now())).lastrowid
        marker = {"vote_id": vote_id, "choice": choice, "winner": winner, "other_narrator": bool(other_narrator)}
        for j in jobs:
            j["payload"]["ab"]["voted"] = marker
            conn.execute("UPDATE jobs SET payload_json=? WHERE id=?", (dumps(j["payload"]), j["id"]))
        tally = N_ab.tally(conn, jobs[0]["book_id"], comparison)[comparison]
    label = {"a": a_label, "b": b_label}
    return {"round_id": round_id, "voted": True, "winner": winner, "other_narrator": bool(other_narrator), "comparison": comparison,
            "revealed": {s: label[slots[s]["payload"]["ab"]["side"]] for s in ("x", "y")}, "tally": tally}


def ab_tally(ctx, book_id: int) -> dict:
    with ctx.db.session() as conn:
        if not row(conn, "SELECT 1 FROM books WHERE id=?", (book_id,)):
            raise ServiceError("Book not found", 404)
        return N_ab.tally(conn, book_id)


def random_differing_segment(ctx, book_id: int, comparison: str, exclude: list[int] | None = None, limit: int = 200) -> dict:
    """A segment of the book whose two plans differ for this comparison (pure plan computation, no synthesis). Looks at a random
    sample of up to ``limit`` segments, preferring those that have a direction."""
    _ab_comparison(comparison)
    skip = set(exclude or [])
    with ctx.db.session() as conn:
        book = row(conn, "SELECT * FROM books WHERE id=?", (book_id,))
        if not book:
            raise ServiceError("Book not found", 404)
        ids = [r["segment_id"] for r in rows(conn, "SELECT segment_id FROM directions WHERE book_id=?", (book_id,))]
        if not ids:
            ids = [r["id"] for r in rows(conn, "SELECT id FROM segments WHERE book_id=?", (book_id,))]
        ids = [i for i in ids if i not in skip]
        random.shuffle(ids)
        for sid in ids[:limit]:
            seg = row(conn, "SELECT s.*, c.idx AS chapter_idx FROM segments s JOIN chapters c ON c.id=s.chapter_id WHERE s.id=?", (sid,))
            if not seg:
                continue
            try:
                _ab_plans(conn, ctx, seg, book, comparison)
            except ServiceError:
                continue
            text = N_store.spoken_text(seg)
            return {"segment_id": sid, "sid": seg["sid"], "chapter_idx": seg["chapter_idx"], "excerpt": text[:160] + ("…" if len(text) > 160 else "")}
    raise ServiceError("No segment differs for this comparison in this book (the two settings give the same audio everywhere that was checked).", 404)


def apply_delivery_to_takes(ctx, book_id: int, confirm: bool = False, include_approved: bool = False, actor: str = "operator") -> dict:
    """Bring already rendered takes in line with the delivery settings now in force: only segments whose selected take's
    generation key differs from the current plan are reset (old takes stay as history) and queued at the front."""
    with ctx.db.tx() as conn:
        b = row(conn, "SELECT * FROM books WHERE id=? AND archived=0", (book_id,))
        if not b:
            raise ServiceError("Book not found", 404)
        would = N_store.invalidate_changed(conn, ctx, book_id, include_approved=True, dry_run=True)
        status = {r["id"]: (r["status"], r["chapter_id"]) for r in rows(conn, f"SELECT id, status, chapter_id FROM segments WHERE id IN ({','.join('?' * len(would))})", would)} if would else {}
        approved = [i for i in would if status[i][0] == "approved"]
        chosen = would if include_approved else [i for i in would if status[i][0] != "approved"]
        chapters = len({status[i][1] for i in chosen})
        details = {"segments": len(chosen), "approved": len(approved), "chapters": chapters, "approved_included": bool(include_approved)}
        if not chosen:
            return {**details, "reset": 0, "queued": False,
                    "message": ("No rendered segment differs from the current delivery settings." if not approved else
                                f"No unapproved segment differs; {len(approved)} approved segment(s) would change. Include approved segments to regenerate them.")}
        if not confirm:
            raise NeedsConfirmation(f"{len(chosen)} rendered segments ({len(approved) if include_approved else 0} approved) in {chapters} chapter(s) would be regenerated to match the "
                                    "current delivery settings. Old takes are kept as history, never mixed into the book. "
                                    + ("" if include_approved else f"{len(approved)} approved segment(s) that would also change are left alone."), details)
        reset = N_store.invalidate_changed(conn, ctx, book_id, include_approved=include_approved)
        ctx.audit(conn, "book", book_id, "apply_delivery", {"segments": len(reset), "chapters": chapters, "include_approved": bool(include_approved)}, actor)
        ctx.event(conn, "narrator", f"Delivery settings applied to existing takes: {len(reset)} segment(s) marked for regeneration.", book_id, "warning")
        refresh_book_state(conn, book_id)
    job = enqueue_book(ctx, book_id, front=True) if reset else None
    return {**details, "segments": len(reset), "reset": len(reset), "queued": bool(job), "job_id": job["id"] if job else None}


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
        timing = None
        try:  # find this segment's entry inside SQLite: the whole chapter map (hundreds of entries) is not decoded into Python objects
            hit = row(conn, """SELECT j.value AS entry FROM chapter_renders r, json_each(r.segment_map_json) j
                               WHERE r.chapter_id=? AND r.kind='wav' AND json_extract(j.value, '$.sid')=? LIMIT 1""", (s["chapter_id"], s["sid"]))
            timing = loads(hit["entry"], None) if hit else None
        except Exception:  # SQLite built without JSON1: decode the map the old way
            render = row(conn, "SELECT segment_map_json FROM chapter_renders WHERE chapter_id=? AND kind='wav'", (s["chapter_id"],))
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
    actor = resolve_actor(actor)
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


def regenerate_segment(ctx, segment_id: int, note: str | None = None, extra: dict | None = None) -> dict:
    with ctx.db.tx() as conn:
        s, b = _seg_and_book(conn, segment_id)
        if s["status"] in ("pending", "synthesizing"):
            return {"ok": True, "status": s["status"], "segment_id": segment_id, "already_pending": True}
        conn.execute("UPDATE segments SET round=round+1, attempts_used=0, status='pending', selected_attempt_id=NULL, flag_reason=NULL, updated_at=? WHERE id=?",
                     (now(), segment_id))
        conn.execute("DELETE FROM chapter_renders WHERE chapter_id=?", (s["chapter_id"],))
        _review(conn, ctx, s, "regenerate", {"previous_status": s["status"], "previous_attempt": s["selected_attempt_id"], "note": note, **(extra or {})})
        _revoke(conn, ctx, s["book_id"], f"segment {s['sid']} regenerated")
    enqueue_book(ctx, s["book_id"], front=True)
    return {"ok": True, "status": "pending", "segment_id": segment_id}


def regenerate_with_direction(ctx, segment_id: int, direction: dict, note: str | None = None, actor: str = "operator") -> dict:
    """Regenerate one segment with a direction from the editor (for example emotion anger, intensity 5, delivery "shouting").

    The direction is ALWAYS kept as the segment's own user direction (the same record ``PATCH /api/segments/{id}/narrative`` writes); there is
    no one-shot mode, so later re-renders of the segment keep sounding the same. It is visible and clearable in Inspect and in the Review
    panel. Fields left out keep their stored value; a field given as None is cleared. The result carries ``delivery``: what the segment's
    voice kind will actually be given (see narrative.store.delivery_summary)."""
    patch = N_store.validate_direction(direction)
    if patch.get("emotion") and patch["emotion"] != "neutral" and patch.get("intensity") is None and "intensity" not in patch:
        patch["intensity"] = 3  # an emotion without a strength would otherwise inherit whatever the rules engine guessed
    if not patch:
        out = regenerate_segment(ctx, segment_id, note)
    else:
        N_store.set_segment_narrative(ctx, segment_id, patch, actor)
        with ctx.db.tx() as conn:
            s, _b = _seg_and_book(conn, segment_id)
            queued = s["status"] in ("pending", "synthesizing")
            if queued:  # the new direction already changed the take's key, so the segment went back to pending by itself
                _review(conn, ctx, s, "regenerate", {"note": note, "direction": patch, "reason": "direction changed"}, actor)
        if queued:
            try:
                enqueue_book(ctx, s["book_id"], front=True)  # idempotent; the segment may be pending without a job (a cascade reset)
            except ServiceError:
                pass
            out = {"ok": True, "status": s["status"], "segment_id": segment_id}
        else:
            out = regenerate_segment(ctx, segment_id, note, {"direction": patch})
    with ctx.db.session() as conn:
        s, b = _seg_and_book(conn, segment_id)
        out["delivery"] = N_store.delivery_summary(conn, ctx, s, b)
    return out


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


def release_check_conn(conn, b: dict, ctx=None, ready: list[dict] | None = None, counts: dict | None = None) -> dict:
    """Release blockers. ``ready`` (assembly.chapter_readiness output) and ``counts`` (segment status counts) may be passed by a caller that
    already computed them for the same book in the same transaction; the result is identical, without repeating the per-file checks."""
    if counts is None:
        counts = {r["status"]: r["n"] for r in rows(conn, "SELECT status, COUNT(*) n FROM segments WHERE book_id=? GROUP BY status", (b["id"],))}
    blockers = []
    pend = counts.get("pending", 0) + counts.get("synthesizing", 0)
    if pend:
        blockers.append(f"{pend} segment(s) have no audio yet.")
    if counts.get("flagged", 0):
        blockers.append(f"{counts['flagged']} segment(s) are flagged by automatic QA - approve, fix or regenerate them.")
    if ready is None:
        ready = chapter_readiness(conn, b, ctx)
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
        chk = release_check_conn(conn, b, ctx)
        if not chk["ready"]:
            raise ServiceError("Release is blocked: " + " ".join(chk["blockers"]), 409, chk)
        statement = ("I have reviewed this audiobook, confirm the rights to the manuscript and narrator voice, and approve these files as "
                     "publication candidates. Automatic QA is not a substitute for listening.")
        conn.execute("INSERT INTO releases(book_id, approved_by, listen_scope, statement, checklist_json, approved_at) VALUES (?,?,?,?,?,?)",
                     (book_id, approved_by, listen_scope, statement, dumps({"notes": notes[:2000], "counts": chk["counts"]}), now()))
        conn.execute("UPDATE chapter_renders SET draft=0 WHERE chapter_id IN (SELECT id FROM chapters WHERE book_id=?)", (book_id,))
        signed_in = resolve_actor(None)
        ctx.audit(conn, "book", book_id, "release_approved", {"by": approved_by, "signed_in_user": signed_in, "listen_scope": listen_scope},
                  signed_in if signed_in != "operator" else approved_by)
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
        flags = rows(conn, f"""SELECT s.id, s.sid, s.flag_reason, s.book_id, {DISPLAY_TITLE_SQL} AS title, c.idx AS chapter_idx FROM segments s JOIN books b ON b.id=s.book_id
                              JOIN chapters c ON c.id=s.chapter_id WHERE s.status='flagged' AND b.archived=0 ORDER BY s.updated_at DESC LIMIT 25""")
        events = rows(conn, "SELECT id, kind, book_id, message, level, created_at FROM events ORDER BY id DESC LIMIT 25")
        rt_ctx = R.runtime_context(conn, now(), ctx.settings())
        recent = [_book_summary(conn, b, stats.get(b["id"], {}), rt_ctx) for b in books[:12]]
        worker = row(conn, "SELECT * FROM worker_state WHERE id=1")
        failed_jobs = rows(conn, f"SELECT j.id, j.book_id, j.error, {DISPLAY_TITLE_SQL} AS title FROM jobs j JOIN books b ON b.id=j.book_id WHERE j.status='failed' AND j.kind='book' ORDER BY j.id DESC LIMIT 10")
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
            "heartbeat_at": hb, "heartbeat_age_s": round(now() - hb, 1) if hb else None, "current_job_id": w.get("current_job_id") if alive else None,
            "current_segment_sid": w.get("current_segment_sid") if alive else None, "engine": ctx.engine_mode}


def state_digest(ctx) -> dict:
    """Cheap snapshot used by the SSE stream to detect changes. Must be a pure function of stored state: nothing that
    varies with the clock alone (ages, timestamps of "now") may appear here, or the stream emits an event every second."""
    with ctx.db.session() as conn:
        ev = scalar(conn, "SELECT MAX(id) FROM events", (), 0)
        seg = scalar(conn, "SELECT MAX(updated_at) FROM segments", (), 0)
        job = scalar(conn, "SELECT MAX(updated_at) FROM jobs", (), 0)
        att = scalar(conn, "SELECT MAX(id) FROM attempts", (), 0)
        w = row(conn, "SELECT * FROM worker_state WHERE id=1")
        active = rows(conn, f"""SELECT j.id, j.book_id, j.status, j.current_segment_sid, {DISPLAY_TITLE_SQL} AS title FROM jobs j LEFT JOIN books b ON b.id=j.book_id
                               WHERE j.status IN ('running') ORDER BY j.id""")
        progress = {}
        for a in active:
            if a["book_id"]:
                st = {r["status"]: r["n"] for r in rows(conn, "SELECT status, COUNT(*) n FROM segments WHERE book_id=? GROUP BY status", (a["book_id"],))}
                total = sum(st.values())
                done = total - st.get("pending", 0) - st.get("synthesizing", 0)
                progress[a["book_id"]] = {"done": done, "total": total, "flagged": st.get("flagged", 0)}
    # heartbeat_age_s / heartbeat_at change on every beat; the digest must only move when something real changed.
    # (online flips when the heartbeat goes stale, which is a real change.) /api/worker still returns the exact age.
    ws = {k: v for k, v in worker_status_from_row(ctx, w).items() if k not in ("heartbeat_age_s", "heartbeat_at")}
    return {"events": ev, "segments": seg, "jobs": job, "attempts": att, "worker": ws, "active": active, "progress": progress,
            "worker_message": ws.get("message")}


# ===================================================================== settings


def get_settings(ctx) -> dict:
    s = ctx.settings()
    return {"settings": s.model_dump(mode="json"), "categories": {c: CATEGORY_LABELS[c] for c in STORAGE_CATEGORIES},
            "speakers": PRESET_SPEAKERS, "english_speakers": list(ENGLISH_SPEAKERS), "engine_mode": ctx.engine_mode,
            "effective_paths": {c: str(ctx.category_dir(c, s)) for c in STORAGE_CATEGORIES}, "library_root": str(ctx.library_root(s)),
            "secrets": ctx.secrets.status()}


def test_llm(ctx, base_url: str, model: str, transport=None, chapter_model: str = "", scene_model: str = "", translator_model: str = "") -> dict:
    """Try the endpoint and model currently typed in Settings (saved or not); uses the saved key slot and key-env setting."""
    from pydantic import ValidationError

    from .config import LLMSettings
    from .narrative.llm import LLMClient

    saved = ctx.settings().narrative.llm
    try:
        cfg = LLMSettings(provider="openai_compatible", base_url=base_url, model=model, api_key_env=saved.api_key_env, timeout_s=30.0,
                          chapter_model=chapter_model, scene_model=scene_model, translator_model=translator_model)
    except ValidationError as exc:
        raise ServiceError("; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors()), 422)
    return LLMClient(cfg, transport=transport, secrets=ctx.secrets).test_connection()


def set_secret(ctx, slot: str, value: str | None, actor: str = "operator") -> dict:
    """Save (or, with value=None, remove) the API key in a slot. The key itself is never audited or returned."""
    try:
        if value is None:
            action = "clear" if ctx.secrets.delete(slot) else "clear_noop"
        else:
            ctx.secrets.set(slot, value)
            action = "set"
    except SecretError as exc:
        raise ServiceError(str(exc), 422)
    with ctx.db.tx() as conn:
        ctx.audit(conn, "secret", slot, action, {}, actor)
    return {"secrets": ctx.secrets.status()}


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
