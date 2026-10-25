"""Explicit, operator-run model provisioning and checksum verification."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from .errors import ConfigurationError, UnavailableError, ValidationFailure
from .modelspec import (
    CHATTERBOX_COMMIT,
    PERTH_COMMIT,
    TURBO_FILES,
    TURBO_OPTIONAL_FILES,
    TURBO_REPO_ID,
    TURBO_REVISION,
    expected_files,
)
from .util import atomic_write, sha256_file

MODEL_MARKER = ".clawvoice-model.json"


def verify_model(
    model_dir: Path, *, backend: str = "turbo", deep: bool = False, require_marker: bool = True
) -> dict[str, Any]:
    if backend != "turbo":
        raise ValidationFailure(f"unsupported backend: {backend}")
    model_dir = Path(model_dir)
    if not model_dir.is_dir():
        return {
            "ok": False,
            "model_dir": str(model_dir),
            "revision": TURBO_REVISION,
            "problems": [{"problem": "model_directory_missing"}],
            "files_checked": 0,
        }
    problems: list[dict[str, Any]] = []
    checked = 0
    for item in (*TURBO_FILES, *TURBO_OPTIONAL_FILES):
        path = model_dir / item.name
        optional = item in TURBO_OPTIONAL_FILES
        if not path.is_file():
            if not optional:
                problems.append({"file": item.name, "problem": "missing"})
            continue
        size = path.stat().st_size
        if size != item.size:
            problems.append(
                {"file": item.name, "problem": "size_mismatch", "expected": item.size, "actual": size}
            )
            continue
        if deep:
            actual = sha256_file(path)
            if actual != item.sha256:
                problems.append({"file": item.name, "problem": "sha256_mismatch"})
                continue
        checked += 1
    marker_path = model_dir / MODEL_MARKER
    marker: dict[str, Any] | None = None
    if marker_path.is_file():
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            marker = None
    if require_marker:
        if marker is None:
            problems.append({"file": MODEL_MARKER, "problem": "provisioning_marker_missing"})
        elif marker.get("revision") != TURBO_REVISION:
            problems.append({"file": MODEL_MARKER, "problem": "revision_mismatch"})
        else:
            for item in expected_files(backend):
                recorded = (marker.get("files") or {}).get(item.name)
                if recorded != item.sha256:
                    problems.append({"file": item.name, "problem": "marker_checksum_mismatch"})
                    break
    return {
        "ok": not problems,
        "model_dir": str(model_dir),
        "revision": TURBO_REVISION,
        "repo_id": TURBO_REPO_ID,
        "chatterbox_commit": CHATTERBOX_COMMIT,
        "perth_commit": PERTH_COMMIT,
        "deep": deep,
        "files_checked": checked,
        "problems": problems,
        "marker": marker,
    }


def write_marker(model_dir: Path, *, backend: str = "turbo") -> dict[str, Any]:
    if backend != "turbo":
        raise ValidationFailure(f"unsupported backend: {backend}")
    files = {item.name: item.sha256 for item in expected_files(backend)}
    optional = {item.name: item.sha256 for item in TURBO_OPTIONAL_FILES}
    marker = {
        "schema_version": 1,
        "repo_id": TURBO_REPO_ID,
        "revision": TURBO_REVISION,
        "chatterbox_commit": CHATTERBOX_COMMIT,
        "perth_commit": PERTH_COMMIT,
        "files": files,
        "optional_files": optional,
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "license": "MIT",
        "note": "Reference conditioning only; this model is not fine-tuned by enrollment.",
    }
    atomic_write(model_dir / MODEL_MARKER, json.dumps(marker, indent=2, sort_keys=True).encode(), mode=0o644)
    return marker


def fetch_model(
    model_dir: Path,
    *,
    backend: str = "turbo",
    revision: str = TURBO_REVISION,
    force: bool = False,
) -> dict[str, Any]:
    if backend != "turbo":
        raise ValidationFailure(f"unsupported backend: {backend}")
    if revision != TURBO_REVISION:
        raise ConfigurationError(
            "this release only permits the pinned model revision; do not move it silently",
            details={"requested": revision, "pinned": TURBO_REVISION},
        )
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise UnavailableError(
            "huggingface_hub is not installed; run this command in the inference environment"
        ) from exc
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    needed = sum(item.size for item in expected_files(backend)) + 512 * 1024 * 1024
    try:
        free = shutil.disk_usage(model_dir).free
    except OSError as exc:
        raise UnavailableError("cannot check free space for model provisioning") from exc
    if free < needed:
        raise UnavailableError(
            "not enough free disk space to provision the pinned model",
            details={"free_bytes": free, "required_bytes": needed},
        )
    if force:
        marker = model_dir / MODEL_MARKER
        marker.unlink(missing_ok=True)
    snapshot_download(
        repo_id=TURBO_REPO_ID,
        revision=TURBO_REVISION,
        local_dir=str(model_dir),
        allow_patterns=["*.safetensors", "*.json", "*.txt", "*.yaml", "*.pt", "*.model"],
    )
    result = verify_model(model_dir, backend=backend, deep=True, require_marker=False)
    if not result["ok"]:
        raise ValidationFailure(
            "the downloaded model failed checksum verification",
            details={"problems": result["problems"][:12]},
        )
    write_marker(model_dir, backend=backend)
    result = verify_model(model_dir, backend=backend, deep=False)
    return result
