# RoleMesh

> Multi-tenant agent platform with enterprise-grade safety.

Run AI coworkers on your own infrastructure: each tenant gets sandboxed Claude or Pi agents, reachable from any team channel, governed by a real safety pipeline.

[![CI](https://github.com/jerryguan777/rolemesh/actions/workflows/ci.yml/badge.svg)](https://github.com/jerryguan777/rolemesh/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.12%2B-blue)
[![Last commit](https://img.shields.io/github/last-commit/jerryguan777/rolemesh)](https://github.com/jerryguan777/rolemesh/commits)

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

## Why RoleMesh

Most agent platforms today either run as a closed SaaS (Claude Projects, Devin) or as a single-tenant library (LangChain, AutoGPT, CrewAI). Neither fits when an AI coworker has to handle real company data, talk in your team channels, and not exfiltrate credentials.

RoleMesh is built for that gap:

- **Self-hosted**, AGPL-licensed.
- **Multi-tenant from the database up** — Postgres Row-Level Security on every tenant-scoped table, dual-pool fail-closed default.
- **Sandboxed by architecture, not by bolt-on** — container hardening, isolated agent network, gateway-only egress, three-stage safety pipeline.
- **Two interchangeable agent runtimes** — Claude SDK or Pi (open-source, model-agnostic).

---

## Features

| | Feature | What it gives you |
|---|---|---|
| 🤖 | **Two agent runtimes** | Claude SDK (proven by Claude Code) or [Pi](https://github.com/earendil-works/pi) (model-agnostic: Anthropic, OpenAI, Gemini, Bedrock). Swap per-coworker or globally. |
| 🔒 | **Enterprise-grade safety** | Container hardening, network isolation, a three-stage policy engine, and a host-side credential proxy — on by default. |
| 🏢 | **Multi-tenant isolation** | Postgres Row-Level Security on every tenant-scoped table; dual-pool architecture gives fail-closed isolation by default. |
| 💬 | **Interactive channels** | Web chat (FastAPI + WebSocket), Telegram, and Slack. Microsoft Teams planned. |
| 🧩 | **Per-coworker skills** | Markdown capability folders the agent auto-invokes; DB-backed, tenant-scoped, projected read-only per spawn. |
| 🛎️ | **Frontdesk** | One user-facing entry point per tenant that delegates to specialist agents (accounting / portfolio / trading / …). |
| 📊 | **Evaluation framework** | `rolemesh-eval` CLI to measure how coworker behavior changes across prompts, tools, skills, backends, and models. |

Deeper write-ups live under [Skills](#skills), [Evaluation](#evaluation), [Safety model](#safety-model), and [Documentation](#documentation).

---

## Quick Start

**Prerequisites:** Docker (plus Python 3.12+ and [uv](https://github.com/astral-sh/uv) for development and tests).

Everything runs as containers — infrastructure (Postgres, NATS, egress gateway, egress-control bridges) and application services (orchestrator, WebUI) are all declared in `deploy/compose/compose.yaml`.

```bash
# 1. Clone
git clone https://github.com/jerryguan777/rolemesh.git
cd rolemesh

# 2. Configure the minimum required keys (see Configuration for the rest)
echo "DOCKER_GID=$(getent group docker | cut -d: -f3)" >> .env   # docker.sock access
echo "ROLEMESH_HOST_DATA_DIR=$PWD/data" >> .env                  # absolute host path
echo "WS_TICKET_SECRET=$(openssl rand -hex 32)" >> .env          # required to boot
echo "CREDENTIAL_VAULT_KEY=$(openssl rand -hex 32)" >> .env      # required to boot
echo "EGRESS_TOKEN_SECRET=$(openssl rand -hex 32)" >> .env       # required to boot

# 3. Build the agent image, then build + start the full stack
container/build.sh
docker compose --env-file .env -f deploy/compose/compose.yaml up -d --build
```

Open <http://localhost:8080> in your browser. Then [seed your first administrator](#seeding-the-first-administrator) and add an [LLM provider key](#llm-provider-pick-at-least-one).

> For a dev login without an IdP, also set `BOOTSTRAP_USERS`, then open the WebUI with `?token=...`.

<details>
<summary><b>Deployment details: DooD path translation, fail-closed startup, hot reload, debugpy, one-off CLI</b></summary>

There is no host-process mode for the orchestrator; it always runs as a container (see `docs/21-container-runtime-decoupling.md` §1).

**Two deployment keys explained:**

- `DOCKER_GID` — the orchestrator container runs as non-root UID 1000 and gets `docker.sock` access via this supplemental group.
- `ROLEMESH_HOST_DATA_DIR` — host path of the repo's `data/` directory (DooD path translation: the orchestrator sees `/app/data`, but bind sources for agent sandboxes are resolved by the **host** dockerd). Must be absolute — compose interpolates in the caller's environment.

`--env-file` matters: the project directory is `deploy/compose`, so the repo-root `.env` is not picked up automatically.

**Fail-closed startup.** The orchestrator verifies the declared infrastructure (networks, gateway IP/healthz, NATS) and runs a DooD loopback self-check — it writes a sentinel under `data/`, binds the translated host path into a one-shot probe container and reads it back. A misconfigured `ROLEMESH_HOST_DATA_DIR` refuses to start instead of silently spawning agents on empty directories.

**Development conveniences** (wrappers in `container/orchestrator-entrypoint.sh`):

- **Hot reload** — on by default in compose: `../../src` is bind-mounted over `/app/src` and the entrypoint wraps the orchestrator in `watchfiles` (`ROLEMESH_RELOAD=1`), restarting it on source changes. Set `ROLEMESH_RELOAD=0` in `.env` to disable.
- **debugpy** — set `DEBUGPY=1` in `.env` (or `DEBUGPY=1 docker compose ... up orchestrator`) and attach your IDE to `localhost:5678`. The listener is only bound to the host loopback. Tip: combined with reload, the debug session dies on each restart; for long debugging sessions set `ROLEMESH_RELOAD=0`.

**One-off CLI runs** reuse the orchestrator image and override its command:

```bash
docker compose --env-file .env -f deploy/compose/compose.yaml \
    run --rm orchestrator rolemesh-eval --help
```

**Operator mount allowlist** is bind-mounted read-only from `~/.config/rolemesh` (override the host dir with `ROLEMESH_CONFIG_DIR` in `.env`). Note: if the directory does not exist, docker creates it root-owned on the host.

</details>

---

## Configuration

Create a `.env` file at the project root. The [Quick Start](#quick-start) sets the bare minimum; the tables below list the most useful keys, grouped by purpose.

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

Channel credentials (Telegram / Slack / web) are **stored per coworker** in the `channel_bindings` DB table — they are not read from `.env`. Use the Settings → Coworker → Channels UI (or `POST /api/v1/coworkers/{id}/bindings`) to bind a coworker to a channel with its bot tokens.

<details>
<summary><b>Storage, secrets, egress, and other keys</b></summary>

### Storage

| Key                  | Purpose                                                          |
|----------------------|------------------------------------------------------------------|
| `DATABASE_URL`       | App-pool URL (`rolemesh_app` role, NOBYPASSRLS).                 |
| `ADMIN_DATABASE_URL` | Admin-pool URL for migrations and system-level tasks.            |
| `NATS_URL`           | Defaults to `nats://localhost:4222`.                             |

### Misc

| Key                          | Purpose                                                                  |
|------------------------------|--------------------------------------------------------------------------|
| `ASSISTANT_NAME`             | Display name for your AI coworker.                                       |
| `CONTAINER_NETWORK_NAME`     | Name of the EC agent bridge (Internal=true). Defaults to `rolemesh-agent-net` and must match the compose-declared network; must be non-empty (empty → startup error). |
| `CREDENTIAL_VAULT_KEY`       | **Required.** Master secret for the per-tenant LLM credential vault; hashed into a Fernet key, so any non-empty string works (use a long random value in production). The orchestrator AND the webui refuse to boot without it. |
| `EGRESS_DNS_ALLOWLIST`       | Platform-wide DNS allowlist for the gateway resolver (comma-separated, `exact` or `*.suffix`). Default **empty** — proxied traffic never needs agent-side DNS, so the resolver is a tripwire; see `docs/16-egress-control-architecture.md`. |
| `EGRESS_DNS_MODE`            | `enforce` (default: non-matching names get NXDOMAIN) or `observe` (resolve everything, log would-be blocks; migration aid only). |
| `EGRESS_TOKEN_SECRET`        | **Required.** Shared HMAC secret (≥16 chars) used to sign and verify agent identity tokens; the orchestrator signs, the gateway verifies. Never injected into agent containers. Missing → refuse to boot. |
| `EGRESS_TOKEN_TTL_SECONDS`   | Max lifetime of an identity token (and thus an agent container before the orchestrator re-mints). Default 7 days. |
| `ROLEMESH_ENV`               | `development` (default) or `production`. See note below.                 |
| `ROLEMESH_SEED_ADMIN_EMAIL`  | If set, the WebUI seeds a `platform_admin` with this email at startup.   |
| `WS_TICKET_SECRET`           | **Required.** Dedicated signing key for WebSocket handshake tickets.     |
| `BOOTSTRAP_USERS`            | Dev/test only: JSON token→user map for multi-identity without an IdP. Aborts startup under `ROLEMESH_ENV=production`. |
| `ROLEMESH_AGENT_BACKEND`     | `claude` (default) or `pi`.                                              |

</details>

### Seeding the first administrator

On a fresh deployment nobody can log in yet, so the very first `platform_admin` is seeded out-of-band. Run the CLI against the same database (the schema is created idempotently on connect):

```bash
uv run rolemesh-admin create-admin --email you@example.com
```

This writes one privileged user row through the admin (BYPASSRLS) connection and is idempotent — re-running it is a no-op. The email is the identifier your IdP is matched against on login, **not** a credential; authentication still runs through the IdP. Pass `--external-sub` if you already know the IdP subject, otherwise the row is linked on the first OIDC login matching the email.

For managed / IaC deploys, set `ROLEMESH_SEED_ADMIN_EMAIL` (plus optional `ROLEMESH_SEED_ADMIN_EXTERNAL_SUB` / `ROLEMESH_SEED_ADMIN_NAME`) and the WebUI seeds the same `platform_admin` at startup. Further admins and tenant owners are created from the platform_admin UI/API (or OIDC JIT) — the CLI is only for platform genesis and emergency recovery.

> **Production hardening.** When `ROLEMESH_ENV=production`, a populated `BOOTSTRAP_USERS` aborts startup; it remains available in `development` for local multi-identity testing. There is no static owner token — the first admin is seeded out-of-band (above). In `external` auth mode the JWT `user-id` claim must be a valid UUID (non-UUID claims are rejected at the auth boundary), and `WS_TICKET_SECRET` must be set explicitly.

See `docs/6-auth-architecture.md` for the full auth model.

---

## Agent backends

Two interchangeable runtimes; configurable per-coworker or globally via `ROLEMESH_AGENT_BACKEND`.

| Backend     | Models                                                | Best for                                                    |
|-------------|-------------------------------------------------------|-------------------------------------------------------------|
| **Claude**  | Anthropic                                             | Standard Claude Code workflows; OAuth Max subscription.     |
| **Pi**      | Anthropic, OpenAI, Google Gemini, AWS Bedrock         | Multi-provider, on-prem Bedrock, model-agnostic strategies. |

Pi is ported from [pi-mono](https://github.com/earendil-works/pi) (the runtime behind OpenClaw). Switch backends by setting the keys in `.env` and restarting the orchestrator service:

```bash
echo "ROLEMESH_AGENT_BACKEND=pi" >> .env
echo "PI_MODEL_ID=amazon-bedrock/us.anthropic.claude-sonnet-4-6" >> .env
docker compose --env-file .env -f deploy/compose/compose.yaml up -d orchestrator
```

Details: `docs/8-switchable-agent-backend.md`.

---

## Channels

| Channel  | Setup                                                                                                |
|----------|------------------------------------------------------------------------------------------------------|
| WebUI    | `webui` compose service (defaults to port 8080).                                                     |
| Telegram | Bind a coworker via Settings → Channels with a Telegram bot token; the gateway hot-loads on bind.   |
| Slack    | Bind a coworker via Settings → Channels with `bot_token` + `app_token`.                              |
| Teams    | Planned.                                                                                             |

---

## Skills

Per-coworker skill folders (a `SKILL.md` plus optional supporting files) are read by the agent on demand — the model auto-invokes a skill based on its frontmatter `description`, with no slash commands and no human-side wiring. Skills are DB-backed and tenant-scoped: they live in Postgres with RLS, are projected per-spawn into a read-only bind mount, and are never shared across tenants. Backend-aware frontmatter lets you write a skill once and project it to either Claude SDK (`/home/agent/.claude/skills`) or Pi (`/home/agent/.pi/skills`).

They are managed via the admin REST API:

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

Architecture: `docs/19-skills-architecture.md`.

---

## Evaluation

`rolemesh-eval` is an [Inspect AI](https://inspect.ai-safety-institute.org.uk/) based, manual / nightly tool for measuring how coworker behavior changes across `system_prompt` / `tools` / `skills` / `agent_backend` / `model` configurations. Four orthogonal scorers — `final_answer` (exact / regex / LLM-judge), `tool_trace` (required / forbidden / expected order), `routing_accuracy` (frontdesk delegate-target check), and `cost` (per-sample latency + token spend). It reuses the production `ContainerAgentExecutor`, so eval runs the same code path that handles real traffic.

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

Run it as a one-off container on the orchestrator image (see [Quick Start](#quick-start)). The compose infrastructure (Postgres, NATS, egress gateway) must be up; the orchestrator service itself need not be running. Architecture: `docs/20-evaluation-architecture.md`.

---

## Safety model

Three layers, all enabled by default. Overview in `docs/13-safety-overview.md`.

1. **Container layer** — readonly rootfs, dropped capabilities, user-namespace remap, optional gVisor. See `docs/14-container-hardening-architecture.md`.
2. **Network layer** — Internal-only agent bridge plus a dual-homed egress gateway. Per-tenant DNS allowlist, reverse-proxy credential injection, no direct internet from agents. See `docs/16-egress-control-architecture.md`.
3. **Policy layer** — three-stage hooks (`INPUT_PROMPT`, `PRE_TOOL_CALL`, `MODEL_OUTPUT`) with built-in checks plus optional ML-backed detectors (`presidio`, `llm-guard`, `detect-secrets`) via the `safety-ml` extra. See `docs/15-safety-framework-architecture.md`.

A **credential proxy** backs all three: real API keys live only on the host. Agent containers see placeholders; the proxy rewrites the `Authorization` header at the HTTP layer.

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

Contribution flow and coding standards: [`CONTRIBUTING.md`](CONTRIBUTING.md).

---

## Documentation

Most documents have a Chinese counterpart with a `-cn` suffix (e.g. `docs/4-multi-tenant-architecture-cn.md`).

| Topic                            | File                                                  |
|----------------------------------|-------------------------------------------------------|
| Overall architecture             | `docs/1-overall-architecture.md`                      |
| Multi-tenant architecture        | `docs/4-multi-tenant-architecture.md`                 |
| RLS (row-level security)         | `docs/18-rls-architecture.md`                         |
| Auth and permissions             | `docs/6-auth-architecture.md`                         |
| Safety overview                  | `docs/13-safety-overview.md`                          |
| Safety framework                 | `docs/15-safety-framework-architecture.md`            |
| Container hardening              | `docs/14-container-hardening-architecture.md`         |
| Egress control                   | `docs/16-egress-control-architecture.md`              |
| Attack-simulation matrix         | `docs/17-attack-simulation-matrix.md`                 |
| Hooks architecture               | `docs/9-hooks-architecture.md`                        |
| HITL approval                    | `docs/12-hitl-approval-architecture.md`               |
| NATS IPC                         | `docs/2-nats-ipc-architecture.md`                     |
| Agent executor and runtime       | `docs/3-agent-executor-and-container-runtime.md`      |
| Container runtime decoupling     | `docs/21-container-runtime-decoupling.md`             |
| Switchable agent backend         | `docs/8-switchable-agent-backend.md`                  |
| WebUI architecture               | `docs/5-webui-architecture.md`                        |
| Event-stream architecture        | `docs/10-event-stream-architecture.md`                |
| Steering architecture            | `docs/11-steering-architecture.md`                    |
| External MCP                     | `docs/7-external-mcp-architecture.md`                 |
| Frontdesk architecture           | `docs/frontdesk-architecture.md`                      |
| Evaluation architecture          | `docs/20-evaluation-architecture.md`                  |
| Production deploy (RKE2)          | `docs/22-rke2-production-deploy.md`                    |

---

## Contributing

Contributions are welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the development setup, testing, and PR flow. Please report security issues privately — see [`SECURITY.md`](SECURITY.md).

---

## License

[AGPL-3.0-or-later](LICENSE). See [`NOTICE`](NOTICE) for attribution.
