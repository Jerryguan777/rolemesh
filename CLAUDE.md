# CLAUDE.md — RoleMesh Project Context

## What is RoleMesh?

RoleMesh is a **multi-tenant, Claude-powered orchestration platform** that manages AI agents ("coworkers") across messaging channels (Slack, Telegram, Web UI) and scheduled tasks. Each agent runs in an isolated Docker container with Claude Code, communicating via NATS messaging and persisting state in PostgreSQL.

License: AGPL-3.0-or-later (derived from NanoClaw, MIT).

## Architecture Overview

Three main processes:

1. **Orchestrator** (`src/rolemesh/`) — Central event loop: routes messages from channels to agents, manages container lifecycle, enforces concurrency limits, runs scheduled tasks.
2. **Agent Runner** (`src/agent_runner/`) — Runs inside Docker containers. Reads init data from NATS KV, invokes Claude Agent SDK, publishes results/messages/tasks back via NATS JetStream.
3. **Web UI Server** (`src/webui/`) — FastAPI app serving REST API + WebSocket for browser-based chat. Handles auth (OIDC/JWT/builtin).

Communication: **NATS** (JetStream + KV) for IPC between orchestrator and agent containers. **PostgreSQL** for persistent multi-tenant state.

## Data Flow

```
User message (Slack/Telegram/Web)
  → Channel Gateway → Router (find coworker by conversation binding)
  → Store message in PostgreSQL
  → Enqueue to GroupQueue (container scheduler)
  → ContainerAgentExecutor spawns Docker container
  → Agent Runner reads AgentInitData from NATS KV
  → claude_agent_sdk.query() executes with system prompt + tools
  → Results published to NATS JetStream
  → Orchestrator consumes results → routes response back to channel
```

## Source Layout

```
src/
├── rolemesh/                    # Orchestrator (main process)
│   ├── main.py                  # Entry point, event loop
│   ├── core/                    # Types, config, state, logging
│   │   ├── orchestrator_state.py  # Runtime state: coworkers, conversations, concurrency
│   │   ├── config.py            # Environment-based configuration
│   │   └── types.py             # Domain models: Tenant, Coworker, Conversation, etc.
│   ├── agent/                   # Agent protocol & container executor
│   │   └── container_executor.py  # Spawns Docker containers, polls NATS for output
│   ├── channels/                # Channel gateways
│   │   ├── slack.py             # Slack Bot (multiple app instances per binding)
│   │   ├── telegram.py          # Telegram Bot
│   │   └── web_nats.py          # Web channel bridge to NATS
│   ├── container/               # Container runtime & scheduling
│   │   ├── runner.py            # Volume mounts, container spec building
│   │   ├── docker_runtime.py    # Docker API integration
│   │   └── scheduler.py         # Concurrency control (global/tenant/coworker limits)
│   ├── orchestration/           # Message routing & task scheduling
│   │   ├── router.py            # Routes messages to correct coworker
│   │   └── task_scheduler.py    # Cron/interval/once scheduled task execution
│   ├── ipc/                     # NATS transport & protocol
│   │   ├── protocol.py          # AgentInitData, McpServerSpec definitions
│   │   └── task_handler.py      # Processes task operations from agent MCP calls
│   ├── db/
│   │   └── pg.py                # Async PostgreSQL via asyncpg (multi-tenant schema)
│   ├── auth/                    # Auth providers & permissions
│   │   └── permissions.py       # AgentPermissions: data_scope, task_schedule, etc.
│   └── security/                # Credential proxy, mount validation, sender allowlist
│       ├── credential_proxy.py  # HTTP proxy injecting real API keys (containers never see secrets)
│       └── mount_security.py    # Validates container volume mounts
├── agent_runner/                # Runs inside Docker containers
│   ├── main.py                  # Reads NATS KV init, runs claude_agent_sdk.query() loop
│   └── ipc_mcp.py              # In-process MCP server (send_message, schedule_task, etc.)
└── webui/                       # FastAPI web server
    ├── main.py                  # App factory, static files, CORS
    ├── ws.py                    # WebSocket handler (auth, NATS subscriptions, broadcast)
    ├── auth.py                  # OIDC PKCE, JWT, builtin auth modes
    └── admin.py                 # RESTful admin API (CRUD for coworkers, conversations, etc.)
```

## Web Frontend

Located in `web/`, built with **Lit 3.3** web components + **Vite** + **Tailwind CSS 4** + **TypeScript**.

Key components:
- `rm-app` — Root: auth state machine (loading → login → authenticated)
- `rm-chat-panel` — Chat interface, WebSocket connection management
- `rm-sidebar` — Conversation list, new chat
- `rm-login-page` — OIDC login

WebSocket protocol messages: `session`, `thinking`, `text` (streaming), `done`, `error`.

## Key Design Patterns

- **Multi-tenancy**: All data keyed by `tenant_id`; queries filtered by user's tenant
- **3-level concurrency**: global limit → per-tenant `max_concurrent_containers` → per-coworker default
- **Credential proxy**: Containers call `ANTHROPIC_BASE_URL` (proxy at port 3001) which injects real API keys; containers never see secrets
- **Permission model**: `AgentPermissions` with 4 fields: `data_scope`, `task_schedule`, `task_manage_others`, `agent_delegate`
- **NATS IPC channels per job**: `agent.{JOB_ID}.results`, `.input`, `.messages`, `.tasks`, `.close`
- **Session resumption**: Agent runner maintains Claude session IDs across multi-turn conversations

## Development

```bash
# Prerequisites: Python 3.12+, Docker, uv

# Start infrastructure
docker compose -f docker-compose.dev.yml up -d   # NATS + PostgreSQL

# Install dependencies
uv sync --all-extras

# Run tests
uv run pytest

# Lint & format
uv run ruff check src/ tests/
uv run ruff format src/ tests/

# Type check
uv run mypy src/

# Build agent container
cd container && ./build.sh
```

## Testing

- **Framework**: pytest + pytest-asyncio (auto mode)
- **Database**: Real PostgreSQL via testcontainers (session-scoped, fresh DB per test)
- **Coverage threshold**: 80% (`uv run pytest --cov`)
- **Test categories**: E2E flows, auth/OIDC, database CRUD, orchestration routing, container specs, IPC protocol, security, core config
- **Key fixture pattern**: `conftest.py` provides `pg` (PostgreSQL pool), `sample_tenant`, `sample_coworker`, `sample_conversation`

## Entry Points

- `rolemesh` CLI → `src/rolemesh/main.py:main_sync` (orchestrator)
- `rolemesh-webui` CLI → `src/webui/main.py:main` (web server)
- Agent container → `python -m agent_runner` (in Docker)

## Dependencies

Core: `structlog`, `aiohttp`, `python-telegram-bot`, `slack-bolt`, `nats-py`, `aiodocker`, `asyncpg`, `fastapi`, `uvicorn`, `PyJWT[crypto]`, `httpx`, `cryptography`, `croniter`

Dev: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`, `testcontainers[postgres]`

## Configuration

All config via environment variables (see `src/rolemesh/core/config.py`):
- `NATS_URL` — NATS server address
- `DATABASE_URL` — PostgreSQL connection string
- `ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN` — Claude credentials (injected by proxy)
- `AUTH_MODE` — "external", "oidc", or "builtin"
- `OIDC_*` — OIDC provider settings (issuer, client_id, etc.)
