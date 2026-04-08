"""PostgreSQL database operations using asyncpg."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import asyncpg

from rolemesh.auth.permissions import AgentPermissions
from rolemesh.core.config import DATABASE_URL
from rolemesh.core.group_folder import is_valid_group_folder
from rolemesh.core.logger import get_logger
from rolemesh.core.types import (
    ChannelBinding,
    ContainerConfig,
    Conversation,
    Coworker,
    McpServerConfig,
    NewMessage,
    RegisteredGroup,
    ScheduledTask,
    TaskRunLog,
    Tenant,
    User,
)

logger = get_logger()


def _to_dt(ts: str | None) -> datetime | None:
    """Convert an ISO timestamp string to a datetime object for asyncpg."""
    if not ts:
        return None
    return datetime.fromisoformat(ts)


_pool: asyncpg.Pool[asyncpg.Record] | None = None
DEFAULT_TENANT: str = "default"


def _get_pool() -> asyncpg.Pool[asyncpg.Record]:
    """Return the module-level connection pool, asserting it is initialized."""
    assert _pool is not None, "Database not initialized. Call await init_database() first."
    return _pool


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


async def _create_schema(conn: asyncpg.pool.PoolConnectionProxy[asyncpg.Record]) -> None:
    """Create tables and indexes.

    Handles upgrade from Step 4 (legacy tables) to Step 5 (multi-tenant).
    Legacy tables (messages, sessions, scheduled_tasks, task_run_logs) may exist
    with a different schema. We detect this and skip new-format table creation
    until the migration script has run.
    """
    # --- New multi-tenant tables ---
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tenants (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            slug TEXT UNIQUE,
            name TEXT NOT NULL,
            plan TEXT,
            max_concurrent_containers INT DEFAULT 5,
            last_message_cursor TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            name TEXT NOT NULL,
            email TEXT,
            role TEXT DEFAULT 'member',
            channel_ids JSONB DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT now()
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS coworkers (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            name TEXT NOT NULL,
            folder TEXT NOT NULL,
            agent_backend TEXT DEFAULT 'claude-code',
            system_prompt TEXT,
            tools JSONB DEFAULT '[]',
            skills JSONB DEFAULT '[]',
            is_admin BOOLEAN DEFAULT FALSE,
            container_config JSONB,
            max_concurrent INT DEFAULT 2,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (tenant_id, folder)
        )
    """)

    # Migrate from roles table if it exists (Step 5 -> merged schema)
    has_roles = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM information_schema.tables WHERE table_name='roles')")
    if has_roles:
        has_role_id = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='coworkers' AND column_name='role_id')"
        )
        if has_role_id:
            for col, default in [
                ("agent_backend", "'claude-code'"),
                ("system_prompt", "NULL"),
                ("tools", "'[]'::jsonb"),
                ("skills", "'[]'::jsonb"),
            ]:
                await conn.execute(
                    f"ALTER TABLE coworkers ADD COLUMN IF NOT EXISTS {col} "
                    f"{'JSONB' if col in ('tools', 'skills') else 'TEXT'} DEFAULT {default}"
                )
            await conn.execute("""
                UPDATE coworkers SET
                    agent_backend = r.agent_backend,
                    system_prompt = r.system_prompt,
                    tools = r.tools,
                    skills = r.skills
                FROM roles r WHERE coworkers.role_id = r.id
            """)
            await conn.execute("ALTER TABLE coworkers DROP COLUMN role_id")
        await conn.execute("DROP TABLE IF EXISTS roles CASCADE")
    # --- Auth: add agent_role + permissions to coworkers, migrate is_admin ---
    await conn.execute(
        "ALTER TABLE coworkers ADD COLUMN IF NOT EXISTS agent_role TEXT DEFAULT 'agent'"
    )
    await conn.execute(
        "ALTER TABLE coworkers ADD COLUMN IF NOT EXISTS permissions JSONB "
        "DEFAULT '{\"data_scope\":\"self\",\"task_schedule\":false,"
        "\"task_manage_others\":false,\"agent_delegate\":false}'"
    )
    # Idempotent migration: is_admin=TRUE -> super_agent + full permissions
    await conn.execute("""
        UPDATE coworkers SET
            agent_role = 'super_agent',
            permissions = '{"data_scope":"tenant","task_schedule":true,"task_manage_others":true,"agent_delegate":true}'
        WHERE is_admin = TRUE AND agent_role = 'agent'
    """)
    # User-agent assignment table
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_agent_assignments (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            coworker_id UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            assigned_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (user_id, coworker_id)
        )
    """)
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_uaa_user ON user_agent_assignments(user_id)")
    await conn.execute("CREATE INDEX IF NOT EXISTS idx_uaa_coworker ON user_agent_assignments(coworker_id)")
    # Password hash for future builtin auth
    await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT")

    # OIDC: external subject identifier (sub claim from IdP)
    await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS external_sub TEXT")
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_external_sub "
        "ON users(external_sub) WHERE external_sub IS NOT NULL"
    )

    # OIDC: external tenant mapping (one IdP tenant → one local tenant)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS external_tenant_map (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            provider TEXT NOT NULL DEFAULT 'oidc',
            external_tenant_id TEXT NOT NULL,
            local_tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (provider, external_tenant_id)
        )
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS channel_bindings (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            coworker_id UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            channel_type TEXT NOT NULL,
            credentials JSONB NOT NULL DEFAULT '{}',
            bot_display_name TEXT,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (coworker_id, channel_type)
        )
    """)
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            coworker_id UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
            channel_binding_id UUID NOT NULL REFERENCES channel_bindings(id),
            channel_chat_id TEXT NOT NULL,
            name TEXT,
            requires_trigger BOOLEAN DEFAULT TRUE,
            last_agent_invocation TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (channel_binding_id, channel_chat_id)
        )
    """)

    # Auth: add user_id to conversations (nullable — Telegram/Slack groups have no single owner)
    await conn.execute(
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id)"
    )

    # --- Tables that exist in both legacy (Step 4) and new (Step 5) formats ---
    # Detect if legacy messages table exists (has chat_jid column).
    # If so, skip creating new-format tables — migration script will handle it.
    legacy_exists = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM information_schema.columns WHERE table_name='messages' AND column_name='chat_jid')"
    )

    if not legacy_exists:
        # Fresh install or post-migration: create new-format tables
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                conversation_id UUID PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
                tenant_id UUID NOT NULL REFERENCES tenants(id),
                coworker_id UUID NOT NULL REFERENCES coworkers(id),
                session_id TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT NOT NULL,
                tenant_id UUID NOT NULL REFERENCES tenants(id),
                conversation_id UUID NOT NULL REFERENCES conversations(id),
                sender TEXT,
                sender_name TEXT,
                content TEXT,
                timestamp TIMESTAMPTZ NOT NULL,
                is_from_me BOOLEAN DEFAULT FALSE,
                is_bot_message BOOLEAN DEFAULT FALSE,
                PRIMARY KEY (tenant_id, id, conversation_id)
            )
        """)
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(tenant_id, conversation_id, timestamp)"
        )
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id UUID NOT NULL REFERENCES tenants(id),
                coworker_id UUID NOT NULL REFERENCES coworkers(id),
                conversation_id UUID REFERENCES conversations(id),
                prompt TEXT NOT NULL,
                schedule_type TEXT NOT NULL,
                schedule_value TEXT NOT NULL,
                context_mode TEXT DEFAULT 'isolated',
                next_run TIMESTAMPTZ,
                last_run TIMESTAMPTZ,
                last_result TEXT,
                status TEXT DEFAULT 'active',
                created_at TIMESTAMPTZ DEFAULT now()
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_next ON scheduled_tasks(tenant_id, next_run)")
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON scheduled_tasks(tenant_id, status)")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS task_run_logs (
                id SERIAL PRIMARY KEY,
                tenant_id UUID NOT NULL REFERENCES tenants(id),
                task_id UUID NOT NULL REFERENCES scheduled_tasks(id) ON DELETE CASCADE,
                run_at TIMESTAMPTZ NOT NULL,
                duration_ms INT NOT NULL,
                status TEXT NOT NULL,
                result TEXT,
                error TEXT
            )
        """)
        await conn.execute("CREATE INDEX IF NOT EXISTS idx_task_run_logs ON task_run_logs(task_id, run_at)")


async def init_database(database_url: str | None = None) -> None:
    """Initialize PostgreSQL connection pool and create schema."""
    global _pool
    url = database_url or DATABASE_URL
    _pool = await asyncpg.create_pool(url, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        await _create_schema(conn)


async def _init_test_database(database_url: str) -> None:
    """Initialize a test database with a fresh schema."""
    global _pool
    _pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
    async with _pool.acquire() as conn:
        # Drop all tables for a clean slate
        await conn.execute("DROP TABLE IF EXISTS user_agent_assignments CASCADE")
        await conn.execute("DROP TABLE IF EXISTS task_run_logs CASCADE")
        await conn.execute("DROP TABLE IF EXISTS messages CASCADE")
        await conn.execute("DROP TABLE IF EXISTS scheduled_tasks CASCADE")
        await conn.execute("DROP TABLE IF EXISTS sessions CASCADE")
        await conn.execute("DROP TABLE IF EXISTS sessions_legacy CASCADE")
        await conn.execute("DROP TABLE IF EXISTS conversations CASCADE")
        await conn.execute("DROP TABLE IF EXISTS channel_bindings CASCADE")
        await conn.execute("DROP TABLE IF EXISTS coworkers CASCADE")
        await conn.execute("DROP TABLE IF EXISTS roles CASCADE")
        await conn.execute("DROP TABLE IF EXISTS users CASCADE")
        await conn.execute("DROP TABLE IF EXISTS tenants CASCADE")
        await conn.execute("DROP TABLE IF EXISTS chats CASCADE")
        await conn.execute("DROP TABLE IF EXISTS registered_groups CASCADE")
        await conn.execute("DROP TABLE IF EXISTS router_state CASCADE")
        await _create_schema(conn)


async def close_database() -> None:
    """Close the connection pool. Call on shutdown."""
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# Tenant CRUD
# ---------------------------------------------------------------------------


async def create_tenant(
    name: str,
    slug: str | None = None,
    plan: str | None = None,
    max_concurrent_containers: int = 5,
) -> Tenant:
    """Create a new tenant and return it."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO tenants (slug, name, plan, max_concurrent_containers)
            VALUES ($1, $2, $3, $4)
            RETURNING id, slug, name, plan, max_concurrent_containers, last_message_cursor, created_at
            """,
            slug,
            name,
            plan,
            max_concurrent_containers,
        )
    assert row is not None
    return _record_to_tenant(row)


def _record_to_tenant(row: asyncpg.Record) -> Tenant:
    lmc = row["last_message_cursor"]
    return Tenant(
        id=str(row["id"]),
        name=row["name"],
        slug=row["slug"],
        plan=row["plan"],
        max_concurrent_containers=row["max_concurrent_containers"],
        last_message_cursor=lmc.isoformat() if lmc else None,
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
    )


async def get_tenant(tenant_id: str) -> Tenant | None:
    """Get a tenant by ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM tenants WHERE id = $1::uuid", tenant_id)
    if row is None:
        return None
    return _record_to_tenant(row)


