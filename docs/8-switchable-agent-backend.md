# Switchable Agent Backend Architecture

This document describes how RoleMesh supports multiple agent backends (Claude SDK and Pi) behind a single NATS IPC protocol, so each coworker can run on a different LLM framework without the orchestrator knowing which one. It covers the design goals, the abstractions introduced, the tradeoffs considered, and the pitfalls encountered during implementation.

Target audience: developers who want to add a third backend, understand the credential flow across providers, or debug why the Pi backend behaves differently from Claude.

## Background: Why Two Backends?

The original design ran only the Claude SDK inside the agent container. Claude SDK is excellent for general-purpose coding with first-class support for MCP, skills, and subagents. But locking in one framework is risky:

- **Vendor lock-in**: Anthropic API costs, feature roadmap, rate limits all become single points of failure.
- **Model diversity**: Some tasks are cheaper or better on OpenAI, Gemini, or open-source models. Claude SDK only speaks to Anthropic.
- **Framework diversity**: Different agent frameworks have different strengths (Pi has cleaner streaming events, better session/fork semantics, simpler tool model).

We needed a way to run different agent frameworks inside containers without rewriting the orchestrator, the NATS protocol, the IPC tools, or the channel gateways. Whichever framework is inside the container should be invisible to the host.

## Design Goals

1. **Per-coworker selection**: Each coworker chooses its backend in the database (`coworkers.agent_backend` column). Default to `"claude"`.
2. **Single Docker image**: One image, one entrypoint, one build pipeline. Backend is selected at runtime by an env var.
3. **Host-side transparency**: The orchestrator doesn't know or care which backend runs inside a container. All IPC stays on the existing NATS subjects.
4. **Shared tool logic**: The IPC tools (`send_message`, `schedule_task`, `pause_task`, …) are written once and adapted per-backend.
5. **Provider-neutral credential management**: The credential proxy handles auth for Anthropic, OpenAI, Google, Bedrock. Containers never see real API keys.

## The Abstraction: AgentBackend Protocol

Inside the container, `agent_runner` is split into two parts:

1. **NATS bridge** (`main.py`): reads `AgentInitData` from KV, subscribes to input / interrupt / shutdown subjects, publishes results / messages / tasks back to the orchestrator. Backend-agnostic.
2. **Backend** (`claude_backend.py` or `pi_backend.py`): wraps the specific SDK. Implements `AgentBackend` protocol.

```
             ┌──────────────────────────────────────┐
             │  NATS Bridge (main.py)               │
             │  reads AgentInitData from KV,        │
             │  subscribes input / interrupt,       │
             │  handles shutdown request-reply,     │
             │  publishes results / messages / ...  │
             └──────────────┬───────────────────────┘
                            │ BackendEvent
                            ▼
             ┌──────────────────────────────────────┐
             │  AgentBackend Protocol               │
             │  start(init, ctx, mcp_servers)       │
             │  run_prompt(text)                    │
             │  handle_follow_up(text)              │
             │  abort()      — stop current turn    │
             │  shutdown()   — close container      │
             │  subscribe(listener)                 │
             └────┬──────────────────────┬──────────┘
                  │                      │
       ┌──────────▼──────────┐  ┌────────▼────────────┐
       │  ClaudeBackend      │  │  PiBackend          │
       │  wraps              │  │  wraps              │
       │  claude_agent_sdk   │  │  pi.AgentSession    │
       └─────────────────────┘  └─────────────────────┘
```

### BackendEvent Types

The bridge listens to backend events via `backend.subscribe(listener)`:

| Event | Purpose |
|-------|---------|
| `ResultEvent(text, usage, new_session_id)` | Final (or intermediate) assistant output. Bridge publishes it on `agent.{job}.results`. |
| `SessionInitEvent(session_id)` | Backend has established a session ID. Bridge updates its tracker. |
| `CompactionEvent` | Backend is about to compact context (archival hooks fire). |
| `ErrorEvent(error, usage)` | Unrecoverable backend error. Bridge publishes error status. |
| `RunningEvent(stage)` | Progress signal for the WebUI ("running", "container_starting", etc.). See [`event-stream-architecture.md`](event-stream-architecture.md). |
| `ToolUseEvent(tool_name, input_preview)` | A tool call is about to fire. Surfaced to the WebUI as a "tool_use" status. |
| `StoppedEvent(usage)` | The agent has acknowledged a stop and is idle. The orchestrator uses this as the real "agent acked the abort" signal — see [`backend-stop-contract.md`](backend-stop-contract.md). |
| `SafetyBlockEvent(reason, usage)` | Safety pipeline blocked the turn. Kept distinct from `ResultEvent` so the orchestrator can flag the message correctly for audit. |

