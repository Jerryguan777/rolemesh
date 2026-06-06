# RoleMesh Realtime Channel — WebSocket Protocol

Third-party integration contract for RoleMesh's realtime chat channel.

> **Source of truth.** This document is hand-written prose and is **not**
> covered by a drift test. When it disagrees with code, the code wins —
> specifically:
>
> - **Frame shapes** are defined as machine-readable schemas in
>   [`contracts/openapi.yaml`](./openapi.yaml) under `components/schemas`
>   (the `Ws*` block). Those schemas *are* drift-protected: they generate
>   `web/src/api/generated/types.ts` and are mirrored by Pydantic models in
>   `src/webui/schemas_v1.py` (`WsServerEventModel` / `WsClientFrameModel`),
>   with `tests/test_openapi_codegen_freshness.py` and
>   `tests/test_openapi_contract.py` failing on divergence.
> - **Connection behaviour** (handshake, close codes, lifecycle, semantics)
>   is implemented in `src/webui/v1/ws_stream.py`. This document cites the
>   relevant functions/line ranges so you can verify against the
>   implementation.
>
> This file documents only the **v1 canonical surface**
> (`/api/v1/conversations/{id}/stream`). The legacy `/ws/chat` endpoint is
> being retired and is intentionally undocumented.

---

## 1. Overview

RoleMesh's public API splits across two contracts in this directory:

| Contract | Covers | Format |
|---|---|---|
| [`openapi.yaml`](./openapi.yaml) | REST — every `/api/v1/*` HTTP endpoint and the JSON it exchanges | OpenAPI 3.1 |
| **`websocket-protocol.md`** (this file) | Realtime chat — the WebSocket channel for streaming runs, tokens, and HITL approvals | Markdown + `$ref` into `openapi.yaml` |

OpenAPI models HTTP, not channels — but every WebSocket frame is plain JSON,
so the **frame payloads themselves live in `openapi.yaml`** (the `Ws*`
schemas). This document supplies what OpenAPI cannot express: the handshake
sequence, connection lifecycle, close-code semantics, and end-to-end frame
ordering a client must implement.

If you only want to build a realtime chat UI, this file plus the `Ws*`
schemas in `openapi.yaml` are everything you need.

---

## 2. Handshake

The WebSocket upgrade is authenticated by a **short-lived, conversation-bound
JWT ticket** carried as a query parameter — not by the long-lived API session
token. Two steps:

### Step 1 — Mint a ticket (REST)

```
POST /api/v1/auth/ws-ticket
Authorization: <your normal API session credential>
Content-Type: application/json

{ "conversation_id": "<uuid>" }
```

Request body: [`WsTicketRequest`](./openapi.yaml) · Response:
[`WsTicket`](./openapi.yaml) — `{ "ticket": "<jwt>", "expires_in_s": <1..60> }`.

The ticket is bound to exactly one `conversation_id` and expires in **≤ 60
seconds** (`src/rolemesh/auth/ws_ticket.py`, `issue_ws_ticket`). It is a
one-shot handshake credential, not a session: once the socket is open the
connection is authenticated for its lifetime. The endpoint verifies you are a
member of the conversation before signing; a conversation you can't see
returns `404 NOT_FOUND` (existence is not leaked) — see
`src/webui/v1/auth.py` `post_ws_ticket`.

### Step 2 — Open the socket

```
WS /api/v1/conversations/{conversation_id}/stream?ticket=<jwt>
```

The `conversation_id` in the path **must match** the one the ticket was minted
for. The server compares ticket → path before any DB work and accepts the
socket only on success (`ws_stream.py` `_verify_handshake` :102, then
`stream` :339). The ticket must be passed as the `?ticket=` query parameter
(`Query` arg, `ws_stream.py:342`); there is no header-based variant.

---

## 3. Connection Lifecycle

### Close codes

Authentication and setup failures **close the socket** with an RFC 6455
private-range code (4000–4999) instead of accepting it. The codes are split
so a client can branch on the reason without re-reading any body
(`ws_stream.py:77-80`, `_verify_handshake` :102-131, `stream` :350-394):

