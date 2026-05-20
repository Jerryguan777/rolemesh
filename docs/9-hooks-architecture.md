# Unified Hook System Architecture

This document explains RoleMesh's unified hook system — the mechanism that lets audit, DLP, transcript-archive, approval, safety, and observability modules observe and intercept agent activity across both agent backends (Claude SDK and Pi) without coupling to either backend's native hook API.

The goal is to document the *why* behind the shape: what alternatives were rejected, which backend asymmetries we had to paper over, and which silent bugs this system is designed to catch.

Target audience: developers adding a new hook event, a new handler, or a third agent backend, plus anyone debugging why a handler fires on one backend but not the other.

---

## Background: Two Backends, Two Hook Dialects

RoleMesh agents run on two LLM frameworks behind a common `AgentBackend` protocol (see [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md)). Each framework ships its own hook system:

- **Claude Agent SDK** — exposes hook callbacks through `ClaudeAgentOptions.hooks`, keyed on event names `PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `PreCompact`, `Stop`, etc. Each callback receives an SDK-specific `input_data` shape and returns an SDK-specific response dict (e.g. `{"hookSpecificOutput": {"permissionDecision": "deny", ...}}`).
- **Pi** — exposes an extension system. Extensions subscribe to events like `tool_call`, `tool_result`, `session_before_compact`. Each handler receives a Pi `@dataclass` event and returns a Pi `@dataclass` result (e.g. `ToolCallEventResult(block=True, reason=...)`).

Both surfaces cover roughly the same lifecycle points (before a tool, after a tool, before context compaction, before a prompt), but they differ in:

1. **Event names** — `PreToolUse` vs `tool_call`, `PreCompact` vs `session_before_compact`.
2. **Payload shape** — SDK uses dicts with PascalCase keys; Pi uses dataclasses with snake_case fields.
3. **Response shape** — SDK nests permission decisions under `hookSpecificOutput`; Pi returns typed dataclasses.
4. **Capabilities** — Pi's `tool_result` handler can rewrite content outright; SDK's `PostToolUse` can only append additional context.
5. **Lifecycle coverage** — Pi emits `session_before_compact` but never invokes its own `emit_before_agent_start` internally; Claude SDK has no direct analogue of "context transform".

If we let RoleMesh applications register Pi-shaped handlers when a coworker runs on Pi and Claude-shaped handlers when it runs on Claude, we would:

- Force every audit / DLP policy to be written twice with different return shapes.
- Risk DLP handlers that silently no-op on the other backend (a handler that returns `modified_input` "works" on Claude and is silently ignored on Pi — that's a data exfil path nobody notices in testing).
- Couple the application layer to each SDK's version churn (a Pi event name rename cascades into every downstream handler).

A unified hook layer sits between applications and the two backends and presents a single consistent vocabulary. Applications write one handler and it runs unchanged on either backend — or fails loudly at the bridge instead of silently at runtime.

---

## Design Goals

1. **One handler, both backends** — a `HookHandler` class with the same methods runs identically whether the container chose Claude or Pi.
2. **Backend-neutral event vocabulary** — the 6 events cover the intersection of both backends' capabilities. No SDK-specific fields leak into the handler API.
3. **Fail-close for control, fail-safe for observation** — a handler that crashes while deciding "block or allow" MUST deny (audit DB down shouldn't mean an unaudited tool call goes through); a handler that crashes while writing a log line MUST NOT break the agent.
4. **Lowest-common-denominator capability surface** — if one backend supports "replace tool result" and the other only "append context", expose only "append". A capability advertised to handlers must work on every backend, not silently degrade on some.
5. **Stop contract preserved** — exactly one `Stop` hook per `run_prompt` or `abort` cycle, regardless of backend mechanics. (See [`backend-stop-contract.md`](backend-stop-contract.md).)
6. **Schema drift surfaces loudly** — if Pi or Claude SDK renames a field, tests break rather than silently-mislocated events.

---

## The Six Hook Events

| Hook | Type | Capability | Failure policy |
|---|---|---|---|
| `PreToolUse` | control | block a tool call, or modify its input | **fail-close** |
| `PostToolUse` | observation + append | append additional context to the tool result | fail-safe |
| `PostToolUseFailure` | observation only | observe tool errors | fail-safe |
| `PreCompact` | side effect | run before the backend compacts transcripts | fail-safe |
| `UserPromptSubmit` | control | block an incoming user message, or append context | **fail-close** |
| `Stop` | notification | fire once per `run_prompt`/`abort` cycle | fail-safe |

The canonical shapes live in `src/agent_runner/hooks/events.py`. All event and verdict types are `@dataclass(frozen=True)`.

### Why Only "Append" on PostToolUse?

Claude SDK's `PostToolUse` hook only supports `additionalContext` (text appended to the result the agent sees). Pi's `tool_result` extension can replace content wholesale. The earliest draft exposed `modified_result` in `ToolResultVerdict` to use Pi's capability.

We reverted. A DLP handler that redacts a secret from a tool result by returning `modified_result="<redacted>"` would **work on Pi** and **silently no-op on Claude**. A developer testing on Pi, deploying to prod on Claude, would ship a DLP rule that never redacts anything. Cross-backend asymmetry on a security surface is the worst kind of silent bug.

Exposing only "append" means the Pi capability is underused but the Claude capability is the contract. Handlers that need true replacement must block at `PreToolUse` or rewrite `tool_input` there — both of which work on both backends.

### Why `reason: str` on `StopEvent` instead of a `Literal`?

Mild tradeoff. `Literal["completed", "aborted", "error"]` gives static-typing benefits but forces every Stop emit site to import a specific typing value; `str` with a docstring lets future backends extend without a library-wide rename. The three values are documented on `StopEvent` and asserted in tests.

---

## Architecture

```
                       application-defined handlers
                ┌──────────────────────────────────────────┐
                │  TranscriptArchiveHandler  (built-in)    │
                │  ApprovalHandler            (built-in)   │
                │  SafetyHandler              (built-in)   │
                │  (future) DLPHandler                     │
                │  (future) AuditHandler                   │
                └────────────────┬─────────────────────────┘
                                 │ register()
                                 ▼
                    ┌───────────────────────────┐
                    │       HookRegistry        │
                    │  emit_pre_tool_use        │    control ← fail-close
                    │  emit_user_prompt_submit  │    control ← fail-close
                    │  emit_post_tool_use       │    observ.  ← fail-safe
                    │  emit_post_tool_use_fail  │    observ.  ← fail-safe
                    │  emit_pre_compact         │    observ.  ← fail-safe
                    │  emit_stop                │    observ.  ← fail-safe
                    └────────────┬──────────────┘
                                 │ backend-neutral
                                 │  ToolCallEvent,
                                 │  ToolResultEvent,
                                 │  CompactionEvent, ...
                                 ▼
                ┌────────────────┴───────────────┐
                ▼                                ▼
 ┌─────────────────────────┐      ┌──────────────────────────────┐
 │  Claude Bridge          │      │  Pi Bridge                   │
 │  _build_hook_callbacks  │      │  _build_bridge_extension     │
 │                         │      │                              │
 │  produces SDK-shaped    │      │  produces a Pi Extension     │
 │  HookMatcher dict       │      │  with tool_call /            │
 │  passed to              │      │  tool_result /               │
 │  ClaudeAgentOptions     │      │  session_before_compact      │
 │  .hooks                 │      │  handlers                    │
 │                         │      │                              │
 │  Stop / UserPromptSubmit│      │  Stop / UserPromptSubmit     │
 │  emitted manually from  │      │  emitted manually from       │
 │  run_prompt / abort     │      │  run_prompt / abort /        │
 │                         │      │  handle_follow_up            │
 └────────────┬────────────┘      └──────────┬───────────────────┘
              │                              │
              ▼                              ▼
 ┌─────────────────────────┐      ┌──────────────────────────────┐
 │  claude_agent_sdk       │      │  pi.coding_agent             │
 └─────────────────────────┘      └──────────────────────────────┘
