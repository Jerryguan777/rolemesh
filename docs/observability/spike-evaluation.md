# Observability Spike — End-to-End Evaluation

**Date:** 2026-05-02
**Branch:** `claude/review-main-branch-2Wi1v`
**Verdict:** Framework path **PASS**; LLM auto-instrumentation assumption **FAIL** — design pivot needed for P1.

This document records what actually happened when the spike in
`docs/observability/spike.md` was run end-to-end against a real
Langfuse + a real Claude-backed agent on a developer machine. It is
the evidence that decides whether the spike approach should graduate
to P1.

If the design assumptions changed but the framework holds, the
recommendation at the bottom names the smallest change that gets
P1 unblocked.

---

## TL;DR

| Spike question                                                 | Result           | Evidence                                                                |
|----------------------------------------------------------------|------------------|--------------------------------------------------------------------------|
| Orchestrator → Langfuse OTLP export                            | **PASS**         | 4 `agent.turn` traces in Langfuse                                        |
| Container → Langfuse OTLP export, across `Internal=true` bridge| **PASS** (after a NO_PROXY fix) | `tool_call:Bash` span in Langfuse, traced from `claude-agent` service.name |
| W3C trace-context propagation orchestrator → container         | **PASS**         | Single trace `f7737d5e185be3df91cc27b79ca6bcb7` containing nested `agent.turn` (root) → `tool_call:Bash` (child) |
| No conflict with credential proxy                              | **PASS**         | After NO_PROXY fix, OTel exporter bypasses egress-gateway; LLM/MCP traffic continues to flow through the proxy untouched |
| OpenInference auto-captures Claude LLM tokens + cost           | **FAIL — design assumption wrong** | Inside the running agent container `sys.modules` contains no `anthropic`. `claude-agent-sdk` subprocesses the Node.js Claude Code CLI; the Python `anthropic` SDK is never imported, so OpenInference's `wrapt` patches have nothing to wrap |

Spike validated the **framework**: cross-process tracing works,
Langfuse intake works, no security regression. It did **not** validate
the **content capture path** for Claude backend, because the assumption
that Claude SDK uses Python `anthropic` was wrong.

---

## What "PASS" looks like in Langfuse today

A single trace `f7737d5e185be3df91cc27b79ca6bcb7` contains:

```
agent.turn          (orchestrator,    span_id=eaddc9e726b044f6, root)
└── tool_call:Bash  (agent container, parent=eaddc9e726b044f6)
        attributes:
            tool.name = "Bash"
            tool.input.preview = "{'command': 'ls /workspace', 'description': 'List files in /workspace'}"
            tool.is_error = false
            rolemesh.tool_call_id = "toolu_01JhkYEeavwiRtjunupYBrMw"
        resource:
            service.name = "rolemesh-agent"
            rolemesh.tenant_id, rolemesh.coworker_id, rolemesh.conversation_id, rolemesh.job_id
            rolemesh.agent_backend = "claude"
        SDK: opentelemetry-python 1.41.1
```

That output proves four things in one shot:
- The orchestrator's `agent.turn` span exported under `service.name=rolemesh-orchestrator`.
- The container's `tool_call:Bash` span exported under `service.name=rolemesh-agent`.
- Both share a single `trace_id`, so `traceparent` propagation through the `AgentInitData.trace_context` carrier works.
- The container reached `langfuse-web:3000` from `rolemesh-agent-net` (Internal=true) without going through credential proxy and without DNS allowlist changes (after the NO_PROXY fix).

---

## What "FAIL" means for Claude backend

Probe inside a live agent container:

```bash
$ docker exec <agent-container> python -c \
    "import sys; print([m for m in sys.modules if 'anthropic' in m or 'openinference' in m])"
[]

$ docker exec <agent-container> sh -c \
    "ls /proc | grep -E '^[0-9]+$' | while read p; do echo \$p \$(cat /proc/\$p/comm 2>/dev/null); done"
1 python
16 claude        ← the Node.js Claude Code CLI, not Python
```

`claude-agent-sdk` (the Python package) is a subprocess wrapper around
the `claude` Node.js CLI. The Anthropic API call happens inside the
Node process. OpenInference's `AnthropicInstrumentor` monkey-patches
the Python `anthropic.Anthropic.messages.create` symbol, which is
never imported in this process. Result: instrumentation runs but
captures nothing, and we get zero `ChatAnthropic` spans for the
Claude path.

