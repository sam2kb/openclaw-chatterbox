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
from .util import is_inside, safe_storage_path


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
        except ValueError:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        # Remove now-empty generated-artifact job directories only.
        parent = path.parent
        if parent.is_dir() and parent.name and parent.parent == self.settings.artifacts_dir:
            try:
                parent.rmdir()
            except OSError:
                pass

    def cleanup_expired_artifacts(self) -> int:
        paths = self.store.expire_artifacts()
        for path in paths:
            self.remove_relative(path)
        return len(paths)

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
