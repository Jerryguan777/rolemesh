# Steering: Stop and Follow-up Architecture

This document explains RoleMesh's "steering" feature — giving the user control over an in-flight agent turn. Two capabilities:

1. **Stop button**: interrupt the agent's current turn without killing its container, so the user can immediately redirect with a new message.
2. **Follow-up while running**: type and send new messages while the agent is still generating a response to a previous one.

This doc covers the WebUI-side UX, the control-plane signal design, and the reasons we made some non-obvious choices. For the progress-events half of the story (`container_starting`, `running`, `tool_use` — the *output* from agent to user), see [`10-event-stream-architecture.md`](10-event-stream-architecture.md). The two features share a NATS protocol surface but solve different problems.

---

## Background: "The Agent Is Stuck — Let Me Redo That"

Before steering, a user who realized the agent was heading the wrong way had exactly one option: wait. The "thinking…" animation would run for 30–60 seconds while the agent finished its misguided turn, and only then could they type again. The input box was disabled during generation (`<textarea ?disabled=${this.isStreaming}>`).

This mirrored ChatGPT's original behavior circa 2022, but by now every major agent UI (ChatGPT, Claude.ai, Cursor, Copilot) supports:

- A Stop button to abort the current generation
- Typing the next message while the current one is still producing

We needed these too. The question was *how* — and the answer has interesting constraints because RoleMesh runs agents in Docker containers with several seconds of cold-start cost, which rules out the naïve "kill and restart" pattern.

---

## Design Goals

1. **Stop must not cost a cold-start.** A user clicks Stop because they want to redirect, not to walk away. If the next message they type requires a container respawn (image pull, SDK init, MCP connect, session resume), that's 5–10 seconds of dead air — worse UX than just waiting for the wrong turn to finish.
2. **UI must not lie.** When the user clicks Stop, the button should communicate "asking the server to stop" until the server confirms, not "stopped" immediately. Abort is best-effort and can take seconds inside the agent.
3. **Follow-up must not require new infrastructure.** Telegram and Slack users already type multiple messages during a turn; the orchestrator already handles this path. The WebUI change should only be "unblock the send button" plus whatever UX polish is needed.
4. **No new authentication surface.** The Stop path must not introduce new attacker-controlled inputs. A compromised browser should not be able to Stop someone else's conversation.
5. **Backend-agnostic.** The Stop protocol must work identically for Pi and Claude SDK agents, since the user doesn't know or care which is running.

---

## Stop: The `interrupt` Signal vs. `shutdown`

### The existing `shutdown` signal was tempting — and wrong

RoleMesh already had `GroupQueue.request_shutdown(group_jid)`, which publishes to `agent.{job_id}.shutdown`. Inside the container, the handler:

1. `abort`s the current turn
2. **Breaks out of the main `while True:` loop**, causing the container process to exit

This is the right behavior for its three original callers — `IDLE_TIMEOUT` expiry, scheduler preemption for a task container, and graceful shutdown. All three share the intent "we're done with this container for now, reclaim the resources."

The first draft of this feature reused `request_shutdown` for the Stop button. It passed E2E tests. But the user's **next** action — typing a follow-up message — had to cold-start a fresh container, because the previous one had exited. That's 5–10 seconds of latency the user didn't ask for, and it violates Goal 1.

### The fix: two signals, one bit of difference

We added a parallel signal, `agent.{job_id}.interrupt`, that diverges from `shutdown` in exactly one line:

```python
# agent_runner/main.py

async def handle_shutdown(msg: Any) -> None:
    await msg.respond(b"ack")
    shutdown_received.set()           # ← main loop detects this → break

async def handle_interrupt(msg: Any) -> None:
    await msg.respond(b"ack")
    log("Interrupt signal received, aborting current turn")
    await backend.abort()             # ← no shutdown_received; loop continues
```

Both invoke `backend.abort()` — the expensive part (Pi's `session.abort()` waits for the agent to become idle; Claude's `stream.end()` closes the SDK input). The single difference is whether the `while True` loop also exits.

After an interrupt, the container:

- Finishes draining the current turn (may take seconds if a tool is running; abort is not preemptive)
- Returns to the "waiting for next input" branch of the main loop
- Continues to hold its Pi `AgentSession` / Claude `query()` session, MCP connections, workspace mounts, and credential proxy

When the user types their next message, it flows through the normal follow-up path (see below) and hits a warm container.

The wire-level differences (JetStream for `interrupt` vs Core NATS request-reply for `shutdown`, and why) are documented in [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) Channel 3.

