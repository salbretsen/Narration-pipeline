"""Per-manuscript characters and story notes.

Every inferred fact carries provenance (``rules`` / ``llm`` / ``user``), a confidence and a
``confirmed`` flag. A field the user has set or confirmed is never overwritten by analysis.
Merging two identities re-points their dialogue and keeps the old name as an alias, so a
recurring character is not split into new speakers when their name or title changes.
"""
from __future__ import annotations

from ..db import row, rows
from ..util import dumps, loads, new_uid, now
from .structure import NARRATOR_NAME, Cast, KnownCharacter

TEXT_FIELDS = ("gender", "voice_description", "accent", "speech_pattern", "personality", "description", "delivery_pref")


def _err(message: str, status: int = 400, details=None):
    from ..services import ServiceError

    return ServiceError(message, status, details)


def parse_char(r: dict) -> dict:
    out = dict(r)
    out["aliases"] = loads(out.pop("aliases_json"), []) or []
    out["relationships"] = loads(out.pop("relationships_json"), []) or []
    out["prov"] = loads(out.pop("prov_json"), {}) or {}
    return out


def set_prov(prov: dict, field: str, source: str, confidence: float = 1.0, confirmed: bool = False) -> None:
    prov[field] = {"source": source, "confidence": round(float(confidence), 2), "confirmed": bool(confirmed)}


def is_confirmed(prov: dict, field: str) -> bool:
    return bool((prov.get(field) or {}).get("confirmed"))


def list_characters(conn, book_id: int, include_archived: bool = False) -> list[dict]:
    q = "SELECT * FROM characters WHERE book_id=?" + ("" if include_archived else " AND archived=0") + " ORDER BY (kind='narrator') DESC, id"
    chars = [parse_char(r) for r in rows(conn, q, (book_id,))]
    lines = {r["character_id"]: r for r in rows(
        conn, """SELECT character_id, COUNT(*) segs, SUM(LENGTH(original_text)) chars, SUM(needs_review) review,
                        MIN(chapter_id) first_ch FROM segments WHERE book_id=? AND character_id IS NOT NULL GROUP BY character_id""", (book_id,))}
    voices = {r["id"]: r for r in rows(conn, "SELECT id, name, kind FROM voices")}
    for c in chars:
        st = lines.get(c["id"], {})
        c["segments"], c["characters_spoken"], c["review_count"] = st.get("segs", 0) or 0, st.get("chars", 0) or 0, st.get("review", 0) or 0
        v = voices.get(c["voice_id"])
        c["voice"] = {"id": v["id"], "name": v["name"], "kind": v["kind"]} if v else None
    return chars


def known_for_analysis(conn, book_id: int) -> list[KnownCharacter]:
    out = []
    for r in rows(conn, "SELECT * FROM characters WHERE book_id=? AND archived=0 AND merged_into IS NULL", (book_id,)):
        c = parse_char(r)
        out.append(KnownCharacter(name=c["name"], aliases=c["aliases"], gender=c["gender"], kind=c["kind"], id=c["id"]))
    return out