```

The Approval and Safety handlers are the two largest consumers of the hook system — both use `PreToolUse` to gate or block calls. Each has its own architecture document ([`approval-architecture.md`](approval-architecture.md), [`safety/safety-framework.md`](safety/safety-framework.md)); this document is about the **mechanism** they share.

### File layout

```
src/agent_runner/
  hooks/
    events.py                    # backend-neutral dataclasses
    registry.py                  # HookRegistry + HookHandler protocol
    handlers/
      transcript_archive.py
      approval.py
  safety/                        # safety pipeline hooks (separate package)
  claude_backend.py              # owns _build_hook_callbacks + Stop emit
  pi_backend.py                  # owns _build_bridge_extension + Stop emit
  backend.py                     # AgentBackend protocol w/ hooks param
  main.py                        # constructs HookRegistry, wires handlers
```

---

## Core Abstraction: HookRegistry

```python
class HookRegistry:
    def register(self, handler: object) -> None: ...

    # Control (fail-close)
    async def emit_pre_tool_use(e: ToolCallEvent) -> ToolCallVerdict | None: ...
    async def emit_user_prompt_submit(e: UserPromptEvent) -> UserPromptVerdict | None: ...

    # Observation (fail-safe)
    async def emit_post_tool_use(e: ToolResultEvent) -> ToolResultVerdict | None: ...
    async def emit_post_tool_use_failure(e: ToolResultEvent) -> None: ...
    async def emit_pre_compact(e: CompactionEvent) -> None: ...
    async def emit_stop(e: StopEvent) -> None: ...