async def update_tenant(
    tenant_id: str,
    *,
    name: str | None = None,
    max_concurrent_containers: int | None = None,
) -> Tenant | None:
    """Update selected fields on a tenant."""
    fields: list[str] = []
    values: list[Any] = []
    param_idx = 1

    if name is not None:
        fields.append(f"name = ${param_idx}")
        values.append(name)
        param_idx += 1
    if max_concurrent_containers is not None:
        fields.append(f"max_concurrent_containers = ${param_idx}")
        values.append(max_concurrent_containers)
        param_idx += 1

    if not fields:
        return await get_tenant(tenant_id)

    values.append(tenant_id)
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE tenants SET {', '.join(fields)} WHERE id = ${param_idx}::uuid RETURNING *",
            *values,
        )
    if row is None:
        return None
    return _record_to_tenant(row)


async def get_tenant_by_slug(slug: str) -> Tenant | None:
    """Get a tenant by slug."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM tenants WHERE slug = $1", slug)
    if row is None:
        return None
    return _record_to_tenant(row)


async def get_all_tenants() -> list[Tenant]:
    """Get all tenants."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM tenants ORDER BY created_at")
    return [_record_to_tenant(row) for row in rows]


async def update_tenant_message_cursor(tenant_id: str, cursor: str) -> None:
    """Update the last_message_cursor for a tenant."""
    pool = _get_pool()
    ts = datetime.fromisoformat(cursor) if cursor else None
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE tenants SET last_message_cursor = $1 WHERE id = $2::uuid",
            ts,
            tenant_id,
        )


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------


