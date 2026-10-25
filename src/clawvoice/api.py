"""Versioned HTTP API and private recorder UI entrypoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import stat
import tempfile
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request, Response, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.cors import CORSMiddleware

from .audio import AudioDecodeError, AudioProcessor
from .config import Settings
from .db import Database
from .errors import (
    AuthenticationError,
    AuthorizationError,
    ClawvoiceError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    PayloadTooLarge,
    UnavailableError,
    ValidationFailure,
)
from .modelspec import missing_or_bad_files
from .schemas import (
    EnrollmentSessionRequest,
    EnrollmentVoiceRequest,
    ErrorResponse,
    SynthesisRequest,
    VoiceRenameRequest,
)
from .speech import build_plan, default_controls
from .storage import StorageManager
from .store import Store
from .util import (
    atomic_copy,
    ensure_private_dir,
    iso_from_ms,
    new_id,
    new_secret,
    now_ms,
    sha256_file,
    token_hash,
    validate_id,
)
from .worker import InferenceWorker

logger = logging.getLogger("clawvoice.api")

ENROLLMENT_AUDITION_TEXT = (
    "Hello. This is a preview of the voice you created. "
    "Press 2 to hear it again."
)
PREVIEW_AUDITION_TEXT = "Hello. This is a preview of your saved voice."
_OPAQUE = re.compile(r"^[a-z]+_[0-9a-f]{24}$")
_EXTENSIONS = {
    "wav": "wav",
    "mp3": "mp3",
    "ogg": "ogg",
    "webm": "webm",
    "mp4": "m4a",
    "mov": "mov",
    "flac": "flac",
    "aac": "aac",
}


def _safe_name(value: str) -> str:
    name = " ".join(value.split())
    if not name or len(name) > 80:
        raise ValidationFailure("voice name must be 1-80 characters")
    if any(unicodedata.category(ch).startswith("C") for ch in name):
        raise ValidationFailure("voice name contains unsupported control characters")
    return name


def _session_view(session: dict[str, Any], now: int | None = None) -> dict[str, Any]:
    now = now or now_ms()
    status = session["status"]
    if status == "open" and session["expires_at"] <= now:
        status = "expired"
    return {
        "id": session["id"],
        "name": session["name"],
        "status": status,
        "created_at": iso_from_ms(session["created_at"]),
        "expires_at": iso_from_ms(session["expires_at"]),
        "upload_id": session.get("upload_id"),
        "voice_id": session.get("voice_id"),
        "audition_job_id": session.get("audition_job_id"),
        "create_voice_once": True,
        "permissions": "upload-and-audition-only" if status != "expired" else "none",
    }


def _upload_view(upload: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": upload["id"],
        "duration_ms": upload["duration_ms"],
        "sample_rate": upload["sample_rate"],
        "channels": upload["channels"],
        "media_type": upload["media_type"],
        "format": upload["format_name"],
        "bytes": upload["size_bytes"],
        "sha256": upload["sha256"],
        "checks": json.loads(upload["checks_json"]),
        "created_at": iso_from_ms(upload["created_at"]),
    }


def _media_type_for_format(format_name: str) -> str:
    if "mp3" in format_name or "mpeg" in format_name:
        return "audio/mpeg"
    if "wav" in format_name or "wave" in format_name:
        return "audio/wav"
    if "webm" in format_name:
        return "audio/webm"
    if "mp4" in format_name or "mov" in format_name:
        return "audio/mp4"
    if "ogg" in format_name:
        return "audio/ogg"
    if "flac" in format_name:
        return "audio/flac"
    return "application/octet-stream"


def _extension_for_format(format_name: str) -> str:
    for key, ext in _EXTENSIONS.items():
        if key in format_name:
            return ext
    return "bin"


def _artifact_extension(fmt: str) -> str:
    if "mp3" in fmt:
        return "mp3"
    if "mulaw" in fmt:
        return "ulaw"
    return "wav"


def _audition_job_payload() -> tuple[dict[str, Any], dict[str, Any], int]:
    return {"text": ENROLLMENT_AUDITION_TEXT}, default_controls(), secrets.randbelow(2**31 - 1)


class _BodyTooLarge(Exception):
    pass


async def _read_body_to_temp(
    request: Request,
    *,
    settings: Settings,
    storage: StorageManager,
) -> tuple[Path, int, str]:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError as exc:
            raise ValidationFailure("invalid Content-Length header") from exc
        if declared < 0:
            raise ValidationFailure("invalid Content-Length header")
        if declared > settings.max_upload_bytes:
            raise PayloadTooLarge(
                "upload exceeds the configured byte limit",
                details={"max_bytes": settings.max_upload_bytes},
            )
        storage.ensure_capacity(declared)
    else:
        storage.ensure_capacity(0)
    fd, tmp_name = tempfile.mkstemp(prefix="upload-", dir=settings.tmp_dir)
    path = Path(tmp_name)
    received = 0
    digest = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as fh:
            async for chunk in request.stream():
                if not chunk:
                    continue
                received += len(chunk)
                if received > settings.max_upload_bytes:
                    raise PayloadTooLarge(
                        "upload exceeds the configured byte limit while streaming",
                        details={"max_bytes": settings.max_upload_bytes},
                    )
                digest.update(chunk)
                fh.write(chunk)
            fh.flush()
            os.fsync(fh.fileno())
        if received == 0:
            raise ValidationFailure("the uploaded audio file is empty")
        storage.ensure_capacity(received)
        os.chmod(path, 0o600)
        return path, received, digest.hexdigest()
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _prepare_reference(
    *,
    settings: Settings,
    audio: AudioProcessor,
    staged: Path,
    audio_sha256: str,
    voice_id: str,
    reference_id: str,
    start_ms: int | None,
    end_ms: int | None,
    origin: str,
    session_id: str | None,
    storage: StorageManager,
) -> tuple[dict[str, Any], dict[str, Any]]:
    probe = audio.probe(staged)
    _, checks = audio.inspect(staged, probe)
    selected_start, selected_end = audio.validate_segment(probe.duration_ms, start_ms, end_ms)
    selected_checks = audio.checks_for_selection(probe, checks, selected_start, selected_end)
    reference_dir = settings.voices_dir / voice_id / "reference" / reference_id
    ensure_private_dir(reference_dir)
    ext = _extension_for_format(probe.format_name)
    original_abs = reference_dir / f"original.{ext}"
    normalized_abs = reference_dir / "reference.wav"
    try:
        atomic_copy(staged, original_abs, mode=0o600)
        normalized_ms = audio.normalize(
            original_abs,
            normalized_abs,
            start_ms=selected_start,
            end_ms=selected_end,
            sample_rate=settings.reference_sample_rate,
        )
        normalized_info = audio.wav_info(normalized_abs)
        if normalized_info["sample_width"] != 2:
            raise AudioDecodeError("normalized reference is not 16-bit PCM")
        reference = {
            "id": reference_id,
            "original_path": storage.relative(original_abs),
            "normalized_path": storage.relative(normalized_abs),
            "original_sha256": audio_sha256,
            "normalized_sha256": sha256_file(normalized_abs),
            "original_media_type": _media_type_for_format(probe.format_name),
            "original_format": probe.format_name,
            "source_duration_ms": probe.duration_ms,
            "source_sample_rate": probe.sample_rate,
            "source_channels": probe.channels,
            "selected_start_ms": selected_start,
            "selected_end_ms": selected_end,
            "reference_duration_ms": normalized_ms,
            "reference_sample_rate": settings.reference_sample_rate,
            "preparation": {
                "tool": "ffmpeg",
                "decode": "verified by ffprobe; content type not trusted",
                "mono": True,
                "sample_rate": settings.reference_sample_rate,
                "sample_format": "pcm_s16le",
                "trim": {"start_ms": selected_start, "end_ms": selected_end},
                "denoise": False,
                "loudness_normalization": "performed inside Chatterbox prepare_conditionals",
                "conditioning_only": True,
                "training": False,
            },
            "checks": selected_checks,
        }
        metadata = {
            "schema_version": 1,
            "origin": origin,
            "enrollment_session": session_id,
            "created_via": "enrollment" if session_id else "direct-upload",
            "authorization_attestation": "my_voice_or_permission_obtained",
            "selected_segment": {
                "start_ms": selected_start,
                "end_ms": selected_end,
                "duration_ms": normalized_ms,
            },
            "speaker_identity_verified": False,
            "background_noise_removed": False,
            "reference_conditioned": True,
            "model_weights_updated": False,
            "notice": "creating a voice stores and conditions on a reference recording; it does not train model weights",
        }
        return reference, metadata
    except BaseException:
        shutil.rmtree(reference_dir, ignore_errors=True)
        raise


def _normalize_staged_voice(
    *,
    settings: Settings,
    audio: AudioProcessor,
    staged: Path,
    audio_sha256: str,
    name: str,
    authorized: bool,
    start_ms: int | None,
    end_ms: int | None,
    origin: str,
    session_id: str | None,
    upload_id: str | None,
    store: Store,
    storage: StorageManager,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not authorized:
        raise ValidationFailure(
            "the owner must attest that this is their voice or that they have permission to use it"
        )
    voice_id = new_id("voc")
    reference_id = new_id("ref")
    reference, metadata = _prepare_reference(
        settings=settings,
        audio=audio,
        staged=staged,
        audio_sha256=audio_sha256,
        voice_id=voice_id,
        reference_id=reference_id,
        start_ms=start_ms,
        end_ms=end_ms,
        origin=origin,
        session_id=session_id,
        storage=storage,
    )
    audition_input, controls, seed = _audition_job_payload()
    try:
        voice, job = store.create_voice(
            voice_id=voice_id,
            name=name,
            backend=settings.backend,
            model_revision=settings.model_revision,
            authorized=True,
            metadata=metadata,
            reference=reference,
            session_id=session_id,
            upload_id=upload_id,
            audition={
                "input": audition_input,
                "controls": controls,
                "seed": seed,
                "preset": "master",
            },
            max_queue_depth=settings.max_queue_depth,
            max_attempts=settings.max_attempts,
        )
        if job is None:
            raise UnavailableError("voice was created but no audition job could be queued")
        return voice, job
    except BaseException:
        shutil.rmtree(settings.voices_dir / voice_id, ignore_errors=True)
        raise


def _revise_voice_reference(
    *,
    settings: Settings,
    audio: AudioProcessor,
    staged: Path,
    audio_sha256: str,
    voice_id: str,
    authorized: bool,
    start_ms: int | None,
    end_ms: int | None,
    store: Store,
    storage: StorageManager,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not authorized:
        raise ValidationFailure(
            "the owner must attest that this is their voice or that they have permission to use it"
        )
    store.get_voice_row(voice_id)
    reference_id = new_id("ref")
    reference, metadata = _prepare_reference(
        settings=settings,
        audio=audio,
        staged=staged,
        audio_sha256=audio_sha256,
        voice_id=voice_id,
        reference_id=reference_id,
        start_ms=start_ms,
        end_ms=end_ms,
        origin="direct-revision",
        session_id=None,
        storage=storage,
    )
    audition_input, controls, seed = _audition_job_payload()
    try:
        voice, job = store.replace_voice_reference(
            voice_id=voice_id,
            reference=reference,
            metadata=metadata,
            audition={
                "input": audition_input,
                "controls": controls,
                "seed": seed,
                "preset": "master",
            },
            max_queue_depth=settings.max_queue_depth,
            max_attempts=settings.max_attempts,
        )
        return voice, job
    except BaseException:
        shutil.rmtree(settings.voices_dir / voice_id / "reference" / reference_id, ignore_errors=True)
        raise


class _MaxBodySizeJSON:
    """Reject obviously oversized JSON payloads before parsing."""

    def __init__(self, app: Any, limit: int, upload_limit: int) -> None:
        self.app = app
        self.limit = limit
        self.upload_limit = upload_limit

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PATCH", "PUT"):
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        is_upload = (
            path.endswith("/uploads")
            or path.endswith("/reference")
            or path == "/api/v1/voices"
        )
        limit = self.upload_limit if is_upload else self.limit
        headers = dict(scope.get("headers") or [])
        length = headers.get(b"content-length")
        content_type = (headers.get(b"content-type") or b"").lower()
        if not is_upload and content_type.startswith(b"application/json") and not length:
            await JSONResponse(
                status_code=411,
                content={
                    "error": {
                        "code": "length_required",
                        "message": "JSON requests require a Content-Length header",
                        "retryable": False,
                        "details": {"max_bytes": limit},
                    }
                },
            )(scope, receive, send)
            return
        if length:
            try:
                if int(length) > limit:
                    await JSONResponse(
                        status_code=413,
                        content={
                            "error": {
                                "code": "payload_too_large",
                                "message": "request body exceeds the configured limit",
                                "retryable": False,
                                "details": {"max_bytes": limit},
                            }
                        },
                    )(scope, receive, send)
                    return
            except ValueError:
                pass
        await self.app(scope, receive, send)


def create_app(
    settings: Settings,
    *,
    start_worker: bool = True,
    worker: InferenceWorker | None = None,
) -> FastAPI:
    settings.validate()
    if len(settings.service_token) < 24:
        raise ConfigurationError("a service token of at least 24 characters is required")
    settings.ensure_directories()
    web_dir = Path(settings.web_dir)
    if not (web_dir / "index.html").is_file():
        raise UnavailableError("recorder web assets are missing")
    db = Database(settings.db_path)
    db.initialize()
    store = Store(db)
    audio = AudioProcessor(settings)
    storage = StorageManager(settings, store)
    storage.cleanup_temp(older_than_seconds=0)
    bearer = HTTPBearer(auto_error=False, scheme_name="ClawvoiceBearer")

    worker_instance = worker
    if start_worker and worker_instance is None:
        worker_instance = InferenceWorker(settings, store, audio)
    if worker_instance is not None and worker_instance.store is not store:
        # An injected worker may have been built with a different store (tests);
        # it is the caller's responsibility that they point at the same DB.
        pass

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if start_worker and worker_instance is not None:
            worker_instance.start_thread()
        try:
            yield
        finally:
            if worker_instance is not None:
                worker_instance.stop(timeout=10.0)

    app = FastAPI(
        title="clawvoice API",
        version=settings.service_version,
        description="Local reference-conditioned voice service. All voice/audio routes require a bearer token.",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.audio = audio
    app.state.storage = storage
    app.state.worker = worker_instance
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
        expose_headers=["Content-Length", "Content-Disposition", "X-Artifact-SHA256"],
        max_age=600,
    )
    app.add_middleware(
        _MaxBodySizeJSON,
        limit=settings.max_json_bytes,
        upload_limit=settings.max_upload_bytes,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Permissions-Policy", "microphone=(self)")
        if request.url.path.startswith("/api/v1"):
            response.headers.setdefault("Cache-Control", "no-store")
        if not request.url.path.startswith("/api/v1"):
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'",
            )
        return response

    @app.exception_handler(ClawvoiceError)
    async def clawvoice_error_handler(request: Request, exc: ClawvoiceError) -> JSONResponse:
        headers: dict[str, str] = {}
        if isinstance(exc, (AuthenticationError, AuthorizationError)):
            headers["WWW-Authenticate"] = 'Bearer realm="clawvoice"'
        return JSONResponse(exc.as_dict(), status_code=exc.http_status, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = []
        for item in exc.errors():
            errors.append(
                {
                    "loc": [str(part) for part in item.get("loc", ())],
                    "type": item.get("type", "validation_error"),
                    "msg": item.get("msg", "invalid value"),
                }
            )
        payload = {
            "error": {
                "code": "validation_error",
                "message": "request validation failed",
                "retryable": False,
                "details": {"errors": errors[:20]},
            }
        }
        return JSONResponse(payload, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "not_found" if exc.status_code == 404 else "http_error"
        payload = {
            "error": {
                "code": code,
                "message": "the requested resource was not found"
                if exc.status_code == 404
                else "request failed",
                "retryable": False,
                "details": {},
            }
        }
        return JSONResponse(payload, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled API error on %s", request.url.path)
        return JSONResponse(
            {
                "error": {
                    "code": "internal_error",
                    "message": "internal service error",
                    "retryable": False,
                    "details": {},
                }
            },
            status_code=500,
        )

    def _bearer_token(credentials: HTTPAuthorizationCredentials | None) -> str:
        if credentials is None or credentials.scheme.lower() != "bearer":
            raise AuthenticationError("a bearer token is required")
        token = credentials.credentials.strip()
        if not token:
            raise AuthenticationError("a bearer token is required")
        return token

    def require_service(
        credentials: HTTPAuthorizationCredentials | None = Security(bearer),
    ) -> None:
        token = _bearer_token(credentials)
        if not hmac.compare_digest(token, settings.service_token):
            raise AuthenticationError("invalid service credential")

    def require_enrollment(
        session_id: str,
        credentials: HTTPAuthorizationCredentials | None = Security(bearer),
    ) -> dict[str, Any]:
        token = _bearer_token(credentials)
        try:
            validate_id(session_id, prefixes=("enr",))
        except ValueError as exc:
            raise NotFoundError("enrollment session not found") from exc
        session = store.get_enrollment_session(session_id)
        if not hmac.compare_digest(token_hash(token), session["token_hash"]):
            raise AuthenticationError("invalid enrollment credential")
        if session["status"] == "expired" or session["expires_at"] <= now_ms():
            raise AuthenticationError("enrollment credential has expired")
        store.touch_enrollment(session_id)
        return session

    def _require_open(session: dict[str, Any]) -> None:
        if session["status"] != "open" or session["expires_at"] <= now_ms():
            raise ConflictError("enrollment session is expired or already used")

    def _resolve_voice_id(requested: str | None) -> str:
        if requested:
            try:
                validate_id(requested, prefixes=("voc",))
            except ValueError as exc:
                raise NotFoundError("voice not found") from exc
            return requested
        default = store.get_setting("default_voice_id")
        if not default:
            raise ValidationFailure("no voice_id was supplied and no default voice is selected")
        return default

    # ------------------------------------------------------------- liveness
    @app.get("/api/v1/health/live", tags=["health"])
    def live() -> dict[str, Any]:
        return {"status": "live", "service": "clawvoice", "version": settings.service_version}

    @app.get(
        "/api/v1/health/ready",
        tags=["health"],
        dependencies=[Depends(require_service)],
        responses={503: {"model": ErrorResponse}},
    )
    def ready() -> JSONResponse:
        now = now_ms()
        worker_info = store.worker_lock_view()
        checks: dict[str, Any] = {}
        backend = settings.backend
        checks["backend"] = backend
        if backend == "turbo":
            problems = missing_or_bad_files(settings.model_dir, backend)
            checks["model"] = {
                "status": "missing" if problems else "present",
                "revision": settings.model_revision,
                "problems": problems[:12],
            }
        else:
            checks["model"] = {"status": "test-only", "real_model": False}
        owner = worker_info.get("owner")
        lease = worker_info.get("lease_expires_at") or 0
        worker_online = bool(owner and lease >= now)
        checks["worker"] = {
            "online": worker_online,
            "status": worker_info.get("status"),
            "heartbeat_at": iso_from_ms(worker_info.get("heartbeat_at")),
            "model_revision": worker_info.get("detail", {}).get("model_revision"),
            "device": worker_info.get("detail", {}).get("device"),
            "real_model": worker_info.get("detail", {}).get("real_model"),
        }
        checks["queue"] = {"depth": store.queue_depth(), "counts": store.job_counts()}
        checks["storage"] = {
            "data_dir_writable": os.access(settings.data_dir, os.W_OK),
            "usage_bytes": storage.usage_bytes(),
            "max_data_bytes": settings.max_data_bytes,
        }
        checks["ffmpeg"] = shutil.which(settings.ffmpeg) is not None
        status = "ready"
        reason = "ready"
        if backend == "turbo" and checks["model"]["status"] == "missing":
            status, reason = "not_ready", "model_missing"
        elif not checks["storage"]["data_dir_writable"] or not checks["ffmpeg"]:
            status, reason = "not_ready", "service_dependency_unavailable"
        elif not worker_online:
            status, reason = "not_ready", "worker_offline"
        elif worker_info.get("status") == "model_missing":
            status, reason = "not_ready", "model_missing"
        elif worker_info.get("status") == "loading":
            status, reason = "not_ready", "worker_loading"
        elif worker_info.get("status") in ("starting", "waiting_for_lock"):
            status, reason = "not_ready", "worker_cold"
        elif worker_info.get("status") == "failed":
            status, reason = "not_ready", "worker_failed"
        elif not checks["ffmpeg"]:
            status, reason = "not_ready", "ffmpeg_missing"
        body = {"status": status, "reason": reason, "checks": checks}
        return JSONResponse(body, status_code=200 if status == "ready" else 503)

    @app.get(
        "/api/v1/capabilities",
        tags=["service"],
        dependencies=[Depends(require_service)],
    )
    def capabilities() -> dict[str, Any]:
        model = {
            "backend": settings.backend,
            "revision": settings.model_revision,
            "sample_rate": settings.reference_sample_rate,
            "language": "en",
            "real_model": settings.backend == "turbo",
        }
        return {
            "service_version": settings.service_version,
            "api_version": "v1",
            "model": model,
            "output_presets": ["master", "mp3", "telephony", "telephony-mulaw"],
            "presets": {
                "master": "mono PCM WAV at the model's native 24000 Hz",
                "mp3": "listening/download copy; master is retained",
                "telephony": "derived 8 kHz mono PCM WAV; master is retained",
                "telephony-mulaw": "derived raw 8 kHz mono mu-law; documented separately",
            },
            "controls": {
                "temperature": {"min": 0.05, "max": 2.0, "default": 0.8},
                "top_p": {"min": 0.0, "max": 1.0, "default": 0.95},
                "top_k": {"min": 1, "max": 10000, "default": 1000},
                "repetition_penalty": {"min": 0.5, "max": 2.0, "default": 1.2},
                "cfg_weight": {"supported": False, "reason": "pinned Turbo ignores CFG"},
                "exaggeration": {"supported": False, "reason": "pinned Turbo ignores it"},
            },
            "seed": {
                "supported": True,
                "reproducibility": "not guaranteed bit-for-bit across hardware or library versions",
            },
            "limits": {
                "max_upload_bytes": settings.max_upload_bytes,
                "max_text_chars": settings.max_text_chars,
                "max_segments": settings.max_segments,
                "max_pause_ms": settings.max_pause_ms,
                "max_total_pause_ms": settings.max_total_pause_ms,
                "max_output_ms": settings.max_output_ms,
                "max_reference_ms": settings.reference_max_ms,
                "max_queue_depth": settings.max_queue_depth,
                "artifact_retention_days": settings.artifact_retention_days,
            },
            "enrollment": {
                "browser_microphone": True,
                "file_upload": True,
                "accepted_formats": "whatever the installed FFmpeg decoder supports (WAV/MP3/WebM/Opus/MP4/AAC tested paths)",
                "session_ttl_seconds": settings.enrollment_ttl_seconds,
            },
            "generation_side_effects": {
                "publishes_audio": False,
                "changes_twilio": False,
                "sends_messages": False,
            },
        }

    # ------------------------------------------------------------ enrollment
    @app.post(
        "/api/v1/enrollment-sessions",
        status_code=201,
        tags=["enrollment"],
        dependencies=[Depends(require_service)],
        responses={401: {"model": ErrorResponse}},
    )
    def create_enrollment_session(body: EnrollmentSessionRequest) -> dict[str, Any]:
        name = _safe_name(body.name)
        token = new_secret(32)
        session = store.create_enrollment_session(
            name=name,
            token_hash=token_hash(token),
            ttl_seconds=settings.enrollment_ttl_seconds,
            origin=None,
        )
        recorder_url = (
            f"{settings.public_base_url.rstrip('/')}/enroll/{session['id']}"
            f"#token={token}"
        )
        view = _session_view(session)
        view["recorder_url"] = recorder_url
        view["warning"] = (
            "The URL fragment is a single-enrollment credential. Do not paste it into logs or share it."
        )
        return view

    @app.get(
        "/api/v1/enrollment-sessions/{session_id}",
        tags=["enrollment"],
    )
    def get_enrollment_session(
        session_id: str, session: dict[str, Any] = Depends(require_enrollment)
    ) -> dict[str, Any]:
        view = _session_view(session)
        upload = None
        if session.get("upload_id"):
            try:
                upload_row = store.get_enrollment_upload(session_id, session["upload_id"])
                upload = _upload_view(upload_row)
            except NotFoundError:
                upload = None
        view["upload"] = upload
        view["audition"] = store.enrollment_audition_job(session_id)
        view["limits"] = {
            "max_upload_bytes": settings.max_upload_bytes,
            "reference_max_ms": settings.reference_max_ms,
            "recommended_min_ms": 10_000,
            "recommended_max_ms": 15_000,
            "minimum_ms": 5_001,
            "max_pause_ms": settings.max_pause_ms,
        }
        return view

    @app.post(
        "/api/v1/enrollment-sessions/{session_id}/uploads",
        status_code=201,
        tags=["enrollment"],
    )
    async def upload_enrollment_sample(
        request: Request,
        session_id: str,
        session: dict[str, Any] = Depends(require_enrollment),
    ) -> dict[str, Any]:
        _require_open(session)
        staged, size, digest = await _read_body_to_temp(
            request, settings=settings, storage=storage
        )
        try:
            probe = audio.probe(staged)
            _, checks = audio.inspect(staged, probe)
            upload_id = new_id("upl")
            upload_dir = settings.enrollment_dir / session_id / upload_id
            ensure_private_dir(upload_dir)
            ext = _extension_for_format(probe.format_name)
            destination = upload_dir / f"original.{ext}"
            atomic_copy(staged, destination, mode=0o600)
            old_paths = store.replace_enrollment_upload(
                upload_id=upload_id,
                session_id=session_id,
                original_path=storage.relative(destination),
                media_type=_media_type_for_format(probe.format_name),
                format_name=probe.format_name,
                duration_ms=probe.duration_ms,
                sample_rate=probe.sample_rate,
                channels=probe.channels,
                size_bytes=size,
                sha256=digest,
                checks=checks.as_dict(),
            )
            for old in old_paths:
                storage.remove_relative(old)
            upload = store.get_enrollment_upload(session_id, upload_id)
            return _upload_view(upload)
        finally:
            staged.unlink(missing_ok=True)

    @app.get(
        "/api/v1/enrollment-sessions/{session_id}/uploads/{upload_id}",
        tags=["enrollment"],
    )
    def get_enrollment_upload(
        session_id: str,
        upload_id: str,
        session: dict[str, Any] = Depends(require_enrollment),
    ) -> dict[str, Any]:
        try:
            validate_id(upload_id, prefixes=("upl",))
        except ValueError as exc:
            raise NotFoundError("upload not found") from exc
        return _upload_view(store.get_enrollment_upload(session_id, upload_id))

    @app.post(
        "/api/v1/enrollment-sessions/{session_id}/voices",
        status_code=201,
        tags=["enrollment"],
    )
    def create_voice_from_enrollment(
        session_id: str,
        body: EnrollmentVoiceRequest,
        session: dict[str, Any] = Depends(require_enrollment),
    ) -> dict[str, Any]:
        _require_open(session)
        if not session.get("upload_id"):
            raise ConflictError("upload a sample before creating the voice")
        if body.upload_id != session["upload_id"]:
            raise ConflictError("upload does not belong to this enrollment session")
        name = _safe_name(body.name)
        upload_row = store.get_enrollment_upload(session_id, body.upload_id)
        staged = storage.resolve(upload_row["original_path"])
        voice, job = _normalize_staged_voice(
            settings=settings,
            audio=audio,
            staged=staged,
            audio_sha256=upload_row["sha256"],
            name=name,
            authorized=body.authorized,
            start_ms=body.start_ms,
            end_ms=body.end_ms,
            origin="enrollment",
            session_id=session_id,
            upload_id=body.upload_id,
            store=store,
            storage=storage,
        )
        # The store transaction removed the staging row. The copied original
        # remains in the voice directory; remove only the now-orphaned staged file.
        try:
            staged.unlink(missing_ok=True)
            staged.parent.rmdir()
        except OSError:
            pass
        return {"voice": voice, "audition_job": job, "create_voice_once": True}

    @app.get(
        "/api/v1/enrollment-sessions/{session_id}/audition",
        tags=["enrollment"],
    )
    def enrollment_audition(
        session_id: str, session: dict[str, Any] = Depends(require_enrollment)
    ) -> dict[str, Any]:
        job = store.enrollment_audition_job(session_id)
        if job is None:
            raise NotFoundError("no audition job exists for this enrollment session")
        return {"job": job, "artifacts": store.enrollment_artifacts(session_id)}

    @app.get(
        "/api/v1/enrollment-sessions/{session_id}/artifacts/{artifact_id}/download",
        tags=["enrollment"],
    )
    def enrollment_artifact_download(
        session_id: str,
        artifact_id: str,
        session: dict[str, Any] = Depends(require_enrollment),
    ) -> Response:
        try:
            validate_id(artifact_id, prefixes=("art",))
        except ValueError as exc:
            raise NotFoundError("artifact not found") from exc
        row = store.artifact_path_for_session(session_id, artifact_id)
        return _artifact_response(row)

    # ----------------------------------------------------------------- voices
    @app.get(
        "/api/v1/voices",
        tags=["voices"],
        dependencies=[Depends(require_service)],
    )
    def list_voices() -> dict[str, Any]:
        return {"voices": store.list_voices(), "default_voice_id": store.get_setting("default_voice_id")}

    @app.post(
        "/api/v1/voices",
        status_code=201,
        tags=["voices"],
        dependencies=[Depends(require_service)],
    )
    async def create_voice_direct(
        request: Request,
        name: str,
        authorized: bool = False,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> dict[str, Any]:
        clean_name = _safe_name(name)
        if not authorized:
            raise ValidationFailure(
                "the owner must attest that this is their voice or that they have permission to use it"
            )
        staged, _size, digest = await _read_body_to_temp(
            request, settings=settings, storage=storage
        )
        try:
            voice, job = _normalize_staged_voice(
                settings=settings,
                audio=audio,
                staged=staged,
                audio_sha256=digest,
                name=clean_name,
                authorized=authorized,
                start_ms=start_ms,
                end_ms=end_ms,
                origin="direct-upload",
                session_id=None,
                upload_id=None,
                store=store,
                storage=storage,
            )
            return {"voice": voice, "audition_job": job}
        finally:
            staged.unlink(missing_ok=True)

    @app.post(
        "/api/v1/voices/{voice_id}/reference",
        status_code=202,
        tags=["voices"],
        dependencies=[Depends(require_service)],
    )
    async def revise_voice_reference(
        request: Request,
        voice_id: str,
        authorized: bool = False,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> dict[str, Any]:
        _validate_voice_id(voice_id)
        if not authorized:
            raise ValidationFailure(
                "the owner must attest that this is their voice or that they have permission to use it"
            )
        staged, _size, digest = await _read_body_to_temp(
            request, settings=settings, storage=storage
        )
        try:
            voice, job = _revise_voice_reference(
                settings=settings,
                audio=audio,
                staged=staged,
                audio_sha256=digest,
                voice_id=voice_id,
                authorized=True,
                start_ms=start_ms,
                end_ms=end_ms,
                store=store,
                storage=storage,
            )
            return {"voice": voice, "audition_job": job, "revised": True}
        finally:
            staged.unlink(missing_ok=True)

    @app.get(
        "/api/v1/voices/{voice_id}",
        tags=["voices"],
        dependencies=[Depends(require_service)],
    )
    def get_voice(voice_id: str) -> dict[str, Any]:
        _validate_voice_id(voice_id)
        return store.get_voice(voice_id)

    @app.patch(
        "/api/v1/voices/{voice_id}",
        tags=["voices"],
        dependencies=[Depends(require_service)],
    )
    def rename_voice(voice_id: str, body: VoiceRenameRequest) -> dict[str, Any]:
        _validate_voice_id(voice_id)
        return store.rename_voice(voice_id, _safe_name(body.name))

    @app.delete(
        "/api/v1/voices/{voice_id}",
        tags=["voices"],
        dependencies=[Depends(require_service)],
    )
    def delete_voice(voice_id: str) -> dict[str, Any]:
        _validate_voice_id(voice_id)
        paths = store.delete_voice(voice_id)
        for path in paths:
            storage.remove_relative(path)
        shutil.rmtree(settings.voices_dir / voice_id, ignore_errors=True)
        storage.cleanup_empty_dirs()
        return {"deleted": True, "id": voice_id, "files_removed": len(paths)}

    @app.post(
        "/api/v1/voices/{voice_id}/select",
        tags=["voices"],
        dependencies=[Depends(require_service)],
    )
    def select_voice(voice_id: str) -> dict[str, Any]:
        _validate_voice_id(voice_id)
        return store.select_voice(voice_id)

    @app.post(
        "/api/v1/voices/{voice_id}/preview",
        status_code=202,
        tags=["voices"],
        dependencies=[Depends(require_service)],
    )
    def preview_voice(voice_id: str) -> dict[str, Any]:
        _validate_voice_id(voice_id)
        store.get_voice(voice_id)
        job = store.create_audition_job(
            voice_id=voice_id,
            input_payload={"text": PREVIEW_AUDITION_TEXT},
            controls=default_controls(),
            seed=secrets.randbelow(2**31 - 1),
            max_queue_depth=settings.max_queue_depth,
            max_attempts=settings.max_attempts,
        )
        return {"job": job}

    # -------------------------------------------------------------- synthesis
    @app.post(
        "/api/v1/synthesis-jobs",
        status_code=202,
        tags=["synthesis"],
        dependencies=[Depends(require_service)],
    )
    def create_synthesis(body: SynthesisRequest, response: Response) -> dict[str, Any]:
        voice_id = _resolve_voice_id(body.voice_id)
        voice = store.get_voice_row(voice_id)
        if voice["backend"] != settings.backend:
            raise ValidationFailure(
                "the selected voice is not compatible with this service backend",
                details={"voice_backend": voice["backend"], "service_backend": settings.backend},
            )
        payload = body.input_payload()
        try:
            plan = build_plan(
                payload,
                settings=settings,
                controls=body.controls_payload(),
                seed=body.seed,
            )
        except ValueError as exc:
            raise ValidationFailure("invalid synthesis input") from exc
        job = store.create_synthesis_job(
            voice_id=voice_id,
            preset=body.preset,
            input_payload=payload,
            controls=plan.controls,
            seed=plan.seed,
            max_queue_depth=settings.max_queue_depth,
            max_attempts=settings.max_attempts,
        )
        response.headers["Location"] = f"/api/v1/jobs/{job['id']}"
        return {"job": job, "request": {"text_sha256": plan.text_sha256, "chunks": plan.chunk_count}}

    @app.get(
        "/api/v1/jobs/{job_id}",
        tags=["jobs"],
        dependencies=[Depends(require_service)],
    )
    def get_job(job_id: str) -> dict[str, Any]:
        try:
            validate_id(job_id, prefixes=("job",))
        except ValueError as exc:
            raise NotFoundError("job not found") from exc
        return store.get_job(job_id)

    @app.post(
        "/api/v1/jobs/{job_id}/cancel",
        status_code=202,
        tags=["jobs"],
        dependencies=[Depends(require_service)],
    )
    def cancel_job(job_id: str) -> dict[str, Any]:
        try:
            validate_id(job_id, prefixes=("job",))
        except ValueError as exc:
            raise NotFoundError("job not found") from exc
        job = store.request_cancel(job_id)
        if job["status"] in ("succeeded", "failed", "canceled"):
            job["note"] = "job is already terminal"
        return job

    # -------------------------------------------------------------- artifacts
    @app.get(
        "/api/v1/artifacts/{artifact_id}",
        tags=["artifacts"],
        dependencies=[Depends(require_service)],
    )
    def get_artifact(artifact_id: str) -> dict[str, Any]:
        try:
            validate_id(artifact_id, prefixes=("art",))
        except ValueError as exc:
            raise NotFoundError("artifact not found") from exc
        return store.get_artifact(artifact_id)

    @app.get(
        "/api/v1/artifacts/{artifact_id}/download",
        tags=["artifacts"],
        dependencies=[Depends(require_service)],
    )
    def artifact_download(artifact_id: str) -> Response:
        try:
            validate_id(artifact_id, prefixes=("art",))
        except ValueError as exc:
            raise NotFoundError("artifact not found") from exc
        return _artifact_response(store.get_artifact_row(artifact_id))

    def _artifact_response(row: dict[str, Any]) -> Response:
        try:
            path = storage.resolve(row["path"])
        except ValidationFailure as exc:
            raise NotFoundError("artifact file is no longer available") from exc
        if not path.is_file() or path.is_symlink():
            raise NotFoundError("artifact file is no longer available")
        extension = _artifact_extension(row["format"])
        filename = f"clawvoice-{row['id']}.{extension}"
        iterator = _iter_artifact(path)
        headers = {
            "Content-Length": str(path.stat().st_size),
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Artifact-SHA256": row["sha256"],
            "Cache-Control": "private, no-store",
        }
        return StreamingResponse(iterator, media_type=row["media_type"], headers=headers)

    async def _iter_artifact(path: Path) -> AsyncIterator[bytes]:
        import anyio

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            raise NotFoundError("artifact file is no longer available") from exc
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise NotFoundError("artifact file is no longer available")
            while True:
                chunk = await anyio.to_thread.run_sync(os.read, fd, 64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            os.close(fd)

    def _validate_voice_id(voice_id: str) -> None:
        try:
            validate_id(voice_id, prefixes=("voc",))
        except ValueError as exc:
            raise NotFoundError("voice not found") from exc

    # ------------------------------------------------------------------- web
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(web_dir / "favicon.svg", media_type="image/svg+xml")

    @app.get("/", include_in_schema=False)
    def root_page() -> FileResponse:
        return FileResponse(web_dir / "index.html", media_type="text/html")

    @app.get("/enroll/{session_id}", include_in_schema=False)
    def enroll_page(session_id: str) -> FileResponse:
        try:
            validate_id(session_id, prefixes=("enr",))
        except ValueError as exc:
            raise NotFoundError("enrollment session not found") from exc
        return FileResponse(web_dir / "index.html", media_type="text/html")

    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")
    return app
