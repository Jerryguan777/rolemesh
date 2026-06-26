# Red-team sandbox MCP image (TEST / RED-TEAM ONLY — NOT FOR PRODUCTION).
#
# One image serves all three servers; compose picks which one to run by
# overriding `command:` (e.g. `python records_mcp.py`). Build context is
# the repo root so the COPY path resolves:
#
#   docker build -f redteam/mcp/Dockerfile -t rolemesh-redteam-mcp:latest .
#
# These servers hold only fake, seeded data and are wired onto an isolated
# `redteam-mcp-net` reachable by the egress gateway — never by agents
# directly. See redteam/mcp/README.md.
FROM python:3.12-slim

WORKDIR /app

COPY redteam/mcp/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY redteam/mcp/ /app/

# Default target; overridden per-service in compose.redteam.yaml.
CMD ["python", "files_mcp.py"]
