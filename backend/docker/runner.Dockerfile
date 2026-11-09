# syntax=docker/dockerfile:1.7
# Argus agent runner: the vendors' official CLIs behind an internal API. claude, codex, gemini,
# copilot, qwen and opencode come from npm (pinned by package-lock.json); kiro-cli and goose from
# the vendors' release downloads (pinned by version and SHA-256); vibe from PyPI (hash-pinned).
#   docker build -f docker/runner.Dockerfile -t argus-council/runner backend/
ARG PYTHON_VERSION=3.12
ARG NODE_VERSION=22
ARG KIRO_VERSION=3.1.0
ARG KIRO_SHA256=6f43c9ca9811d1ef2d4b47148667243b5af2d4aaea7cdef3e9a6af1c10d4c8ba
ARG GOOSE_VERSION=1.57.0
ARG GOOSE_SHA256=10eb96cb6a159b6e1d38eeedbef6ab64cd28168aad91948f1fa5c34eefdf98e3

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
 && mkdir -p /opt/clis/bin \
 # opencode's postinstall only copies its platform binary into place; do that one step (baseline:
 # no AVX2 requirement).
 && cp node_modules/opencode-linux-x64-baseline/bin/opencode /opt/clis/bin/opencode \
 && npm cache clean --force

# ---------------------------------------------------------------- release downloads (checksum-pinned)
FROM python:${PYTHON_VERSION}-slim-bookworm AS downloads
ARG KIRO_VERSION
ARG KIRO_SHA256
ARG GOOSE_VERSION
ARG GOOSE_SHA256
ADD --checksum=sha256:${KIRO_SHA256} \
    https://desktop-release.q.us-east-1.amazonaws.com/${KIRO_VERSION}/kirocli-x86_64-linux.zip /tmp/kiro.zip
ADD --checksum=sha256:${GOOSE_SHA256} \
    https://github.com/aaif-goose/goose/releases/download/v${GOOSE_VERSION}/goose-x86_64-unknown-linux-gnu.tar.gz /tmp/goose.tar.gz
RUN mkdir -p /opt/clis/bin /tmp/kiro /tmp/goose \
 && python -c "import zipfile; zipfile.ZipFile('/tmp/kiro.zip').extractall('/tmp/kiro')" \
 && find /tmp/kiro -path '*/bin/*' -type f -exec install -m 0755 {} /opt/clis/bin/ \; \
 && test -x /opt/clis/bin/kiro-cli \
 && tar -xzf /tmp/goose.tar.gz -C /tmp/goose \
 && find /tmp/goose -name goose -type f -exec install -m 0755 {} /opt/clis/bin/goose \; \
 && test -x /opt/clis/bin/goose

# ---------------------------------------------------------------- Mistral Vibe (own venv, hash-pinned)
FROM python:${PYTHON_VERSION}-slim-bookworm AS vibe
COPY docker/runner/vibe-requirements.txt /tmp/vibe-requirements.txt
RUN --mount=type=secret,id=extra_ca,required=false --mount=type=cache,target=/root/.cache/pip \
    if [ -f /run/secrets/extra_ca ]; then export PIP_CERT=/run/secrets/extra_ca; fi; \
    python -m venv /opt/vibe \
 && /opt/vibe/bin/pip install --no-deps --require-hashes -r /tmp/vibe-requirements.txt

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
LABEL org.opencontainers.image.title="argus-council-runner" \
      org.opencontainers.image.source="https://github.com/lordprime/Argus-Council" \
      org.opencontainers.image.licenses="MIT"

ENV PATH="/opt/clis/bin:/opt/clis/node_modules/.bin:/opt/vibe/bin:/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARGUS_ENV=production \
    ARGUS_RUNNER_HOME_ROOT=/home/runner/users \
    DISABLE_AUTOUPDATER=1 \
    COPILOT_AUTO_UPDATE=false \
    OPENCODE_DISABLE_AUTOUPDATE=1

RUN groupadd --system --gid 10002 runner \
 && useradd --system --uid 10002 --gid runner --home-dir /home/runner --shell /usr/sbin/nologin runner \
 && mkdir -p /home/runner/users /etc/gemini-cli/policies /etc/kiro /etc/qwen-code /etc/opencode /run/argus-runner \
 && chown runner:runner /home/runner /home/runner/users /run/argus-runner && chmod 0700 /home/runner/users \
 && chmod 1777 /run/argus-runner \
 && chmod 0755 /etc/gemini-cli /etc/gemini-cli/policies /etc/kiro /etc/qwen-code /etc/opencode

COPY --from=clis /usr/local/bin/node /usr/local/bin/node
COPY --from=clis --chown=root:root /opt/clis /opt/clis
COPY --from=downloads --chown=root:root /opt/clis/bin/ /opt/clis/bin/
COPY --from=vibe --chown=root:root /opt/vibe /opt/vibe
COPY --from=build --chown=root:root /opt/venv /opt/venv
# Root-owned and not writable by the runner user: the Gemini CLI only honours admin policies then.
COPY --chown=root:root --chmod=0644 docker/runner/gemini-deny-all.toml /etc/gemini-cli/policies/argus-deny-all.toml
# Operator policies that turn every tool of these CLIs off; root-owned, so users cannot override them.
COPY --chown=root:root --chmod=0644 docker/runner/policies/kiro-managed-settings.json /etc/kiro/managed-settings.json
COPY --chown=root:root --chmod=0644 docker/runner/policies/qwen-settings.json /etc/qwen-code/settings.json
COPY --chown=root:root --chmod=0644 docker/runner/policies/opencode.json /etc/opencode/opencode.json

USER 10002:10002
WORKDIR /home/runner
# The control API listens on a Unix socket in a volume shared with the API (no TCP port).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(4); s.connect('/run/argus-runner/runner.sock'); s.sendall(b'GET /health HTTP/1.0\\r\\nHost: runner\\r\\n\\r\\n'); sys.exit(0 if b' 200 ' in s.recv(64) else 1)"]

CMD ["uvicorn", "argus.runner.server:app", "--uds", "/run/argus-runner/runner.sock", "--no-server-header"]