def ensure_narrator(conn, book_id: int) -> int:
    r = row(conn, "SELECT id FROM characters WHERE book_id=? AND kind='narrator' AND archived=0", (book_id,))
    if r:
        return r["id"]
    return conn.execute(
        "INSERT INTO characters(book_id, uid, name, kind, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (book_id, new_uid("ch"), NARRATOR_NAME, "narrator", now(), now())).lastrowid


def sync_cast(conn, book_id: int, cast: Cast, chapter_idx: int | None = None) -> dict[str, int]:
    """Create/update character rows from the analysis cast. -> {entry name: character id}."""
    existing = [parse_char(r) for r in rows(conn, "SELECT * FROM characters WHERE book_id=? AND archived=0", (book_id,))]
    by_id = {c["id"]: c for c in existing}
    by_name = {}
    for c in existing:
        for nm in {c["name"], *c["aliases"]}:
            by_name.setdefault(nm.lower(), c)
    mapping: dict[str, int] = {}
    for e in cast.entries.values():
        c = by_id.get(e.id) if e.id else None
        if c is None:
            c = by_name.get(e.name.lower()) or next((by_name[a.lower()] for a in e.aliases if a.lower() in by_name), None)
        if e.kind == "narrator":
            mapping[e.name] = ensure_narrator(conn, book_id)
            continue
        if c is None:
            prov = {}
            if e.gender:
                set_prov(prov, "gender", "rules", 0.5)
            set_prov(prov, "name", "rules", 0.8)
            cid = conn.execute(
                """INSERT INTO characters(book_id, uid, name, kind, aliases_json, gender, prov_json, first_chapter, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (book_id, new_uid("ch"), e.name, "character", dumps(sorted(e.aliases)), e.gender, dumps(prov), chapter_idx, now(), now())).lastrowid
            mapping[e.name] = cid
            by_name[e.name.lower()] = {"id": cid, "name": e.name, "aliases": sorted(e.aliases)}
            continue
        prov = c["prov"]
        aliases = list(c["aliases"])
        for a in sorted(e.aliases | ({e.name} if e.name != c["name"] else set())):
            if a != c["name"] and a not in aliases:
                aliases.append(a)
        sets = {"aliases_json": dumps(aliases)}
        if e.gender and not c["gender"] and not is_confirmed(prov, "gender"):
            sets["gender"] = e.gender
            set_prov(prov, "gender", "rules", 0.5)
        if e.name != c["name"] and not is_confirmed(prov, "name") and not e.known:
            if c["name"] not in aliases:
                aliases.append(c["name"])
            sets.update({"name": e.name, "aliases_json": dumps([a for a in aliases if a != e.name])})
        sets["prov_json"] = dumps(prov)
        conn.execute(f"UPDATE characters SET {', '.join(k + '=?' for k in sets)}, updated_at=? WHERE id=?", (*sets.values(), now(), c["id"]))
        mapping[e.name] = c["id"]
    return mapping


def create_character(ctx, book_id: int, name: str, actor: str = "operator") -> dict:
    name = " ".join((name or "").split())
    if not 1 <= len(name) <= 80:
        raise _err("Enter a character name (1-80 characters).", 422)
    with ctx.db.tx() as conn:
        if not row(conn, "SELECT id FROM books WHERE id=?", (book_id,)):
            raise _err("Book not found", 404)
        for c in rows(conn, "SELECT name, aliases_json FROM characters WHERE book_id=? AND archived=0", (book_id,)):
            if name.lower() == c["name"].lower() or name.lower() in [a.lower() for a in loads(c["aliases_json"], [])]:
                raise _err(f"'{name}' already exists as a character or alias.", 409)
        prov = {}
        set_prov(prov, "name", "user", 1.0, True)
        cid = conn.execute("INSERT INTO characters(book_id, uid, name, prov_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                           (book_id, new_uid("ch"), name, dumps(prov), now(), now())).lastrowid
        ctx.audit(conn, "character", cid, "create", {"name": name}, actor)
        return parse_char(row(conn, "SELECT * FROM characters WHERE id=?", (cid,)))


def get_character(conn, character_id: int) -> dict:
    r = row(conn, "SELECT * FROM characters WHERE id=?", (character_id,))
    if not r:
        raise _err("Character not found", 404)
    return parse_char(r)


def apply_patch(ctx, conn, character_id: int, patch: dict, actor: str = "operator") -> tuple[dict, bool]:
    """Apply user edits (confirmed, never overwritten by analysis). -> (updated character, voice_changed)."""
    c = get_character(conn, character_id)
    if c["archived"]:
        raise _err("This character was merged into another one.", 409)
    prov = c["prov"]
    sets: dict = {}
    voice_changed = False
    if "name" in patch and patch["name"] is not None:
        nm = " ".join(str(patch["name"]).split())
        if not 1 <= len(nm) <= 80:
            raise _err("Name must be 1-80 characters.", 422)
        if c["kind"] == "narrator":
            raise _err("The narrator's name cannot be changed.", 422)
        if nm != c["name"]:
            clash = row(conn, "SELECT id FROM characters WHERE book_id=? AND id<>? AND archived=0 AND LOWER(name)=LOWER(?)", (c["book_id"], c["id"], nm))
            if clash:
                raise _err(f"Another character is already named '{nm}'. Merge them instead.", 409)
            sets["name"] = nm
            aliases = [a for a in c["aliases"] if a.lower() != nm.lower()] + [c["name"]]
            sets["aliases_json"] = dumps(aliases)
            c["aliases"] = aliases
            set_prov(prov, "name", "user", 1.0, True)
    if "aliases" in patch and patch["aliases"] is not None:
        al = []
        for a in patch["aliases"]:
            a = " ".join(str(a).split())
            if a and a.lower() != sets.get("name", c["name"]).lower() and a.lower() not in [x.lower() for x in al]:
                if len(a) > 80:
                    raise _err("Aliases must be at most 80 characters.", 422)
                other = row(conn, "SELECT id FROM characters WHERE book_id=? AND id<>? AND archived=0 AND (LOWER(name)=LOWER(?) OR aliases_json LIKE ?)",
                            (c["book_id"], c["id"], a, f'%"{a}"%'))
                if other:
                    raise _err(f"'{a}' already belongs to another character; merge the characters instead.", 409)
                al.append(a)
        sets["aliases_json"] = dumps(al[:30])
        set_prov(prov, "aliases", "user", 1.0, True)
    for f in TEXT_FIELDS:
        if f in patch and patch[f] is not None:
            v = " ".join(str(patch[f]).split())[:600]
            if f == "gender" and v not in ("", "male", "female", "neutral"):
                raise _err("gender must be male, female, neutral or empty", 422)
            if v != c[f]:
                sets[f] = v
            set_prov(prov, f, "user", 1.0, True)
    if "relationships" in patch and patch["relationships"] is not None:
        rel = [{"with": str(x.get("with", ""))[:80], "kind": str(x.get("kind", ""))[:80]} for x in patch["relationships"] if isinstance(x, dict)][:30]
        sets["relationships_json"] = dumps(rel)
        set_prov(prov, "relationships", "user", 1.0, True)
    if "voice_id" in patch:
        if c["kind"] == "narrator":
            raise _err("The narrator uses the book's narrator voice. Change it from the book's narrator settings.", 422)
        vid = patch["voice_id"]
        if vid in (None, 0):
            if c["voice_id"] is not None:
                voice_changed = True
            sets.update({"voice_id": None, "voice_version_id": None})
        else:
            v = row(conn, "SELECT * FROM voices WHERE id=? AND archived=0", (vid,))
            if not v:
                raise _err("Voice not found", 404)
            if c["voice_id"] != vid or c["voice_version_id"] != v["current_version_id"]:
                voice_changed = True
            sets.update({"voice_id": vid, "voice_version_id": v["current_version_id"]})
        set_prov(prov, "voice", "user", 1.0, True)
    if sets or prov != c["prov"]:
        sets["prov_json"] = dumps(prov)
        conn.execute(f"UPDATE characters SET {', '.join(k + '=?' for k in sets)}, updated_at=? WHERE id=?", (*sets.values(), now(), character_id))
        ctx.audit(conn, "character", character_id, "update", {k: v for k, v in patch.items()}, actor)
    return get_character(conn, character_id), voice_changed


def merge_into(ctx, conn, src_id: int, dst_id: int, actor: str = "operator") -> dict:
    """Fold ``src`` into ``dst``: dialogue, segments and aliases move; src is archived (kept for audit)."""
    if src_id == dst_id:
        raise _err("Choose a different character to merge into.", 422)
    src, dst = get_character(conn, src_id), get_character(conn, dst_id)
    if src["book_id"] != dst["book_id"]:
        raise _err("Characters belong to different books.", 422)
    if src["kind"] == "narrator":
        raise _err("The narrator cannot be merged into a character.", 422)
    if src["archived"] or dst["archived"]:
        raise _err("One of these characters was already merged.", 409)
    aliases = list(dst["aliases"])
    for a in [src["name"], *src["aliases"]]:
        if a.lower() != dst["name"].lower() and a.lower() not in [x.lower() for x in aliases]:
            aliases.append(a)
    prov = dst["prov"]
    set_prov(prov, "aliases", "user", 1.0, True)
    sets = {"aliases_json": dumps(aliases), "prov_json": dumps(prov)}
    for f in TEXT_FIELDS:
        if not dst[f] and src[f]:
            sets[f] = src[f]
            prov[f] = src["prov"].get(f, prov.get(f, {"source": "user", "confidence": 1.0, "confirmed": True}))
    sets["prov_json"] = dumps(prov)
    if dst["voice_id"] is None and src["voice_id"] is not None and dst["kind"] != "narrator":
        sets.update({"voice_id": src["voice_id"], "voice_version_id": src["voice_version_id"]})
    conn.execute(f"UPDATE characters SET {', '.join(k + '=?' for k in sets)}, updated_at=? WHERE id=?", (*sets.values(), now(), dst_id))
    n_seg = conn.execute("UPDATE segments SET character_id=? WHERE character_id=?", (dst_id, src_id)).rowcount
    n_span = conn.execute("UPDATE nar_spans SET speaker_id=? WHERE speaker_id=?", (dst_id, src_id)).rowcount
    conn.execute("UPDATE nar_spans SET guess_id=? WHERE guess_id=?", (dst_id, src_id))
    conn.execute("UPDATE story_notes SET character_id=? WHERE character_id=?", (dst_id, src_id))
    conn.execute("UPDATE characters SET merged_into=?, archived=1, updated_at=? WHERE id=?", (dst_id, now(), src_id))
    ctx.audit(conn, "character", dst_id, "merge", {"from": src_id, "from_name": src["name"], "segments": n_seg, "spans": n_span}, actor)
    return get_character(conn, dst_id)


def profile_for_prompt(c: dict) -> dict:
    """The character profile as the Director sees it: user-confirmed facts first, inferred ones labelled."""
    prof = {"name": c["name"], "aliases": c["aliases"][:8]}
    confirmed, inferred = {}, {}
    for f in (*TEXT_FIELDS, "relationships"):
        v = c.get(f)
        if not v:
            continue
        (confirmed if is_confirmed(c["prov"], f) else inferred)[f] = v
    if confirmed:
        prof["confirmed"] = confirmed
    if inferred:
        prof["inferred_unconfirmed"] = inferred
    return prof
