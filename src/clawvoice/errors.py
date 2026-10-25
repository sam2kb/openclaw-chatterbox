"""Typed errors with stable machine codes and safe, user-facing messages."""

from __future__ import annotations

from typing import Any


class ClawvoiceError(Exception):
    """Base error. Messages must be safe to return to an authenticated caller."""

    code = "internal_error"
    http_status = 500
    exit_code = 1

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}
        self.retryable = retryable

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "details": self.details,
            }
        }


class ConfigurationError(ClawvoiceError):
    code = "configuration_error"
    http_status = 500
    exit_code = 3


class AuthenticationError(ClawvoiceError):
    code = "authentication_failed"
    http_status = 401
    exit_code = 3


class AuthorizationError(ClawvoiceError):
    code = "not_authorized"
    http_status = 403
    exit_code = 3


class NotFoundError(ClawvoiceError):
    code = "not_found"
    http_status = 404
    exit_code = 2


class ValidationFailure(ClawvoiceError):
    code = "validation_error"
    http_status = 422
    exit_code = 2


class ConflictError(ClawvoiceError):
    code = "conflict"
    http_status = 409
    exit_code = 2


class PayloadTooLarge(ClawvoiceError):
    code = "payload_too_large"
    http_status = 413
    exit_code = 2


class UnsupportedMedia(ClawvoiceError):
    code = "unsupported_media"
    http_status = 415
    exit_code = 2


class RateLimited(ClawvoiceError):
    code = "queue_full"
    http_status = 429
    exit_code = 4


class UnavailableError(ClawvoiceError):
    code = "unavailable"
    http_status = 503
    exit_code = 4


class ModelUnavailable(UnavailableError):
    code = "model_unavailable"


class JobFailed(ClawvoiceError):
    code = "job_failed"
    http_status = 200
    exit_code = 5


class JobCanceled(ClawvoiceError):
    code = "job_canceled"
    http_status = 200
    exit_code = 6


class CancellationRequested(Exception):
    """Internal control flow for a cooperative worker cancellation."""


class InferenceError(Exception):
    """Raised by a backend. The worker converts it to a safe job error."""


class BackendOOM(InferenceError):
    """A GPU allocation failed. Never fall back to CPU."""


class BackendUnavailable(InferenceError):
    """A backend could not run this request."""
