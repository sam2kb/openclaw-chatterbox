"""Read-only local compatibility report for operators."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .client import ApiError, Client, ClientConfig, ClientConfigurationError, ClientUnavailableError
from .config import Settings
from .modelprovision import verify_model
from .modelspec import CHATTERBOX_COMMIT, PERTH_COMMIT, TURBO_REPO_ID, TURBO_REVISION


def _read_os_release() -> dict[str, str]:
    data: dict[str, str] = {}
    path = Path("/etc/os-release")
    if not path.is_file():
        return data
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                data[key] = value.strip().strip('"')
    except OSError:
        pass
    return data


def _cpu_info() -> dict[str, Any]:
    model = None
    physical: set[tuple[str, str]] = set()
    try:
        text = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
        models = re.findall(r"^model name\s*:\s*(.+)$", text, flags=re.MULTILINE)
        if models:
            model = models[0].strip()
        for block in re.split(r"\n\s*\n", text):
            pid = re.search(r"^physical id\s*:\s*(\d+)", block, flags=re.MULTILINE)
            core = re.search(r"^core id\s*:\s*(\d+)", block, flags=re.MULTILINE)
            if pid and core:
                physical.add((pid.group(1), core.group(1)))
    except OSError:
        pass
    return {
        "model": model,
        "logical_threads": os.cpu_count(),
        "physical_cores": len(physical) or None,
    }


def _memory_info() -> dict[str, Any]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return {
        "total_bytes": values.get("MemTotal"),
        "available_bytes": values.get("MemAvailable"),
    }


def _disk_info(path: Path) -> dict[str, Any]:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        usage = shutil.disk_usage(candidate)
        return {
            "path": str(candidate),
            "total_bytes": usage.total,
            "free_bytes": usage.free,
            "used_bytes": usage.used,
        }
    except OSError:
        return {"path": str(candidate), "free_bytes": None, "error": "unavailable"}


def _gpu_info() -> dict[str, Any]:
    binary = shutil.which("nvidia-smi")
    if not binary:
        return {"available": False, "reason": "nvidia-smi_not_found"}
    try:
        proc = subprocess.run(
            [
                binary,
                "--query-gpu=name,driver_version,memory.total,compute_cap",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"available": False, "reason": type(exc).__name__}
    if proc.returncode != 0:
        return {
            "available": False,
            "reason": "nvidia-smi_failed",
            "exit_code": proc.returncode,
        }
    gpus = []
    for line in proc.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 4:
            gpus.append(
                {
                    "name": parts[0],
                    "driver_version": parts[1],
                    "memory_total_mib": _to_int(parts[2]),
                    "compute_capability": parts[3],
                }
            )
    return {"available": bool(gpus), "gpus": gpus}


def _to_int(value: str) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _torch_info() -> dict[str, Any]:
    spec = None
    try:
        spec = importlib.util.find_spec("torch")
    except (ImportError, ValueError):
        spec = None
    if spec is None:
        return {"installed": False}
    try:
        torch = importlib.import_module("torch")
    except Exception as exc:
        return {"installed": True, "importable": False, "error": type(exc).__name__}
    info: dict[str, Any] = {
        "installed": True,
        "importable": True,
        "version": getattr(torch, "__version__", None),
        "cuda_version": getattr(getattr(torch, "version", None), "cuda", None),
        "cuda_available": bool(torch.cuda.is_available()),
    }
    if not info["cuda_available"]:
        return info
    try:
        index = torch.cuda.current_device()
        capability = torch.cuda.get_device_capability(index)
        arch_list = list(torch.cuda.get_arch_list())
        expected_arch = f"sm_{capability[0]}{capability[1]}"
        info.update(
            {
                "device_count": torch.cuda.device_count(),
                "device_name": torch.cuda.get_device_name(index),
                "capability": f"{capability[0]}.{capability[1]}",
                "arch_list": arch_list,
                "compiled_arch_for_device": expected_arch,
                "compiled_arch_matches": (
                    expected_arch in arch_list
                    or f"compute_{capability[0]}{capability[1]}" in arch_list
                ),
                "compatible_compiled_architectures": [
                    arch
                    for arch in arch_list
                    if arch in (expected_arch, f"compute_{capability[0]}{capability[1]}")
                ],
                "cudnn_version": torch.backends.cudnn.version()
                if torch.backends.cudnn.is_available()
                else None,
            }
        )
        torch.cuda.init()
        x = torch.ones((256, 256), dtype=torch.float32, device="cuda")
        y = (x @ x).sum()
        torch.cuda.synchronize()
        info["allocation_math"] = {
            "passed": bool(torch.isfinite(y).item()),
            "result": float(y.item()),
        }
    except Exception as exc:
        info["allocation_math"] = {"passed": False, "error": type(exc).__name__}
    return info


def _ffmpeg_info(settings: Settings) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ffmpeg": shutil.which(settings.ffmpeg),
        "ffprobe": shutil.which(settings.ffprobe),
    }
    for name in ("ffmpeg", "ffprobe"):
        binary = result[name]
        if not binary:
            continue
        try:
            proc = subprocess.run(
                [binary, "-version"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            result[f"{name}_version"] = (proc.stdout or "").splitlines()[0] if proc.stdout else None
        except (OSError, subprocess.TimeoutExpired) as exc:
            result[f"{name}_version_error"] = type(exc).__name__
    return result


def _openclaw_info() -> dict[str, Any]:
    state = os.environ.get("OPENCLAW_STATE_DIR")
    candidates = []
    if state:
        candidates.append(Path(state) / "skills" / "chatterbox-voice" / "SKILL.md")
    candidates.append(Path.home() / ".openclaw" / "skills" / "chatterbox-voice" / "SKILL.md")
    binary = shutil.which("openclaw")
    version = None
    if binary:
        try:
            proc = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, timeout=10, check=False
            )
            version = (proc.stdout or proc.stderr or "").strip().splitlines()[0] if proc.stdout or proc.stderr else None
        except (OSError, subprocess.TimeoutExpired):
            version = None
    return {
        "openclaw_binary": binary,
        "version": version,
        "state_dir_configured": bool(state),
        "skill_installed": any(path.is_file() for path in candidates),
        "note": "Host skill readiness does not imply sandbox reachability; test from the real agent execution context.",
    }


def _service_info(settings: Settings) -> dict[str, Any]:
    try:
        config = ClientConfig.from_env(require_token=False)
    except ClientConfigurationError as exc:
        return {"reachable": False, "error": str(exc)}
    client = Client(config)
    result: dict[str, Any] = {
        "url": config.base_url,
        "service_token_configured": bool(config.token),
        "live": None,
        "ready": None,
    }
    try:
        result["live"] = client.live(timeout=5)
    except (ClientUnavailableError, ApiError) as exc:
        result["reachable"] = False
        result["error"] = str(exc)
        return result
    result["reachable"] = True
    if config.token:
        try:
            result["ready"] = client.ready()
        except ApiError as exc:
            result["ready_status"] = exc.status
            result["ready_error"] = {"status": exc.status, "code": exc.code, "message": exc.message}
            if exc.payload and exc.payload.get("status"):
                result["ready"] = exc.payload
        except ClientUnavailableError as exc:
            result["ready_error"] = {"message": str(exc)}
    return result


def _compatibility_record(settings: Settings) -> dict[str, Any] | None:
    path = settings.state_dir / "compatibility.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def build_report(settings: Settings) -> dict[str, Any]:
    compatibility = _compatibility_record(settings)
    model = verify_model(settings.model_dir, backend="turbo", deep=False)
    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "service_version": settings.service_version,
        "client_version": settings.service_version,
        "os": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "distro": _read_os_release(),
        },
        "cpu": _cpu_info(),
        "memory": _memory_info(),
        "disk": _disk_info(settings.data_dir),
        "gpu": _gpu_info(),
        "torch": _torch_info(),
        "ffmpeg": _ffmpeg_info(settings),
        "model": {
            **model,
            "repo_id": TURBO_REPO_ID,
            "revision": TURBO_REVISION,
            "chatterbox_commit": CHATTERBOX_COMMIT,
            "perth_commit": PERTH_COMMIT,
            "configured_backend": settings.backend,
            "configured_device": settings.device,
        },
        "service": _service_info(settings),
        "openclaw": _openclaw_info(),
        "compatibility_smoke": compatibility,
        "target_support_verified": bool(
            compatibility
            and compatibility.get("real_model") is True
            and compatibility.get("model_revision") == TURBO_REVISION
            and compatibility.get("cuda") is True
        ),
        "notes": [
            "torch.cuda.is_available() is not sufficient; allocation_math must pass and a real synthesis smoke must run.",
            "This report does not download models, install packages, or change service state.",
        ],
    }
    remote = report.get("service", {}).get("ready") or {}
    worker = remote.get("checks", {}).get("worker", {})
    report["assessment"] = {
        "status": "ready" if ready_enough(report) else "not_ready",
        "authority": "service_worker",
        "reason": remote.get("reason", "service_unreachable"),
        "real_model": worker.get("real_model", False),
        "device": worker.get("device"),
        "local_dependencies_required_for_client": False,
        "cleanup": remote.get("checks", {}).get("cleanup", {}),
    }
    report["local_runtime"] = {"scope": "client_machine_only", "torch": report["torch"], "model": report["model"]}
    report["torch"]["scope"] = "client_machine_only"
    report["model"]["scope"] = "client_machine_only"
    report["notes"].insert(0, "Service worker readiness is authoritative. A lightweight client does not need local Torch or model weights.")
    return report


def ready_enough(report: dict[str, Any]) -> bool:
    service = report.get("service") or {}
    ready = service.get("ready") or {}
    return bool(
        service.get("reachable")
        and ready.get("status") == "ready"
        and ready.get("checks", {}).get("worker", {}).get("real_model") is True
    )


def render_text(report: dict[str, Any]) -> str:
    lines = [
        "clawvoice doctor",
        f"  Service worker: {report.get('assessment', {}).get('status', 'unknown')} "
        f"(real model: {report.get('assessment', {}).get('real_model', False)})",
        "  Local probes below describe this client; missing local Torch/weights do not block a ready worker.",
        f"  OS: {report['os']['system']} {report['os']['release']} ({report['os']['machine']})",
        f"  CPU: {(report['cpu'].get('model') or 'unknown')} · {report['cpu'].get('logical_threads')} threads",
    ]
    mem = report["memory"]
    if mem.get("total_bytes"):
        lines.append(
            f"  RAM: {mem['total_bytes'] / 1024**3:.1f} GiB total · "
            f"{(mem.get('available_bytes') or 0) / 1024**3:.1f} GiB available"
        )
    disk = report["disk"]
    if disk.get("free_bytes"):
        lines.append(f"  Disk: {disk['free_bytes'] / 1024**3:.1f} GiB free at {disk['path']}")
    gpu = report.get("gpu") or {}
    if gpu.get("available"):
        for item in gpu.get("gpus", []):
            lines.append(
                f"  GPU: {item['name']} · driver {item['driver_version']} · "
                f"{item.get('memory_total_mib')} MiB · capability {item.get('compute_capability')}"
            )
    else:
        lines.append(f"  GPU: unavailable ({gpu.get('reason', 'not detected')})")
    torch = report.get("torch") or {}
    lines.append(
        f"  Torch: {torch.get('version') or 'not installed'} · CUDA {torch.get('cuda_version') or 'n/a'}"
    )
    if torch.get("allocation_math"):
        lines.append(f"  CUDA allocation/math: {torch['allocation_math']}")
    model = report.get("model") or {}
    lines.append(f"  Model: {'verified files' if model.get('ok') else 'not ready'} · {model.get('revision')}")
    service = report.get("service") or {}
    lines.append(f"  Service: {'reachable' if service.get('reachable') else 'unreachable'} · {service.get('url')}")
    ready = service.get("ready") or {}
    if ready:
        lines.append(f"  Readiness: {ready.get('status')} ({ready.get('reason')})")
    cleanup = ready.get("checks", {}).get("cleanup", {})
    if cleanup:
        lines.append(f"  Cleanup: {cleanup.get('status', 'unknown')}, every {cleanup.get('interval_seconds')} s; last run {cleanup.get('last_run_at')}")
    lines.append(f"  Target support verified: {report.get('target_support_verified')}")
    lines.append("")
    lines.append("A build/dev check is not target verification. Run models smoke on the authorized GPU host.")
    return "\n".join(lines)


__all__ = ["build_report", "ready_enough", "render_text"]
