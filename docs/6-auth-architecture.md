# Authentication & Authorization Architecture

This document describes how RoleMesh authenticates users, authorizes operations, and propagates identity through the agent execution pipeline. It covers the design constraints that shaped the system, the alternatives we considered, and why the current architecture looks the way it does.

## The Problem

RoleMesh started with a single boolean (`is_main`). "Admin" coworkers could do everything — see all tasks, manage other agents' schedules, register new conversations. Non-admin coworkers could do nothing beyond their own scope.

This worked for a single-user setup. It broke down when we needed:

1. **Multi-tenant isolation** — different organizations sharing the same RoleMesh instance
2. **Granular agent capabilities** — an agent that can schedule tasks but cannot invoke other agents
3. **User-level access control** — who can use which agents, who can manage the platform
4. **Three deployment modes** — embedded in an existing SaaS, standalone platform, or OIDC-integrated
5. **Secure identity forwarding** — when an agent calls an external MCP server on behalf of a user, the MCP server needs to know _which_ user, with a fresh IdP-issued token

## Design Principles

Five rules that guide every auth decision in RoleMesh:

**1. AuthN is external, AuthZ is internal.** Authentication ("who are you?") is delegated to a pluggable provider. Authorization ("what can you do?") is always RoleMesh's own logic. No business code ever inspects a raw JWT.

**2. Checks happen at boundaries, not in business logic.** Authorization is enforced at exactly four interception points. The orchestration code, the container runner, and the agent SDK integration contain zero permission checks.

**3. Permissions stay thin.** Only pure yes/no authorization decisions live in the permission model. Resource limits (timeout, concurrency), tool bindings (MCP servers), security policies (mount allowlists), and rate limiting belong to their own modules.

**4. User permissions and agent permissions are independent.** Users are authorized to _use_ agents. Agents are authorized to _perform_ operations. These two checks happen in series, never intersected. This eliminates a combinatorial explosion of user-times-agent permission matrices.

**5. Assign = full access.** Once an agent is assigned to a user, the user can use all of that agent's capabilities. If different users need different capability levels, create multiple agents with different permission configs and assign them accordingly. This keeps the model simple.

## Architecture Overview

```
                     ┌──────────────────────────────┐
                     │      External Auth            │
                     │                               │
                     │  External: SaaS JWT           │
                     │  Builtin: RoleMesh JWT        │
                     │  OIDC: IdP id_token (PKCE)    │
                     └──────────────┬────────────────┘
                                    │
                                    ▼
                           AuthProvider (Protocol)
                           authenticate(token) → AuthenticatedUser
                                    │
                     ┌──────────────┼──────────────────┐
                     │              │                   │
                     ▼              ▼                   ▼
            ExternalJwtProvider  BuiltinProvider     OIDCAuthProvider
              (validates SaaS   (stub, not yet      (JWKS validation,
               JWT, maps        implemented)         claim mapping,
               claims)                               JIT provisioning)
                     │                                  │
                     └──────────┬───────────────────────┘
                                ▼
              ┌─────────────────────────────────────┐
              │  RoleMesh Core (same for all modes) │
              │                                     │
              │  User role check (owner/admin/member)│
              │  Agent assignment check              │
              │  Agent permissions check             │
              │  MCP token forwarding (TokenVault)   │
              └─────────────────────────────────────┘
```

## Three Deployment Modes

### External Mode

RoleMesh runs inside an existing SaaS. Users authenticate with the SaaS, which issues JWTs. RoleMesh validates these JWTs and extracts identity.

Configuration via environment variables:

```
AUTH_MODE=external
EXTERNAL_JWT_SECRET=<symmetric-secret>          # or EXTERNAL_JWT_PUBLIC_KEY for RS256
EXTERNAL_JWT_ISSUER=https://auth.your-saas.com  # optional
EXTERNAL_JWT_ALGORITHMS=HS256                   # comma-separated
EXTERNAL_JWT_CLAIM_USER_ID=sub                  # claim name mapping
EXTERNAL_JWT_CLAIM_TENANT_ID=tid
EXTERNAL_JWT_CLAIM_ROLE=role
```

The claim mapping is config, not code. Integrating with a new SaaS means setting environment variables, not writing an adapter.

### OIDC Mode (Current Focus)

RoleMesh connects to any OIDC-compliant IdP (Okta, Azure AD, Keycloak, Auth0) and handles the full browser-based login flow via PKCE. Users are JIT-provisioned on first login.

Configuration:

