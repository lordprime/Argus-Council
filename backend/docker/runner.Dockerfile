# syntax=docker/dockerfile:1.7
# Argus agent runner: the vendors' official CLIs (claude, codex, gemini) behind an internal API.
#   docker build -f docker/runner.Dockerfile -t argus-council/runner backend/
ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22

# ---------------------------------------------------------------- python app (same as the API image)
FROM python:${PYTHON_VERSION}-slim-bookworm AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
RUN --mount=type=secret,id=extra_ca,required=false --mount=type=cache,target=/root/.cache/pip \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    pip install "uv==0.11.32"
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN --mount=type=secret,id=extra_ca,required=false --mount=type=cache,target=/root/.cache/uv \
    if [ -f /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    uv sync --frozen --no-dev --no-install-project
COPY argus ./argus
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export SSL_CERT_FILE=/run/secrets/extra_ca; fi; \
    uv sync --frozen --no-dev --no-editable --no-cache

# ---------------------------------------------------------------- vendor CLIs (pinned by package-lock.json)
FROM node:${NODE_VERSION}-bookworm-slim AS clis
WORKDIR /opt/clis
COPY docker/runner/package.json docker/runner/package-lock.json ./
# --ignore-scripts: no third-party install scripts run. Claude Code's postinstall only links its
# platform binary from the optional dependency npm already downloaded, so run just that one.
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -f /run/secrets/extra_ca ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/extra_ca; fi; \
    npm ci --omit=dev --ignore-scripts --no-audit --no-fund \
 && node node_modules/@anthropic-ai/claude-code/install.cjs \
 && npm cache clean --force

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
LABEL org.opencontainers.image.title="argus-council-runner" \
      org.opencontainers.image.source="https://github.com/lordprime/Argus-Council" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/opt/clis/node_modules/.bin:/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARGUS_ENV=production \
    ARGUS_RUNNER_HOME_ROOT=/home/runner/users \
    DISABLE_AUTOUPDATER=1

RUN groupadd --system --gid 10002 runner \
 && useradd --system --uid 10002 --gid runner --home-dir /home/runner --shell /usr/sbin/nologin runner \
 && mkdir -p /home/runner/users /etc/gemini-cli/policies \
 && chown runner:runner /home/runner /home/runner/users && chmod 0700 /home/runner/users \
 && chmod 0755 /etc/gemini-cli /etc/gemini-cli/policies

COPY --from=clis /usr/local/bin/node /usr/local/bin/node
COPY --from=clis --chown=root:root /opt/clis /opt/clis
COPY --from=build --chown=root:root /opt/venv /opt/venv
# Root-owned and not writable by the runner user: the Gemini CLI only honours admin policies then.
COPY --chown=root:root --chmod=0644 docker/runner/gemini-deny-all.toml /etc/gemini-cli/policies/argus-deny-all.toml

USER 10002:10002
WORKDIR /home/runner
EXPOSE 8200
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8200/health', timeout=4).status == 200 else 1)"]

CMD ["uvicorn", "argus.runner.server:app", "--host", "0.0.0.0", "--port", "8200", "--no-server-header"]
