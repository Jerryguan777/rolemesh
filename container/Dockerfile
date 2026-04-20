# RoleMesh Agent Container (Python)
# Runs Claude Agent SDK in an isolated Linux container.
#
# Hardening defaults (R9):
#   - Fixed UID 10001 (outside the 1000-1999 range typical for host users,
#     so bind-mounted host files are not accidentally owned by the agent).
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

# Install Python agent SDK (bundles Claude Code CLI) and Pi backend dependencies
RUN pip install --no-cache-dir \
    claude-agent-sdk croniter nats-py \
    anthropic openai google-genai boto3 httpx mcp \
    partial-json-parser pyyaml filelock Pillow mistune wcwidth

# Create app directory
WORKDIR /app

# Copy agent runner code, Pi backend, and shared IPC protocol.
# protocol.py imports rolemesh.auth.permissions, so we mirror the package
# layout under /app rather than flattening to a single module.
COPY src/agent_runner/ ./agent_runner/
COPY src/pi/ ./pi/
COPY src/rolemesh/ipc/protocol.py ./rolemesh/ipc/protocol.py
COPY src/rolemesh/auth/permissions.py ./rolemesh/auth/permissions.py
RUN touch ./rolemesh/__init__.py ./rolemesh/ipc/__init__.py ./rolemesh/auth/__init__.py

# Create workspace directories (no IPC dirs needed — all IPC goes through NATS)
RUN mkdir -p /workspace/group /workspace/global /workspace/extra

# Non-root user with a fixed UID so host/container UID mapping is stable
# across rebuilds and dev machines. 10001 avoids the host-user range.
RUN useradd -m -u 10001 -s /bin/bash agent || true
RUN chown -R agent:agent /workspace && chmod 700 /home/agent

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
