"""Domain errors mapped to stable HTTP responses by the API layer."""

from __future__ import annotations


class DounaiError(Exception):
    """Base class for expected, user-visible failures."""

    code = "internal_error"
    status_code = 500

    def __init__(self, message: str, *, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class InputValidationError(DounaiError):
    code = "invalid_input"
    status_code = 422


class UnsupportedMediaError(DounaiError):
    code = "unsupported_media"
    status_code = 415


class PayloadTooLargeError(DounaiError):
    code = "payload_too_large"
    status_code = 413


class NotFoundError(DounaiError):
    code = "not_found"
    status_code = 404


class ConflictError(DounaiError):
    code = "conflict"
    status_code = 409


class ConsentRequiredError(DounaiError):
    code = "consent_required"
    status_code = 403


class PrivacyDisabledError(DounaiError):
    code = "privacy_disabled"
    status_code = 403


class ProviderError(DounaiError):
    """A model provider failed.

    The message is safe to log.  Provider response bodies are never included in
    API responses because they can contain user audio transcripts or request ids.
    """

    code = "provider_unavailable"
    status_code = 502

    def __init__(
        self,
        message: str,
        *,
        provider_id: str,
        retryable: bool = True,
        detail: str | None = None,
    ) -> None:
        super().__init__(message, detail=detail)
        self.provider_id = provider_id
        self.retryable = retryable


class AuthenticationError(DounaiError):
    code = "unauthorized"
    status_code = 401