```

### Duck typing, not inheritance

`register()` accepts `object`, not `HookHandler`. Each `emit_*` method uses `getattr(h, "on_<event>", None)` to find the corresponding method. A handler that only cares about `PreCompact` defines only `on_pre_compact` — no abstract base class, no `NotImplementedError` stubs, no "does this handler care about that event?" bookkeeping.

`HookHandler` Protocol is still defined in `registry.py` and exported — it's the canonical list of recognized method names for API docs and type hints, not a runtime check.

### Chaining semantics

Multiple handlers can modify the same event. The registry defines how their outputs combine:

- **PreToolUse**: first `block=True` short-circuits; `modified_input` is chained forward — handler `N+1` sees the input that handler `N` returned.
- **UserPromptSubmit**: same short-circuit on block; `appended_context` from multiple handlers is joined with `"\n\n"`.
- **PostToolUse**: `appended_context` from multiple handlers is joined with `"\n\n"`. No short-circuit — every handler observes.
- **PostToolUseFailure / PreCompact / Stop**: every handler is invoked; no aggregation.

### Fail-close vs fail-safe in practice

The registry itself implements the policy:

```python
# Control — no try/except around the handler call
async def emit_pre_tool_use(self, event):
    for h in self._handlers:
        verdict = await h.on_pre_tool_use(event)  # raises propagate
        ...

# Observation — try/except per handler
async def emit_pre_compact(self, event):
    for h in self._handlers:
        try:
            await h.on_pre_compact(event)
        except Exception as exc:
            _log.warning("pre_compact handler failed: %s", exc)
```

Bridge code owns the other half of the policy: when a control hook raises out of the registry, the bridge translates the raise into the backend's native "block" response. The raise never reaches the SDK or Pi Agent unchanged.

```python
# claude_backend.py
async def pre_tool_use(input_data, tool_use_id, context):
    try:
        verdict = await hooks.emit_pre_tool_use(...)
    except Exception as exc:
        return _deny(f"Hook system error: {exc}")  # fail-close
    ...
