"""Inference backend interface. No implementation imports Torch at module load."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


class BackendNotReady(RuntimeError):
    pass


@dataclass
class RenderResult:
    pcm_s16le: bytes
    sample_rate: int
    metrics: dict[str, Any] = field(default_factory=dict)


@dataclass
class BackendState:
    status: str
    detail: dict[str, Any] = field(default_factory=dict)
    loaded: bool = False


class Backend(Protocol):
    name: str
    sample_rate: int

    def load(self) -> None: ...

    def render(
        self,
        *,
        reference_path: Path,
        text: str,
        controls: dict[str, Any],
        seed: int,
    ) -> RenderResult: ...

    def state(self) -> BackendState: ...

    def identity(self) -> dict[str, Any]: ...

    def shutdown(self) -> None: ...
