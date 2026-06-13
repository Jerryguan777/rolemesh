# RoleMesh WebUI Container (docs/21 §4.5)
#
# Multi-stage: node builds the SPA (web/ -> dist), a python stage runs
# the FastAPI service (src/webui) and serves the built assets via the
# env-injectable WEB_UI_DIST path (src/webui/config.py).
#
# Build (context MUST be the repo root):
#   docker compose -f deploy/compose/compose.yaml build webui

# --- Stage 1: SPA build ----------------------------------------------------
# Node 22 LTS — web/package.json declares no engines constraint; vite 6
# supports 20/22, and 22 matches the version used on the dev host.
FROM node:22-alpine AS web-build

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
# OpenAPI client types are pre-generated and committed
# (web/src/api/generated); the build needs no contracts/ access.
RUN npm run build

# --- Stage 2: FastAPI runtime ----------------------------------------------
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /usr/local/bin/

WORKDIR /app

# Non-root. UID 1000 for symmetry with the orchestrator image; the webui
# touches no bind-mounted host data, so the exact UID is not load-bearing.
RUN useradd -m -u 1000 -s /bin/bash webui \
    && chown webui:webui /app

COPY --chown=webui:webui pyproject.toml uv.lock README.md ./
COPY --chown=webui:webui src/ ./src/

USER webui

# Base deps only: webui imports rolemesh.db / rolemesh.auth / fastapi —
# all in the project's core dependency set. No pi/eval/dev extras.
RUN uv sync --frozen --no-dev

COPY --from=web-build --chown=webui:webui /web/dist /app/web/dist

ENV PATH="/app/.venv/bin:$PATH" \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    # Absolute path to the SPA bundle baked above; src/webui/config.py
    # reads this env (default stays the host-dev relative web/dist).
    WEB_UI_DIST=/app/web/dist

EXPOSE 8080

HEALTHCHECK NONE

CMD ["rolemesh-webui"]
