"""Lightweight stdlib HTTP client. It must work without Torch or ML packages."""

from __future__ import annotations

import hashlib
import json
import os
import ssl
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ClientConfigurationError(Exception):
    pass


class ClientUnavailableError(Exception):
    pass


class ApiError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int,
        code: str = "api_error",
        details: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.details = details or {}
        self.payload = payload


def default_token_path() -> Path:
    env = os.environ.get("CLAWVOICE_TOKEN_FILE")
    if env:
        return Path(env).expanduser()
    data_dir = os.environ.get("CLAWVOICE_DATA_DIR")
    if data_dir:
        return Path(data_dir).expanduser() / "secrets" / "service-token"
    config = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config).expanduser() if config else Path.home() / ".config"
    return root / "clawvoice" / "service-token"


def load_service_token(path: Path | None = None, *, required: bool = True) -> str:
    env_token = os.environ.get("CLAWVOICE_SERVICE_TOKEN")
    if env_token:
        if len(env_token) < 24:
            raise ClientConfigurationError("CLAWVOICE_SERVICE_TOKEN is too short")
        return env_token
    token_path = path or default_token_path()
    if not token_path.is_file():
        if required:
            raise ClientConfigurationError(
                f"service token file is missing: {token_path}; set CLAWVOICE_TOKEN_FILE"
            )
        return ""
    try:
        mode = token_path.stat().st_mode
    except OSError as exc:
        raise ClientConfigurationError(f"cannot stat service token file: {token_path}") from exc
    if os.name == "posix" and stat.S_IMODE(mode) & 0o077:
        raise ClientConfigurationError(
            "service token file must not be readable or writable by group/other (chmod 600)"
        )
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ClientConfigurationError(f"cannot read service token file: {token_path}") from exc
    if len(token) < 24:
        raise ClientConfigurationError(f"service token file is invalid: {token_path}")
    return token


