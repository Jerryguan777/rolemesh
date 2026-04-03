# Auth & Authorization — Complete Implementation Guide

> This is the single source of truth for implementing RoleMesh's auth system.
> Read this document end-to-end before writing any code.

## 1. What We Are Building

RoleMesh needs authentication and authorization that works in two deployment modes:

- **Embedded mode**: RoleMesh is plugged into an existing SaaS. Users are already authenticated by the SaaS. RoleMesh receives a JWT/token and needs to understand "who is this user and what can they do."
- **Standalone mode**: RoleMesh runs as an independent AaaS platform. It must handle user login, registration, and session management itself.

The auth system must be designed so that the same RoleMesh core code runs in both modes, with the only difference being a pluggable identity provider adapter.

## 2. Design Decisions (Already Made)

These decisions are final. Do not revisit them during implementation.

**AuthN is external, AuthZ is internal.** Authentication is handled by an IdentityProvider adapter. Authorization logic is always inside RoleMesh.

**User permissions and agent permissions are fully independent.** There is no intersection calculation. Users are authorized to _use_ agents. Agents are authorized to _perform_ operations. These two checks happen in series, never cross-referenced.

**Assign = full access.** Once an agent is assigned to a user, the user can use all of that agent's capabilities. If different users need different capability levels, create multiple agents with different permission configs and assign accordingly.

**Permissions are thin.** Only pure authorization decisions (yes/no) go into the permissions model. Resource limits (timeout, concurrency), tool bindings (MCP servers), security policies, and rate limiting belong to their own modules.

**Checks happen at boundaries.** Authorization is enforced at interception points (middleware, IPC handlers, credential proxy). Business logic code contains zero permission checks.

**External system auth via token passthrough.** RoleMesh does not enforce business-level permissions (e.g., "can this user access Project X"). Instead, RoleMesh passes user identity to MCP servers, and the external business system enforces its own permissions.

**data:scope has two levels only:** `own` and `tenant`. No `team` level (RoleMesh has no team concept).

## 3. Identity Contract

All RoleMesh internal code depends on one structure. No code ever inspects raw JWTs or session tokens.

```python
@dataclass(frozen=True)
class ResolvedIdentity:
    tenant_id: str
    user_id: str
    role: str                          # owner / admin / member / viewer
    permissions: dict[str, Any]        # RoleMesh user permissions
    metadata: dict[str, str]           # pass-through (email, name, etc.)
```

## 4. Identity Provider

```python
class IdentityProvider(Protocol):
    async def resolve(self, request: Request) -> ResolvedIdentity | None:
        ...
```

Selected via environment variable `AUTH_PROVIDER`:
- `builtin` (default) — standalone mode, validates RoleMesh-issued JWTs
- `external` — embedded mode, validates SaaS JWTs and maps roles

### External Mode: Role Mapping

SaaS roles don't match RoleMesh roles. A declarative JSON mapping config (stored per-tenant in the `tenants` table) handles the translation:

```json
{
  "role_map": {
    "saas:org-owner": "owner",
    "saas:admin": "admin",
    "saas:member": "member",
    "*": "viewer"
  },
  "permission_overrides": {
    "saas:admin": {
      "task:schedule": true
    }
  }
}
```

Resolution: verify JWT → extract external role → map to RoleMesh role via `role_map` → load role defaults → apply `permission_overrides` → return ResolvedIdentity.

The mapping is config, not code. Integrating with a new SaaS means writing a JSON mapping, not changing source code.

## 5. User Permissions

User permissions in RoleMesh are simple:

**Role** (owner / admin / member / viewer) — determines platform management capabilities:
- owner: tenant settings, billing, everything admin can do
- admin: create/edit/delete agents, manage users, assign agents, configure system
- member: use assigned agents, view own conversation history
- viewer: see public agent list, read-only access

**Agent assignments** — determines which agents the user can interact with:
- admin/owner: can access all agents, no assignment needed
- member: can only use explicitly assigned agents
- viewer: can see public agents but cannot send messages

