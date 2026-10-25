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


def skill_diff(target: Path) -> dict[str, Any]:
    desired = hash_tree(SKILL_SRC)
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
        "skill_candidates": [
            {"path": str(path), "present": path.is_dir()} for path in candidates
        ],
        "note": "Version/policy inventory is read-only. Confirm allowlists and sandbox behavior before install.",
    }


def model_report() -> dict[str, Any]:
    cli = find_cli()
    if not cli:
        return {"ok": False, "reason": "clawvoice_not_on_path"}
    result = run(
        [cli, "models", "verify", "--backend", "turbo", "--json"],
        timeout=180,
    )
    try:
        payload = json.loads(result.get("stdout") or "{}")
    except json.JSONDecodeError:
        payload = {}
    return {
        "ok": bool(payload.get("ok")),
        "revision": payload.get("revision"),
        "problems": payload.get("problems", [])[:12],
        "command_returncode": result.get("returncode"),
    }


def service_live(port: int) -> dict[str, Any]:
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/health/live", timeout=3) as resp:
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


def wait_live(port: int, *, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {"reachable": False}
    while time.monotonic() < deadline:
        last = service_live(port)
        if last.get("reachable"):
            return last
        time.sleep(1.0)
    return last


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
    model = model_report()
    port = int(env_file_value(args.env_file, "CLAWVOICE_PORT", "8765"))
    docker = docker_report()
    overrides = ["--gpu"] if args.gpu else (["--cpu"] if args.cpu else [])
    compose_files = ["-f", str(COMPOSE), "-f", str(GPU if args.gpu else CPU)] if overrides else ["-f", str(COMPOSE)]
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
            "target_support_verified": bool((manifest.get("runtime") or {}).get("target_support_verified")),
        },
        "prerequisites": {
            "docker": docker,
            "model": model,
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
            "gpu_overlay": bool(args.gpu),
            "cpu_overlay": bool(args.cpu),
            "env_file": args.env_file,
            "volume": "openclaw-chatterbox_clawvoice-data",
            "volume_owner_uid": 10001,
            "api_image": (manifest.get("images") or {}).get("api", {}).get("tag"),
            "api_image_id": (manifest.get("images") or {}).get("api", {}).get("local_image_id"),
            "worker_image": (manifest.get("images") or {}).get("worker", {}).get("tag"),
            "worker_image_id": (manifest.get("images") or {}).get("worker", {}).get("local_image_id"),
        },
        "requirements": {
            "model_download_bytes_approx": 4_300_000_000,
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
    return plan


def require_apply(args: argparse.Namespace) -> None:
    if not args.apply:
        raise SystemExit("refusing to mutate live state without --apply")


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
    return workspace


def stage_skill(workspace: Path, *, backup: Path | None) -> dict[str, Any]:
    target = skill_target(workspace)
    assert target is not None
    skills_dir = target.parent
    skills_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{SKILL_NAME}.stage-", dir=skills_dir))
    try:
        staged_skill = staging / SKILL_NAME
        copy_tree(SKILL_SRC, staged_skill)
        diff = skill_diff(staged_skill)
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


def release_env(version: str | None) -> dict[str, str] | None:
    return {"CLAWVOICE_RELEASE": version} if version else None


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
    path = BACKUPS / now_id()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def compose_command(args: argparse.Namespace, *extra: str) -> list[str]:
    docker = which("docker")
    if not docker:
        raise SystemExit("docker is not installed")
    command = [docker, "compose"]
    if args.env_file:
        command += ["--env-file", str(Path(args.env_file).expanduser())]
    command += ["-f", str(COMPOSE)]
    if args.gpu:
        command += ["-f", str(GPU)]
    elif args.cpu:
        command += ["-f", str(CPU)]
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
    backup = backup_root() / "skill"
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not skill_target(workspace).exists():
        backup = None  # type: ignore[assignment]
    # Docker operation first: if it fails, the skill has not been replaced.
    result = run(
        compose_command(args, "up", "-d", "--no-build"),
        timeout=300,
        env_overrides=release_env(load_manifest().get("release")),
    )
    if result.get("returncode") != 0:
        raise SystemExit(
            "docker compose up failed; skill was not changed. "
            f"exit={result.get('returncode')} stderr={result.get('stderr', '')[:300]}"
        )
    skill_result = stage_skill(workspace, backup=backup)
    live = wait_live(int(env_file_value(args.env_file, "CLAWVOICE_PORT", "8765")))
    state = load_release_state()
    state.update(
        {
            "release": load_manifest().get("release"),
            "skill_target": skill_result["target"],
            "backup": skill_result["backup"],
            "gpu": bool(args.gpu),
            "cpu": bool(args.cpu),
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
        "warnings": plan["warnings"],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if live.get("reachable") else 1


def apply_verify(args: argparse.Namespace) -> int:
    workspace = Path(args.openclaw_workspace).expanduser().resolve() if args.openclaw_workspace else None
    target = skill_target(workspace)
    diff = skill_diff(target) if target else None
    cli = find_cli()
    doctor = run([cli, "doctor", "--json"], timeout=180) if cli else None
    compose_ps = run(compose_command(args, "ps", "--format", "json"), timeout=30)
    ok = bool(
        doctor
        and doctor.get("returncode") == 0
        and diff
        and diff.get("exists")
        and not diff.get("drift")
    )
    payload = {
        "mode": "verify",
        "ok": ok,
        "skill": diff,
        "doctor": doctor,
        "compose_ps": compose_ps,
        "state": load_release_state(),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if ok else 1


def apply_upgrade(args: argparse.Namespace) -> int:
    require_apply(args)
    workspace = require_workspace(args)
    new_release = Path(args.release).expanduser().resolve() if args.release else None
    if not new_release or not (new_release / "deploy" / "release-manifest.json").is_file():
        raise SystemExit("--release must point to an extracted release bundle")
    new_manifest = json.loads(
        (new_release / "deploy" / "release-manifest.json").read_text(encoding="utf-8")
    )
    if (new_manifest.get("source") or {}).get("model_revision") != (load_manifest().get("source") or {}).get("model_revision"):
        raise SystemExit("model revision changed; run a new compatibility gate before upgrading")
    old_state = load_release_state()
    backup = backup_root() / "skill"
    backup.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not skill_target(workspace).exists():
        backup = None  # type: ignore[assignment]
    # Stage the new skill first; no service change until it validates.
    global SKILL_SRC
    saved_src = SKILL_SRC
    try:
        SKILL_SRC = new_release / "skill" / SKILL_NAME
        skill_result = stage_skill(workspace, backup=backup)
    finally:
        SKILL_SRC = saved_src
    result = run(
        compose_command(args, "up", "-d", "--no-build"),
        timeout=300,
        env_overrides=release_env(new_manifest.get("release")),
    )
    state = {
        "release": new_manifest.get("release"),
        "previous_release": old_state.get("release"),
        "skill_target": skill_result["target"],
        "backup": skill_result["backup"],
        "gpu": bool(args.gpu),
        "cpu": bool(args.cpu),
        "upgraded_at": datetime.now(UTC).isoformat(),
    }
    save_state(state)
    payload = {"mode": "upgrade", "applied": True, "skill": skill_result, "compose": result}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result.get("returncode") == 0 else 1


def apply_rollback(args: argparse.Namespace) -> int:
    require_apply(args)
    workspace = require_workspace(args)
    state = load_release_state()
    backup = Path(args.backup).expanduser().resolve() if args.backup else None
    if backup is None and state.get("backup"):
        backup = Path(state["backup"])
    if backup is None or not backup.is_dir():
        raise SystemExit("no retained skill backup found")
    restored = restore_skill(workspace, backup)
    previous_release = state.get("previous_release") or state.get("release")
    result = run(
        compose_command(args, "up", "-d", "--no-build"),
        timeout=300,
        env_overrides=release_env(previous_release),
    )
    state["release"] = previous_release
    state["rolled_back_at"] = datetime.now(UTC).isoformat()
    state["backup"] = str(backup)
    save_state(state)
    payload = {
        "mode": "rollback",
        "applied": result.get("returncode") == 0,
        "skill": restored,
        "release": previous_release,
        "compose": result,
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if result.get("returncode") == 0 else 1


def apply_uninstall(args: argparse.Namespace) -> int:
    require_apply(args)
    command = compose_command(args, "down")
    if args.delete_data:
        if args.confirm_delete_data != "clawvoice-data":
            raise SystemExit("--delete-data requires --confirm-delete-data clawvoice-data")
        command.append("-v")
    result = run(command, timeout=300)
    workspace = Path(args.openclaw_workspace).expanduser().resolve() if args.openclaw_workspace else None
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
                "applied": result.get("returncode") == 0,
                "compose": result,
                "skill_removed": removed_skill,
                "data_retained": not args.delete_data,
                "backup_root": str(BACKUPS),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if result.get("returncode") == 0 else 1


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
