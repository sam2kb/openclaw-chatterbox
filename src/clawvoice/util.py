"""Small, dependency-free helpers shared by the API, worker, and client."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

ID_ALPHABET = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_HEX_ID = re.compile(r"^[a-z]+_[0-9a-f]{24}$")


def now_ms() -> int:
    return int(time.time() * 1000)


def iso_from_ms(value: int | None) -> str | None:
    if value is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value / 1000))


def new_id(prefix: str) -> str:
    if not ID_ALPHABET.match(prefix):
        raise ValueError("invalid id prefix")
    return f"{prefix}_{secrets.token_hex(12)}"


def validate_id(value: str, *, prefixes: tuple[str, ...] | None = None) -> str:
    if not isinstance(value, str) or not _HEX_ID.fullmatch(value):
        raise ValueError("invalid opaque id")
    if prefixes and not any(value.startswith(f"{p}_") for p in prefixes):
        raise ValueError("unexpected opaque id type")
    return value


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_secret(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, *, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def redact(value: str, *, keep: int = 4) -> str:
    if not value:
        return "<empty>"
    if len(value) <= keep * 2:
        return "<redacted>"
    return f"{value[:keep]}…{value[-keep:]}"


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass


def atomic_write(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    """Write bytes to a same-directory temp file, fsync, and replace atomically."""
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def atomic_copy(src: Path, dst: Path, *, mode: int = 0o600) -> None:
    ensure_private_dir(dst.parent)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dst.name}.", dir=dst.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, src.open("rb") as inp:
            for block in iter(lambda: inp.read(1024 * 1024), b""):
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def iter_file(path: Path, *, chunk: int = 64 * 1024) -> Iterator[bytes]:
    with _open_regular_nofollow(path) as fh:
        yield from iter(lambda: fh.read(chunk), b"")


def _open_regular_nofollow(path: Path) -> BinaryIO:
    """Open a regular file without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def safe_child(root: Path, relative: str | Path) -> Path:
    """Resolve an app-owned relative path and prove it is inside root.

    This follows symlinks, so it is only for paths whose final target is meant
    to be read directly. Public artifact access must use
    :func:`safe_storage_path`, which refuses symlinked path components.
    """
    root = root.resolve()
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("path escapes root")
    return candidate


def safe_storage_path(root: Path, relative: str | Path) -> Path:
    """Lexically contain a relative storage path and refuse symlink components.

    The service only stores generated paths, but a tampered SQLite row or a
    symlink placed in private storage must never turn into a file-read
    primitive. This does not follow the final component or any parent under
    the storage root.
    """
    root = root.resolve()
    rel = Path(relative)
    if rel.is_absolute() or any(part == ".." for part in rel.parts):
        raise ValueError("path escapes root")
    candidate = root
    for part in rel.parts:
        if part in ("", "."):
            continue
        candidate = candidate / part
        if candidate.is_symlink():
            raise ValueError("symlink in storage path")
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes root") from exc
    return candidate


def is_inside(root: Path, candidate: Path) -> bool:
    root = root.resolve()
    try:
        candidate = candidate.resolve(strict=False)
    except OSError:
        return False
    return candidate == root or root in candidate.parents


def chmod_private(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass
