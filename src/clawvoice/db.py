"""SQLite setup. One host, one database, WAL, explicit transactions."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS voices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    backend TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    reference_version_id TEXT NOT NULL,
    preview_artifact_id TEXT,
    authorized INTEGER NOT NULL DEFAULT 0,
    authorized_at INTEGER,
    metadata_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reference_versions (
    id TEXT PRIMARY KEY,
    voice_id TEXT NOT NULL REFERENCES voices(id) ON DELETE CASCADE,
    original_path TEXT NOT NULL,
    normalized_path TEXT NOT NULL,
    original_sha256 TEXT NOT NULL,
    normalized_sha256 TEXT NOT NULL,
    original_media_type TEXT,
    original_format TEXT,
    source_duration_ms INTEGER NOT NULL,
    source_sample_rate INTEGER NOT NULL,
    source_channels INTEGER NOT NULL,
    selected_start_ms INTEGER NOT NULL,
    selected_end_ms INTEGER NOT NULL,
    reference_duration_ms INTEGER NOT NULL,
    reference_sample_rate INTEGER NOT NULL,
    preparation_json TEXT NOT NULL,
    checks_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reference_voices ON reference_versions(voice_id);

CREATE TABLE IF NOT EXISTS enrollment_sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER,
    completed_at INTEGER,
    upload_id TEXT,
    voice_id TEXT,
    audition_job_id TEXT,
    origin TEXT,
    last_seen_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_enrollment_expiry ON enrollment_sessions(expires_at);

CREATE TABLE IF NOT EXISTS enrollment_uploads (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES enrollment_sessions(id) ON DELETE CASCADE,
    original_path TEXT NOT NULL,
    media_type TEXT,
    format_name TEXT,
    duration_ms INTEGER NOT NULL,
    sample_rate INTEGER NOT NULL,
    channels INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    checks_json TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_session ON enrollment_uploads(session_id);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    voice_id TEXT REFERENCES voices(id) ON DELETE CASCADE,
    reference_version_id TEXT,
    session_id TEXT,
    preset TEXT NOT NULL,
    input_json TEXT NOT NULL,
    controls_json TEXT NOT NULL,
    seed INTEGER NOT NULL,
    output_artifact_id TEXT,
    master_artifact_id TEXT,
    progress TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    worker_id TEXT,
    error_code TEXT,
    error_message TEXT,
    metrics_json TEXT,
    created_at INTEGER NOT NULL,
    started_at INTEGER,
    heartbeat_at INTEGER,
    lease_expires_at INTEGER,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_voice ON jobs(voice_id);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    preset TEXT NOT NULL,
    path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    format TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    sample_rate INTEGER NOT NULL,
    channels INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_artifacts_job ON artifacts(job_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_expiry ON artifacts(expires_at);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS worker_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner TEXT,
    status TEXT NOT NULL DEFAULT 'stopped',
    detail_json TEXT NOT NULL DEFAULT '{}',
    started_at INTEGER,
    heartbeat_at INTEGER,
    lease_expires_at INTEGER,
    current_job_id TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    action TEXT NOT NULL,
    subject TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

INSERT OR IGNORE INTO worker_lock (id, status) VALUES (1, 'stopped');
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA secure_delete = ON")
        return conn

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = NORMAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {version} is newer than this build ({SCHEMA_VERSION})"
                )
            conn.executescript(SCHEMA)
            if version == 1:
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
                }
                if "reference_version_id" not in columns:
                    conn.execute("ALTER TABLE jobs ADD COLUMN reference_version_id TEXT")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        finally:
            conn.close()

    def checkpoint(self) -> None:
        """Remove WAL frames that may still contain previously deleted rows."""
        conn = self.connect()
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
