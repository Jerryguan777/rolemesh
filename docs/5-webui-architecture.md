# WebUI Architecture

This document explains the design of RoleMesh's browser-based WebUI channel — why it is an independent process, how it communicates with the orchestrator via NATS, and the trade-offs behind these decisions.

## Background

RoleMesh supports Telegram and Slack as messaging channels. Each has a gateway (TelegramGateway, SlackGateway) that implements the `ChannelGateway` protocol and runs inside the orchestrator process. This works because Telegram and Slack gateways are event-driven listeners — they receive messages from external APIs and invoke the orchestrator's callback.

Adding a browser-based WebUI introduces a different challenge: the WebUI needs an **HTTP/WebSocket server** that browsers connect to. This server must handle real-time streaming, static file serving, REST APIs (conversation history, admin panel), and a real authentication flow — surfaces that don't fit the in-process gateway shape.

## Why a Separate Process?

We considered two architectures:

### Option A: Embed in Orchestrator

```
Browser ←WebSocket→ [Orchestrator + WebSocket handler] ←NATS→ [Agent Container]
```

The WebSocket server runs inside the orchestrator process, same as Telegram/Slack gateways.

**Pros:**
- Simple — direct function calls, no serialization overhead
- Single process to deploy

**Cons:**
- Couples HTTP concerns (routing, middleware, auth, static files) into the orchestrator
- Cannot scale WebUI independently of the orchestrator
- Hard to embed into existing SaaS platforms — they need a standard REST/WebSocket API, not a function call interface
- Adding REST APIs for future features (conversation management, admin panel) bloats the orchestrator
- No OpenAPI documentation for third-party integration

### Option B: Independent FastAPI Process (chosen)

```
Browser ←WebSocket→ [FastAPI service] ←NATS→ [Orchestrator] ←NATS→ [Agent Container]
```

FastAPI runs as a separate process. Communication with the orchestrator is exclusively via NATS.

**Pros:**
- Clean separation — orchestrator handles agent lifecycle, FastAPI handles HTTP
- FastAPI provides automatic OpenAPI docs, Pydantic validation, dependency injection
- Can scale independently (multiple FastAPI instances behind a load balancer)
- Easy to embed into existing SaaS — it's a standard HTTP/WebSocket API
- Third parties can generate client SDKs from the OpenAPI spec
- Future REST APIs (conversation management, billing, admin) live here naturally

**Cons:**
- More complex — two processes, NATS message serialization
- Slightly higher latency (NATS hop vs direct function call)
- Need to define and maintain a NATS protocol

We chose Option B because RoleMesh is designed as an **agent-as-a-service platform**. The WebUI is just one consumer of the API — future consumers include third-party SaaS integrations, mobile apps, and admin dashboards. A well-defined API boundary is worth the added complexity.

## Why NATS (Not Direct DB)?

The FastAPI service could bypass NATS entirely and write directly to the database, then have the orchestrator poll for new messages. We rejected this because:

1. **Latency** — Polling adds seconds of delay. NATS delivers messages in milliseconds.
2. **Streaming** — Agent output must stream to the browser in real time. The orchestrator receives agent chunks via NATS (`agent.{job_id}.results`) and must forward them immediately. There is no database table to poll for streaming chunks.
3. **Consistency** — The orchestrator manages conversation state, session IDs, agent scheduling, and container lifecycle. If FastAPI writes directly to the DB, it bypasses all this logic and creates race conditions.
4. **Alignment** — The orchestrator already uses NATS for agent IPC. Adding web channel subjects to the same infrastructure is natural.

FastAPI does read the database directly for one thing: **token validation and conversation history reads** (RLS-bound to the requesting tenant). This is a read-only path that doesn't interfere with the orchestrator's authoritative state.

## NATS Subject Design

A separate JetStream stream `web-ipc` carries four subject patterns:

| Subject | Direction | Purpose |
|---|---|---|
| `web.inbound.{binding_id}` | FastAPI → Orchestrator | User sent a message |
| `web.stream.{binding_id}.{chat_id}` | Orchestrator → FastAPI | Streaming text chunks |
| `web.typing.{binding_id}.{chat_id}` | Orchestrator → FastAPI | Agent started/stopped processing |
| `web.outbound.{binding_id}.{chat_id}` | Orchestrator → FastAPI | Complete agent reply |

