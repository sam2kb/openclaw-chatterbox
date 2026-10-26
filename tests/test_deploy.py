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
    monkeypatch.setattr(
        clawbox,
        "model_report",
        lambda *a, **k: {"ok": model_ok, "revision": "pinned", "problems": []},
    )
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


@pytest.mark.parametrize("target_verified", [False, True])
def test_plan_is_read_only_and_reports_drift(tmp_path, monkeypatch, target_verified):
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    stub_plan(monkeypatch)
    manifest = clawbox.load_manifest()
    manifest["runtime"]["target_support_verified"] = target_verified
    monkeypatch.setattr(clawbox, "load_manifest", lambda: manifest)
    before = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*"))
    plan = clawbox.build_plan(args_for(tmp_path, workspace))
    after = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*"))
    assert before == after
    assert plan["mode"] == "plan"
    assert plan["side_effects"] == "none"
    assert plan["skill"]["drift"] is True  # desired files are not installed yet
    assert plan["prerequisites"]["model"]["ok"] is True
    assert plan["release"]["target_support_verified"] is target_verified
    assert any("target GPU support is unverified" in item for item in plan["warnings"]) is (not target_verified)


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
    assert "does not train model weights" in " ".join(skill.replace("**", "").split())


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


def _installed_state(tmp_path, workspace):
    import json

    old_root = tmp_path / "previous-release"
    (old_root / "deploy").mkdir(parents=True, exist_ok=True)
    previous_manifest = clawbox.load_manifest()
    previous_manifest["release"] = "0.1.0"
    (old_root / "deploy/release-manifest.json").write_text(json.dumps(previous_manifest))
    (old_root / "deploy/compose.yaml").write_text((ROOT / "deploy/compose.yaml").read_text())
    return {
        "release": "0.1.0",
        "release_root": str(old_root),
        "skill_target": str(workspace / "skills" / "chatterbox-voice"),
        "gpu": True,
        "cpu": False,
        "model_dir": str(tmp_path / "persistent-models" / "chatterbox-turbo"),
    }


def _new_release(tmp_path):
    import json

    release = tmp_path / "release"
    (release / "deploy").mkdir(parents=True)
    data = clawbox.load_manifest()
    data["release"] = "0.1.1"
    (release / "deploy" / "release-manifest.json").write_text(json.dumps(data))
    source = release / "skills" / "chatterbox-voice"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("new release skill\n")
    return release