Backends emit events; the bridge translates them to NATS messages. The backend never touches NATS directly.

### `abort()` vs `shutdown()`

Two cancellation methods with distinct contracts:

- **`abort()`** — stop the **current turn** but keep the container alive. Used by the WebUI Stop button (the user can immediately redirect with a follow-up; no cold-start penalty).
- **`shutdown()`** — close the **container itself**. Used by the orchestrator for idle timeout, preemption, or scheduler-driven cleanup.

The two backends implement these differently (Claude uses preemptive `Task.cancel()`; Pi uses cooperative `asyncio.Event` checks between provider stream chunks), but both must deliver the same observable behaviors regardless of internal mechanics. The full contract — "no late events for the aborted turn", "no leaked aborted-context into the next turn", "no latent `_aborting` flag gagging future follow-ups" — lives in [`backend-stop-contract.md`](backend-stop-contract.md). That contract is the load-bearing piece for swapping backends: if a new backend honors it, the orchestrator doesn't need to know which backend is running.

### Why Not "Different Containers for Different Backends"?

Rejected because:

- Maintaining two Dockerfiles doubles the build/CI surface.
- The NATS bridge code is 100% shared; duplicating it into two images invites drift.
- Per-coworker backend selection means both backends must be available at image pull time.

A single image with runtime dispatch via `AGENT_BACKEND=claude|pi` env var is much simpler. `__main__.py` reads the env var and imports the right backend class.

## Host-Side Dispatch

The host maintains one `ContainerAgentExecutor` per backend in an `_executors` dict, keyed by backend name. The orchestrator looks up `_executors[coworker.agent_backend]` at dispatch time. Both executors point to the same Docker image — only `extra_env` (notably `AGENT_BACKEND=claude|pi`) and a few volume-mount toggles (e.g. `skip_claude_session`) differ.

The full design — per-coworker dispatch, the single-image rationale, the `BACKEND_CONFIGS` map — lives in [`3-agent-executor-and-container-runtime.md`](3-agent-executor-and-container-runtime.md). What matters here is that **the host side carries no backend-specific logic** beyond picking the right executor.

## Shared Tool Logic

The IPC tools (`send_message`, `schedule_task`, `pause_task`, `resume_task`, `cancel_task`, `update_task`, `list_tasks`) are pure async functions in `agent_runner/tools/rolemesh_tools.py`:

```python
async def send_message(args: dict, ctx: ToolContext) -> ToolResult: ...
async def schedule_task(args: dict, ctx: ToolContext) -> ToolResult: ...
# ...
```

`ToolContext` carries what the tools need (NATS JetStream client, `job_id`, tenant / coworker IDs, permissions). Each backend has a thin adapter:

- **`tools/claude_adapter.py`** — wraps each function with Claude SDK's `@tool` decorator and registers them as an in-process MCP server via `create_sdk_mcp_server("rolemesh", ...)`.
- **`tools/pi_adapter.py`** — wraps each function as a `pi.agent.types.AgentTool` subclass.

The tool business logic (validation, NATS publish format, permission checks, dedup with the results stream) lives in exactly one place. Backend adapters only translate signatures and result formats.

The full per-tool wire format — subject, payload shape, orchestrator-side authorization — lives in [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) (Channel 4 messages, Channel 5 task ops, Channel 6 snapshot reads).

### Design Tradeoff: Why Not a Unified Tool Interface in Pi?

Pi has its own `AgentTool` ABC, and Claude SDK has its own `@tool` decorator. We could have forced Pi to use Claude's format (or vice versa) and avoided the adapters. We didn't, because:

- Each framework has framework-specific features in its tool interface (Claude SDK has MCP namespacing, Pi has `AgentToolResult.details` for UI).
- Forcing a lowest-common-denominator interface would leak framework details into the other backend.
- The adapter code is ~60 lines per backend. Cheap to maintain.