### Why Separate Stream?

We use a separate `web-ipc` stream rather than adding subjects to the existing `agent-ipc` stream because:

- **Different retention needs** — Agent IPC messages are transient (consumed once by the orchestrator). Web messages may need different retention for debugging or replay.
- **Different consumers** — Agent IPC is consumed by the orchestrator only. Web subjects are consumed by FastAPI. Separate streams avoid cross-contamination of consumer offsets.
- **Operational clarity** — `nats stream info web-ipc` immediately shows web channel health without mixing in agent traffic.

### Why `binding_id` and `chat_id` in Subjects?

- `binding_id` identifies which coworker's web channel this message belongs to. It maps to a `channel_bindings` row in the database. FastAPI subscribes to subjects matching the binding IDs it serves.
- `chat_id` identifies the specific browser session (conversation). Including it in the subject allows FastAPI to route messages to the correct WebSocket connection without inspecting the payload.

## Orchestrator Side: WebNatsGateway

On the orchestrator side, `WebNatsGateway` satisfies the same `ChannelGateway` protocol as `TelegramGateway` and `SlackGateway`.

```python
_gateways = {
    "telegram": TelegramGateway(on_message=_handle_incoming),
    "slack":    SlackGateway(on_message=_handle_incoming),
    "web":      WebNatsGateway(on_message=_handle_incoming, transport=_transport),
}
```

The key difference from other gateways:

| | TelegramGateway | SlackGateway | WebNatsGateway |
|---|---|---|---|
| Receives messages from | Telegram API (polling) | Slack Socket Mode | NATS `web.inbound.*` |
| Sends messages via | Telegram Bot API | Slack Web API | NATS `web.outbound.*` |
| Typing indicator via | `ChatAction.TYPING` | Not supported | NATS `web.typing.*` |

The orchestrator treats all three identically after the gateway layer — the same message routing, conversation lookup, agent scheduling, and output handling applies.

### Streaming: Why a New Method?

The `ChannelGateway` protocol defines `send_message(binding_id, chat_id, text)` which sends a **complete** text. For Telegram and Slack, this is correct — you send one message with the full response.

For WebUI, we want **streaming** — the browser should see text appear as the agent produces it, not wait for the entire response. To support this without changing the protocol that Telegram/Slack use, `WebNatsGateway` adds two extra methods:

- `send_stream_chunk(binding_id, chat_id, content)` — publishes one text chunk
- `send_stream_done(binding_id, chat_id)` — signals the response is complete

The agent output callback checks the gateway type:

```python
if isinstance(gw, WebNatsGateway):
    await gw.send_stream_chunk(binding.id, chat_id, text)
else:
    await gw.send_message(binding.id, chat_id, text)
```

This keeps Telegram/Slack unaffected while enabling streaming for WebUI.

## FastAPI Side: WebSocket Lifecycle

When a browser opens a WebSocket connection:

1. **Auth**: FastAPI validates the request via `webui.auth.authenticate_ws()` (see "Authentication" below).
2. **Session**: A UUID is used as `chat_id` (passed by the client or auto-created). The browser receives the session info on connect.
3. **Subscribe**: FastAPI creates NATS subscriptions scoped to this `(binding_id, chat_id)`:
   - `web.stream.{binding_id}.{chat_id}` → push `text`/`done` to browser
   - `web.typing.{binding_id}.{chat_id}` → push `thinking` / `done` to browser
4. **Message loop**: Browser sends `{ type: "message", content }`, FastAPI publishes to `web.inbound.{binding_id}`.
5. **Disconnect**: NATS subscriptions are cleaned up.

### Why Per-Connection Subscriptions?

Each WebSocket connection subscribes to subjects containing its own `chat_id`. This means:

- Messages for tab A never reach tab B (subject-level isolation)
- No need for client-side filtering
- NATS handles the routing, not application code
- Subscriptions are automatically scoped and cleaned up on disconnect