def test_upgrade_stages_source_layout_only_after_new_service_is_ready(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    clawbox.stage_skill(workspace, backup=None)
    old = _installed_state(tmp_path, workspace)
    release = _new_release(tmp_path)
    monkeypatch.setattr(clawbox, "load_release_state", lambda: old)
    monkeypatch.setattr(clawbox, "image_present", lambda image: True)
    monkeypatch.setattr(
        clawbox,
        "image_id",
        lambda image: next(
            spec.get("local_image_id")
            for spec in clawbox.load_manifest()["images"].values()
            if spec["tag"] == image
        ),
    )
    monkeypatch.setattr(clawbox, "backup_root", lambda: tmp_path / "backup")
    calls = []

    def start(args, root, version):
        calls.append((root, version, args.model_dir))
        assert (workspace / "skills/chatterbox-voice/SKILL.md").read_text() != "new release skill\n"
        return {"returncode": 0}, {"ready": True}

    saved = []
    monkeypatch.setattr(clawbox, "start_release", start)
    monkeypatch.setattr(clawbox, "save_state", saved.append)
    args = args_for(tmp_path, workspace, "upgrade")
    args.apply = args.allow_unverified_target = True
    args.release = str(release)
    assert clawbox.apply_upgrade(args) == 0
    assert calls == [(release, "0.1.1", old["model_dir"])]
    assert (workspace / "skills/chatterbox-voice/SKILL.md").read_text() == "new release skill\n"
    assert saved[0]["release_root"] == str(release)
    assert saved[0]["model_dir"] == old["model_dir"]


def test_failed_upgrade_restores_service_without_replacing_skill(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    clawbox.stage_skill(workspace, backup=None)
    target = workspace / "skills/chatterbox-voice/SKILL.md"
    original = target.read_bytes()
    old = _installed_state(tmp_path, workspace)
    release = _new_release(tmp_path)
    monkeypatch.setattr(clawbox, "load_release_state", lambda: old)
    monkeypatch.setattr(clawbox, "image_present", lambda image: True)
    monkeypatch.setattr(
        clawbox,
        "image_id",
        lambda image: next(
            spec.get("local_image_id")
            for spec in clawbox.load_manifest()["images"].values()
            if spec["tag"] == image
        ),
    )
    calls = []

    def start(args, root, version):
        calls.append(version)
        return {"returncode": 0}, {"ready": version == "0.1.0"}

    monkeypatch.setattr(clawbox, "start_release", start)
    monkeypatch.setattr(
        clawbox, "save_state", lambda state: pytest.fail("failed upgrade changed state")
    )
    args = args_for(tmp_path, workspace, "upgrade")
    args.apply = args.allow_unverified_target = True
    args.release = str(release)
    assert clawbox.apply_upgrade(args) == 1
    assert calls == ["0.1.1", "0.1.0"]
    assert target.read_bytes() == original


def test_verify_rejects_a_stale_running_worker(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    clawbox.stage_skill(workspace, backup=None)
    manifest = clawbox.load_manifest()
    expected = {service: f"sha256:{service}-new" for service in ("api", "worker")}
    for service, value in expected.items():
        manifest["images"][service]["local_image_id"] = value
    monkeypatch.setattr(
        clawbox, "load_release_state", lambda: _installed_state(tmp_path, workspace)
    )
    # Return distinct test image identities for the release being verified.
    import json

    root = tmp_path / "installed"
    (root / "deploy").mkdir(parents=True)
    (root / "deploy/release-manifest.json").write_text(json.dumps(manifest))
    clawbox.copy_tree(ROOT / "skills/chatterbox-voice", root / "skills/chatterbox-voice")
    state = _installed_state(tmp_path, workspace)
    state["release_root"] = str(root)
    monkeypatch.setattr(clawbox, "load_release_state", lambda: state)
    monkeypatch.setattr(clawbox, "run", lambda *a, **k: {"returncode": 0})
    monkeypatch.setattr(clawbox, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(
        clawbox,
        "running_image_id",
        lambda args, service, root, version: (
            expected["api"] if service == "api" else "sha256:worker-old"
        ),
    )
    assert clawbox.apply_verify(args_for(tmp_path, workspace, "verify")) == 1


def test_backup_directories_do_not_collide_within_one_second(tmp_path, monkeypatch):
    monkeypatch.setattr(clawbox, "BACKUPS", tmp_path / "backups")
    monkeypatch.setattr(clawbox, "now_id", lambda: "same-second")
    first = clawbox.backup_root()
    second = clawbox.backup_root()
    assert first != second
    assert first.is_dir() and second.is_dir()


def test_release_environment_pins_model_mount_to_absolute_existing_location(tmp_path):
    model = tmp_path / "models" / "chatterbox-turbo"
    env = clawbox.release_env("0.1.1", model)
    assert env == {"CLAWVOICE_RELEASE": "0.1.1", "CLAWVOICE_MODELS_DIR": str(model.parent)}


def test_failed_rollback_recovers_current_service_without_replacing_skill(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    clawbox.stage_skill(workspace, backup=None)
    backups = tmp_path / "backups"
    backup = backups / "previous" / "skill"
    backup.mkdir(parents=True)
    current = _new_release(tmp_path)
    state = _installed_state(tmp_path, workspace)
    state.update(
        release="0.1.1",
        previous_release="0.1.0",
        release_root=str(current),
        previous_root=state["release_root"],
        backup=str(backup),
    )
    monkeypatch.setattr(clawbox, "BACKUPS", backups)
    monkeypatch.setattr(clawbox, "load_release_state", lambda: state)
    monkeypatch.setattr(
        clawbox, "restore_skill", lambda *a, **k: pytest.fail("unready rollback replaced skill")
    )
    monkeypatch.setattr(
        clawbox, "save_state", lambda value: pytest.fail("failed rollback changed state")
    )
    calls = []

    def start(args, root, version):
        calls.append(version)
        return {"returncode": 0}, {"ready": version == "0.1.1"}

    monkeypatch.setattr(clawbox, "start_release", start)
    args = args_for(tmp_path, workspace, "rollback")
    args.apply = True
    assert clawbox.apply_rollback(args) == 1
    assert calls == ["0.1.0", "0.1.1"]


def test_verified_snapshot_rejects_changed_archive(tmp_path, monkeypatch):
    import hashlib
    import json

    backups = tmp_path / "backups"
    directory = backups / "snapshot" / "data"
    directory.mkdir(parents=True)
    monkeypatch.setattr(clawbox, "BACKUPS", backups)
    archive = directory / "data.tar.gz"
    archive.write_bytes(b"consistent test snapshot")
    manifest = directory / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "consistent": True,
                "sqlite_quick_check": "ok",
                "data_schema_version": 2,
                "volume": "openclaw-chatterbox_clawvoice-data",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            }
        )
    )
    assert clawbox.validate_data_backup(manifest, 2)["data_schema_version"] == 2
    archive.write_bytes(b"changed")
    with pytest.raises(SystemExit, match="checksum"):
        clawbox.validate_data_backup(manifest, 2)


def test_schema_upgrade_failure_never_launches_old_app_against_new_data(tmp_path, monkeypatch):
    import json

    workspace = tmp_path / "workspace"
    (workspace / "skills").mkdir(parents=True)
    clawbox.stage_skill(workspace, backup=None)
    state = _installed_state(tmp_path, workspace)
    old_manifest_path = Path(state["release_root"]) / "deploy/release-manifest.json"
    old_manifest = json.loads(old_manifest_path.read_text())
    old_manifest["data_schema_version"] = 2
    old_manifest_path.write_text(json.dumps(old_manifest))
    release = _new_release(tmp_path)
    new_manifest_path = release / "deploy/release-manifest.json"
    new_manifest = json.loads(new_manifest_path.read_text())
    new_manifest["data_schema_version"] = 3
    new_manifest_path.write_text(json.dumps(new_manifest))
    monkeypatch.setattr(clawbox, "load_release_state", lambda: state)
    monkeypatch.setattr(clawbox, "image_present", lambda image: True)
    monkeypatch.setattr(
        clawbox,
        "image_id",
        lambda image: next(
            s.get("local_image_id") for s in new_manifest["images"].values() if s["tag"] == image
        ),
    )
    monkeypatch.setattr(clawbox, "validate_data_backup", lambda *a: {"consistent": True})
    monkeypatch.setattr(
        clawbox, "save_state", lambda *a: pytest.fail("failed upgrade changed state")
    )
    calls = []

    def start(args, root, version):
        calls.append(root)
        return {"returncode": 0}, {"ready": False}

    monkeypatch.setattr(clawbox, "start_release", start)
    args = args_for(tmp_path, workspace, "upgrade")
    args.apply = args.allow_unverified_target = True
    args.release = str(release)
    args.data_backup = str(tmp_path / "snapshot/manifest.json")
    assert clawbox.apply_upgrade(args) == 1
    assert calls == [release]


def test_schema_upgrade_without_snapshot_stops_before_service_change(tmp_path, monkeypatch):
    import json

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = _installed_state(tmp_path, workspace)
    old = Path(state["release_root"]) / "deploy/release-manifest.json"
    data = json.loads(old.read_text())
    data["data_schema_version"] = 2
    old.write_text(json.dumps(data))
    release = _new_release(tmp_path)
    target = release / "deploy/release-manifest.json"
    data = json.loads(target.read_text())
    data["data_schema_version"] = 3
    target.write_text(json.dumps(data))
    monkeypatch.setattr(clawbox, "load_release_state", lambda: state)
    monkeypatch.setattr(clawbox, "image_present", lambda image: True)
    monkeypatch.setattr(
        clawbox,
        "image_id",
        lambda image: next(
            s.get("local_image_id") for s in data["images"].values() if s["tag"] == image
        ),
    )
    monkeypatch.setattr(clawbox, "start_release", lambda *a: pytest.fail("snapshot was skipped"))
    args = args_for(tmp_path, workspace, "upgrade")
    args.apply = args.allow_unverified_target = True
    args.release = str(release)
    with pytest.raises(SystemExit, match="verified snapshot"):
        clawbox.apply_upgrade(args)