## MCP Integration in Pi

Claude SDK has built-in MCP client support. Pi does not. Rather than adding MCP support to Pi's core (invasive, forks from upstream), we added `src/pi/mcp/` as a sidecar module:

- `pi.mcp.client.McpServerConnection` — manages one MCP server connection (SSE or streamable-HTTP transport).
- `pi.mcp.tool_bridge.load_mcp_tools(specs, user_id)` — connects to servers, discovers tools, wraps each as a `pi.agent.types.AgentTool`.

Pi's agent loop sees remote MCP tools as indistinguishable from local tools. The wrapper's `execute()` forwards the call over the wire. The wire format (URL rewriting, `auth_mode`, per-user token injection) is described in [`7-external-mcp-architecture.md`](7-external-mcp-architecture.md); this section is only about how the Pi backend plugs into that.

### Why Sidecar Instead of Pi Extension?

Pi has an extension system (`ExtensionAPI.register_tool(ToolDefinition)`) that would have seemed like a natural fit. We rejected it because:

- Pi extensions are filesystem-discovered (Python files with factory functions). MCP server configs come from NATS `AgentInitData`, not the filesystem.
- Pi's extension system is designed for user plugins, not framework-level infrastructure. Semantic mismatch.
- Extension lifecycle has no proper shutdown hook for closing MCP connections.

The sidecar approach keeps MCP concerns self-contained and lets us track Pi upstream without conflicts.

### Deprecated SDK Function Trap

The `mcp` Python SDK 1.27 exports two functions from `mcp.client.streamable_http`:

- `streamablehttp_client(url, headers=...)` — accepts headers but is `@deprecated`.
- `streamable_http_client(url, http_client=...)` — recommended, does NOT accept headers.

We use the non-deprecated function and inject headers via a custom `httpx.AsyncClient`. If you see `unexpected keyword argument 'headers'`, check which function you're calling.

## Credential Flow Across Backends

The credential proxy sits in front of every outbound LLM API call. Containers always get placeholder API keys; the proxy injects the real keys at the HTTP layer. The proxy distinguishes providers by path prefix (`/proxy/openai/...`, `/proxy/google/...`, `/v1/...` for legacy Anthropic). Each LLM SDK reads its own `*_BASE_URL` env var pointed at the proxy.

Adding a new provider means adding one entry to the proxy's provider registry — not changing the agent backends. The detailed proxy mechanics (route layout, per-provider config, MCP forwarding, `auth_mode`) live in [`7-external-mcp-architecture.md`](7-external-mcp-architecture.md); the per-user IdP token model lives in [`6-auth-architecture.md`](6-auth-architecture.md).

What matters for backend swapping: **each backend's SDK only needs the right `*_BASE_URL` and a placeholder key in its env**. Whether the backend talks to Anthropic, OpenAI, or Bedrock, the same path through the proxy applies.

## Pi-Specific Pitfalls

Integrating Pi uncovered several issues that are worth documenting so the next person doesn't repeat the debugging.

### 1. Providers aren't auto-registered

Pi's LLM providers (Anthropic, OpenAI, Google) live in a registry but aren't populated by default. `register_built_in_api_providers()` must be called explicitly before the first LLM call. If you forget, `stream()` raises `ValueError: No API provider registered for api: anthropic-messages` — but Pi's error handling catches it and stores it as `assistant_message.error_message`, so the outer code sees "query finished successfully with no output."

We call `register_built_in_api_providers()` at the top of `PiBackend.start()`.

### 2. `custom_tools` is a stub in Pi's SDK

`CreateAgentSessionOptions.custom_tools` is stored on the session config but never passed to `agent.set_tools()`. Pi's agent state starts with empty tools — the LLM sees nothing, no matter what you pass to `custom_tools`.

This is a known gap in Pi's Python port (the TypeScript original wires tools through the interactive mode's extension system). Our patch in `pi/coding_agent/core/sdk.py` assembles built-in tools filtered by `initial_tool_names` plus `custom_tools` and sets them on the agent's initial state.

### 3. Multiple `TurnEndEvent`s per prompt

