from __future__ import annotations

import stat
from pathlib import Path

import pytest

from clawvoice.config import Settings, read_service_token
from clawvoice.errors import ConfigurationError
from clawvoice.util import new_secret


def _settings(tmp_path: Path, **overrides) -> Settings:
    data = tmp_path / "data"
    values = dict(
        data_dir=data,
        db_path=data / "db.sqlite3",
        model_dir=tmp_path / "model",
        token_file=data / "secrets" / "token",
        service_token=new_secret(32),
        backend="fake",
        device="cpu",
        allow_fake_backend=True,
    )
    values.update(overrides)
    return Settings(**values)


def test_fake_backend_requires_explicit_opt_in(tmp_path):
    with pytest.raises(ConfigurationError):
        _settings(tmp_path, backend="fake", allow_fake_backend=False).validate()
    _settings(tmp_path).validate()


def test_unknown_model_revision_is_refused(tmp_path):
    with pytest.raises(ConfigurationError):
        _settings(tmp_path, model_revision="0" * 40).validate()


def test_wildcard_origins_are_refused(tmp_path):
    settings = _settings(tmp_path, allowed_origins=("*",))
    with pytest.raises(ConfigurationError):
        settings.validate()


def test_token_generated_with_owner_only_mode(tmp_path):
    path = tmp_path / "secret" / "token"
    token = read_service_token(path, create=True)
    assert len(token) >= 24
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    assert read_service_token(path, create=False) == token


def test_insecure_token_mode_is_refused(tmp_path):
    path = tmp_path / "token"
    path.write_text("x" * 40)
    path.chmod(0o644)
    with pytest.raises(ConfigurationError):
        read_service_token(path, create=False)


def test_missing_token_is_a_configuration_error(tmp_path):
    with pytest.raises(ConfigurationError):
        read_service_token(tmp_path / "missing", create=False)


def test_environment_token_must_be_long(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAWVOICE_SERVICE_TOKEN", "short")
    with pytest.raises(ConfigurationError):
        read_service_token(tmp_path / "unused", create=False)


def test_v1_database_migration_adds_job_reference_column(tmp_path):
    import sqlite3

    from clawvoice.db import Database

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            voice_id TEXT,
            created_at INTEGER NOT NULL
        );
        PRAGMA user_version = 1;
        """
    )
    conn.commit()
    conn.close()
    db = Database(path)
    db.initialize()
    with db.connect() as check:
        columns = {row[1] for row in check.execute("PRAGMA table_info(jobs)")}
        version = check.execute("PRAGMA user_version").fetchone()[0]
    assert "reference_version_id" in columns
    assert version == 2
