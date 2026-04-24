# RoleMesh Agent Container (Python)
# Runs Claude Agent SDK in an isolated Linux container.
#
# Hardening defaults (R9):
#   - Fixed UID 1000 (matches the typical first-user UID on Linux dev
#     hosts so bind-mounted data/ directories created by the orchestrator
#     stay accessible to the agent inside the container; see the detailed
#     comment above the `useradd` call for why this is deliberate and
#     what to do if you ever need a different UID).
#   - Locale and Python buffering pinned — these are image properties, not
#     per-tenant configuration, so they belong here and not in
#     CONTAINER_ENV_ALLOWLIST.
#   - HEALTHCHECK NONE — the orchestrator owns lifecycle via NATS;
#     Docker-layer health probes just add a shell process for nothing.
#
# curl + git are intentionally KEPT (not a hardening hole):
#   - Agents that have reached shell execution can already run
#     `python -c "import urllib.request; ..."` — removing curl buys no
#     real attack-surface reduction, but removing git breaks a few
#     MCP tools and on-call debugging.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Python agent SDK (bundles Claude Code CLI) and Pi backend dependencies.
# ``structlog`` is needed by the rolemesh.core.logger shim that the safety
# loader + hook handler imports at module load — without it, agent_runner
# crashes at boot with ``No module named 'structlog'``.
RUN pip install --no-cache-dir \
    claude-agent-sdk croniter nats-py \
    anthropic openai google-genai boto3 httpx mcp \
    partial-json-parser pyyaml filelock Pillow mistune wcwidth \
    structlog

# Create app directory
WORKDIR /app

# Copy agent runner code, Pi backend, and shared IPC protocol.
# protocol.py imports rolemesh.auth.permissions, so we mirror the package
# layout under /app rather than flattening to a single module.
COPY src/agent_runner/ ./agent_runner/
COPY src/pi/ ./pi/
COPY src/rolemesh/ipc/protocol.py ./rolemesh/ipc/protocol.py
COPY src/rolemesh/auth/permissions.py ./rolemesh/auth/permissions.py

# rolemesh.safety subset needed by agent_runner's container-side hook
# chain + tool_reversibility helper. The orchestrator-only modules
# (audit.py, engine.py, subscriber.py, maintenance.py, rpc_server.py,
# the slow checks, egress_domain_rule) are intentionally NOT copied —
# they import asyncpg / rolemesh.db / approval / ML libraries that
# have no business in the agent image. We ship a stub __init__.py
# rather than the real one because the real __init__ eagerly imports
# audit + engine + loader's pg path and would fail at container
# startup.
COPY src/rolemesh/core/logger.py ./rolemesh/core/logger.py
COPY src/rolemesh/safety/types.py ./rolemesh/safety/types.py
COPY src/rolemesh/safety/errors.py ./rolemesh/safety/errors.py
COPY src/rolemesh/safety/loader.py ./rolemesh/safety/loader.py
COPY src/rolemesh/safety/pipeline_core.py ./rolemesh/safety/pipeline_core.py
COPY src/rolemesh/safety/rpc_codec.py ./rolemesh/safety/rpc_codec.py
COPY src/rolemesh/safety/registry.py ./rolemesh/safety/registry.py
COPY src/rolemesh/safety/tool_reversibility.py ./rolemesh/safety/tool_reversibility.py
# audit.py is safe to ship to agents: it holds the stateless
# compute_context_digest/summarize_context helpers pipeline_core
# imports. ``DbAuditSink`` is in this file too but lazy-imports pg
# inside ``.write`` — the agent container never instantiates it, so
# the lazy path never fires.
COPY src/rolemesh/safety/audit.py ./rolemesh/safety/audit.py
COPY src/rolemesh/safety/checks/pii_regex.py ./rolemesh/safety/checks/pii_regex.py
COPY src/rolemesh/safety/checks/domain_allowlist.py ./rolemesh/safety/checks/domain_allowlist.py

RUN touch ./rolemesh/__init__.py ./rolemesh/ipc/__init__.py ./rolemesh/auth/__init__.py \
    && touch ./rolemesh/core/__init__.py \
    && touch ./rolemesh/safety/__init__.py \
    && touch ./rolemesh/safety/checks/__init__.py

# Create workspace directories (no IPC dirs needed — all IPC goes through NATS)
RUN mkdir -p /workspace/group /workspace/global /workspace/extra

# Non-root user with a fixed UID so host/container UID mapping is stable
# across rebuilds and base-image version bumps. 1000 matches the typical
# first-user UID on Linux dev hosts; this is deliberate — agent containers
# bind-mount host-created directories (data/tenants/.../sessions/), and
# those directories are owned by the host user that launched rolemesh.
# Any UID mismatch here produces EACCES on session writes.
#
# A prior revision used 10001 on the "avoid host-user range" argument; that
# broke session-file persistence on every dev laptop and was caught only by
# manual acceptance (2026-04-20). If you ever need to collide-proof this,
# set up userns-remap at the daemon level rather than drifting the in-image
# UID away from 1000.
#
# IMPORTANT: this UID is also hardcoded in rolemesh/container/runner.py as
# AGENT_UID. If you change the `-u 1000` below, change AGENT_UID too —
# otherwise tmpfs mounts will be owned by the wrong UID and produce silent
# EACCES on agent writes.
RUN useradd -m -u 1000 -s /bin/bash agent || true
# 711 (rwx--x--x) rather than 700: the orchestrator may run the agent
# container as a host UID other than 1000 (macOS 501/502, Linux non-
# first-user). Those UIDs need `traverse` permission on /home/agent to
# reach the tmpfs subdirs (.cache/.config/.pi) and the bind-mounted
# .claude. They don't need `read` on /home/agent itself — the directory
# has no sensitive content at its top level; all secrets live in
# children that keep their own stricter modes (tmpfs entries are 700
# owned by the runtime UID; .claude is per-coworker bind mount).
RUN chown -R agent:agent /workspace && chmod 711 /home/agent

# Switch to non-root user
USER agent

# Ensure agent_runner is importable from any working directory.
# LANG / LC_ALL / PYTHONUNBUFFERED are image properties, not tenant knobs.
ENV PYTHONPATH=/app \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1

# Set working directory to group workspace
WORKDIR /workspace/group

# Orchestrator owns lifecycle via NATS — Docker healthcheck would just spawn
# an extra process and mask real problems behind a "healthy" status.
HEALTHCHECK NONE

# Entry point reads config from NATS KV, outputs results via NATS JetStream
ENTRYPOINT ["python", "-m", "agent_runner"]
