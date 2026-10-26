"""Single-process inference worker and exact-silence assembly.

One worker instance owns the model. A database worker lease prevents a second
process from claiming jobs. Each backend render restores the previous model
conditioning in `finally`; the worker serializes all renders.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
import traceback
from pathlib import Path
from typing import Any

from .audio import AudioDecodeError, AudioProcessor
from .backends.base import Backend
from .config import Settings
from .errors import (
    BackendOOM,
    CancellationRequested,
    InferenceError,
    ModelUnavailable,
    ValidationFailure,
)
from .modelspec import model_identity
from .speech import PausePiece, SpeechPlan, TextPiece, build_plan
from .storage import StorageManager
from .store import Store
from .util import (
    ensure_private_dir,
    iso_from_ms,
    new_id,
    now_ms,
    safe_storage_path,
    sha256_file,
)

logger = logging.getLogger("clawvoice.worker")


class JobTimedOut(Exception):
    pass


class _Deadline:
    """Best-effort hard deadline in the worker's main thread.

    The embedded worker runs in a background thread (signals are unavailable);
    it still checks elapsed time around every render. The dedicated worker
    process is the production path and can interrupt a single long render.
    """

    def __init__(self, seconds: float) -> None:
        self.seconds = max(0.1, seconds)
        self.enabled = (
            threading.current_thread() is threading.main_thread()
            and hasattr(signal, "SIGALRM")
            and hasattr(signal, "setitimer")
        )
        self._old_handler: Any = None

    def __enter__(self) -> _Deadline:
        if self.enabled:
            self._old_handler = signal.getsignal(signal.SIGALRM)

            def _raise(signum: int, frame: Any) -> None:
                raise JobTimedOut("synthesis job exceeded its execution deadline")

            signal.signal(signal.SIGALRM, _raise)
            signal.setitimer(signal.ITIMER_REAL, self.seconds)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.enabled:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, self._old_handler)


class InferenceWorker:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        audio: AudioProcessor,
        *,
        backend: Backend | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.audio = audio
        self.storage = StorageManager(settings, store)
        self.backend = backend
        self.worker_id = worker_id or new_id("wrk")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._status = "starting"
        self._detail: dict[str, Any] = {}
        self._current_job_id: str | None = None
        self._lock_owned = False
        self._lost_lock = False
        self._last_load_attempt = 0.0
        self._backend_ready = False
        self._started_at = now_ms()

    # ------------------------------------------------------------- lifecycle
    def start_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self.run_forever, name="clawvoice-worker", daemon=True)
        self._thread.start()

    def request_stop(self) -> None:
        self._stop.set()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def current_state(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "status": self._status,
                "detail": dict(self._detail),
                "current_job_id": self._current_job_id,
            }

    def _set_state(self, status: str, **detail: Any) -> None:
        with self._state_lock:
            self._status = status
            if detail:
                self._detail = detail

    def _set_job(self, job_id: str | None) -> None:
        with self._state_lock:
            self._current_job_id = job_id

    # ------------------------------------------------------------------ run
    def run_forever(self) -> None:
        logger.info("inference worker %s starting", self.worker_id)
        while not self._stop.is_set():
            if not self._lock_owned:
                if not self.store.try_acquire_worker_lock(
                    owner=self.worker_id,
                    lease_seconds=self.settings.worker_lease_seconds,
                    status="starting",
                    detail={"backend": self.settings.backend},
                ):
                    self._set_state("waiting_for_lock")
                    self._stop.wait(2.0)
                    continue
                self._lock_owned = True
                recovered = self.store.recover_stale_jobs(
                    lease_seconds=self.settings.worker_lease_seconds
                )
                if recovered:
                    logger.warning("recovered %d interrupted job(s)", len(recovered))
                self._start_heartbeat()
                self._last_load_attempt = 0.0
            if not self._backend_ready:
                self._ensure_backend()
                if not self._backend_ready:
                    self._stop.wait(5.0 if self._status == "model_missing" else 15.0)
                    continue
            if self._lost_lock:
                logger.error("worker lost its lease; stopping instead of claiming more work")
                break
            job = self.store.claim_job(
                worker_id=self.worker_id, lease_seconds=self.settings.worker_lease_seconds
            )
            if job is None:
                self._stop.wait(0.25)
                continue
            self._process_job(job)
        self._finish()
        logger.info("inference worker %s stopped", self.worker_id)

    def _ensure_backend(self) -> None:
        now = time.monotonic()
        if now - self._last_load_attempt < 10.0:
            return
        self._last_load_attempt = now
        self._set_state("loading")
        if self.backend is None:
            from .backends import create_backend

            self.backend = create_backend(self.settings)
        try:
            self.backend.load()
        except ModelUnavailable as exc:
            state = self.backend.state()
            status = state.status if state.status in ("model_missing", "failed") else "model_missing"
            detail = self._sanitize_detail(state.detail, fallback=str(exc))
            self._set_state(status, **detail)
            self._backend_ready = False
            try:
                self.store.renew_worker_lock(
                    owner=self.worker_id,
                    lease_seconds=self.settings.worker_lease_seconds,
                    status=status,
                    detail=detail,
                )
            except Exception:
                logger.exception("could not publish worker failure")
            logger.error("backend unavailable: %s", status)
            return
        except Exception:
            self._set_state("failed", backend=self.settings.backend, reason="worker initialization failed")
            self._backend_ready = False
            logger.exception("backend initialization failed")
            return
        state = self.backend.state()
        self._backend_ready = state.status == "ready"
        detail = self._sanitize_detail(state.detail or self.backend.identity(), fallback="")
        self._set_state(state.status, **detail)
        if self._backend_ready:
            try:
                self.store.renew_worker_lock(
                    owner=self.worker_id,
                    lease_seconds=self.settings.worker_lease_seconds,
                    status="ready",
                    detail=detail,
                )
            except Exception:
                logger.exception("could not publish worker readiness")
            logger.info("backend ready: %s", self.settings.backend)

    @staticmethod
    def _sanitize_detail(detail: dict[str, Any], *, fallback: str) -> dict[str, Any]:
        allowed = {
            "backend",
            "device",
            "device_name",
            "capability",
            "torch_version",
            "torch_cuda_version",
            "arch_list",
            "cuda_allocation_math",
            "reason",
            "problems",
            "model_revision",
            "missing_files",
            "variant",
            "real_model",
            "watermark",
            "dtype",
            "eager",
            "repo_id",
            "chatterbox_commit",
            "perth_commit",
        }
        safe = {key: value for key, value in detail.items() if key in allowed}
        if "problems" in safe and isinstance(safe["problems"], list):
            safe["problems"] = safe["problems"][:12]
        if not safe and fallback:
            safe["reason"] = fallback
        return safe

    def _start_heartbeat(self) -> None:
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="clawvoice-heartbeat", daemon=True
        )
        self._heartbeat_thread.start()

    def _heartbeat_loop(self) -> None:
        interval = max(2, self.settings.worker_heartbeat_seconds)
        while self._lock_owned and not self._stop.is_set():
            if self._stop.wait(interval):
                break
            state = self.current_state()
            current_job = state["current_job_id"]
            try:
                ok = self.store.renew_worker_lock(
                    owner=self.worker_id,
                    lease_seconds=self.settings.worker_lease_seconds,
                    status=state["status"],
                    detail=state["detail"],
                    current_job_id=current_job,
                )
                if not ok:
                    self._lost_lock = True
                    return
                if current_job:
                    if not self.store.heartbeat_job(
                        current_job,
                        self.worker_id,
                        lease_seconds=self.settings.worker_lease_seconds,
                    ):
                        self._lost_lock = True
                        return
            except Exception:
                logger.exception("worker heartbeat failed")

    def _finish(self) -> None:
        if self.backend is not None:
            try:
                self.backend.shutdown()
            except Exception:
                logger.exception("backend shutdown failed")
        if self._lock_owned:
            try:
                self.store.release_worker_lock(self.worker_id)
            except Exception:
                logger.exception("worker lock release failed")
            self._lock_owned = False
        self._set_state("stopped")

    # ------------------------------------------------------------- job work
    def _process_job(self, job: dict[str, Any]) -> None:
        job_id = job["id"]
        self._set_job(job_id)
        self._set_state("ready")
        self.store.update_progress(job_id, self.worker_id, "loading_voice")
        work_dir: Path | None = None
        started = time.monotonic()
        rendered_ms = 0
        plan: SpeechPlan | None = None
        try:
            # Each attempt gets its own directory. A stale worker that loses
            # its lease must never delete a newer attempt's committed files.
            work_dir = self.settings.artifacts_dir / job_id / new_id("att")
            ensure_private_dir(work_dir)
            payload = json.loads(job["input_json"])
            controls = json.loads(job["controls_json"])
            if not job.get("voice_id"):
                raise ValidationFailure("job has no voice")
            voice = self.store.get_voice_row(job["voice_id"])
            if voice["backend"] != self.settings.backend:
                raise ValidationFailure(
                    "the selected voice is not compatible with this worker backend",
                    details={"voice_backend": voice["backend"], "worker_backend": self.settings.backend},
                )
            if voice["model_revision"] != self.settings.model_revision:
                raise ValidationFailure(
                    "the selected voice was created against a different model revision"
                )
            reference_id = job.get("reference_version_id")
            reference = (
                self.store.get_reference_by_id(job["voice_id"], reference_id)
                if reference_id
                else self.store.get_reference_for_voice(job["voice_id"])
            )
            ref_path = safe_storage_path(self.settings.data_dir, reference["normalized_path"])
            if not ref_path.is_file():
                raise ValidationFailure("the voice reference audio is missing")
            plan = build_plan(
                payload,
                settings=self.settings,
                controls=controls,
                seed=int(job["seed"]),
            )
            self.store.update_progress(job_id, self.worker_id, "synthesizing")
            timeout = self.settings.job_timeout_seconds
            with _Deadline(timeout):
                pcm, rendered_ms = self._render_plan(
                    job_id=job_id,
                    plan=plan,
                    reference_path=ref_path,
                    deadline=started + timeout,
                )
                self._check_cancel(job_id)
                self.store.update_progress(job_id, self.worker_id, "writing_artifacts")
                # Admit output before writing; generated audio is subject to the
                # same private-storage bound as uploads and references.
                self.storage.ensure_capacity(len(pcm) + 1024 * 1024)
                artifacts, output_id, master_id, metrics = self._write_artifacts(
                    job=job,
                    reference=reference,
                    plan=plan,
                    pcm=pcm,
                    rendered_ms=rendered_ms,
                    work_dir=work_dir,
                    elapsed=time.monotonic() - started,
                )
            self._check_cancel(job_id)
            result = self.store.complete_job(
                job_id=job_id,
                worker_id=self.worker_id,
                artifacts=artifacts,
                output_artifact_id=output_id,
                master_artifact_id=master_id,
                metrics={**metrics, "elapsed_seconds": round(time.monotonic() - started, 3)},
            )
            if result != "succeeded":
                self._remove_tree(work_dir)
                if result == "lost":
                    logger.warning("job %s lost its worker lease before commit", job_id)
            else:
                logger.info("job %s succeeded in %.1fs", job_id, time.monotonic() - started)
        except CancellationRequested:
            self.store.mark_canceled(job_id, self.worker_id)
            if work_dir:
                self._remove_tree(work_dir)
            logger.info("job %s canceled", job_id)
        except JobTimedOut:
            self.store.fail_job(
                job_id,
                self.worker_id,
                code="job_timeout",
                message="Synthesis exceeded the configured execution deadline",
            )
            if work_dir:
                self._remove_tree(work_dir)
            logger.warning("job %s exceeded its deadline", job_id)
        except ValidationFailure as exc:
            self.store.fail_job(
                job_id,
                self.worker_id,
                code=exc.code,
                message=exc.message,
                metrics={"elapsed_seconds": round(time.monotonic() - started, 3)},
            )
            if work_dir:
                self._remove_tree(work_dir)
            logger.warning("job %s validation failure: %s", job_id, exc.code)
        except AudioDecodeError as exc:
            self.store.fail_job(
                job_id,
                self.worker_id,
                code=exc.code,
                message=exc.message,
            )
            if work_dir:
                self._remove_tree(work_dir)
        except ModelUnavailable:
            self.store.fail_job(
                job_id,
                self.worker_id,
                code="model_unavailable",
                message="The inference model is unavailable; check readiness and provisioning",
            )
            if work_dir:
                self._remove_tree(work_dir)
            logger.error("job %s model unavailable", job_id)
        except BackendOOM:
            self.store.fail_job(
                job_id,
                self.worker_id,
                code="cuda_oom",
                message="CUDA out of memory; no CPU fallback was attempted",
            )
            if work_dir:
                self._remove_tree(work_dir)
            self._backend_ready = False
            self._last_load_attempt = 0.0
            self._set_state(
                "failed",
                backend=self.settings.backend,
                reason="CUDA out of memory; worker reload required",
            )
            try:
                if self.backend is not None:
                    self.backend.shutdown()
                self.store.renew_worker_lock(
                    owner=self.worker_id,
                    lease_seconds=self.settings.worker_lease_seconds,
                    status="failed",
                    detail={"backend": self.settings.backend, "reason": "CUDA OOM"},
                )
            except Exception:
                logger.exception("could not mark worker failed after OOM")
            logger.error("job %s ran out of GPU memory", job_id)
        except InferenceError:
            self.store.fail_job(
                job_id,
                self.worker_id,
                code="inference_failed",
                message="The inference backend failed to render this job",
            )
            if work_dir:
                self._remove_tree(work_dir)
            logger.exception("job %s inference error", job_id)
        except Exception:
            self.store.fail_job(
                job_id,
                self.worker_id,
                code="internal_error",
                message="Synthesis failed for an internal reason",
            )
            if work_dir:
                self._remove_tree(work_dir)
            logger.error("job %s internal error\n%s", job_id, traceback.format_exc(limit=8))
        finally:
            self._set_job(None)
            self._set_state("ready" if self._backend_ready else "failed")

    def _render_plan(
        self,
        *,
        job_id: str,
        plan: SpeechPlan,
        reference_path: Path,
        deadline: float,
    ) -> tuple[bytes, int]:
        if self.backend is None:
            raise ModelUnavailable("inference backend is not ready")
        output = bytearray()
        rendered_ms = 0
        chunk_index = 0
        for piece_index, piece in enumerate(plan.pieces):
            self._check_deadline(deadline)
            self._check_cancel(job_id)
            if isinstance(piece, PausePiece):
                if rendered_ms + piece.pause_ms > self.settings.max_output_ms:
                    raise ValidationFailure(
                        "generated speech would exceed the configured output duration limit",
                        details={"max_output_ms": self.settings.max_output_ms},
                    )
                output.extend(self.audio.silence_pcm(piece.pause_ms, self.backend.sample_rate))
                rendered_ms += piece.pause_ms
                continue
            assert isinstance(piece, TextPiece)
            for local_index, chunk in enumerate(piece.chunks):
                self._check_deadline(deadline)
                self._check_cancel(job_id)
                self.store.update_progress(
                    job_id, self.worker_id, f"synthesizing {chunk_index + 1}/{plan.chunk_count}"
                )
                result = self.backend.render(
                    reference_path=reference_path,
                    text=chunk.text,
                    controls=plan.controls,
                    seed=plan.seed + chunk_index,
                )
                if result.sample_rate != self.backend.sample_rate:
                    raise InferenceError("backend returned a different sample rate")
                if len(result.pcm_s16le) % 2:
                    raise InferenceError("backend returned malformed PCM")
                chunk_ms = int(round((len(result.pcm_s16le) // 2) / result.sample_rate * 1000))
                if chunk_ms <= 0:
                    raise InferenceError("backend returned empty audio")
                if rendered_ms + chunk_ms > self.settings.max_output_ms:
                    raise ValidationFailure(
                        "generated speech would exceed the configured output duration limit",
                        details={"max_output_ms": self.settings.max_output_ms},
                    )
                output.extend(result.pcm_s16le)
                rendered_ms += chunk_ms
                chunk_index += 1
                if local_index < len(piece.chunks) - 1:
                    gap = self.audio.silence_pcm(
                        self.settings.chunk_join_gap_ms, self.backend.sample_rate
                    )
                    output.extend(gap)
                    rendered_ms += self.settings.chunk_join_gap_ms
                if rendered_ms > self.settings.max_output_ms:
                    raise ValidationFailure(
                        "generated speech would exceed the configured output duration limit"
                    )
            if piece_index < len(plan.pieces) - 1:
                next_piece = plan.pieces[piece_index + 1]
                if not isinstance(next_piece, PausePiece) and self.settings.chunk_join_gap_ms:
                    # Adjacent text segments get the same modest separation as
                    # chunks. Explicit pause segments remain exact.
                    output.extend(
                        self.audio.silence_pcm(
                            self.settings.chunk_join_gap_ms, self.backend.sample_rate
                        )
                    )
                    rendered_ms += self.settings.chunk_join_gap_ms
        if rendered_ms > self.settings.max_output_ms:
            raise ValidationFailure(
                "generated speech would exceed the configured output duration limit",
                details={"max_output_ms": self.settings.max_output_ms},
            )
        self.store.update_progress(job_id, self.worker_id, "assembling")
        return bytes(output), rendered_ms

    def _check_deadline(self, deadline: float) -> None:
        if time.monotonic() > deadline:
            raise JobTimedOut("synthesis job exceeded its execution deadline")

    def _check_cancel(self, job_id: str) -> None:
        if self.store.is_cancel_requested(job_id):
            raise CancellationRequested()

    def _write_artifacts(
        self,
        *,
        job: dict[str, Any],
        reference: dict[str, Any],
        plan: SpeechPlan,
        pcm: bytes,
        rendered_ms: int,
        work_dir: Path,
        elapsed: float,
    ) -> tuple[list[dict[str, Any]], str, str, dict[str, Any]]:
        if self.backend is None:
            raise ModelUnavailable("inference backend is not ready")
        master_id = new_id("art")
        master_name = f"{master_id}.wav"
        master_abs = work_dir / master_name
        master_ms = self.audio.write_wav(master_abs, pcm, self.backend.sample_rate)
        backend_identity = self.backend.identity()
        backend_identity.pop("model_dir", None)
        segment_manifest = []
        for piece in plan.pieces:
            if isinstance(piece, PausePiece):
                segment_manifest.append({"type": "pause", "pause_ms": piece.pause_ms})
            else:
                segment_manifest.append(
                    {
                        "type": "text",
                        "chars": piece.chars,
                        "sha256": piece.sha256,
                        "chunks": len(piece.chunks),
                    }
                )
        manifest_base = {
            "schema_version": 1,
            "generator": {"name": "clawvoice", "version": self.settings.service_version},
            "generated_at": iso_from_ms(now_ms()),
            "backend": backend_identity,
            "model": model_identity(self.settings.backend)
            if self.settings.backend == "turbo"
            else {"backend": "fake", "real_model": False},
            "reference": {
                "version_id": reference["id"],
                "sha256": reference["normalized_sha256"],
            },
            "request": {
                "kind": job["kind"],
                "preset_requested": job["preset"],
                "text_sha256": plan.text_sha256,
                "total_chars": plan.total_chars,
                "segments": segment_manifest,
                "controls": plan.controls,
                "seed": plan.seed,
                "seed_reproducibility": "not_guaranteed_across_hardware_or_library_versions",
            },
            "audio": {
                "sample_rate": self.backend.sample_rate,
                "channels": 1,
                "duration_ms": master_ms,
                "requested_silence_ms": plan.total_pause_ms,
                "exact_pause_assembly": True,
            },
            "watermark": {
                "expected": "perth" if self.settings.backend == "turbo" else "not_applicable_fake",
                "behavior": "upstream model output retained; not removed or re-encoded before master",
            },
            "metrics": {"render_seconds": round(elapsed, 3), "synthesized_audio_ms": rendered_ms},
        }
        expires_at = now_ms() + self.settings.artifact_retention_days * 86_400_000
        master_manifest = {
            **manifest_base,
            "artifact": {"kind": "master", "preset": "master", "format": "wav/pcm_s16le"},
        }
        master_info = {
            "media_type": "audio/wav",
            "format": "wav/pcm_s16le",
            "duration_ms": master_ms,
            "sample_rate": self.backend.sample_rate,
            "channels": 1,
            "size_bytes": master_abs.stat().st_size,
            "sha256": sha256_file(master_abs),
        }
        artifacts: list[dict[str, Any]] = [
            self._artifact_record(
                artifact_id=master_id,
                job_id=job["id"],
                kind="master",
                preset="master",
                relative_path=f"artifacts/{job['id']}/{work_dir.name}/{master_name}",
                info=master_info,
                manifest=master_manifest,
                expires_at=expires_at,
            )
        ]
        output_id = master_id
        if job["preset"] != "master":
            output_id = new_id("art")
            ext = {"mp3": "mp3", "telephony": "wav", "telephony-mulaw": "ulaw"}.get(job["preset"])
            if ext is None:
                raise ValidationFailure("unsupported output preset")
            out_name = f"{output_id}.{ext}"
            out_abs = work_dir / out_name
            info = self.audio.convert(master_abs, out_abs, job["preset"])
            derived_manifest = {
                **manifest_base,
                "artifact": {
                    "kind": "output",
                    "preset": job["preset"],
                    "format": info["format"],
                    "derived_from": master_id,
                    "master_sha256": master_info["sha256"],
                },
            }
            artifacts.append(
                self._artifact_record(
                    artifact_id=output_id,
                    job_id=job["id"],
                    kind="output",
                    preset=job["preset"],
                    relative_path=f"artifacts/{job['id']}/{work_dir.name}/{out_name}",
                    info=info,
                    manifest=derived_manifest,
                    expires_at=expires_at,
                )
            )
        metrics = {
            "audio_duration_ms": master_ms,
            "synthesized_audio_ms": rendered_ms,
            "real_time_factor": round((elapsed / max(0.001, master_ms / 1000.0)), 3),
            "artifact_count": len(artifacts),
            "backend": self.settings.backend,
            "device": self.settings.device,
        }
        last_render = dict(getattr(self.backend, "last_metrics", {}) or {})
        if last_render.get("peak_vram_mb") is not None:
            metrics["peak_vram_mb"] = last_render["peak_vram_mb"]
        state = self.backend.state()
        if state.detail.get("peak_vram_mb") is not None:
            metrics["peak_vram_mb"] = state.detail["peak_vram_mb"]
        return artifacts, output_id, master_id, metrics

    def _artifact_record(
        self,
        *,
        artifact_id: str,
        job_id: str,
        kind: str,
        preset: str,
        relative_path: str,
        info: dict[str, Any],
        manifest: dict[str, Any],
        expires_at: int,
    ) -> dict[str, Any]:
        return {
            "id": artifact_id,
            "job_id": job_id,
            "kind": kind,
            "preset": preset,
            "path": relative_path,
            "media_type": info["media_type"],
            "format": info["format"],
            "size_bytes": info["size_bytes"],
            "duration_ms": info["duration_ms"],
            "sample_rate": info["sample_rate"],
            "channels": info["channels"],
            "sha256": info["sha256"],
            "manifest": manifest,
            "expires_at": expires_at,
        }

    @staticmethod
    def _remove_tree(path: Path | None) -> None:
        if path is None:
            return
        try:
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_symlink() or child.is_file():
                    child.unlink(missing_ok=True)
                elif child.is_dir():
                    child.rmdir()
            path.rmdir()
        except OSError:
            logger.warning("could not remove partial artifact directory")
