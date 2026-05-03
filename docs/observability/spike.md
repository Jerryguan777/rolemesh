# Observability Spike — OTel + Langfuse

Status: scaffold landed; end-to-end validation pending.

## Goal

Verify that the [OpenTelemetry](https://opentelemetry.io) + [Langfuse](https://langfuse.com) approach captures every step of an agent turn — orchestrator dispatch, container spawn, tool calls, LLM HTTP calls, token + cost — with **no changes to the credential proxy** and **zero impact on default deployments** (when the optional extra is not installed).

If the spike clears, this becomes the recommended path for the full observability feature; the alternative (custom `agent_traces` / `agent_spans` schema in Postgres) gets shelved.

## What's wired up

| Layer | Code | Status |
|-------|------|--------|
| Optional dep group `[observability]` (otel-sdk, otlp http exporter, openinference-anthropic) | `pyproject.toml` | done |
| Tracer wrapper that degrades to noop without the extra or the env var | `src/rolemesh/observability/tracer.py` | done |
| W3C trace-context carrier on `AgentInput` + `AgentInitData` | `src/rolemesh/agent/executor.py`, `src/rolemesh/ipc/protocol.py` | done |
| Orchestrator: install tracer + open per-turn `agent.turn` span + inject carrier | `src/rolemesh/main.py` | done |
| Agent runner: install tracer + attach parent context + auto-instrument anthropic | `src/agent_runner/main.py` | done |
| `TracingHookHandler` — backend-agnostic tool_call spans via HookRegistry | `src/agent_runner/hooks/handlers/tracing.py` | done |
| Langfuse self-hosted compose | `docker-compose.observability.yml` | done |
| Pi backend openai/google-genai/bedrock auto-instrumentation | (deferred) | follow-up |
| Approval / safety / container-spawn spans | (deferred) | follow-up |

## How spans nest

```
agent.turn (orchestrator)
└── agent.run_query_loop  ← implicit via attach_parent_context
    ├── tool_call:Bash    ← TracingHookHandler
    ├── ChatAnthropic     ← OpenInference auto-instrumentation
    │   ├── input_tokens, output_tokens, cache_read, cache_creation
    │   └── prompt + completion content
    ├── tool_call:Edit
    └── ChatAnthropic
```

Both `agent.turn` and `tool_call:*` carry `rolemesh.tenant_id` / `coworker_id` / `conversation_id` / `job_id` as attributes — Langfuse uses them to group traces into a session.

## Walkthrough: validate the spike on a dev box

```bash
# 1. Install the project with the new optional extra.
uv sync --extra dev --extra observability

# 2. Boot the dev infra (Postgres + NATS) and Langfuse.
docker compose -f docker-compose.dev.yml \
               -f docker-compose.observability.yml up -d

# 3. Open Langfuse, log in with the seeded admin, create an OTLP-capable
#    public/secret key pair under Settings -> API Keys.
open http://localhost:3000   # email: dev@rolemesh.local  password: rolemesh-dev

# 4. Export the OTLP env vars into the shell that will run the orchestrator.
#    The `Authorization` header is the base64 of "<public-key>:<secret-key>".
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:3000/api/public/otel/v1/traces"
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic $(printf '%s:%s' pk-... sk-... | base64)"

# 5. Run a real turn through the WebUI / Telegram / Slack / eval harness.
rolemesh &
rolemesh-webui &

# 6. In Langfuse, the trace shows up under Projects -> RoleMesh Spike ->
#    Traces. Drill into one trace and confirm you see:
#      - top-level "agent.turn" span with tenant + coworker attrs
#      - nested "tool_call:*" spans for each tool the agent fired
#      - "ChatAnthropic" generation span(s) with token + cost
#      - the credential proxy is transparent (no errors, no
#        duplicate requests)
```

## What "PASS" looks like

- Trace tree renders with at least one `agent.turn` parent and ≥1 nested `tool_call:*` child.
- LLM generation span shows non-zero `input_tokens` + `output_tokens`; Langfuse computes a cost from its model price table.
- The credential-proxy logs (`docker logs <orch>` or however you tail them) show one outbound request per LLM call — instrumentation does not double-fire.
- The dev test suite (`uv run pytest`) still passes against this branch.

## What "FAIL" tells us

| Symptom | Likely cause | Next step |
|---------|--------------|-----------|
| Spans never reach Langfuse | OTLP endpoint / auth header wrong, or BatchSpanProcessor flushes too late | Run with `OTEL_LOG_LEVEL=debug` and tail orchestrator stderr |
| Only orchestrator spans appear, container spans missing | Container can't reach the OTLP endpoint (egress gateway blocks `localhost:3000`) | Either allow `langfuse-web` in the per-tenant DNS allowlist or design the NATS-OTLP bridge before going further |
| LLM tokens are zero | `AnthropicInstrumentor` was imported after `anthropic.Anthropic` got captured (monkey-patch race) | Move `instrument()` even earlier — or pre-import the SDK lazily |
| Existing tests fail | Optional extra not actually optional | Re-check that `install_tracer` short-circuits without the env var and lazy-import guards everywhere |
| Credential proxy returns 401 / 5xx during instrumented runs | OpenInference somehow rewrote URLs (it shouldn't — only wraps methods) | Compare wire traffic with/without the instrumentor; confirm `ANTHROPIC_BASE_URL` still points at the proxy |

## Decision criteria for going past the spike

If all four "PASS" bullets check out, write up the result on the issue/PR and move to **P1 of the original observability plan**:

- Pi backend: add `openinference-instrumentation-openai` + `-google-genai` + `-bedrock`.
- Add spans for: approval wait, safety check dispatch, container spawn lifecycle.
- Decide: keep talking direct OTLP from the container (current spike), or swap to the NATS-OTLP bridge for stronger network isolation.
- Add a thin `agent_traces` linking table so RoleMesh's own admin UI can deep-link into Langfuse from a conversation row.
- Wire `rolemesh-eval` to read cost / latency from Langfuse via its API for the new `cost_scorer`.

If any "PASS" bullet fails and the cause isn't fixable in <1 day, fall back to the custom-schema design in `docs/observability/design.md` (still TBD).
