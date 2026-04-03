# Audit Log Design

> Status: Proposal
> Scope: Cross-cutting audit logging infrastructure for auth, approval, and admin operations

## Problem

RoleMesh needs a structured record of "who did what, when, to what, and what was the result" for compliance, security analysis, and troubleshooting. This is different from operational logs (structlog) which are for debugging and can be freely deleted.

## Scope

Audit log is NOT part of the auth module. It is an independent infrastructure module. Auth, approval, admin API, and any future module can emit audit events without depending on each other.

## What Gets Logged

### Authentication events
- User login success/failure
- JWT verification failure (invalid signature, expired)
- Identity resolution result

### Access events
- User requests to use an agent → granted/denied
- Access to restricted agents

### Agent execution events
- Agent calls a tool → permission check passed/denied
- Which user triggered, which agent, which tool

### Admin operations
- Agent created/deleted/permissions modified
- User assigned/unassigned to agent
- Role changes

## Architecture

```
Auth module        → emit_audit_event()
Approval module    → emit_audit_event()
Admin API          → emit_audit_event()
                         │
                         ▼
                   Audit module
                   (collect, store, query)
```

Emitters don't know how events are stored. The audit module can be absent — emit calls silently skip. Audit is an optional enhancement, not a core dependency.

## Audit Event Structure

```python
@dataclass(frozen=True)
class AuditEvent:
    timestamp: str
    tenant_id: str
    actor_type: str          # "user" / "agent" / "system"
    actor_id: str
    action: str              # "auth.login" / "access.granted" / "tool.denied"
    resource_type: str       # "agent" / "task" / "conversation"
    resource_id: str
    result: str              # "success" / "denied" / "error"
    detail: dict             # Additional context (e.g. denial reason)
```

## Action naming convention

```
auth.login.success
auth.login.failed
auth.token.invalid
access.agent.granted
access.agent.denied
agent.tool.granted
agent.tool.denied
admin.agent.created
admin.agent.deleted
admin.agent.permissions_changed
admin.assignment.created
admin.assignment.deleted
admin.user.role_changed
```

## Storage

Database, not files. Audit logs need to be queried by tenant, time range, actor, and action.

```sql
CREATE TABLE audit_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     UUID NOT NULL,
    timestamp     TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_type    TEXT NOT NULL,
    actor_id      TEXT NOT NULL,
    action        TEXT NOT NULL,
    resource_type TEXT,
    resource_id   TEXT,
    result        TEXT NOT NULL,
    detail        JSONB DEFAULT '{}',
);

CREATE INDEX idx_audit_tenant_time ON audit_log(tenant_id, timestamp DESC);
CREATE INDEX idx_audit_actor ON audit_log(actor_id, timestamp DESC);
```

## Module Structure

```
src/rolemesh/audit/
  types.py       # AuditEvent dataclass
  emitter.py     # emit_audit_event() — called by other modules
  store.py       # Write to database
  query.py       # Query interface (for Admin API)
```

## Design Decisions

1. **Independent module** — Not part of auth. Any module can emit events.
2. **Optional** — If audit module is not initialized, emit calls are silently skipped. Auth works without it.
3. **Append-only** — Audit logs are never updated or deleted by application code. Retention policy handled separately.
4. **Structured** — Fixed schema with JSONB detail field for extensibility. Not free-form text.
5. **Database storage** — Queryable by tenant, actor, time range. Not file-based.

## Future Considerations

- Log retention policies (auto-delete after N days)
- Export to external SIEM systems
- Real-time alerting on suspicious patterns (e.g. repeated access denials)
- Audit log viewer in WebUI
