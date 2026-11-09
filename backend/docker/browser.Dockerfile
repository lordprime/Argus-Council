# syntax=docker/dockerfile:1.7
# Argus browser service: read-only browser sessions for council agents (agent-browser driving
# Chrome for Testing) behind an internal, token-protected API on a Unix socket.
#   docker build -f docker/browser.Dockerfile -t argus-council/browser backend/
#
# Both downloads are pinned to an exact version and SHA-256 and checked before use:
# - agent-browser: the release binary from github.com/vercel-labs/agent-browser (the sha256 is the
#   digest GitHub publishes for that release asset). It is a single native binary (no Node).
# - Chrome for Testing: the linux64 zip from the official bucket listed by
#   https://googlechromelabs.github.io/chrome-for-testing/ (the sha256 is of that zip; the bucket
#   publishes only MD5/CRC32C, which matched when the sha256 was taken).
# To move a pin, change the version and its sha256 together. Chrome for Testing is published for
# linux x64 only, so this image is amd64 only.
ARG PYTHON_VERSION=3.12
ARG AGENT_BROWSER_VERSION=0.41.2
ARG AGENT_BROWSER_SHA256=c4351f3802ffa5254d8c69f20a52e2fcf56925c6016fcf4203db25a07d811379
ARG CHROME_VERSION=157.0.8103.38
ARG CHROME_SHA256=8cdeb344e9b2cefe59f4aea8d1d93d5d93054c03cde217665c16fc1f62e72a66

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

# ---------------------------------------------------------------- pinned downloads, checksum-verified
FROM debian:bookworm-slim AS fetch
ARG AGENT_BROWSER_VERSION
ARG AGENT_BROWSER_SHA256
ARG CHROME_VERSION
ARG CHROME_SHA256
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*
# chrome_sandbox (the setuid sandbox helper) is deleted: it is never used, see "Chrome's own sandbox".
RUN --mount=type=secret,id=extra_ca,required=false \
    set -eu; \
    [ "$(dpkg --print-architecture)" = amd64 ] \
      || { echo "Chrome for Testing is published for linux x64 only; build this image for linux/amd64" >&2; exit 1; }; \
    if [ -f /run/secrets/extra_ca ]; then \
      cat /etc/ssl/certs/ca-certificates.crt /run/secrets/extra_ca >/tmp/ca.pem; export CURL_CA_BUNDLE=/tmp/ca.pem; \
    fi; \
    curl -fsSL --retry 3 -o /tmp/agent-browser \
      "https://github.com/vercel-labs/agent-browser/releases/download/v${AGENT_BROWSER_VERSION}/agent-browser-linux-x64"; \
    echo "${AGENT_BROWSER_SHA256}  /tmp/agent-browser" | sha256sum -c -; \
    install -D -m 0755 /tmp/agent-browser /opt/agent-browser/agent-browser; \
    curl -fsSL --retry 3 -o /tmp/chrome.zip \
      "https://storage.googleapis.com/chrome-for-testing-public/${CHROME_VERSION}/linux64/chrome-linux64.zip"; \
    echo "${CHROME_SHA256}  /tmp/chrome.zip" | sha256sum -c -; \
    unzip -q /tmp/chrome.zip -d /opt; \
    mv /opt/chrome-linux64 /opt/chrome; \
    rm -f /opt/chrome/chrome_sandbox /tmp/agent-browser /tmp/chrome.zip /tmp/ca.pem

# ---------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime
ARG AGENT_BROWSER_VERSION
ARG CHROME_VERSION
LABEL org.opencontainers.image.title="argus-council-browser" \
      org.opencontainers.image.source="https://github.com/lordprime/Argus-Council" \
      org.opencontainers.image.licenses="MIT" \
      io.argus.agent-browser.version="${AGENT_BROWSER_VERSION}" \
      io.argus.chrome-for-testing.version="${CHROME_VERSION}"

# Chrome's shared libraries, from the dependency list that ships inside the Chrome for Testing zip
# (deb.deps, the list Puppeteer's --install-deps uses), minus wget and xdg-utils, which only the
# desktop install script needs. Fails the build if any library Chrome links against is missing.
COPY --from=fetch /opt/chrome/deb.deps /tmp/chrome.deb.deps
RUN set -eu; \
    apt-get update; \
    apt-get satisfy -y --no-install-recommends \
      "$(grep -vE '^(wget|xdg-utils)( |$)|^[[:space:]]*$' /tmp/chrome.deb.deps | paste -sd, -)"; \
    rm -rf /var/lib/apt/lists/* /tmp/chrome.deb.deps

COPY --from=fetch --chown=root:root /opt/chrome /opt/chrome
COPY --from=fetch --chown=root:root /opt/agent-browser/agent-browser /usr/local/bin/agent-browser
COPY --from=build --chown=root:root /opt/venv /opt/venv
# Managed policy (root-owned, read-only for the service): turns off Chrome's own calls to Google
# services that no switch covers (captive portal checks, search preconnect, network time), and
# blocks downloads. Chrome for Testing reads its policies from /etc/opt/chrome_for_testing.
COPY --chown=root:root --chmod=0644 docker/chrome-policy.json /etc/opt/chrome_for_testing/policies/managed/argus-browser.json
RUN set -eu; \
    if ldd /opt/chrome/chrome | grep 'not found'; then echo "Chrome is missing shared libraries" >&2; exit 1; fi; \
    /opt/chrome/chrome --version; \
    agent-browser --version

# Chrome's own sandbox: Chrome runs with --no-sandbox (ARGUS_BROWSER_CHROME_EXTRA_ARGS). Its
# namespace sandbox needs unprivileged user namespaces, which Docker's default seccomp profile
# refuses without CAP_SYS_ADMIN, and its setuid fallback is ruled out by no-new-privileges and
# cap_drop ALL. The container is the boundary instead: non-root, no capabilities, read-only root,
# pid/memory limits, no route anywhere except the egress proxy, and no access to the API's data.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARGUS_ENV=production \
    HOME=/tmp \
    AGENT_BROWSER_SOCKET_DIR=/tmp/agent-browser \
    ARGUS_BROWSER_AGENT_BIN=/usr/local/bin/agent-browser \
    ARGUS_BROWSER_CHROME_BIN=/opt/chrome/chrome \
    ARGUS_BROWSER_WORKDIR=/tmp/argus-browser \
    ARGUS_BROWSER_CHROME_EXTRA_ARGS=--no-sandbox

# /run/argus-browser seeds the shared volume for the service's Unix socket. Sticky and writable,
# like /run/argus-runner: whichever container seeds the volume first, the browser user can create
# its token-protected socket and nobody else can replace it.
RUN groupadd --system --gid 10003 browser \
 && useradd --system --uid 10003 --gid browser --home-dir /tmp --no-create-home --shell /usr/sbin/nologin browser \
 && mkdir -p /run/argus-browser /app \
 && chown browser:browser /run/argus-browser && chmod 1777 /run/argus-browser

# Root-owned and read-only for the service: agent-browser reads ./agent-browser.json from its
# working directory, so that directory must not be writable.
WORKDIR /app
USER 10003:10003
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import socket,sys; s=socket.socket(socket.AF_UNIX); s.settimeout(4); s.connect('/run/argus-browser/browser.sock'); s.sendall(b'GET /health HTTP/1.0\\r\\nHost: browser\\r\\n\\r\\n'); sys.exit(0 if b' 200 ' in s.recv(64) else 1)"]

CMD ["uvicorn", "argus.browser.server:app", "--uds", "/run/argus-browser/browser.sock", "--no-server-header"]
