"""Owner workflow: private managed exports, ASR checks, and media receipts."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .client import ClientConfigurationError
from .util import atomic_write, sha256_file


def library_root() -> Path:
    value = os.environ.get("CLAWVOICE_LIBRARY_DIR")
    root = Path(value).expanduser() if value else Path.home() / ".openclaw/media/clawvoice"
    if root.is_symlink():
        raise ClientConfigurationError("the managed audio library must not be a symlink")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root.resolve()


@contextmanager
def library_lock(root):
    path = root / ".library.lock"
    if path.is_symlink():
        raise ClientConfigurationError("invalid library lock")
    with path.open("a") as f:
        path.chmod(0o600)
        if os.name == "posix":
            import fcntl

            fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "posix":
                fcntl.flock(f, fcntl.LOCK_UN)


def master_artifact(client, artifact_id):
    artifact = client.get_artifact(artifact_id)
    if artifact["kind"] != "master":
        job = client.get_job(artifact["job_id"])
        if not job.get("master_artifact_id"):
            raise ClientConfigurationError("the original master is unavailable")
        artifact = client.get_artifact(job["master_artifact_id"])
    if (
        artifact["kind"] != "master"
        or artifact["sample_rate"] != 24000
        or artifact["channels"] != 1
    ):
        raise ClientConfigurationError("delivery requires the original 24 kHz mono master")
    return artifact


def export_master(client, artifact):
    root = library_root()
    digest = artifact["sha256"]
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ClientConfigurationError("invalid artifact checksum")
    output, metadata = root / (digest + ".wav"), root / (digest + ".json")
    with library_lock(root):
        if output.is_symlink() or metadata.is_symlink():
            raise ClientConfigurationError("symlink in managed library")
        if output.exists():
            if sha256_file(output) != digest:
                raise ClientConfigurationError(
                    "cached audio checksum mismatch; refusing to deliver it"
                )
        else:
            client.fetch_artifact(artifact["id"], output)
        prior = json.loads(metadata.read_text()) if metadata.exists() else {}
        expires = artifact["expires_at"]
        if prior and prior.get("expires_at") is None:
            expires = None
        record = {
            "sha256": digest,
            "artifact_ids": sorted(set(prior.get("artifact_ids", []) + [artifact["id"]])),
            "expires_at": expires,
            "sample_rate": 24000,
            "channels": 1,
            "retained": expires is None,
            "duration_ms": artifact["duration_ms"],
        }
        atomic_write(metadata, json.dumps(record, indent=2).encode(), mode=0o600)
    if not os.access(output, os.R_OK):
        raise ClientConfigurationError("exported audio is not readable by this agent")
    return {**record, "path": str(output)}


def cleanup_library(*, dry_run=False, now=None):
    root = library_root()
    now = now or datetime.now(UTC)
    removed, size = 0, 0
    with library_lock(root):
        for metadata in root.glob("*.json"):
            if metadata.is_symlink() or not re.fullmatch(r"[a-f0-9]{64}", metadata.stem):
                continue
            try:
                record = json.loads(metadata.read_text())
                expires = record.get("expires_at")
                if not expires or datetime.fromisoformat(expires.replace("Z", "+00:00")) >= now:
                    continue
                audio = root / (metadata.stem + ".wav")
                if audio.is_symlink() or record.get("sha256") != metadata.stem:
                    continue
                size += audio.stat().st_size if audio.exists() else 0
                if not dry_run:
                    audio.unlink(missing_ok=True)
                    metadata.unlink()
                removed += 1
            except (OSError, ValueError, TypeError):
                continue
    return {"dry_run": dry_run, "expired_files": removed, "bytes": size}


def deliver(client, artifact_ids, *, player=False):
    cleanup_library()
    artifacts, seen = [], set()
    for aid in artifact_ids:
        a = master_artifact(client, aid)
        if a["sha256"] not in seen:
            artifacts.append(a)
            seen.add(a["sha256"])
    exports = [export_master(client, a) for a in artifacts]
    labels = [
        f"Take {i + 1}: {a['duration_ms'] / 1000:.2f} s, original WAV"
        for i, a in enumerate(artifacts)
    ]
    reply = "\n".join(labels)
    review = None
    if player:
        review = client._json(
            "POST", "/api/v1/reviews", body={"artifact_ids": [a["id"] for a in artifacts]}
        )
        reply += "\n\nPrivate player: " + review["player_url"]
    reply += "\n\n" + "\n".join("MEDIA:" + e["path"] for e in exports)
    return {
        "reply": reply,
        "files": exports,
        "review": review,
        "notice": "Copy reply verbatim as plain text; every MEDIA line belongs at the end outside code fences.",
    }


def measure_audio(path):
    import array
    import math
    import sys
    import wave

    count = total = peak = clipped = 0
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
            raise ClientConfigurationError("signal review requires the original mono PCM master")
        rate = wav.getframerate()
        while data := wav.readframes(65536):
            samples = array.array("h", data)
            if sys.byteorder != "little":
                samples.byteswap()
            count += len(samples)
            total += sum(value * value for value in samples)
            peak = max(peak, max((abs(value) for value in samples), default=0))
            clipped += sum(abs(value) >= 32760 for value in samples)
    return {
        "frames": count,
        "sample_rate": rate,
        "channels": 1,
        "duration_ms": round(count / rate * 1000),
        "peak_dbfs": round(20 * math.log10(peak / 32768), 2) if peak else None,
        "rms_dbfs": round(20 * math.log10(math.sqrt(total / count) / 32768), 2)
        if total and count
        else None,
        "clipped_fraction": clipped / count if count else 0,
    }


def verify_artifact(client, artifact_id, *, transcript_file=None, language="en"):
    artifact = master_artifact(client, artifact_id)
    verifier = "owner-supplied-transcript"
    metrics = None
    if transcript_file:
        transcript = Path(transcript_file).read_text(encoding="utf-8")
    else:
        if not re.fullmatch(r"[a-z]{2,3}", language):
            raise ClientConfigurationError("language must be a two- or three-letter code")
        # Keep the transcription tool's existing read boundary. Only this new
        # 0700 directory is removed; no general inbox cleanup is performed.
        root = Path(os.environ.get("CLAWVOICE_ASR_STAGING_ROOT", "/srv/inbox"))
        if not root.is_dir() or root.is_symlink():
            raise ClientConfigurationError("approved transcription staging root is unavailable")
        with tempfile.TemporaryDirectory(prefix="clawvoice-qa-", dir=root) as temporary:
            output = Path(temporary) / "reference-check.wav"
            client.fetch_artifact(artifact["id"], output)
            metrics = measure_audio(output)
            try:
                proc = subprocess.run(
                    [
                        "/usr/local/bin/openclaw-transcribe",
                        str(output),
                        "--language",
                        language,
                        "--json",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=600,
                    check=False,
                )
                if proc.returncode:
                    raise ClientConfigurationError("transcription failed; audio remains unverified")
                result = json.loads(proc.stdout)
                transcript = result["text"]
                verifier = "openclaw-transcribe/" + str(result.get("model", "unknown"))
            except (OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
                raise ClientConfigurationError(
                    "transcription unavailable; audio remains unverified"
                ) from exc
    return client._json(
        "POST",
        f"/api/v1/artifacts/{artifact['id']}/verify",
        body={
            "audio_sha256": artifact["sha256"],
            "transcript": transcript,
            "verifier": verifier,
            "audio_metrics": metrics,
        },
    )


def schedule_library_cleanup(*, apply=False):
    """Schedule only the agent-owned export cache; service data has its own loop."""
    import getpass
    import sys

    if os.name != "posix" or os.geteuid() == 0 or getpass.getuser() in ("dsh", "coder"):
        raise ClientConfigurationError(
            "install cache maintenance as the authorized host agent user"
        )
    root = library_root()
    units = Path.home() / ".config/systemd/user"

    def quoted(value):
        if any(ord(c) < 32 for c in value):
            raise ClientConfigurationError("unsupported control character in unit path")
        return '"' + value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'

    service = (
        "[Unit]\nDescription=Expire unapproved clawvoice export cache\n\n"
        "[Service]\nType=oneshot\nUMask=0077\nNoNewPrivileges=true\nTimeoutStartSec=120\n"
        + "Environment="
        + quoted("CLAWVOICE_LIBRARY_DIR=" + str(root))
        + "\n"
        + "ExecStart="
        + quoted(sys.executable)
        + " -m clawvoice library cleanup --json\n"
    )
    timer = (
        "[Unit]\nDescription=Daily clawvoice export maintenance\n\n[Timer]\n"
        "OnCalendar=daily\nPersistent=true\nRandomizedDelaySec=15m\n\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    if apply:
        units.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name, content in (
            ("clawvoice-cache-cleanup.service", service),
            ("clawvoice-cache-cleanup.timer", timer),
        ):
            target = units / name
            if target.is_symlink():
                raise ClientConfigurationError("refusing a symlinked cache-maintenance unit")
            atomic_write(target, content.encode(), mode=0o600)
        for command in (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", "clawvoice-cache-cleanup.timer"],
        ):
            result = subprocess.run(
                command, capture_output=True, text=True, check=False, timeout=30
            )
            if result.returncode:
                raise ClientConfigurationError(
                    "cache-maintenance activation failed; check the user systemd manager"
                )
    return {
        "applied": apply,
        "timer": "clawvoice-cache-cleanup.timer",
        "schedule": "daily",
        "retained_assets_preserved": True,
    }
