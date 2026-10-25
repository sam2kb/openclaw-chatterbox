"""Deterministic synthetic backend for tests/CI only.

It is not Chatterbox, does not clone a voice, and cannot prove audio quality,
watermarking, CUDA support, latency, or memory use. The service refuses to use
it unless CLAWVOICE_ALLOW_FAKE=1 is explicit.
"""

from __future__ import annotations

import hashlib
import math
import struct
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import BackendOOM, ConfigurationError, InferenceError
from ..modelspec import TURBO_SAMPLE_RATE
from ..util import sha256_file
from .base import BackendState, RenderResult


class FakeBackend:
    name = "fake"
    sample_rate = TURBO_SAMPLE_RATE

    def __init__(self, settings: Settings) -> None:
        if not settings.allow_fake_backend:
            raise ConfigurationError("fake backend requested without CLAWVOICE_ALLOW_FAKE=1")
        self.settings = settings
        self.loaded = False
        self.last_reference_sha: str | None = None
        self.render_count = 0
        self.fail_next = False
        self._current_reference: str | None = None
        self._active = 0
        self.max_active = 0

    def load(self) -> None:
        self.loaded = True

    def identity(self) -> dict[str, Any]:
        return {
            "backend": "fake",
            "variant": "synthetic-test-only",
            "sample_rate": self.sample_rate,
            "watermark": "not_applicable_fake_backend",
            "real_model": False,
        }

    def state(self) -> BackendState:
        return BackendState(
            status="ready" if self.loaded else "cold",
            detail={"backend": "fake", "real_model": False},
            loaded=self.loaded,
        )

    def render(
        self,
        *,
        reference_path: Path,
        text: str,
        controls: dict[str, Any],
        seed: int,
    ) -> RenderResult:
        if not self.loaded:
            raise InferenceError("fake backend is not loaded")
        if not reference_path.is_file():
            raise InferenceError("reference audio is missing")
        self._active += 1
        self.max_active = max(self.max_active, self._active)
        try:
            ref_sha = sha256_file(reference_path)
            self.last_reference_sha = ref_sha
            self._current_reference = ref_sha
            self.render_count += 1
            if "[[fail]]" in text:
                raise InferenceError("synthetic failure requested by test marker")
            if "[[oom]]" in text:
                raise BackendOOM("synthetic CUDA out-of-memory marker")
            digest = hashlib.sha256((ref_sha + text + str(seed)).encode("utf-8")).digest()
            base_freq = 180.0 + (digest[0] / 255.0) * 180.0
            seconds = max(0.35, min(15.0, len(text) * 0.045))
            frames = int(self.sample_rate * seconds)
            samples = bytearray()
            for i in range(frames):
                t = i / self.sample_rate
                envelope = min(1.0, i / 240, (frames - i) / 240)
                value = 0.20 * envelope * math.sin(2 * math.pi * base_freq * t)
                # A second partial makes it easy to hear deterministic changes
                # without pretending to be speech.
                value += 0.05 * envelope * math.sin(2 * math.pi * base_freq * 2.01 * t)
                samples.extend(struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767)))
            return RenderResult(
                pcm_s16le=bytes(samples),
                sample_rate=self.sample_rate,
                metrics={"backend": "fake", "real_model": False, "reference_sha256": ref_sha},
            )
        finally:
            self._active -= 1
            self._current_reference = None

    def shutdown(self) -> None:
        self.loaded = False
