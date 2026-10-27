"""Application factory."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from argus import __version__
from argus.api.v1 import auth, councils, health, providers
from argus.cache.store import build_cache
from argus.core.config import Settings, get_settings
from argus.core.errors import install_error_handlers
from argus.core.logging import configure_logging, get_logger
from argus.core.security import hash_password
from argus.db.models import User
from argus.db.session import Database
from argus.services.llm import LLMRouter

log = get_logger(__name__)

_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
}
_DOCS_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com"
)


async def _bootstrap_admin(app: FastAPI, settings: Settings) -> None:
    if not (settings.bootstrap_admin_email and settings.bootstrap_admin_password):
        return
    async with app.state.db.sessionmaker() as session:
        email = settings.bootstrap_admin_email.lower()
        if (await session.execute(select(User.id).where(User.email == email))).first():
            return
        session.add(
            User(
                email=email,
                display_name="Administrator",
                password_hash=hash_password(settings.bootstrap_admin_password.get_secret_value()),
                is_admin=True,
            )
        )
        await session.commit()
        log.info("bootstrap.admin_created", email=email)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.db = Database(settings)
        app.state.cache = await build_cache(settings.redis_url, settings.cache_default_ttl)
        if not hasattr(app.state, "llm"):
            app.state.llm = LLMRouter(settings, app.state.cache)
        if not settings.is_production:
            # Production schemas are managed by Alembic migrations.
            await app.state.db.create_all()
        await _bootstrap_admin(app, settings)
        log.info("argus.started", env=settings.env, version=__version__)
        try:
            yield
        finally:
            await app.state.llm.aclose()
            await app.state.cache.close()
            await app.state.db.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
    )

    @app.middleware("http")
    async def _edge(request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get("X-Request-ID", "")
        if not (0 < len(rid) <= 64 and rid.replace("-", "").isalnum()):
            rid = uuid.uuid4().hex
        structlog.contextvars.bind_contextvars(request_id=rid)
        try:
            length = request.headers.get("content-length")
            if length is not None and (not length.isdigit() or int(length) > settings.max_request_bytes):
                return JSONResponse(
                    status_code=413,
                    content={"error": {"code": "payload_too_large", "message": "Request body too large"}},
                    headers={"X-Request-ID": rid},
                )
            response = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers["X-Request-ID"] = rid
        for k, v in _SECURITY_HEADERS.items():
            response.headers.setdefault(k, v)
        if request.url.path in ("/docs", "/docs/oauth2-redirect"):
            response.headers["Content-Security-Policy"] = _DOCS_CSP
        if settings.is_production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        return response

    install_error_handlers(app)
    app.include_router(health.router)
    api = "/api/v1"
    app.include_router(auth.router, prefix=api)
    app.include_router(councils.router, prefix=api)
    app.include_router(providers.router, prefix=api)
    return app
