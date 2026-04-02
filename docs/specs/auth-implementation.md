# Auth & Authorization Implementation Spec

> This document is an actionable implementation guide. After reading it, a developer
> should know exactly what files to create, what interfaces to implement, and how
> the pieces connect to existing RoleMesh code.

## Prerequisites

Read `docs/design/auth-architecture.md` for the rationale and design decisions behind this spec.

## Scope

This spec covers:

- Identity provider abstraction (Protocol + two adapters)
- User-agent assignment model
- Agent capability permissions
- Role-permission mapping configuration
- Database schema changes
- Integration points with existing code

This spec does NOT cover:

- Approval workflows (see `docs/design/approval-module.md`)
- Security policies (network, mount restrictions)
- Rate limiting
- Built-in login/registration UI (standalone mode only, separate spec)

---

## 1. New Module Structure

```
src/rolemesh/auth/
  __init__.py
  types.py               # ResolvedIdentity, IdentityProvider Protocol
  permissions.py          # Role defaults, permission checking helpers
  middleware.py           # Request-level auth interception
  builtin_provider.py     # Standalone mode: verify RoleMesh-issued JWT
  external_provider.py    # Embedded mode: verify external SaaS JWT
  mapping.py              # Declarative role/permission mapping engine
```

## 2. Core Types (`auth/types.py`)

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from aiohttp import web


@dataclass(frozen=True)
class ResolvedIdentity:
    """The standard identity object used by all RoleMesh internal code."""
    tenant_id: str
    user_id: str
    role: str                                    # owner / admin / member / viewer
    permissions: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)

    def has_permission(self, key: str) -> bool:
        """Check a boolean permission."""
        return bool(self.permissions.get(key, False))

    def get_permission(self, key: str, default: Any = None) -> Any:
        """Get a permission value."""
        return self.permissions.get(key, default)

    @property
    def is_admin_or_owner(self) -> bool:
        return self.role in ("owner", "admin")


class IdentityProvider(Protocol):
    """Protocol that all auth adapters must satisfy."""

    async def resolve(self, request: web.Request) -> ResolvedIdentity | None:
        """
        Extract and validate identity from the request.
        Returns ResolvedIdentity on success, None on authentication failure.
        """
        ...
```

## 3. Role Defaults & Permission Helpers (`auth/permissions.py`)

```python
from typing import Any

# Predefined role → default permissions mapping.
# These are the baseline. Mapping config can override individual values.
ROLE_PERMISSION_DEFAULTS: dict[str, dict[str, Any]] = {
    "owner": {
        "data:scope": "tenant",
        "message:send": "all",
        "message:mention-human": True,
        "task:schedule": True,
        "task:manage-others": True,
        "agent:delegate": True,
    },
    "admin": {
        "data:scope": "tenant",
        "message:send": "all",
        "message:mention-human": True,
        "task:schedule": True,
        "task:manage-others": True,
        "agent:delegate": True,
    },
    "member": {
        "data:scope": "own",
        "message:send": "assigned",
        "message:mention-human": False,
        "task:schedule": False,
        "task:manage-others": False,
        "agent:delegate": False,
    },
    "viewer": {
        "data:scope": "own",
        "message:send": "assigned",
        "message:mention-human": False,
        "task:schedule": False,
        "task:manage-others": False,
        "agent:delegate": False,
    },
}

# The default permission set for newly created agents.
DEFAULT_AGENT_PERMISSIONS: dict[str, Any] = {
    "data:scope": "own",
    "message:send": "assigned",
    "message:mention-human": False,
    "task:schedule": False,
    "task:manage-others": False,
    "agent:delegate": False,
}


def permissions_for_role(role: str) -> dict[str, Any]:
    """Return default permissions for a given RoleMesh role."""
    return dict(ROLE_PERMISSION_DEFAULTS.get(role, ROLE_PERMISSION_DEFAULTS["viewer"]))
```

Note: these are USER role defaults (used by IdentityProvider during resolution) AND agent permission defaults (used when creating a new coworker). They share the same key names but are applied independently.

## 4. Declarative Mapping Engine (`auth/mapping.py`)

For embedded mode, external SaaS roles must be mapped to RoleMesh roles and permissions. The mapping is stored as JSON in the `tenants` table.

### Mapping Config Schema

```json
{
  "role_map": {
    "<external_role>": "<rolemesh_role>",
    "*": "viewer"
  },
  "permission_overrides": {
    "<external_role>": {
      "<permission_key>": "<value>"
    }
  }
}
```

### Implementation

```python
from typing import Any

from rolemesh.auth.permissions import permissions_for_role