### Naming: `stop` (product) vs `interrupt` (system)

| Layer | Term |
|---|---|
| User-facing button | **Stop** |
| WebSocket message type | `{type: "stop"}` |
| FastAPI → NATS subject | `web.stop.{binding_id}.{chat_id}` |
| NATS → agent subject | `agent.{job_id}.interrupt` |
| Backend method | `backend.abort()` |

The split is deliberate. **Stop** is the product concept users and product managers talk about. **Interrupt** is the system concept matching Unix / Jupyter conventions for "pause current activity, return to ready." This distinguishes it from `shutdown` (close container) and from `cancel` (which has HTTP/connection-teardown overtones in most libraries).

---

## The Three-State UI

### Why three, not two

A two-state button (Send ↔ Stop) with optimistic Stop confirmation would feel snappy but lies to the user. The backend's `abort()` isn't instantaneous:

- Claude SDK: `stream.end()` closes the input stream but the SDK keeps draining current tool output until it completes.
- Pi: `session.abort()` internally calls `wait_for_idle()`. If the current tool is `bash sleep 60`, abort waits for bash to finish — 60 seconds.

During this window the agent may still emit `tool_use`, `text`, `done` events. If the UI had already flipped back to Send mode, the user could:

- Click Stop again — on a turn that isn't running. No-op on the server but confusing.
- Send a new message — which races with the abort. The new message may arrive before the previous turn has cleared, violating the user's mental model of "I stopped the old one first."

We picked three states:

```
idle ── user sends ──► running ── user clicks Stop ──► stopping ── server confirms ──► idle
 ▲                       │                                │
 │                       │                                │ 10s safety timer
 └───────────────────────┴────────────────────────────────┘
```

| State | Button appearance | Enabled? | Textarea |
|---|---|---|---|
| `idle` | Brand-colored ↑ (send) | Only if text present | Accepts input |
| `running` | Dark ■ (stop) | Always | Accepts follow-up input |
| `stopping` | Dark half-opacity ■ + spinning ring | **Disabled**, cursor `wait` | Accepts input |

The textarea stays enabled across all three states so the user can be composing their next message while waiting for the abort to complete.

### Entry and exit conditions

`stopping` is entered optimistically — the moment the user clicks Stop, the UI transitions without waiting for the server. This is safe because:

- The button becomes disabled, so the user can't click again.
- The transition out is driven by *any* of four server-side events (see below), so a stuck `stopping` state is self-healing.

Exits from `stopping`:

| Event | Why it works |
|---|---|
| `{type: "status", status: "stopped"}` | Happy path — server confirmed the abort |
| `{type: "done"}` | The current turn ran to natural completion before the abort took effect. Acceptable — the user sees the normal final output rather than being cut off mid-response |
| `{type: "error"}` | Something broke; treat as terminal |
| 10s safety timer | Server never confirmed (container crash, NATS partition, agent stuck). UI unwedges itself rather than requiring a page reload |

### State-machine hardening against late progress events

Claude and Pi can emit `tool_use` or `running` *after* the user has clicked Stop but before the abort takes effect. A naïve implementation would reset `stopping` back to `running` when these events arrive, causing the button to flicker.

The guards in `chat-panel.ts` `handleMessage`:

```typescript
case 'status':
  if (msg.status === 'stopped') {
    // Happy path out of stopping.
    this.clearStoppingTimer();
    this.agentState = 'idle';
    break;
  }
  // Any other status (tool_use, running, etc.) updates the status bar
  // but does NOT reset 'stopping' back to 'running'. Only idle can
  // elevate to running — stopping is sticky until terminal.
  this.agentStatus = { status: msg.status, tool: msg.tool, input: msg.input };
  if (this.agentState === 'idle') {
    this.agentState = 'running';
  }
  break;
```

The rule is: **progress events can only elevate from `idle` to `running`, never from `stopping` back to `running`.** This is a one-way door during the abort window.

---

## Stop Flow End-to-End

