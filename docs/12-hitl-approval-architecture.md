# Human-in-the-Loop Tool Approval Architecture

This document describes how RoleMesh asks a human to approve a high-stakes tool call *before* the agent runs it, and why the approval mechanism is shaped the way it is.

The focus is the *why* behind the shape: the requirement that drove it, the one architectural fork that defines everything else (an out-of-band execution subsystem vs. an in-loop bounded pause), and the load-bearing invariants that keep a blocking wait safe inside a sandboxed, idle-reaped container.

Target audience: developers extending the approval policy model, wiring a new delivery channel, or debugging why an approval times out, hangs, or fires on one backend but not the other.

---

## Background: Some Tool Calls Should Not Fire Unattended

RoleMesh coworkers call external MCP tools on the user's behalf — issue a refund, post a journal entry, delete a record, message a customer. Most are routine. A few are consequential enough that an organization wants a human to look before the agent acts, and wants that gate to be *conditional*: not "approve every `refund`", but "approve a `refund` over 100".

The safety pipeline (see [`safety/safety-framework.md`](safety/safety-framework.md)) already classifies tool calls at `PRE_TOOL_CALL` and can **block** them. Blocking is the wrong tool for this job: it refuses the action outright, with no path to "let a person decide". What's needed is a third outcome between *allow* and *block* — **pause, ask a human, then allow or deny based on the answer**.

That third outcome is human-in-the-loop (HITL) approval.

## Requirements

1. **Conditional, user-defined policy.** A tenant administrator declares which calls need approval, by **MCP server + tool name + argument condition** (e.g. `amount > 100`). Everything not matched runs without friction.
2. **Self-approval routing.** The approver is the person who owns the work: the user who sent the message, or — for a scheduled task — the user who created the task. The request goes to *them*.
3. **Bounded wait.** A pending approval does not wait forever. After `APPROVAL_TIMEOUT` (default 20 minutes) it auto-rejects.
4. **Graceful expiry.** On reject or timeout the agent tells the user what happened ("that action was declined / timed out; I can re-request if you want"), and the conversation can resume later — the user comes back, says "go ahead", and the agent re-requests.
5. **Backend parity.** Approval must behave identically whether the coworker runs on the Claude SDK or Pi.
6. **Multi-tenant isolation.** Policies, requests, and approval decisions are tenant-scoped under Postgres RLS, like every other tenant resource.

---

## The Architectural Fork: Out-of-Band Subsystem vs. In-Loop Pause

There are two fundamentally different ways to build approval. RoleMesh shipped the first, removed it wholesale, and rebuilt the second greenfield. Understanding the difference is the key to this document — every other design decision is downstream of it.

### Approach A — approval as an external execution subsystem (removed)

Approval is a self-contained, out-of-band system. When a call needs approval, the tool call is **lifted out of the agent's hands**: the request is persisted, the container moves on, and once a human approves, a dedicated `ApprovalWorker` consumes an `approval.decided.*` event, claims the request, and **executes the MCP call itself** through the credential proxy — then tries to thread the result back.

