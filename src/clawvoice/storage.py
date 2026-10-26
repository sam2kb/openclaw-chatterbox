"""Private runtime storage, capacity bounds, and cleanup."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any

from .config import Settings
from .errors import UnavailableError, ValidationFailure
from .store import Store
from .util import is_inside, now_ms, safe_storage_path


class StorageFull(ValidationFailure):
    code = "storage_full"
    http_status = 507


class StorageManager:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings = settings
        self.store = store

    def usage_bytes(self) -> int:
        total = 0
        for root in (
            self.settings.voices_dir,
            self.settings.artifacts_dir,
            self.settings.enrollment_dir,
        ):
            if not root.exists():
                continue
            for base, _dirs, files in os.walk(root):
                for name in files:
                    try:
                        path = Path(base) / name
                        if path.is_symlink():
                            continue
                        total += path.stat().st_size
                    except OSError:
                        continue
        return total

    def ensure_capacity(self, incoming: int) -> None:
        current = self.usage_bytes()
        if current + max(0, incoming) > self.settings.max_data_bytes:
            raise StorageFull(
                "private voice storage is full; remove old generated audio or raise the configured limit",
                details={
                    "used_bytes": current,
                    "incoming_bytes": max(0, incoming),
                    "max_data_bytes": self.settings.max_data_bytes,
                },
            )
        try:
            free = shutil.disk_usage(self.settings.data_dir).free
        except OSError as exc:
            raise UnavailableError("private storage is unavailable") from exc
        if free < max(64 * 1024 * 1024, incoming * 2):
            raise StorageFull(
                "the filesystem does not have enough free space for this operation",
                details={"free_bytes": free, "incoming_bytes": max(0, incoming)},
            )

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.settings.data_dir))
        except ValueError as exc:
            raise ValidationFailure("internal path is outside private storage") from exc

    def resolve(self, relative_path: str) -> Path:
        try:
            path = safe_storage_path(self.settings.data_dir, relative_path)
        except (OSError, ValueError) as exc:
            raise ValidationFailure("artifact path is outside private storage") from exc
        if not is_inside(self.settings.data_dir, path):
            raise ValidationFailure("artifact path is outside private storage")
        return path

    def remove_relative(self, relative_path: str) -> None:
        try:
            path = self.resolve(relative_path)
        except (ValueError, ValidationFailure):
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        # Remove empty attempt/job directories under the artifact root, but
        # never the root itself or anything outside it.
        parent = path.parent
        while parent != self.settings.artifacts_dir:
            try:
                parent.relative_to(self.settings.artifacts_dir)
            except ValueError:
                break
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def cleanup_expired_artifacts(self) -> int:
        paths = self.store.expire_artifacts()
        for path in paths:
            self.remove_relative(path)
        cutoff = now_ms() - self.settings.artifact_retention_days * 86_400_000
        self.store.redact_terminal_job_texts(cutoff_ms=cutoff)
        return len(paths)

    def cleanup_expired_enrollments(self) -> int:
        paths = self.store.purge_expired_enrollment_sessions()
        for path in paths:
            self.remove_relative(path)
        return len(paths)

    def cleanup_orphans(self, *, grace_seconds: int = 3600) -> int:
        """Remove old files/dirs with no live DB reference.

        Active/queued jobs are never touched. Files written by a crashed or
        stale attempt but absent from the artifacts table are removed after a
        grace period so a newer attempt is not disturbed.
        """
        cutoff = time.time() - grace_seconds
        removed = 0
        if not self.settings.artifacts_dir.exists():
            return 0
        with self.store.db.connect() as conn:
            for job_dir in self.settings.artifacts_dir.iterdir():
                if not job_dir.is_dir() or job_dir.is_symlink():
                    continue
                if not job_dir.name.startswith("job_"):
                    continue
                job = conn.execute(
                    "SELECT status FROM jobs WHERE id=?", (job_dir.name,)
                ).fetchone()
                if job is not None and job["status"] in ("queued", "running"):
                    continue
                referenced = {
                    row["path"]
                    for row in conn.execute(
                        "SELECT path FROM artifacts WHERE job_id=?", (job_dir.name,)
                    )
                }
                if job is None:
                    try:
                        if job_dir.stat().st_mtime < cutoff:
                            shutil.rmtree(job_dir)
                            removed += 1
                            continue
                    except OSError:
                        pass
                for base, dirs, files in os.walk(job_dir, topdown=False, followlinks=False):
                    for name in files:
                        path = Path(base) / name
                        if path.is_symlink():
                            continue
                        try:
                            rel = str(
                                path.resolve().relative_to(self.settings.data_dir.resolve())
                            )
                        except ValueError:
                            continue
                        if rel in referenced:
                            continue
                        try:
                            if path.stat().st_mtime < cutoff:
                                path.unlink()
                                removed += 1
                        except OSError:
                            pass
                    for name in dirs:
                        path = Path(base) / name
                        if path.is_symlink():
                            continue
                        try:
                            path.rmdir()
                        except OSError:
                            pass
        if self.settings.voices_dir.exists():
            with self.store.db.connect() as conn:
                for voice_dir in self.settings.voices_dir.iterdir():
                    if not voice_dir.is_dir() or voice_dir.is_symlink():
                        continue
                    if not voice_dir.name.startswith("voc_"):
                        continue
                    exists = conn.execute(
                        "SELECT 1 FROM voices WHERE id=?", (voice_dir.name,)
                    ).fetchone()
                    if exists is None:
                        try:
                            if voice_dir.stat().st_mtime < cutoff:
                                shutil.rmtree(voice_dir)
                                removed += 1
                        except OSError:
                            pass
        return removed

    def cleanup_temp(self, *, older_than_seconds: int = 3600) -> int:
        cutoff = time.time() - older_than_seconds
        removed = 0
        if not self.settings.tmp_dir.exists():
            return 0
        for entry in self.settings.tmp_dir.iterdir():
            try:
                if entry.is_symlink() or entry.is_file():
                    if entry.stat().st_mtime < cutoff:
                        entry.unlink()
                        removed += 1
            except OSError:
                continue
        return removed

    def cleanup_empty_dirs(self) -> int:
        removed = 0
        for root in (self.settings.artifacts_dir, self.settings.enrollment_dir):
            if not root.exists():
                continue
            for base, dirs, files in os.walk(root, topdown=False):
                if not dirs and not files:
                    try:
                        Path(base).rmdir()
                        removed += 1
                    except OSError:
                        pass
        return removed

    def backup_inventory(self) -> dict[str, Any]:
        return {
            "data_dir": str(self.settings.data_dir),
            "usage_bytes": self.usage_bytes(),
            "max_data_bytes": self.settings.max_data_bytes,
            "sqlite": str(self.settings.db_path),
            "includes": ["sqlite metadata/jobs", "original recordings", "normalized references", "artifacts"],
            "excludes": ["model weights", "service token", "generated audio after retention"],
        }
