"""SQLite persistence: connection handling, transactions and ordered schema migrations."""
from __future__ import annotations

import shutil
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS voices (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('preset','design','clone')),
    description TEXT NOT NULL DEFAULT '',
    builtin INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    current_version_id INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS voice_versions (
    id INTEGER PRIMARY KEY,
    voice_id INTEGER NOT NULL REFERENCES voices(id),
    version INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    speaker TEXT,
    language TEXT NOT NULL DEFAULT 'English',
    instruct TEXT,
    ref_audio_path TEXT,
    ref_original_path TEXT,
    ref_text TEXT,
    ref_sha256 TEXT,
    ref_duration_s REAL,
    consent_json TEXT,
    created_at REAL NOT NULL,
    UNIQUE (voice_id, version)
);
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    narrator_credit TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    year TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'English',
    genre TEXT NOT NULL DEFAULT 'Audiobook',
    cover_path TEXT,
    voice_id INTEGER REFERENCES voices(id),
    voice_version_id INTEGER REFERENCES voice_versions(id),
    instructions TEXT NOT NULL DEFAULT '',
    settings_json TEXT NOT NULL,
    synth_fingerprint TEXT NOT NULL,
    storage_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'imported',
    last_error TEXT,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS manuscripts (
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id),
    original_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    format TEXT NOT NULL,
    parse_report_json TEXT NOT NULL,
    imported_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_manuscripts_sha ON manuscripts(sha256);
CREATE TABLE IF NOT EXISTS chapters (
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id),
    idx INTEGER NOT NULL,
    title TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '',
    word_count INTEGER NOT NULL DEFAULT 0,
    segment_count INTEGER NOT NULL DEFAULT 0,
    est_duration_s REAL NOT NULL DEFAULT 0,
    UNIQUE (book_id, idx)
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY,
    sid TEXT NOT NULL UNIQUE,
    book_id INTEGER NOT NULL REFERENCES books(id),
    chapter_id INTEGER NOT NULL REFERENCES chapters(id),
    idx INTEGER NOT NULL,
    para_idx INTEGER NOT NULL DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'body',
    pause_kind TEXT NOT NULL DEFAULT 'sentence',
    original_text TEXT NOT NULL,
    narration_text TEXT NOT NULL,
    override_text TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','synthesizing','passed','flagged','approved')),
    round INTEGER NOT NULL DEFAULT 1,
    attempts_used INTEGER NOT NULL DEFAULT 0,
    selected_attempt_id INTEGER,
    flag_reason TEXT,
    note TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_segments_book_status ON segments(book_id, status);
