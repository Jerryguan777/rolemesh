# RoleMesh

> Multi-tenant agent platform with enterprise-grade safety.

Run AI coworkers on your own infrastructure: each tenant gets sandboxed Claude or Pi agents, reachable from any team channel, governed by a real safety pipeline and a human-approval flow.

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.12%2B-blue)

---

## Why RoleMesh

Most agent platforms today either run as a closed SaaS (Claude Projects, Devin) or as a single-tenant library (LangChain, AutoGPT, CrewAI). Neither fits when an AI coworker has to handle real company data, talk in your team channels, and not exfiltrate credentials.

RoleMesh is built for that gap:

- **Self-hosted**, AGPL-licensed.
- **Multi-tenant from the database up** — Postgres Row-Level Security on every tenant-scoped table, dual-pool fail-closed default.
- **Sandboxed by architecture, not by bolt-on** — container hardening, isolated agent network, gateway-only egress, three-stage safety pipeline.
- **Two interchangeable agent runtimes** — Claude SDK or Pi (open-source, model-agnostic).

---

## Features

### 1. Top-tier agent runtime

- **Claude SDK** — proven by Claude Code (Anthropic's official agentic CLI).
- **Pi** — open-source, model-agnostic. Ported from [pi-mono](<TODO: pi-mono GitHub URL>) (the runtime behind OpenClaw). Supports Anthropic, OpenAI, Google Gemini, and AWS Bedrock.

### 2. Enterprise-grade safety

- **Container hardening** — readonly rootfs, dropped capabilities, user-namespace remap, optional gVisor runtime.
- **Network isolation** — `Internal=true` agent bridge plus a dual-homed egress gateway. Agent containers have no direct route to the internet; every outbound flow passes through the gateway.
- **Safety policy engine** — three-stage pipeline (`INPUT_PROMPT` / `PRE_TOOL_CALL` / `MODEL_OUTPUT`). Built-in checks plus optional ML-backed detectors (`presidio`, `llm-guard`, `detect-secrets`) via the `safety-ml` extra.
- **Credential proxy** — real API keys live only on the host. Agent containers see placeholders; the proxy rewrites the `Authorization` header at the HTTP layer.

### 3. Multi-tenant management and isolation

- **Database-level isolation** — Postgres Row-Level Security on every tenant-scoped table. Dual-pool architecture (`rolemesh_app` NOBYPASSRLS plus `rolemesh_system` BYPASSRLS) gives fail-closed isolation by default.
- **Easy multi-tenant SaaS integration** — bring your own tenant identity; RoleMesh maps it to scoped DB connections and isolated agent containers.

### 4. Interactive interfaces

- Web chat (FastAPI plus WebSocket).
- Telegram.
- Slack.
- Microsoft Teams (planned).

### 5. Human-approval flow

- `Agent plan → human approve → Agent execute`.
- Goes beyond chat: your coworker can take real actions, with you kept in the loop.
- Batch approval for related actions.

### 6. Per-coworker skills

- Markdown-based capability folders (a `SKILL.md` plus optional supporting files) that the agent reads on demand.
- The model auto-invokes a skill based on its frontmatter `description` — no slash commands, no human-side wiring.
- DB-backed and tenant-scoped: skills live in Postgres with RLS, projected per-spawn into a read-only bind mount, never shared across tenants.
- Backend-aware frontmatter: write a skill once, project it to either Claude SDK (`/home/agent/.claude/skills`) or Pi (`/home/agent/.pi/skills`); fields scoped to the other backend are dropped at projection time.

### 7. Evaluation framework

- `rolemesh-eval` CLI — Inspect AI based, manual / nightly tool for measuring how coworker behavior changes across `system_prompt` / `tools` / `skills` / `agent_backend` / `model` configurations.
- Three orthogonal scorers: `final_answer` (exact / regex / LLM-judge), `tool_trace` (required / forbidden / expected order), `cost` (per-sample latency + token spend).
- Reuses the production `ContainerAgentExecutor` so eval runs the same code path that handles real traffic.
- Coworker config snapshot inlined into each run with a sha256 over the canonical form, so `rolemesh-eval list` clusters runs that share a configuration.

---

## Architecture

```
            ┌──────────┐  ┌──────────┐  ┌──────────┐
            │  WebUI   │  │ Telegram │  │  Slack   │
            └────┬─────┘  └────┬─────┘  └────┬─────┘
                 │             │             │
                 └─────────────┼─────────────┘
                               │ NATS
                               ▼
                  ┌──────────────────────┐
                  │     Orchestrator     │     Postgres
                  │   (rolemesh proc.)   │ ◄─►  (RLS)
                  └──────────┬───────────┘
                             │ aiodocker
                             ▼
                  ┌──────────────────────┐
                  │   Agent Containers   │      ┌──────────────┐
                  │   (per coworker,     │ ◄──► │   Egress     │ ──► LLM
                  │    sandboxed,        │      │   Gateway    │     MCP
                  │    Internal=true)    │      │ (proxy + DNS │     internet
                  └──────────────────────┘      │  + safety)   │
                                                └──────────────┘
```

---

## Quick Start

Prerequisites: Python 3.12+, Docker, and [uv](https://github.com/astral-sh/uv).

```bash
git clone https://github.com/<TODO>/rolemesh.git
cd rolemesh

# Install dependencies (default: Claude SDK only)
uv sync --extra dev

# Or include the Pi backend for multi-provider support
uv sync --extra pi --extra dev

# Add the eval extra to use rolemesh-eval (Inspect AI based, manual)
uv sync --extra pi --extra dev --extra eval

# Bring up Postgres + NATS
docker compose -f docker-compose.dev.yml up -d

# Build the agent and egress-gateway container images
container/build.sh
container/build-egress-gateway.sh

# Configure .env — see "Configuration" below
$EDITOR .env

# Start the orchestrator
rolemesh

# In a second shell: start the WebUI
rolemesh-webui
```

Open <http://localhost:8080> in your browser.

---

## Configuration

Create a `.env` file at the project root. The tables below list the most useful keys, grouped by purpose.

### LLM provider (pick at least one)

| Key                          | Purpose                                                              |
|------------------------------|----------------------------------------------------------------------|
| `CLAUDE_CODE_OAUTH_TOKEN`    | Anthropic Max-subscription OAuth token (no per-call API charges).    |
| `ANTHROPIC_API_KEY`          | Direct Anthropic API key.                                            |
| `PI_OPENAI_API_KEY`          | Pi backend — OpenAI.                                                 |
| `PI_GOOGLE_API_KEY`          | Pi backend — Google Gemini.                                          |
| `AWS_BEARER_TOKEN_BEDROCK`   | Pi backend — AWS Bedrock long-term API key.                          |
| `PI_MODEL_ID`                | e.g. `openai/gpt-4o`, `amazon-bedrock/us.anthropic.claude-sonnet-4-6`.|

### Channels

| Key                  | Required for     |
|----------------------|------------------|
| `TELEGRAM_BOT_TOKEN` | Telegram channel |
| `SLACK_BOT_TOKEN`    | Slack channel    |
| `SLACK_APP_TOKEN`    | Slack channel    |

### Storage

| Key                  | Purpose                                                          |
|----------------------|------------------------------------------------------------------|
| `DATABASE_URL`       | App-pool URL (`rolemesh_app` role, NOBYPASSRLS).                 |
| `ADMIN_DATABASE_URL` | Admin-pool URL for migrations and system-level tasks.            |
| `NATS_URL`           | Defaults to `nats://localhost:4222`.                             |

### Misc

| Key                       | Purpose                                                                  |
|---------------------------|--------------------------------------------------------------------------|
| `ASSISTANT_NAME`          | Display name for your AI coworker.                                       |
| `CONTAINER_NETWORK_NAME`  | Set to enable EC-2 (Internal=true) agent bridge with egress gateway.     |
| `ADMIN_BOOTSTRAP_TOKEN`   | First-run admin token; rotate after the first sign-in.                   |
| `ROLEMESH_AGENT_BACKEND`  | `claude` (default) or `pi`.                                              |

See `docs/auth-architecture.md` for the full auth model.

---

## Agent backends

Two interchangeable runtimes; configurable per-coworker or globally via `ROLEMESH_AGENT_BACKEND`.

| Backend     | Models                                                | Best for                                                    |
|-------------|-------------------------------------------------------|-------------------------------------------------------------|
| **Claude**  | Anthropic                                             | Standard Claude Code workflows; OAuth Max subscription.     |
| **Pi**      | Anthropic, OpenAI, Google Gemini, AWS Bedrock         | Multi-provider, on-prem Bedrock, model-agnostic strategies. |

Switch backends with:

```bash
ROLEMESH_AGENT_BACKEND=pi \
PI_MODEL_ID=amazon-bedrock/us.anthropic.claude-sonnet-4-6 \
rolemesh
```

Details: `docs/switchable-agent-backend.md`.

---

## Channels

| Channel  | Setup                                                                   |
|----------|-------------------------------------------------------------------------|
| WebUI    | `rolemesh-webui` (defaults to port 8080).                               |
| Telegram | Set `TELEGRAM_BOT_TOKEN`; gateway starts automatically with the orchestrator. |
| Slack    | Set `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`.                              |
| Teams    | Planned — <TODO: link to tracking issue>.                               |

---

## Skills

Per-coworker skill folders are managed via the admin REST API:

```bash
# List skills on a coworker
curl -H "Authorization: Bearer <admin-token>" \
  http://localhost:8080/api/admin/agents/<coworker_id>/skills

# Create a skill (SKILL.md + optional supporting files in one payload)
curl -X POST -H "Authorization: Bearer <admin-token>" \
     -H "Content-Type: application/json" \
     -d @my_skill.json \
     http://localhost:8080/api/admin/agents/<coworker_id>/skills
```

The `files` map accepts either flat strings (`{"SKILL.md": "..."}`) or richer shapes (`{"SKILL.md": {"content": "...", "mime_type": "text/markdown"}}`). The first `---…---` block in `SKILL.md` is parsed as YAML frontmatter and split into a structured `frontmatter_common` + `frontmatter_backend` (per-backend overrides). Skills project at agent spawn time; toggling `enabled` or editing a body affects the next spawn, not the running container.

Architecture: `docs/skills-architecture.md`.

---

## Evaluation

Manual / nightly measurement of coworker behavior under different configurations.

```bash
# Run an eval over a JSONL dataset
rolemesh-eval --tenant <tenant_uuid> run \
  --coworker <coworker_id_or_folder> \
  --dataset path/to/dataset.jsonl \
  --threshold "scorers.final_answer_scorer.accuracy>=0.9"

# List past runs (filter by coworker, get JSON)
rolemesh-eval --tenant <tenant_uuid> list --coworker <id_or_folder> --json

# Show a single run
rolemesh-eval --tenant <tenant_uuid> show <run_uuid>
```

Exit codes: `0` (run completed and thresholds met), `1` (infrastructure / config error), `2` (run completed but at least one threshold violated).

Requires `--extra eval` plus a reachable Postgres + NATS + Docker daemon. Eval reuses the production `ContainerAgentExecutor` rather than rolling a parallel orchestrator, so containers behave identically to real traffic. The orchestrator daemon need not be running (eval bootstraps its own gateway and DNS registration).

---

## Safety model

Three layers, all enabled by default. Details in `docs/safety/`.

1. **Container layer** — readonly rootfs, dropped capabilities, user-namespace remap, optional gVisor. See `docs/safety/container-hardening.md`.
2. **Network layer** — Internal-only agent bridge plus a dual-homed egress gateway. Per-tenant DNS allowlist, reverse-proxy credential injection, no direct internet from agents. See `docs/egress/deployment.md`.
3. **Policy layer** — three-stage hooks (`INPUT_PROMPT`, `PRE_TOOL_CALL`, `MODEL_OUTPUT`) with built-in checks plus optional ML-backed detectors via the `safety-ml` extra. See `docs/safety/safety-framework.md`.

Human-approval flow lives orthogonally on the policy layer — see `docs/approval-architecture.md`.

---

## Development

```bash
# Install dev + pi + eval extras
uv sync --extra pi --extra dev --extra eval

# Run unit and integration tests (Postgres via testcontainers)
uv run pytest

# Type-check and lint
uv run mypy src
uv run ruff check src tests

# Verify container hardening
scripts/verify-hardening.sh
```

Step-by-step build plan and contribution flow: `STEPS.md`.

---

## Documentation

| Topic                            | File                                            |
|----------------------------------|-------------------------------------------------|
| Multi-tenant architecture        | `docs/multi-tenant-architecture.md`             |
| Auth and permissions             | `docs/auth-architecture.md`                     |
| Approval flow                    | `docs/approval-architecture.md`                 |
| Safety framework                 | `docs/safety/safety-framework.md`               |
| Container hardening              | `docs/safety/container-hardening.md`            |
| Attack-simulation matrix         | `docs/safety/attack-simulation-matrix.md`       |
| Egress gateway deployment        | `docs/egress/deployment.md`                     |
| Hooks architecture               | `docs/hooks-architecture.md`                    |
| NATS IPC                         | `docs/nats-ipc-architecture.md`                 |
| Agent executor and runtime       | `docs/agent-executor-and-container-runtime.md`  |
| Switchable agent backend         | `docs/switchable-agent-backend.md`              |
| WebUI architecture               | `docs/webui-architecture.md`                    |
| Event-stream architecture        | `docs/event-stream-architecture.md`             |
| Steering architecture            | `docs/steering-architecture.md`                 |
| External MCP                     | `docs/external-mcp-architecture.md`             |
| PPI integration guide            | `docs/ppi-integration-guide.md`                 |

---

## License

[AGPL-3.0-or-later](LICENSE). See [`NOTICE`](NOTICE) for attribution.
