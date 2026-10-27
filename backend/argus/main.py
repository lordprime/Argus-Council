"""Application factory."""

from __future__ import annotations

import asyncio
import functools
import hmac
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select

from argus import __version__
from argus.api.sse import SseHub, chain_shutdown_signals
from argus.api.v1 import (
    admin,
    auth,
    connections,
    councils,
    data,
    health,
    knowledge,
    mfa,
    notifications,
    observability,
    providers,
    runs,
    skills,
    templates,
    webhooks,
)
from argus.cache.store import RedisBackend, build_cache, redis_client
from argus.core.config import Settings, get_settings
from argus.core.errors import error_response, install_error_handlers
from argus.core.logging import configure_logging, get_logger
from argus.core.metrics import (
    APPROVALS_PENDING,
    HTTP_LATENCY,
    HTTP_REQUESTS,
    REGISTRY,
    SSE_STREAMS,
    UNMATCHED_ROUTE,
    method_label,
    route_template,
    tracked_stream,
)
from argus.core.security import hash_password
from argus.db.models import User
from argus.db.session import Database
from argus.protocols import a2a, acp, mcp_server
from argus.runtime.events import build_broker
from argus.runtime.manager import RunManager
from argus.services.audit import audit
from argus.services.llm import LLMRouter
from argus.services.retention import RetentionService
from argus.services.runs import BlockingWaits
from argus.services.webhooks import WebhookDispatcher

log = get_logger(__name__)

# High-volume or credential-exchange endpoints that need no audit row.
_NOT_AUDITED = {"/llm/complete", "/auth/refresh", "/{run_id}/stream-ticket", "/runs/{run_id}/stream-ticket"}

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


def _pending_approvals(manager: RunManager) -> float:
    """Approval requests still waiting for a decision across this process's live runs (scrape time)."""
    return float(
        sum(
            1
            for live in list(manager.live.values())
            for approval in list(live.ctx.approvals.values())
            if not approval.future.done()
        )
    )


