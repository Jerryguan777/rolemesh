# Agent Event Stream Architecture

This document explains RoleMesh's **progress event stream** — the flow of real-time status events from a running agent container to the user's browser. Progress events (`queued`, `container_starting`, `running`, `tool_use`) let the user see what the agent is doing during the 10–60 seconds between sending a message and getting a result.

The companion **Stop / follow-up (steering)** feature is closely related — they share the same NATS subject and the same UI surface — but its design is documented separately in [`steering-architecture.md`](steering-architecture.md). This document only covers the progress / status side of the protocol.

---

## Background: The Silent Agent Problem

Before progress events existed, the WebUI experience was:

```
User sends message → "thinking..." spinner → (10–60s of silence) → result
```

During the silent period the user had no way to know:

- Is the container still starting?
- Is the LLM generating?
- Is a tool running?
- Is it stuck?

Telegram / Slack users tolerate the silence because they expect chat latency. Browser users compare to ChatGPT / Claude / Cursor and expect tighter feedback.

## Scope: WebUI Only

We deliberately did **not** extend progress events to Telegram / Slack:

- Those channels have no notion of a "status bar" — the rate-limited message API can't carry multiple ephemeral updates per turn.
- Stop buttons don't exist in those UIs.
- The streaming UX that makes progress events valuable lives in the browser.

All progress-related code paths gate on `isinstance(gw, WebNatsGateway)`. The `BackendEvent` types are backend-agnostic, but the routing layer drops them silently for non-web channels.

## Design Goals

1. **Low-cost integration in existing infrastructure** — no new NATS streams, no new authentication paths, no new processes.
2. **Backend-agnostic event vocabulary** — Pi and Claude SDK must emit the same events with the same meaning, so the UI has a stable contract regardless of which engine runs inside the container.
3. **Ordered with the text stream** — `tool_use: Read src/app.ts` followed by `text: "I found..."` must arrive in that order, end-to-end.

---

## Why Not a New NATS Subject for Progress?

The obvious design would be a new subject pattern, e.g. `web.progress.{binding_id}.{chat_id}`, with its own payload type. We rejected this.

| Option | Pros | Cons |
|---|---|---|
| **New subject `web.progress.*`** | Clean semantic separation | +1 stream consumer task in orchestrator and FastAPI; +1 subscription cleanup path; ordering with `web.stream.*` not guaranteed across subjects |
| **Piggyback on existing `web.stream.*` with a `type="status"` discriminator** (chosen) | Ordering preserved (same ordered consumer); zero new infrastructure; trivially removable | `WebStreamChunk` gains a third type |

The ordering concern decided it. A user sees `tool_use: Read src/app.ts` → `text: "I found…"`. These two events must arrive **in that order**. With two separate NATS subjects, ordering depends on consumer scheduling — easy to get wrong, impossible to guarantee. With one subject, JetStream's ordered consumer gives it for free.

`WebStreamChunk` now looks like:

```python
@dataclass(frozen=True, slots=True)
class WebStreamChunk:
    type: str  # "text" | "done" | "status"
    content: str = ""   # for status: a JSON-encoded payload
```

`ws.py` unwraps the inner JSON and forwards as a typed frame:

```json
{"type": "status", "status": "tool_use", "tool": "Bash", "input": "ls /tmp"}
```

## Why Not `is_typing`?

The WebUI had a legacy `typing` channel that fired when the agent started processing. We considered overloading it — extending `WebTypingMessage` to carry a phase string. Rejected:

- `typing` maps to the legacy chat convention "the other side is typing" — a boolean presence indicator. Overloading it with phase semantics confuses both readers and future channels (e.g. a mobile app that handles typing natively).
- Keeping them separate lets us eventually deprecate `typing` without touching the progress-events protocol.

Today, `typing` still fires (the frontend uses it to spawn the empty assistant message bubble), but it carries no phase information and will not be extended.

---

## The BackendEvent Abstraction

Both Pi and Claude SDK run inside containers behind an `AgentBackend` Protocol. They emit `BackendEvent`s that the NATS bridge translates into NATS publishes. Three event types matter for progress:

- **`RunningEvent`** — emitted when `run_prompt()` (or `handle_follow_up()`) begins. Maps to UI status `running`.
- **`ToolUseEvent(tool, input_preview)`** — emitted at the start of each tool invocation. Maps to UI status `tool_use`.
- **`StoppedEvent`** — emitted when `abort()` finishes. Maps to UI status `stopped`. (Steering-related — see [`steering-architecture.md`](steering-architecture.md).)

The full `BackendEvent` union, why we extended it instead of inventing per-backend payloads, and how Claude's `SystemMessage(init)` / `AssistantMessage` blocks vs. Pi's `SessionInitEvent` / `ToolExecutionStartEvent` map onto it are documented in [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md). This document only owns what these three events mean for the UI status bar.

