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

# Each migration is (version, sql). Append only; never edit a released migration.
MIGRATIONS: list[tuple[int, str]] = [
    (1, SCHEMA_V1),
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
            for version, sql in MIGRATIONS:
                if version <= current:
                    continue
                c.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in _split_sql(sql):
                        c.execute(stmt)
                    c.execute(f"PRAGMA user_version = {int(version)}")
                    c.execute("COMMIT")
                except BaseException:
                    c.execute("ROLLBACK")
                    raise
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
