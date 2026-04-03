# Authentication & Authorization Architecture

> Status: Approved Design
> Audience: Contributors and integrators of RoleMesh

## Overview

RoleMesh serves two deployment scenarios:

1. **Embedded mode** — Integrated into an existing SaaS as the AI agent subsystem. User authentication is handled by the host SaaS; RoleMesh only needs to understand "who is this user."
2. **Standalone mode** — Deployed as an independent AaaS (Agent-as-a-Service) platform. RoleMesh must handle user registration, login, and session management itself.

This document describes how auth is designed to support both scenarios cleanly, without coupling business logic to any specific authentication mechanism.

## Design Principles

1. **AuthN is external, AuthZ is internal.** Authentication (who are you?) is delegated to a pluggable `IdentityProvider` adapter. Authorization (what can you do?) is always handled by RoleMesh's own logic.

2. **User permissions and agent permissions are fully independent.** Users are authorized to _use_ agents. Agents are authorized to _perform_ operations. These two checks happen in series, never cross-referenced. There is no intersection calculation at runtime.

3. **Assign = full access.** Once an agent is assigned to a user, the user can use all of that agent's capabilities. If different users need different capability levels, create multiple agents with different permission configs and assign them accordingly.

4. **Permissions stay thin.** Only pure authorization decisions live in the permissions model. Resource limits (timeout, concurrency), tool bindings (MCP servers), security policies, and rate limiting are managed by their respective modules — not duplicated in permissions.

5. **Checks happen at boundaries, not in business logic.** All authorization checks are performed at interception points (IPC handlers, middleware). Business logic code contains zero permission checks.

6. **External system auth via token passthrough.** RoleMesh does not enforce business-level permissions (e.g., "can this user access Project X in the SaaS"). Instead, RoleMesh passes user identity to MCP servers via a delegation token, and the external business system enforces its own permissions.

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

The bridge between external auth and internal logic:

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

Validates the host SaaS's JWT/token and maps external roles to RoleMesh roles using a declarative JSON mapping config stored per-tenant in the database:

```json
{
  "role_map": {
    "saas:org-owner": "owner",
    "saas:admin": "admin",
    "saas:member": "member",
    "*": "viewer"
  },
  "permission_overrides": {
    "saas:admin": { "task:schedule": true }
  }
}
```

Resolution: verify JWT → extract external role → map via `role_map` → load role defaults → apply `permission_overrides` → return ResolvedIdentity. Integrating with a new SaaS means writing a mapping config, not changing code.

### Standalone Mode: BuiltinAuthProvider

Validates JWTs issued by RoleMesh itself. Roles and permissions are read directly from JWT claims.

## User Permissions

User permissions answer two questions:

### 1. What can this user manage on the platform? (Role)

| Role | Capabilities |
|------|-------------|
| owner | Tenant settings, billing, everything admin can do |
| admin | Create/edit/delete agents, manage users, assign agents |
| member | Use assigned agents, view own conversations |
| viewer | See public agent list, read-only |

### 2. Which agents can this user interact with? (Assignment)

Agents are resources. Admins assign agents to users. Assigned = can use all capabilities of that agent.

- admin/owner: can access all agents without assignment
- member: can only use explicitly assigned agents
- viewer: can see public agents but cannot send messages

### Agent Visibility

Agents have a `visibility` field (`public` or `restricted`):

- `public` + assigned → visible and usable
- `public` + not assigned → visible but not usable
- `restricted` + not assigned → invisible to non-admin users
- admin/owner → always visible and usable

## Agent Permissions (Capabilities)

Agent permissions are completely independent of user permissions. They define what an agent is allowed to do within the RoleMesh platform.

### Agent Roles

Two predefined roles, used as permission templates:

**super_agent** (replaces old `is_main=True`): global visibility, cross-agent management.
**agent** (replaces old `is_main=False`): scoped to own data and tasks.

### Permission Fields

| Permission | super_agent | agent | Meaning |
|-----------|------------|-------|---------|
| `data:scope` | `tenant` | `own` | RoleMesh data visibility (tasks, snapshots) |
| `task:schedule` | `true` | `false` | Can create scheduled tasks |
| `task:manage-others` | `true` | `false` | Can manage other agents' tasks |
| `agent:delegate` | `true` | `false` | Can invoke other agents |

Roles set defaults. Individual permissions can be adjusted per agent.

### What is NOT in agent permissions

| Concern | Where it lives | Reason |
|---------|---------------|--------|
| max_concurrent | `coworkers.max_concurrent` | Resource config |
| timeout | `container_config.timeout` | Resource config |
| Which MCP servers | `coworkers.tools[]` | Tool binding |
| Network/mount restrictions | Security module | Security policy |
| Rate limits | Credential proxy | Operational safeguard |
| @mention humans | Not controlled | Agent decides by context |
| Cross-conversation messaging | Not supported | All agents message within own conversations only |

## External System Auth: Token Passthrough

When an agent calls an MCP tool that accesses external business systems, RoleMesh does not check business-level permissions. Instead, it passes user identity through.

### Two-layer headers on MCP requests

- **Server auth**: proves the request comes from RoleMesh (existing `McpServerConfig.headers`)
- **User delegation token**: identifies which user the agent is acting on behalf of

### Delegation token

RoleMesh mints a short-lived token (not the original SaaS JWT, which may expire):
- Signed by RoleMesh's own key
- Contains: user_id, tenant_id, role
- Expiration: matches agent execution timeout
- MCP servers verify using RoleMesh's public key
- Follows OAuth 2.0 Token Exchange pattern (RFC 8693)

For scheduled tasks (no active user), the delegation token is minted for the task creator's identity.

## Complete Authorization Flow

```
User sends message
  │
  ① AuthN: IdentityProvider.resolve() → ResolvedIdentity
  │  Failure → 401
  │
  ② User access: is this agent assigned to this user?
  │  (admin/owner bypass)
  │  Failure → 403
  │
  ③ Agent executes: LLM processes, decides to call tools
  │
  ④ Agent capability: does this agent have the permission?
  │  Checked at IPC task_handler
  │  Failure → PermissionDenied → agent tells user
  │
  ⑤ External auth: MCP tool receives delegation token
  │  Business system checks user's permissions
  │  Failure → tool returns error → agent tells user
  │
  ⑥ Success
```

Four layers. Each independent. Each checking a different concern.

## Module Boundaries

```
Auth module        — AuthN + user→agent access + agent capabilities
Security module    — Network, mount, domain policies
Approval module    — Human-in-the-loop for sensitive operations
Rate limiter       — MCP call frequency at credential proxy
Config             — Resource limits (timeout, concurrency)
```

Five concerns, five modules. Composed at interception points. Business logic is clean.
