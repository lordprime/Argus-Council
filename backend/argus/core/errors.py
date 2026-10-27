"""Domain errors and their HTTP mapping."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ArgusError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFound(ArgusError):
    status_code = 404
    code = "not_found"


class Conflict(ArgusError):
    status_code = 409
    code = "conflict"


class Unauthorized(ArgusError):
    status_code = 401
    code = "unauthorized"


class Forbidden(ArgusError):
    status_code = 403
    code = "forbidden"


class RateLimited(ArgusError):
    status_code = 429
    code = "rate_limited"


class GuardrailViolation(ArgusError):
    status_code = 422
    code = "guardrail_violation"


class ServiceUnavailable(ArgusError):
    """A dependency this request cannot safely do without is down (retry later)."""

    status_code = 503
    code = "service_unavailable"


# Stable codes for framework-raised HTTP errors (routing, method mismatch, ...).
_HTTP_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
}


def request_id_of(request: Request | None) -> str | None:
    """The id the edge middleware assigned to this request (also bound in the log context)."""
    rid = getattr(request.state, "request_id", None) if request is not None else None
    return rid or structlog.contextvars.get_contextvars().get("request_id")


def error_body(
    code: str, message: str, *, request_id: str | None, details: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The one error envelope every API error uses: ``{"error": {code, message, [details], request_id}}``."""
    err: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        err["details"] = details
    err["request_id"] = request_id
    return {"error": err}


def error_response(
    request: Request | None,
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    rid = request_id_of(request)
    merged = {**(headers or {}), **({"X-Request-ID": rid} if rid else {})}
    return JSONResponse(
        status_code=status_code,
        content=error_body(code, message, request_id=rid, details=details),
        headers=merged,
    )


def _validation_message(errors: list[dict[str, Any]]) -> str:
    parts = []
    for e in errors[:5]:
        loc = ".".join(str(x) for x in e.get("loc", ())[1:]) or "request"
        parts.append(f"{loc}: {e.get('msg', 'invalid')}")
    return "; ".join(parts) or "Invalid request"


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ArgusError)
    async def _argus_error(request: Request, exc: ArgusError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if isinstance(exc, Unauthorized) else None
        if isinstance(exc, ServiceUnavailable):
            headers = {"Retry-After": "5"}
        return error_response(
            request,
            exc.status_code,
            exc.code,
            exc.message,
            details=exc.details,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Submitted values ("input") and pydantic context are dropped: bodies can carry passwords and keys.
        errors = [
            {"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
            for e in exc.errors()
        ][:20]
        return error_response(
            request, 422, "validation_error", _validation_message(errors), details={"errors": errors}
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if isinstance(exc.detail, str):
            message, details = exc.detail, None
        else:
            message, details = HTTPStatus(exc.status_code).phrase, {"detail": exc.detail}
        return error_response(
            request,
            exc.status_code,
            _HTTP_CODES.get(exc.status_code, "http_error"),
            message,
            details=details,
            headers=dict(exc.headers or {}),
        )