```
Browser (chat-panel.ts)
  click handler → state = 'stopping' (optimistic, synchronous)
  → client.stop()
  │
  ▼
WebSocket: {"type": "stop"}
  │
  ▼
ws.py (handle_ws receive loop)
  uses authenticated binding_id + chat_id from the WS handshake
  (NEVER reads these from the client payload — IDOR guard)
  → _js.publish("web.stop.{binding_id}.{chat_id}", b"{}")
  │
  ▼
NATS subject: web.stop.{binding_id}.{chat_id}
  │
  ▼
WebNatsGateway._stop_listener (orchestrator)
  parses subject parts → invokes _on_stop(binding_id, chat_id)
  │
  ▼
main.py._handle_web_stop
  find_conversation_by_binding_and_chat → conv
  _queue.interrupt_current_turn(conv.id)
  │
  ▼
GroupQueue.interrupt_current_turn (scheduler.py)
  guard: if not state.active or not state.job_id: return
  else: publish NATS request to agent.{job_id}.interrupt
  │
  ▼
agent_runner.handle_interrupt (container)
  ack, then await backend.abort()
  │
  ▼
Backend.abort() emits StoppedEvent
  Pi:     await session.abort()       → emit
  Claude: stream.end()                → emit
  │
  ▼
NATS bridge → ContainerOutput(status="stopped")
  published to agent.{job_id}.results
  │
  ▼
Orchestrator _on_output (rolemesh/main.py)
  status == "stopped" branch:
    gw.send_status(binding, chat, {"status": "stopped"})
    gw.send_stream_done(binding, chat)
    _queue.notify_idle(conv.id)
  │
  ▼
NATS subject: web.stream.{binding_id}.{chat_id}
  │
  ▼
ws.py._forward_stream → WebSocket frames:
  {type:"status", status:"stopped"} and {type:"done"}
  │
  ▼
Browser handleMessage('status'/'stopped')
  → clearStoppingTimer; state = 'idle'; button = ↑
```

The full round-trip runs in 200ms to a few seconds, dominated by how long the agent takes to actually honor the abort inside its current tool or generation.

---

## Security: Subject-Based Authorization

The Stop path is a new attacker surface: any compromised browser could in principle publish to *any* `web.stop.*.*` subject it wants. We mitigate this with a strict rule in `ws.py`:

**The browser's payload is ignored entirely.** The `binding_id` and `chat_id` encoded in the NATS subject are captured from the authenticated WebSocket handshake and never re-read from the client:

```python
elif data.get("type") == "stop":
    # Do NOT use data.get("chat_id") / data.get("binding_id") from
    # the payload — always use the authenticated binding_id/chat_id
    # from the WebSocket handshake to prevent IDOR from a compromised
    # or malicious client.
    await _js.publish(f"web.stop.{binding_id}.{chat_id}", b"{}")
```

The body is `b"{}"` — a deliberate marker that there is nothing to parse. The orchestrator's `WebNatsGateway._stop_listener` reads only `msg.subject`, never `msg.data`. A compromised browser can only Stop conversations it's already authenticated to.

This design choice means the subject itself *is* the authorization token. If we ever needed per-signal permissions (e.g. "admin can Stop any agent"), we'd need a different mechanism. For now, "you can Stop what you're connected to" is the entire policy.

---

## Follow-up Messages: Wiring That Already Existed

Follow-ups required **zero backend changes**. Telegram / Slack users already send multiple messages during a turn, and the orchestrator's message loop handles this:

```
Browser types during running turn
  ↓ WebSocket {type: "message", content: "..."}
  ↓ ws.py → NATS web.inbound.{binding_id}
  ↓ _handle_incoming → DB store + enqueue_message_check
  ↓ (state.active=True → state.pending_messages=True; no-op on schedule)
  ↓
  _message_loop polls every POLL_INTERVAL (2s)
  ↓ get_messages_since finds new messages
  ↓ _queue.send_message(conv_id, formatted_text)
  ↓ NATS publish agent.{job_id}.input
  ↓
  Container's poll_nats_during_query receives
  ↓ backend.handle_follow_up(text)
  ↓
  Pi:     session.prompt(text, streaming_behavior="followUp")
  Claude: self._stream.push(text)
```

The only WebUI change was removing the `if (this.isStreaming) return` guard in `message-editor.ts`:

```typescript
private handleSend() {
  // Follow-up messages are allowed even while the agent is running.
  // The orchestrator queues them for after the current turn.
  if (!this.value.trim()) return;  // ← removed the isStreaming check
  ...
}
```

### Known latency: 0–2.5 seconds

Because the path goes through `_message_loop`'s 2-second polling tick, follow-ups have a latency floor:

- 0–2 s waiting for the next poll tick
- ~50–100 ms DB read + format
- 0–500 ms for the container's `input_sub.next_msg(timeout=0.5)` cycle

**Total: 0–2.5 seconds, ~1 second average.** This is not a regression — it's how Telegram / Slack have always worked.

A fast-path that lets `_handle_incoming` bypass the polling loop is feasible (call `_queue.send_message` directly when `state.active`) but would duplicate formatting logic and wasn't in scope. If this latency becomes a user complaint, that's where to optimize.

