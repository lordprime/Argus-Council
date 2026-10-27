# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------- build
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
# Optional extra CA (corporate / egress proxies): docker build --secret id=extra_ca,src=ca.crt
RUN --mount=type=secret,id=extra_ca,required=false --mount=type=cache,target=/root/.cache/pip \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install "uv==0.11.32"
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN --mount=type=secret,id=extra_ca,required=false --mount=type=cache,target=/root/.cache/uv \
    if [ -f /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    uv sync --frozen --no-dev --no-install-project
COPY argus ./argus
# --no-cache: uv keys local-project wheels on pyproject metadata, not sources, so a cached
# build would ship stale code.
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    uv sync --frozen --no-dev --no-editable --no-cache

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
LABEL org.opencontainers.image.title="argus-council-api" \
      org.opencontainers.image.source="https://github.com/lordprime/Argus-Council" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARGUS_ENV=production \
    ARGUS_LOG_JSON=true \
    FORWARDED_ALLOW_IPS=127.0.0.1

# No package-manager steps: the slim base is rebuilt upstream for CVEs; PID 1 init comes from
# the runtime (`init: true` in compose).
RUN groupadd --system --gid 10001 argus \
 && useradd --system --uid 10001 --gid argus --home-dir /app --shell /usr/sbin/nologin argus

WORKDIR /app
COPY --from=build --chown=root:root /opt/venv /opt/venv
COPY --chown=root:root alembic.ini ./
COPY --chown=root:root alembic ./alembic
COPY --chown=root:root --chmod=0755 docker/entrypoint.sh /usr/local/bin/argus-entrypoint
# /run/argus-sandbox seeds the shared volume that carries the sandbox Unix socket (owned by argus).
# /run/argus-runner (CLI agents) is shared with the runner image's own user: sticky and writable so
# whichever container seeds the volume first, the runner can create its token-protected socket.
RUN mkdir -p /data /run/argus-sandbox /run/argus-runner && chown argus:argus /data /run/argus-sandbox \
 && chmod 1777 /run/argus-runner

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4).status == 200 else 1)"]

ENTRYPOINT ["argus-entrypoint"]
CMD ["uvicorn", "argus.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header"]
