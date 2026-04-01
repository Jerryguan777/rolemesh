# RoleMesh - AI Coworker Platform

## Overview

RoleMesh is a multi-tenant orchestration platform for AI-powered chatbot assistants ("coworkers"). It runs Claude AI agents as containerized workloads, routes messages across Telegram and Slack, and provides secure credential isolation via an HTTP proxy.

**License**: AGPL-3.0-or-later  
**Python**: >= 3.12  
**Entry point**: `rolemesh.main:main_sync`

## Architecture

```
User (Telegram/Slack)
  │
  ▼
ChannelGateway (telegram_gateway / slack_gateway)
  │
  ▼
Orchestrator (main.py)
  ├── OrchestratorState (runtime state, concurrency gates)
  ├── GroupQueue / Scheduler (container/scheduler.py)
  ├── TaskScheduler (orchestration/task_scheduler.py)
  └── CredentialProxy (security/credential_proxy.py)
        │
        ▼
ContainerRuntime (Docker) ──► Agent Container
  │                              │
  └── NATS IPC ◄─────────────────┘
        (JetStream + KV)
```

### Core Layers

| Layer | Path | Purpose |
|-------|------|---------|
| **Core** | `src/rolemesh/core/` | Config, types, state, env, timezone, logging |
| **Agent** | `src/rolemesh/agent/` | Agent execution protocol & container executor |
| **Orchestration** | `src/rolemesh/orchestration/` | Message routing, task scheduling, remote control |
| **Container** | `src/rolemesh/container/` | Docker runtime, volume mounts, per-group queues |
| **IPC** | `src/rolemesh/ipc/` | NATS JetStream transport, protocol types, task handler |
| **Channels** | `src/rolemesh/channels/` | Telegram & Slack gateway abstractions |
| **Security** | `src/rolemesh/security/` | Credential proxy, mount allowlist, sender allowlist |
| **DB** | `src/rolemesh/db/` | PostgreSQL via asyncpg (multi-tenant schema) |
| **Agent Runner** | `src/agent_runner/` | In-container process that invokes Claude Agent SDK |

## Key Concepts

- **Tenant**: Organization/workspace with its own concurrency limits
- **Coworker**: An AI agent with a name, folder, system prompt, tools, and skills
- **ChannelBinding**: Bot credentials linking a coworker to a Telegram/Slack bot
- **Conversation**: A chat context (coworker + channel binding + chat_id)
- **ScheduledTask**: Cron/interval/once tasks executed by coworkers
- **GroupQueue**: Per-coworker message/task queue with 3-level concurrency control

## Multi-Tenancy & Concurrency

Three-level concurrency gates:
1. **Global**: `GLOBAL_MAX_CONTAINERS` (env)
2. **Per-tenant**: `Tenant.max_concurrent_containers` (DB)
3. **Per-coworker**: `Coworker.max_concurrent` (DB)

Managed in `OrchestratorState` and enforced in `GroupQueue`.

## IPC (NATS)

- **Orchestrator -> Agent**: KV bucket `agent-init` (initial input), JetStream `agent.{job_id}.input` (follow-up messages)
- **Agent -> Orchestrator**: JetStream `agent.{job_id}.results`, `.messages`, `.tasks`
- Stream: `agent-ipc` (1-hour retention)
- KV buckets: `agent-init`, `snapshots` (1-hour TTL)

## Security Model

- `.env` secrets read via `env.py` without loading into `os.environ`
- Credential proxy intercepts container -> Anthropic API calls (API-key or OAuth mode)
- Mount allowlist external to project: `~/.config/rolemesh/mount-allowlist.json`
- Sender allowlist: `~/.config/rolemesh/sender-allowlist.json`
- Sensitive directories blocked by default (.ssh, .gnupg, .aws, etc.)

## Database (PostgreSQL)

Multi-tenant schema with tables: `tenants`, `users`, `coworkers`, `channel_bindings`, `conversations`, `sessions`, `messages`, `scheduled_tasks`, `task_run_logs`.

Connection pool: asyncpg (min=2, max=10).

## Dependencies

- `structlog` - structured logging
- `aiohttp` - HTTP (credential proxy)
- `python-telegram-bot` - Telegram gateway
- `slack-bolt` - Slack gateway
- `nats-py` - NATS JetStream IPC
- `aiodocker` - Docker container management
- `asyncpg` - PostgreSQL
- `croniter` - Cron expression parsing

## Development

```bash
# Install dependencies
uv sync --dev

# Run tests
uv run pytest

# Run tests with coverage
uv run pytest --cov

# Lint
uv run ruff check src tests

# Type check
uv run mypy src
```

### Test Configuration
- `pytest-asyncio` with `asyncio_mode = "auto"`
- Coverage threshold: 80% (`fail_under = 80`)
- Coverage omits: main.py, IPC transport, channel gateways, container runtime, credential proxy

## Message Flow

1. User sends message via Telegram/Slack
2. Gateway calls `_handle_incoming()` callback
3. Orchestrator finds conversation by binding + chat_id
4. Trigger pattern check (group chats) + sender allowlist validation
5. Message stored in DB
6. Enqueued to GroupQueue (concurrency gated)
7. Messages formatted as XML with context
8. ContainerAgentExecutor launches Docker container
9. Agent reads initial input from NATS KV, runs Claude Agent SDK
10. Results streamed back via JetStream, forwarded to user

## Agent Backends

Pre-configured in `executor.py`:
- `CLAUDE_CODE_BACKEND`: Claude Code agent
- `PIMONO_BACKEND`: PiMono agent

Backend config: image, entrypoint, extra mounts, skip_claude_session flag.
