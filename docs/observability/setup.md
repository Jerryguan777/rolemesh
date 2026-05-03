# Observability Setup (Dev)

End-to-end OpenTelemetry tracing for RoleMesh, exporting to a
self-hosted Langfuse. Single-developer dev workflow only — see
the security note at the bottom before doing anything else with it.

## What you get

After completing this walkthrough, every agent turn shows up in
the Langfuse UI as a trace tree:

```
agent.turn  (orchestrator-side root, attributes: tenant/coworker/conv/backend)
├── tool_call:Read
├── tool_call:Bash
├── claude.message       (Claude backend: synthetic span with token usage)
└── ChatOpenAI / ...     (Pi backend: full prompt + response, OpenInference auto-instrumented)
```

The `agent.turn` span carries `langfuse.session.id = conversation_id`
so the Langfuse Sessions view groups all turns of a conversation
together. Cost is computed automatically from `gen_ai.*` token
attributes against Langfuse's model registry.

## 1. Install the optional extra

```sh
uv sync --extra observability
```

Adds `opentelemetry-sdk`, the OTLP/HTTP exporter, and three
OpenInference instrumentors (openai / google-genai / bedrock) to
the venv. Without this step every observability helper short-
circuits to a no-op and rolemesh runs unchanged.

## 2. Start Langfuse

The `rolemesh-agent-net` Docker network must exist before Langfuse
can attach to it. If you haven't run `rolemesh` yet:

```sh
docker network create -d bridge --internal rolemesh-agent-net
```

(Otherwise `rolemesh` will create it on first start — start it once
with `uv run rolemesh`, kill with Ctrl-C, then proceed.)

```sh
docker compose -f docker-compose.observability.yml up -d
```

This brings up: `langfuse-postgres`, `langfuse-clickhouse`,
`langfuse-redis`, `langfuse-minio` + a one-shot init container that
creates the `langfuse` MinIO bucket, plus `langfuse-worker` and
`langfuse-web`.

Visit http://localhost:3000 and log in:

| Field    | Value                |
|----------|----------------------|
| email    | `admin@example.com`  |
| password | `changeme`           |

(These come from `LANGFUSE_INIT_USER_*` in the compose file. Change
them or remove the bootstrap stanza for any non-throwaway use.)

## 3. Create an OTLP API key pair

Inside Langfuse:

1. Go to the auto-created `rolemesh` project.
2. **Settings → API Keys → Create new API keys**.
3. Save the public key (`pk-lf-...`) and secret key (`sk-lf-...`).

## 4. Configure RoleMesh to export

Add these to your `.env` (or shell exports — `rolemesh.bootstrap`
loads `.env` at process start):

```dotenv
# Orchestrator-side endpoint (host's view of Langfuse).
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:3000/api/public/otel

# Container-side endpoint (agent-net's view of Langfuse).
# build_container_spec prefers this over the unsuffixed value when
# present. Both can be set; the orchestrator picks the right one
# per side.
OTEL_EXPORTER_OTLP_ENDPOINT_AGENT=http://langfuse-web:3000/api/public/otel

# Auth header. Format is "Authorization=Basic <base64(pk:sk)>".
# Generate the base64 part with:
#   echo -n "pk-lf-...:sk-lf-..." | base64
OTEL_EXPORTER_OTLP_HEADERS=Authorization=Basic <BASE64_PK_COLON_SK>
```

The auth header is forwarded into the agent container by the
orchestrator (added to `CONTAINER_ENV_ALLOWLIST`), so containers
authenticate against the same Langfuse project as the orchestrator.

## 5. Start RoleMesh and verify

```sh
uv run rolemesh
```

In the startup logs you should see:

```
OTel tracer installed  service=rolemesh-orchestrator  endpoint=http://localhost:3000/api/public/otel
```

Send a message that triggers a tool call (e.g. "show me the README").
After a few seconds, refresh **Tracing → Traces** in Langfuse — the
new trace appears with the `agent.turn` root and its children. Token
counts and cost are populated automatically on the `claude.message`
or `ChatOpenAI` / `Bedrock` / `Gemini` child.

## Known limitations

- **Claude latency is approximate.** The Claude Agent SDK dispatches
  Anthropic API calls from a Node.js subprocess. Python only sees
  the `ResultEvent` at the *end* of the call, so the synthetic
  `claude.message` span has its `start_time` set to `end - 1ms` —
  enough for Langfuse to render it as a finite bar but NOT a real
  duration. Token counts and cost ARE accurate; latency is not.
- **Claude prompt is not captured.** Same root cause — the prompt
  lives in the Node subprocess. Pi backend captures full prompts
  via OpenInference.
- **No multi-turn parent linking.** Each turn opens its own
  `agent.turn` span; there's no "conversation" parent above. Use
  the Sessions view (which groups by `langfuse.session.id`) to see
  all turns of a conversation together.
- **No sampling.** All spans go through. For a high-traffic
  deployment you'd want a `TraceIdRatioBased` sampler; out of
  scope for the dev brief.

## Troubleshooting

| Symptom                                         | Likely cause                                                                                                                          |
|-------------------------------------------------|---------------------------------------------------------------------------------------------------------------------------------------|
| `Connection refused` in orchestrator logs       | Langfuse not running. `docker compose -f docker-compose.observability.yml ps`.                                                        |
| `403` from `langfuse-web`                       | OTLP `Authorization` header missing or malformed. Re-base64 `pk:sk`.                                                                  |
| Spans visible in worker logs but never in UI    | MinIO `langfuse` bucket doesn't exist. Re-run `langfuse-minio-init` or `mc mb local/langfuse` manually.                               |
| Container traces missing, orchestrator OK       | `OTEL_EXPORTER_OTLP_ENDPOINT_AGENT` not set or unreachable from agent-net. `docker exec <agent_container> wget -O- $endpoint/v1/traces`. |
| `langfuse-web` boot loops                       | `ENCRYPTION_KEY` low entropy. `openssl rand -hex 32` and replace.                                                                     |
| Container OTLP requests 403 at credential proxy | `NO_PROXY` not forwarded. Check `build_container_spec` parsed your endpoint host correctly.                                           |

## Security posture

This setup is **dev-only**. Specifically broken for prod:

1. Agent containers reach `langfuse-web` directly over the agent
   bridge. That defeats RoleMesh's "agents are on Internal=true,
   no egress route" invariant. A real prod setup needs a NATS →
   OTLP bridge (orchestrator subscribes, forwards to Langfuse).
2. ENCRYPTION_KEY / SALT / NEXTAUTH_SECRET / database passwords
   are static dev defaults in the compose file. Rotate everything
   for any non-throwaway environment.
3. The bootstrap admin (LANGFUSE_INIT_USER_*) is a single hardcoded
   credential. Disable for multi-user installs and use SSO instead.
4. No PII redaction. Tool inputs / responses land in Langfuse
   as-is. Brief Section 3.2 explicitly defers PII redaction.

If any of those bullets aren't acceptable for your deployment,
treat this compose file as a reference, not a target.