def resolve_mapping(
    external_role: str,
    mapping_config: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """
    Map an external role to a RoleMesh role + permissions.

    Returns (rolemesh_role, permissions_dict).
    """
    role_map = mapping_config.get("role_map", {})
    overrides = mapping_config.get("permission_overrides", {})

    # Step 1: Map role (exact match, then wildcard fallback)
    rm_role = role_map.get(external_role, role_map.get("*", "viewer"))

    # Step 2: Start with role defaults
    perms = permissions_for_role(rm_role)

    # Step 3: Apply overrides for this external role
    if external_role in overrides:
        perms.update(overrides[external_role])

    return rm_role, perms
```

### Storage

Add a `auth_mapping_config` JSONB column to the `tenants` table:

```sql
ALTER TABLE tenants ADD COLUMN auth_mapping_config JSONB DEFAULT '{}';
```

Each tenant can have its own mapping (useful when different SaaS customers integrate with RoleMesh differently).

## 5. Identity Providers

### ExternalAuthProvider (`auth/external_provider.py`)

```python
"""
Embedded mode: validates JWTs issued by the host SaaS.

Configuration (per tenant):
  - JWT verification: public key URL (JWKS) or shared secret
  - Claim paths: where to find tenant_id, user_id, role in the JWT
  - Mapping config: role_map + permission_overrides (stored in DB)
"""

# Key responsibilities:
# 1. Extract Bearer token from Authorization header
# 2. Verify JWT signature (JWKS or shared secret, configurable per tenant)
# 3. Extract claims: tenant_id, user_id, external_role
# 4. Load tenant's auth_mapping_config from DB (cache with TTL)
# 5. Call resolve_mapping(external_role, mapping_config)
# 6. Return ResolvedIdentity
#
# Claim paths are configurable. Example config:
# {
#   "jwt_issuer": "https://auth.example-saas.com",
#   "jwks_url": "https://auth.example-saas.com/.well-known/jwks.json",
#   "claims": {
#     "tenant_id": "org_id",
#     "user_id": "sub",
#     "role": "role"
#   }
# }
```

### BuiltinAuthProvider (`auth/builtin_provider.py`)

```python
"""
Standalone mode: validates JWTs issued by RoleMesh itself.

This provider is simpler because:
  - JWT secret/key is known (RoleMesh controls issuance)
  - Claims follow a fixed schema (no configurable claim paths)
  - Role + permissions are read from the JWT directly (set at issuance time)
  - No mapping needed
"""

# Key responsibilities:
# 1. Extract Bearer token from Authorization header
# 2. Verify JWT with RoleMesh's own signing key
# 3. Extract: tenant_id, user_id, role, permissions from claims
# 4. Return ResolvedIdentity
#
# JWT payload schema:
# {
#   "sub": "<user_id>",
#   "tenant_id": "<tenant_id>",
#   "role": "member",
#   "permissions": { ... },   // optional override
#   "exp": 1234567890
# }
```

### Provider Selection

Configured via environment variable:

```
AUTH_PROVIDER=builtin    # standalone mode (default)
AUTH_PROVIDER=external   # embedded mode
```

In `main.py` initialization:

```python
if AUTH_PROVIDER == "external":
    identity_provider = ExternalAuthProvider(db_pool=pool)
else:
    identity_provider = BuiltinAuthProvider(jwt_secret=JWT_SECRET)
```

## 6. User-Agent Assignment

### Database Schema

```sql
CREATE TABLE user_agent_assignments (
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    user_id      UUID NOT NULL REFERENCES users(id),
    coworker_id  UUID NOT NULL REFERENCES coworkers(id),
    assigned_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_id, coworker_id)
);

CREATE INDEX idx_uaa_tenant ON user_agent_assignments(tenant_id);
CREATE INDEX idx_uaa_user   ON user_agent_assignments(user_id);
```

### Coworker Visibility

Add column to `coworkers` table:

```sql
ALTER TABLE coworkers ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public';
-- Values: 'public' | 'restricted'
```

### Access Check Logic

```python
async def check_user_agent_access(
    identity: ResolvedIdentity,
    coworker_id: str,
    db_pool: asyncpg.Pool,
) -> bool:
    """Check if a user can send messages to an agent."""
    # Admin/owner can access all agents
    if identity.is_admin_or_owner:
        return True

    # Check explicit assignment
    row = await db_pool.fetchrow(
        "SELECT 1 FROM user_agent_assignments WHERE user_id = $1 AND coworker_id = $2",
        identity.user_id,
        coworker_id,
    )
    return row is not None
