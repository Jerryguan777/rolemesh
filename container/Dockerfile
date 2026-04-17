# RoleMesh Agent Container (Python)
# Runs Claude Agent SDK in an isolated Linux container

FROM python:3.12-slim

# Install essential tools only
# Chromium is NOT included — agent-browser is a Node.js package with no Python equivalent.
# WebSearch/WebFetch work without a browser (built into Claude Code CLI).
# If browser automation is needed in the future, add playwright + chromium.
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

# Create non-root user for sandbox
RUN useradd -m -s /bin/bash agent || true
RUN chown -R agent:agent /workspace && chmod 777 /home/agent

# Switch to non-root user
USER agent

# Ensure agent_runner is importable from any working directory
ENV PYTHONPATH=/app

# Set working directory to group workspace
WORKDIR /workspace/group

# Entry point reads config from NATS KV, outputs results via NATS JetStream
ENTRYPOINT ["python", "-m", "agent_runner"]
