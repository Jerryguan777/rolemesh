# Approval Module Design

> Status: Proposal  
> Scope: Independent module for human-in-the-loop approval of sensitive agent tool calls

## Problem

Agents can call external tools (MCP servers) that perform real-world actions: refunds, ad bid adjustments, infrastructure changes, etc. Some of these actions are high-risk and should require human confirmation before execution.

This is **not** a permissions problem. Permissions answer "is this agent allowed to call this tool?" (static yes/no). Approval answers "this call is allowed, but this specific invocation needs a human to confirm" (dynamic, per-call).

## Two Approval Modes

### Mode A: Interception (Safety Net)

The agent autonomously decides to call a tool. The platform intercepts the call, holds execution, and requests human approval.

```
Agent calls refund(amount=50000)
  → Interceptor matches rule: refund + amount > 10000
  → Execution paused
  → Approval request sent to designated approvers
  → Approved → execute
  → Rejected → return "approval denied" to agent
```

Use case: agent acting on scheduled tasks or autonomous workflows where no human is actively participating in the conversation.

### Mode B: Plan-then-Execute (Collaborative)

The agent presents a plan to the user. The user reviews and approves. The agent then executes with an approval token.

```
Agent: "I recommend adjusting ad X bid from 5 to 15. Shall I proceed?"
User: "Yes, go ahead"
Agent calls request_approval(tool="adjust_bid", args={ad: X, price: 15})
  → System sends approval to the user (who is the "initiator")
  → User clicks approve
  → Agent receives approval token
  → Agent calls adjust_bid(ad=X, price=15, approval_token=...)
  → Interceptor validates token → executes
```

Use case: interactive conversations where the agent and human collaborate on a decision.

### Relationship Between Modes

Mode B is the primary path. Mode A is the safety net. They work together:

- If the agent has a valid approval token → skip interception, execute directly
- If the agent has no token → trigger interception (Mode A)

## Architecture

### Components

```
src/rolemesh/approval/
  types.py      # ApprovalRule, ApprovalRequest, ApprovalToken
  rules.py      # Rule matching engine
  gate.py       # Interceptor (called from IPC task_handler)
  notify.py     # Sends approval requests via existing channels
```

### Approval Rules (Declarative, stored in DB as JSONB)

```json
{
  "rules": [
    {
      "tool": "refund",
      "condition": "args.amount > 10000",
      "approvers": ["role:admin", "user:finance-lead"],
      "timeout": "24h"
    },
    {
      "tool": "adjust_bid",
      "condition": "args.new_price / args.old_price > 2.0",
      "approvers": ["role:admin", "initiator"],
      "timeout": "4h"
    },
    {
      "tool": "delete_campaign",
      "condition": "true",
      "approvers": ["role:owner"],
      "timeout": "24h"
    }
  ]
}
```

Key field: `"initiator"` refers to the user who started the conversation with the agent. This enables Mode B naturally.

### Approval Request Lifecycle

```
PENDING → APPROVED
        → REJECTED
        → EXPIRED (timeout reached)
```

### Approval Token

```python
@dataclass(frozen=True)
class ApprovalToken:
    request_id: str       # Links back to the approval request
    tool: str             # Which tool was approved
    args_hash: str        # SHA-256 of the approved arguments (tamper-proof)
    approved_by: str      # Who approved
    approved_at: str      # When
    expires_at: str       # Short-lived (minutes, not hours)
    constraints: dict     # Optional: approver-imposed constraints
```

The `args_hash` is critical: an approval for `refund(amount=50000)` cannot be used to execute `refund(amount=500000)`. If the agent changes arguments after approval, the token is invalid.

### Execution Flow

```
Tool call arrives at IPC task_handler
  │
  ├─ ① Permission check (auth module) → allowed / denied
  │
  ├─ ② Approval gate (approval module) → check rules
  │     │
  │     ├─ No rule matches → pass through
  │     ├─ Rule matches + valid token → pass through
  │     └─ Rule matches + no token → create ApprovalRequest → return PENDING
  │
  └─ ③ Execute tool
```

Permission check and approval gate are adjacent but independent. They don't know about each other.

### Agent-side Tools

The approval module exposes tools to the agent via the existing rolemesh MCP server:

- `request_approval(tool, args)` — explicitly request approval before calling a tool
- `check_approval(request_id)` — poll approval status
- Approval results can also be pushed to the agent via NATS IPC

### Notification

Approval requests are delivered through existing channels (Telegram, Slack, WebUI). No new notification infrastructure needed. The `notify.py` component formats the approval request and calls `route_outbound()` to send it to the appropriate approver.

## Database Schema

```sql
CREATE TABLE approval_rules (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    tool        TEXT NOT NULL,
    condition   TEXT NOT NULL DEFAULT 'true',
    approvers   JSONB NOT NULL,        -- ["role:admin", "initiator"]
    timeout     INTERVAL NOT NULL DEFAULT '24 hours',
    enabled     BOOLEAN DEFAULT true,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE approval_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    coworker_id     UUID NOT NULL REFERENCES coworkers(id),
    conversation_id UUID,
    rule_id         UUID REFERENCES approval_rules(id),
    tool            TEXT NOT NULL,
    args            JSONB NOT NULL,
    args_hash       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected/expired
    approvers       JSONB NOT NULL,
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    constraints     JSONB DEFAULT '{}',
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

## Design Decisions

1. **Independent module** — Not part of auth/permissions. Different concern, different lifecycle.
2. **Declarative rules** — Stored as config in DB, not hardcoded. Admins can manage without code changes.
3. **Token = args_hash bound** — Prevents parameter tampering after approval.
4. **Short-lived tokens** — Approval tokens expire in minutes, not hours. Reduces window for misuse.
5. **Reuses existing channels** — Notifications go through Telegram/Slack/WebUI, no new delivery mechanism.
6. **Agent-explicit flow preferred** — Mode B (agent calls `request_approval`) is more reliable than trying to infer user intent from natural language.

## Future Considerations

- Multi-level approval chains (manager → director → VP)
- Approval delegation (out-of-office forwarding)
- Batch approvals (approve multiple similar requests at once)
- Audit log integration
- Approval analytics (average response time, approval rate)