@dataclass
class ClientConfig:
    base_url: str
    token: str
    token_path: Path
    timeout: float = 30.0
    ca_file: str | None = None

    @classmethod
    def from_env(cls, *, require_token: bool = True) -> ClientConfig:
        base_url = os.environ.get("CLAWVOICE_URL", "http://127.0.0.1:8765").rstrip("/")
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ClientConfigurationError("CLAWVOICE_URL must be an absolute http(s) URL")
        if parsed.username or parsed.password:
            raise ClientConfigurationError("CLAWVOICE_URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ClientConfigurationError("CLAWVOICE_URL must not contain a query or fragment")
        token_path = default_token_path()
        token = load_service_token(token_path, required=require_token)
        try:
            timeout = float(os.environ.get("CLAWVOICE_CLIENT_TIMEOUT", "30"))
        except ValueError as exc:
            raise ClientConfigurationError("CLAWVOICE_CLIENT_TIMEOUT must be a number") from exc
        if not 1 <= timeout <= 600:
            raise ClientConfigurationError("CLAWVOICE_CLIENT_TIMEOUT is outside the allowed range")
        ca_file = os.environ.get("CLAWVOICE_CA_FILE") or None
        return cls(
            base_url=base_url,
            token=token,
            token_path=token_path,
            timeout=timeout,
            ca_file=ca_file,
        )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise urllib.error.HTTPError(req.full_url, code, "redirects are not followed", headers, fp)


class Client:
    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self._opener = self._build_opener()

    def _build_opener(self) -> urllib.request.OpenerDirector:
        handlers: list[Any] = [_NoRedirect()]
        if self.config.base_url.startswith("https://"):
            if self.config.ca_file:
                context = ssl.create_default_context(cafile=self.config.ca_file)
            else:
                context = ssl.create_default_context()
            handlers.append(urllib.request.HTTPSHandler(context=context))
        return urllib.request.build_opener(*handlers)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        auth: str | None = None,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        url = f"{self.config.base_url}{path}"
        request_headers = {"Accept": "application/json", "User-Agent": "clawvoice-client/0.1"}
        token = self.config.token if auth is None else auth
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        if headers:
            request_headers.update(headers)
        if json_body is not None:
            data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with self._opener.open(request, timeout=timeout or self.config.timeout) as response:
                body = response.read()
                return response.status, dict(response.headers.items()), body
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            code = "http_error"
            message = f"request failed with HTTP {exc.code}"
            details: dict[str, Any] = {}
            payload: dict[str, Any] | None = None
            try:
                parsed = json.loads(raw.decode("utf-8"))
                if isinstance(parsed, dict):
                    payload = parsed
                error = payload.get("error", {}) if payload else {}
                code = str(error.get("code") or code)
                message = str(error.get("message") or message)
                details = error.get("details") if isinstance(error.get("details"), dict) else {}
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                payload = None
            raise ApiError(
                message, status=exc.code, code=code, details=details, payload=payload
            ) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            raise ClientUnavailableError(
                f"cannot reach clawvoice at {self.config.base_url}: {reason or exc}"
            ) from exc
        except TimeoutError as exc:
            raise ClientUnavailableError("the clawvoice request timed out") from exc

    def _json(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        auth: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        _, _, raw = self._request(method, path, json_body=body, auth=auth, timeout=timeout)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("server returned invalid JSON", status=502) from exc

    def live(self, *, timeout: float | None = None) -> dict[str, Any]:
        return self._json("GET", "/api/v1/health/live", auth="", timeout=timeout)

    def ready(self) -> dict[str, Any]:
        return self._json("GET", "/api/v1/health/ready")

    def capabilities(self) -> dict[str, Any]:
        return self._json("GET", "/api/v1/capabilities")

    def create_enrollment(self, name: str) -> dict[str, Any]:
        return self._json("POST", "/api/v1/enrollment-sessions", body={"name": name})

    def get_enrollment(self, session_id: str, token: str) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/enrollment-sessions/{session_id}", auth=token)

    def create_voice(
        self,
        *,
        name: str,
        audio: Path,
        authorized: bool,
        start_ms: int | None,
        end_ms: int | None,
    ) -> dict[str, Any]:
        query = {"name": name, "authorized": "true" if authorized else "false"}
        if start_ms is not None:
            query["start_ms"] = str(start_ms)
        if end_ms is not None:
            query["end_ms"] = str(end_ms)
        path = "/api/v1/voices?" + urllib.parse.urlencode(query)
        data = audio.read_bytes()
        _, _, raw = self._request(
            "POST",
            path,
            data=data,
            headers={"Content-Type": "application/octet-stream"},
            timeout=max(self.config.timeout, 120.0),
        )
        return json.loads(raw.decode("utf-8"))

    def revise_voice(
        self,
        voice_id: str,
        *,
        audio: Path,
        authorized: bool,
        start_ms: int | None,
        end_ms: int | None,
    ) -> dict[str, Any]:
        query = {"authorized": "true" if authorized else "false"}
        if start_ms is not None:
            query["start_ms"] = str(start_ms)
        if end_ms is not None:
            query["end_ms"] = str(end_ms)
        path = f"/api/v1/voices/{voice_id}/reference?" + urllib.parse.urlencode(query)
        _, _, raw = self._request(
            "POST",
            path,
            data=audio.read_bytes(),
            headers={"Content-Type": "application/octet-stream"},
            timeout=max(self.config.timeout, 120.0),
        )
        return json.loads(raw.decode("utf-8"))

    def list_voices(self) -> dict[str, Any]:
        return self._json("GET", "/api/v1/voices")

    def show_voice(self, voice_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/voices/{voice_id}")

    def rename_voice(self, voice_id: str, name: str) -> dict[str, Any]:
        return self._json("PATCH", f"/api/v1/voices/{voice_id}", body={"name": name})

    def select_voice(self, voice_id: str) -> dict[str, Any]:
        return self._json("POST", f"/api/v1/voices/{voice_id}/select")

    def preview_voice(self, voice_id: str) -> dict[str, Any]:
        return self._json("POST", f"/api/v1/voices/{voice_id}/preview")

    def delete_voice(self, voice_id: str) -> dict[str, Any]:
        return self._json("DELETE", f"/api/v1/voices/{voice_id}")

    def create_synthesis(
        self,
        *,
        voice_id: str | None,
        text: str | None,
        segments: list[dict[str, Any]] | None,
        preset: str,
        controls: dict[str, Any],
        seed: int | None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "voice_id": voice_id,
            "preset": preset,
            "controls": controls,
            "seed": seed,
        }
        if text is not None:
            body["text"] = text
        else:
            body["segments"] = segments
        return self._json("POST", "/api/v1/synthesis-jobs", body=body)

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/jobs/{job_id}")

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._json("POST", f"/api/v1/jobs/{job_id}/cancel")

    def get_artifact(self, artifact_id: str) -> dict[str, Any]:
        return self._json("GET", f"/api/v1/artifacts/{artifact_id}")

    def fetch_artifact(self, artifact_id: str, output: Path, *, force: bool = False) -> dict[str, Any]:
        if output.exists() and not force:
            raise ClientConfigurationError(
                f"output already exists: {output}; pass --force to replace it"
            )
        _, headers, raw = self._request(
            "GET", f"/api/v1/artifacts/{artifact_id}/download", timeout=max(120.0, self.config.timeout)
        )
        sha = hashlib.sha256(raw).hexdigest()
        expected = headers.get("X-Artifact-SHA256") or headers.get("x-artifact-sha256")
        if expected and not secrets_compare(sha, expected):
            raise ApiError("downloaded artifact checksum did not match the server manifest", status=502)
        output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, output)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return {
            "artifact_id": artifact_id,
            "output": str(output.resolve()),
            "bytes": len(raw),
            "sha256": sha,
            "server_sha256": expected,
            "media_type": headers.get("Content-Type") or headers.get("content-type"),
        }


def secrets_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left, right)