CREATE INDEX IF NOT EXISTS ix_segments_chapter ON segments(chapter_id, idx);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    round INTEGER NOT NULL DEFAULT 1,
    attempt_no INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('in_progress','done','aborted','error')),
    engine TEXT NOT NULL,
    model_id TEXT,
    voice_version_id INTEGER,
    fingerprint TEXT NOT NULL,
    seed INTEGER,
    temperature REAL,
    text_spoken TEXT NOT NULL,
    wav_path TEXT,
    duration_s REAL,
    sample_rate INTEGER,
    asr_text TEXT,
    asr_json TEXT,
    wer REAL,
    issues_json TEXT,
    acoustics_json TEXT,
    score REAL,
    passed INTEGER,
    rtf REAL,
    peak_mem_gb REAL,
    synth_seconds REAL,
    error TEXT,
    versions_json TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS ix_attempts_segment ON attempts(segment_id);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('book','preview','assemble','export')),
    book_id INTEGER REFERENCES books(id),
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('queued','running','paused','completed','failed','cancelled')),
    position REAL NOT NULL DEFAULT 0,
    pause_requested INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    paused_reason TEXT,
    error TEXT,
    retries INTEGER NOT NULL DEFAULT 0,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    current_segment_sid TEXT,
    result_json TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    updated_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_book_job ON jobs(book_id)
    WHERE kind = 'book' AND status IN ('queued','running','paused');
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, kind, position);
CREATE TABLE IF NOT EXISTS job_logs (
    id INTEGER PRIMARY KEY,
    job_id INTEGER REFERENCES jobs(id),
    book_id INTEGER REFERENCES books(id),
    level TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_job_logs_job ON job_logs(job_id, id);
CREATE INDEX IF NOT EXISTS ix_job_logs_book ON job_logs(book_id, id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    book_id INTEGER,
    message TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS review_actions (
    id INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL REFERENCES segments(id),
    action TEXT NOT NULL,
    detail_json TEXT,
    actor TEXT NOT NULL DEFAULT 'operator',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_review_segment ON review_actions(segment_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    entity TEXT NOT NULL,
    entity_id TEXT,
    action TEXT NOT NULL,
    detail_json TEXT,
    actor TEXT NOT NULL DEFAULT 'operator',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS worker_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner TEXT,
    pid INTEGER,
    heartbeat_at REAL,
    state TEXT NOT NULL DEFAULT 'offline',
    current_job_id INTEGER,
    current_segment_sid TEXT,
    message TEXT,
    engine TEXT,
    started_at REAL
);
INSERT OR IGNORE INTO worker_state (id, state) VALUES (1, 'offline');
CREATE TABLE IF NOT EXISTS chapter_renders (
    id INTEGER PRIMARY KEY,
    chapter_id INTEGER NOT NULL REFERENCES chapters(id),
    kind TEXT NOT NULL CHECK (kind IN ('wav','mp3')),
    path TEXT NOT NULL,
    duration_s REAL NOT NULL,
    segment_map_json TEXT NOT NULL,
    takes_digest TEXT NOT NULL,
    draft INTEGER NOT NULL DEFAULT 1,
    probe_json TEXT,
    created_at REAL NOT NULL,
    UNIQUE (chapter_id, kind)
);
CREATE TABLE IF NOT EXISTS exports (
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id),
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    path TEXT NOT NULL,
    draft INTEGER NOT NULL DEFAULT 1,
    size_bytes INTEGER,
    duration_s REAL,
    probe_json TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS releases (
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id),
    approved_by TEXT NOT NULL,
    listen_scope TEXT NOT NULL,
    statement TEXT NOT NULL,
    checklist_json TEXT NOT NULL,
    approved_at REAL NOT NULL,
    revoked_at REAL,
    revoked_reason TEXT
);
CREATE TABLE IF NOT EXISTS imports (
    id INTEGER PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    format TEXT NOT NULL,
    preview_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    consumed_book_id INTEGER
);
"""

# v0.3.0: semantic segmentation, narrative director and per-manuscript story context.
# The jobs table is rebuilt only to widen its CHECK constraint with the 'analyze' kind
# (SQLite cannot alter a CHECK); migrate() disables foreign keys around it and re-verifies them.
SCHEMA_V2 = """
CREATE TABLE jobs_v2 (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('book','preview','assemble','export','analyze')),
    book_id INTEGER REFERENCES books(id),
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('queued','running','paused','completed','failed','cancelled')),
    position REAL NOT NULL DEFAULT 0,
    pause_requested INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    paused_reason TEXT,
    error TEXT,
    retries INTEGER NOT NULL DEFAULT 0,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    current_segment_sid TEXT,
    result_json TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    updated_at REAL NOT NULL
);
INSERT INTO jobs_v2 (id, kind, book_id, payload_json, status, position, pause_requested, cancel_requested, paused_reason, error,
                     retries, consecutive_errors, current_segment_sid, result_json, created_at, started_at, finished_at, updated_at)
    SELECT id, kind, book_id, payload_json, status, position, pause_requested, cancel_requested, paused_reason, error,
           retries, consecutive_errors, current_segment_sid, result_json, created_at, started_at, finished_at, updated_at FROM jobs;
DROP TABLE jobs;
ALTER TABLE jobs_v2 RENAME TO jobs;
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_book_job ON jobs(book_id)
    WHERE kind = 'book' AND status IN ('queued','running','paused');
CREATE UNIQUE INDEX IF NOT EXISTS ux_one_active_analyze_job ON jobs(book_id)
    WHERE kind = 'analyze' AND status IN ('queued','running','paused');
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, kind, position);