Implementation: `user_agent_assignments` table (user_id, coworker_id). Assignment = can use. No assignment = cannot use (unless admin/owner).

### Agent Visibility

Agents have a `visibility` field (`public` or `restricted`):
- `public` + assigned → visible and usable
- `public` + not assigned → visible but not usable (member can see it exists)
- `restricted` + not assigned → invisible to non-admin users
- admin/owner → always visible and usable

## 6. Agent Permissions (Capabilities)

### Agent Roles

Agents have two predefined roles. The role is a template that sets default permission values:

**super_agent** — replaces the old `is_main=True`. Global visibility, cross-agent management.

**agent** — replaces the old `is_main=False`. Scoped to own data and tasks only.

The role is stored in `coworkers.agent_role`. Permissions are stored in `coworkers.permissions` (JSONB). Selecting a role auto-fills defaults, but individual permissions can be adjusted.

### Permission Fields (4 total)

| Permission | super_agent default | agent default | Meaning |
|-----------|-------------------|--------------|---------|
| `data:scope` | `tenant` | `own` | What RoleMesh data the agent can see (tasks, group snapshots) |
| `task:schedule` | `true` | `false` | Can the agent create scheduled tasks |
| `task:manage-others` | `true` | `false` | Can the agent manage other agents' tasks (pause/resume/cancel/update) |
| `agent:delegate` | `true` | `false` | Can the agent invoke other agents |

These are enforced at IPC interception points in `task_handler.py`. The agent's business logic (LLM execution) is unaware of these checks.

### What is NOT in agent permissions

| Concern | Where it lives | Reason |
|---------|---------------|--------|
| max_concurrent | `coworkers.max_concurrent` | Resource config, not authorization |
| timeout | `container_config.timeout` | Resource config |
| Which MCP servers | `coworkers.tools[]` | Tool binding config |
| Network/mount restrictions | Security module | Security policy |
| Rate limits | Credential proxy | Operational safeguard |
| @mention humans | Not controlled | Agent decides based on context |
| Cross-conversation messaging | Not supported | All agents only message within assigned conversations |

## 7. Handling is_main Migration

The `is_main` boolean field is fully replaced by `agent_role` + `permissions`. Here is how every `is_main`-dependent feature is handled:

### Features to DELETE (no longer needed)

| Feature | Current location | Action |
|---------|-----------------|--------|
| Conversation management (register_conversation, refresh_conversations) | task_handler.py, ipc_mcp.py | Remove MCP tools. Future: admin API |
| Remote control | main.py | Remove. System management via admin API |
| Project root special mount | runner.py:91-108 | Remove. Use existing `additional_mounts` config |
| Trigger mechanism bypass | main.py:337-347 | Already controlled by `requires_trigger` field on conversation, decouple from is_main |
| Cross-conversation messaging (message:send=all) | main.py:575-593 | Remove. All agents only message within their own conversations |

For these features, simplify the code as if `is_main=False` everywhere, then delete the dead branches.

### Features to REPLACE with permission checks

| Current code pattern | Replace with |
|---------------------|-------------|
| `if is_main:` allow cross-coworker task create | `if permissions["task:schedule"]:` |
| `if is_main:` allow cross-coworker task pause/resume/cancel/update | `if permissions["task:manage-others"]:` |
| `if is_main:` show all tasks/groups snapshot | `if permissions["data:scope"] == "tenant":` |
| `if is_main:` allow delegate to other agent | `if permissions["agent:delegate"]:` |
| `if is_main:` allow read-write extra mounts | Remove special case, use mount_security normal validation |

### Data migration

```sql
ALTER TABLE coworkers ADD COLUMN agent_role TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE coworkers ADD COLUMN permissions JSONB NOT NULL DEFAULT
  '{"data:scope":"own","task:schedule":false,"task:manage-others":false,"agent:delegate":false}';

UPDATE coworkers SET
  agent_role = 'super_agent',
  permissions = '{"data:scope":"tenant","task:schedule":true,"task:manage-others":true,"agent:delegate":true}'
WHERE is_main = true;

ALTER TABLE coworkers DROP COLUMN is_main;
```

