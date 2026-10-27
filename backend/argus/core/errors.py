"""Domain errors and their HTTP mapping."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


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


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ArgusError)
    async def _argus_error(_: Request, exc: ArgusError) -> JSONResponse:
        headers = {"WWW-Authenticate": "Bearer"} if isinstance(exc, Unauthorized) else None
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
            headers=headers,
        )
