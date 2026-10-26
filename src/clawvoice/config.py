"""Configuration is environment/runtime-secret driven and has no ML imports."""

from __future__ import annotations

import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import ConfigurationError
from .modelspec import TURBO_REFERENCE_MAX_MS, TURBO_REVISION, TURBO_SAMPLE_RATE
from .util import atomic_write, ensure_private_dir, new_secret

DEFAULT_PORT = 8765
PRESETS = ("master", "mp3", "telephony", "telephony-mulaw")
SUPPORTED_BACKENDS = ("turbo", "fake")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _float_env(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}")
    return value


def _origin_of(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigurationError("CLAWVOICE_PUBLIC_BASE_URL must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise ConfigurationError("CLAWVOICE_PUBLIC_BASE_URL must not contain credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ConfigurationError(
            "CLAWVOICE_PUBLIC_BASE_URL must be an origin without a path, query or fragment"
        )
    return f"{parts.scheme}://{parts.netloc}"


def _parse_origins(raw: str | None, base_url: str) -> tuple[str, ...]:
    if not raw:
        return (_origin_of(base_url),)
    origins: list[str] = []
    for value in raw.split(","):
        value = value.strip().rstrip("/")
        if not value:
            continue
        if value == "*" or "*" in value:
            raise ConfigurationError("wildcard CORS origins are not allowed")
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ConfigurationError(f"invalid allowed origin: {value}")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ConfigurationError(f"allowed origin must be an origin only: {value}")
        if value not in origins:
            origins.append(value)
    if not origins:
        raise ConfigurationError("at least one allowed origin is required")
    return tuple(origins)


def find_web_dir() -> Path:
    env = _env("CLAWVOICE_WEB_DIR")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env).expanduser())
    here = Path(__file__).resolve()
    candidates.extend(
        [
            here.parent / "web",
            here.parents[2] / "web" if len(here.parents) > 2 else here.parent / "web",
        ]
    )
    try:
        import sysconfig

        candidates.append(Path(sysconfig.get_path("data")) / "share" / "clawvoice" / "web")
    except Exception:  # pragma: no cover - platform dependent
        pass
    for candidate in candidates:
        if (candidate / "index.html").is_file():
            return candidate.resolve()
    return candidates[0]


def read_service_token(path: Path, *, create: bool) -> str:
    env_token = _env("CLAWVOICE_SERVICE_TOKEN")
    if env_token:
        if len(env_token) < 24:
            raise ConfigurationError("CLAWVOICE_SERVICE_TOKEN must contain at least 24 characters")
        return env_token
    if path.is_file():
        try:
            if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise ConfigurationError(
                    f"service token file must be owner-only (chmod 600): {path}"
                )
            token = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError(f"cannot read service token file: {path}") from exc
        if len(token) < 24:
            raise ConfigurationError(f"service token file is invalid: {path}")
        return token
    if not create:
        raise ConfigurationError(f"service token file is missing: {path}")
    ensure_private_dir(path.parent)
    token = new_secret(32)
    atomic_write(path, (token + "\n").encode("utf-8"), mode=0o600)
    print(f"clawvoice: generated service token at {path} (mode 0600)", file=sys.stderr)
    return token