| Code | Reason (label) | Meaning | Client action |
|---|---|---|---|
| `4001` | `WS_TICKET_EXPIRED` | Ticket's `exp` passed (≤ 60s window). | Re-mint a ticket (step 1) and reconnect. |
| `4002` | `WS_TICKET_INVALID` | Ticket missing, malformed, or bad signature. | Re-mint and reconnect; if it persists, your credential/secret config is wrong. |
| `4003` | ticket conversation mismatch | Ticket is valid but was minted for a different `conversation_id` than the path. | **Do not retry as-is** — client bug: mint the ticket for the conversation you're connecting to. |
| `4004` | conversation not found | Conversation doesn't exist, isn't in your tenant, or the id is malformed. Deliberately converged so existence isn't leaked. | **Do not retry** — you don't have access. |
| `1011` | server error | Server-side fault (e.g. JetStream not initialised). | Transient; back off and retry. |

A socket that **accepts successfully** then stays open until either side
closes it. Normal client-initiated close uses standard code `1000`.

### Keepalive

There is **no application-level ping/pong or heartbeat frame** in the
protocol. Rely on the transport's standard WebSocket ping/pong and your
client library's idle handling.

### Reconnect — fetch truth, don't replay

The server **does not replay history** on connect. After any reconnect, fetch
the current state of any run you care about via REST:

```
GET /api/v1/runs/{run_id}
```

This is the authoritative source for a run's status and result. The WebSocket
only pushes *live* events from the moment you connect (JetStream
`DeliverPolicy.NEW`, `ws_stream.py:400`).

---

## 4. Frame Envelope

Every frame — both directions — is a JSON object discriminated on a `type`
string:

```json
{ "type": "<discriminator>", "...": "..." }
```

Both directions are modelled as discriminated unions in `openapi.yaml`:
[`WsClientFrame`](./openapi.yaml) (client → server) and
[`WsServerEvent`](./openapi.yaml) (server → client). The sections below
summarise each frame and link to its schema; **the schema is authoritative
for field names, types, and optionality.**

---

## 5. Client → Server Frames

Four frame types. Send them as text frames containing JSON.

### `request.run` — start an agent run

Schema: [`WsClientFrameRequestRun`](./openapi.yaml). Handler:
`ws_stream.py` `_handle_request_run` :665.

| Field | Required | Notes |
|---|---|---|
| `type` | yes | `"request.run"` |
| `input` | yes | Non-empty user input string. |
| `idempotency_key` | **yes** | Client-minted UUID4, one per send. |

**`idempotency_key` is mandatory.** The server caches
`(conversation_id, idempotency_key) → run_id` for the life of the run. A
duplicate frame within the dedup window returns the **same `run_id`** and is
**not re-published** to the orchestrator; the resulting `event.run.started`
carries `idempotent: true`. This makes a reconnect-and-resend safe.

On success the server replies with `event.run.started`. On validation failure
it replies with `event.run.error`:

- missing/empty `input` → code `PROTOCOL_MISSING_INPUT`
- missing/empty `idempotency_key` → code `PROTOCOL_MISSING_IDEMPOTENCY_KEY`

Example:

```json
{ "type": "request.run", "input": "Summarise the latest deploy log", "idempotency_key": "f1c2…UUID4" }
```

### `request.cancel` — cancel a run

Schema: [`WsClientFrameRequestCancel`](./openapi.yaml). Handler:
`_handle_request_cancel` :833.

| Field | Required | Notes |
|---|---|---|
| `type` | yes | `"request.cancel"` |
| `run_id` | yes | The run to cancel. |

Fire-and-forget: the server relays the cancel to the orchestrator, which
performs the actual `status='cancelled'` write. The WS handler never writes
terminal status itself. Missing `run_id` → `event.run.error` code
`PROTOCOL_MISSING_RUN_ID`. This is equivalent to
`POST /api/v1/runs/{id}/cancel`.

### `request.stop` — interrupt the current turn

Schema: [`WsClientFrameRequestStop`](./openapi.yaml). Handler inline at
`ws_stream.py:612-625`.

| Field | Required | Notes |
|---|---|---|
| `type` | yes | `"request.stop"` |
| `run_id` | no | **Advisory only** — logged, not used for routing. |

Interrupts the currently-running agent turn for this conversation. The target
container is identified from the authenticated handshake (binding + chat),
**never** from the frame payload (IDOR guard). The `run_id` is optional
because the client may click Stop before it has seen the first
`event.run.started`.