### IPC protocol change

In `AgentInitData` (ipc/protocol.py): replace `is_main: bool` with `permissions: dict[str, Any]`. The agent runner reads permissions from this dict instead of checking a boolean flag.

## 8. User Identity Passthrough to MCP Servers

When an agent calls an MCP tool that accesses external business systems, RoleMesh does NOT check business-level permissions. Instead, it passes user identity to the MCP server so the business system can enforce its own permissions.

### Two-layer headers

The credential proxy injects two kinds of headers into MCP requests:

- **Server auth** (`X-Service-Auth` or the configured `McpServerConfig.headers`): proves the request comes from RoleMesh. This is the existing mechanism.
- **User identity** (`X-User-Token`): a delegation token representing the user on whose behalf the agent is acting.

### Delegation token

RoleMesh does NOT forward the original SaaS JWT (it may expire during agent execution). Instead, the orchestrator mints a short-lived delegation token:

- Signed by RoleMesh's own key
- Contains: user_id, tenant_id, role, and any relevant metadata
- Expiration = agent execution timeout (e.g. 10 minutes)
- MCP servers verify the signature using RoleMesh's public key
- This follows the OAuth 2.0 Token Exchange pattern (RFC 8693)

### Flow

1. User sends request with SaaS JWT
2. Orchestrator verifies SaaS JWT, extracts identity
3. Orchestrator mints a delegation token containing user identity
4. Delegation token is passed via AgentInitData to the container
5. When agent calls MCP tool, credential proxy injects both server auth headers and the delegation token
6. MCP server verifies RoleMesh signature (trusted source), reads user identity, checks business permissions

### Scheduled tasks (no user online)

Scheduled tasks run without an active user session. The `created_by` user_id is recorded when the task is created. At execution time, a delegation token is minted for the task creator's identity.

## 9. Complete Authorization Flow

```
User sends message
  │
  ① AuthN: IdentityProvider.resolve() → ResolvedIdentity
  │  Failure → 401
  │
  ② User access check: is this agent assigned to this user?
  │  (admin/owner bypass)
  │  Failure → 403
  │
  ③ Agent executes: LLM processes message, decides to call tools
  │
  ④ Agent capability check: does this agent have permission?
  │  Checked at IPC task_handler interception point
  │  Failure → PermissionDenied returned to agent → agent tells user
  │
  ⑤ External auth: MCP tool receives delegation token
  │  Business system checks user's business-level permissions
  │  Failure → tool returns error → agent tells user
  │
  ⑥ Success
```

Four layers, each independent, each checking a different concern.

## 10. Database Schema Changes

### New table: user_agent_assignments

```sql
CREATE TABLE user_agent_assignments (
    tenant_id    UUID NOT NULL REFERENCES tenants(id),
    user_id      UUID NOT NULL REFERENCES users(id),
    coworker_id  UUID NOT NULL REFERENCES coworkers(id),
    assigned_at  TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (user_id, coworker_id)
);
```

### Changes to coworkers table

```sql
ALTER TABLE coworkers ADD COLUMN agent_role TEXT NOT NULL DEFAULT 'agent';
ALTER TABLE coworkers ADD COLUMN visibility TEXT NOT NULL DEFAULT 'public';
ALTER TABLE coworkers ADD COLUMN permissions JSONB NOT NULL DEFAULT
  '{"data:scope":"own","task:schedule":false,"task:manage-others":false,"agent:delegate":false}';
ALTER TABLE coworkers DROP COLUMN is_main;
```

### Changes to tenants table

```sql
ALTER TABLE tenants ADD COLUMN auth_mapping_config JSONB DEFAULT '{}';
```

## 11. New Module Structure

