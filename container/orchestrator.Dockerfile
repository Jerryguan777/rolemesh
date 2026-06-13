# RoleMesh Orchestrator Container (docs/21 §4.5)
#
# Runs the orchestrator (`rolemesh`) inside compose instead of as a host
# process. The image carries the full project venv (uv sync, pi extra —
# matching the README's `uv sync --extra pi`); the dev compose file
# bind-mounts ../../src over /app/src for hot reload, which works because
# uv installs the project itself in editable mode.
#
# Build (context MUST be the repo root):
#   docker compose -f deploy/compose/compose.yaml build orchestrator
#
# Docker socket access: the image does NOT bake in a docker group —
# host docker GIDs vary per machine. The compose service grants it via
#   group_add: ["${DOCKER_GID:-999}"]
# with DOCKER_GID taken from .env (see README).
#
# One-off CLI runs (evaluation etc.) override the default command:
#   docker compose run --rm orchestrator rolemesh-eval ...

FROM python:3.12-slim

# uv pinned to a minor line for reproducible-enough local builds; the
# resolve itself is fully pinned by uv.lock (--frozen below).
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /usr/local/bin/

WORKDIR /app

# Non-root, fixed UID 1000: matches the typical first-user UID on Linux
# dev hosts, so the bind-mounted ./data tree (created host-side) stays
# writable in-container and files the orchestrator creates stay readable
# by the host user and by agent containers (whose image also runs UID
# 1000 — see container/Dockerfile).
RUN useradd -m -u 1000 -s /bin/bash orchestrator \
    && chown orchestrator:orchestrator /app

COPY --chown=orchestrator:orchestrator pyproject.toml uv.lock README.md ./
COPY --chown=orchestrator:orchestrator src/ ./src/

USER orchestrator

# Extras: `pi` for the multi-provider backend, `k8s` for the Kubernetes
# container runtime (kubernetes_asyncio). The same image runs under both
# ROLEMESH_CONTAINER_RUNTIME backends — k8s mode imports the K8sRuntime at
# startup, so the dependency must be present here, not optional; in docker
# mode the extra libs are simply unused. No dev extra — tests run on the
# host or in CI. watchfiles (hot reload) ships with uvicorn[standard].
RUN uv sync --frozen --extra pi --extra k8s --no-dev

# debugpy is NOT a project dependency (it would only ever be used here).
# Baking it into the image (~5 MB) buys "set DEBUGPY=1 and attach"
# without rebuilding — the dev-experience acceptance gate of docs/21
# §11 — which beats keeping the image minimal.
RUN uv pip install debugpy

COPY --chown=orchestrator:orchestrator container/orchestrator-entrypoint.sh /usr/local/bin/orchestrator-entrypoint
# COPY does not preserve +x reliably across checkouts; the entrypoint is
# invoked via sh instead of relying on the execute bit.

ENV PATH="/app/.venv/bin:$PATH" \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1

# debugpy listener (enabled only when DEBUGPY=1, see entrypoint).
EXPOSE 5678

# The deployment layer owns liveness; the orchestrator's own
# verify_infrastructure is the meaningful readiness gate.
HEALTHCHECK NONE

# ENTRYPOINT execs "$@", so `docker compose run --rm orchestrator <cmd>`
# replaces the default command cleanly (evaluation CLI, admin CLI, ...).
ENTRYPOINT ["sh", "/usr/local/bin/orchestrator-entrypoint"]
CMD ["rolemesh"]