### The `ToolUseEvent.input_preview` design

```python
@dataclass(frozen=True)
class ToolUseEvent:
    tool: str            # "Bash", "Read", "mcp__rolemesh__send_message", ...
    input_preview: str   # "ls /tmp", "/etc/hostname", ...
```

`input_preview` is a single short user-facing string, not a structured `{args: dict}`. Reasons:

- The UI shows one line: `Bash · ls /tmp`. The display format is an emitter-side decision, not a consumer-side one.
- The emitter (backend) knows which tool input fields are interesting. `Bash` wants `command`; `Read` wants `file_path`; `Grep` wants `pattern`. The UI shouldn't need to carry this tool-specific knowledge.
- Shipping the full `args` risks leaking sensitive content (passwords, tokens, large payloads). The preview function truncates to 80 chars.

### Bug caught at E2E: MCP prefix and case

The `tool_input_preview` helper was initially written against Claude SDK's PascalCase tool names (`Bash`, `Read`). Pi uses lowercase (`bash`, `read`), and MCP tools arrive as `mcp__<server>__<tool>` — neither hit the match table, causing empty previews.

The fix strips the namespace and lowercases before matching:

```python
base = tool_name.rsplit("__", 1)[-1] if "__" in tool_name else tool_name
tn = base.lower()
if tn in ("read", "write", "edit", "glob", "grep", "notebookedit"): ...
```

Cross-backend consistency lives in this one pure helper. Adding a new tool means extending one match table.

---

## The `AgentOutput` Status Enum

On the orchestrator side, the `AgentOutput` dataclass carries the status that flows into `_on_output`. Extending this was simpler than it sounds once we committed to a rule: **every status is either a terminal or a progress indicator, never both**.

```python
# src/rolemesh/agent/executor.py
PROGRESS_STATUSES = ("queued", "container_starting", "running", "tool_use")
TERMINAL_STATUSES = ("success", "error", "stopped", "safety_blocked")

@dataclass(frozen=True)
class AgentOutput:
    status: Literal[*PROGRESS_STATUSES, *TERMINAL_STATUSES]
    result: str | None
    metadata: dict[str, object] | None = None   # tool preview payload

    def is_progress(self) -> bool:
        return self.status in PROGRESS_STATUSES
```

`safety_blocked` is the Safety Framework V2 terminal status — the pipeline intercepted the turn before it could produce output. See [`safety/safety-framework.md`](safety/safety-framework.md) for what triggers it.

The progress branch in `_on_output` is an early-return that never touches idle timers, `notify_idle`, or the "had_error" flag:

```python
async def _on_output(result: AgentOutput) -> None:
    if result.is_progress():
        if binding and isinstance(gw, WebNatsGateway):
            payload = {"status": result.status, **(result.metadata or {})}
            await gw.send_status(binding.id, conv.channel_chat_id, payload)
        return   # ← progress events do not touch terminal state
    # ... existing success/error/stopped/safety_blocked handling ...
```

Earlier reviewers asked whether to reuse the `result` string field for the tool payload (e.g. `result=json.dumps({"tool": "Bash"})`). We rejected that: `result` is a text field, overloading it makes type-safe handling harder. The new `metadata: dict` field is nominal, always optional, and never conflicts with `result`.

---

## Emit Site Placement: Where Each Event Originates

| Event | Emitter | Trigger |
|---|---|---|
| `queued` | `GroupQueue.enqueue_message_check` (orchestrator) | Cross-coworker waiting queue entered |
| `container_starting` | `GroupQueue._run_for_coworker` (orchestrator) | Transition from "nothing running" to "spawning container" |
| `running` | Backend (in container) | `run_prompt()` start AND `handle_follow_up()` start |
| `tool_use` | Backend (in container) | Claude's `AssistantMessage.content[ToolUseBlock]` or Pi's `ToolExecutionStartEvent` |
| `stopped` | Backend (in container) | `backend.abort()` completion — see steering doc |

### Why `running` fires per-turn, not per-session