async def create_user(
    tenant_id: str,
    name: str,
    email: str | None = None,
    role: str = "member",
    channel_ids: dict[str, str] | None = None,
) -> User:
    """Create a new user."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (tenant_id, name, email, role, channel_ids)
            VALUES ($1::uuid, $2, $3, $4, $5::jsonb)
            RETURNING id, tenant_id, name, email, role, channel_ids, created_at
            """,
            tenant_id,
            name,
            email,
            role,
            json.dumps(channel_ids or {}),
        )
    assert row is not None
    return _record_to_user(row)


def _record_to_user(row: asyncpg.Record) -> User:
    cids = row["channel_ids"]
    return User(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        name=row["name"],
        email=row["email"],
        role=row["role"],
        channel_ids=cids if isinstance(cids, dict) else json.loads(cids) if cids else {},
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        external_sub=row.get("external_sub"),
    )


async def get_user_by_external_sub(external_sub: str) -> User | None:
    """Look up a user by their external OIDC subject identifier."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE external_sub = $1", external_sub)
    if row is None:
        return None
    return _record_to_user(row)


async def create_user_with_external_sub(
    tenant_id: str,
    name: str,
    email: str | None,
    role: str,
    external_sub: str,
) -> User:
    """Create a user linked to an external OIDC subject."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO users (tenant_id, name, email, role, channel_ids, external_sub)
            VALUES ($1::uuid, $2, $3, $4, '{}'::jsonb, $5)
            RETURNING *
            """,
            tenant_id,
            name,
            email,
            role,
            external_sub,
        )
    assert row is not None
    return _record_to_user(row)


async def get_local_tenant_id(provider: str, external_tenant_id: str) -> str | None:
    """Look up the local tenant ID for an external IdP tenant."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT local_tenant_id FROM external_tenant_map WHERE provider = $1 AND external_tenant_id = $2",
            provider,
            external_tenant_id,
        )
    if row is None:
        return None
    return str(row["local_tenant_id"])


async def create_external_tenant_mapping(
    provider: str,
    external_tenant_id: str,
    local_tenant_id: str,
) -> None:
    """Create a mapping between an external IdP tenant and a local tenant."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO external_tenant_map (provider, external_tenant_id, local_tenant_id)
            VALUES ($1, $2, $3::uuid)
            ON CONFLICT (provider, external_tenant_id) DO NOTHING
            """,
            provider,
            external_tenant_id,
            local_tenant_id,
        )


async def get_user(user_id: str) -> User | None:
    """Get a user by ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE id = $1::uuid", user_id)
    if row is None:
        return None
    return _record_to_user(row)


async def get_users_for_tenant(tenant_id: str) -> list[User]:
    """Get all users for a tenant."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM users WHERE tenant_id = $1::uuid ORDER BY name",
            tenant_id,
        )
    return [_record_to_user(row) for row in rows]


async def update_user(
    user_id: str,
    *,
    name: str | None = None,
    email: str | None = None,
    role: str | None = None,
) -> User | None:
    """Update selected fields on a user."""
    fields: list[str] = []
    values: list[Any] = []
    param_idx = 1

    if name is not None:
        fields.append(f"name = ${param_idx}")
        values.append(name)
        param_idx += 1
    if email is not None:
        fields.append(f"email = ${param_idx}")
        values.append(email or None)  # "" → NULL in DB
        param_idx += 1
    if role is not None:
        fields.append(f"role = ${param_idx}")
        values.append(role)
        param_idx += 1

    if not fields:
        return await get_user(user_id)

    values.append(user_id)
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE users SET {', '.join(fields)} WHERE id = ${param_idx}::uuid RETURNING *",
            *values,
        )
    if row is None:
        return None
    return _record_to_user(row)


async def delete_user(user_id: str) -> bool:
    """Delete a user by ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM users WHERE id = $1::uuid", user_id)
    return result == "DELETE 1"


# ---------------------------------------------------------------------------
# Coworker CRUD
# ---------------------------------------------------------------------------


def _parse_container_config(raw: dict[str, Any] | str | None) -> ContainerConfig | None:
    """Parse container_config JSONB into ContainerConfig."""
    if not raw:
        return None
    parsed = raw if isinstance(raw, dict) else json.loads(raw)
    if not isinstance(parsed, dict):
        return None
    from rolemesh.core.types import AdditionalMount

    mounts = [
        AdditionalMount(
            host_path=m.get("host_path", ""),
            container_path=m.get("container_path"),
            readonly=m.get("readonly", True),
        )
        for m in parsed.get("additional_mounts", [])
    ]
    return ContainerConfig(
        additional_mounts=mounts,
        timeout=parsed.get("timeout", 300_000),
    )


async def create_coworker(
    tenant_id: str,
    name: str,
    folder: str,
    agent_backend: str = "claude-code",
    system_prompt: str | None = None,
    tools: list[McpServerConfig] | None = None,
    skills: list[str] | None = None,
    is_admin: bool = False,  # Deprecated: use agent_role instead. Ignored when agent_role is set.
    container_config: ContainerConfig | None = None,
    max_concurrent: int = 2,
    agent_role: str = "",
    permissions: AgentPermissions | None = None,
) -> Coworker:
    """Create a new coworker.

    Use ``agent_role`` ("super_agent" / "agent") instead of ``is_admin``.
    ``agent_role`` takes priority when set; ``is_admin`` is only used as
    fallback for backward compatibility.
    """
    cc_json: str | None = None
    if container_config:
        cc_json = json.dumps(
            {
                "additional_mounts": [
                    {"host_path": m.host_path, "container_path": m.container_path, "readonly": m.readonly}
                    for m in container_config.additional_mounts
                ],
                "timeout": container_config.timeout,
            }
        )
    # Derive agent_role from is_admin if not explicitly set
    effective_role = agent_role or ("super_agent" if is_admin else "agent")
    effective_perms = permissions or AgentPermissions.for_role(effective_role)
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO coworkers (tenant_id, name, folder, agent_backend, system_prompt,
                tools, skills, is_admin, container_config, max_concurrent, agent_role, permissions)
            VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9::jsonb, $10, $11, $12::jsonb)
            RETURNING *
            """,
            tenant_id,
            name,
            folder,
            agent_backend,
            system_prompt,
            json.dumps(
                [{"name": t.name, "type": t.type, "url": t.url, "headers": t.headers} for t in tools]
                if tools
                else []
            ),
            json.dumps(skills or []),
            is_admin,
            cc_json,
            max_concurrent,
            effective_role,
            json.dumps(effective_perms.to_dict()),
        )
    assert row is not None
    return _record_to_coworker(row)