def _spawn(app: FastAPI, coro) -> None:  # type: ignore[no-untyped-def]
    tasks: set[asyncio.Task[None]] = app.state.background
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.background = set()
        app.state.db = Database(settings)
        app.state.cache = await build_cache(
            settings.redis_url,
            settings.cache_default_ttl,
            socket_timeout=settings.redis_socket_timeout_s,
            connect_timeout=settings.redis_connect_timeout_s,
        )
        if not hasattr(app.state, "llm"):
            app.state.llm = LLMRouter(settings, app.state.cache, sessionmaker=app.state.db.sessionmaker)
        if not settings.is_production:
            # Production schemas are managed by Alembic migrations.
            await app.state.db.create_all()
        await _bootstrap_admin(app, settings)
        # Pub/sub gets its own client: subscribers block on reads for minutes, which the cache
        # client's read timeout would cut off.
        app.state.broker_client = (
            redis_client(settings.redis_url, connect_timeout=settings.redis_connect_timeout_s)
            if isinstance(app.state.cache.backend, RedisBackend) and settings.redis_url
            else None
        )
        app.state.broker = build_broker(app.state.broker_client)
        app.state.blocking_waits = BlockingWaits(settings.protocol_max_blocking_waits)
        # Open SSE streams end as soon as SIGTERM/SIGINT arrives, so uvicorn's graceful shutdown
        # is not held up by them and the run finalisation below gets to run.
        app.state.sse = SseHub()
        restore_signals = chain_shutdown_signals(app.state.sse.shutdown)
        app.state.webhooks = WebhookDispatcher(settings, app.state.db.sessionmaker)
        app.state.webhooks.start()  # resumes deliveries left in the outbox by a previous process
        app.state.runs = RunManager(
            settings, app.state.db.sessionmaker, app.state.broker, app.state.llm, webhooks=app.state.webhooks
        )
        await app.state.runs.recover()
        APPROVALS_PENDING.set_function(functools.partial(_pending_approvals, app.state.runs))
        app.state.retention = RetentionService(app.state.db.sessionmaker, settings)
        # Long-lived loop: kept out of app.state.background (which shutdown awaits) and cancelled first.
        app.state.periodic = set()
        if settings.retention_enabled:
            app.state.periodic.add(asyncio.create_task(app.state.retention.loop(), name="retention"))
        log.info("argus.started", env=settings.env, version=__version__)
        try:
            yield
        finally:
            app.state.sse.shutdown()
            restore_signals()
            for task in app.state.periodic:
                task.cancel()
            await asyncio.gather(*app.state.periodic, return_exceptions=True)
            await app.state.runs.shutdown()
            # Bounded: unfinished deliveries stay in the outbox for the next process.
            await app.state.webhooks.shutdown(settings.webhook_shutdown_grace_s)
            if app.state.background:
                await asyncio.gather(*app.state.background, return_exceptions=True)
            await app.state.broker.close()
            if app.state.broker_client is not None:
                await app.state.broker_client.aclose()
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
        allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Request-ID", "Idempotency-Key"],
        expose_headers=["X-Request-ID", "Idempotent-Replayed"],
    )

    @app.middleware("http")
    async def _edge(request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get("X-Request-ID", "")
        if not (0 < len(rid) <= 64 and rid.replace("-", "").isalnum()):
            rid = uuid.uuid4().hex
        request.state.request_id = rid
        structlog.contextvars.bind_contextvars(request_id=rid)
        try:
            length = request.headers.get("content-length")
            is_upload = request.headers.get("content-type", "").startswith("multipart/form-data")
            limit = settings.max_upload_bytes if is_upload else settings.max_request_bytes
            if length is not None and (not length.isdigit() or int(length) > limit):
                return error_response(request, 413, "payload_too_large", "Request body too large")
            started = time.perf_counter()
            try:
                response = await call_next(request)
            except Exception:
                # Nothing mapped it to a response: answer with the envelope, then count and audit it
                # like any other response. The traceback is logged under the request id.
                log.exception(
                    "http.unhandled_exception",
                    method=request.method,
                    path=request.url.path,
                    route=route_template(request),
                )
                response = error_response(request, 500, "internal_error", "Internal error")
            template = route_template(request)
            method = method_label(request.method)
            HTTP_REQUESTS.labels(method, template, str(response.status_code)).inc()
            if (
                request.method in ("POST", "PUT", "PATCH", "DELETE")
                and request.url.path.startswith("/api/v1/")
                and template not in _NOT_AUDITED
                and template != UNMATCHED_ROUTE
            ):
                params = dict(request.path_params)
                # Fire-and-forget: the write must neither wait on nor block the request's own transaction.
                _spawn(
                    app,
                    audit(
                        request,
                        f"{request.method} {template}",
                        actor_id=getattr(request.state, "actor_id", None),
                        target_type=template.strip("/").split("/")[0],
                        target_id=next(iter(params.values()), ""),
                        success=response.status_code < 400,
                        detail={"status": response.status_code, **getattr(request.state, "audit_detail", {})},
                    ),
                )
            if not template.endswith("/events/stream"):
                HTTP_LATENCY.labels(method, template).observe(time.perf_counter() - started)
            elif response.status_code == 200 and hasattr(response, "body_iterator"):
                response.body_iterator = tracked_stream(response.body_iterator, SSE_STREAMS)
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

    @app.get("/metrics", include_in_schema=False)
    async def metrics(request: Request) -> Response:
        token = settings.metrics_token
        if token is not None:
            expected = f"Bearer {token.get_secret_value()}".encode()
            if not hmac.compare_digest(request.headers.get("authorization", "").encode(), expected):
                return Response(status_code=401)
        elif settings.is_production:
            return Response(status_code=404)  # metrics must be explicitly enabled in production
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    install_error_handlers(app)
    app.include_router(health.router)
    api = "/api/v1"
    app.include_router(auth.router, prefix=api)
    app.include_router(mfa.router, prefix=api)
    app.include_router(admin.router, prefix=api)
    app.include_router(councils.router, prefix=api)
    app.include_router(providers.router, prefix=api)
    app.include_router(runs.router, prefix=api)
    app.include_router(skills.router, prefix=api)
    app.include_router(connections.router, prefix=api)
    app.include_router(observability.router, prefix=api)
    app.include_router(webhooks.router, prefix=api)
    app.include_router(data.router, prefix=api)
    app.include_router(knowledge.router, prefix=api)
    app.include_router(templates.router, prefix=api)
    app.include_router(notifications.router, prefix=api)
    # Agent protocols
    app.include_router(mcp_server.router)
    app.include_router(a2a.router)
    app.include_router(acp.router)
    return app