This is **not** a bug in our wiring. The wiring is correct. The
spike's underlying assumption — "OpenInference auto-instruments any
Anthropic call" — was wrong because the auto-instrument only sees
in-process Python calls.

The Pi backend (`pi/` package) does use Python `openai` /
`google-genai` / `boto3-bedrock` SDKs directly. The corresponding
OpenInference instrumentors would work for Pi. We did not validate
Pi end-to-end in this spike, but the architectural reason that
Claude fails (subprocess) does not apply to Pi.

---

## Walkthrough: what we actually ran

Starting from `claude/review-main-branch-2Wi1v`:

1. `uv sync --extra dev --extra observability` — installed
   `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`,
   `openinference-instrumentation-anthropic`.
2. `docker compose -f docker-compose.observability.yml up -d` — booted
   Langfuse stack (langfuse-web/worker, postgres, clickhouse, redis,
   minio + a one-shot minio-init container that creates the
   `langfuse` bucket).
3. `docker build -t rolemesh-agent:latest -f container/Dockerfile .`
   — rebuilt the agent image with the observability deps and the
   `rolemesh.observability` module copied in.
4. Logged into Langfuse at `http://localhost:3000` as
   `dev@rolemesh.local` and created an OTLP API key pair.
5. Exported on the host shell:
   ```bash
   export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:3000/api/public/otel/v1/traces"
   export OTEL_EXPORTER_OTLP_ENDPOINT_AGENT="http://langfuse-web:3000/api/public/otel/v1/traces"
   export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic $(printf '%s:%s' pk-lf-... sk-lf-... | base64)"
   ```
6. Started orchestrator + webui from the spike-branch venv against
   the live `data/` directory of the existing repo (so we ran with
   real coworker state, not an empty fixture).
7. Sent a test message ("use Bash to run `ls /workspace`") via the
   WebUI to a Claude-backed coworker.
8. Stopped the idle agent container after the turn completed to
   force `BatchSpanProcessor` flush.
9. Queried `GET /api/public/traces` and `GET /api/public/observations`
   on Langfuse to verify span landing + parent links.

---

## Issues caught and fixed during the walkthrough

The walkthrough surfaced a punch list of issues. Each one would have
made the spike's verdict an unreadable failure on its own. These are
in this commit's diff except where noted; rerunning the walkthrough
on a fresh checkout should not re-encounter any of them.

### P0 blockers — would have made the spike untestable

1. **`container/Dockerfile` did not COPY `src/rolemesh/observability/`.**
   `agent_runner.hooks.handlers.tracing` does a top-level
   `from rolemesh.observability import get_tracer`. Without the
   module in the image, every spawn would `ModuleNotFoundError` at
   boot. Fixed in the spike's earlier commit by adding a `COPY` line.
2. **Same Dockerfile did not pip-install `opentelemetry-*` /
   `openinference-instrumentation-anthropic`.** Without these,
   `install_tracer` short-circuits with a warning and the container
   tracer is a noop. Fixed by adding to the Dockerfile pip step.
3. **`runner.build_container_spec` did not propagate
   `OTEL_EXPORTER_OTLP_ENDPOINT` / `OTEL_EXPORTER_OTLP_HEADERS` into
   the container env.** Container's tracer would have nothing to
   export to. Fixed by adding the propagation logic plus a separate
   `OTEL_EXPORTER_OTLP_ENDPOINT_AGENT` override (because the
   container's `langfuse-web:3000` and the host's `localhost:3000`
   resolve differently). Both keys added to `CONTAINER_ENV_ALLOWLIST`.
4. **`safety/audit.py` had a top-level
   `from rolemesh.db import insert_safety_decision`.** The Dockerfile
   ships `audit.py` to the agent image (it holds shared stateless
   helpers). The agent image deliberately does not ship `rolemesh.db`
   (orchestrator-only, asyncpg dep). The top-level import made the
   agent crash with `No module named 'rolemesh.db'` on every boot.
   This is a regression introduced by the recent `refactor/db` PR
   that was merged into `main` — not the spike's fault, but the
   spike was the first thing to hit it because spike code starts the
   container in the same path. Fixed in this commit by moving the db
   import into `DbAuditSink.write()` (the agent never instantiates
   `DbAuditSink`, so the lazy import never fires there).