def _record_to_coworker(row: asyncpg.Record) -> Coworker:
    tools_raw = row.get("tools")
    if isinstance(tools_raw, str):
        tools_raw = json.loads(tools_raw) if tools_raw else []
    elif not isinstance(tools_raw, list):
        tools_raw = []
    tools: list[McpServerConfig] = []
    for item in tools_raw:
        if isinstance(item, dict) and "name" in item:
            raw_headers = item.get("headers")
            tools.append(
                McpServerConfig(
                    name=item["name"],
                    type=item.get("type", "sse"),
                    url=item.get("url", ""),
                    headers=raw_headers if isinstance(raw_headers, dict) else {},
                )
            )
        # Skip legacy string entries silently
    skills_raw = row.get("skills")

    # Parse agent_role and permissions (new auth fields)
    agent_role = row.get("agent_role") or "agent"
    perms_raw = row.get("permissions")
    if isinstance(perms_raw, dict):
        permissions = AgentPermissions.from_dict(perms_raw)
    elif isinstance(perms_raw, str) and perms_raw:
        permissions = AgentPermissions.from_dict(json.loads(perms_raw))
    else:
        permissions = AgentPermissions.for_role(agent_role)
    return Coworker(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        name=row["name"],
        folder=row["folder"],
        agent_backend=row.get("agent_backend") or "claude-code",
        system_prompt=row.get("system_prompt"),
        tools=tools,
        skills=skills_raw if isinstance(skills_raw, list) else json.loads(skills_raw) if skills_raw else [],
        is_admin=bool(row["is_admin"]),
        container_config=_parse_container_config(row["container_config"]),
        max_concurrent=row["max_concurrent"],
        status=row["status"] or "active",
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        agent_role=agent_role,
        permissions=permissions,
    )


async def get_coworker(coworker_id: str) -> Coworker | None:
    """Get a coworker by ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM coworkers WHERE id = $1::uuid", coworker_id)
    if row is None:
        return None
    return _record_to_coworker(row)


async def get_coworker_by_folder(tenant_id: str, folder: str) -> Coworker | None:
    """Get a coworker by tenant and folder."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM coworkers WHERE tenant_id = $1::uuid AND folder = $2",
            tenant_id,
            folder,
        )
    if row is None:
        return None
    return _record_to_coworker(row)


async def get_coworkers_for_tenant(tenant_id: str) -> list[Coworker]:
    """Get all coworkers for a tenant."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM coworkers WHERE tenant_id = $1::uuid ORDER BY name",
            tenant_id,
        )
    return [_record_to_coworker(row) for row in rows]


async def get_all_coworkers() -> list[Coworker]:
    """Get all coworkers."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM coworkers ORDER BY tenant_id, name")
    return [_record_to_coworker(row) for row in rows]


async def update_coworker(
    coworker_id: str,
    *,
    name: str | None = None,
    system_prompt: str | None = None,
    tools: list[McpServerConfig] | None = None,
    skills: list[str] | None = None,
    max_concurrent: int | None = None,
    status: str | None = None,
    agent_role: str | None = None,
    permissions: AgentPermissions | None = None,
) -> Coworker | None:
    """Update selected fields on a coworker."""
    fields: list[str] = []
    values: list[Any] = []
    param_idx = 1

    if name is not None:
        fields.append(f"name = ${param_idx}")
        values.append(name)
        param_idx += 1
    if system_prompt is not None:
        fields.append(f"system_prompt = ${param_idx}")
        values.append(system_prompt)
        param_idx += 1
    if tools is not None:
        fields.append(f"tools = ${param_idx}::jsonb")
        values.append(
            json.dumps([{"name": t.name, "type": t.type, "url": t.url, "headers": t.headers} for t in tools])
        )
        param_idx += 1
    if skills is not None:
        fields.append(f"skills = ${param_idx}::jsonb")
        values.append(json.dumps(skills))
        param_idx += 1
    if max_concurrent is not None:
        fields.append(f"max_concurrent = ${param_idx}")
        values.append(max_concurrent)
        param_idx += 1
    if status is not None:
        fields.append(f"status = ${param_idx}")
        values.append(status)
        param_idx += 1
    if agent_role is not None:
        fields.append(f"agent_role = ${param_idx}")
        values.append(agent_role)
        param_idx += 1
        fields.append(f"is_admin = ${param_idx}")
        values.append(agent_role == "super_agent")
        param_idx += 1
    if permissions is not None:
        fields.append(f"permissions = ${param_idx}::jsonb")
        values.append(json.dumps(permissions.to_dict()))
        param_idx += 1

    if not fields:
        return await get_coworker(coworker_id)

    values.append(coworker_id)
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE coworkers SET {', '.join(fields)} WHERE id = ${param_idx}::uuid RETURNING *",
            *values,
        )
    if row is None:
        return None
    return _record_to_coworker(row)


