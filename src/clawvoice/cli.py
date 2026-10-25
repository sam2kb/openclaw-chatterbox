"""clawvoice command line client and operational entrypoints."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .client import (
    ApiError,
    Client,
    ClientConfig,
    ClientConfigurationError,
    ClientUnavailableError,
)
from .errors import ClawvoiceError, ConfigurationError
from .modelspec import TURBO_REVISION

EXIT_OK = 0
EXIT_VALIDATION = 2
EXIT_CONFIG = 3
EXIT_UNAVAILABLE = 4
EXIT_JOB_FAILED = 5
EXIT_JOB_CANCELED = 6

log = logging.getLogger("clawvoice.cli")


def _json_mode(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "json", False) or getattr(args, "json_global", False))


def _emit(payload: Any, args: argparse.Namespace, human: str | None = None) -> None:
    if _json_mode(args):
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        sys.stdout.flush()
    elif human is not None:
        print(human)


def _emit_error(exc: BaseException, args: argparse.Namespace, exit_code: int) -> int:
    if isinstance(exc, ApiError):
        payload = {
            "error": {
                "code": exc.code,
                "message": exc.message,
                "status": exc.status,
                "details": exc.details,
            }
        }
    elif isinstance(exc, ClawvoiceError):
        payload = exc.as_dict()
    else:
        payload = {
            "error": {
                "code": "client_error",
                "message": str(exc),
                "retryable": False,
                "details": {},
            }
        }
    if _json_mode(args):
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        sys.stdout.flush()
    else:
        print(f"clawvoice: {payload['error']['message']}", file=sys.stderr)
    return exit_code


def _api_exit_code(exc: ApiError) -> int:
    if exc.status in (401, 403):
        return EXIT_CONFIG
    if exc.status in (404, 409, 413, 415, 422):
        return EXIT_VALIDATION
    if exc.status in (429, 502, 503, 507):
        return EXIT_UNAVAILABLE
    return 1


def _client(args: argparse.Namespace) -> Client:
    return Client(ClientConfig.from_env(require_token=True))


def _settings(*, create_token: bool = False, create_dirs: bool = False, require_token: bool = True):
    from .config import Settings

    return Settings.from_env(
        create_token=create_token,
        create_dirs=create_dirs,
        require_token=require_token,
    )


def _print_json_or(payload: Any, args: argparse.Namespace, human: str) -> None:
    _emit(payload, args, human)


def _wait_for_job(
    client: Client, job_id: str, *, timeout: float, interval: float = 1.0
) -> tuple[dict[str, Any], int | None]:
    deadline = time.monotonic() + timeout
    last_status = None
    while True:
        job = client.get_job(job_id)
        status = job.get("status")
        if status != last_status:
            print(f"clawvoice: job {job_id}: {status}", file=sys.stderr)
            last_status = status
        if status == "succeeded":
            return job, None
        if status == "failed":
            return job, EXIT_JOB_FAILED
        if status == "canceled":
            return job, EXIT_JOB_CANCELED
        if time.monotonic() >= deadline:
            return job, EXIT_UNAVAILABLE
        time.sleep(interval)


def _controls_from(args: argparse.Namespace) -> dict[str, Any]:
    controls: dict[str, Any] = {}
    if args.temperature is not None:
        controls["temperature"] = args.temperature
    if args.top_p is not None:
        controls["top_p"] = args.top_p
    if args.top_k is not None:
        controls["top_k"] = args.top_k
    if args.repetition_penalty is not None:
        controls["repetition_penalty"] = args.repetition_penalty
    return controls


def _read_text_file(path: Path) -> str:
    if not path.is_file():
        raise ConfigurationError(f"text file not found: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigurationError("text file must be valid UTF-8") from exc


def _read_segments_file(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ConfigurationError(f"segments file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("segments file must be valid UTF-8 JSON") from exc
    if isinstance(payload, dict) and "segments" in payload:
        payload = payload["segments"]
    if not isinstance(payload, list):
        raise ConfigurationError("segments file must contain a JSON array or {segments: [...]}")
    return payload


def command_doctor(args: argparse.Namespace) -> int:
    from .doctor import build_report, ready_enough, render_text

    settings = _settings(create_dirs=False, require_token=False)
    report = build_report(settings)
    _emit(report, args, render_text(report))
    if args.require_ready and not ready_enough(report):
        return EXIT_UNAVAILABLE
    return EXIT_OK


def command_models(args: argparse.Namespace) -> int:
    from .modelprovision import fetch_model, verify_model

    settings = _settings(create_dirs=True, require_token=False)
    if args.models_command == "fetch":
        result = fetch_model(
            settings.model_dir,
            backend=args.backend,
            revision=args.revision,
            force=args.force,
        )
        _emit(
            result,
            args,
            f"Model verified at {result['model_dir']} ({result['revision']}).\n"
            "No model was downloaded by the service; this was an explicit operator fetch.",
        )
        return EXIT_OK
    if args.models_command == "verify":
        result = verify_model(
            Path(args.dir or settings.model_dir), backend=args.backend, deep=args.deep
        )
        _emit(
            result,
            args,
            f"Model verification: {'ok' if result['ok'] else 'failed'}\n"
            + "\n".join(f"- {p}" for p in result["problems"]),
        )
        return EXIT_OK if result["ok"] else EXIT_UNAVAILABLE
    if args.models_command == "smoke":
        if not args.reference.is_file():
            raise ConfigurationError(f"reference audio not found: {args.reference}")
        text = args.text_file and _read_text_file(args.text_file) or args.text
        if not text:
            raise ConfigurationError("provide --text-file or --text")
        from .smoke import run_smoke

        result = run_smoke(
            settings,
            reference=args.reference,
            output=args.output,
            text=text,
        )
        _emit(
            result,
            args,
            f"Real-model smoke passed. Output: {result['output']['path']}\n"
            f"Output duration: {result['output']['duration_ms']} ms; cold/warm: "
            f"{result['latency']['renders'][0]['latency_seconds']}s / "
            f"{result['latency']['renders'][1]['latency_seconds']}s\n"
            f"Target support is still not a full quality/speaker-similarity verification.",
        )
        return EXIT_OK
    raise ConfigurationError("unknown models command")


def command_enroll(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.enroll_command != "start":
        raise ConfigurationError("unknown enroll command")
    result = client.create_enrollment(args.name)
    _emit(
        result,
        args,
        f"Enrollment link (single use, expires {result['expires_at']}):\n{result['recorder_url']}\n"
        "Treat the URL fragment as a credential: do not paste it into logs or share it.",
    )
    return EXIT_OK


def command_voices(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.voices_command == "create":
        if not args.authorized:
            raise ConfigurationError(
                "pass --authorized only after confirming this is your voice or you have permission"
            )
        if not args.audio.is_file():
            raise ConfigurationError(f"audio file not found: {args.audio}")
        result = client.create_voice(
            name=args.name,
            audio=args.audio,
            authorized=True,
            start_ms=args.start_ms,
            end_ms=args.end_ms,
        )
        voice = result["voice"]
        human = (
            f"Created voice {voice['id']} ({voice['name']})\n"
            f"Reference: {voice['reference']['duration_ms']} ms, "
            f"{voice['reference']['sample_rate']} Hz\n"
            f"Audition job: {result['audition_job']['id']}\n"
            "This used the recording as conditioning; model weights were not trained."
        )
        _emit(result, args, human)
        return EXIT_OK
    if args.voices_command == "revise":
        if not args.authorized:
            raise ConfigurationError(
                "pass --authorized only after confirming this is your voice or you have permission"
            )
        if not args.audio.is_file():
            raise ConfigurationError(f"audio file not found: {args.audio}")
        result = client.revise_voice(
            args.voice_id,
            audio=args.audio,
            authorized=True,
            start_ms=args.start_ms,
            end_ms=args.end_ms,
        )
        _emit(result, args, f"Added reference version for {args.voice_id}; audition job {result['audition_job']['id']}")
        return EXIT_OK
    if args.voices_command == "list":
        result = client.list_voices()
        human = "\n".join(
            f"{v['id']}  {v['name']}  {v['reference']['duration_ms']} ms"
            + ("  [default]" if v.get("is_default") else "")
            for v in result["voices"]
        ) or "No voices yet."
        _emit(result, args, human)
        return EXIT_OK
    if args.voices_command == "show":
        result = client.show_voice(args.voice_id)
        _emit(result, args, json.dumps(result, indent=2, ensure_ascii=False))
        return EXIT_OK
    if args.voices_command == "rename":
        result = client.rename_voice(args.voice_id, args.name)
        _emit(result, args, f"Renamed {result['id']} to {result['name']}")
        return EXIT_OK
    if args.voices_command == "select":
        result = client.select_voice(args.voice_id)
        _emit(result, args, f"Default voice: {result['id']} ({result['name']})")
        return EXIT_OK
    if args.voices_command == "preview":
        result = client.preview_voice(args.voice_id)
        _emit(result, args, f"Preview job: {result['job']['id']}")
        return EXIT_OK
    if args.voices_command == "delete":
        if not args.yes:
            raise ConfigurationError("pass --yes to delete a saved voice and its generated artifacts")
        result = client.delete_voice(args.voice_id)
        _emit(result, args, f"Deleted {args.voice_id} and {result['files_removed']} private file(s)")
        return EXIT_OK
    raise ConfigurationError("unknown voices command")


def command_render(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.command == "speak":
        text = _read_text_file(args.text_file) if args.text_file else args.text
        if not text:
            raise ConfigurationError("provide --text-file (preferred) or --text")
        result = client.create_synthesis(
            voice_id=args.voice,
            text=text,
            segments=None,
            preset=args.preset,
            controls=_controls_from(args),
            seed=args.seed,
        )
    else:
        segments = _read_segments_file(args.segments_file)
        result = client.create_synthesis(
            voice_id=args.voice,
            text=None,
            segments=segments,
            preset=args.preset,
            controls=_controls_from(args),
            seed=args.seed,
        )
    job = result["job"]
    code = EXIT_OK
    if args.wait:
        job, failure = _wait_for_job(client, job["id"], timeout=args.timeout)
        if failure is not None:
            code = failure
            if failure == EXIT_UNAVAILABLE:
                print("clawvoice: timed out waiting for the job", file=sys.stderr)
    human = (
        f"Job {job['id']}: {job['status']}\n"
        f"Preset: {job['preset']}. No audio was published."
    )
    if job.get("output_artifact_id"):
        human += f"\nArtifact: {job['output_artifact_id']}"
    _emit({"job": job, "request": result.get("request", {})}, args, human)
    return code


def command_jobs(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.jobs_command == "status":
        job = client.get_job(args.job_id)
        _emit(job, args, json.dumps(job, indent=2, ensure_ascii=False))
        return EXIT_OK
    if args.jobs_command == "cancel":
        job = client.cancel_job(args.job_id)
        note = "cancellation pending" if job.get("cancel_pending") else job["status"]
        _emit(job, args, f"Job {job['id']}: {note}")
        return EXIT_OK
    raise ConfigurationError("unknown jobs command")


def command_artifacts(args: argparse.Namespace) -> int:
    client = _client(args)
    if args.artifacts_command == "fetch":
        result = client.fetch_artifact(args.artifact_id, args.output, force=args.force)
        _emit(
            result,
            args,
            f"Downloaded {result['output']} ({result['bytes']} bytes, sha256 {result['sha256']})",
        )
        return EXIT_OK
    if args.artifacts_command == "show":
        result = client.get_artifact(args.artifact_id)
        _emit(result, args, json.dumps(result, indent=2, ensure_ascii=False))
        return EXIT_OK
    raise ConfigurationError("unknown artifacts command")


def command_openapi(args: argparse.Namespace) -> int:
    import tempfile

    from .api import create_app
    from .config import Settings, find_web_dir
    from .util import new_secret

    with tempfile.TemporaryDirectory(prefix="clawvoice-openapi-") as temp:
        root = Path(temp)
        settings = Settings(
            data_dir=root,
            db_path=root / "db.sqlite3",
            model_dir=root / "models",
            token_file=root / "service-token",
            service_token=new_secret(32),
            backend="fake",
            device="cpu",
            allow_fake_backend=True,
            web_dir=find_web_dir(),
        )
        app = create_app(settings, start_worker=False)
        schema = app.openapi()
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        from .util import atomic_write

        atomic_write(args.output, text.encode(), mode=0o644)
        if not _json_mode(args):
            print(f"Wrote OpenAPI schema to {args.output}")
        else:
            _emit({"output": str(args.output)}, args, None)
    return EXIT_OK


def command_cleanup(args: argparse.Namespace) -> int:
    from .db import Database
    from .storage import StorageManager
    from .store import Store

    settings = _settings(create_dirs=False, require_token=False)
    if not settings.db_path.is_file():
        raise ConfigurationError(f"database not found: {settings.db_path}")
    db = Database(settings.db_path)
    db.initialize()
    store = Store(db)
    storage = StorageManager(settings, store)
    expired = storage.cleanup_expired_artifacts()
    temp = storage.cleanup_temp()
    empty = storage.cleanup_empty_dirs()
    result = {"expired_artifacts": expired, "temp_files": temp, "empty_directories": empty}
    _emit(
        result,
        args,
        f"Removed {expired} expired artifact(s), {temp} temp file(s), and {empty} empty directory/directories.",
    )
    return EXIT_OK


def command_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .api import create_app

    settings = _settings(create_token=True, create_dirs=True, require_token=True)
    if args.host:
        settings.bind_host = args.host
    if args.port:
        settings.bind_port = args.port
    settings.validate()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    app = create_app(settings, start_worker=not args.no_worker)
    uvicorn.run(
        app,
        host=settings.bind_host,
        port=settings.bind_port,
        log_level=settings.log_level,
        access_log=False,
        proxy_headers=False,
    )
    return EXIT_OK


def command_worker(args: argparse.Namespace) -> int:
    import signal

    from .audio import AudioProcessor
    from .db import Database
    from .store import Store
    from .worker import InferenceWorker

    settings = _settings(create_token=False, create_dirs=True, require_token=False)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    db = Database(settings.db_path)
    db.initialize()
    store = Store(db)
    audio = AudioProcessor(settings)
    worker = InferenceWorker(settings, store, audio)
    stopping = False

    def _stop(signum: int, frame: Any) -> None:
        nonlocal stopping
        if stopping:
            return
        stopping = True
        print("clawvoice: stopping after the current job", file=sys.stderr)
        worker.request_stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    try:
        worker.run_forever()
    finally:
        worker.stop(timeout=1.0)
    return EXIT_OK


def command_healthcheck(args: argparse.Namespace) -> int:
    config = ClientConfig.from_env(require_token=False)
    client = Client(config)
    try:
        result = client.live(timeout=3)
    except (ClientUnavailableError, ApiError):
        return EXIT_UNAVAILABLE
    return EXIT_OK if result.get("status") == "live" else EXIT_UNAVAILABLE


def command_worker_healthcheck(args: argparse.Namespace) -> int:
    from .db import Database
    from .store import Store
    from .util import now_ms

    settings = _settings(create_dirs=False, require_token=False)
    if not settings.db_path.is_file():
        return EXIT_UNAVAILABLE
    db = Database(settings.db_path)
    db.initialize()
    worker = Store(db).worker_lock_view()
    ok = bool(
        worker.get("owner")
        and (worker.get("lease_expires_at") or 0) >= now_ms()
        and worker.get("status") == "ready"
    )
    return EXIT_OK if ok else EXIT_UNAVAILABLE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="clawvoice",
        description="Local reference-conditioned voice service client",
    )
    parser.add_argument("--version", action="version", version=f"clawvoice {__version__}")
    parser.add_argument("--json", action="store_true", dest="json_global", help="machine-readable stdout")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument("--json", action="store_true", help="machine-readable stdout")
        return p

    doctor = common(sub.add_parser("doctor", help="probe host, dependencies, model and service"))
    doctor.add_argument("--require-ready", action="store_true")
    doctor.set_defaults(func=command_doctor)

    models = sub.add_parser("models", help="explicit model provisioning and smoke tests")
    models_sub = models.add_subparsers(dest="models_command", required=True)
    fetch = common(models_sub.add_parser("fetch", help="download and verify the pinned model"))
    fetch.add_argument("--backend", default="turbo", choices=["turbo"])
    fetch.add_argument("--revision", default=TURBO_REVISION)
    fetch.add_argument("--force", action="store_true")
    fetch.set_defaults(func=command_models)
    verify = common(models_sub.add_parser("verify", help="verify provisioned model files"))
    verify.add_argument("--backend", default="turbo", choices=["turbo"])
    verify.add_argument("--dir", type=Path)
    verify.add_argument("--deep", action="store_true", help="recompute all SHA-256 checksums")
    verify.set_defaults(func=command_models)
    smoke = common(models_sub.add_parser("smoke", help="run a real-model synthesis on this host"))
    smoke.add_argument("--reference", type=Path, required=True)
    smoke.add_argument("--output", type=Path, required=True)
    smoke.add_argument("--text-file", type=Path)
    smoke.add_argument("--text")
    smoke.set_defaults(func=command_models)

    enroll = sub.add_parser("enroll", help="create a single-use browser enrollment session")
    enroll_sub = enroll.add_subparsers(dest="enroll_command", required=True)
    enroll_start = common(enroll_sub.add_parser("start"))
    enroll_start.add_argument("--name", required=True)
    enroll_start.set_defaults(func=command_enroll)

    voices = sub.add_parser("voices", help="manage named voices")
    voices_sub = voices.add_subparsers(dest="voices_command", required=True)
    create = common(voices_sub.add_parser("create"))
    create.add_argument("--name", required=True)
    create.add_argument("--audio", type=Path, required=True)
    create.add_argument("--authorized", action="store_true")
    create.add_argument("--start-ms", type=int)
    create.add_argument("--end-ms", type=int)
    create.set_defaults(func=command_voices)
    for name in ("list",):
        p = common(voices_sub.add_parser(name))
        p.set_defaults(func=command_voices)
    revise = common(voices_sub.add_parser("revise"))
    revise.add_argument("voice_id")
    revise.add_argument("--audio", type=Path, required=True)
    revise.add_argument("--authorized", action="store_true")
    revise.add_argument("--start-ms", type=int)
    revise.add_argument("--end-ms", type=int)
    revise.set_defaults(func=command_voices)
    show = common(voices_sub.add_parser("show"))
    show.add_argument("voice_id")
    show.set_defaults(func=command_voices)
    rename = common(voices_sub.add_parser("rename"))
    rename.add_argument("voice_id")
    rename.add_argument("--name", required=True)
    rename.set_defaults(func=command_voices)
    select = common(voices_sub.add_parser("select"))
    select.add_argument("voice_id")
    select.set_defaults(func=command_voices)
    preview = common(voices_sub.add_parser("preview"))
    preview.add_argument("voice_id")
    preview.set_defaults(func=command_voices)
    delete = common(voices_sub.add_parser("delete"))
    delete.add_argument("voice_id")
    delete.add_argument("--yes", action="store_true")
    delete.set_defaults(func=command_voices)

    def add_render_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--voice", help="voice ID; omit to use the selected default")
        p.add_argument("--preset", choices=["master", "mp3", "telephony", "telephony-mulaw"], default="master")
        p.add_argument("--temperature", type=float)
        p.add_argument("--top-p", type=float)
        p.add_argument("--top-k", type=int)
        p.add_argument("--repetition-penalty", type=float)
        p.add_argument("--seed", type=int)
        p.add_argument("--wait", action="store_true", help="poll the job with a bounded timeout")
        p.add_argument("--timeout", type=float, default=180.0)

    speak = common(sub.add_parser("speak", help="generate speech from literal text"))
    add_render_args(speak)
    speak.add_argument("--text-file", type=Path)
    speak.add_argument("--text")
    speak.set_defaults(func=command_render)
    render = common(sub.add_parser("render", help="generate speech with structured text/pause segments"))
    add_render_args(render)
    render.add_argument("--segments-file", type=Path, required=True)
    render.set_defaults(func=command_render)

    jobs = sub.add_parser("jobs", help="inspect or cancel jobs")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    status = common(jobs_sub.add_parser("status"))
    status.add_argument("job_id")
    status.set_defaults(func=command_jobs)
    cancel = common(jobs_sub.add_parser("cancel"))
    cancel.add_argument("job_id")
    cancel.set_defaults(func=command_jobs)

    artifacts = sub.add_parser("artifacts", help="fetch generated audio artifacts")
    artifacts_sub = artifacts.add_subparsers(dest="artifacts_command", required=True)
    fetch_art = common(artifacts_sub.add_parser("fetch"))
    fetch_art.add_argument("artifact_id")
    fetch_art.add_argument("--output", type=Path, required=True)
    fetch_art.add_argument("--force", action="store_true")
    fetch_art.set_defaults(func=command_artifacts)
    show_art = common(artifacts_sub.add_parser("show"))
    show_art.add_argument("artifact_id")
    show_art.set_defaults(func=command_artifacts)

    openapi = common(sub.add_parser("openapi", help="write the versioned OpenAPI schema"))
    openapi.add_argument("--output", type=Path, default=Path("docs/api/openapi.json"))
    openapi.set_defaults(func=command_openapi)

    cleanup = common(sub.add_parser("cleanup", help="remove expired generated artifacts"))
    cleanup.set_defaults(func=command_cleanup)

    serve = sub.add_parser("serve", help="run the local API and recorder UI")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--no-worker", action="store_true", help="API only; run clawvoice worker separately")
    serve.set_defaults(func=command_serve)

    worker = sub.add_parser("worker", help="run the dedicated inference worker")
    worker.set_defaults(func=command_worker)

    health = sub.add_parser("healthcheck", help="API liveness check for container health")
    health.set_defaults(func=command_healthcheck)
    worker_health = sub.add_parser("worker-healthcheck", help="worker lease check for container health")
    worker_health.set_defaults(func=command_worker_healthcheck)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ClientConfigurationError as exc:
        return _emit_error(exc, args, EXIT_CONFIG)
    except ApiError as exc:
        return _emit_error(exc, args, _api_exit_code(exc))
    except ClientUnavailableError as exc:
        return _emit_error(exc, args, EXIT_UNAVAILABLE)
    except ClawvoiceError as exc:
        return _emit_error(exc, args, exc.exit_code)
    except KeyboardInterrupt:
        print("clawvoice: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        return _emit_error(exc, args, 1)


if __name__ == "__main__":
    raise SystemExit(main())
