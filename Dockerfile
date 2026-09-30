# Base images are pinned one step past the tags they used to float on, so a
# rebuild of the same commit gets the same versions. The server reuses its
# cached copies until `./deploy.sh ... --refresh` pulls these tags again (OS
# rebuilds, and patch releases where the tag names only a minor). To move a
# version on, change it here.

# --- frontend build ---------------------------------------------------------
FROM node:22.23-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend .
RUN npm run build

# --- runtime ----------------------------------------------------------------
FROM python:3.12.14-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    # The vault volume's uid varies by host; without this, git's dubious-
    # ownership check rejects /data/vault and every git shell-out dies.
    # Running as app (10001) does not remove the need: deploy.sh hands the
    # volume to 10001, but a restored or hand-copied one may not be.
    && git config --system --add safe.directory '*'
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

# The app runs as this user (USER below). A fresh named volume takes the owner
# of the image directory it mounts over, so /data is created here for it: a
# first deploy or a restore into an empty volume comes up writable.
RUN groupadd -g 10001 app \
    && useradd -u 10001 -g app -m app \
    && mkdir /data \
    && chown app:app /data

WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
# Bytecode is compiled at build time because the running container cannot
# write it: its root fs is read-only (docker-compose.yml).
RUN uv sync --frozen --no-dev --compile-bytecode

COPY backend/app ./app
RUN python -m compileall -q app
COPY --from=frontend /fe/build ./frontend-build

# fastembed ignores HF_HOME: it hands the Hub download its own cache_dir,
# FASTEMBED_CACHE_PATH, which defaults to /tmp/fastembed_cache in the
# container's writable layer, so every recreate re-downloaded the model.
# The root fs is read-only, so whatever else caches under ~/.cache (the Hub
# client's and hf_xet's scratch among it) goes to the /tmp tmpfs instead.
# OMNIA_REQUIRE_AUTH: the container refuses to boot with an empty password or
# the default session secret; running it without auth takes an explicit
# OMNIA_REQUIRE_AUTH=0.
ENV OMNIA_DATA_DIR=/data \
    OMNIA_FRONTEND_DIR=/app/frontend-build \
    FASTEMBED_CACHE_PATH=/data/fastembed-cache \
    HOME=/home/app \
    XDG_CACHE_HOME=/tmp/.cache \
    OMNIA_REQUIRE_AUTH=1

# The commit this image was built from (deploy.sh passes it), served by
# /api/health. Declared after the COPYs so a new commit does not bust their
# cache.
ARG GIT_SHA=dev
ENV OMNIA_GIT_SHA=$GIT_SHA

VOLUME /data
EXPOSE 8000
# Not root: a parser bug or an injected tool call lands as uid 10001, with no
# capabilities and a read-only root fs (docker-compose.yml).
USER app
# Only Caddy can reach this unpublished container port. Trust its spoof-safe
# X-Forwarded-* headers so sessions record the real client IP and HTTPS scheme.
# uvicorn runs lifespan shutdown (final autosave commit, backup push) only once
# every connection has closed; the graceful timeout cancels any request still
# open after 5s so that happens well inside compose's stop_grace_period.
# The venv binary makes uvicorn PID 1, so SIGTERM reaches it directly.
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "5"]