```
AUTH_MODE=oidc
OIDC_DISCOVERY_URL=https://idp.example.com/.well-known/openid-configuration
OIDC_CLIENT_ID=rolemesh
OIDC_CLIENT_SECRET=                             # optional (public clients)
OIDC_AUDIENCE=rolemesh                          # defaults to client_id
OIDC_SCOPES=openid profile email offline_access
OIDC_REDIRECT_URI=https://app.example.com/oauth2/callback

# Claim mapping for role resolution (all optional)
OIDC_CLAIM_ROLE=role                            # direct role claim
OIDC_CLAIM_GROUPS=groups                        # group membership claim
OIDC_GROUP_ROLE_MAP={"FirmAdministrators":"admin","Developers":"member"}
OIDC_SCOPE_ROLE_MAP={"admin:rolemesh":"admin"}  # fallback scope mapping
OIDC_CLAIM_TENANT_ID=tid                        # multi-tenant claim

# Auto-assignment
OIDC_AUTO_ASSIGN_TO_ALL=true                    # new users get all coworkers

# Token vault for MCP forwarding
ROLEMESH_TOKEN_SECRET=<any-secret>              # Fernet encryption key derivation
```

> **Both processes need the OIDC env.** `create_vault_from_env()` runs in
> the orchestrator *and* the webui process, and returns `None` without
> `OIDC_DISCOVERY_URL` + `OIDC_CLIENT_ID`. If only webui gets them, the
> orchestrator never subscribes the `egress.token.access.request`
> responder, the gateway's `RemoteTokenVault` RPC gets "no responders",
> and every user-mode MCP request forwards an empty `Authorization` header.
> Wire `OIDC_DISCOVERY_URL` / `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET` (and
> `ROLEMESH_TOKEN_SECRET`) into both services.

OIDC mode role resolution priority: direct role claim → group mapping → scope mapping → fallback "member".

### Builtin Mode (Stub)

`BuiltinProvider` exists as a placeholder. When implemented, it will handle user registration, login, password hashing (bcrypt), and JWT issuance. The `users.password_hash` column is already in the schema.

## OIDC Architecture

### Subpackage: `src/rolemesh/auth/oidc/`

The OIDC implementation is split into focused modules:

| File | Purpose |
|------|---------|
| `config.py` | `OIDCConfig` frozen dataclass + `from_env()`. Aggregates all IdP-level settings. Cookie vars excluded (webui-only). |
| `discovery.py` | `DiscoveryDocument` dataclass (issuer, endpoints, jwks_uri). |
| `jwks.py` | `JWKSManager` — async JWKS fetch + cache with key rotation handling. Uses `asyncio.Lock`. |
| `algorithms.py` | `ALLOWED_ALGORITHMS` — whitelist of 8 JWT algorithms to prevent algorithm confusion attacks. |
| `adapter.py` | `OIDCAdapter` Protocol + `DefaultOIDCAdapter` for pluggable claim mapping. |
| `provider.py` | `OIDCAuthProvider` — the main provider: id_token validation, JIT tenant/user provisioning, auto-assign. |

### Login Flow

The browser drives a standard OIDC PKCE flow against the IdP, exchanges the resulting authorization code with the WebUI, receives an `id_token` (and a paired httpOnly refresh cookie), and refreshes silently before expiry. The full UX (which endpoint the SPA hits in which order, how `sessionStorage` and the refresh cookie cooperate, what happens during silent refresh) lives in [`5-webui-architecture.md`](5-webui-architecture.md) — that's the layer that owns the browser and the FastAPI route handlers.

What `OIDCAuthProvider` is responsible for, regardless of who's calling it:

- Validate the `id_token` signature against the IdP's JWKS (with key rotation handling).
- Validate `iss`, `aud`, `exp`, and reject any algorithm outside `ALLOWED_ALGORITHMS` (defends against algorithm confusion).
- Map claims to a tenant/user via `OIDCAdapter` (see below).
- JIT-provision tenant and user on first sight; mirror the IdP-issued refresh / access tokens into the per-user vault for downstream MCP calls.

### JIT Provisioning

On first OIDC login:

1. **Tenant resolution**: `OIDCAdapter.map_tenant_id(claims)` extracts external tenant ID. If empty → single-tenant mode → use `default` tenant. Otherwise → look up `external_tenant_map` → JIT-create tenant if not found.
2. **User creation**: Look up by `external_sub`. If not found → `create_user_with_external_sub()` → call `OIDCAdapter.on_user_provisioned()` hook.
3. **Auto-assign** (if `OIDC_AUTO_ASSIGN_TO_ALL=true`): New user gets assigned to every coworker in the tenant. Existing users are NOT re-assigned on login (admin may have intentionally unassigned).

