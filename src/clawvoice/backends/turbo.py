"""Reference-conditioned Chatterbox Turbo backend.

The upstream model mutates conditioning on the shared instance. A render
therefore swaps in this reference's conditionals and restores the previous
conditionals in `finally`, inside one backend lock. The worker also permits
only one active synthesis. This is reference-conditioned inference, not
fine-tuning.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path
from typing import Any

from ..config import Settings
from ..errors import BackendOOM, ModelUnavailable
from ..modelspec import (
    CHATTERBOX_COMMIT,
    PERTH_COMMIT,
    TURBO_REPO_ID,
    TURBO_REVISION,
    TURBO_SAMPLE_RATE,
    model_identity,
)
from ..util import sha256_file
from .base import BackendState, RenderResult

logger = logging.getLogger(__name__)


class TurboBackend:
    name = "turbo"
    sample_rate = TURBO_SAMPLE_RATE

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.model = None
        self.torch = None
        self._np = None
        self.last_metrics: dict[str, Any] = {}
        self.status = "cold"
        self.detail: dict[str, Any] = {}
        self._last_metrics: dict[str, Any] = {}
        self._render_lock = threading.RLock()

    def identity(self) -> dict[str, Any]:
        identity = model_identity("turbo")
        identity.update(
            {
                "backend": "turbo",
                "device": self.settings.device,
                "real_model": True,
                "model_dir": str(self.settings.model_dir),
            }
        )
        return identity

    def state(self) -> BackendState:
        return BackendState(
            status=self.status,
            detail=self.detail,
            loaded=self.status == "ready",
        )

    def load(self) -> None:
        from ..modelprovision import verify_model

        verification = verify_model(self.settings.model_dir, backend="turbo", deep=False)
        if not verification["ok"]:
            problems = verification["problems"]
            self.status = "model_missing"
            self.detail = {
                "backend": "turbo",
                "reason": "model files are missing, wrong, or not provisioned by the pinned fetch step",
                "problems": problems[:12],
                "model_revision": TURBO_REVISION,
                "model_dir": str(self.settings.model_dir),
            }
            raise ModelUnavailable(
                "Chatterbox Turbo model files are not provisioned; run the pinned model fetch step",
                details={
                    "model_revision": TURBO_REVISION,
                    "missing_files": [p["file"] for p in problems if p["problem"] == "missing"],
                },
            )
        try:
            import numpy as np
            import torch
            from chatterbox.tts_turbo import ChatterboxTurboTTS
        except Exception as exc:  # pragma: no cover - exercised on inference hosts only
            self.status = "failed"
            self.detail = {"backend": "turbo", "reason": "inference dependencies are unavailable"}
            raise ModelUnavailable(
                "Chatterbox inference dependencies are unavailable in this worker",
                details={"hint": "install the inference extra in the worker image"},
            ) from exc
        self.torch = torch
        self._np = np
        if self.settings.device == "cuda":
            if not torch.cuda.is_available():
                self.status = "failed"
                self.detail = {"backend": "turbo", "reason": "CUDA is not available; no CPU fallback"}
                raise ModelUnavailable(
                    "CUDA is unavailable in the worker; this service does not silently fall back to CPU"
                )
            try:
                torch.cuda.init()
                device_index = torch.cuda.current_device()
                probe = torch.zeros((8, 8), dtype=torch.float32, device="cuda")
                probe = (probe + 1.0) @ (probe + 2.0)
                torch.cuda.synchronize()
                if not math.isfinite(float(probe.sum().item())):
                    raise RuntimeError("CUDA math returned a non-finite value")
                capability = torch.cuda.get_device_capability(device_index)
                name = torch.cuda.get_device_name(device_index)
                arch_list = list(torch.cuda.get_arch_list())
                self.detail = {
                    "backend": "turbo",
                    "device": "cuda",
                    "device_index": device_index,
                    "device_name": name,
                    "capability": f"{capability[0]}.{capability[1]}",
                    "torch_cuda_version": torch.version.cuda,
                    "torch_version": torch.__version__,
                    "arch_list": arch_list,
                    "cuda_allocation_math": "passed",
                }
            except Exception as exc:
                self.status = "failed"
                self.detail = {"backend": "turbo", "reason": "CUDA allocation/math probe failed"}
                raise ModelUnavailable(
                    "CUDA allocation or math failed; refusing to fall back to CPU"
                ) from exc
        else:
            self.detail = {
                "backend": "turbo",
                "device": "cpu",
                "torch_version": torch.__version__,
                "torch_cuda_version": torch.version.cuda,
            }
        self.status = "loading"
        try:
            model = ChatterboxTurboTTS.from_local(str(self.settings.model_dir), self.settings.device)
        except Exception as exc:  # pragma: no cover - requires model weights
            self.status = "failed"
            self.detail["reason"] = "model load failed"
            raise ModelUnavailable("Chatterbox Turbo model could not be loaded") from exc
        if int(model.sr) != self.sample_rate:
            self.status = "failed"
            self.detail["reason"] = "unexpected model sample rate"
            raise ModelUnavailable(
                "the model reported an unexpected output sample rate",
                details={"expected": self.sample_rate, "actual": int(model.sr)},
            )
        self.model = model
        self.status = "ready"
        self.detail.update(
            {
                "model_revision": TURBO_REVISION,
                "repo_id": TURBO_REPO_ID,
                "chatterbox_commit": CHATTERBOX_COMMIT,
                "perth_commit": PERTH_COMMIT,
                "watermarker": "Perth implicit watermark enabled by upstream",
                "dtype": "float32",
                "eager": True,
            }
        )
        logger.info(
            "loaded Chatterbox Turbo revision %s on %s", TURBO_REVISION, self.settings.device
        )

    def render(
        self,
        *,
        reference_path: Path,
        text: str,
        controls: dict[str, Any],
        seed: int,
    ) -> RenderResult:
        if self.model is None or self.torch is None:
            raise ModelUnavailable("Chatterbox model is not loaded")
        torch = self.torch
        start = time.monotonic()
        with self._render_lock:
            previous = getattr(self.model, "conds", None)
            cuda_device = [torch.cuda.current_device()] if self.settings.device == "cuda" else []
            if self.settings.device == "cuda":
                torch.cuda.reset_peak_memory_stats()
                torch.cuda.synchronize()
            try:
                self.model.conds = None
                self.model.prepare_conditionals(str(reference_path), norm_loudness=True)
                with torch.random.fork_rng(devices=cuda_device, enabled=True):
                    torch.manual_seed(int(seed))
                    if self.settings.device == "cuda":
                        torch.cuda.manual_seed_all(int(seed))
                    with torch.inference_mode():
                        wav = self.model.generate(
                            text,
                            temperature=float(controls["temperature"]),
                            top_p=float(controls["top_p"]),
                            top_k=int(controls["top_k"]),
                            repetition_penalty=float(controls["repetition_penalty"]),
                            norm_loudness=True,
                        )
                if self.settings.device == "cuda":
                    torch.cuda.synchronize()
                array = wav.detach().to("cpu", dtype=torch.float32).numpy().reshape(-1)
                clipped = self._np.clip(array, -1.0, 1.0)
                pcm = (clipped * 32767.0).astype("int16").tobytes()
                metrics: dict[str, Any] = {
                    "backend": "turbo",
                    "real_model": True,
                    "device": self.settings.device,
                    "reference_sha256": sha256_file(reference_path),
                    "watermark": "perth",
                    "render_seconds": round(time.monotonic() - start, 3),
                }
                if self.settings.device == "cuda":
                    peak = round(torch.cuda.max_memory_allocated() / (1024 * 1024), 1)
                    metrics["peak_vram_mb"] = peak
                    self.detail["peak_vram_mb"] = peak
                    metrics["device_name"] = torch.cuda.get_device_name()
                self._last_metrics = metrics
                self.last_metrics = metrics
                return RenderResult(pcm_s16le=pcm, sample_rate=self.sample_rate, metrics=metrics)
            except torch.cuda.OutOfMemoryError as exc:
                try:
                    torch.cuda.empty_cache()
                except Exception:
                    pass
                metrics = {
                    "backend": "turbo",
                    "real_model": True,
                    "device": "cuda",
                    "error": "cuda_oom",
                }
                self._last_metrics = metrics
                raise BackendOOM("CUDA out of memory") from exc
            finally:
                # Never let one profile's conditioning become the next job's
                # implicit default. Restore even after a partial failure.
                self.model.conds = previous
                if self.settings.device == "cuda":
                    try:
                        torch.cuda.reset_peak_memory_stats()
                    except Exception:
                        pass

    def shutdown(self) -> None:
        self.model = None
        self.status = "stopped"
