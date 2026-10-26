"""Domain persistence and the transactional job queue.

Public API responses must be built from `public_*` helpers; raw rows contain
private filesystem paths and stored text and must not cross the API boundary.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from .db import Database
from .errors import ConflictError, NotFoundError, RateLimited
from .util import iso_from_ms, new_id, now_ms

TERMINAL_STATUSES = ("succeeded", "failed", "canceled")


def _json(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class Store:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ voices
    def create_voice(
        self,
        *,
        voice_id: str,
        name: str,
        backend: str,
        model_revision: str,
        authorized: bool,
        metadata: dict[str, Any],
        reference: dict[str, Any],
        session_id: str | None,
        upload_id: str | None,
        audition: dict[str, Any] | None,
        max_queue_depth: int,
        max_attempts: int,
        now: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        now = now or now_ms()
        with self.db.transaction(immediate=True) as conn:
            if session_id is not None:
                session = conn.execute(
                    "SELECT * FROM enrollment_sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if session is None:
                    raise NotFoundError("enrollment session not found")
                if session["status"] != "open":
                    raise ConflictError("enrollment session is no longer open")
                if session["expires_at"] <= now:
                    conn.execute(
                        "UPDATE enrollment_sessions SET status='expired' WHERE id=?",
                        (session_id,),
                    )
                    raise ConflictError("enrollment session has expired")
                if upload_id and session["upload_id"] != upload_id:
                    raise ConflictError("upload does not belong to this enrollment session")
            if audition is not None:
                self._check_queue(conn, max_queue_depth)
            conn.execute(
                """
                INSERT INTO voices (
                    id, name, status, backend, model_revision, reference_version_id,
                    authorized, authorized_at, metadata_json, created_at, updated_at
                ) VALUES (?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    voice_id,
                    name,
                    backend,
                    model_revision,
                    reference["id"],
                    int(authorized),
                    now if authorized else None,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO reference_versions (
                    id, voice_id, original_path, normalized_path, original_sha256,
                    normalized_sha256, original_media_type, original_format,
                    source_duration_ms, source_sample_rate, source_channels,
                    selected_start_ms, selected_end_ms, reference_duration_ms,
                    reference_sample_rate, preparation_json, checks_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reference["id"],
                    voice_id,
                    reference["original_path"],
                    reference["normalized_path"],
                    reference["original_sha256"],
                    reference["normalized_sha256"],
                    reference.get("original_media_type"),
                    reference.get("original_format"),
                    reference["source_duration_ms"],
                    reference["source_sample_rate"],
                    reference["source_channels"],
                    reference["selected_start_ms"],
                    reference["selected_end_ms"],
                    reference["reference_duration_ms"],
                    reference["reference_sample_rate"],
                    json.dumps(reference["preparation"], ensure_ascii=False, sort_keys=True),
                    json.dumps(reference["checks"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            job: dict[str, Any] | None = None
            if audition is not None:
                job = self._insert_job(
                    conn,
                    kind="audition",
                    voice_id=voice_id,
                    reference_version_id=reference["id"],
                    session_id=session_id,
                    preset=audition.get("preset", "master"),
                    input_payload=audition["input"],
                    controls=audition.get("controls", {}),
                    seed=audition["seed"],
                    max_attempts=max_attempts,
                    now=now,
                )
            if session_id is not None:
                conn.execute(
                    """
                    UPDATE enrollment_sessions
                    SET status='completed', consumed_at=?, completed_at=?, voice_id=?,
                        audition_job_id=?, upload_id=NULL, last_seen_at=?
                    WHERE id=?
                    """,
                    (now, now, voice_id, job["id"] if job else None, now, session_id),
                )
                if upload_id:
                    conn.execute("DELETE FROM enrollment_uploads WHERE id=?", (upload_id,))
            conn.execute(
                "INSERT INTO audit_events (created_at, action, subject, detail_json) "
                "VALUES (?, 'voice.created', ?, ?)",
                (now, voice_id, json.dumps({"backend": backend, "session": bool(session_id)})),
            )
            voice_row = conn.execute("SELECT * FROM voices WHERE id=?", (voice_id,)).fetchone()
            ref_row = conn.execute(
                "SELECT * FROM reference_versions WHERE id=?", (reference["id"],)
            ).fetchone()
        return self.public_voice(voice_row, ref_row), (self.public_job(job) if job else None)

    def replace_voice_reference(
        self,
        *,
        voice_id: str,
        reference: dict[str, Any],
        metadata: dict[str, Any],
        audition: dict[str, Any],
        max_queue_depth: int,
        max_attempts: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Add a new reference version and make it current; keep old versions."""
        now = now_ms()
        with self.db.transaction(immediate=True) as conn:
            voice = conn.execute(
                "SELECT * FROM voices WHERE id=? AND status='active'", (voice_id,)
            ).fetchone()
            if voice is None:
                raise NotFoundError("voice not found")
            self._check_queue(conn, max_queue_depth)
            conn.execute(
                """
                INSERT INTO reference_versions (
                    id, voice_id, original_path, normalized_path, original_sha256,
                    normalized_sha256, original_media_type, original_format,
                    source_duration_ms, source_sample_rate, source_channels,
                    selected_start_ms, selected_end_ms, reference_duration_ms,
                    reference_sample_rate, preparation_json, checks_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reference["id"],
                    voice_id,
                    reference["original_path"],
                    reference["normalized_path"],
                    reference["original_sha256"],
                    reference["normalized_sha256"],
                    reference.get("original_media_type"),
                    reference.get("original_format"),
                    reference["source_duration_ms"],
                    reference["source_sample_rate"],
                    reference["source_channels"],
                    reference["selected_start_ms"],
                    reference["selected_end_ms"],
                    reference["reference_duration_ms"],
                    reference["reference_sample_rate"],
                    json.dumps(reference["preparation"], ensure_ascii=False, sort_keys=True),
                    json.dumps(reference["checks"], ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            previous_metadata = _json(voice["metadata_json"], {})
            revision_count = conn.execute(
                "SELECT COUNT(*) FROM reference_versions WHERE voice_id=?", (voice_id,)
            ).fetchone()[0]
            merged_metadata = {
                **previous_metadata,
                **metadata,
                "revision_count": revision_count + 1,
                "reference_revised_at": iso_from_ms(now),
            }
            conn.execute(
                "UPDATE voices SET reference_version_id=?, metadata_json=?, updated_at=? WHERE id=?",
                (
                    reference["id"],
                    json.dumps(merged_metadata, ensure_ascii=False, sort_keys=True),
                    now,
                    voice_id,
                ),
            )
            job = self._insert_job(
                conn,
                kind="audition",
                voice_id=voice_id,
                reference_version_id=reference["id"],
                session_id=None,
                preset=audition.get("preset", "master"),
                input_payload=audition["input"],
                controls=audition.get("controls", {}),
                seed=audition["seed"],
                max_attempts=max_attempts,
                now=now,
            )
            conn.execute(
                "INSERT INTO audit_events (created_at, action, subject, detail_json) "
                "VALUES (?, 'voice.reference_added', ?, ?)",
                (now, voice_id, json.dumps({"reference_id": reference["id"]})),
            )
            voice_row = conn.execute("SELECT * FROM voices WHERE id=?", (voice_id,)).fetchone()
            ref_row = conn.execute(
                "SELECT * FROM reference_versions WHERE id=?", (reference["id"],)
            ).fetchone()
            view = self.public_voice(voice_row, ref_row)
        return view, self.public_job(job)

    def list_voices(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM voices WHERE status='active' ORDER BY created_at, id"
            ).fetchall()
            result = []
            for row in rows:
                ref = conn.execute(
                    "SELECT * FROM reference_versions WHERE id=?", (row["reference_version_id"],)
                ).fetchone()
                result.append(self.public_voice(row, ref))
            default = self.get_setting("default_voice_id")
        for voice in result:
            voice["is_default"] = voice["id"] == default
        return result

    def get_voice(self, voice_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM voices WHERE id=? AND status='active'", (voice_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("voice not found")
            ref = conn.execute(
                "SELECT * FROM reference_versions WHERE id=?", (row["reference_version_id"],)
            ).fetchone()
            voice = self.public_voice(row, ref)
            voice["is_default"] = self.get_setting("default_voice_id") == voice_id
            audition = conn.execute(
                "SELECT id, status, error_code, error_message, output_artifact_id, created_at, "
                "finished_at FROM jobs WHERE voice_id=? AND kind='audition' "
                "ORDER BY created_at DESC, id DESC LIMIT 1",
                (voice_id,),
            ).fetchone()
            voice["last_audition"] = (
                {
                    "id": audition["id"],
                    "status": audition["status"],
                    "artifact_id": audition["output_artifact_id"],
                    "error": (
                        {"code": audition["error_code"], "message": audition["error_message"]}
                        if audition["error_code"]
                        else None
                    ),
                    "created_at": iso_from_ms(audition["created_at"]),
                    "finished_at": iso_from_ms(audition["finished_at"]),
                }
                if audition
                else None
            )
            versions = conn.execute(
                "SELECT id, normalized_sha256, original_sha256, selected_start_ms, "
                "selected_end_ms, reference_duration_ms, reference_sample_rate, created_at "
                "FROM reference_versions WHERE voice_id=? ORDER BY created_at, id",
                (voice_id,),
            ).fetchall()
            voice["reference_versions"] = [
                {
                    "id": item["id"],
                    "current": item["id"] == voice["reference"]["id"],
                    "sha256": item["normalized_sha256"],
                    "original_sha256": item["original_sha256"],
                    "selected_start_ms": item["selected_start_ms"],
                    "selected_end_ms": item["selected_end_ms"],
                    "duration_ms": item["reference_duration_ms"],
                    "sample_rate": item["reference_sample_rate"],
                    "created_at": iso_from_ms(item["created_at"]),
                }
                for item in versions
            ]
            return voice

    def get_voice_row(self, voice_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM voices WHERE id=? AND status='active'", (voice_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("voice not found")
            return dict(row)

    def get_reference_for_voice(self, voice_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT rv.* FROM reference_versions rv
                JOIN voices v ON v.reference_version_id = rv.id
                WHERE v.id=? AND v.status='active'
                """,
                (voice_id,),
            ).fetchone()
            if row is None:
                raise NotFoundError("voice reference not found")
            return dict(row)

    def get_reference_by_id(self, voice_id: str, reference_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT rv.* FROM reference_versions rv
                JOIN voices v ON v.id = rv.voice_id
                WHERE rv.id=? AND rv.voice_id=? AND v.status='active'
                """,
                (reference_id, voice_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("voice reference version not found")
            return dict(row)

    def rename_voice(self, voice_id: str, name: str) -> dict[str, Any]:
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE voices SET name=?, updated_at=? WHERE id=? AND status='active'",
                (name, now_ms(), voice_id),
            )
            if cur.rowcount == 0:
                raise NotFoundError("voice not found")
        return self.get_voice(voice_id)

    def select_voice(self, voice_id: str) -> dict[str, Any]:
        self.get_voice(voice_id)
        self.set_setting("default_voice_id", voice_id)
        voice = self.get_voice(voice_id)
        voice["is_default"] = True
        return voice

    def delete_voice(self, voice_id: str) -> list[str]:
        """Delete profile metadata and return private files to unlink.

        Generated artifacts and reference versions belonging to the voice are
        removed too; this is the explicit deletion path, not uninstall.
        """
        with self.db.transaction(immediate=True) as conn:
            voice = conn.execute(
                "SELECT * FROM voices WHERE id=? AND status='active'", (voice_id,)
            ).fetchone()
            if voice is None:
                raise NotFoundError("voice not found")
            active = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE voice_id=? AND status IN ('queued','running')",
                (voice_id,),
            ).fetchone()[0]
            if active:
                raise ConflictError("cannot delete a voice while it has queued or running jobs")
            paths: list[str] = []
            for row in conn.execute(
                "SELECT path FROM artifacts WHERE job_id IN (SELECT id FROM jobs WHERE voice_id=?)",
                (voice_id,),
            ):
                paths.append(row["path"])
            for row in conn.execute(
                "SELECT original_path, normalized_path FROM reference_versions WHERE voice_id=?",
                (voice_id,),
            ):
                paths.extend([row["original_path"], row["normalized_path"]])
            for row in conn.execute(
                "SELECT eu.original_path FROM enrollment_uploads eu "
                "JOIN enrollment_sessions es ON es.upload_id=eu.id WHERE es.voice_id=?",
                (voice_id,),
            ):
                paths.append(row["original_path"])
            if self.get_setting("default_voice_id") == voice_id:
                conn.execute("DELETE FROM settings WHERE key='default_voice_id'")
            conn.execute(
                "UPDATE enrollment_sessions SET voice_id=NULL, audition_job_id=NULL WHERE voice_id=?",
                (voice_id,),
            )
            conn.execute("DELETE FROM voices WHERE id=?", (voice_id,))
            conn.execute(
                "INSERT INTO audit_events (created_at, action, subject, detail_json) "
                "VALUES (?, 'voice.deleted', ?, '{}')",
                (now_ms(), voice_id),
            )
        return sorted(set(paths))

    def set_voice_preview(self, voice_id: str, artifact_id: str | None) -> None:
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE voices SET preview_artifact_id=?, updated_at=? WHERE id=?",
                (artifact_id, now_ms(), voice_id),
            )

    # ------------------------------------------------------------ enrollments
    def create_enrollment_session(
        self, *, name: str, token_hash: str, ttl_seconds: int, origin: str | None
    ) -> dict[str, Any]:
        now = now_ms()
        session_id = new_id("enr")
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO enrollment_sessions
                    (id, name, token_hash, status, created_at, expires_at, origin, last_seen_at)
                VALUES (?, ?, ?, 'open', ?, ?, ?, ?)
                """,
                (session_id, name, token_hash, now, now + ttl_seconds * 1000, origin, now),
            )
        return self.get_enrollment_session(session_id)

    def get_enrollment_session(self, session_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM enrollment_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("enrollment session not found")
            return dict(row)

    def touch_enrollment(self, session_id: str, now: int | None = None) -> None:
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE enrollment_sessions SET last_seen_at=? WHERE id=?",
                (now or now_ms(), session_id),
            )

    def expire_enrollment_sessions(self, now: int | None = None) -> int:
        now = now or now_ms()
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE enrollment_sessions SET status='expired' "
                "WHERE status='open' AND expires_at <= ?",
                (now,),
            )
            return cur.rowcount

    def purge_expired_enrollment_sessions(self, now: int | None = None) -> list[str]:
        """Delete expired enrollment metadata and return staged files to unlink.

        The service may still address a completed session's audition job and
        artifact by ID, but the expired enrollment credential and staged
        recording are removed. Voice originals live under voices/, not here.
        """
        now = now or now_ms()
        paths: list[str] = []
        with self.db.transaction(immediate=True) as conn:
            sessions = conn.execute(
                "SELECT id FROM enrollment_sessions WHERE expires_at <= ?", (now,)
            ).fetchall()
            for session in sessions:
                for row in conn.execute(
                    "SELECT original_path FROM enrollment_uploads WHERE session_id=?",
                    (session["id"],),
                ).fetchall():
                    paths.append(row["original_path"])
                conn.execute("DELETE FROM enrollment_sessions WHERE id=?", (session["id"],))
            if sessions:
                conn.execute(
                    "INSERT INTO audit_events (created_at, action, subject, detail_json) "
                    "VALUES (?, 'enrollment.expired_purged', NULL, ?)",
                    (now, json.dumps({"count": len(sessions)})),
                )
        return sorted(set(paths))

    def replace_enrollment_upload(
        self,
        *,
        upload_id: str,
        session_id: str,
        original_path: str,
        media_type: str | None,
        format_name: str | None,
        duration_ms: int,
        sample_rate: int,
        channels: int,
        size_bytes: int,
        sha256: str,
        checks: dict[str, Any],
        now: int | None = None,
    ) -> list[str]:
        """Stage one upload, returning the previous path (if any) for cleanup."""
        now = now or now_ms()
        old_paths: list[str] = []
        with self.db.transaction(immediate=True) as conn:
            session = conn.execute(
                "SELECT * FROM enrollment_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session is None:
                raise NotFoundError("enrollment session not found")
            if session["status"] != "open" or session["expires_at"] <= now:
                raise ConflictError("enrollment session is not open")
            if session["upload_id"]:
                old = conn.execute(
                    "SELECT original_path FROM enrollment_uploads WHERE id=?",
                    (session["upload_id"],),
                ).fetchone()
                if old:
                    old_paths.append(old["original_path"])
                conn.execute("DELETE FROM enrollment_uploads WHERE id=?", (session["upload_id"],))
            conn.execute(
                """
                INSERT INTO enrollment_uploads (
                    id, session_id, original_path, media_type, format_name, duration_ms,
                    sample_rate, channels, size_bytes, sha256, checks_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    upload_id,
                    session_id,
                    original_path,
                    media_type,
                    format_name,
                    duration_ms,
                    sample_rate,
                    channels,
                    size_bytes,
                    sha256,
                    json.dumps(checks, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            conn.execute(
                "UPDATE enrollment_sessions SET upload_id=?, last_seen_at=? WHERE id=?",
                (upload_id, now, session_id),
            )
        return old_paths

    def get_enrollment_upload(self, session_id: str, upload_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT * FROM enrollment_uploads WHERE id=? AND session_id=?",
                (upload_id, session_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("upload not found")
            return dict(row)

    def consume_enrollment_for_voice(self, session_id: str, now: int | None = None) -> None:
        """Mark a session consumed without creating a voice (unused by API)."""
        now = now or now_ms()
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE enrollment_sessions SET status='completed', consumed_at=?, completed_at=? "
                "WHERE id=? AND status='open'",
                (now, now, session_id),
            )

    def enrollment_artifacts(self, session_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                """
                SELECT a.* FROM artifacts a
                JOIN jobs j ON j.id = a.job_id
                JOIN enrollment_sessions es ON es.audition_job_id = j.id
                WHERE es.id=?
                ORDER BY a.created_at
                """,
                (session_id,),
            ).fetchall()
            return [self.public_artifact(dict(r)) for r in rows]

    def enrollment_audition_job(self, session_id: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            session = conn.execute(
                "SELECT audition_job_id FROM enrollment_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if session is None or not session["audition_job_id"]:
                return None
            job = conn.execute(
                "SELECT * FROM jobs WHERE id=?", (session["audition_job_id"],)
            ).fetchone()
            if job is None:
                return None
            view = self.public_job(dict(job))
            view["artifacts"] = [
                self.public_artifact(dict(a))
                for a in conn.execute(
                    "SELECT * FROM artifacts WHERE job_id=? ORDER BY created_at", (job["id"],)
                ).fetchall()
            ]
            return view

    # ------------------------------------------------------------------ jobs
    def _check_queue(self, conn: sqlite3.Connection, max_queue_depth: int) -> None:
        depth = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"
        ).fetchone()[0]
        if depth >= max_queue_depth:
            raise RateLimited(
                "the synthesis queue is full; wait for a job to finish and try again",
                details={"queue_depth": depth, "max_queue_depth": max_queue_depth},
                retryable=True,
            )

    def _insert_job(
        self,
        conn: sqlite3.Connection,
        *,
        kind: str,
        voice_id: str | None,
        reference_version_id: str | None,
        session_id: str | None,
        preset: str,
        input_payload: dict[str, Any],
        controls: dict[str, Any],
        seed: int,
        max_attempts: int,
        now: int,
    ) -> dict[str, Any]:
        job_id = new_id("job")
        conn.execute(
            """
            INSERT INTO jobs (
                id, kind, status, voice_id, reference_version_id, session_id, preset,
                input_json, controls_json, seed, progress, max_attempts, created_at
            ) VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?)
            """,
            (
                job_id,
                kind,
                voice_id,
                reference_version_id,
                session_id,
                preset,
                json.dumps(input_payload, ensure_ascii=False, sort_keys=True),
                json.dumps(controls, ensure_ascii=False, sort_keys=True),
                seed,
                max_attempts,
                now,
            ),
        )
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row)

    def create_synthesis_job(
        self,
        *,
        voice_id: str,
        preset: str,
        input_payload: dict[str, Any],
        controls: dict[str, Any],
        seed: int,
        max_queue_depth: int,
        max_attempts: int,
    ) -> dict[str, Any]:
        now = now_ms()
        with self.db.transaction(immediate=True) as conn:
            voice = conn.execute(
                "SELECT * FROM voices WHERE id=? AND status='active'", (voice_id,)
            ).fetchone()
            if voice is None:
                raise NotFoundError("voice not found")
            self._check_queue(conn, max_queue_depth)
            job = self._insert_job(
                conn,
                kind="synthesis",
                voice_id=voice_id,
                reference_version_id=voice["reference_version_id"],
                session_id=None,
                preset=preset,
                input_payload=input_payload,
                controls=controls,
                seed=seed,
                max_attempts=max_attempts,
                now=now,
            )
            conn.execute(
                "INSERT INTO audit_events (created_at, action, subject, detail_json) "
                "VALUES (?, 'job.created', ?, ?)",
                (now, job["id"], json.dumps({"kind": "synthesis", "preset": preset})),
            )
        return self.public_job(job)

    def create_audition_job(
        self,
        *,
        voice_id: str,
        input_payload: dict[str, Any],
        controls: dict[str, Any],
        seed: int,
        max_queue_depth: int,
        max_attempts: int,
    ) -> dict[str, Any]:
        now = now_ms()
        with self.db.transaction(immediate=True) as conn:
            voice = conn.execute(
                "SELECT * FROM voices WHERE id=? AND status='active'", (voice_id,)
            ).fetchone()
            if voice is None:
                raise NotFoundError("voice not found")
            self._check_queue(conn, max_queue_depth)
            job = self._insert_job(
                conn,
                kind="audition",
                voice_id=voice_id,
                reference_version_id=voice["reference_version_id"],
                session_id=None,
                preset="master",
                input_payload=input_payload,
                controls=controls,
                seed=seed,
                max_attempts=max_attempts,
                now=now,
            )
        return self.public_job(job)

    def get_job_row(self, job_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("job not found")
            return dict(row)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("job not found")
            view = self.public_job(dict(row))
            view["artifacts"] = [
                self.public_artifact(dict(a))
                for a in conn.execute(
                    "SELECT * FROM artifacts WHERE job_id=? ORDER BY created_at", (job_id,)
                ).fetchall()
            ]
            return view

    def claim_job(
        self, *, worker_id: str, lease_seconds: int, now: int | None = None
    ) -> dict[str, Any] | None:
        now = now or now_ms()
        with self.db.transaction(immediate=True) as conn:
            lock = conn.execute("SELECT owner, lease_expires_at FROM worker_lock WHERE id=1").fetchone()
            if (
                lock is None
                or lock["owner"] != worker_id
                or not lock["lease_expires_at"]
                or lock["lease_expires_at"] < now
            ):
                return None
            row = conn.execute(
                "SELECT * FROM jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            cur = conn.execute(
                """
                UPDATE jobs SET status='running', worker_id=?, progress='starting',
                    attempts=attempts+1, started_at=?, heartbeat_at=?, lease_expires_at=?,
                    error_code=NULL, error_message=NULL
                WHERE id=? AND status='queued'
                """,
                (
                    worker_id,
                    now,
                    now,
                    now + lease_seconds * 1000,
                    row["id"],
                ),
            )
            if cur.rowcount != 1:
                return None
            conn.execute(
                "UPDATE worker_lock SET current_job_id=?, heartbeat_at=?, lease_expires_at=? "
                "WHERE owner=?",
                (row["id"], now, now + lease_seconds * 1000, worker_id),
            )
            claimed = conn.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone()
            return dict(claimed)

    def recover_stale_jobs(self, *, lease_seconds: int, now: int | None = None) -> list[str]:
        """Requeue interrupted jobs or fail them after bounded attempts."""
        now = now or now_ms()
        changed: list[str] = []
        with self.db.transaction(immediate=True) as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status='running' AND "
                "(lease_expires_at IS NULL OR lease_expires_at < ?)",
                (now,),
            ).fetchall()
            for row in rows:
                if row["cancel_requested"]:
                    conn.execute(
                        "UPDATE jobs SET status='canceled', finished_at=?, progress='canceled', "
                        "error_code='canceled', error_message='Synthesis was canceled' WHERE id=?",
                        (now, row["id"]),
                    )
                elif row["attempts"] >= row["max_attempts"]:
                    conn.execute(
                        "UPDATE jobs SET status='failed', finished_at=?, progress='failed', "
                        "error_code='worker_interrupted', "
                        "error_message='The inference worker stopped repeatedly before the job completed' "
                        "WHERE id=?",
                        (now, row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE jobs SET status='queued', worker_id=NULL, progress='recovered', "
                        "lease_expires_at=NULL, error_code='worker_interrupted', "
                        "error_message='The job was interrupted and will be retried' WHERE id=?",
                        (row["id"],),
                    )
                changed.append(row["id"])
                conn.execute(
                    "INSERT INTO audit_events (created_at, action, subject, detail_json) "
                    "VALUES (?, 'job.recovered', ?, '{}')",
                    (now, row["id"]),
                )
        return changed

    def heartbeat_job(
        self, job_id: str, worker_id: str, *, lease_seconds: int, now: int | None = None
    ) -> bool:
        now = now or now_ms()
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE jobs SET heartbeat_at=?, lease_expires_at=? "
                "WHERE id=? AND status='running' AND worker_id=?",
                (now, now + lease_seconds * 1000, job_id, worker_id),
            )
            conn.execute(
                "UPDATE worker_lock SET current_job_id=?, heartbeat_at=?, lease_expires_at=? "
                "WHERE owner=?",
                (job_id, now, now + lease_seconds * 1000, worker_id),
            )
            return cur.rowcount == 1

    def update_progress(self, job_id: str, worker_id: str, progress: str) -> None:
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE jobs SET progress=? WHERE id=? AND status='running' AND worker_id=?",
                (progress[:120], job_id, worker_id),
            )

    def is_cancel_requested(self, job_id: str) -> bool:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            return bool(row and row["cancel_requested"])

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        now = now_ms()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise NotFoundError("job not found")
            if row["status"] in TERMINAL_STATUSES:
                return self.get_job(job_id)
            if row["status"] == "queued":
                conn.execute(
                    "UPDATE jobs SET status='canceled', cancel_requested=1, finished_at=?, "
                    "progress='canceled', error_code='canceled', "
                    "error_message='Synthesis was canceled' WHERE id=?",
                    (now, job_id),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET cancel_requested=1, progress='cancel_pending' WHERE id=?",
                    (job_id,),
                )
            conn.execute(
                "INSERT INTO audit_events (created_at, action, subject, detail_json) "
                "VALUES (?, 'job.cancel_requested', ?, '{}')",
                (now, job_id),
            )
        return self.get_job(job_id)

    def mark_canceled(self, job_id: str, worker_id: str) -> None:
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE jobs SET status='canceled', finished_at=?, progress='canceled', "
                "error_code='canceled', error_message='Synthesis was canceled' "
                "WHERE id=? AND status='running' AND worker_id=?",
                (now_ms(), job_id, worker_id),
            )

    def fail_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        code: str,
        message: str,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        now = now_ms()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id=? AND status='running' AND worker_id=?",
                (job_id, worker_id),
            ).fetchone()
            if row is None:
                return
            if row["cancel_requested"]:
                conn.execute(
                    "UPDATE jobs SET status='canceled', finished_at=?, progress='canceled', "
                    "error_code='canceled', error_message='Synthesis was canceled' WHERE id=?",
                    (now, job_id),
                )
                return
            conn.execute(
                "UPDATE jobs SET status='failed', finished_at=?, progress='failed', "
                "error_code=?, error_message=?, metrics_json=? WHERE id=?",
                (
                    now,
                    code[:80],
                    message[:500],
                    json.dumps(metrics or {}, sort_keys=True),
                    job_id,
                ),
            )

    def complete_job(
        self,
        *,
        job_id: str,
        worker_id: str,
        artifacts: Iterable[dict[str, Any]],
        output_artifact_id: str,
        master_artifact_id: str,
        metrics: dict[str, Any],
        progress: str = "assembling",
    ) -> str:
        """Atomically record artifacts and success. Returns a status string.

        `canceled` means cancellation won the race and any written files are
        now orphans; the caller must remove them.
        """
        now = now_ms()
        artifact_list = list(artifacts)
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE id=? AND status='running' AND worker_id=?",
                (job_id, worker_id),
            ).fetchone()
            if row is None:
                return "lost"
            if row["cancel_requested"]:
                conn.execute(
                    "UPDATE jobs SET status='canceled', finished_at=?, progress='canceled', "
                    "error_code='canceled', error_message='Synthesis was canceled' WHERE id=?",
                    (now, job_id),
                )
                return "canceled"
            for item in artifact_list:
                conn.execute(
                    """
                    INSERT INTO artifacts (
                        id, job_id, kind, preset, path, media_type, format, size_bytes,
                        duration_ms, sample_rate, channels, sha256, manifest_json,
                        created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["id"],
                        job_id,
                        item["kind"],
                        item["preset"],
                        item["path"],
                        item["media_type"],
                        item["format"],
                        item["size_bytes"],
                        item["duration_ms"],
                        item["sample_rate"],
                        item["channels"],
                        item["sha256"],
                        json.dumps(item["manifest"], ensure_ascii=False, sort_keys=True),
                        now,
                        item.get("expires_at"),
                    ),
                )
            conn.execute(
                """
                UPDATE jobs SET status='succeeded', finished_at=?, progress='succeeded',
                    output_artifact_id=?, master_artifact_id=?, metrics_json=?,
                    error_code=NULL, error_message=NULL
                WHERE id=? AND status='running' AND worker_id=?
                """,
                (
                    now,
                    output_artifact_id,
                    master_artifact_id,
                    json.dumps(metrics, sort_keys=True),
                    job_id,
                    worker_id,
                ),
            )
            if row["kind"] == "audition" and row["voice_id"]:
                conn.execute(
                    "UPDATE voices SET preview_artifact_id=?, updated_at=? WHERE id=?",
                    (output_artifact_id, now, row["voice_id"]),
                )
            conn.execute(
                "INSERT INTO audit_events (created_at, action, subject, detail_json) "
                "VALUES (?, 'job.succeeded', ?, ?)",
                (now, job_id, json.dumps({"preset": row["preset"]})),
            )
        return "succeeded"

    def discard_artifacts(self, job_id: str) -> None:
        """Only used when a terminal race means no artifact row was committed."""
        with self.db.connect() as conn:
            conn.execute("DELETE FROM artifacts WHERE job_id=?", (job_id,))

    def add_audition_session_job(
        self, *, session_id: str, job_id: str
    ) -> None:
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE enrollment_sessions SET audition_job_id=? WHERE id=?",
                (job_id, session_id),
            )

    # --------------------------------------------------------------- artifacts
    def public_artifact(self, row: dict[str, Any]) -> dict[str, Any]:
        manifest = _json(row.get("manifest_json"), {})
        return {
            "id": row["id"],
            "job_id": row["job_id"],
            "kind": row["kind"],
            "preset": row["preset"],
            "media_type": row["media_type"],
            "format": row["format"],
            "bytes": row["size_bytes"],
            "duration_ms": row["duration_ms"],
            "sample_rate": row["sample_rate"],
            "channels": row["channels"],
            "sha256": row["sha256"],
            "created_at": iso_from_ms(row["created_at"]),
            "expires_at": iso_from_ms(row.get("expires_at")),
            "manifest": manifest,
            "download_path": f"/api/v1/artifacts/{row['id']}/download",
        }

    def get_artifact_row(self, artifact_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
            if row is None:
                raise NotFoundError("artifact not found")
            return dict(row)

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        return self.public_artifact(self.get_artifact_row(artifact_id))

    def artifact_path_for_session(self, session_id: str, artifact_id: str) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT a.* FROM artifacts a
                JOIN enrollment_sessions es ON es.audition_job_id = a.job_id
                WHERE es.id=? AND a.id=?
                """,
                (session_id, artifact_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("audition artifact not found")
            return dict(row)

    def redact_terminal_job_texts(self, *, cutoff_ms: int) -> int:
        """Remove request text from terminal jobs after the audio retention window."""
        with self.db.transaction(immediate=True) as conn:
            cur = conn.execute(
                "UPDATE jobs SET input_json='{\"redacted\":true}' "
                "WHERE status IN ('succeeded','failed','canceled') "
                "AND finished_at IS NOT NULL AND finished_at < ? "
                "AND input_json != '{\"redacted\":true}'",
                (cutoff_ms,),
            )
            return cur.rowcount

    def expire_artifacts(self, *, now: int | None = None) -> list[str]:
        now = now or now_ms()
        paths: list[str] = []
        with self.db.transaction(immediate=True) as conn:
            rows = conn.execute(
                "SELECT id, path FROM artifacts WHERE expires_at IS NOT NULL AND expires_at < ?",
                (now,),
            ).fetchall()
            for row in rows:
                paths.append(row["path"])
                conn.execute(
                    "UPDATE voices SET preview_artifact_id=NULL WHERE preview_artifact_id=?",
                    (row["id"],),
                )
                conn.execute("DELETE FROM artifacts WHERE id=?", (row["id"],))
            if rows:
                conn.execute(
                    "INSERT INTO audit_events (created_at, action, subject, detail_json) "
                    "VALUES (?, 'artifacts.expired', NULL, ?)",
                    (now, json.dumps({"count": len(rows)})),
                )
        return paths

    # ---------------------------------------------------------------- settings
    def get_setting(self, key: str) -> str | None:
        with self.db.connect() as conn:
            row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now_ms()),
            )

    # ------------------------------------------------------------ worker lock
    def try_acquire_worker_lock(
        self,
        *,
        owner: str,
        lease_seconds: int,
        status: str = "starting",
        detail: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> bool:
        now = now or now_ms()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM worker_lock WHERE id=1").fetchone()
            if row["owner"] and row["owner"] != owner:
                if row["lease_expires_at"] and row["lease_expires_at"] >= now:
                    return False
            conn.execute(
                """
                UPDATE worker_lock SET owner=?, status=?, detail_json=?, started_at=?,
                    heartbeat_at=?, lease_expires_at=?, current_job_id=NULL
                WHERE id=1
                """,
                (
                    owner,
                    status,
                    json.dumps(detail or {}, sort_keys=True),
                    now,
                    now,
                    now + lease_seconds * 1000,
                ),
            )
            return True

    def renew_worker_lock(
        self,
        *,
        owner: str,
        lease_seconds: int,
        status: str | None = None,
        detail: dict[str, Any] | None = None,
        current_job_id: str | None = None,
    ) -> bool:
        now = now_ms()
        with self.db.transaction(immediate=True) as conn:
            row = conn.execute("SELECT * FROM worker_lock WHERE id=1").fetchone()
            if row["owner"] != owner:
                return False
            fields = ["heartbeat_at=?", "lease_expires_at=?"]
            args: list[Any] = [now, now + lease_seconds * 1000]
            if status is not None:
                fields.append("status=?")
                args.append(status)
            if detail is not None:
                fields.append("detail_json=?")
                args.append(json.dumps(detail, sort_keys=True))
            if current_job_id is not None:
                fields.append("current_job_id=?")
                args.append(current_job_id)
            args.extend([owner])
            conn.execute(f"UPDATE worker_lock SET {', '.join(fields)} WHERE owner=?", args)
            return True

    def release_worker_lock(self, owner: str, *, status: str = "stopped") -> None:
        now = now_ms()
        with self.db.transaction(immediate=True) as conn:
            conn.execute(
                "UPDATE worker_lock SET owner=NULL, status=?, current_job_id=NULL, "
                "heartbeat_at=?, lease_expires_at=NULL WHERE owner=?",
                (status, now, owner),
            )

    def worker_lock_view(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM worker_lock WHERE id=1").fetchone()
            data = dict(row)
            data["detail"] = _json(data.get("detail_json"), {})
            data.pop("detail_json", None)
            return data

    def queue_depth(self) -> int:
        with self.db.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')"
            ).fetchone()[0]

    def job_counts(self) -> dict[str, int]:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
            return {row["status"]: row["n"] for row in rows}

    # ---------------------------------------------------------------- public
    def public_voice(
        self, voice: sqlite3.Row | dict[str, Any] | None, reference: sqlite3.Row | dict[str, Any] | None
    ) -> dict[str, Any]:
        assert voice is not None
        voice = dict(voice)
        ref = dict(reference) if reference is not None else {}
        metadata = _json(voice.get("metadata_json"), {})
        checks = _json(ref.get("checks_json"), {})
        preparation = _json(ref.get("preparation_json"), {})
        return {
            "id": voice["id"],
            "name": voice["name"],
            "backend": voice["backend"],
            "model_revision": voice["model_revision"],
            "authorized": bool(voice["authorized"]),
            "authorized_at": iso_from_ms(voice.get("authorized_at")),
            "created_at": iso_from_ms(voice["created_at"]),
            "updated_at": iso_from_ms(voice["updated_at"]),
            "preview_artifact_id": voice.get("preview_artifact_id"),
            "metadata_version": metadata.get("schema_version", 1),
            "reference": {
                "id": ref.get("id"),
                "sha256": ref.get("normalized_sha256"),
                "original_sha256": ref.get("original_sha256"),
                "duration_ms": ref.get("reference_duration_ms"),
                "source_duration_ms": ref.get("source_duration_ms"),
                "selected_start_ms": ref.get("selected_start_ms"),
                "selected_end_ms": ref.get("selected_end_ms"),
                "sample_rate": ref.get("reference_sample_rate"),
                "source_sample_rate": ref.get("source_sample_rate"),
                "channels": ref.get("source_channels"),
                "preparation": preparation,
                "checks": checks,
            },
            "is_default": False,
        }

    def public_job(self, job: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        job = dict(job)
        metrics = _json(job.get("metrics_json"), {})
        status = job["status"]
        return {
            "id": job["id"],
            "kind": job["kind"],
            "status": status,
            "voice_id": job.get("voice_id"),
            "reference_version_id": job.get("reference_version_id"),
            "preset": job["preset"],
            "progress": job["progress"],
            "cancel_requested": bool(job["cancel_requested"]),
            "cancel_pending": bool(job["cancel_requested"]) and status == "running",
            "output_artifact_id": job.get("output_artifact_id"),
            "master_artifact_id": job.get("master_artifact_id"),
            "attempts": job["attempts"],
            "max_attempts": job["max_attempts"],
            "created_at": iso_from_ms(job["created_at"]),
            "started_at": iso_from_ms(job.get("started_at")),
            "finished_at": iso_from_ms(job.get("finished_at")),
            "error": (
                {"code": job["error_code"], "message": job["error_message"]}
                if job.get("error_code")
                else None
            ),
            "metrics": metrics,
        }