Initially we emitted `RunningEvent` once at `backend.start()` (when the container's session initialized). This looked correct in cold-start tests but broke warm-container follow-ups: the second message on a still-alive container emitted no `RunningEvent`, and if that turn happened to be a simple text-only response with no tools, the UI's status bar stayed empty the entire time.

The fix is to emit `RunningEvent` at the start of every `run_prompt` and `handle_follow_up`, independent of session state. Claude SDK already re-emits `SystemMessage(init)` per `query()` call, but we emit defensively anyway — the UI contract shouldn't depend on SDK internals.

### Why progress emit sites live in the scheduler, not the executor

`container_starting` could theoretically be emitted from `ContainerAgentExecutor.execute()` — it knows when the container is starting. But the executor's semantic is "run a container and translate its output"; it's not an event source.

The scheduler (`GroupQueue`) is the correct emit site because it owns the **decision** to start a container. The first version had the emit in the executor and was flagged in review as a layering smell. The `set_on_queued` / `set_on_container_starting` callback injection keeps the scheduler agnostic of what happens with the event (it just calls a function pointer with the conversation_id), while letting `main.py` route to the gateway.

### Why `tool_use` fires per block, not per message

A Claude `AssistantMessage` may contain multiple `ToolUseBlock` entries (parallel tool calls). We emit one `ToolUseEvent` per block rather than coalescing them into a single event with a list.

Reasons:

- The UI renders the status bar as a single line showing the *current* activity. A list of tools is misleading: they may run in parallel but the user sees them sequentially as the agent works through them.
- The overwrite semantics of the status bar mean only the last event is visible anyway. Emitting N events and letting the UI render the last one is simpler than client-side coalescing.
- A future UI could render a per-tool list; splitting now leaves that option open.

---

## Stop / Follow-up (Steering)

The Stop button and the ability to type follow-up messages mid-turn are out of scope here. They share the same NATS subject (`web.stream.{binding_id}.{chat_id}` with a `status: "stopped"` chunk) and the same UI state machine vocabulary, but the design decisions — close vs. interrupt, the three-state UI machine, IDOR scoping on the Stop signal, follow-up latency — are documented in [`steering-architecture.md`](steering-architecture.md). The progress-events protocol is consciously designed to coexist cleanly with them: `stopped` is just another terminal status; the status bar machine in the UI treats it uniformly with `done` / `error`.

---

## Known Limitations

1. **Progress events are not persisted.** On reconnect, the UI cannot replay recent progress events — only the messages themselves are stored in the DB. A user who refreshes mid-turn loses the status bar and sees a raw "in-flight" experience until the next event arrives.
2. **Status bar is overwrite-only.** Multiple `tool_use` events in parallel collapse into "show the latest one." A future UI could render a per-tool list; the emit shape (one event per block) leaves room for that.
3. **Cold-start visibility.** If the user sends a message and the container takes 5+ seconds to spawn, only `container_starting` (one event) bridges the gap. The UI does not currently distinguish "image pull" from "process starting" — both fall under `container_starting`.

---

## Test Strategy

Unit tests cover the protocol (`AgentOutput.is_progress`, `WebStreamChunk` round-trips, `tool_input_preview` across all three naming conventions, scheduler guards):

- `tests/agent/test_executor.py`
- `tests/ipc/test_web_protocol.py`
- `tests/test_agent_runner/test_event_translation.py`
- `tests/container/test_scheduler.py`

End-to-end validation was done with Playwright MCP against a live stack: cold start → status bar progression → tool_use → result. The status bar resistance to late-arriving `tool_use` events after a Stop is exercised by the steering test suite — see [`steering-architecture.md`](steering-architecture.md).

The frontend status bar has no automated tests yet — it's a gap worth filling. A vitest + happy-dom setup with a mocked `AgentClient` would cover the critical transitions cheaply.

---

## Summary of Trade-offs

| Decision | Chosen | Alternative | Why |
|---|---|---|---|
| Progress channel | Reuse `web.stream.*` with `type="status"` | New `web.progress.*` subject | Ordering guarantee, zero new infrastructure |
| Event vocabulary | Extend `BackendEvent` union | Backend-specific payloads | Pi and Claude share one contract |
| `ToolUseEvent` shape | `{tool, input_preview: str}` | `{tool, args: dict}` | Emitter knows how to format; UI stays tool-agnostic; no leaked payloads |
| `running` emit | Per-turn (`run_prompt` + `handle_follow_up`) | Once per session | Warm-container follow-ups would have no progress |
| `tool_use` granularity | One event per `ToolUseBlock` | One coalesced event with a list | Matches UI overwrite semantics; keeps per-tool list as a future option |
| Progress emit site | Scheduler (`_run_for_coworker`) | Executor (`execute()`) | Executor isn't an event source; scheduler owns the decision |

---

## Related documentation

- [`steering-architecture.md`](steering-architecture.md) — Stop / follow-up design that shares the `status: "stopped"` chunk with this protocol
- [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md) — `BackendEvent` union and how each backend maps native events to it
- [`5-webui-architecture.md`](5-webui-architecture.md) — `WebNatsGateway`, the FastAPI WebSocket layer that delivers these events to the browser
- [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) — `web.stream.{binding_id}.{chat_id}` subject, ordered consumer behavior