This buys one thing: the wait can be arbitrarily long, because nothing is blocked — the state lives in Postgres. It costs a great deal to get there: a ten-state machine, an audit trigger, a worker and an executor, idempotency keys for redelivery, two reconciliation loops, cross-restart dedup, a `submit_proposal` tool, an `auto_execute` collapse path. The execution mechanism is entangled with the backend (whoever re-runs the call has to reinject the result into *that* backend's transcript). It was on the order of twenty thousand lines.

Approach A was removed in its entirety (`feat/remove-approval`).

### Approach B — approval as a bounded pause in the agent's own loop (current)

Approval is **one timed `await` inside the agent's own ReAct loop**. There is no out-of-band executor. The `PreToolUse` hook, on a policy match, blocks on `asyncio.wait_for(decision, APPROVAL_TIMEOUT)`:

- **Approved** → the hook returns "allow", and the agent runs the very same tool call, in the same turn, and the real result flows back to the LLM naturally.
- **Rejected / timed out** → the hook returns a block verdict whose `reason` is fed back as the tool result, and the agent continues its loop in its own words ("that was declined — want me to re-request?").

To the agent, an approval is just "a tool call paused for a moment". The wait is short by construction: the container blocks for at most `APPROVAL_TIMEOUT`, then auto-rejects and exits. Long waits aren't *engineered around* — they're **defined away**.

### Why B

| Dimension | A — out-of-band subsystem | B — in-loop pause |
|---|---|---|
| **Execution model** | A separate worker re-runs the approved call and reinjects the result | The agent runs the approved call itself, in the original turn |
| **Relation to the ReAct loop** | Breaks the loop — reasoning/acting is interrupted and taken over | Rides the loop — a `PreToolUse` `await`; allow lets the tool run, deny becomes a tool result the agent reads |
| **Wait & timeout** | Long wait: container exits, state in Postgres, re-wake on decision | Short wait: container blocks ≤ `APPROVAL_TIMEOUT`, auto-reject on expiry, resume next time |
| **Backend coupling** | Execution is entangled with each backend's transcript | None — pure `async hook + asyncio.wait_for`; works on Claude and Pi unchanged |
| **Complexity** | ~20k LOC: 10-state machine, worker, executor, audit trigger, idempotency, two reconcile loops, proposals, auto-execute | 4 states (pending / approved / rejected / expired), one gate hook, one decision channel, one suspend/resume rule |

In one line: **A makes approval an external system that executes on the agent's behalf; B makes approval the agent waiting a moment for a human to nod.**

B is the better fit for RoleMesh specifically because the hook layer already gives us a backend-neutral `PreToolUse` gate that works identically on Claude and Pi (see [`9-hooks-architecture.md`](9-hooks-architecture.md)). Approval needs nothing more than that gate plus a way to wait — and waiting in-loop deletes the entire out-of-band execution problem that made A heavy and backend-coupled.

---

## Design Goals (Approach B)

1. **Approval is a hook outcome, not a subsystem.** It lives behind the unified `HookRegistry.on_pre_tool_use`, the same gate the safety pipeline uses. No tool call is ever executed by anything other than the agent.
2. **Backend-neutral by construction.** Because B never re-executes a call out of band, there is no "which backend do I reinject into" problem. The same hook handler runs unchanged on Claude and Pi.
3. **Bounded, self-healing waits.** Every pending approval has a hard deadline and multiple independent paths to resolution, so a lost message, a crashed orchestrator, or a killed container can never strand a conversation forever.
4. **The blocking wait must survive the container's own liveness machinery.** A container that blocks silently for 20 minutes must not be mistaken for a hung one and reaped.
5. **Minimal state.** Four request states, one policy table, one request table. No audit triggers, no workers, no proposals.

---

## Architecture

```
        Agent ReAct loop (inside the sandboxed container)
        ┌─────────────────────────────────────────────────┐
        │  LLM emits a tool call: mcp__erp__refund {500}   │
        │                  │                               │
        │                  ▼                               │
        │     HookRegistry.on_pre_tool_use                 │
        │       ApprovalHookHandler                        │
        │        • match tenant policy (server+tool+cond)  │
        │        • no match → allow (return None)          │
        │        • match → publish approval_request,       │
        │                  await decision (≤ TIMEOUT)      │
        │            approve → allow, tool runs in-loop     │
        │            reject  → block(reason) → tool result  │
        │            timeout → block(reason) → tool result  │
        └───────────────┬───────────────────▲─────────────┘
                approval_request │           │ approval_decision
                                 ▼           │
        ┌─────────────────────────────────────────────────┐
        │  Orchestrator                                    │
        │   • persist request (pending, expires_at)        │
        │   • SUSPEND idle reaping for this conversation   │
        │   • resolve approver (self), deliver to channel  │
        │   • on decision/expiry: RESUME idle reaping       │
        └───────────────┬─────────────────────────────────┘
                        │ card + buttons / WS event
                        ▼
                  Telegram / Web  →  the approver
```

Two halves, one timed handshake:

- **Container side** is a single hook handler that matches policy, fires one request, and blocks. It never talks to a channel and never executes anything out of band.
- **Orchestrator side** persists the request, routes it to the right human, relays the decision back, and — critically — **suspends the container's idle reaping while the wait is in progress**.

---

## Design Essentials

These are the load-bearing pieces. Everything else is mechanical.

### 1. Approval is a `PreToolUse` outcome, scoped to MCP tools

The handler runs inside the unified hook layer. Non-MCP tools (`Read`, `Bash`, …) return immediately. For an `mcp__server__tool` call it evaluates the tenant's policy against the call's arguments; a miss allows, a hit pauses. Because the gate is the same backend-neutral `on_pre_tool_use` the safety pipeline uses, Claude and Pi get approval for free — no backend-specific code.

### 2. Policy is per-tenant, conditional, fail-closed

A policy matches on **MCP server + tool name (exact or `*`) + a structured argument condition** (`{"field": "amount", "op": ">", "value": 100}`, composable with `and`/`or`). The matcher is a pure function shared by container and orchestrator. If a condition can't be evaluated — missing field, type mismatch, malformed expression — it **fails closed** (requires approval), never open.

### 3. The wait is in-loop and bounded; cleanup is owned by the container

The hook blocks on `asyncio.wait_for(decision, APPROVAL_TIMEOUT)`. On any exit — approve, reject, timeout, user Stop, or exception — the handler's `finally` deterministically emits a cancel for any still-undecided request. The container is the only party that knows for certain "this turn is over", so it owns cleanup. The blocking wait also means an **approved** call simply continues in the same turn — no session resume, no result reinjection.

### 4. The orchestrator suspends idle reaping during the wait — it does not fake liveness

A container blocked for 20 minutes produces no output, and the orchestrator's idle machinery would otherwise reap it as hung. The wrong fix is a heartbeat that *pretends* the container is busy. The right fix is to tell the orchestrator the truth: this is a known, bounded, legitimate wait. On an `approval_request` the orchestrator **suspends** idle reaping for that conversation; on the decision (or on the container's own timeout-cancel) it **resumes** it. The container's own watchdog is left untouched, guaranteed by a startup invariant (`APPROVAL_TIMEOUT < IDLE_TIMEOUT + 30s`) that ensures the approval always resolves first.