async def delete_coworker(coworker_id: str) -> bool:
    """Delete a coworker by ID. CASCADE handles dependent tables."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM coworkers WHERE id = $1::uuid", coworker_id)
    return result == "DELETE 1"


# ---------------------------------------------------------------------------
# ChannelBinding CRUD
# ---------------------------------------------------------------------------


async def create_channel_binding(
    coworker_id: str,
    tenant_id: str,
    channel_type: str,
    credentials: dict[str, str] | None = None,
    bot_display_name: str | None = None,
) -> ChannelBinding:
    """Create a channel binding."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO channel_bindings (coworker_id, tenant_id, channel_type, credentials, bot_display_name)
            VALUES ($1::uuid, $2::uuid, $3, $4::jsonb, $5)
            RETURNING id, coworker_id, tenant_id, channel_type, credentials, bot_display_name, status, created_at
            """,
            coworker_id,
            tenant_id,
            channel_type,
            json.dumps(credentials or {}),
            bot_display_name,
        )
    assert row is not None
    return _record_to_channel_binding(row)


def _record_to_channel_binding(row: asyncpg.Record) -> ChannelBinding:
    creds = row["credentials"]
    return ChannelBinding(
        id=str(row["id"]),
        coworker_id=str(row["coworker_id"]),
        tenant_id=str(row["tenant_id"]),
        channel_type=row["channel_type"],
        credentials=creds if isinstance(creds, dict) else json.loads(creds) if creds else {},
        bot_display_name=row["bot_display_name"],
        status=row["status"] or "active",
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
    )


async def get_channel_binding(binding_id: str) -> ChannelBinding | None:
    """Get a channel binding by ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM channel_bindings WHERE id = $1::uuid", binding_id)
    if row is None:
        return None
    return _record_to_channel_binding(row)


async def get_channel_binding_for_coworker(coworker_id: str, channel_type: str) -> ChannelBinding | None:
    """Get the channel binding for a coworker and channel type."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM channel_bindings WHERE coworker_id = $1::uuid AND channel_type = $2",
            coworker_id,
            channel_type,
        )
    if row is None:
        return None
    return _record_to_channel_binding(row)


async def get_all_channel_bindings() -> list[ChannelBinding]:
    """Get all channel bindings."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM channel_bindings ORDER BY tenant_id, coworker_id")
    return [_record_to_channel_binding(row) for row in rows]


async def get_channel_bindings_for_coworker(coworker_id: str) -> list[ChannelBinding]:
    """Get all channel bindings for a coworker."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM channel_bindings WHERE coworker_id = $1::uuid",
            coworker_id,
        )
    return [_record_to_channel_binding(row) for row in rows]


async def update_channel_binding(
    binding_id: str,
    *,
    credentials: dict[str, str] | None = None,
    bot_display_name: str | None = None,
    status: str | None = None,
) -> ChannelBinding | None:
    """Update selected fields on a channel binding."""
    fields: list[str] = []
    values: list[Any] = []
    param_idx = 1

    if credentials is not None:
        fields.append(f"credentials = ${param_idx}::jsonb")
        values.append(json.dumps(credentials))
        param_idx += 1
    if bot_display_name is not None:
        fields.append(f"bot_display_name = ${param_idx}")
        values.append(bot_display_name)
        param_idx += 1
    if status is not None:
        fields.append(f"status = ${param_idx}")
        values.append(status)
        param_idx += 1

    if not fields:
        return await get_channel_binding(binding_id)

    values.append(binding_id)
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            f"UPDATE channel_bindings SET {', '.join(fields)} WHERE id = ${param_idx}::uuid RETURNING *",
            *values,
        )
    if row is None:
        return None
    return _record_to_channel_binding(row)


async def delete_channel_binding(binding_id: str) -> bool:
    """Delete a channel binding by ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM channel_bindings WHERE id = $1::uuid", binding_id)
    return result == "DELETE 1"


# ---------------------------------------------------------------------------
# Conversation CRUD
# ---------------------------------------------------------------------------


async def create_conversation(
    tenant_id: str,
    coworker_id: str,
    channel_binding_id: str,
    channel_chat_id: str,
    name: str | None = None,
    requires_trigger: bool = True,
    user_id: str | None = None,
) -> Conversation:
    """Create a conversation."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO conversations (tenant_id, coworker_id, channel_binding_id, channel_chat_id, name, requires_trigger, user_id)
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7::uuid)
            RETURNING *
            """,
            tenant_id,
            coworker_id,
            channel_binding_id,
            channel_chat_id,
            name,
            requires_trigger,
            user_id,
        )
    assert row is not None
    return _record_to_conversation(row)


def _record_to_conversation(row: asyncpg.Record) -> Conversation:
    lai = row["last_agent_invocation"]
    uid = row.get("user_id")
    return Conversation(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=str(row["coworker_id"]),
        channel_binding_id=str(row["channel_binding_id"]),
        channel_chat_id=row["channel_chat_id"],
        name=row["name"],
        requires_trigger=bool(row["requires_trigger"]) if row["requires_trigger"] is not None else True,
        last_agent_invocation=lai.isoformat() if lai else None,
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        user_id=str(uid) if uid else None,
    )


async def get_conversation(conversation_id: str) -> Conversation | None:
    """Get a conversation by ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM conversations WHERE id = $1::uuid", conversation_id)
    if row is None:
        return None
    return _record_to_conversation(row)


async def get_conversations_for_coworker(coworker_id: str) -> list[Conversation]:
    """Get all conversations for a coworker."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM conversations WHERE coworker_id = $1::uuid ORDER BY created_at",
            coworker_id,
        )
    return [_record_to_conversation(row) for row in rows]


async def get_all_conversations() -> list[Conversation]:
    """Get all conversations."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT * FROM conversations ORDER BY tenant_id, coworker_id")
    return [_record_to_conversation(row) for row in rows]


async def get_conversation_by_binding_and_chat(channel_binding_id: str, channel_chat_id: str) -> Conversation | None:
    """Get a conversation by binding and chat ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM conversations WHERE channel_binding_id = $1::uuid AND channel_chat_id = $2",
            channel_binding_id,
            channel_chat_id,
        )
    if row is None:
        return None
    return _record_to_conversation(row)


async def delete_conversation(conversation_id: str) -> bool:
    """Delete a conversation by ID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute("DELETE FROM conversations WHERE id = $1::uuid", conversation_id)
    return result == "DELETE 1"


async def update_conversation_last_invocation(conversation_id: str, timestamp: str) -> None:
    """Update the last_agent_invocation timestamp for a conversation."""
    pool = _get_pool()
    ts = datetime.fromisoformat(timestamp) if timestamp else None
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE conversations SET last_agent_invocation = $1 WHERE id = $2::uuid",
            ts,
            conversation_id,
        )


async def update_conversation_user_id(conversation_id: str, user_id: str) -> None:
    """Set the user_id on a conversation (binds a user to a web conversation)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE conversations SET user_id = $1::uuid WHERE id = $2::uuid",
            user_id,
            conversation_id,
        )