On subsequent logins: sync changeable fields (name, email, role) from claims.

### OIDCAdapter Protocol

Custom IdP-specific claim mapping can be plugged in via `OIDC_ADAPTER=module.path.ClassName`:

```python
class OIDCAdapter(Protocol):
    def map_role(self, claims: dict[str, Any]) -> str: ...
    def map_tenant_id(self, claims: dict[str, Any]) -> str: ...
    async def on_tenant_provisioned(self, tenant_id: str, claims: dict[str, Any]) -> None: ...
    async def on_user_provisioned(self, user_id: str, tenant_id: str, claims: dict[str, Any]) -> None: ...
```

`DefaultOIDCAdapter` supports three role mapping strategies via env vars:

- **Direct claim**: `OIDC_CLAIM_ROLE` — value must be owner/admin/member
- **Group mapping**: `OIDC_CLAIM_GROUPS` + `OIDC_GROUP_ROLE_MAP` (JSON dict)
- **Scope mapping**: `OIDC_SCOPE_ROLE_MAP` (JSON dict)

Invalid role map values are rejected at startup with `logger.error`; unmatched groups/scopes emit `logger.warning` at runtime.

## User Roles

Three roles, deliberately minimal:

| Role | Manage tenant | Manage agents | Manage users | View all conversations | Use agents |
|------|:---:|:---:|:---:|:---:|:---:|
| **owner** | yes | yes | yes | yes | yes |
| **admin** | — | yes | yes | yes | yes |
| **member** | — | — | — | — | assigned only |

Implementation: `user_can(role, action) -> bool` in `src/rolemesh/auth/permissions.py`. A lookup table, not a rule engine.

### User-Agent Assignment

The `user_agent_assignments` table maps users to coworkers. Member-role users can only see and use agents explicitly assigned to them. Admin/owner bypass assignment checks.

```sql
CREATE TABLE user_agent_assignments (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    coworker_id UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    UNIQUE (user_id, coworker_id)
);
```

CRUD lives in `src/rolemesh/db/user.py` (`assign_agent_to_user()`, `unassign_agent_from_user()`, `get_agents_for_user()`, `get_users_for_agent()`).

## Agent Permissions

### Why Not Just Use Roles?

We considered three approaches for agent authorization:

| Approach | Pros | Cons |
|----------|------|------|
| **Single boolean** (`is_main`) | Simple | All-or-nothing. Can't have an agent that schedules tasks but can't manage others' tasks. |
| **Full RBAC** (roles + permissions + resources) | Maximally flexible | Overkill for 3 capabilities. Combinatorial explosion. Hard to reason about. |
| **Flat capability bits** | Simple to understand, covers real use cases, no abstraction overhead | Can't express deeply nested policies (but we don't need to) |

We chose **flat capability bits**. An agent carries three booleans that default to least-privilege (all `false`); each is granted explicitly. There is no role template — an earlier `super_agent`/`agent` role and a `data_scope` axis were removed as over-design (the role only packed defaults, and `data_scope` doubled as a host-filesystem mount switch and a full-tenant data window that conflicted with the sandbox-isolation model).

The legacy `is_admin` column and the `agent_role` column have both been fully removed. The sole authority is the permissions JSONB.

### The Three Permission Fields

```python
@dataclass(frozen=True)
class AgentPermissions:
    task_schedule: bool = False
    task_manage_others: bool = False
    agent_delegate: bool = False
```

| Permission | Default | What it controls |
|-----------|:---:|---|
| `task_schedule` | `false` | Can create scheduled tasks (cron, interval, once). |
| `task_manage_others` | `false` | Can pause/resume/cancel/update other agents' tasks. **Also** grants visibility of other agents' tasks in the snapshot — managing requires seeing. |
| `agent_delegate` | `false` | Can invoke other agents (reserved for a future frontdesk agent; not yet enabled). |

### What is NOT in Agent Permissions

This is at least as important as what is in: keeping permissions thin (Design Principle 3) means resource and tool concerns belong elsewhere.

| Concern | Where it lives | Why not permissions |
|---------|---------------|-------------------|
| Max concurrent containers | `coworkers.max_concurrent_containers` | Resource limit, not authorization |
| Container timeout | `container_config.timeout` | Resource limit |
| Which MCP servers are available | `coworkers.tools[]` | Tool binding, orthogonal to auth |
| Mount restrictions | `mount_security.py` with external allowlist | Security policy, not capability |
| Rate limiting | Credential proxy | Operational safeguard |
| Cross-conversation messaging | Removed for all agents | Architectural decision, not per-agent |