### `request.approval_decision` — resolve a HITL approval

Schema: [`WsClientFrameApprovalDecision`](./openapi.yaml). Handler:
`_handle_approval_decision` :775.

| Field | Required | Notes |
|---|---|---|
| `type` | yes | `"request.approval_decision"` |
| `request_id` | yes | Echoes the `request_id` from an `event.approval.requested`. |
| `decision` | yes | `"approve"` or `"reject"`. |
| `note` | no | Optional free-text rationale shown to the agent. |

This is how a client **responds** to an approval request (see §8). The
approver's identity is **server-stamped from the verified ticket**, never
taken from the frame (IDOR guard) — a browser cannot forge who approved.
Errors: missing `request_id` → `PROTOCOL_MISSING_REQUEST_ID`; `decision` not
in `{approve, reject}` → `PROTOCOL_BAD_DECISION`.

### Frame-level protocol errors

Independent of the frame type, the server replies with `event.run.error`
(no `run_id`) when a received frame can't be parsed or routed
(`ws_stream.py:587-596`, :635-643):

- non-JSON text → code `PROTOCOL_BAD_JSON`
- unknown/missing `type` → code `PROTOCOL_UNKNOWN_TYPE`

---

## 6. Server → Client Frames

Eight event types, all members of the [`WsServerEvent`](./openapi.yaml)
discriminated union. **Field-level contract is in `openapi.yaml`** — this
table summarises each and links to its schema. Cross-references point at the
builder/send site in `ws_stream.py` for drift checks.

| `type` | Schema | Builder / send site | Summary |
|---|---|---|---|
| `event.run.started` | [`WsServerEventRunStarted`](./openapi.yaml) | `_handle_request_run` :764 | Ack of `request.run`. `{ run_id, idempotent }`. `idempotent: true` ⇒ this re-used an in-flight run. |
| `event.run.token` | [`WsServerEventRunToken`](./openapi.yaml) | `_forward_stream` :453 | Streamed assistant text. `{ run_id, delta }` — `delta` is the new text since the last token (possibly empty string). |
| `event.run.progress` | [`WsServerEventRunProgress`](./openapi.yaml) | `_build_progress_frame_or_none` :166 | `{ run_id, status, tool?, input_preview? }`. `status` ∈ `running`/`tool_use`/`queued`/`container_starting` (open string). `tool` + `input_preview` present only for `tool_use`. |
| `event.run.completed` | [`WsServerEventRunCompleted`](./openapi.yaml) | `_forward_stream` :480 | Run finished. **`{ run_id }` only — no `usage` on the wire** (usage is written server-side to the run row; read it via `GET /api/v1/runs/{id}`). |
| `event.run.error` | [`WsServerEventRunError`](./openapi.yaml) | multiple | `{ code, message, run_id?, details? }`. `run_id` omitted for protocol-level errors fired before a run exists. |
| `event.message.appended` | [`WsServerEventMessageAppended`](./openapi.yaml) | `_build_outbound_frame` :147 | Out-of-band agent message (e.g. scheduled-task reply). `{ content, source, timestamp }` — **deliberately no `run_id`**; render like a REST-fetched message. |
| `event.approval.requested` | [`WsServerEventApprovalRequested`](./openapi.yaml) | `_build_approval_frame_or_none` :209 | A HITL approval card. `{ request_id, … }` plus decision context (`tool_name`, `params`, `rationale`, `expires_at`, `triggered_by`, etc.). `triggered_by` `$ref`s [`ApprovalTriggeredBy`](./openapi.yaml). |
| `event.approval.resolved` | [`WsServerEventApprovalResolved`](./openapi.yaml) | `_build_approval_frame_or_none` :279 | Terminal approval state. `{ request_id, outcome }`, `outcome` ∈ `approved`/`rejected`/`expired`/`cancelled`. |

**`event.run.error` codes** emitted by this endpoint: `SAFETY_BLOCKED`,
`PROTOCOL_BAD_JSON`, `PROTOCOL_UNKNOWN_TYPE`, `PROTOCOL_MISSING_INPUT`,
`PROTOCOL_MISSING_IDEMPOTENCY_KEY`, `PROTOCOL_MISSING_RUN_ID`,
`PROTOCOL_MISSING_REQUEST_ID`, `PROTOCOL_BAD_DECISION`. Treat `code` as an
open set and fall back gracefully on unknown values.

