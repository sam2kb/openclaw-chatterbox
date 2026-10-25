"""Backend factory. Importing this module never imports Torch."""

from __future__ import annotations

from ..config import Settings
from .base import Backend


def create_backend(settings: Settings) -> Backend:
    if settings.backend == "fake":
        from .fake import FakeBackend

        return FakeBackend(settings)
    if settings.backend == "turbo":
        from .turbo import TurboBackend

        return TurboBackend(settings)
    raise ValueError(f"unsupported backend: {settings.backend}")


__all__ = ["Backend", "create_backend"]