The alternative — a single wildcard subscription per binding_id with client-side dispatch — would work but adds complexity and makes it harder to reason about message delivery guarantees.

## Conversation Model

Each browser tab is identified by a `(binding_id, chat_id)` pair, and that pair maps to one row in the `conversations` table. Opening a brand-new tab without a `chat_id` query param starts a new conversation; opening a tab with `?chat_id=...` rejoins an existing one.

```
(binding_id, chat_id) → one conversation in the database
```

Conversations are persistent: history survives page reloads and shows up in the sidebar. Two REST endpoints back this:

- `GET /api/conversations?agent_id=&token=` — list conversations the requesting user can see
- `GET /api/conversations/{chat_id}/messages?agent_id=&token=` — replay history on rejoin

### Why Per-Tab Conversations (not a Shared "default" Chat)?

A fixed `chat_id` per binding (one shared conversation à la Telegram private chat) would have two problems:

- **Streaming confusion** — If two tabs share a conversation and the user sends a message from tab A, the streaming response would also appear in tab B. If the user is also typing in tab B, the experience is confusing.
- **Sidebar UX** — The product needs a ChatGPT-style sidebar of past conversations. Per-tab UUIDs map naturally to that — each conversation has its own ID, and the sidebar lets users switch between them.

The trade-off (page reload loses the *active tab's* in-flight WebSocket session) is paid only for that one tab; the conversation itself is in the DB and reopens via the sidebar.

## Authentication

Auth lives behind a pluggable `AuthProvider` (External JWT / Builtin / OIDC). The WebUI process configures the provider via `AUTH_MODE` and exposes the surface the browser actually talks to.

There are three valid auth paths into the WebUI, in order of priority:

### 1. URL `?token=...` (SaaS embed + dev)

The browser opens with `?agent_id=<uuid>&token=<jwt-or-bootstrap>` in the URL. FastAPI's `authenticate_ws(token)` handler resolves it through:

1. **Bootstrap admin shortcut** — if `token == ADMIN_BOOTSTRAP_TOKEN` (env var), the request is accepted as the `default` tenant's owner. This is the dev / first-run / smoke-test path.
2. **Configured `AuthProvider`** — otherwise the token is verified by the active provider (External JWT validates a SaaS-issued JWT; Builtin checks a RoleMesh-issued credential; OIDC validates the IdP-issued `id_token`).

The token is forwarded to the orchestrator inside the agent's `AgentInitData` so MCP tool calls can carry the user's identity downstream — see [`auth-architecture.md`](auth-architecture.md) and [`external-mcp-architecture.md`](external-mcp-architecture.md).

### 2. OIDC PKCE login (`AUTH_MODE=oidc`)

When OIDC is configured, the WebUI process registers the `oidc_routes` router, which exposes:

| Endpoint | Purpose |
|---|---|
| `GET /api/auth/config` | Frontend reads IdP discovery (issuer, authorization_endpoint, client_id, audience, scope) to start login |
| `POST /api/auth/exchange` | PKCE code → `id_token` + httpOnly refresh cookie |
| `POST /api/auth/refresh` | Refresh cookie → fresh `id_token` (called by `scheduleRefresh` 5 min before expiry) |
| `POST /api/auth/logout` | Clears the refresh cookie |

The frontend (`web/src/services/oidc-auth.ts`) drives the flow: `fetchAuthConfig()` → `startLogin()` (generates PKCE verifier+challenge, redirects to IdP) → callback page captures the code → `handleCallback()` exchanges via `/api/auth/exchange` → `id_token` lands in `sessionStorage`, refresh cookie lands httpOnly. `scheduleRefresh()` sets a timer for silent re-issue before expiry.

The IdP-issued tokens are mirrored into a host-side `TokenVault` so the orchestrator (and external MCP servers) can call APIs on behalf of the user without exposing the refresh material to the agent container.

### 3. Stored token replay

If the OIDC flow stored a token in `sessionStorage` and it's not expired, the SPA resolves auth without a fresh login (see `app.ts:resolveAuth()`).

The full identity model — user roles, agent permissions, IdP integration choices — lives in [`auth-architecture.md`](auth-architecture.md). The WebUI process is a transport for those concepts; it does not own them.

## Beyond chat: Admin surface

The WebUI process is more than a chat panel. The same FastAPI app also serves the platform's REST + UI admin surface. Putting the admin endpoints here (rather than in the orchestrator) is a direct extension of the "Why a Separate Process" decision: HTTP concerns belong on the HTTP side.

The admin surface is grouped by module — each group's authoritative documentation lives in its own file:

| Surface | Endpoints | Owning doc |
|---|---|---|
| Conversation history | `GET /api/conversations`, `GET /api/conversations/{chat_id}/messages` | This file |
| Coworker / agent CRUD | `/api/admin/agents/*` (incl. skills CRUD nested under each agent) | [`auth-architecture.md`](auth-architecture.md), [`skills-architecture.md`](skills-architecture.md) |
| Safety rules | `/api/admin/safety/checks`, `/safety/rules`, `/safety/decisions`, `/safety/decisions.csv`, `/safety/rules/{id}/audit` | [`safety/safety-framework.md`](safety/safety-framework.md) |
| Approval policies + decisions | `/api/admin/approval/*` | [`approval-architecture.md`](approval-architecture.md) |
| OIDC auth | `/api/auth/{config,exchange,refresh,logout}` | "Authentication" above |

The frontend mounts these as hash-routed pages alongside the chat (`#/admin/safety/rules`, `#/admin/safety/decisions`, …). Hash routing avoids needing a SPA history-API fallback in the FastAPI static handler — the same `index.html` works for the dev server (Vite at port 5173) and the FastAPI static mount (port 8080) without configuration drift.

## Frontend

The frontend is built with Lit (Web Components), Vite, and Tailwind CSS. Routing is hash-based; admin pages are mounted at `#/admin/...` alongside the chat panel.

### Why Lit, Not React/Vue?

- **Lightweight** — Lit compiles to native Web Components. No virtual DOM, no framework runtime. The entire frontend builds to ~70KB gzipped.
- **No build complexity** — No JSX transform, no special compiler plugins. Just TypeScript + standard DOM APIs.
- **Embeddable** — Web Components can be dropped into any existing page. When third-party SaaS platforms embed RoleMesh, they can use `<rm-chat-panel>` as a custom element without framework conflicts.

### Why Tailwind in a Web Components Project?

Lit components typically use Shadow DOM with scoped CSS. We render to the light DOM instead (`createRenderRoot() { return this; }`), which lets Tailwind's global utility classes work normally. The trade-off is losing Shadow DOM encapsulation; since the WebUI is a standalone page (not embedded inside another app's CSS), this is acceptable.