### Storage and IPC contract

Permissions are stored as a JSONB column on the `coworkers` table. They flow into a running agent through `AgentInitData` (the NATS KV bootstrap payload — see [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md)). The IPC contract is one sentence: **payloads carry `tenantId + coworkerId` but never carry the permissions themselves; the orchestrator looks up the authoritative permissions for that coworker before honoring any Channel 4 / Channel 5 request**, so an agent cannot escalate by editing the payload.

Missing or empty permissions on deserialize coerce to the least-privilege default (all `false`).

## MCP Token Forwarding: TokenVault

### The Problem

When a user asks an agent to access external data via MCP, the MCP server needs to know _which user_ is making the request. Simple token forwarding fails because agents can run for 30+ minutes, outliving typical 1-hour IdP token TTLs.

### The Solution: Per-User Server-Side Token Vault

Instead of issuing RoleMesh-signed JWTs (which MCP servers can't verify without RoleMesh's secret), we forward the IdP's own access tokens, which MCP servers already trust via OIDC discovery.

```
Login (once):
  Browser → /api/auth/exchange → backend gets id_token + refresh_token
  Backend stores (encrypted) in oidc_user_tokens; sets httpOnly refresh cookie

Agent execution (many MCP calls):
  Container → MCP request via credential proxy with X-RoleMesh-User-Id
  Credential proxy:
    1. Look up cached access_token for this user
    2. If close to expiry → refresh against IdP, persist new tokens
    3. Inject Authorization: Bearer <fresh access_token>
    4. Forward to MCP server
  MCP server validates the access_token via OIDC discovery (standard flow)
```

**Location**: `src/rolemesh/auth/token_vault.py`. The vault encrypts refresh tokens at rest, deduplicates concurrent refreshes per-user, and handles refresh-token rotation when the IdP issues a new one. Detailed mechanics (encryption choice, lock granularity, threshold tuning) are implementation details inside that module — they don't shape the contract.

### MCP Server Auth Modes

Each MCP server can be configured with an `auth_mode`:

| auth_mode | Per-server headers | User token | Use case |
|-----------|:---:|:---:|---|
| `user` (default) | Injected, but `Authorization` overridden by user token | ✓ | OIDC-aware MCP server |
| `service` | Fully injected (including admin-set `Authorization`) | ✗ | Service-to-service / legacy MCP |
| `both` | Injected + user token via `X-User-Authorization` header | ✓ | Dual-layer verification |

How tokens are wired into specific MCP servers (proxy URLs in `AgentInitData.mcp_servers`, host-side `Authorization` rewrite) is covered in [`external-mcp-architecture.md`](external-mcp-architecture.md).

## Authorization Enforcement: Four Interception Points

All authorization happens at exactly four places. Business logic is clean.

### 1. WebUI / HTTP Middleware

`src/webui/auth.py` validates request tokens via `AuthProvider.authenticate()` for every REST and WebSocket handler. The dev/test `BOOTSTRAP_USERS` fast-path and the OIDC PKCE flow hook in here. Every path yields an `AuthenticatedUser` whose `user_id` is a real UUID — the external JWT provider enforces this at the boundary (a non-UUID `user-id` claim is rejected), so audit / `created_by` FK writes never need to coerce a pseudo-user. Surface details (which paths, refresh handling, `?token=` query param semantics) live in [`5-webui-architecture.md`](5-webui-architecture.md).

### 2. IPC Task Handler

The central enforcement point for agent capabilities. Every task IPC request passes through `process_task_ipc()` in `src/rolemesh/ipc/task_handler.py` with the agent's `AgentPermissions`:

```python
async def process_task_ipc(
    data: dict,
    source_group: str,
    permissions: AgentPermissions,   # <-- authorization context
    deps: IpcDeps,
    tenant_id: str,
    coworker_id: str,
) -> None:
```

Authorization checks use pure functions from `src/rolemesh/auth/authorization.py`:

```python
if not can_schedule_task(permissions):
    return  # blocked

if not can_manage_task(permissions, task.coworker_id, self_coworker_id):
    return  # blocked
```

These functions have no side effects, no DB access, no logging. They return `bool`. This makes them trivial to unit-test and trivial to reason about: the same input always produces the same authorization decision.

### 3. IPC Message Handler

In the orchestrator's message dispatch path: all agents can only send messages to their own conversations. There is no admin bypass — no permission unlocks cross-conversation messaging, because that's an architectural choice, not a permission (see "What is NOT in Agent Permissions").

### 4. Container Builder

`src/rolemesh/container/runner.py:build_volume_mounts()` never mounts the host project root into any container. Snapshot visibility is gated by `task_manage_others`:

```python
async def write_tasks_snapshot(transport, tenant_id, coworker_folder, permissions=None, tasks=None):
    if permissions.task_manage_others:
        filtered = tasks                      # see all tenant tasks
    else:
        filtered = [t for t in tasks if t["coworkerFolder"] == coworker_folder]
```

Agents without `task_manage_others` only see their own tasks in snapshots — the orchestrator pre-filters Channel 6's snapshots so even a buggy `list_tasks` call cannot read another agent's data.

## Permission Propagation

Permissions flow from `coworkers.permissions` (DB) through `AgentInitData` (NATS KV) into the container, where the agent_runner reads them as a plain `dict[str, object]` and passes that dict to the IPC tool gating layer.

The IPC wire format is a `dict`, not the `AgentPermissions` dataclass, by design: the agent_runner runs inside a Docker container with a deliberately minimal Python dependency set, so the container side can do `permissions.get("task_schedule")` without importing the dataclass module. The dataclass is used host-side (where richer typing is appropriate) and converted with `to_dict()` at the IPC boundary.

The full IPC payload contract — including how `tenantId` / `coworkerId` are set by the agent_runner (not the LLM) and re-checked by the orchestrator — lives in [`2-nats-ipc-architecture.md`](2-nats-ipc-architecture.md).

## Database Schema

| Table | Purpose |
|-------|---------|
| `users` | User accounts (local ID, `external_sub` for OIDC, role, `password_hash` for builtin) |
| `coworkers` | Agent definitions (`permissions` JSONB, tools, container_config) |
| `user_agent_assignments` | Many-to-many user ↔ coworker mapping |
| `external_tenant_map` | Maps `(provider, external_tenant_id) → local tenant_id` for OIDC multi-tenant |
| `oidc_user_tokens` | Encrypted per-user refresh_token + cached access_token for TokenVault |

Schema mechanics (default-tenant creation, idempotent `_create_schema()` shape) are described in [`4-multi-tenant-architecture.md`](4-multi-tenant-architecture.md).

## Admin API

`src/webui/admin.py` exposes RESTful endpoints under `/api/admin/` for tenant, user, agent, binding, conversation, and task management — protected by user-role checks. The full surface (which endpoints exist, which module owns each one) is documented in the "Beyond chat: Admin surface" section of [`5-webui-architecture.md`](5-webui-architecture.md).

## File Map

| File | Purpose |
|------|---------|
| `src/rolemesh/auth/permissions.py` | `AgentPermissions`, `UserRole`, `user_can()` |
| `src/rolemesh/auth/authorization.py` | Pure auth functions: `can_schedule_task()`, `can_manage_task()`, `can_delegate()` |
| `src/rolemesh/auth/provider.py` | `AuthProvider` protocol, `AuthenticatedUser` dataclass |
| `src/rolemesh/auth/external_jwt_provider.py` | Validates external SaaS JWTs |
| `src/rolemesh/auth/builtin_provider.py` | Stub for future builtin auth |
| `src/rolemesh/auth/factory.py` | `create_auth_provider(mode)` factory |
| `src/rolemesh/auth/token_vault.py` | Encrypted per-user token store with automatic IdP refresh |
| `src/rolemesh/auth/oidc/{config,discovery,jwks,algorithms,adapter,provider}.py` | OIDC submodules (see "Subpackage" table above) |
| `src/rolemesh/db/user.py`, `db/coworker.py`, … | Per-entity CRUD (split out of the legacy `pg.py` by the refactor/db PR) |
| `src/rolemesh/db/schema.py` | DDL — table / index / RLS / migration steps (idempotent `_create_schema()`) |
| `src/webui/auth.py` | WebUI auth initialization and request token validation |
| `src/webui/oidc_routes.py` | OIDC PKCE endpoints (config, exchange, refresh, logout, callback) |
| `src/webui/admin.py` | RESTful Admin API |
| `src/rolemesh/security/credential_proxy.py` | MCP proxy with per-user token injection |
| `web/src/services/oidc-auth.ts` | Client-side PKCE flow + token management |

## Not Yet Implemented

| Feature | Status | Notes |
|---------|--------|-------|
| BuiltinProvider | Stub | Needs login/register endpoints, password hashing, JWT issuance |
| `agent_delegate` enforcement | Schema only | Multi-agent delegation protocol not yet defined |
| Agent `visibility` field | Not started | `public` / `restricted` visibility for non-admin users |
| Multi-IdP support | Structural readiness only | `OIDCConfig` + provider key are instance-level; registry not built |