ALTER TABLE books ADD COLUMN narrative_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE segments ADD COLUMN src_start INTEGER;
ALTER TABLE segments ADD COLUMN src_end INTEGER;
ALTER TABLE segments ADD COLUMN character_id INTEGER;
ALTER TABLE segments ADD COLUMN span_kind TEXT NOT NULL DEFAULT 'narration';
ALTER TABLE segments ADD COLUMN scene_idx INTEGER NOT NULL DEFAULT 0;
ALTER TABLE segments ADD COLUMN voice_override_id INTEGER;
ALTER TABLE segments ADD COLUMN needs_review INTEGER NOT NULL DEFAULT 0;
ALTER TABLE segments ADD COLUMN review_reason TEXT;
ALTER TABLE segments ADD COLUMN boundary_manual INTEGER NOT NULL DEFAULT 0;
ALTER TABLE attempts ADD COLUMN gen_key TEXT;
ALTER TABLE attempts ADD COLUMN instruct_used TEXT;
ALTER TABLE attempts ADD COLUMN delivery_applied INTEGER;
ALTER TABLE attempts ADD COLUMN voice_id_used INTEGER;
ALTER TABLE attempts ADD COLUMN asr_skipped INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS ix_segments_char ON segments(character_id);

CREATE TABLE chapter_text (
    chapter_id INTEGER PRIMARY KEY REFERENCES chapters(id),
    book_id INTEGER NOT NULL REFERENCES books(id),
    text TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE characters (
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id),
    uid TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'character' CHECK (kind IN ('narrator','character')),
    aliases_json TEXT NOT NULL DEFAULT '[]',
    gender TEXT NOT NULL DEFAULT '',
    voice_id INTEGER REFERENCES voices(id),
    voice_version_id INTEGER REFERENCES voice_versions(id),
    voice_description TEXT NOT NULL DEFAULT '',
    accent TEXT NOT NULL DEFAULT '',
    speech_pattern TEXT NOT NULL DEFAULT '',
    personality TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    relationships_json TEXT NOT NULL DEFAULT '[]',
    delivery_pref TEXT NOT NULL DEFAULT '',
    prov_json TEXT NOT NULL DEFAULT '{}',
    merged_into INTEGER,
    archived INTEGER NOT NULL DEFAULT 0,
    first_chapter INTEGER,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_characters_book ON characters(book_id);
CREATE TABLE nar_spans (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL UNIQUE,
    book_id INTEGER NOT NULL REFERENCES books(id),
    chapter_id INTEGER NOT NULL REFERENCES chapters(id),
    para_idx INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    start INTEGER NOT NULL,
    "end" INTEGER NOT NULL,
    kind TEXT NOT NULL,
    speaker_id INTEGER,
    guess_id INTEGER,
    attribution TEXT NOT NULL DEFAULT 'none',
    confidence REAL NOT NULL DEFAULT 0,
    needs_review INTEGER NOT NULL DEFAULT 0,
    evidence TEXT NOT NULL DEFAULT '',
    candidates_json TEXT NOT NULL DEFAULT '[]',
    scene_idx INTEGER NOT NULL DEFAULT 0,
    cues_json TEXT NOT NULL DEFAULT '{}',
    user_locked INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_spans_chapter ON nar_spans(chapter_id, start);
CREATE TABLE story_notes (
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id),
    chapter_idx INTEGER,
    scene_idx INTEGER,
    kind TEXT NOT NULL,
    character_id INTEGER,
    text TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1,
    confirmed INTEGER NOT NULL DEFAULT 0,
    refs_json TEXT NOT NULL DEFAULT '[]',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_notes_book ON story_notes(book_id, kind);
CREATE TABLE directions (
    id INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL UNIQUE REFERENCES segments(id),
    book_id INTEGER NOT NULL REFERENCES books(id),
    source TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    smoothed_json TEXT NOT NULL DEFAULT '{}',
    user_json TEXT NOT NULL DEFAULT '{}',
    final_json TEXT NOT NULL,
    smoothing_note TEXT NOT NULL DEFAULT '',
    needs_review INTEGER NOT NULL DEFAULT 0,
    cache_key TEXT,
    input_hash TEXT,
    state_json TEXT NOT NULL DEFAULT '{}',
    stale INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_directions_book ON directions(book_id);
CREATE TABLE direction_log (
    id INTEGER PRIMARY KEY,
    segment_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_dirlog_segment ON direction_log(segment_id, id);
CREATE TABLE director_cache (
    key TEXT PRIMARY KEY,
    response_json TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    hits INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE seg_edits (
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id),
    chapter_id INTEGER NOT NULL REFERENCES chapters(id),
    kind TEXT NOT NULL CHECK (kind IN ('cut','join')),
    offset INTEGER NOT NULL,
    actor TEXT NOT NULL DEFAULT 'operator',
    created_at REAL NOT NULL,
    UNIQUE (chapter_id, kind, offset)
);
CREATE TABLE segment_history (
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL,
    chapter_id INTEGER NOT NULL,
    sid TEXT NOT NULL,
    src_start INTEGER,
    src_end INTEGER,
    text TEXT NOT NULL,
    reason TEXT NOT NULL,
    retired_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_seghist_chapter ON segment_history(chapter_id);
CREATE TABLE analysis_stages (
    book_id INTEGER NOT NULL REFERENCES books(id),
    stage TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at REAL,
    finished_at REAL,
    error TEXT,
    stats_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (book_id, stage)
);
"""

SCHEMA_V3 = """
CREATE TABLE IF NOT EXISTS pronunciations (
    id INTEGER PRIMARY KEY,
    book_id INTEGER REFERENCES books(id),
    written TEXT NOT NULL,
    spoken TEXT NOT NULL,
    case_sensitive INTEGER NOT NULL DEFAULT 0,
    whole_word INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pron_book ON pronunciations(book_id);
ALTER TABLE voices ADD COLUMN pace REAL NOT NULL DEFAULT 1.0;
"""

# Each migration is (version, sql). Append only; never edit a released migration.
MIGRATIONS: list[tuple[int, str]] = [
    (1, SCHEMA_V1),
    (2, SCHEMA_V2),
    (3, SCHEMA_V3),
]
LATEST_VERSION = MIGRATIONS[-1][0]


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def tx(self, conn: sqlite3.Connection | None = None) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception."""
        own = conn is None
        c = conn or self.connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
            except BaseException:
                c.execute("ROLLBACK")
                raise
            else:
                c.execute("COMMIT")
        finally:
            if own:
                c.close()

    def schema_version(self) -> int:
        with self.session() as c:
            return int(c.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self, backup_dir: Path | None = None) -> tuple[int, int]:
        """Apply pending migrations. Takes a consistent backup copy first when upgrading an
        existing database."""
        current = self.schema_version()
        if current >= LATEST_VERSION:
            return current, current
        if current > 0 and backup_dir is not None:
            backup_dir.mkdir(parents=True, exist_ok=True)
            dest = backup_dir / f"studio-v{current}-{time.strftime('%Y%m%d-%H%M%S')}.db"
            with self.session() as src, sqlite3.connect(str(dest)) as dst:
                src.backup(dst)
        with self.session() as c:
            # Table rebuilds need foreign keys off (a no-op inside a transaction); the
            # integrity of every reference is verified before each commit instead.
            c.execute("PRAGMA foreign_keys = OFF")
            for version, sql in MIGRATIONS:
                if version <= current:
                    continue
                c.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in _split_sql(sql):
                        c.execute(stmt)
                    bad = c.execute("PRAGMA foreign_key_check").fetchall()
                    if bad:
                        raise sqlite3.IntegrityError(f"migration {version} left {len(bad)} dangling foreign key reference(s)")
                    c.execute(f"PRAGMA user_version = {int(version)}")
                    c.execute("COMMIT")
                except BaseException:
                    c.execute("ROLLBACK")
                    raise
            c.execute("PRAGMA foreign_keys = ON")
        return current, LATEST_VERSION


def _split_sql(sql: str) -> list[str]:
    stmts, buf = [], []
    for line in sql.splitlines():
        buf.append(line)
        joined = "\n".join(buf)
        if joined.strip().endswith(";") and sqlite3.complete_statement(joined):
            stmts.append(joined.strip())
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def rows(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def row(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r is not None else None


def scalar(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
    r = conn.execute(sql, tuple(params)).fetchone()
    if r is None or r[0] is None:
        return default
    return r[0]


def copy_db_file(src: Path, dest: Path) -> None:
    shutil.copy2(src, dest)
