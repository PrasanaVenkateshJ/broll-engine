"""One error format for the whole API.

Every error response looks like:
    {"error": {"code": "UPPER_SNAKE_CASE", "message": "...", "retryable": false, "details": {}}}
"""
import logging
from enum import Enum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_setup import correlation_id_var

log = logging.getLogger("app.errors")


class ErrorCode(str, Enum):
    INVALID_INPUT = "INVALID_INPUT"
    EMPTY_SCRIPT = "EMPTY_SCRIPT"
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"
    FILE_TOO_LONG = "FILE_TOO_LONG"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    CORRUPTED_MEDIA = "CORRUPTED_MEDIA"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    NO_SPEECH_DETECTED = "NO_SPEECH_DETECTED"
    MUSIC_ONLY_AUDIO = "MUSIC_ONLY_AUDIO"
    UNSUPPORTED_ASPECT_RATIO = "UNSUPPORTED_ASPECT_RATIO"
    JOB_NOT_FOUND = "JOB_NOT_FOUND"
    SCENE_NOT_FOUND = "SCENE_NOT_FOUND"
    JOB_NOT_READY = "JOB_NOT_READY"              # clip swap: the original job has not finished (or failed)
    NO_ALTERNATIVE_CLIP = "NO_ALTERNATIVE_CLIP"  # clip swap: no other usable clip exists for the scene
    SWAP_IN_PROGRESS = "SWAP_IN_PROGRESS"        # clip swap: another swap of the same video is queued / running
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    UNAUTHORIZED = "UNAUTHORIZED"
    RATE_LIMITED = "RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    RENDER_FAILED = "RENDER_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    # Generic HTTP cases that are not about jobs:
    NOT_FOUND = "NOT_FOUND"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    NOT_READY = "NOT_READY"


class AppError(Exception):
    """Raise this anywhere in the app; the handler turns it into the JSON error format."""

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        status_code: int = 400,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.details = details or {}
        self.headers = headers or {}

    def to_dict(self) -> dict[str, Any]:
        return error_body(self.code, self.message, self.retryable, self.details)


def error_body(code: ErrorCode | str, message: str, retryable: bool = False, details: dict | None = None) -> dict:
    return {
        "error": {
            "code": code.value if isinstance(code, ErrorCode) else code,
            "message": message,
            "retryable": retryable,
            "details": details or {},
        }
    }


def error_response(status_code: int, body: dict, headers: dict[str, str] | None = None) -> JSONResponse:
    headers = dict(headers or {})
    # Also add the correlation id here: for 500s the middleware never sees the response.
    cid = correlation_id_var.get()
    if cid:
        headers["X-Correlation-Id"] = cid
    return JSONResponse(status_code=status_code, content=body, headers=headers)


# Map plain HTTP errors (e.g. unknown URL) to our codes.
_HTTP_CODES = {
    401: ErrorCode.UNAUTHORIZED,
    404: ErrorCode.NOT_FOUND,
    405: ErrorCode.METHOD_NOT_ALLOWED,
    413: ErrorCode.FILE_TOO_LARGE,
    429: ErrorCode.RATE_LIMITED,
}


async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    log.info("request failed: %s %s", exc.code.value, exc.message)
    return error_response(exc.status_code, exc.to_dict(), exc.headers)


async def _http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _HTTP_CODES.get(exc.status_code, ErrorCode.INVALID_INPUT if exc.status_code < 500 else ErrorCode.INTERNAL_ERROR)
    message = exc.detail if isinstance(exc.detail, str) else "HTTP error"
    return error_response(exc.status_code, error_body(code, message, exc.status_code >= 500), getattr(exc, "headers", None))


def validation_details(errors: list[dict]) -> dict:
    """Keep only the safe, useful parts of Pydantic errors (where + what)."""
    return {
        "fields": [
            {"loc": [str(p) for p in e.get("loc", ())], "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in errors
        ]
    }


async def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    body = error_body(ErrorCode.INVALID_INPUT, "Request validation failed", False, validation_details(exc.errors()))
    return error_response(422, body)


async def _unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Log the full stack trace, but never send internals to the client.
    log.exception("unhandled error")
    return error_response(500, error_body(ErrorCode.INTERNAL_ERROR, "Internal server error", True))


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _unhandled_error_handler)