### P1 blockers — would have made the spike misreport "no spans"

5. **Langfuse v3 rejects `ENCRYPTION_KEY` with insufficient entropy.**
   The compose file shipped a placeholder of 64 zeros which v2
   accepted. v3 enforces 256-bit hex entropy and refuses to start.
   Fixed by generating a real `openssl rand -hex 32` and committing
   it (dev-only secret, replace before sharing the deployment).
6. **Next.js v16 inside `langfuse-web` binds to a single network
   interface unless `HOSTNAME=0.0.0.0` is set.** With the container
   attached to both `default` (so it can reach postgres/clickhouse)
   and `rolemesh-agent-net` (so agents can reach it), Next.js picked
   one interface and the other side got `ECONNREFUSED`. Fixed by
   adding `HOSTNAME: 0.0.0.0` to the web service env.
7. **Compose did not bootstrap the MinIO `langfuse` bucket.** Langfuse's
   OTLP intake writes spans to S3 (MinIO) before queueing them for
   Clickhouse. Without the bucket, the intake returns HTTP 500
   (`The specified bucket does not exist`) on every push. The
   exporter retries 3 times and drops the batch silently. Fixed by
   adding a `langfuse-minio-init` one-shot container running
   `mc mb -p local/langfuse`.
8. **Compose container names had the project-name prefix
   (`rolemesh-2-langfuse-*`), which the orchestrator's
   `cleanup_orphans("rolemesh-")` matched and force-removed on
   startup.** Every time we restarted the orchestrator, Langfuse
   disappeared. Fixed by giving every Langfuse service an explicit
   `container_name: langfuse-*` (no `rolemesh-` prefix).
9. **`HTTPS_PROXY` / `HTTP_PROXY` set in the agent container env
   captured the OTel exporter's request to `langfuse-web:3000` and
   routed it through the credential proxy.** The proxy has no
   allowlist entry for langfuse-web and returned HTTP 403 to every
   span batch. Fixed in `runner.build_container_spec` by appending
   the OTLP host to `NO_PROXY` whenever the OTLP env is present, so
   the OTel exporter bypasses the forward proxy and goes direct
   over agent-net.

### P2 — would not have prevented spike completion, but worth noting

10. **`BatchSpanProcessor.schedule_delay` defaults to 5 seconds.** A
    sub-5s turn that doesn't trigger a flush via span end will drop
    its spans on container exit. The spike already ships
    `shutdown_tracer()` in both orchestrator and agent runner
    `finally` blocks, which empirically made the orchestrator's
    `agent.turn` spans land within seconds of the orchestrator exit.
    Container-side `tool_call:*` spans land within ~5 seconds of
    `tool.end()` because they are individually short-lived spans
    that the BatchSpanProcessor schedules immediately.
11. **`agent.turn` span is alive for the full container lifetime,
    not the duration of one user turn.** The orchestrator opens
    `agent.turn` and waits on `executor.execute()`, which itself
    waits on `handle.wait()` (container exit). Both Claude and Pi
    backends keep the container idle after a turn for follow-ups,
    so the orchestrator's "turn" span actually represents a session.
    For the spike this just means the orchestrator's span only
    lands when the container exits or is stopped. For P1 we should
    rescope `agent.turn` to one user turn — likely by opening it
    around each `_process_conversation_message` call instead of
    around `executor.execute()`.

---

## Architectural follow-ups (for the design doc, not for P1's first commit)

- **Claude LLM span capture** — the OpenInference path is dead for
  Claude. See "Recommendation" below for the candidate alternatives.
- **Pi LLM span capture** — not validated yet. The architectural
  reason Claude fails (subprocess) does not apply: Pi imports
  `openai` / `google-genai` / `boto3` directly. Adding the
  corresponding OpenInference instrumentors in `agent_runner/main.py`
  is mechanical (one `Instrumentor().instrument()` call each, behind
  the same try/except as the existing Anthropic one).
- **Multi-turn context refresh** — `agent_runner/main.py` only
  attaches the orchestrator's parent context for the *first* turn of
  the container's session. Follow-up turns inherit no parent context,
  so they would create their own root traces. This is unfixed in
  the spike (TODO comment in code). Cleanest fix is for the
  orchestrator to write a fresh `trace_context` carrier into the
  per-turn input message it sends over NATS, and for the container
  to swap the active context per follow-up.