@dataclass(slots=True)
class Settings:
    data_dir: Path
    db_path: Path
    model_dir: Path
    token_file: Path
    service_token: str = field(repr=False)
    backend: str = "turbo"
    device: str = "cuda"
    model_revision: str = TURBO_REVISION
    public_base_url: str = f"http://127.0.0.1:{DEFAULT_PORT}"
    allowed_origins: tuple[str, ...] = (f"http://127.0.0.1:{DEFAULT_PORT}",)
    bind_host: str = "127.0.0.1"
    bind_port: int = DEFAULT_PORT
    web_dir: Path = field(default_factory=find_web_dir)
    allow_fake_backend: bool = False
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    max_upload_bytes: int = 50 * 1024 * 1024
    max_json_bytes: int = 1024 * 1024
    max_reference_decoded_ms: int = 120_000
    max_text_chars: int = 5_000
    max_segments: int = 64
    max_pause_ms: int = 10_000
    max_total_pause_ms: int = 60_000
    max_output_ms: int = 180_000
    max_queue_depth: int = 32
    max_attempts: int = 2
    job_timeout_seconds: int = 300
    worker_lease_seconds: int = 90
    worker_heartbeat_seconds: int = 10
    enrollment_ttl_seconds: int = 1_800
    artifact_retention_days: int = 7
    cleanup_interval_seconds: int = 3_600
    max_data_bytes: int = 20 * 1024 * 1024 * 1024
    reference_sample_rate: int = TURBO_SAMPLE_RATE
    reference_max_ms: int = TURBO_REFERENCE_MAX_MS
    decoder_timeout_seconds: int = 60
    probe_timeout_seconds: int = 15
    max_chunks_per_text: int = 48
    chunk_join_gap_ms: int = 80
    chunk_target_chars: int = 280
    log_level: str = "info"
    service_version: str = "0.1.0"

    @property
    def voices_dir(self) -> Path:
        return self.data_dir / "voices"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def enrollment_dir(self) -> Path:
        return self.data_dir / "enrollment"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    def ensure_directories(self) -> None:
        for directory in (
            self.data_dir,
            self.voices_dir,
            self.artifacts_dir,
            self.enrollment_dir,
            self.tmp_dir,
            self.state_dir,
            self.token_file.parent,
        ):
            ensure_private_dir(directory)

    def validate(self) -> None:
        if self.backend not in SUPPORTED_BACKENDS:
            raise ConfigurationError(f"unsupported backend: {self.backend}")
        if self.backend == "fake" and not self.allow_fake_backend:
            raise ConfigurationError(
                "the fake backend is test-only; set CLAWVOICE_ALLOW_FAKE=1 explicitly"
            )
        if self.device not in ("cuda", "cpu"):
            raise ConfigurationError("CLAWVOICE_DEVICE must be cuda or cpu")
        if self.backend == "fake" and self.device != "cpu":
            raise ConfigurationError("the fake backend only supports device=cpu")
        if self.model_revision != TURBO_REVISION:
            raise ConfigurationError(
                "this release only supports the pinned model revision "
                f"{TURBO_REVISION}; see docs/compatibility.md"
            )
        if self.max_upload_bytes < 1024:
            raise ConfigurationError("upload limit is too small")
        if self.max_output_ms <= 0 or self.max_text_chars <= 0:
            raise ConfigurationError("limits must be positive")
        if self.artifact_retention_days < 1:
            raise ConfigurationError("artifact retention must be at least one day")
        if self.cleanup_interval_seconds < 0:
            raise ConfigurationError("cleanup interval cannot be negative")
        _origin_of(self.public_base_url)
        for origin in self.allowed_origins:
            if origin == "*":
                raise ConfigurationError("wildcard origins are not allowed")
        if not self.ffmpeg or not self.ffprobe:
            raise ConfigurationError("ffmpeg and ffprobe are required")
        if not isinstance(self.web_dir, Path):
            self.web_dir = Path(self.web_dir)

    @classmethod
    def from_env(
        cls,
        *,
        create_dirs: bool = True,
        create_token: bool = False,
        require_token: bool = True,
    ) -> Settings:
        data_dir = Path(_env("CLAWVOICE_DATA_DIR", "./var")).expanduser().resolve()
        token_file = Path(
            _env("CLAWVOICE_TOKEN_FILE", str(data_dir / "secrets" / "service-token"))
        ).expanduser()
        if not token_file.is_absolute():
            token_file = (Path.cwd() / token_file).resolve()
        base_url = _env("CLAWVOICE_PUBLIC_BASE_URL", f"http://127.0.0.1:{DEFAULT_PORT}")
        settings = cls(
            data_dir=data_dir,
            db_path=data_dir / "clawvoice.sqlite3",
            model_dir=Path(_env("CLAWVOICE_MODEL_DIR", "./models/chatterbox-turbo"))
            .expanduser()
            .resolve(),
            token_file=token_file,
            service_token=(
                read_service_token(token_file, create=create_token)
                if require_token or create_token
                else ""
            ),
            backend=_env("CLAWVOICE_BACKEND", "turbo") or "turbo",
            device=_env("CLAWVOICE_DEVICE", "cuda") or "cuda",
            model_revision=_env("CLAWVOICE_MODEL_REVISION", TURBO_REVISION) or TURBO_REVISION,
            public_base_url=base_url,
            allowed_origins=_parse_origins(_env("CLAWVOICE_ALLOWED_ORIGINS"), base_url),
            bind_host=_env("CLAWVOICE_BIND_HOST", "127.0.0.1") or "127.0.0.1",
            bind_port=_int_env("CLAWVOICE_BIND_PORT", DEFAULT_PORT, 1, 65535),
            allow_fake_backend=_env("CLAWVOICE_ALLOW_FAKE", "0") == "1",
            max_upload_bytes=_int_env(
                "CLAWVOICE_MAX_UPLOAD_BYTES", 50 * 1024 * 1024, 1024, 500 * 1024 * 1024
            ),
            max_text_chars=_int_env("CLAWVOICE_MAX_TEXT_CHARS", 5_000, 1, 50_000),
            max_output_ms=_int_env("CLAWVOICE_MAX_OUTPUT_MS", 180_000, 1_000, 1_800_000),
            max_queue_depth=_int_env("CLAWVOICE_MAX_QUEUE_DEPTH", 32, 1, 1_000),
            job_timeout_seconds=_int_env("CLAWVOICE_JOB_TIMEOUT_SECONDS", 300, 10, 3_600),
            worker_lease_seconds=_int_env("CLAWVOICE_WORKER_LEASE_SECONDS", 90, 15, 3_600),
            artifact_retention_days=_int_env("CLAWVOICE_ARTIFACT_RETENTION_DAYS", 7, 1, 365),
            cleanup_interval_seconds=_int_env(
                "CLAWVOICE_CLEANUP_INTERVAL_SECONDS", 3_600, 0, 86_400
            ),
            max_data_bytes=_int_env(
                "CLAWVOICE_MAX_DATA_BYTES", 20 * 1024 * 1024 * 1024, 1024**3, 10 * 1024**4
            ),
            decoder_timeout_seconds=_int_env(
                "CLAWVOICE_DECODER_TIMEOUT_SECONDS", 60, 5, 600
            ),
            log_level=_env("CLAWVOICE_LOG_LEVEL", "info") or "info",
        )
        settings.validate()
        if create_dirs:
            settings.ensure_directories()
        return settings


__all__ = [
    "DEFAULT_PORT",
    "PRESETS",
    "SUPPORTED_BACKENDS",
    "Settings",
    "find_web_dir",
    "read_service_token",
]
