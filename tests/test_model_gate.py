from __future__ import annotations

import pytest

from clawvoice.backends.turbo import TurboBackend
from clawvoice.errors import ConfigurationError, ModelUnavailable
from clawvoice.modelspec import TURBO_REVISION


def test_turbo_backend_refuses_missing_provisioning_without_importing_torch(settings):
    backend = TurboBackend(settings)
    with pytest.raises(ModelUnavailable):
        backend.load()
    assert backend.state().status == "model_missing"
    assert backend.identity()["revision"] == TURBO_REVISION


def test_api_requires_a_real_token(tmp_path):
    from clawvoice.api import create_app
    from clawvoice.config import Settings

    settings = Settings(
        data_dir=tmp_path / "d",
        db_path=tmp_path / "d" / "db.sqlite3",
        model_dir=tmp_path / "m",
        token_file=tmp_path / "t",
        service_token="short",
        backend="fake",
        device="cpu",
        allow_fake_backend=True,
    )
    with pytest.raises(ConfigurationError):
        create_app(settings, start_worker=False)