# ---------------------------------------------------------------------------
# Sessions (new: per-conversation)
# ---------------------------------------------------------------------------


async def get_session(conversation_id: str) -> str | None:
    """Get the session ID for a conversation."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT session_id FROM sessions WHERE conversation_id = $1::uuid",
            conversation_id,
        )
    if row is None:
        return None
    return row["session_id"]  # type: ignore[no-any-return]


async def set_session(conversation_id: str, tenant_id: str, coworker_id: str, session_id: str) -> None:
    """Set the session ID for a conversation."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO sessions (conversation_id, tenant_id, coworker_id, session_id)
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4)
            ON CONFLICT (conversation_id) DO UPDATE SET session_id = EXCLUDED.session_id
            """,
            conversation_id,
            tenant_id,
            coworker_id,
            session_id,
        )


async def get_all_sessions() -> dict[str, str]:
    """Get all session mappings (conversation_id -> session_id)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT conversation_id, session_id FROM sessions")
    return {str(row["conversation_id"]): row["session_id"] for row in rows}


# ---------------------------------------------------------------------------
# Messages (new: per-conversation with TIMESTAMPTZ)
# ---------------------------------------------------------------------------


async def store_message(
    tenant_id: str,
    conversation_id: str,
    msg_id: str,
    sender: str,
    sender_name: str,
    content: str,
    timestamp: str,
    is_from_me: bool = False,
    is_bot_message: bool = False,
) -> None:
    """Store a message."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO messages (tenant_id, conversation_id, id, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (tenant_id, id, conversation_id) DO UPDATE SET
                content = EXCLUDED.content,
                timestamp = EXCLUDED.timestamp
            """,
            tenant_id,
            conversation_id,
            msg_id,
            sender,
            sender_name,
            content,
            _to_dt(timestamp),
            is_from_me,
            is_bot_message,
        )


def _record_to_new_message(row: asyncpg.Record, chat_jid: str = "") -> NewMessage:
    """Convert an asyncpg.Record to a NewMessage dataclass."""
    ts = row["timestamp"]
    return NewMessage(
        id=row["id"],
        chat_jid=chat_jid,
        sender=row["sender"] or "",
        sender_name=row["sender_name"] or "",
        content=row["content"] or "",
        timestamp=ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
        is_from_me=bool(row["is_from_me"]),
        is_bot_message=bool(row.get("is_bot_message", False)) if hasattr(row, "get") else False,
    )


async def get_messages_since(
    tenant_id: str,
    conversation_id: str,
    since_timestamp: str,
    bot_name: str,
    limit: int = 200,
    chat_jid: str = "",
) -> list[NewMessage]:
    """Get messages since a timestamp for a specific conversation."""
    pool = _get_pool()
    # Handle empty timestamp by using epoch
    ts = since_timestamp if since_timestamp else "1970-01-01T00:00:00+00:00"
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM (
                SELECT id, sender, sender_name, content, timestamp, is_from_me, is_bot_message
                FROM messages
                WHERE tenant_id = $1::uuid AND conversation_id = $2::uuid AND timestamp > $3
                    AND is_bot_message = FALSE AND content NOT LIKE $4
                    AND content != '' AND content IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT $5
            ) sub ORDER BY timestamp
            """,
            tenant_id,
            conversation_id,
            _to_dt(ts),
            f"{bot_name}:%",
            limit,
        )
    return [_record_to_new_message(row, chat_jid) for row in rows]


async def get_new_messages_for_conversations(
    tenant_id: str,
    conversation_ids: list[str],
    since_timestamp: str,
    bot_name: str,
    limit: int = 200,
) -> list[tuple[str, NewMessage]]:
    """Get new messages across multiple conversations.

    Returns list of (conversation_id, message) tuples.
    """
    if not conversation_ids:
        return []
    pool = _get_pool()
    ts = since_timestamp if since_timestamp else "1970-01-01T00:00:00+00:00"
    async with pool.acquire() as conn:
        placeholders = ", ".join(f"${i + 3}::uuid" for i in range(len(conversation_ids)))
        sql = f"""
            SELECT * FROM (
                SELECT id, conversation_id, sender, sender_name, content, timestamp, is_from_me, is_bot_message
                FROM messages
                WHERE tenant_id = $1::uuid AND timestamp > $2
                    AND conversation_id IN ({placeholders})
                    AND is_bot_message = FALSE
                    AND content NOT LIKE ${len(conversation_ids) + 3}
                    AND content != '' AND content IS NOT NULL
                ORDER BY timestamp DESC
                LIMIT ${len(conversation_ids) + 4}
            ) sub ORDER BY timestamp
        """
        params: list[Any] = [tenant_id, _to_dt(ts), *conversation_ids, f"{bot_name}:%", limit]
        rows = await conn.fetch(sql, *params)

    result: list[tuple[str, NewMessage]] = []
    for row in rows:
        conv_id = str(row["conversation_id"])
        ts_val = row["timestamp"]
        result.append(
            (
                conv_id,
                NewMessage(
                    id=row["id"],
                    chat_jid="",
                    sender=row["sender"] or "",
                    sender_name=row["sender_name"] or "",
                    content=row["content"] or "",
                    timestamp=ts_val.isoformat() if hasattr(ts_val, "isoformat") else str(ts_val),
                    is_from_me=bool(row["is_from_me"]),
                    is_bot_message=bool(row.get("is_bot_message", False)) if hasattr(row, "get") else False,
                ),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Scheduled tasks (new: per-coworker with UUID/TIMESTAMPTZ)
# ---------------------------------------------------------------------------


async def create_task(task: ScheduledTask) -> None:
    """Create a new scheduled task."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO scheduled_tasks (id, tenant_id, coworker_id, conversation_id, prompt, schedule_type, schedule_value, context_mode, next_run, status, created_at)
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, $6, $7, $8, $9, $10, now())
            """,
            task.id,
            task.tenant_id,
            task.coworker_id,
            task.conversation_id,
            task.prompt,
            task.schedule_type,
            task.schedule_value,
            task.context_mode or "isolated",
            _to_dt(task.next_run),
            task.status,
        )


def _record_to_scheduled_task(row: asyncpg.Record) -> ScheduledTask:
    """Convert an asyncpg.Record to a ScheduledTask dataclass."""
    nr = row["next_run"]
    lr = row["last_run"]
    ca = row["created_at"]
    conv_id = row.get("conversation_id")
    return ScheduledTask(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=str(row["coworker_id"]),
        conversation_id=str(conv_id) if conv_id else None,
        prompt=row["prompt"],
        schedule_type=row["schedule_type"],
        schedule_value=row["schedule_value"],
        context_mode=row["context_mode"] or "isolated",
        next_run=nr.isoformat() if nr else None,
        last_run=lr.isoformat() if lr else None,
        last_result=row["last_result"],
        status=row["status"],
        created_at=ca.isoformat() if ca else "",
    )


async def get_task_by_id(task_id: str) -> ScheduledTask | None:
    """Get a task by its ID, or None if not found."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM scheduled_tasks WHERE id = $1::uuid",
            task_id,
        )
    if row is None:
        return None
    return _record_to_scheduled_task(row)