```

This function is called once when a message arrives, in the gateway layer. It is NOT called inside business logic.

## 7. Agent Capability Permissions

### Database Schema Change

Add `permissions` JSONB column to `coworkers` table:

```sql
ALTER TABLE coworkers ADD COLUMN permissions JSONB NOT NULL DEFAULT '{
    "data:scope": "own",
    "message:send": "assigned",
    "message:mention-human": false,
    "task:schedule": false,
    "task:manage-others": false,
    "agent:delegate": false
}';
```

### Enforcement Points

Agent permissions are checked at IPC interception points. Here is the mapping from permission key to enforcement location:

| Permission | Check location | When |
|-----------|---------------|------|
| `data:scope` | `container_executor.py` | Injected into agent context at container start |
| `message:send` | `ipc/task_handler.py` | When agent calls `send_message` tool |
| `message:mention-human` | `ipc/task_handler.py` | When agent calls `send_message` with @mentions |
| `task:schedule` | `ipc/task_handler.py` | When agent calls `schedule_task` tool |
| `task:manage-others` | `ipc/task_handler.py` | When agent calls `pause/resume/cancel_task` on another agent's task |
| `agent:delegate` | `ipc/task_handler.py` | When agent attempts to invoke another agent |

### How to add a check (example)

In `ipc/task_handler.py`, the existing `_handle_schedule_task` function:

```python
# Before (current code checks is_main):
if not is_main:
    return {"error": "Only main coworker can manage all tasks"}

# After (check capability permission):
coworker = get_coworker(coworker_id)
if not coworker.permissions.get("task:schedule", False):
    return {"error": "This coworker does not have task:schedule permission"}
```

The pattern is always the same: read `coworker.permissions`, check the relevant key, return error if denied. No complex logic.

## 8. Integration with Existing Code

### Changes to `main.py`

```python
# At startup:
identity_provider = create_identity_provider()  # Based on AUTH_PROVIDER env

# In _handle_incoming (message arrival):
# Currently: finds conversation by binding + chat_id
# Add: resolve identity + check user-agent access
identity = await identity_provider.resolve(request)
if identity is None:
    return  # 401

if not await check_user_agent_access(identity, coworker_id, pool):
    return  # 403
```

### Changes to `core/types.py`

```python
@dataclass
class Coworker:
    # ... existing fields ...
    visibility: str = "public"                                      # NEW
    permissions: dict[str, Any] = field(default_factory=lambda:     # NEW
        dict(DEFAULT_AGENT_PERMISSIONS))
```

### Changes to `db/pg.py`

- Add `visibility` and `permissions` columns to CREATE TABLE
- Add `auth_mapping_config` column to tenants table
- Add `user_agent_assignments` table
- Update `_record_to_coworker()` to deserialize `permissions` from JSONB
- Update `create_coworker()` to accept and store `permissions`
- Add CRUD functions for `user_agent_assignments`

### Changes to `ipc/task_handler.py`

Replace `is_main` checks with `coworker.permissions[key]` checks at each handler. The `is_main` field on coworkers can be kept for backward compatibility but is no longer the primary authorization mechanism.

## 9. Migration Path

### Phase 1: Add schema + types (non-breaking)

1. Add `auth/` module with types, permissions, mapping
2. Add DB columns with defaults (existing data unaffected)
3. Add `user_agent_assignments` table (empty = all users have access, preserving current behavior)

### Phase 2: Wire up identity provider

4. Implement `BuiltinAuthProvider` (standalone mode)
5. Implement `ExternalAuthProvider` (embedded mode)
6. Add middleware to WebUI and API endpoints
7. When no assignment rows exist for a tenant, treat as "all users can access all agents" (backward compatible)

### Phase 3: Enforce agent capabilities

8. Replace `is_main` checks in `task_handler.py` with permission checks
9. Add permission checks to `send_message` handler
10. Populate `coworkers.permissions` based on existing `is_admin` values during migration

### Backward Compatibility

- Existing deployments without auth config continue to work (no identity provider = all access allowed)
- Existing `is_admin=True` coworkers get `task:manage-others=True` and `task:schedule=True` in migration
- Empty `user_agent_assignments` table means unrestricted access (opt-in enforcement)

## 10. Testing Strategy

### Unit Tests

- `tests/auth/test_types.py` — ResolvedIdentity helper methods
- `tests/auth/test_permissions.py` — Role defaults, DEFAULT_AGENT_PERMISSIONS
- `tests/auth/test_mapping.py` — Mapping engine: role resolution, overrides, wildcards, missing keys
- `tests/auth/test_middleware.py` — Auth middleware with mock providers

### Integration Tests

- `tests/auth/test_external_provider.py` — JWT verification with test keys
- `tests/auth/test_builtin_provider.py` — RoleMesh JWT issuance + verification
- `tests/auth/test_access_check.py` — User-agent assignment queries with test DB

### Existing Tests

- Update `tests/ipc/test_task_handler.py` — Verify permission checks replace `is_main` checks
- Update `tests/test_e2e.py` — Add auth headers to E2E test flows
