# HITL Approval — S2 handoff notes (container blocking hook)

Session S2 of `docs/21-hitl-approval-plan.md`. Built on the frozen S1
contract (§3 IPC / §4 DB / §5 config); no contract drift. Branch:
`feat/hitl-approval-B`.

## What S2 landed

- **`src/agent_runner/hooks/handlers/approval.py`** — `ApprovalHookHandler`,
  a unified `HookHandler` whose `on_pre_tool_use`:
  - returns `None` immediately for non-`mcp__*` tools (§2 redline);
  - parses `mcp__<server>__<tool>`, runs `find_matching_policy` against the
    init policy snapshot; no match → `None` (allow);
  - on a match, publishes `agent.{job_id}.approval_request` (§3.1) and
    **blocks in place** awaiting an `asyncio.Future` resolved by the
    orchestrator's `approval_decision` relay, bounded by `APPROVAL_TIMEOUT`;
  - approve → `None` (tool executes in-band, same turn); reject / timeout /
    unreadable decision → `ToolCallVerdict(block=True, reason=…)`;
  - `finally` publishes `agent.{job_id}.approval_cancel` on every terminal
    path **except a clean approve** (reject / timeout / Stop / exception),
    best-effort and idempotent (§3.3 / §8 three-layer cleanup).
  - `resolve_decision(payload)` routes a decision back by `request_id`,
    first-wins (unknown / stale / done → no-op).
  - `policies_from_snapshot()` builds `ApprovalPolicy` objects from the init
    snapshot, fail-closed per field (odd `condition_expr` → `{}` which the
    matcher treats as a match → gate; bad `updated_at` → epoch).
- **`src/agent_runner/main.py`** — wires the handler when the run carries a
  non-empty policy snapshot (mirrors the safety handler's zero-cost-when-
  inactive rule): registers it on the `HookRegistry`, publishes via
  `js.publish`, and subscribes to `agent.{job_id}.approval_decision`
  (JetStream push, ordered, `DeliverPolicy.NEW`, ack-on-receipt) to drive
  `resolve_decision`. Unsubscribed in the loop `finally`.
- **`src/rolemesh/ipc/protocol.py`** — additive `AgentInitData.approval_policies`
  field (list of `ApprovalPolicy` dicts, ISO `updated_at`). `None`/empty ⇒ no
  approval gating this run. Forward-compatible via `from_dict_filter_unknown`;
  the orchestrator (S3/S4) populates it.

## Concurrency / lifecycle (§6, §8 decision race)

- Multiple `ToolUseBlock`s in one turn dispatch `on_pre_tool_use` concurrently
  on the same handler. Each call owns a fresh `request_id` + `Future` in
  `_pending`; decisions route independently (test:
  `test_concurrent_double_approval_routes_independently`).
- First-wins both sides: container `asyncio.wait_for` cancels the future on
  timeout so a late decision is a no-op; the DB `resolve_approval_request`
  (S1) makes the orchestrator transition idempotent.

## R1 (the MUST-answer risk) — finding

**Question (§9 R1):** after approval, the tool runs against an MCP connection
and credential-proxy setup that idled through a ≤5-min block. Do they survive,
and how is "tool failed after approval" reported?

**Findings:**

1. **The block is in-band and cooperative — the event loop is never frozen.**
   The hook `await`s an `asyncio.Future`; it does not busy-wait or block the
   loop. So during a pending approval, MCP keepalives (stdio subprocess stays
   alive; SSE/streamable-HTTP read loops keep running), NATS decision delivery,
   and the idle/interrupt pollers all keep ticking. Verified in-process by
   `test_block_is_cooperative_loop_not_frozen` (a concurrent keepalive
   coroutine completes while the hook is still blocked).
2. **Connection lifecycle is container/turn-scoped, not per-call.** Claude
   registers `mcp_servers` in `ClaudeAgentOptions` for the whole `run_prompt`,
   and the blocking hook runs *inside* that same `run_prompt`, so the SDK's MCP
   session persists across the block. Pi creates `McpServerConnection`s in
   `start()` and reuses them for the container lifetime. **Nothing in our code
   closes an MCP connection during a block.**
3. **No container-held credential token expires during the block.** LLM creds
   are injected per-request by the credential proxy (the container only holds
   `ANTHROPIC_BASE_URL` → proxy, not a bearer that ages). External MCP calls
   authenticate with the per-request `X-RoleMesh-User-Id` header derived from
   `init.user_id`, static for the container lifetime. There is no in-container
   token whose validity lapses across a 5-min wait.
4. **Residual risk (NOT closed in-process; environment/server-specific):** a
   *remote* MCP server, or an intermediary (egress gateway, the remote's own
   idle policy), MAY drop an idle HTTP/SSE session during the block. The 5-min
   `APPROVAL_TIMEOUT` keeps that window short. This cannot be settled by a unit
   test — it needs a live MCP server + orchestrator in staging (S4 E2E).

**"Tool failed after approval" reporting (defined):** if the post-approval MCP
call fails, it surfaces through the **normal tool-error path** — the MCP client
raises / returns an error result, which the backend delivers to the model as a
failed tool result inside its ReAct loop (Claude: `PostToolUseFailure`; Pi:
`tool_result` with `is_error`). The agent then sees the error in-context and
reports or retries it to the user. There is **no separate "approved-but-failed"
hard channel in the MVP**; the soft (agent-context) path covers it. If a retry
re-hits the hook, it produces a *new* approval request (sessions/resume make
"continue → retry → re-approve" work; S4 verifies E2E).

**Mitigations if remote idle-drop proves real in staging (tracked for S4/ops):**
(a) lower `APPROVAL_TIMEOUT`; (b) rely on transparent MCP client reconnect on
the first post-approval call (most SSE/streamable-HTTP MCP clients reconnect);
(c) a lightweight keepalive ping to gated MCP servers while an approval is
pending. None are needed for the MVP block-and-await mechanics to be correct;
they only harden the remote-idle edge.

## For S3 (orchestrator side)

- Subjects + payloads are exactly §3. The container acks `approval_decision`
  on receipt (no redelivery dependence) and never sets a custom `ack_wait` on
  any sub it relies on during a block (§6 anchor honored).
- `approval_cancel` arrives on **reject too** (not only timeout/Stop). S3's
  resume must treat decision-then-cancel for the same `request_id` as
  idempotent (`set.discard`) so it does not double-re-arm idle.
- The container forwards `user_id=None` verbatim when there is no creator; S3
  fails closed on a null approver.
