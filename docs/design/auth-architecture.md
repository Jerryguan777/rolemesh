# Authentication & Authorization Architecture

> Status: Approved Design  
> Audience: Contributors and integrators of RoleMesh

## Overview

RoleMesh serves two deployment scenarios:

1. **Embedded mode** — Integrated into an existing SaaS as the AI agent subsystem. User authentication is handled by the host SaaS; RoleMesh only needs to understand "who is this user."
2. **Standalone mode** — Deployed as an independent AaaS (Agent-as-a-Service) platform. RoleMesh must handle user registration, login, and session management itself.

This document describes how auth is designed to support both scenarios cleanly, without coupling business logic to any specific authentication mechanism.

## Design Principles

### 1. AuthN is external, AuthZ is internal

Authentication (who are you?) is delegated to an `IdentityProvider` — a pluggable adapter. Authorization (what can you do?) is always handled by RoleMesh's own logic.

### 2. User permissions and agent permissions are independent

Users are authorized to *use* agents. Agents are authorized to *perform* actions. These two concerns never cross. There is no intersection calculation at runtime.

### 3. Permissions stay thin

Only pure authorization decisions live in the permissions model. Resource limits (timeout, concurrency), tool bindings (MCP servers), security policies (network, mounts), and rate limiting are managed by their respective modules — not duplicated in permissions.

### 4. Checks happen at boundaries, not in business logic

All authorization checks are performed at interception points (IPC handlers, middleware). Business logic code contains zero permission checks and is unaware of the auth system.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  External (varies by deployment)                            │
│                                                             │
│  Embedded: SaaS JWT/Session  │  Standalone: RoleMesh JWT   │
└──────────────┬───────────────┴──────────────┬───────────────┘
               │                              │
               ▼                              ▼
      ExternalAuthProvider           BuiltinAuthProvider
               │                              │
               └──────────┬───────────────────┘
                          ▼
                  IdentityProvider (Protocol)
                  resolve(request) → ResolvedIdentity
                          │
                          ▼
               ┌─────────────────────┐
               │  RoleMesh Core      │
               │  (same for both     │
               │   deployment modes) │
               └─────────────────────┘
```

## Identity Contract

The bridge between external auth and internal logic is `ResolvedIdentity`:

```python
@dataclass(frozen=True)
class ResolvedIdentity:
    tenant_id: str
    user_id: str
    role: str                         # owner / admin / member / viewer
    permissions: dict[str, Any]       # RoleMesh-specific permissions
    metadata: dict[str, str]          # Pass-through fields (email, name, etc.)
```

All RoleMesh code depends only on this structure. No code ever inspects raw JWTs or session tokens.

## Identity Provider

```python
class IdentityProvider(Protocol):
    async def resolve(self, request: Request) -> ResolvedIdentity | None:
        """Extract and validate identity from a request. Returns None on failure."""
        ...
```

### Embedded Mode: ExternalAuthProvider

Validates the host SaaS's JWT/token and maps external roles to RoleMesh roles using a declarative mapping configuration:

```json
{
  "role_map": {
    "saas:org-owner":    "owner",
    "saas:org-admin":    "admin",
    "saas:project-lead": "admin",
    "saas:member":       "member",
    "*":                 "viewer"
  },
  "permission_overrides": {
    "saas:org-admin": {
      "task:schedule": true,
      "agent:delegate": true
    }
  }
}
```

Resolution logic:

1. Verify external JWT (public key / introspection endpoint)
2. Map external role → RoleMesh role via `role_map`
3. Load role's default permissions
4. Apply `permission_overrides` for the external role (if any)
5. Return `ResolvedIdentity`

The mapping config is stored as JSON in the database, editable by tenant admins. No code changes needed when integrating with a new SaaS.

### Standalone Mode: BuiltinAuthProvider

Validates JWTs issued by RoleMesh's own auth module. Roles and permissions are read directly from the database.

## User Permissions

User permissions answer one question: **which agents can this user access?**

```
User ──[assignment]──→ Agent
       "can I use this agent?"