Pi emits a `TurnEndEvent` after every turn, including intermediate tool-call turns. If we emit a `ResultEvent` on each one, the host receives multiple "success" results per user message, which triggers `notify_idle` repeatedly and can cause scheduling races.

Fix: collect the last assistant text across all `TurnEndEvent`s, emit exactly one `ResultEvent` after `session.prompt()` returns.

### 4. JetStream ephemeral consumer redelivery

The original loop created a fresh `js.subscribe()` for every iteration. JetStream treats each `subscribe` call as a new ephemeral consumer with no memory of previous acks. Follow-up messages got redelivered to the new consumer every loop iteration, causing infinite processing loops.

Fix: subscribe once at the top of `run_query_loop()`, reuse across iterations, unsubscribe in the `finally` block.

Why Claude backend was unaffected: Claude SDK's `query()` iterator stays alive across prompts via the `MessageStream` push queue. The whole session is one iteration of the outer loop. Pi's `session.prompt()` returns after each turn, exposing the latent bug.

### 5. The `model.base_url` gotcha

Pi's model registry hardcodes `base_url="https://api.openai.com/v1"` on every OpenAI model. When Pi creates the SDK client:

```python
openai.AsyncOpenAI(api_key=key, base_url=model.base_url or None)
```

If `model.base_url` is set, the SDK ignores the `OPENAI_BASE_URL` env var. The request goes straight to OpenAI with a placeholder key and gets a 401.

**Fix**: In `pi_backend.py`, after resolving the model, override `model.base_url` with the proxy URL from the env var:

```python
_PROXY_ENV_MAP = {"openai": "OPENAI_BASE_URL", "anthropic": "ANTHROPIC_BASE_URL"}
proxy_env = _PROXY_ENV_MAP.get(model.provider)
if proxy_env and os.environ.get(proxy_env):
    model.base_url = os.environ[proxy_env]
```

This is a workaround for Pi's design choice; if/when Pi's providers read env vars natively, this can be removed.

## When to Add a Third Backend

The process:

1. Write `agent_runner/new_backend.py` implementing `AgentBackend` (including the abort/shutdown contract in [`backend-stop-contract.md`](backend-stop-contract.md)).
2. Write `agent_runner/tools/new_adapter.py` wrapping the shared tool functions.
3. Add `NEW_BACKEND = AgentBackendConfig(name="new", ...)` in `rolemesh/agent/executor.py`.
4. Register it in `BACKEND_CONFIGS`.
5. Add a provider config to the credential proxy's `_build_provider_registry()` if the new framework uses a different LLM API shape.
6. Add env-var injection in `rolemesh/container/runner.py` if the framework's SDK reads a different `*_BASE_URL` env var.

Things you do **not** need to change:

- NATS protocol
- Host orchestrator routing
- The shared IPC tools' business logic
- Channel gateways
- Database schema

## Testing Strategy

Backend integration is covered by three test layers:

1. **Unit tests** (`tests/test_agent_runner/test_rolemesh_tools.py`, `test_pi_adapter.py`, `test_event_translation.py`) — tool validation, event translation, adapter behavior. No NATS, no real LLM.
2. **NATS integration tests** (`tests/test_agent_runner/test_nats_bridge.py`) — real NATS server (from `docker-compose.dev.yml`), `FakeBackend` that simulates agent events. Validates the agent IPC subjects end-to-end.
3. **Manual E2E** — real channel + real LLM API calls. Use one coworker on Claude backend and another on Pi backend to compare behavior side-by-side.

The `FakeBackend` pattern is reusable: any new backend can be tested against the same NATS bridge tests by implementing `AgentBackend` with canned responses.

## Summary of Design Decisions

| Decision | Why |
|----------|-----|
| Single Docker image, env-var dispatch | Avoid duplicating NATS bridge code across images |
| `AgentBackend` protocol inside container | Keep backend logic isolated; bridge stays framework-agnostic |
| Shared tool functions + per-backend adapters | Tool business logic (validation, NATS format) lives once |
| Pi MCP as sidecar (`src/pi/mcp/`) | Avoid modifying Pi core; avoid extension-system semantic mismatch |
| Stop contract decoupled from implementation | Each backend can pick preemptive or cooperative cancellation as long as the contract holds |
| Per-coworker backend override | Production can run mixed backends for A/B testing or cost optimization |