---

## 7. Semantic Conventions

These behaviours are load-bearing for a correct client (`ws_stream.py` module
docstring :1-31, and `:644-648`):

- **Disconnect does *not* cancel a run.** Closing the WebSocket (tab close,
  network drop) leaves any in-flight run running. Only an explicit
  `request.cancel` / `POST /api/v1/runs/{id}/cancel` cancels it. The agent
  finishes its work regardless of whether anyone is listening.
- **Reconnect fetches truth.** The server never replays missed events. After
  reconnecting, call `GET /api/v1/runs/{run_id}` for the authoritative state
  and `GET /api/v1/conversations/{id}/messages` for persisted messages.
- **`run_id` correlates the stream.** Server `event.run.*` frames are a thin
  projection of the orchestrator's `web.stream.{binding_id}.{chat_id}` topic,
  keyed by the active `run_id` returned in `event.run.started`. Use it to
  route tokens/progress/completion to the right run on your side.
- **Out-of-band events carry no `run_id`.** `event.message.appended` and the
  `event.approval.*` frames are independent of any `request.run` lifecycle
  (an approval can outlive its run; scheduled-task messages have no run).

---

## 8. End-to-End Examples

### A normal run

```text
# 1. Handshake (REST, then WS upgrade)
→ POST /api/v1/auth/ws-ticket           { "conversation_id": "C1" }
← 200                                    { "ticket": "ey…", "expires_in_s": 60 }
   (open) WS /api/v1/conversations/C1/stream?ticket=ey…   → accepted

# 2. Start a run
→ { "type": "request.run", "input": "Hello", "idempotency_key": "<uuid4>" }
← { "type": "event.run.started", "run_id": "R1", "idempotent": false }

# 3. Progress + streamed tokens
← { "type": "event.run.progress", "run_id": "R1", "status": "running" }
← { "type": "event.run.progress", "run_id": "R1", "status": "tool_use", "tool": "Read", "input_preview": "main.py" }
← { "type": "event.run.token", "run_id": "R1", "delta": "Hi" }
← { "type": "event.run.token", "run_id": "R1", "delta": " there!" }

# 4. Completion (no usage on the wire — GET /runs/R1 for it)
← { "type": "event.run.completed", "run_id": "R1" }
```

### A HITL approval round-trip

```text
# Mid-run, the agent calls a gated tool. The server pushes a card:
← { "type": "event.approval.requested",
    "request_id": "A1",
    "tool_name": "shell.exec",
    "params": { "cmd": "rm -rf build/" },
    "rationale": "Clean the build dir before rebuild",
    "expires_at": "2026-06-06T12:00:30Z" }

# The client renders the card and the user clicks Approve:
→ { "type": "request.approval_decision",
    "request_id": "A1",
    "decision": "approve",
    "note": "ok" }
   (approver identity is stamped server-side from the ticket, not this frame)

# The orchestrator resolves it and the server pushes the terminal state:
← { "type": "event.approval.resolved", "request_id": "A1", "outcome": "approved" }

# The run then continues (more tokens, eventually event.run.completed).
# If the user had not decided in time:
← { "type": "event.approval.resolved", "request_id": "A1", "outcome": "expired" }
```

---

## 9. Future

The WebSocket frame **shapes** are already machine-readable (the `Ws*`
schemas in `openapi.yaml`), already typed on the backend (Pydantic
discriminated unions in `src/webui/schemas_v1.py`), and already drift-tested
(via the OpenAPI codegen + contract tests). What remains for a future
upgrade:

- **Adopt AsyncAPI 3.0** (`contracts/asyncapi.yaml`) — OpenAPI's
  event-driven sibling — to model the *channel and subscription* semantics
  (handshake, close codes, message direction) that OpenAPI cannot express,
  enabling WebSocket-client codegen. The connection-lifecycle prose in this
  document would migrate into that spec, with a freshness test added to
  match the REST side. Until then, this Markdown is the integrator-facing
  source for connection behaviour, with `ws_stream.py` as the final
  authority.