```

### Agent Assignment

Agents are treated as resources. An admin assigns agents to users. Once assigned, the user can use all of the agent's capabilities.

```sql
CREATE TABLE user_agent_assignments (
    tenant_id    UUID NOT NULL,
    user_id      UUID NOT NULL,
    coworker_id  UUID NOT NULL,
    assigned_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_id, coworker_id)
);
```

### Agent Visibility

Agents have a `visibility` field:

| visibility | assigned | role: admin/owner | Result |
|------------|----------|-------------------|--------|
| `public` | yes | - | Visible + usable |
| `public` | no | - | Visible + not usable |
| `restricted` | yes | - | Visible + usable |
| `restricted` | no | - | Not visible |
| any | - | yes | Visible + usable (always) |

Admins and owners bypass assignment checks and can access all agents.

### Role Defaults

| Role | Default agent access | Typical use |
|------|---------------------|-------------|
| `owner` | All agents, full control | Platform/tenant owner |
| `admin` | All agents, manage assignments | IT admin |
| `member` | Assigned agents only | Regular employee |
| `viewer` | See public agents, cannot message | Read-only stakeholder |

## Agent Permissions (Capabilities)

Agent permissions define what an agent is allowed to do. They are completely independent of user permissions.

```python
DEFAULT_AGENT_PERMISSIONS = {
    "data:scope": "own",              # own / team / tenant
    "message:send": "assigned",       # assigned / all
    "message:mention-human": False,   # Can @mention humans in group chats
    "task:schedule": False,           # Can create scheduled tasks
    "task:manage-others": False,      # Can manage other agents' tasks
    "agent:delegate": False,          # Can invoke other agents
}
```

These are stored in the `coworkers.permissions` JSONB column.

### Why no overlap with other config

| Concern | Where it lives | Why not in permissions |
|---------|---------------|----------------------|
| `max_concurrent` | `coworkers.max_concurrent` | Resource config, not authorization |
| `timeout` | `container_config.timeout` | Resource config |
| Allowed MCP servers | `coworkers.tools[]` | Tool binding, not authorization |
| Network/mount restrictions | Security module | Security policy |
| Rate limiting | Credential proxy interceptor | Operational safeguard |

Permissions only contain pure yes/no authorization decisions. Everything else has its own home.

## Authorization Flow

```
User sends message to Agent
│
├─① AuthN: IdentityProvider.resolve(request) → ResolvedIdentity
│  Failure → 401
│
├─② Access check: Is this agent assigned to this user?
│  (admin/owner skip this check)
│  Failure → 403
│
├─③ Message delivered to agent, agent processes and decides to call tools
│
├─④ Capability check: Does this agent have permission for this operation?
│  Checked at IPC task_handler, before tool execution
│  Failure → PermissionDenied returned to agent → agent informs user
│
└─⑤ Tool executes successfully
```

Steps ② and ④ are independent. User access is checked when the message arrives. Agent capability is checked when the agent acts. Business logic in between knows nothing about either check.

## Why "assign = full access" (no intersection)

We considered computing `min(user.data_scope, agent.data_scope)` at runtime. We rejected this because:

1. **Complexity** — Runtime intersection of two permission sets adds complexity to every tool call path.
2. **Unpredictability** — Admins can't easily reason about what a user+agent combination can actually do.
3. **Redundancy** — If an admin assigns a `data:scope=tenant` agent to a regular user, that's an explicit decision. The admin already considered the implications.
4. **Simplicity** — "Assigned = full access" is one concept. Intersection requires explaining a matrix.

If an admin wants to restrict data access for certain users, they create a separate agent with `data:scope=own` and assign that one instead. This is more explicit and auditable than runtime intersection.

## Separation from Other Modules

```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐
│ Auth Module  │  │ Security    │  │ Approval    │  │ Rate Limiter │
│              │  │ Module      │  │ Module      │  │              │
│ · AuthN     │  │ · Network   │  │ · Rules     │  │ · MCP calls  │
│ · User→Agent│  │ · Mounts    │  │ · Tokens    │  │ · Per-job    │
│ · Agent caps│  │ · Domains   │  │ · Workflow   │  │ · Counters   │
└──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬───────┘
       │                │                │                │
       └────────────────┴────────┬───────┴────────────────┘
                                 ▼
                     Interception points
                  (middleware, IPC handler,
                   credential proxy)
                                 │
                                 ▼
                     Business logic (pure,
                     no auth/security code)
```

Each module is independent. They are composed at interception points, not inside business logic.
