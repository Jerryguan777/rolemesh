# External MCP Tools Architecture

This document describes how RoleMesh integrates external MCP (Model Context Protocol) servers — why the credential-proxy approach was chosen over alternatives, how MCP configuration flows from the database to the agent container, the URL rewriting that hides credentials from the container, and the auth modes that decide how each MCP request is authenticated.

## Background: Why External MCP?

RoleMesh agents run inside Docker containers, with either the Claude SDK or Pi backend. Each backend has built-in tools (Bash, Read, Write, …) and an in-process `rolemesh` MCP server for IPC with the orchestrator (`send_message`, `schedule_task`, …).

But agents often need to access **external services** — internal APIs, databases, third-party platforms — exposed as MCP servers. These external servers run alongside the orchestrator (or further away), use SSE or streamable-HTTP transport, and require authentication (typically a JWT in the `Authorization` header).

Both backends support MCP servers natively:

```python
mcp_servers={
    "my-server": {
        "type": "sse",  # or "http"
        "url": "http://...",
        "headers": {"Authorization": "Bearer <token>"},
    }
}
```

The problem this document solves is: **how do we get the MCP server URL and auth token into the container securely?**

## Design Constraints

1. **Containers must not hold auth tokens.** The same rule already protects LLM API keys (Anthropic / OpenAI / Bedrock) — containers see a placeholder, the credential proxy injects the real key. MCP tokens follow the same pattern.
2. **Per-coworker configuration.** Different coworkers need different MCP servers. Configuration lives on `coworkers.tools` (JSONB), not as a global env var.
3. **Token source is external.** MCP tokens come from the user's IdP (via OIDC), not minted by RoleMesh. The per-user, auto-refreshing token model is managed by `TokenVault` — see "Token forwarding" below.
4. **No container image rebuild for new servers.** Adding an MCP server should only require a DB update and (optionally) a hot-reload signal — no rebuild, ideally no restart.

## Alternatives Considered

### Option A: Pass Token Directly to Container

```
Orchestrator → AgentInitData.mcp_servers[].token → Container → MCP Server
```

The simplest approach: include the JWT in `AgentInitData`.

**Pros**: minimal code, no proxy involvement.

**Cons**: tokens are visible inside the container. Agents run arbitrary tool calls (Bash, etc.) — any tool call could read the token from memory or environment. Leaked MCP tokens often grant broad access to internal services; the blast radius is too large.

**Rejected** — violates the "containers don't hold credentials" principle.

### Option B: Generate JWT in Container

Pass a JWT signing secret into the container; the agent runner mints short-lived tokens before each MCP call.

**Pros**: tokens are always fresh, no expiry issues for long-running containers.

**Cons**: the signing secret is **more sensitive** than any individual token — a leak grants unlimited token issuance. Also, RoleMesh doesn't *control* JWT issuance for these MCP servers — tokens come from the IdP.

**Rejected**.

### Option C: Credential Proxy Forwarding (Chosen)

```
Container → Credential Proxy → injects Authorization → MCP Server
```

The container sends MCP requests to the credential proxy with **no** auth header. The proxy looks up the registered MCP server, picks the right `Authorization` (per-user IdP token, or a static service key — see "auth_mode" below), and forwards.

**Pros**:

- Containers never see the token — same security model as LLM API keys.
- Reuses existing credential-proxy infrastructure (already in front of LLM APIs).
- Token semantics are decoupled from container lifetime — auto-refreshing per-user tokens work for agent runs that span hours, well past any single IdP token TTL.

**Cons**:

- All MCP traffic adds one network hop through the proxy.
- The proxy must handle SSE / streamable-HTTP without response buffering.

**Chosen** — preserves the security boundary, plugs cleanly into existing infrastructure.

## Where the Credential Proxy Lives

The credential proxy was originally a host-side process bound to `host.docker.internal:3001`. **Since EC-2 / Egress Control V1 it lives inside the `egress-gateway` container** alongside the forward proxy and the authoritative DNS resolver. Agent containers reach it through Docker DNS as `http://egress-gateway:3001`. The agent network (`rolemesh-agent-net`) is `Internal=true`, so the gateway is the only path out — for LLM API calls, MCP calls, and any other outbound traffic. Topology details: [`egress/deployment.md`](egress/deployment.md).

The legacy import path `src/rolemesh/security/credential_proxy.py` is kept as a thin re-export of the real implementation in `src/rolemesh/egress/reverse_proxy.py`, so older call sites continue to resolve.

## Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│                         CONFIGURATION                                │
│                                                                      │
│  PostgreSQL                          OIDC IdP                        │
│  ┌──────────────────────────────┐    ┌──────────────────────────────┐│
│  │ coworkers.tools (JSONB):     │    │ Per-user access tokens,      ││
│  │ [{"name":"my-server",        │    │ refreshed automatically      ││
│  │   "type":"sse",              │    │ (TokenVault — see auth doc)  ││
│  │   "url":"http://.../mcp/",   │    └──────────────────────────────┘│
│  │   "auth_mode":"user"}]        │                                   │
│  └──────────────┬───────────────┘                                    │
└─────────────────┼────────────────────────────────────────────────────┘
                  │
                  ▼
   ORCHESTRATOR registers each server with the credential proxy:
     (name, origin URL, per-server static headers, auth_mode)

   And distributes the registry over NATS:
     egress.mcp.snapshot.request   (request-reply; gateway pulls on boot)
     egress.mcp.changed            (broadcast on admin edit; hot-reload)

                  ▼
       ┌────────────────────────────────────────────┐
       │  Credential Proxy   (in egress-gateway)    │
       │  Routes  /mcp-proxy/{name}/**  :           │
       │    1. lookup name → (origin, headers,      │
       │       auth_mode)                           │
       │    2. inject Authorization per auth_mode   │
       │    3. forward to origin                    │
       └────────────────────────────────────────────┘
                  ▲
                  │  http://egress-gateway:3001/mcp-proxy/my-server/mcp/
                  │  (no Authorization header)
                  │
       ┌──────────┴────────────────────────────────┐
       │   Agent container (Claude SDK or Pi)       │
       │   Reads AgentInitData.mcp_servers from KV │
       │   → registers with the agent SDK           │
       └────────────────────────────────────────────┘
```

### Request Flow (single tool call)

1. Agent decides to call `mcp__my-server__some_tool`.
2. The SDK sends an HTTP request to `http://egress-gateway:3001/mcp-proxy/my-server/mcp/` with no `Authorization` header. The container side never knew the token.
3. Credential proxy:
   - Strips the `/mcp-proxy/{name}` prefix; remaining path = `/mcp/`.
   - Looks up `my-server` in the registry → `(origin URL, per-server static headers, auth_mode)`.
   - Picks `Authorization` according to `auth_mode` (see below).
   - Forwards to the origin URL.
4. MCP server validates the token (it doesn't know there's a proxy in front), processes the tool call, returns SSE / streamable-HTTP.
5. Proxy streams the response back to the container without buffering.

## auth_mode: three authentication strategies

A single proxy serves both OIDC-aware and legacy MCP servers. `auth_mode` (set on `McpServerConfig`) tells the proxy which authentication shape to use for each server.

| `auth_mode` | What the proxy injects | Use case |
|---|---|---|
| **`user`** (default) | The user's IdP-issued access token as `Authorization: Bearer <fresh access_token>`. Per-server static headers are passed through but `Authorization` is overridden. | OIDC-aware MCP servers — they validate the token via OIDC discovery and learn which user is calling. |
| **`service`** | Per-server static headers verbatim (including any admin-set `Authorization`). **No per-user token.** | Service-to-service / legacy MCP that uses a shared service key. |
| **`both`** | Per-server static headers stay intact; the user's access token rides on `X-User-Authorization`. | Dual-layer verification — the MCP server checks both a service key (for itself) and a user token (for the user behind the request). |

The `user` and `both` modes need a per-request user identity. The container forwards it via `X-RoleMesh-User-Id`, set by the agent runner from `AgentInitData`. If `TokenVault` has no fresh token for that user, the request is forwarded **without** the user token — the MCP server returns 401, the agent surfaces the error, and the user is prompted to re-login. No silent fallback to another user's token.

Token mechanics (encryption at rest, automatic refresh, rotation handling) live in [`6-auth-architecture.md`](6-auth-architecture.md) — "MCP Token Forwarding: TokenVault". This document is concerned only with the contract between the credential proxy and the MCP server.

## Data Model

### `McpServerConfig` (orchestrator side)

Stored in `coworkers.tools` JSONB. Carries everything the proxy needs to forward a request, plus per-tool metadata used elsewhere:

```python
@dataclass(frozen=True)
class McpServerConfig:
    name: str             # registered name in the SDK, e.g. "my-server"
    type: str             # "sse" or "http" (streamable-HTTP)
    url: str              # actual MCP server URL on the host network
    headers: dict[str, str] = ...   # per-server static headers (service keys, ...)
    auth_mode: str = "user"          # "user" | "service" | "both"
    tool_reversibility: dict[str, bool] = ...
```

The `tool_reversibility` field is per-tool metadata for the Safety Framework V2 cost-class × reversibility guard — `True` for read-only queries, `False` (default) for state-mutating tools. Missing entries fall back to a built-in table; the agent does not decide reversibility, the operator does. See [`safety/safety-framework.md`](safety/safety-framework.md).

### `McpServerSpec` (container side)

Passed via `AgentInitData.mcp_servers` (the NATS KV bootstrap payload — see [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md)). Contains only what the container is allowed to see:

```python
@dataclass(frozen=True)
class McpServerSpec:
    name: str
    type: str
    url: str              # rewritten proxy URL — no token, no upstream host
    tool_reversibility: dict[str, bool] = ...
```

No `headers`, no `auth_mode`, no token. Authentication decisions stay strictly on the orchestrator/proxy side of the boundary.

### URL Rewriting

The orchestrator transforms each host-side URL into a proxy URL before writing it into `AgentInitData`:

```
Input:  http://localhost:9100/mcp/
              ↓
Parse:  scheme=http, host=localhost, port=9100, path=/mcp/
              ↓
Output: http://egress-gateway:3001/mcp-proxy/my-server/mcp/
        ├── proxy host:port ───────┤├── prefix ──┤├─ path ─┤
```

The proxy reverses the rewrite on each request:

```
Request: /mcp-proxy/my-server/mcp/
              ↓
Strip:   server_name = "my-server", remaining_path = "/mcp/"
              ↓
Lookup:  registry["my-server"] → "http://localhost:9100"
              ↓
Forward: http://localhost:9100/mcp/
```

Admin-supplied `localhost` URLs are also rewritten to a Docker-reachable hostname (`host.docker.internal` on Linux and Darwin, picked at every publishing boundary) so the gateway — running in a container — can actually reach the upstream service. The cross-platform fix-ups landed in the egress series PRs (#13–#17).

## Registry Distribution

The MCP registry lives in two places that must agree: the orchestrator's host-side dict and the same dict in the gateway container. They sync via NATS:

- **`egress.mcp.snapshot.request`** — request-reply RPC. The gateway issues this on boot to fetch the current registry.
- **`egress.mcp.changed`** — broadcast. When an admin adds, edits, or removes an MCP server through the WebUI admin API, the orchestrator publishes a delta and the gateway updates its in-memory cache without a restart.

Editing `coworkers.tools` through the admin REST API is therefore a hot operation. New MCP servers become available on the next agent spawn (the agent reads the proxy URL from `AgentInitData`); existing agents continue to work because their proxy URL didn't change.

## Security Model

| Concern | Approach |
|---|---|
| Token storage | Per-user IdP refresh + access tokens encrypted at rest in `oidc_user_tokens`; never enters container memory |
| Token injection | Credential proxy adds `Authorization` per `auth_mode` |
| Container access | Container only knows the proxy URL — no token, no upstream host |
| Token expiry | TokenVault auto-refreshes against the IdP; permanent failure forces re-login |
| MCP server validation | MCP server validates the token via OIDC discovery — RoleMesh is a passthrough, not an issuer |
| Proxy scope | The MCP route only forwards to **registered** server names — unknown names return 404 |

### What a compromised container can do

A container running arbitrary code (via the Bash tool) can:

- **See** the proxy URL (`http://egress-gateway:3001/mcp-proxy/my-server/...`).
- **Call** the MCP server through the proxy — the proxy doesn't authenticate the *caller*, it authenticates the *user* the call is on behalf of. A compromised container can therefore act as the user it was started for, but cannot impersonate other users (their tokens are not present in this container).
- **Cannot** see any auth token, per-user or service.
- **Cannot** reach the MCP server directly — the agent network is `Internal=true`, so the only outbound path is through the gateway.

The boundary is identical to the LLM API path: the container can make API calls through the proxy, but cannot extract or forge credentials.

## Related documentation

- [`6-auth-architecture.md`](6-auth-architecture.md) — `TokenVault` mechanics, OIDC integration, per-user token model
- [`safety/safety-framework.md`](safety/safety-framework.md) — `tool_reversibility` and how it gates risky tool calls
- [`egress/deployment.md`](egress/deployment.md) — agent network topology, where the credential proxy actually lives, gateway boot sequence
- [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) — `AgentInitData.mcp_servers` field; `egress.mcp.*` subjects