async def get_tasks_for_coworker(coworker_id: str) -> list[ScheduledTask]:
    """Get all tasks for a specific coworker."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM scheduled_tasks WHERE coworker_id = $1::uuid ORDER BY created_at DESC",
            coworker_id,
        )
    return [_record_to_scheduled_task(row) for row in rows]


async def get_all_tasks(tenant_id: str | None = None) -> list[ScheduledTask]:
    """Get all scheduled tasks, optionally filtered by tenant."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        if tenant_id:
            rows = await conn.fetch(
                "SELECT * FROM scheduled_tasks WHERE tenant_id = $1::uuid ORDER BY created_at DESC",
                tenant_id,
            )
        else:
            rows = await conn.fetch("SELECT * FROM scheduled_tasks ORDER BY created_at DESC")
    return [_record_to_scheduled_task(row) for row in rows]


async def update_task(
    task_id: str,
    *,
    prompt: str | None = None,
    schedule_type: str | None = None,
    schedule_value: str | None = None,
    next_run: str | None = None,
    status: str | None = None,
) -> None:
    """Update selected fields on a scheduled task."""
    fields: list[str] = []
    values: list[Any] = []
    param_idx = 1

    if prompt is not None:
        fields.append(f"prompt = ${param_idx}")
        values.append(prompt)
        param_idx += 1
    if schedule_type is not None:
        fields.append(f"schedule_type = ${param_idx}")
        values.append(schedule_type)
        param_idx += 1
    if schedule_value is not None:
        fields.append(f"schedule_value = ${param_idx}")
        values.append(schedule_value)
        param_idx += 1
    if next_run is not None:
        fields.append(f"next_run = ${param_idx}")
        values.append(_to_dt(next_run))
        param_idx += 1
    if status is not None:
        fields.append(f"status = ${param_idx}")
        values.append(status)
        param_idx += 1

    if not fields:
        return

    values.append(task_id)
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            f"UPDATE scheduled_tasks SET {', '.join(fields)} WHERE id = ${param_idx}::uuid",
            *values,
        )


async def delete_task(task_id: str) -> None:
    """Delete a task and its run logs (CASCADE handles task_run_logs)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM scheduled_tasks WHERE id = $1::uuid", task_id)


async def get_due_tasks(tenant_id: str | None = None) -> list[ScheduledTask]:
    """Get all active tasks whose next_run is in the past."""
    pool = _get_pool()
    now = datetime.now(UTC)
    async with pool.acquire() as conn:
        if tenant_id:
            rows = await conn.fetch(
                """
                SELECT * FROM scheduled_tasks
                WHERE tenant_id = $1::uuid AND status = 'active' AND next_run IS NOT NULL AND next_run <= $2
                ORDER BY next_run
                """,
                tenant_id,
                now,
            )
        else:
            rows = await conn.fetch(
                """
                SELECT * FROM scheduled_tasks
                WHERE status = 'active' AND next_run IS NOT NULL AND next_run <= $1
                ORDER BY next_run
                """,
                now,
            )
    return [_record_to_scheduled_task(row) for row in rows]


async def update_task_after_run(
    task_id: str,
    next_run: str | None,
    last_result: str,
) -> None:
    """Update task state after execution."""
    pool = _get_pool()
    now = datetime.now(UTC)
    new_status = "completed" if next_run is None else None
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE scheduled_tasks
            SET next_run = $1, last_run = $2, last_result = $3,
                status = COALESCE($4::text, status)
            WHERE id = $5::uuid
            """,
            _to_dt(next_run),
            now,
            last_result[:500] if last_result else last_result,
            new_status,
            task_id,
        )


async def log_task_run(log: TaskRunLog) -> None:
    """Insert a task run log entry."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO task_run_logs (tenant_id, task_id, run_at, duration_ms, status, result, error)
            VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7)
            """,
            log.tenant_id,
            log.task_id,
            _to_dt(log.run_at),
            log.duration_ms,
            log.status,
            log.result[:500] if log.result else log.result,
            log.error,
        )


# ---------------------------------------------------------------------------
# Legacy migration helpers (only used by migration script)
# After migration these tables are dropped; functions kept for script compat.
# ---------------------------------------------------------------------------


def _parse_registered_group_record(
    row: asyncpg.Record,
) -> tuple[str, RegisteredGroup] | None:
    """Parse a registered_groups row into (jid, RegisteredGroup)."""
    jid: str = row["jid"]
    folder: str = row["folder"]

    if not is_valid_group_folder(folder):
        logger.warn("Skipping registered group with invalid folder", jid=jid, folder=folder)
        return None

    container_config = _parse_container_config(row["container_config"])
    requires_trigger = bool(row["requires_trigger"]) if row["requires_trigger"] is not None else True
    is_main = bool(row["is_main"]) if row["is_main"] is not None else False

    return jid, RegisteredGroup(
        name=row["name"],
        folder=folder,
        trigger=row["trigger_pattern"],
        added_at=row["added_at"],
        container_config=container_config,
        requires_trigger=requires_trigger,
        is_main=is_main,
    )