```
src/rolemesh/auth/
  __init__.py
  types.py                # ResolvedIdentity, IdentityProvider Protocol
  permissions.py           # Role defaults, agent role defaults, checking helpers
  middleware.py            # Request-level auth interception
  builtin_provider.py      # Standalone: verify RoleMesh JWT
  external_provider.py     # Embedded: verify SaaS JWT + role mapping
  mapping.py               # Declarative role/permission mapping engine
  delegation.py            # Mint/verify delegation tokens for MCP passthrough
```

## 12. Integration Points with Existing Code

### main.py
- At startup: create identity provider based on AUTH_PROVIDER env
- In `_handle_incoming`: resolve identity → check user-agent access → proceed or reject

### core/types.py
- Coworker: add `agent_role`, `visibility`, `permissions` fields
- Remove `is_main` field (after migration)

### db/pg.py
- Add new columns and table (see schema changes above)
- Update `_record_to_coworker()` to deserialize new fields
- Add CRUD for user_agent_assignments

### ipc/protocol.py
- AgentInitData: replace `is_main: bool` with `permissions: dict`
- Add `user_delegation_token: str | None` field

### ipc/task_handler.py
- Replace every `if is_main:` with the corresponding `permissions[key]` check
- Delete handlers for register_conversation and refresh_conversations

### agent/container_executor.py
- Pass delegation token into AgentInitData
- Remove is_main-dependent logic

### container/runner.py
- Remove project root special mount for is_main
- Remove is_main visibility logic for group snapshots, use permissions["data:scope"] instead

### agent_runner/main.py
- Read permissions from AgentInitData instead of is_main flag
- Pass delegation token as header when calling MCP servers

### security/credential_proxy.py
- Add delegation token injection for MCP proxy requests (alongside existing server auth headers)

## 13. Implementation Phases

### Phase 1: Foundation (non-breaking)
- Create `auth/` module with types, permissions, mapping engine
- Add DB columns with defaults (existing behavior unchanged)
- Add user_agent_assignments table (empty = unrestricted, backward compatible)
- Add delegation token minting/verification

### Phase 2: Identity providers
- Implement BuiltinAuthProvider
- Implement ExternalAuthProvider
- Wire middleware into WebUI and API endpoints
- Empty assignment table = all access (backward compatible)

### Phase 3: Agent role migration
- Replace is_main checks with permission checks in task_handler.py
- Replace is_main checks in runner.py (snapshots, mounts)
- Update IPC protocol (is_main → permissions)
- Migrate data: is_main=true → super_agent, is_main=false → agent
- Drop is_main column

### Phase 4: Cleanup
- Delete removed features (register_conversation, refresh_conversations tools, remote control)
- Delete is_main-related dead code paths
- Update all tests

## 14. Testing

### New tests
- `tests/auth/test_types.py` — ResolvedIdentity helpers
- `tests/auth/test_permissions.py` — role defaults, agent role defaults
- `tests/auth/test_mapping.py` — mapping engine (role resolution, overrides, wildcards, fallback)
- `tests/auth/test_middleware.py` — auth middleware with mock providers
- `tests/auth/test_delegation.py` — delegation token mint/verify/expiry
- `tests/auth/test_external_provider.py` — JWT verification with test keys
- `tests/auth/test_builtin_provider.py` — RoleMesh JWT verification
- `tests/auth/test_access_check.py` — user-agent assignment queries

### Updated tests
- `tests/ipc/test_task_handler.py` — verify permission checks replace is_main checks
- `tests/test_e2e.py` — add auth flow to E2E tests
- `tests/container/test_runner.py` — verify mount logic without is_main special cases

## 15. Out of Scope (Future Work)

These are explicitly not part of this implementation:

- **Approval module** — human-in-the-loop approval for sensitive tool calls (see `docs/design/approval-module.md`)
- **Admin API for conversation management** — replacing the removed register_conversation agent tool
- **Built-in login/registration UI** — only needed for standalone mode, separate effort
- **Security module** — network policies, mount restrictions, domain allowlists
- **Rate limiting** — MCP call frequency limits at credential proxy
- **Audit logging** — recording all auth decisions for compliance