### 5. Concurrency and crash-safety are first-class

A single assistant turn can emit several tool calls in parallel, so a conversation can have several approvals pending at once. Suspension is therefore tracked as a **set of pending request IDs** — idle reaping resumes only when the last one clears, never on the first. And because in-memory suspension state is just a cache, the **database is authoritative**: on restart the orchestrator reloads `pending` rows and either re-arms their deadlines or expires them and notifies the user. No single failure — lost message, killed container, orchestrator restart — strands a conversation.

### 6. Notifying the user is deterministic, not LLM-dependent

On reject or timeout, the `reason` is fed back into the agent's context so it *can* explain in its own words — good UX, but not guaranteed (especially for an unattended scheduled task). So the orchestrator **also** notifies the user directly and unconditionally, editing the approval card in place to "declined" / "timed out". Two channels: a soft one through the LLM for natural phrasing, a hard one through the orchestrator for guaranteed delivery.

---

## Rejected Alternatives

### Keep Approach A, just slim it down

Rejected. A's weight isn't incidental — it's intrinsic to out-of-band execution. The moment a separate worker re-runs the approved call, you inherit result reinjection, backend coupling, redelivery idempotency, and reconciliation. Trimming states doesn't remove the executor; only moving execution back into the agent's loop does. We deleted A rather than slim it.

### Heartbeat to keep the container "alive" during the wait

Rejected. A status heartbeat doesn't reset the orchestrator's idle timer (progress events are intentionally inert there), and even where it would, it's a lie: it disguises a legitimate wait as activity, and its correctness rests on "N heartbeats all arrive on time". Suspend/resume states the truth once and rests on a single state transition plus a static invariant. (See "Design Essentials #4".)

### A boolean "approval pending" flag instead of a set

Rejected. Parallel tool calls in one turn can open several approvals at once. A boolean clears on the first decision and resumes idle reaping while others are still waiting — re-introducing exactly the reaping race the suspend mechanism exists to prevent. The state must be a set keyed by request ID.

### Let the agent be solely responsible for telling the user

Rejected as the *only* channel. The LLM may forget, may phrase the outcome unrecognizably, or may have no turn left to speak in (scheduled tasks). The orchestrator's deterministic notification is the guarantee; the agent's narration is the nicety.

---

## Known Constraints

- **One pending approval pins one container** for up to `APPROVAL_TIMEOUT`. Concurrency ceilings (`MAX_CONCURRENT_CONTAINERS`, `GLOBAL_MAX_CONTAINERS`) must absorb the worst case so approvals don't starve ordinary runs. This is the price of the in-loop model and is accepted deliberately.
- **In-argument conditions only.** Policies decide on the call's own arguments. Cross-call or stateful conditions ("third refund today") are out of scope by design.
- **MCP tools only.** Built-in tools (`Read`, `Edit`, `Bash`, …) are not gated by approval; they're governed by the safety pipeline and container hardening.

---

## Related Documentation

- [`9-hooks-architecture.md`](9-hooks-architecture.md) — the unified `PreToolUse` gate approval is built on, and the Claude/Pi bridge parity it inherits
- [`8-switchable-agent-backend.md`](8-switchable-agent-backend.md) — why two backends, and why a backend-neutral approval mechanism matters
- [`safety/safety-framework.md`](safety/safety-framework.md) — the `PRE_TOOL_CALL` block path approval sits alongside (block vs. pause-and-ask)
- [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md) — the NATS subjects the approval request/decision handshake travels on