async def set_registered_group(jid: str, group: RegisteredGroup) -> None:
    """Insert or replace a registered group."""
    if not is_valid_group_folder(group.folder):
        raise ValueError(f'Invalid group folder "{group.folder}" for JID {jid}')

    container_config_json: str | None = None
    if group.container_config:
        container_config_json = json.dumps(
            {
                "additional_mounts": [
                    {"host_path": m.host_path, "container_path": m.container_path, "readonly": m.readonly}
                    for m in group.container_config.additional_mounts
                ],
                "timeout": group.container_config.timeout,
            }
        )

    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO registered_groups (tenant_id, jid, name, folder, trigger_pattern, added_at, container_config, requires_trigger, is_main)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9)
            ON CONFLICT (tenant_id, jid) DO UPDATE SET
                name = EXCLUDED.name,
                folder = EXCLUDED.folder,
                trigger_pattern = EXCLUDED.trigger_pattern,
                added_at = EXCLUDED.added_at,
                container_config = EXCLUDED.container_config,
                requires_trigger = EXCLUDED.requires_trigger,
                is_main = EXCLUDED.is_main
            """,
            DEFAULT_TENANT,
            jid,
            group.name,
            group.folder,
            group.trigger,
            group.added_at,
            container_config_json,
            group.requires_trigger,
            group.is_main,
        )


async def get_all_registered_groups() -> dict[str, RegisteredGroup]:
    """Get all registered groups as a dict keyed by JID."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM registered_groups WHERE tenant_id = $1",
            DEFAULT_TENANT,
        )
    result: dict[str, RegisteredGroup] = {}
    for row in rows:
        parsed = _parse_registered_group_record(row)
        if parsed is not None:
            result[parsed[0]] = parsed[1]
    return result


# ---------------------------------------------------------------------------
# Legacy session helpers (for migration)
# ---------------------------------------------------------------------------


async def get_session_legacy(group_folder: str) -> str | None:
    """Get session from legacy sessions table (old 'sessions' or 'sessions_legacy')."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        # Try the old sessions table first (pre-migration)
        has_old = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='sessions' AND column_name='group_folder')"
        )
        table = "sessions" if has_old else "sessions_legacy"
        row = await conn.fetchrow(
            f"SELECT session_id FROM {table} WHERE tenant_id = $1 AND group_folder = $2",
            DEFAULT_TENANT,
            group_folder,
        )
    if row is None:
        return None
    return row["session_id"]  # type: ignore[no-any-return]


async def set_session_legacy(group_folder: str, session_id: str) -> None:
    """Set session in legacy sessions table."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        has_old = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='sessions' AND column_name='group_folder')"
        )
        table = "sessions" if has_old else "sessions_legacy"
        await conn.execute(
            f"""
            INSERT INTO {table} (tenant_id, group_folder, session_id) VALUES ($1, $2, $3)
            ON CONFLICT (tenant_id, group_folder) DO UPDATE SET session_id = EXCLUDED.session_id
            """,
            DEFAULT_TENANT,
            group_folder,
            session_id,
        )


async def get_all_sessions_legacy() -> dict[str, str]:
    """Get all legacy session mappings (group_folder -> session_id)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        has_old = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name='sessions' AND column_name='group_folder')"
        )
        table = "sessions" if has_old else "sessions_legacy"
        rows = await conn.fetch(
            f"SELECT group_folder, session_id FROM {table} WHERE tenant_id = $1",
            DEFAULT_TENANT,
        )
    return {row["group_folder"]: row["session_id"] for row in rows}


# ---------------------------------------------------------------------------
# Drop legacy tables
# ---------------------------------------------------------------------------


async def drop_legacy_tables() -> None:
    """Drop legacy tables and recreate shared tables in new format."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        # Drop legacy-only tables
        await conn.execute("DROP TABLE IF EXISTS router_state CASCADE")
        await conn.execute("DROP TABLE IF EXISTS registered_groups CASCADE")
        await conn.execute("DROP TABLE IF EXISTS chats CASCADE")
        await conn.execute("DROP TABLE IF EXISTS sessions_legacy CASCADE")
        # Drop old-format shared tables (messages, sessions, scheduled_tasks, task_run_logs)
        await conn.execute("DROP TABLE IF EXISTS task_run_logs CASCADE")
        await conn.execute("DROP TABLE IF EXISTS messages CASCADE")
        await conn.execute("DROP TABLE IF EXISTS scheduled_tasks CASCADE")
        await conn.execute("DROP TABLE IF EXISTS sessions CASCADE")
        # Recreate in new format (now legacy_exists check will be False)
        await _create_schema(conn)
    logger.info("Legacy tables dropped and new-format tables created")


# ---------------------------------------------------------------------------
# User-Agent assignment CRUD
# ---------------------------------------------------------------------------


async def assign_agent_to_user(user_id: str, coworker_id: str, tenant_id: str) -> None:
    """Assign a coworker (agent) to a user."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO user_agent_assignments (user_id, coworker_id, tenant_id)
            VALUES ($1::uuid, $2::uuid, $3::uuid)
            ON CONFLICT (user_id, coworker_id) DO NOTHING
            """,
            user_id,
            coworker_id,
            tenant_id,
        )


async def unassign_agent_from_user(user_id: str, coworker_id: str) -> None:
    """Remove a coworker assignment from a user."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM user_agent_assignments WHERE user_id = $1::uuid AND coworker_id = $2::uuid",
            user_id,
            coworker_id,
        )


async def get_agents_for_user(user_id: str) -> list[Coworker]:
    """Get all coworkers assigned to a user."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT c.* FROM coworkers c
            JOIN user_agent_assignments uaa ON c.id = uaa.coworker_id
            WHERE uaa.user_id = $1::uuid
            ORDER BY c.name
            """,
            user_id,
        )
    return [_record_to_coworker(row) for row in rows]


async def get_users_for_agent(coworker_id: str) -> list[User]:
    """Get all users assigned to a coworker."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT u.* FROM users u
            JOIN user_agent_assignments uaa ON u.id = uaa.user_id
            WHERE uaa.coworker_id = $1::uuid
            ORDER BY u.name
            """,
            coworker_id,
        )
    return [_record_to_user(row) for row in rows]