```

---

## Pi Bridge: the timing constraint

Pi's extension system wraps tools at `create_agent_session()` time. Our bridge extension can only be built **after** the session is created — it needs access to the session's `SessionManager`, `ModelRegistry`, etc. So the bridge needs to install itself *post-construction* and still apply to built-in tools that were resolved during construction.

The solution: a **mutable ref dict** passed to `create_agent_session` and resolved lazily inside the tool wrapper:

- `create_agent_session(..., extension_runner_ref=ref_dict)` stores the dict and wraps each tool with a lazy proxy that reads `ref_dict["current"]` at every `execute()` call.
- The caller (PiBackend) builds the bridge extension after session creation and assigns `ref_dict["current"] = runner`.
- If the ref is still unbound at execute time, the tool runs pass-through without hooks — null-safe rather than crashing.

This pattern (mutable ref + lazy wrap) is the load-bearing piece of Pi integration. Implementation lives in `src/pi/coding_agent/core/sdk.py:_wrap_tools_lazy` and is exercised end-to-end by `test_full_chain_pi_e2e.py`. The history that led to it — including the `is_not_none` vs truthy-check bug and the silently-swallowed missing `await` — is preserved as inline comments at the relevant sites in `pi_backend.py` so the next person doesn't repeat the debugging.

---

## Stop Lifecycle

The hook-layer contract is one sentence: **`emit_stop` fires exactly once per `run_prompt` / `abort` cycle**.

- `run_prompt` → exactly one Stop emit, `reason ∈ {"completed", "error", "aborted"}`.
- `abort()` → exactly one Stop emit, `reason="aborted"`.
- abort-during-active-run → exactly one Stop, owned by `abort()`; `run_prompt`'s finally must skip its own emit.

### Why not wire to SDK-native Stop hooks?

- Claude SDK's `Stop` hook fires when the **model** decides to stop generating — multiple times per turn, with a `stop_hook_active` anti-loop flag. Wrong semantics (we care about "run_prompt finished", not "model stopped streaming").
- Pi's `agent_end` event fires once per `_run_loop` exit, which doesn't map 1:1 to user-visible prompt completion for steering-based turns.

Both backends emit Stop manually from their own `run_prompt` / `abort` paths. One emit per user-facing event.

### Implementation-specific anti-double-emit

Claude (preemptive cancellation) and Pi (cooperative cancellation) coordinate this single-emit invariant differently — Claude uses a local `aborted` flag caught from `CancelledError`; Pi uses a `_stop_emitted_by_abort` latch set synchronously inside `abort()` before any await. The full cancellation contract — including which order things happen, how the bridge guarantees no late events for an aborted turn, and the seven path permutations the lifecycle tests cover — lives in [`backend-stop-contract.md`](backend-stop-contract.md). This document only owns the hook-layer invariant: one emit per cycle.

---

## Backend Asymmetries (and Why They're Documented, Not Hidden)

Two capability gaps between Claude and Pi that we chose to **document explicitly** rather than emulate.

### 1. `PreToolUse.modified_input` works on Claude, degrades on Pi

- Claude: the bridge returns `{"hookSpecificOutput": {"updatedInput": <dict>}}` and the SDK feeds the modified input to the tool.
- Pi: `ToolCallEventResult` has no input-modification slot. Our bridge logs a warning and drops the modification; the tool runs with the original input.

We could emulate Pi by intercepting the tool inside `_wrap_tools_lazy` and rewriting `params` before calling `inner.execute(...)`. We chose not to because:

- The degradation is visible: a warning log fires the moment a handler returns `modified_input` on Pi.
- Applications that *need* guaranteed modification can use the portable alternative: block at `PreToolUse` with a reason explaining why, and let the agent retry with modified intent. This pattern works identically on both backends.
- Adding a second in-process wrap would mean two layers that both have to be kept coherent with Pi's tool pipeline — more surface for silent drift.

`test_hook_parity.py::test_pre_tool_use_modified_input_pi_degrades` locks this behavior.

### 2. Pi does not emit `before_agent_start` internally

Pi's `ExtensionRunner` defines `emit_before_agent_start` and `emit_input` but **Pi core never invokes them**. Routing `UserPromptSubmit` through those would silently not fire. The Pi bridge instead calls `hooks.emit_user_prompt_submit(...)` manually from `PiBackend.run_prompt()` and `handle_follow_up()`, before handing the text to `session.prompt()`. Claude SDK's native `UserPromptSubmit` hook fires reliably on every user message, so the Claude bridge uses the native wiring.

`test_user_prompt_submit_e2e.py` covers both paths (initial prompt and follow-up).

---

## Rejected Alternatives

### Why not expose each backend's raw hook surface?

Two parallel hook APIs, one per backend. Rejected:

- DLP handlers would need a Claude version AND a Pi version.
- Cross-backend assertions become impossible: "does this handler fire on both?" requires testing on both.
- Every future backend adds another parallel API.

### Why not a single hook callable (no event types)?

`on_event(event_dict)` with a `"type"` field, like a bus subscription. Rejected:

- No static typing help; handlers dispatch on string keys.
- Harder to document which fields are available when.
- Easy to register a handler that silently never matches any type.

The six named methods on `HookHandler` give both IDE autocomplete and a grep-able list of "what events exist".

### Why not emit Stop via a subscribe-style listener instead of a hook?

The `AgentBackend.subscribe()` machinery already delivers `StoppedEvent` to the NATS bridge. We considered letting Stop handlers observe via the same listener list. Rejected because:

- `subscribe` delivers **UI events**: ordering and delivery semantics are tuned for the NATS bridge, not for handlers. A slow handler would block UI updates.
- Hook handlers are registered at backend `start()` time and outlive individual listener references used by `main.py`.
- Fail-safety: `subscribe` listeners propagate exceptions to the backend; hook handlers need individual `try/except` wrapping.

A separate emit path with its own isolation policy is clearer.

---

## Testing Strategy

The hook system has four test layers; each catches a different class of bug.

| Layer | File(s) | What it catches |
|---|---|---|
| **Registry units** | `test_hook_registry.py` | Reordered dispatch, try/except in wrong scope, default-verdict collapse |
| **Bridge translation parity** | `test_hook_parity.py` (parameterized over Claude / Pi) | Backend drift — a bridge that forgets to invoke the registry on failure |
| **Lifecycle / edge-shape** | `test_stop_lifecycle_{claude,pi}.py`, `test_mcp_tool_names.py`, `test_claude_tool_response_shapes.py`, `test_user_prompt_submit_e2e.py` | Multi-component contracts spanning registry + bridge + runtime |
| **Schema lock** | `test_pi_pre_compact_schema_lock.py` | Pi renaming an event string or dataclass field — silent miswire |
| **Full-chain E2E** | `test_full_chain_pi_e2e.py` (real `create_agent_session` + fake provider) | Bugs that only surface when extensions actually run through Pi's session machinery |

The full-chain layer caught the silently-swallowed missing `await` in `_transform_context` — no earlier layer exercised that code path. The lifecycle layer caught a Pi double-emit of Stop during abort-mid-run.

A future contract test against a stubbed `ANTHROPIC_BASE_URL` server would catch shape drift in Claude SDK's own payloads — not currently implemented.

---

## How to Add a New Hook

1. Add the event + verdict dataclasses in `src/agent_runner/hooks/events.py`. Keep them frozen and minimal.
2. Add the `emit_<event>` method to `HookRegistry` with the appropriate fail-close or fail-safe pattern. Add the corresponding method name to the `HookHandler` Protocol.
3. Export the new event/verdict from `src/agent_runner/hooks/__init__.py`.
4. Wire the bridge for each backend:
   - **Claude:** add a callback in `_build_hook_callbacks` and register it under the SDK event name via `HookMatcher`.
   - **Pi:** add a handler to `_build_bridge_extension` under the Pi event name, OR emit manually from the appropriate backend method if Pi's event system doesn't cover the lifecycle point.
5. Add a registry unit test, a bridge parity test (one `@parametrize` run per backend), and one E2E scenario in the full-chain file.
6. Document the hook in this file's "Six Hooks" table.

`TranscriptArchiveHandler` in `src/agent_runner/hooks/handlers/transcript_archive.py` is the canonical example of a handler that uses only a subset of methods (just `on_pre_compact`) and branches on backend payload shape.

---

## How to Add a Third Agent Backend

See [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md) for the container-level dispatch pattern. Hook-specific requirements on top:

1. **Implement `AgentBackend.start(init, tool_ctx, mcp_servers, hooks)`**. Treat `hooks is None` as an empty registry, not a silent disable.
2. **Route each of the six events** through `hooks.emit_*`. For each event, choose between native SDK hook wiring (if the SDK has a reliable callback point) and manual emission from your `run_prompt` / `abort`.
3. **Translate control-hook exceptions** into the SDK's native "block" response at the bridge layer. A raise from `emit_pre_tool_use` must never reach your SDK's tool dispatch unchanged.
4. **Follow the Stop contract**: one emit per `run_prompt` / `abort` cycle. If cancellation is cooperative (like Pi), use the synchronous-latch pattern. If preemptive (like Claude), use a local exception-caught flag. The full contract — including the seven path permutations the existing two backends are tested against — lives in [`backend-stop-contract.md`](backend-stop-contract.md).
5. **Add a per-backend lifecycle test file** (`test_stop_lifecycle_<yourbackend>.py`) covering the same seven path permutations as the existing two.
6. **Add parity rows**: extend `test_hook_parity.py` parametrize to include `"<yourbackend>"` and supply a `_build_hook_callbacks`-equivalent extractor.

### Per-backend asymmetries: document them, don't hide them

If your backend can't support some feature (e.g. no `modified_input`), log a warning the first time a handler tries to use it, add a parity test that asserts the degradation, and add a note in "Backend Asymmetries" above. The worst failure mode is silent degradation that looks like it works in tests.

---

## Known Gaps

- **Claude SDK payload-shape drift** — no test runs against a real Claude CLI subprocess. A future contract test against a stubbed `ANTHROPIC_BASE_URL` server could catch Anthropic API shape changes.
- **Pi `emit_before_agent_start` / `emit_input`** — defined on `ExtensionRunner` but never called by Pi core. If Pi starts calling them internally, our manual `UserPromptSubmit` emits would produce duplicates. Add regression coverage when that happens.
- **Concurrent tool calls** — no test exercises multiple tool calls in a single `AssistantMessage` (parallel tool use). The hook bridge should fire once per tool call, but the invariant isn't asserted end-to-end.
- **Steering interactions** — Pi supports steering messages mid-turn. The interaction between `UserPromptSubmit` on a steering message and the ongoing turn's context is not explicitly tested.

When any of these shows up as a real bug, add a test to the appropriate layer (unit / parity / lifecycle / full-chain) rather than a fix-in-place without coverage.

---

## Related documentation

- [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md) — `AgentBackend` protocol, why two backends, Pi-Specific Pitfalls
- [`backend-stop-contract.md`](backend-stop-contract.md) — full abort/shutdown semantics across backends
- [`approval-architecture.md`](approval-architecture.md) — `ApprovalHandler`: how the approval module uses `PreToolUse`
- [`safety/safety-framework.md`](safety/safety-framework.md) — safety pipeline checks that fire through `PreToolUse` and friends