- **Production network design** — the dev compose pins `langfuse-web`
  onto `rolemesh-agent-net` so agents can reach it directly. This is
  fine on a single-developer box but **must not ship to production**:
  a malicious agent could exfiltrate arbitrary data into span
  attributes to a Langfuse instance that escaped tenant isolation.
  Production needs a NATS→OTLP bridge: agents publish spans to a
  NATS subject the orchestrator subscribes and forwards (with
  sanitisation) to Langfuse. That bridge is unimplemented.
- **Span attribute sanitisation** — `tool.input.preview` and
  `tool.result.preview` are truncated but not redacted. Anything
  the agent reads or writes ends up in Langfuse. For tenants on a
  shared Langfuse instance this is a privacy leak. The
  sanitisation pass belongs at the NATS→OTLP bridge boundary.
- **Container span flush on abort** — `shutdown_tracer()` only fires
  in the normal `finally` of `run_query_loop`. If the agent process
  is killed (`docker stop` after grace period), spans buffered by
  `BatchSpanProcessor` are lost. Already known; cost is low (only
  the in-flight 5s window).

---

## Recommendation: how to enter P1

Going forward, P1 should:

1. **Accept that OpenInference Anthropic does not work for Claude
   backend** and pivot.
2. **Hook `BackendEvent.ResultEvent` into a Claude-side LLM span.**
   `ResultEvent` already carries `usage` (input/output tokens). The
   minimal change is in `agent_runner/main.py`'s `on_event` callback:
   when a `ResultEvent` fires, open a span named `claude.message`
   (or similar) with `gen_ai.usage.input_tokens` /
   `gen_ai.usage.output_tokens` attributes (use the OTel-GenAI
   semantic convention so Langfuse renders cost from its built-in
   model-price table), and end it immediately. The span timestamp
   bracketing won't be tight but the token + cost surface will.
3. **Wire OpenInference instrumentors for Pi backend** —
   `openai` / `google-genai` / `bedrock`. One liner each, behind
   the same `if AGENT_BACKEND == "pi"` guard the Anthropic
   instrumentor uses for `claude`.
4. **Rescope `agent.turn` to one user turn**, not the container
   lifetime. Open it around the orchestrator's per-message
   processing path instead of around `executor.execute()`.
5. **Open spans for the orchestrator-side approval and safety
   pipelines.** They're already structured (engine + checks);
   wrapping each check in a span is mechanical.
6. **Add the NATS→OTLP bridge** before any non-dev deployment. The
   single-tenant direct-OTLP path the spike validated is the
   wrong long-term posture.

If we want a smaller P1 (cost dashboard only), step 2 alone gets us
token + cost per turn for Claude backend. Steps 3 + 4 give the full
nested trace tree for Pi backend. Steps 5 + 6 are independent and
can ship later.

If we want a different direction (custom `agent_traces`/`agent_spans`
schema in Postgres, no Langfuse), the framework code in
`rolemesh.observability` is reusable — only the exporter changes.
The spike does not validate that path; it only validates that the
OTel-based architecture is sound.

---

## Appendix A — required env vars for the dev walkthrough

```bash
# Host shell (orchestrator)
export OTEL_EXPORTER_OTLP_ENDPOINT="http://localhost:3000/api/public/otel/v1/traces"

# Host shell (passed via runner.build_container_spec into agent containers)
# Container's view of Langfuse — must resolve from rolemesh-agent-net.
export OTEL_EXPORTER_OTLP_ENDPOINT_AGENT="http://langfuse-web:3000/api/public/otel/v1/traces"

# Both — Basic auth for Langfuse OTLP intake.
# pk-lf-... and sk-lf-... come from Langfuse Settings → API Keys.
export OTEL_EXPORTER_OTLP_HEADERS="Authorization=Basic $(printf '%s:%s' <pk> <sk> | base64)"
```

## Appendix B — verified versions

- Langfuse: 3.172.1 (web + worker)
- OpenTelemetry Python SDK: 1.41.1
- openinference-instrumentation-anthropic: latest pinned in pyproject
- claude-agent-sdk: as shipped in `container/Dockerfile`
- Docker: 28.2.2; Compose: 2.37.1
- Linux kernel: 6.17.0
