#!/usr/bin/env python3
"""Guarded Clawbox installer for openclaw-chatterbox.

Planning is read-only. Every mutating command requires --apply. This tool only
touches the dedicated skill directory and the dedicated Compose project; it does
not edit OpenClaw JSON, NVIDIA/CUDA, model weights, or the coder container.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "deploy" / "release-manifest.json"
SKILL_SRC = ROOT / "skills" / "chatterbox-voice"
if not SKILL_SRC.is_dir():
    SKILL_SRC = ROOT / "skill" / "chatterbox-voice"
COMPOSE = ROOT / "deploy" / "compose.yaml"
GPU = ROOT / "deploy" / "compose.gpu.yaml"
CPU = ROOT / "deploy" / "compose.cpu.yaml"
STATE_DIR = ROOT / "deploy" / ".state"
BACKUPS = ROOT / "deploy" / "backups"
SKILL_NAME = "chatterbox-voice"


def now_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def load_manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def run(
    args: list[str], *, timeout: int = 60, env_overrides: dict[str, str] | None = None
) -> dict[str, Any]:
    env = os.environ.copy()
    if env_overrides:
        env.update(env_overrides)
    try:
        proc = subprocess.run(
            args,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        return {
            "command": args,
            "returncode": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"command": args, "returncode": None, "error": type(exc).__name__}


def which(name: str) -> str | None:
    return shutil.which(name)


def find_cli() -> str | None:
    local = ROOT / ".venv" / "bin" / "clawvoice"
    if local.is_file() and os.access(local, os.X_OK):
        return str(local)
    return shutil.which("clawvoice")


def hash_tree(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not root.is_dir():
        return result
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        rel = path.relative_to(root).as_posix()
        result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def skill_diff(target: Path, *, source: Path | None = None) -> dict[str, Any]:
    desired = hash_tree(source or SKILL_SRC)
    existing = hash_tree(target)
    changed = sorted(
        name for name in set(desired) | set(existing) if desired.get(name) != existing.get(name)
    )
    return {
        "target": str(target),
        "exists": target.is_dir(),
        "drift": bool(changed),
        "changed_files": changed,
        "desired_file_count": len(desired),
        "existing_file_count": len(existing),
    }


def copy_tree(src: Path, dst: Path) -> None:
    """Copy a skill tree without inheriting setgid directory modes.

    Task/workspace trees can be setgid; copying that bit is denied by the host
    sandbox and is not meaningful for an installed skill.
    """
    src = Path(src)
    dst = Path(dst)
    dst.mkdir(mode=0o755, parents=True, exist_ok=False)
    try:
        dst.chmod(0o755)
    except OSError:
        pass
    for child in sorted(src.iterdir()):
        target = dst / child.name
        if child.is_dir() and not child.is_symlink():
            copy_tree(child, target)
        elif child.is_file() and not child.is_symlink():
            shutil.copyfile(child, target)
            try:
                target.chmod(0o644)
            except OSError:
                pass
    try:
        dst.chmod(0o755)
    except OSError:
        pass


def load_release_state() -> dict[str, Any]:
    path = STATE_DIR / "install.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(value: dict[str, Any]) -> None:
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = STATE_DIR / "install.json"
    fd, tmp_name = tempfile.mkstemp(prefix=".install.", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, target)
    finally:
        try:
            Path(tmp_name).unlink()
        except FileNotFoundError:
            pass


def env_file_value(path: str | None, key: str, default: str) -> str:
    if path:
        candidate = Path(path).expanduser()
        try:
            for raw in candidate.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, value = line.split("=", 1)
                if name.strip() == key:
                    return value.strip().strip('"').strip("'")
        except OSError:
            pass
    return os.environ.get(key, default)


def port_status(port: int) -> dict[str, Any]:
    sock = socket.socket()
    sock.settimeout(0.5)
    try:
        code = sock.connect_ex(("127.0.0.1", port))
        return {"port": port, "listening": code == 0}
    finally:
        sock.close()


def skill_target(workspace: Path | None) -> Path | None:
    if workspace is None:
        return None
    return workspace.resolve() / "skills" / SKILL_NAME


def openclaw_inventory(workspace: Path | None) -> dict[str, Any]:
    binary = which("openclaw")
    version = run([binary, "--version"], timeout=10) if binary else None
    env_state = os.environ.get("OPENCLAW_STATE_DIR")
    candidates = []
    if workspace:
        candidates.append(workspace / "skills" / SKILL_NAME)
    if env_state:
        candidates.append(Path(env_state) / "skills" / SKILL_NAME)
    candidates.append(Path.home() / ".openclaw" / "skills" / SKILL_NAME)
    return {
        "binary": binary,
        "version": version,
        "state_dir_configured": bool(env_state),
        "workspace": str(workspace) if workspace else None,
        "skill_candidates": [{"path": str(path), "present": path.is_dir()} for path in candidates],
        "note": "Version/policy inventory is read-only. Confirm allowlists and sandbox behavior before install.",
    }


def host_model_dir(args: argparse.Namespace) -> Path:
    if getattr(args, "model_dir", None):
        return Path(args.model_dir).expanduser().resolve()
    base = env_file_value(args.env_file, "CLAWVOICE_MODELS_DIR", "../models")
    path = Path(base).expanduser()
    if not path.is_absolute():
        path = (COMPOSE.parent / path).resolve()
    return path / "chatterbox-turbo"


def model_report(model_dir: Path | None = None) -> dict[str, Any]:
    cli = find_cli()
    if not cli:
        return {"ok": False, "reason": "clawvoice_not_on_path"}
    command = [cli, "models", "verify", "--backend", "turbo", "--json"]
    if model_dir is not None:
        command.extend(["--dir", str(model_dir)])
    result = run(command, timeout=180)
    try:
        payload = json.loads(result.get("stdout") or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "ok": bool(payload.get("ok")),
        "revision": payload.get("revision"),
        "model_dir": payload.get("model_dir"),
        "problems": payload.get("problems", [])[:12],
        "command_returncode": result.get("returncode"),
    }


def service_live(port: int) -> dict[str, Any]:
    try:
        import urllib.request

        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/v1/health/live", timeout=3
        ) as resp:
            return {"reachable": True, "status": resp.status, "body": json.loads(resp.read())}
    except Exception as exc:  # bounded diagnostic only
        return {"reachable": False, "error": type(exc).__name__}


def docker_report() -> dict[str, Any]:
    docker = which("docker")
    compose = run([docker, "compose", "version"], timeout=15) if docker else None
    return {
        "docker": docker,
        "compose_version": compose,
        "daemon": run([docker, "info", "--format", "{{.ServerVersion}}"], timeout=15)
        if docker
        else None,
    }


def wait_container_ready(
    args: argparse.Namespace,
    *,
    root: Path | None = None,
    version: str | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {"ready": False, "reason": "not_started"}
    while time.monotonic() < deadline:
        result = run(
            compose_command(
                args,
                "exec",
                "-T",
                "api",
                "clawvoice",
                "doctor",
                "--json",
                "--require-ready",
                root=root,
            ),
            timeout=60,
            env_overrides=release_env(
                version or load_manifest().get("release"), host_model_dir(args)
            ),
        )
        try:
            payload = json.loads(result.get("stdout") or "{}")
        except json.JSONDecodeError:
            payload = {}
        ready = (payload.get("service") or {}).get("ready") or {}
        last = {
            "ready": bool(
                result.get("returncode") == 0
                and ready.get("status") == "ready"
                and (ready.get("checks", {}).get("worker", {}).get("real_model") is True)
            ),
            "reason": ready.get("reason"),
            "doctor_exit": result.get("returncode"),
        }
        if last["ready"]:
            return last
        time.sleep(3.0)
    return last


def wait_live(port: int, *, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {"reachable": False}
    while time.monotonic() < deadline:
        last = service_live(port)
        if last.get("reachable"):
            return last
        time.sleep(1.0)
    return last


def image_id(image: str | None) -> str | None:
    if not image:
        return None
    docker = which("docker")
    if not docker:
        return None
    result = run([docker, "image", "inspect", "--format", "{{.Id}}", image], timeout=20)
    return result.get("stdout") or None if result.get("returncode") == 0 else None


def image_present(image: str | None) -> bool:
    if not image:
        return False
    docker = which("docker")
    if not docker:
        return False
    return run([docker, "image", "inspect", image], timeout=20).get("returncode") == 0


def disk_report() -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(ROOT)
        return {"path": str(ROOT), "free_bytes": usage.free, "total_bytes": usage.total}
    except OSError:
        return {"path": str(ROOT), "free_bytes": None}


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    manifest = load_manifest()
    workspace = Path(args.openclaw_workspace).expanduser() if args.openclaw_workspace else None
    target = skill_target(workspace)
    model_dir = host_model_dir(args)
    model = model_report(model_dir)
    port = int(env_file_value(args.env_file, "CLAWVOICE_PORT", "8765"))
    docker = docker_report()
    overrides = ["--gpu"] if args.gpu else (["--cpu"] if args.cpu else [])
    compose_files = (
        ["-f", str(COMPOSE), "-f", str(GPU if args.gpu else CPU)]
        if overrides
        else ["-f", str(COMPOSE)]
    )
    env_file = ["--env-file", str(Path(args.env_file).expanduser())] if args.env_file else []
    install_commands = []
    if docker["docker"]:
        install_commands.append(
            [
                "docker",
                "compose",
                *env_file,
                *compose_files,
                "up",
                "-d",
                "--no-build",
            ]
        )
    if target is not None:
        install_commands.append(["stage-skill", str(SKILL_SRC), str(target)])
    plan = {
        "tool": "clawbox",
        "mode": "plan",
        "side_effects": "none",
        "generated_at": datetime.now(UTC).isoformat(),
        "release": {
            "version": manifest.get("release"),
            "status": manifest.get("status"),
            "manifest": str(MANIFEST),
            "model_revision": (manifest.get("source") or {}).get("model_revision"),
            "target_support_verified": bool(
                (manifest.get("runtime") or {}).get("target_support_verified")
            ),
        },
        "prerequisites": {
            "docker": docker,
            "model": model,
            "model_dir": str(model_dir),
            "python": sys.version.split()[0],
            "disk": disk_report(),
            "port": port_status(port),
            "ffmpeg": which("ffmpeg"),
            "clawvoice": find_cli(),
            "openclaw": openclaw_inventory(workspace),
        },
        "skill": skill_diff(target) if target else {"target": None, "exists": False, "drift": None},
        "service": {
            "compose_files": compose_files,
            "api_image_present": image_present(
                (manifest.get("images") or {}).get("api", {}).get("tag")
            ),
            "worker_image_present": image_present(
                (manifest.get("images") or {}).get("worker", {}).get("tag")
            ),
            "api_image_actual_id": image_id(
                (manifest.get("images") or {}).get("api", {}).get("tag")
            ),
            "worker_image_actual_id": image_id(
                (manifest.get("images") or {}).get("worker", {}).get("tag")
            ),
            "api_image_id_expected": (manifest.get("images") or {})
            .get("api", {})
            .get("local_image_id"),
            "worker_image_id_expected": (manifest.get("images") or {})
            .get("worker", {})
            .get("local_image_id"),
            "gpu_overlay": bool(args.gpu),
            "cpu_overlay": bool(args.cpu),
            "env_file": args.env_file,
            "volume": "openclaw-chatterbox_clawvoice-data",
            "volume_owner_uid": 10001,
            "api_image": (manifest.get("images") or {}).get("api", {}).get("tag"),
            "api_image_id": (manifest.get("images") or {}).get("api", {}).get("local_image_id"),
            "worker_image": (manifest.get("images") or {}).get("worker", {}).get("tag"),
            "worker_image_id": (manifest.get("images") or {})
            .get("worker", {})
            .get("local_image_id"),
        },
        "requirements": {
            "model_download_bytes_approx": 3_000_000_000,
            "image_build_bytes_approx": 12_000_000_000,
            "data_min_bytes": 1_000_000_000,
            "loopback_port": port,
            "volume_owner_uid": 10001,
        },
        "live_service": service_live(port),
        "planned_commands": install_commands,
        "warnings": [],
    }
    warnings = plan["warnings"]
    if not docker["docker"] or docker.get("daemon", {}).get("returncode") != 0:
        warnings.append("Docker daemon is unavailable; install cannot be applied here.")
    if not model.get("ok"):
        warnings.append("The pinned model is not verified; install must stop until provisioned.")
    if not args.openclaw_workspace:
        warnings.append("No OpenClaw workspace supplied; skill staging is not planned.")
    if args.gpu and args.cpu:
        warnings.append("GPU and CPU overlays are mutually exclusive.")
    if plan["prerequisites"]["port"].get("listening"):
        warnings.append("The configured loopback port is already in use.")
    if not plan["release"]["target_support_verified"]:
        warnings.append("The release manifest says target GPU support is unverified.")
    if not args.gpu and not args.cpu:
        warnings.append("No device overlay selected; choose --gpu or --cpu explicitly.")
    if not plan["service"]["api_image_present"]:
        warnings.append("The API image is not present locally; build it before install/upgrade.")
    if not plan["service"]["worker_image_present"]:
        warnings.append(
            "The worker image is not present locally; build it on the target before install."
        )
    return plan


def require_apply(args: argparse.Namespace) -> None:
    if not args.apply:
        raise SystemExit("refusing to mutate live state without --apply")


def require_target_gate(args: argparse.Namespace, manifest: dict[str, Any]) -> None:
    if (manifest.get("runtime") or {}).get("target_support_verified") is False:
        if not getattr(args, "allow_unverified_target", False):
            raise SystemExit(
                "target support is unverified; run the GPU acceptance gate or pass "
                "--allow-unverified-target explicitly for a candidate install"
            )


def require_workspace(args: argparse.Namespace) -> Path:
    if not args.openclaw_workspace:
        raise SystemExit("--openclaw-workspace is required for a live skill install")
    if getattr(args, "agent", "main").lower() == "coder":
        raise SystemExit("refusing to install runtime voice access into the contained coder agent")
    workspace = Path(args.openclaw_workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise SystemExit(f"OpenClaw workspace does not exist: {workspace}")
    target = skill_target(workspace)
    assert target is not None
    if target.parent.parent != workspace:
        raise SystemExit("skill path escaped the OpenClaw workspace")
    resolved_parent = target.parent.resolve()
    if resolved_parent != workspace / "skills" or target.is_symlink() or target.parent.is_symlink():
        raise SystemExit("refusing a skill path with a symlink or unexpected parent")
    return workspace


def stage_skill(
    workspace: Path, *, backup: Path | None, source: Path | None = None
) -> dict[str, Any]:
    target = skill_target(workspace)
    assert target is not None
    skills_dir = target.parent
    skills_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}.stage-", dir=skills_dir))
    try:
        staged_skill = staging / SKILL_NAME
        copy_tree(source or SKILL_SRC, staged_skill)
        diff = skill_diff(staged_skill, source=source)
        if diff["changed_files"]:
            raise RuntimeError("staged skill does not match the release")
        old = None
        if target.exists():
            if backup is None:
                raise RuntimeError("refusing to replace an existing skill without a backup")
            copy_tree(target, backup)
            old = skills_dir / f".{SKILL_NAME}.old-{os.getpid()}"
            os.replace(target, old)
        try:
            os.replace(staged_skill, target)
        except BaseException:
            if old is not None and not target.exists():
                os.replace(old, target)
            raise
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
        return {"target": str(target), "backup": str(backup) if backup else None, "installed": True}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def release_env(version: str | None, model_dir: Path | None = None) -> dict[str, str]:
    values = {"CLAWVOICE_RELEASE": version} if version else {}
    if model_dir is not None:
        values["CLAWVOICE_MODELS_DIR"] = str(model_dir.parent)
    return values


def release_skill_source(root: Path) -> Path:
    for directory in ("skills", "skill"):
        source = root / directory / SKILL_NAME
        if (source / "SKILL.md").is_file():
            return source
    raise SystemExit("release is missing its chatterbox-voice skill")


def operation_args(args: argparse.Namespace, state: dict[str, Any]) -> argparse.Namespace:
    result = argparse.Namespace(**vars(args))
    if not result.env_file and state.get("env_file"):
        result.env_file = state["env_file"]
    if not getattr(result, "model_dir", None) and state.get("model_dir"):
        result.model_dir = state["model_dir"]
    if not result.gpu and not result.cpu:
        result.gpu = bool(state.get("gpu"))
        result.cpu = bool(state.get("cpu"))
    return result


def start_release(
    args: argparse.Namespace, root: Path, version: str | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    result = run(
        compose_command(args, "up", "-d", "--no-build", root=root),
        timeout=300,
        env_overrides=release_env(version, host_model_dir(args)),
    )
    readiness = (
        wait_container_ready(args, root=root, version=version)
        if result.get("returncode") == 0
        else {"ready": False, "reason": "compose_failed"}
    )
    return result, readiness


def running_image_id(
    args: argparse.Namespace, service: str, root: Path, version: str | None
) -> str | None:
    result = run(
        compose_command(args, "ps", "-q", service, root=root),
        env_overrides=release_env(version, host_model_dir(args)),
    )
    containers = result.get("stdout", "").splitlines()
    if result.get("returncode") != 0 or len(containers) != 1:
        return None
    docker = which("docker")
    if not docker:
        return None
    inspected = run([docker, "inspect", "--format", "{{.Image}}", containers[0]], timeout=20)
    return inspected.get("stdout") if inspected.get("returncode") == 0 else None


def restore_skill(workspace: Path, backup: Path) -> dict[str, Any]:
    """Restore only the dedicated skill directory from a retained backup."""
    if not backup.is_dir():
        raise SystemExit(f"skill backup is missing: {backup}")
    target = skill_target(workspace)
    assert target is not None
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    safety = None
    if target.exists():
        safety = backup_root() / "pre-rollback-skill"
        copy_tree(target, safety)
    staging = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}.rollback-", dir=target.parent))
    try:
        copy_tree(backup, staging / SKILL_NAME)
        if target.exists():
            old = target.parent / f".{SKILL_NAME}.rollback-old-{os.getpid()}"
            os.replace(target, old)
            try:
                os.replace(staging / SKILL_NAME, target)
            except BaseException:
                os.replace(old, target)
                raise
            shutil.rmtree(old, ignore_errors=True)
        else:
            os.replace(staging / SKILL_NAME, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return {"target": str(target), "backup": str(backup), "safety": str(safety) if safety else None}


def backup_root() -> Path:
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=now_id() + "-", dir=BACKUPS))


def compose_command(args: argparse.Namespace, *extra: str, root: Path | None = None) -> list[str]:
    docker = which("docker")
    if not docker:
        raise SystemExit("docker is not installed")
    root = Path(root) if root else ROOT
    command = [docker, "compose"]
    if args.env_file:
        command += ["--env-file", str(Path(args.env_file).expanduser())]
    command += ["-f", str(root / "deploy" / "compose.yaml")]
    if args.gpu:
        command += ["-f", str(root / "deploy" / "compose.gpu.yaml")]
    elif args.cpu:
        command += ["-f", str(root / "deploy" / "compose.cpu.yaml")]
    command += list(extra)
    return command


def apply_install(args: argparse.Namespace) -> int:
    require_apply(args)
    workspace = require_workspace(args)
    plan = build_plan(args)
    if not plan["prerequisites"]["model"]["ok"]:
        raise SystemExit("refusing install: pinned model is not provisioned and verified")
    if args.gpu and args.cpu:
        raise SystemExit("--gpu and --cpu are mutually exclusive")
    if not args.gpu and not args.cpu:
        raise SystemExit("choose --gpu or --cpu explicitly; there is no implicit device mode")
    manifest = load_manifest()
    require_target_gate(args, manifest)
    for label, image, expected in (
        (
            "API",
            (manifest.get("images") or {}).get("api", {}).get("tag"),
            (manifest.get("images") or {}).get("api", {}).get("local_image_id"),
        ),
        (
            "worker",
            (manifest.get("images") or {}).get("worker", {}).get("tag"),
            (manifest.get("images") or {}).get("worker", {}).get("local_image_id"),
        ),
    ):
        if not image_present(image):
            raise SystemExit(f"{label} image is not present locally: {image}; build it first")
        if expected and image_id(image) != expected:
            raise SystemExit(f"{label} image ID does not match the release manifest")
    backup = backup_root() / "skill"
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not skill_target(workspace).exists():
        backup = None  # type: ignore[assignment]
    # Docker operation first: if it fails, the skill has not been replaced.
    result = run(
        compose_command(args, "up", "-d", "--no-build"),
        timeout=300,
        env_overrides=release_env(load_manifest().get("release"), host_model_dir(args)),
    )
    if result.get("returncode") != 0:
        raise SystemExit(
            "docker compose up failed; skill was not changed. "
            f"exit={result.get('returncode')} stderr={result.get('stderr', '')[:300]}"
        )
    live = wait_live(int(env_file_value(args.env_file, "CLAWVOICE_PORT", "8765")))
    readiness = (
        wait_container_ready(args)
        if live.get("reachable")
        else {"ready": False, "reason": "api_unreachable"}
    )
    if not readiness.get("ready"):
        print(
            json.dumps(
                {"mode": "install", "applied": False, "live": live, "readiness": readiness},
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    skill_result = stage_skill(workspace, backup=backup)
    state = load_release_state()
    state.update(
        {
            "release": load_manifest().get("release"),
            "skill_target": skill_result["target"],
            "backup": skill_result["backup"],
            "gpu": bool(args.gpu),
            "cpu": bool(args.cpu),
            "release_root": str(ROOT),
            "model_dir": str(host_model_dir(args)),
            "env_file": str(Path(args.env_file).expanduser().resolve()) if args.env_file else None,
            "installed_at": datetime.now(UTC).isoformat(),
        }
    )
    save_state(state)
    payload = {
        "mode": "install",
        "applied": True,
        "skill": skill_result,
        "compose": result,
        "live": live,
        "readiness": readiness,
        "warnings": plan["warnings"],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if live.get("reachable") else 1


def apply_verify(args: argparse.Namespace) -> int:
    state = load_release_state()
    args = operation_args(args, state)
    root = Path(state.get("release_root", str(ROOT)))
    manifest = json.loads((root / "deploy" / "release-manifest.json").read_text())
    version = state.get("release") or manifest.get("release")
    workspace = (
        Path(args.openclaw_workspace).expanduser().resolve() if args.openclaw_workspace else None
    )
    if workspace is None and state.get("skill_target"):
        workspace = Path(state["skill_target"]).parents[1]
    target = skill_target(workspace)
    diff = skill_diff(target, source=release_skill_source(root)) if target else None
    doctor = run(
        compose_command(
            args, "exec", "-T", "api", "clawvoice", "doctor", "--json", "--require-ready", root=root
        ),
        timeout=180,
        env_overrides=release_env(version, host_model_dir(args)),
    )
    # Inspect running containers, not only locally tagged images. Both services
    # must belong to this release; a stale worker must never pass verification.
    images = {}
    for service in ("api", "worker"):
        spec = manifest.get("images", {}).get(service, {})
        expected = spec.get("local_image_id") or image_id(spec.get("tag"))
        actual = running_image_id(args, service, root, version)
        images[service] = {
            "expected_id": expected,
            "actual_id": actual,
            "ok": bool(expected and actual == expected),
        }
    ok = bool(
        doctor.get("returncode") == 0
        and diff
        and diff["exists"]
        and not diff["drift"]
        and all(x["ok"] for x in images.values())
    )
    print(
        json.dumps(
            {
                "mode": "verify",
                "ok": ok,
                "skill": diff,
                "doctor": doctor,
                "images": images,
                "state": state,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if ok else 1


def apply_upgrade(args: argparse.Namespace) -> int:
    require_apply(args)
    workspace = require_workspace(args)
    old_state = load_release_state()
    if not old_state.get("release"):
        raise SystemExit("no installed release is recorded; use install first")
    args = operation_args(args, old_state)
    previous_args = operation_args(
        argparse.Namespace(
            **{**vars(args), "env_file": None, "model_dir": None, "gpu": False, "cpu": False}
        ),
        old_state,
    )
    new_release = Path(args.release).expanduser().resolve() if args.release else None
    if not new_release or not (new_release / "deploy" / "release-manifest.json").is_file():
        raise SystemExit("--release must point to an extracted release bundle")
    new_manifest = json.loads((new_release / "deploy" / "release-manifest.json").read_text())
    source = release_skill_source(new_release)
    if new_manifest.get("source", {}).get("model_revision") != load_manifest().get(
        "source", {}
    ).get("model_revision"):
        raise SystemExit("model revision changed; run a new compatibility gate before upgrading")
    require_target_gate(args, new_manifest)
    if not args.gpu and not args.cpu:
        raise SystemExit("choose --gpu or --cpu explicitly; there is no implicit device mode")
    for service in ("api", "worker"):
        spec = new_manifest.get("images", {}).get(service, {})
        if not image_present(spec.get("tag")):
            raise SystemExit(f"{service} image is not present locally; build the new release first")
        if spec.get("local_image_id") and image_id(spec["tag"]) != spec["local_image_id"]:
            raise SystemExit(f"{service} image ID does not match the new release manifest")
    old_root = Path(old_state.get("release_root", str(ROOT)))
    result, readiness = start_release(args, new_release, new_manifest.get("release"))
    skill_result = None
    staging_error = None
    if result.get("returncode") == 0 and readiness.get("ready"):
        backup = backup_root() / "skill" if skill_target(workspace).exists() else None
        try:
            skill_result = stage_skill(workspace, backup=backup, source=source)
        except Exception as exc:
            staging_error = type(exc).__name__
    if skill_result is None:
        rollback, rollback_ready = start_release(previous_args, old_root, old_state["release"])
        print(
            json.dumps(
                {
                    "mode": "upgrade",
                    "applied": False,
                    "compose": result,
                    "readiness": readiness,
                    "skill_error": staging_error,
                    "rollback": {"compose": rollback, "readiness": rollback_ready},
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    state = {
        "release": new_manifest.get("release"),
        "previous_release": old_state["release"],
        "skill_target": skill_result["target"],
        "backup": skill_result["backup"],
        "gpu": bool(args.gpu),
        "cpu": bool(args.cpu),
        "release_root": str(new_release),
        "previous_root": str(old_root),
        "model_dir": str(host_model_dir(args)),
        "env_file": str(Path(args.env_file).expanduser().resolve()) if args.env_file else None,
        "previous_settings": {k: old_state.get(k) for k in ("model_dir", "env_file", "gpu", "cpu")},
        "upgraded_at": datetime.now(UTC).isoformat(),
    }
    save_state(state)
    print(
        json.dumps(
            {
                "mode": "upgrade",
                "applied": True,
                "skill": skill_result,
                "compose": result,
                "readiness": readiness,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def apply_rollback(args: argparse.Namespace) -> int:
    require_apply(args)
    workspace = require_workspace(args)
    state = load_release_state()
    backup = Path(args.backup).expanduser().resolve() if args.backup else None
    if backup is None and state.get("backup"):
        backup = Path(state["backup"]).resolve()
    if backup is None or not backup.is_dir():
        raise SystemExit("no retained skill backup found")
    try:
        backup.relative_to(BACKUPS.resolve())
    except ValueError:
        raise SystemExit("rollback backup must be under deploy/backups") from None
    previous_release = state.get("previous_release")
    if not previous_release:
        raise SystemExit("no previous release is recorded")
    previous_root = Path(state.get("previous_root", str(ROOT))).expanduser()
    if not (previous_root / "deploy" / "compose.yaml").is_file():
        raise SystemExit(f"previous release root is not available: {previous_root}")
    previous_settings = state.get("previous_settings") or state
    args = operation_args(args, previous_settings)
    result, readiness = start_release(args, previous_root, previous_release)
    restored = None
    rollback_recovery = None
    if result.get("returncode") == 0 and readiness.get("ready"):
        try:
            restored = restore_skill(workspace, backup)
        except Exception:
            current_args = operation_args(
                argparse.Namespace(
                    **{
                        **vars(args),
                        "env_file": None,
                        "model_dir": None,
                        "gpu": False,
                        "cpu": False,
                    }
                ),
                state,
            )
            start_release(
                current_args, Path(state.get("release_root", str(ROOT))), state["release"]
            )
            raise
    else:
        current_args = operation_args(
            argparse.Namespace(
                **{**vars(args), "env_file": None, "model_dir": None, "gpu": False, "cpu": False}
            ),
            state,
        )
        recovery, recovery_ready = start_release(
            current_args, Path(state.get("release_root", str(ROOT))), state["release"]
        )
        rollback_recovery = {"compose": recovery, "readiness": recovery_ready}
    if result.get("returncode") == 0 and readiness.get("ready"):
        state["release"] = previous_release
        state["previous_release"] = None
        state["release_root"] = str(previous_root)
        state.update({k: previous_settings.get(k) for k in ("model_dir", "env_file", "gpu", "cpu")})
        state["rolled_back_at"] = datetime.now(UTC).isoformat()
        state["backup"] = str(backup)
        save_state(state)
    payload = {
        "mode": "rollback",
        "recovery": rollback_recovery,
        "applied": bool(result.get("returncode") == 0 and readiness.get("ready")),
        "skill": restored,
        "release": previous_release,
        "compose": result,
        "readiness": readiness,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["applied"] else 1


def apply_uninstall(args: argparse.Namespace) -> int:
    require_apply(args)
    command = compose_command(args, "down")
    volume_name = "openclaw-chatterbox_clawvoice-data"
    if args.delete_data and args.confirm_delete_data != volume_name:
        raise SystemExit(
            f"--delete-data requires --confirm-delete-data {volume_name}; "
            "only that project data volume is removed"
        )
    result = run(command, timeout=300)
    if result.get("returncode") != 0:
        print(
            json.dumps(
                {"mode": "uninstall", "applied": False, "compose": result},
                indent=2,
                sort_keys=True,
            )
        )
        return 1
    volume_result = None
    if args.delete_data:
        docker = which("docker")
        volume_result = (
            run([docker, "volume", "rm", volume_name], timeout=60)
            if docker
            else {"returncode": 127}
        )
        if volume_result.get("returncode") != 0:
            print(
                json.dumps(
                    {
                        "mode": "uninstall",
                        "applied": False,
                        "compose": result,
                        "volume": volume_result,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1
    workspace = require_workspace(args) if args.openclaw_workspace else None
    target = skill_target(workspace) if workspace else None
    removed_skill = False
    if target and target.exists():
        backup = backup_root() / "uninstalled-skill"
        copy_tree(target, backup)
        shutil.rmtree(target)
        removed_skill = True
    print(
        json.dumps(
            {
                "mode": "uninstall",
                "applied": True,
                "compose": result,
                "volume": volume_result,
                "skill_removed": removed_skill,
                "data_retained": not args.delete_data,
                "backup_root": str(BACKUPS),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clawbox", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "install", "verify", "upgrade", "rollback", "uninstall"):
        p = sub.add_parser(name)
        p.add_argument("--json", action="store_true")
        p.add_argument("--apply", action="store_true")
        p.add_argument("--openclaw-workspace")
        p.add_argument("--agent", default="main")
        p.add_argument("--env-file")
        p.add_argument("--model-dir")
        p.add_argument("--allow-unverified-target", action="store_true")
        gpu_group = p.add_mutually_exclusive_group()
        gpu_group.add_argument("--gpu", action="store_true")
        gpu_group.add_argument("--cpu", action="store_true")
        if name == "upgrade":
            p.add_argument("--release", required=True)
        if name == "rollback":
            p.add_argument("--backup")
        if name == "uninstall":
            p.add_argument("--delete-data", action="store_true")
            p.add_argument("--confirm-delete-data")
        p.set_defaults(func=name)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.func == "plan":
            print(json.dumps(build_plan(args), indent=2, sort_keys=True))
            return 0
        if args.func == "install":
            return apply_install(args)
        if args.func == "verify":
            return apply_verify(args)
        if args.func == "upgrade":
            return apply_upgrade(args)
        if args.func == "rollback":
            return apply_rollback(args)
        if args.func == "uninstall":
            return apply_uninstall(args)
        return 2
    except SystemExit:
        raise
    except Exception as exc:  # CLI boundary; no secret values included
        print(f"clawbox: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
