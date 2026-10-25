from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from clawvoice.api import create_app
from clawvoice.config import Settings
from clawvoice.util import new_secret
from tests.helpers import (  # noqa: F401
    wait_job,
    wait_ready,
    write_wav,  # noqa: E402,F401
)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    data = tmp_path / "data"
    return Settings(
        data_dir=data,
        db_path=data / "clawvoice.sqlite3",
        model_dir=tmp_path / "models" / "turbo",
        token_file=data / "secrets" / "service-token",
        service_token=new_secret(32),
        backend="fake",
        device="cpu",
        allow_fake_backend=True,
        web_dir=Path(__file__).resolve().parents[1] / "web",
        max_queue_depth=8,
        enrollment_ttl_seconds=60,
        artifact_retention_days=7,
    )


@pytest.fixture()
def app(settings: Settings):
    application = create_app(settings, start_worker=True)
    yield application


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def headers(settings: Settings) -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.service_token}"}


@pytest.fixture()
def reference(tmp_path: Path) -> Path:
    return write_wav(tmp_path / "reference.wav")