### `followUp` vs `steer`: the Pi mode choice

Pi's `AgentSession.prompt()` has two modes for mid-turn messages:

- **`followUp`** — queue the message for *after* the current turn completes. Pi's internal queue processes it as the next user turn. This is what we use.
- **`steer`** — interrupt the agent *mid-run* and inject the message into the current turn. The agent sees it as a correction and incorporates it immediately.

`followUp` matches Claude SDK behavior (a pushed message enters the input stream and is consumed after the current turn idles) and is predictable to the user: "my new message will be seen after this one finishes."

`steer` is more powerful but also more surprising. A user who types "actually forget the first request" while the agent is drafting a long response would get different behavior than when typing the same text after the response arrives. That kind of user-invisible mode split is a footgun. Steering deserves its own UI affordance (a different button, or a modifier key) before being exposed.

### A known Pi bug: msg1 response can be lost

There is a pre-existing issue in Pi's backend that follow-ups expose more visibly:

`PiBackend.run_prompt` stores the last `TurnEndEvent`'s text in `self._last_result_text` and emits **one** `ResultEvent` at the end of `session.prompt()`. If msg1 is being processed and msg2 arrives as a follow-up, Pi processes both in sequence, but `_last_result_text` is overwritten by msg2's response — **msg1's response is never emitted to the client.**

Claude is unaffected because its `run_prompt` emits a `ResultEvent` per SDK `ResultMessage`, so multiple turns produce multiple results.

Fixing this requires emitting `ResultEvent` inside Pi's `_handle_event` on each `TurnEndEvent` rather than once at the end, plus reasoning about the downstream `notify_idle` consequences (there's a reason the comment in `pi_backend.py` says this was deferred originally). Orthogonal to steering, but worth noting here because steering makes the scenario more likely to occur.

---

## The Orchestrator's Cold-Start Race

`GroupQueue.interrupt_current_turn` has a guard:

```python
def interrupt_current_turn(self, group_jid: str) -> None:
    state = self._get_group(group_jid)
    if not state.active or not state.job_id:
        return   # silent no-op
    ...
```

`state.active` is set to `True` immediately when `_run_for_coworker` is called. But `state.job_id` is set later, by `register_process(container_name, job_id)`, which `ContainerAgentExecutor` invokes **after** the container has been created (via `on_process(...)` callback).

There's a window — typically 0.5–3 seconds during cold start — where `state.active=True` but `state.job_id=None`. A Stop click inside this window hits the guard and silently returns. The user sees:

- Button goes to `stopping` (optimistic client-side)
- Server produces no effect
- 10s safety timer fires → button returns to `idle`
- But the agent runs to natural completion

This is accepted as a rare case. A robust fix would store a pending `interrupt` flag on `_GroupState`, check it in `register_process`, and fire the signal once `job_id` becomes available. Not done today; worth doing if reports come in.

---

## Event Ordering: `stopped` vs `done`

When Pi's `backend.abort()` runs, two coroutines race to emit events:

- **`abort()` path**: `await session.abort()` (which itself waits for idle) → emit `StoppedEvent` → orchestrator publishes `status: stopped` and a `done` frame.
- **`run_prompt()` path**: `await session.prompt()` returns when idle → emit `ResultEvent` with `_last_result_text` → orchestrator publishes `text` (if any) and a `done` frame.

Both fire when the agent enters idle. We observed in E2E that the `ResultEvent` path usually wins: the client sees `text → done → status:stopped → done` rather than the clean `status:stopped → done`.

The frontend tolerates both orders:

- `text → done → stopped → done` (what we see): `done` transitions to `idle`; the late `status:stopped` finds state already idle and is a harmless no-op.
- `stopped → done → done` (theoretical clean order): `status:stopped` transitions to idle; the `done` is a no-op.

We intentionally did not serialize the two paths in the backend:

1. Serializing means either blocking `run_prompt`'s final emit on the abort completing, or vice versa. Either adds complexity.
2. The extra `text` event before `stopped` is *informative* — the user sees Pi's natural-language wrap-up message ("The command was aborted before it could complete. Would you like to try…"). That's actually good UX.
3. The double `done` is detectable and trivially idempotent.

We accepted the order as "close enough" and spent the complexity budget on the three-state UI instead.

---

## Known Limitations

