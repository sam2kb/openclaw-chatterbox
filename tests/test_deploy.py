from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("clawbox_installer", ROOT / "deploy" / "clawbox.py")
assert SPEC and SPEC.loader
clawbox = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(clawbox)


def args_for(tmp_path: Path, workspace: Path | None = None, command: str = "plan"):
    return argparse.Namespace(
        command=command,
        apply=False,
        json=False,
        openclaw_workspace=str(workspace) if workspace else None,
        agent="main",
        env_file=None,
        gpu=False,
        cpu=False,
        func=command,
        release=None,
        backup=None,
        delete_data=False,
        confirm_delete_data=None,
    )


def stub_plan(monkeypatch, *, model_ok=True):
    monkeypatch.setattr(clawbox, "model_report", lambda: {"ok": model_ok, "revision": "pinned", "problems": []})
    monkeypatch.setattr(
        clawbox,
        "docker_report",
        lambda: {
            "docker": "/usr/bin/docker",
            "compose_version": {"returncode": 0, "stdout": "Docker Compose version v9"},
            "daemon": {"returncode": 0, "stdout": "26"},
        },
    )
    monkeypatch.setattr(clawbox, "service_live", lambda port: {"reachable": False})
    monkeypatch.setattr(clawbox, "port_status", lambda port: {"port": port, "listening": False})


def test_plan_is_read_only_and_reports_drift(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    stub_plan(monkeypatch)
    before = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*"))
    plan = clawbox.build_plan(args_for(tmp_path, workspace))
    after = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*"))
    assert before == after
    assert plan["mode"] == "plan"
    assert plan["side_effects"] == "none"
    assert plan["skill"]["drift"] is True  # desired files are not installed yet
    assert plan["prerequisites"]["model"]["ok"] is True
    assert plan["release"]["target_support_verified"] is False
    assert any("target GPU support is unverified" in item for item in plan["warnings"])


def test_stage_skill_backs_up_and_restores(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    first = clawbox.stage_skill(workspace, backup=None)
    target = workspace / "skills" / "chatterbox-voice"
    assert (target / "SKILL.md").is_file()
    assert first["installed"] is True

    modified = target / "SKILL.md"
    original = modified.read_text()
    modified.write_text(original + "\nlocal drift\n")
    backup = tmp_path / "backup" / "skill"
    backup.parent.mkdir()
    clawbox.stage_skill(workspace, backup=backup)
    assert "local drift" not in (target / "SKILL.md").read_text()
    assert "local drift" in (backup / "SKILL.md").read_text()

    modified.write_text("drift again\n")
    with pytest.raises(RuntimeError):
        clawbox.stage_skill(workspace, backup=None)


def test_install_refuses_without_apply_and_coder(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    stub_plan(monkeypatch)
    with pytest.raises(SystemExit):
        clawbox.apply_install(args_for(tmp_path, workspace, command="install"))

    args = args_for(tmp_path, workspace, command="install")
    args.apply = True
    args.agent = "coder"
    with pytest.raises(SystemExit):
        clawbox.require_workspace(args)


def test_install_refuses_unprovisioned_model(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    stub_plan(monkeypatch, model_ok=False)
    args = args_for(tmp_path, workspace, command="install")
    args.apply = True
    with pytest.raises(SystemExit):
        clawbox.apply_install(args)


def test_skill_release_files_match_manifest_identity():
    manifest = clawbox.load_manifest()
    assert manifest["source"]["model_revision"]
    assert (ROOT / "skills" / "chatterbox-voice" / "SKILL.md").is_file()
    skill = (ROOT / "skills" / "chatterbox-voice" / "SKILL.md").read_text()
    assert "metadata:" in skill
    assert "openclaw" in skill
    assert "does **not**" in skill


def test_rollback_restores_skill_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(clawbox, "backup_root", lambda: tmp_path / "rollback-safety")
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    clawbox.stage_skill(workspace, backup=None)
    target = workspace / "skills" / "chatterbox-voice" / "SKILL.md"
    original = target.read_text()
    backup = tmp_path / "backup" / "skill"
    backup.parent.mkdir()
    clawbox.copy_tree(target.parent, backup)
    target.write_text("broken install\n")
    restored = clawbox.restore_skill(workspace, backup)
    assert target.read_text() == original
    assert restored["backup"] == str(backup)
