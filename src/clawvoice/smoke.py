"""Operator-run real-model smoke entrypoint.

This module imports Torch/Chatterbox only when called. It is not used by the
ordinary API or CI, and a successful run here is the only code path that can
mark real synthesis as verified in this checkout's state file.
"""

from __future__ import annotations

import json
import resource
import tempfile
import time
from pathlib import Path
from typing import Any

from .audio import AudioProcessor
from .backends.turbo import TurboBackend
from .config import Settings
from .errors import ValidationFailure
from .modelprovision import verify_model
from .modelspec import TURBO_REVISION
from .speech import build_plan, default_controls
from .util import atomic_write, ensure_private_dir, sha256_file

SMOKE_TEXT = (
    "Hello. This is a real reference-conditioned Chatterbox Turbo smoke test. "
    "The quick brown fox jumps over the lazy dog. Press 2 for billing."
)


def run_smoke(
    settings: Settings,
    *,
    reference: Path,
    output: Path,
    text: str = SMOKE_TEXT,
) -> dict[str, Any]:
    if settings.backend != "turbo":
        raise ValidationFailure("real-model smoke requires CLAWVOICE_BACKEND=turbo")
    verification = verify_model(settings.model_dir, backend="turbo", deep=True)
    if not verification["ok"]:
        raise ValidationFailure(
            "the pinned model is not provisioned or failed verification",
            details={"problems": verification["problems"][:12]},
        )
    audio = AudioProcessor(settings)
    probe = audio.probe(reference)
    _, checks = audio.inspect(reference, probe)
    start, end = audio.validate_segment(probe.duration_ms, None, None)
    ensure_private_dir(settings.tmp_dir)
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=settings.tmp_dir) as temp_dir:
        temp = Path(temp_dir)
        normalized = temp / "reference.wav"
        audio.normalize(reference, normalized, start_ms=start, end_ms=end)
        plan = build_plan({"text": text}, settings=settings, controls=default_controls(), seed=12345)
        backend = TurboBackend(settings)
        load_start = time.monotonic()
        backend.load()
        load_seconds = time.monotonic() - load_start
        output_chunks = bytearray()
        renders: list[dict[str, Any]] = []
        try:
            first = True
            for _pass_index in range(2):
                pass_start = time.monotonic()
                pcm = bytearray()
                for chunk_index, chunk in enumerate(plan.text_pieces[0].chunks):
                    result = backend.render(
                        reference_path=normalized,
                        text=chunk.text,
                        controls=plan.controls,
                        seed=plan.seed + chunk_index,
                    )
                    pcm.extend(result.pcm_s16le)
                    if chunk_index < len(plan.text_pieces[0].chunks) - 1:
                        pcm.extend(audio.silence_pcm(settings.chunk_join_gap_ms, backend.sample_rate))
                elapsed = time.monotonic() - pass_start
                duration_ms = int(round((len(pcm) // 2) / backend.sample_rate * 1000))
                metrics = dict(getattr(backend, "last_metrics", {}) or {})
                renders.append(
                    {
                        "pass": "cold" if first else "warm",
                        "latency_seconds": round(elapsed, 3),
                        "audio_ms": duration_ms,
                        "real_time_factor": round(elapsed / max(duration_ms / 1000, 0.001), 3),
                        "peak_vram_mb": metrics.get("peak_vram_mb"),
                    }
                )
                if first:
                    output_chunks.extend(pcm)
                    first = False
            master_ms = audio.write_wav(output, bytes(output_chunks), backend.sample_rate)
            state = backend.state()
        finally:
            backend.shutdown()
    usage = resource.getrusage(resource.RUSAGE_SELF)
    record = {
        "schema_version": 1,
        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "real_model": True,
        "backend": "turbo",
        "model_revision": TURBO_REVISION,
        "cuda": settings.device == "cuda",
        "device_detail": state.detail,
        "reference": {
            "duration_ms": end - start,
            "sha256": sha256_file(normalized),
            "checks": checks.as_dict(),
        },
        "text_chars": len(text),
        "output": {
            "path": str(output.resolve()),
            "duration_ms": master_ms,
            "sample_rate": backend.sample_rate,
            "sha256": sha256_file(output),
        },
        "latency": {"load_seconds": round(load_seconds, 3), "renders": renders},
        "peak_process_rss_mb": round(usage.ru_maxrss / 1024, 1),
        "note": "This records one real-model smoke run, not a full quality or speaker-similarity evaluation.",
    }
    compatibility_path = settings.state_dir / "compatibility.json"
    ensure_private_dir(compatibility_path.parent)
    atomic_write(compatibility_path, json.dumps(record, indent=2, sort_keys=True).encode(), mode=0o600)
    return record