1. **Abort is not preemptive inside tools.** If the agent is running `bash sleep 60`, the abort waits for bash to complete. "Stopping…" can last up to 60 seconds depending on the tool.
2. **Cold-start Stop silently no-ops.** See the race section above.
3. **Stop is chat-scoped, not tab-scoped.** Multiple tabs on the same `(binding_id, chat_id)` share a container. One tab's Stop aborts the conversation for all tabs.
4. **Follow-up latency of up to 2.5s.** See the follow-up section.
5. **No rate limiting on Stop.** A user spamming the button publishes many NATS messages; the scheduler's guard makes repeats no-ops but there's no throttle.
6. **No resume of partial output.** If Pi generated 200 characters before being aborted, those characters are in the assistant bubble but not stored in the DB (only complete assistant messages are persisted via `_on_output`). A page refresh loses the partial.

---

## Test Strategy

### Automated

Unit tests cover the protocol pieces:

- `tests/agent/test_executor.py` — `AgentOutput.is_progress()`, `TERMINAL_STATUSES` contains `stopped`
- `tests/test_agent_runner/test_event_translation.py` — `StoppedEvent` → `ContainerOutput(status="stopped")` mapping
- `tests/container/test_scheduler.py` — `interrupt_current_turn` guards (no active container, no transport)

### E2E

Validated with Playwright MCP against a full live stack (orchestrator, webui, vite, mock_mcp, Pi backend on OpenAI). Tested flows:

- Cold start → status bar → `Stop` mid-tool → `stopping` → `idle`
- State machine resistance to late `tool_use` after Stop (button does not flicker back to the Stop ■ state)
- Follow-up on a warm container (no `container_starting`, but `running` still fires per-turn so the status bar works)
- Stop + new message immediately after — confirms the container stayed warm and the second message does not incur cold-start latency

### Gap

The frontend state machine has no vitest coverage yet. All transitions were verified manually. A fixture-based test of `chat-panel.ts`'s `handleMessage` against synthetic event sequences would catch regressions cheaply. Out of scope for the initial steering commit.

---

## Summary of Trade-offs

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Stop signal | New `agent.{job_id}.interrupt` (keeps container alive) | Reuse existing `agent.{job_id}.shutdown` (terminates container) | Stop must not cost a cold-start |
| UI button states | Three (`idle` / `running` / `stopping`) | Two (`idle` / `running`) with optimistic flip-back | Abort is best-effort; UI must not lie |
| Stopping exit conditions | Four (stopped / done / error / 10s timer) | Wait only for explicit `stopped` | Event ordering is not strict; `done` is equally valid |
| State machine "stickiness" | `stopping` wins over late progress events | Progress events always update state | Prevent flicker during abort window |
| Stop signal authorization | Subject-based, payload ignored | Include chat_id in payload | IDOR guard — binding/chat only from authenticated handshake |
| Follow-up mechanism | Reuse existing Telegram / Slack message loop | New per-channel fast-path | Zero new infrastructure; familiar code path |
| Follow-up latency | Accept 0–2.5 s polling tick | Fast-path bypass of `_message_loop` | Not a user complaint yet; duplicates formatting logic |
| Pi mid-turn mode | `followUp` (queue for next) | `steer` (inject into current turn) | Predictable UX; steering deserves its own affordance |
| Backend abort API | Existing `backend.abort()`, add `StoppedEvent` emit | New abort-with-confirmation API | Existing method fits; just needed the terminal signal |
| Naming | `stop` (browser) / `interrupt` (backend) | One name throughout | Product concept vs system concept — different readers have different mental models |
| Event ordering | Accept `text → done → stopped → done` | Serialize the two emit paths in Pi | Informative `text` (the "aborted" message) is good UX; double `done` is idempotent |
| Cold-start race handling | Accept silent no-op on pre-`job_id` Stop | Queue pending interrupt in `_GroupState` | Rare case; 10s safety timer recovers UI; fix if reported |
| Partial output on abort | Keep in the assistant bubble, not persisted | Persist to DB with `is_interrupted` flag | Extra schema complexity for marginal value; existing behavior is "message arrives or doesn't" |

---

## Related documentation

- [`10-event-stream-architecture.md`](10-event-stream-architecture.md) — Progress events (`container_starting`, `running`, `tool_use`) on the same `web.stream.*` subject; UI status bar protocol
- [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) — `agent.{job}.interrupt` (JetStream) vs `agent.{job}.shutdown` (Core NATS request-reply) — why each NATS primitive
- [`backend-stop-contract.md`](backend-stop-contract.md) — observable behaviors any backend must deliver on `abort()`; Claude preemptive vs Pi cooperative cancellation
- [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md) — `StoppedEvent` and the `BackendEvent` union