## Summary of Trade-offs

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Process model | Independent FastAPI | Embedded in orchestrator | SaaS integration, API-first, independent scaling |
| Communication | NATS | Direct DB / function calls | Real-time streaming, consistency, alignment with existing IPC |
| Streaming | Per-chunk NATS publish | Complete text via send_message | Real-time UX, like ChatGPT |
| Conversation model | Per-tab UUID, sidebar-listed | Shared fixed ID | Avoids streaming conflicts, sidebar UX |
| Frontend framework | Lit (Web Components) | React / Vue | Lightweight, embeddable |
| Routing | Hash-based | History API | No SPA fallback needed across dev / static-mount |
| Auth | OIDC PKCE + AuthProvider abstraction | Single shared API token | Multi-tenant, IdP-integrated, token rotation |

## Related documentation

- [`auth-architecture.md`](auth-architecture.md) — `AuthProvider` abstraction, agent + user permissions, OIDC details
- [`nats-ipc-architecture.md`](nats-ipc-architecture.md) — orchestrator-side NATS protocol; `web-ipc` stream lives alongside `agent-ipc`
- [`safety/safety-framework.md`](safety/safety-framework.md) — endpoints behind the safety admin pages
- [`approval-architecture.md`](approval-architecture.md) — endpoints behind the approval admin pages
- [`skills-architecture.md`](skills-architecture.md) — endpoints behind the per-agent skills CRUD
