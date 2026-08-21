# --- frontend build ---------------------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend .
RUN npm run build

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/* \
    # The vault volume's uid varies by host; without this, git's dubious-
    # ownership check rejects /data/vault and every git shell-out dies.
    && git config --system --add safe.directory '*'
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev

COPY backend/app ./app
COPY --from=frontend /fe/build ./frontend-build

ENV OMNIA_DATA_DIR=/data \
    OMNIA_FRONTEND_DIR=/app/frontend-build \
    HF_HOME=/data/hf-cache

VOLUME /data
EXPOSE 8000
# Only Caddy can reach this unpublished container port. Trust its spoof-safe
# X-Forwarded-* headers so sessions record the real client IP and HTTPS scheme.
CMD ["uv", "run", "--no-sync", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
