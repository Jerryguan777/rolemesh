"""PostgreSQL database operations using asyncpg."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from rolemesh.approval.types import (
        ApprovalAuditEntry,
        ApprovalPolicy,
        ApprovalRequest,
    )
    from rolemesh.safety.types import Rule as SafetyRule

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
    # Approval module — per-tenant default behaviour when a proposal
    # matches NO policy. Values:
    #   'auto_execute'     — legacy: run the actions unsupervised
    #                        (audit chain created→approved→executing→
    #                        executed, all with system actor).
    #   'require_approval' — create the request as ``skipped`` so an
    #                        operator sees it but it does not run.
    #                        Use when the tenant treats "no matching
    #                        policy" as a config gap, not an allowlist.
    #   'deny'             — create the request as ``rejected`` with a
    #                        system note. Use when the tenant is in a
    #                        deny-by-default posture.
    await conn.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS "
        "approval_default_mode TEXT DEFAULT 'auto_execute' "
        "CHECK (approval_default_mode IN ("
        "'auto_execute', 'require_approval', 'deny'))"
    )
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
    # --- Auth: add agent_role + permissions to coworkers ---
    await conn.execute(
        "ALTER TABLE coworkers ADD COLUMN IF NOT EXISTS agent_role TEXT DEFAULT 'agent'"
    )
    await conn.execute(
        "ALTER TABLE coworkers ADD COLUMN IF NOT EXISTS permissions JSONB "
        "DEFAULT '{\"data_scope\":\"self\",\"task_schedule\":false,"
        "\"task_manage_others\":false,\"agent_delegate\":false}'"
    )
    # Backfill legacy is_admin→agent_role before dropping the column.
    # Idempotent: only runs when the column still exists (fresh deploy from an
    # older schema that had is_admin=TRUE rows but never ran the backfill).
    await conn.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'coworkers' AND column_name = 'is_admin') THEN
                UPDATE coworkers SET
                    agent_role = 'super_agent',
                    permissions = '{"data_scope":"tenant","task_schedule":true,"task_manage_others":true,"agent_delegate":true}'
                WHERE is_admin = TRUE AND agent_role = 'agent';
            END IF;
        END $$
    """)
    await conn.execute("ALTER TABLE coworkers DROP COLUMN IF EXISTS is_admin")
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

    # OIDC: per-user token vault for MCP token forwarding
    # refresh_token / access_token are encrypted with Fernet (key derived from
    # ROLEMESH_TOKEN_SECRET).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS oidc_user_tokens (
            user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            refresh_token_encrypted BYTEA NOT NULL,
            access_token_encrypted BYTEA,
            access_token_expires_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ DEFAULT now()
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

    # --- Approval module tables ---
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS approval_policies (
            id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id              UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            coworker_id            UUID REFERENCES coworkers(id) ON DELETE CASCADE,
            mcp_server_name        TEXT NOT NULL,
            tool_name              TEXT NOT NULL,
            condition_expr         JSONB NOT NULL,
            approver_user_ids      UUID[] NOT NULL DEFAULT '{}',
            notify_conversation_id UUID REFERENCES conversations(id) ON DELETE SET NULL,
            auto_expire_minutes    INT DEFAULT 60,
            post_exec_mode         TEXT NOT NULL DEFAULT 'report'
                CHECK (post_exec_mode IN ('report')),
            enabled                BOOLEAN DEFAULT TRUE,
            priority               INT DEFAULT 0,
            created_at             TIMESTAMPTZ DEFAULT now(),
            updated_at             TIMESTAMPTZ DEFAULT now()
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_policies_tenant "
        "ON approval_policies(tenant_id, enabled)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_policies_tool "
        "ON approval_policies(tenant_id, mcp_server_name, tool_name) "
        "WHERE enabled"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS approval_requests (
            id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id          UUID NOT NULL REFERENCES tenants(id),
            coworker_id        UUID NOT NULL REFERENCES coworkers(id),
            conversation_id    UUID REFERENCES conversations(id),
            -- policy_id is nullable: proposals that do not match any policy
            -- (short-circuit auto-executed path) keep NULL so the admin UI
            -- and reporting queries can distinguish "policy X triggered
            -- this" from "proposal was auto-executed because no policy
            -- applied". ON DELETE SET NULL so disabling/deleting a policy
            -- never orphans old requests.
            policy_id          UUID REFERENCES approval_policies(id) ON DELETE SET NULL,
            user_id            UUID NOT NULL REFERENCES users(id),
            job_id             TEXT NOT NULL,
            mcp_server_name    TEXT NOT NULL,
            actions            JSONB NOT NULL,
            action_hashes      TEXT[] NOT NULL,
            rationale          TEXT,
            source             TEXT NOT NULL
                CHECK (source IN ('proposal', 'auto_intercept',
                                  'safety_require_approval')),
            status             TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN (
                    'pending', 'approved', 'rejected', 'expired', 'cancelled',
                    'skipped', 'executing', 'executed',
                    'execution_failed', 'execution_stale'
                )),
            post_exec_mode     TEXT NOT NULL DEFAULT 'report',
            resolved_approvers UUID[] NOT NULL,
            requested_at       TIMESTAMPTZ DEFAULT now(),
            expires_at         TIMESTAMPTZ NOT NULL,
            created_at         TIMESTAMPTZ DEFAULT now(),
            updated_at         TIMESTAMPTZ DEFAULT now()
        )
    """)
    # Migrate existing deployments: drop the NOT NULL if present.
    await conn.execute(
        "ALTER TABLE approval_requests ALTER COLUMN policy_id DROP NOT NULL"
    )
    # V2 P1.1: widen the source CHECK to include safety-driven
    # approval requests. Old deployments have the two-value CHECK;
    # drop-then-add so the rollout is a single migration.
    await conn.execute(
        "ALTER TABLE approval_requests "
        "DROP CONSTRAINT IF EXISTS approval_requests_source_check"
    )
    await conn.execute(
        "ALTER TABLE approval_requests ADD CONSTRAINT "
        "approval_requests_source_check CHECK ("
        "source IN ('proposal', 'auto_intercept', "
        "'safety_require_approval'))"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_requests_tenant_status "
        "ON approval_requests(tenant_id, status)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_requests_job "
        "ON approval_requests(job_id) WHERE status = 'pending'"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_requests_expires "
        "ON approval_requests(status, expires_at) WHERE status = 'pending'"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_requests_approved "
        "ON approval_requests(status, updated_at) WHERE status = 'approved'"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_requests_executing "
        "ON approval_requests(status, updated_at) WHERE status = 'executing'"
    )

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS approval_audit_log (
            id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            request_id    UUID NOT NULL REFERENCES approval_requests(id) ON DELETE CASCADE,
            action        TEXT NOT NULL
                CHECK (action IN (
                    'created', 'approved', 'rejected', 'expired', 'cancelled',
                    'skipped', 'executing', 'executed',
                    'execution_failed', 'execution_stale'
                )),
            actor_user_id UUID REFERENCES users(id),
            note          TEXT,
            metadata      JSONB,
            created_at    TIMESTAMPTZ DEFAULT now()
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_log_request "
        "ON approval_audit_log(request_id)"
    )

    # ---------------------------------------------------------------------
    # Audit trigger: every INSERT or status-change UPDATE on
    # approval_requests automatically appends an approval_audit_log row in
    # the SAME transaction. This closes the two-step window where a crash
    # between "UPDATE status" and "INSERT audit" could otherwise leave a
    # ghost decision with no audit trail. The app layer can still add
    # richer audit rows (e.g. with ``note`` or ``metadata.results``) via
    # write_approval_audit — the trigger writes the minimal system row;
    # the app layer then UPDATEs that row or writes a companion row with
    # the extra payload.
    #
    # actor_user_id + note are passed through GUC session variables
    # ``approval.actor_user_id`` / ``approval.note`` (set via ``SET LOCAL``
    # inside the calling transaction). Unset GUCs → NULL, which is the
    # right semantics for purely-system transitions (expiry / cancel /
    # skipped / execute status bumps).
    # ---------------------------------------------------------------------
    await conn.execute("""
        CREATE OR REPLACE FUNCTION _approval_write_audit_from_trigger()
        RETURNS TRIGGER AS $$
        DECLARE
            v_actor TEXT;
            v_actor_uuid UUID;
            v_note TEXT;
            v_meta TEXT;
            v_meta_json JSONB;
        BEGIN
            -- Read the three optional GUC session variables. Missing ones
            -- are treated as NULL. The GUCs are transaction-scoped (set
            -- via set_config(..., true)), so they auto-clear after commit.
            BEGIN
                v_actor := current_setting('approval.actor_user_id', TRUE);
            EXCEPTION WHEN OTHERS THEN v_actor := NULL; END;
            BEGIN
                v_note := current_setting('approval.note', TRUE);
            EXCEPTION WHEN OTHERS THEN v_note := NULL; END;
            BEGIN
                v_meta := current_setting('approval.metadata', TRUE);
            EXCEPTION WHEN OTHERS THEN v_meta := NULL; END;

            IF v_actor IS NOT NULL AND v_actor <> '' THEN
                BEGIN
                    v_actor_uuid := v_actor::uuid;
                EXCEPTION WHEN OTHERS THEN v_actor_uuid := NULL; END;
            ELSE
                v_actor_uuid := NULL;
            END IF;

            v_meta_json := COALESCE(NULLIF(v_meta, '')::jsonb, '{}'::jsonb);

            IF TG_OP = 'INSERT' THEN
                -- Every INSERT is a "created" row, attributed to the
                -- caller (v_actor_uuid) when provided.
                INSERT INTO approval_audit_log
                    (request_id, action, actor_user_id, note, metadata)
                VALUES
                    (NEW.id, 'created', v_actor_uuid,
                     NULLIF(v_note, ''), v_meta_json);
                -- Rows created already in a terminal-ish state (e.g.
                -- 'skipped' when resolve_approvers returned empty) need
                -- a second audit row so the status transition is also
                -- captured. The second row is attributed to the system
                -- (NULL actor) because the transition was not a user
                -- action — the same user who proposed could not have
                -- chosen "skipped."
                IF NEW.status <> 'pending' THEN
                    INSERT INTO approval_audit_log
                        (request_id, action, actor_user_id, note, metadata)
                    VALUES
                        (NEW.id, NEW.status, NULL, NULL, '{}'::jsonb);
                END IF;
            ELSIF TG_OP = 'UPDATE' AND NEW.status <> OLD.status THEN
                INSERT INTO approval_audit_log
                    (request_id, action, actor_user_id, note, metadata)
                VALUES
                    (NEW.id, NEW.status, v_actor_uuid,
                     NULLIF(v_note, ''), v_meta_json);
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    await conn.execute(
        "DROP TRIGGER IF EXISTS trg_approval_audit ON approval_requests"
    )
    await conn.execute("""
        CREATE TRIGGER trg_approval_audit
        AFTER INSERT OR UPDATE OF status ON approval_requests
        FOR EACH ROW EXECUTE FUNCTION _approval_write_audit_from_trigger();
    """)

    # --- Safety Framework tables ---
    # Admin-managed rules that the container loads as a snapshot at job
    # start. ``coworker_id`` IS NULL means tenant-wide scope.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS safety_rules (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            coworker_id     UUID REFERENCES coworkers(id) ON DELETE CASCADE,
            stage           TEXT NOT NULL,
            check_id        TEXT NOT NULL,
            config          JSONB NOT NULL DEFAULT '{}'::jsonb,
            priority        INTEGER NOT NULL DEFAULT 100,
            enabled         BOOLEAN NOT NULL DEFAULT TRUE,
            description     TEXT NOT NULL DEFAULT '',
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_safety_rules_lookup "
        "ON safety_rules (tenant_id, stage, enabled)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_safety_rules_coworker "
        "ON safety_rules (coworker_id) WHERE coworker_id IS NOT NULL"
    )

    # Per-decision audit rows. We store only a SHA-256 digest of the
    # payload + a short human summary (tool name, prompt prefix) — not
    # the original text — so the audit table cannot double as a PII
    # leak vector. Full-text reconstitution, when needed, comes from
    # the conversation transcripts.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS safety_decisions (
            id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id             UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            coworker_id           UUID REFERENCES coworkers(id) ON DELETE SET NULL,
            conversation_id       TEXT,
            job_id                TEXT,
            stage                 TEXT NOT NULL,
            verdict_action        TEXT NOT NULL,
            triggered_rule_ids    UUID[] NOT NULL DEFAULT '{}',
            findings              JSONB NOT NULL DEFAULT '[]'::jsonb,
            context_digest        TEXT NOT NULL DEFAULT '',
            context_summary       TEXT NOT NULL DEFAULT '',
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    # V2 P1.1: require_approval verdicts need the original tool_name
    # + tool_input so the approval UI can render a decision surface.
    # This column is the ONLY place the full tool_input is retained
    # (context_digest + context_summary deliberately truncate) — it is
    # live for 24 h after the approval resolves, then zeroed by a
    # cleanup task. Other verdict_actions (block, allow, warn, redact)
    # leave this column NULL.
    await conn.execute(
        "ALTER TABLE safety_decisions "
        "ADD COLUMN IF NOT EXISTS approval_context JSONB"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_safety_decisions_tenant_time "
        "ON safety_decisions (tenant_id, created_at DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_safety_decisions_verdict "
        "ON safety_decisions (tenant_id, verdict_action, created_at DESC)"
    )

    # Rule-change audit log. Every INSERT / UPDATE / DELETE on
    # safety_rules appends one append-only row here via the trigger
    # below. Compliance scenario: "Jan 3 admin disabled the SSN rule,
    # leak followed on Jan 4" must be reconstructable without relying
    # on server logs. rule_id is NOT a FK because the rule itself may
    # have been hard-deleted — we still need the audit trail.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS safety_rules_audit (
            id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            rule_id        UUID NOT NULL,
            tenant_id      UUID NOT NULL,
            action         TEXT NOT NULL
                CHECK (action IN ('created', 'updated', 'deleted')),
            actor_user_id  UUID REFERENCES users(id) ON DELETE SET NULL,
            before_state   JSONB,
            after_state    JSONB,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_safety_rules_audit_rule "
        "ON safety_rules_audit (rule_id, created_at DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_safety_rules_audit_tenant "
        "ON safety_rules_audit (tenant_id, created_at DESC)"
    )

    # Trigger function: same pattern as approval's. The caller sets
    # a transaction-local GUC ``safety.actor_user_id`` so the trigger
    # can attribute the row without a second DML. Missing GUC → NULL
    # actor (system transition, e.g. bulk migration).
    await conn.execute("""
        CREATE OR REPLACE FUNCTION _safety_rules_write_audit_from_trigger()
        RETURNS TRIGGER AS $$
        DECLARE
            v_actor TEXT;
            v_actor_uuid UUID;
            v_before JSONB;
            v_after JSONB;
        BEGIN
            BEGIN
                v_actor := current_setting('safety.actor_user_id', TRUE);
            EXCEPTION WHEN OTHERS THEN v_actor := NULL; END;

            IF v_actor IS NOT NULL AND v_actor <> '' THEN
                BEGIN
                    v_actor_uuid := v_actor::uuid;
                EXCEPTION WHEN OTHERS THEN v_actor_uuid := NULL; END;
            ELSE
                v_actor_uuid := NULL;
            END IF;

            IF TG_OP = 'INSERT' THEN
                v_after := jsonb_build_object(
                    'stage', NEW.stage,
                    'check_id', NEW.check_id,
                    'config', NEW.config,
                    'coworker_id', NEW.coworker_id,
                    'priority', NEW.priority,
                    'enabled', NEW.enabled,
                    'description', NEW.description
                );
                INSERT INTO safety_rules_audit
                    (rule_id, tenant_id, action, actor_user_id,
                     before_state, after_state)
                VALUES
                    (NEW.id, NEW.tenant_id, 'created', v_actor_uuid,
                     NULL, v_after);
                RETURN NEW;
            ELSIF TG_OP = 'UPDATE' THEN
                v_before := jsonb_build_object(
                    'stage', OLD.stage,
                    'check_id', OLD.check_id,
                    'config', OLD.config,
                    'coworker_id', OLD.coworker_id,
                    'priority', OLD.priority,
                    'enabled', OLD.enabled,
                    'description', OLD.description
                );
                v_after := jsonb_build_object(
                    'stage', NEW.stage,
                    'check_id', NEW.check_id,
                    'config', NEW.config,
                    'coworker_id', NEW.coworker_id,
                    'priority', NEW.priority,
                    'enabled', NEW.enabled,
                    'description', NEW.description
                );
                -- Only log when something semantic changed. updated_at
                -- moving on a no-op call is noise.
                IF v_before <> v_after THEN
                    INSERT INTO safety_rules_audit
                        (rule_id, tenant_id, action, actor_user_id,
                         before_state, after_state)
                    VALUES
                        (NEW.id, NEW.tenant_id, 'updated',
                         v_actor_uuid, v_before, v_after);
                END IF;
                RETURN NEW;
            ELSIF TG_OP = 'DELETE' THEN
                v_before := jsonb_build_object(
                    'stage', OLD.stage,
                    'check_id', OLD.check_id,
                    'config', OLD.config,
                    'coworker_id', OLD.coworker_id,
                    'priority', OLD.priority,
                    'enabled', OLD.enabled,
                    'description', OLD.description
                );
                INSERT INTO safety_rules_audit
                    (rule_id, tenant_id, action, actor_user_id,
                     before_state, after_state)
                VALUES
                    (OLD.id, OLD.tenant_id, 'deleted',
                     v_actor_uuid, v_before, NULL);
                RETURN OLD;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;
    """)
    await conn.execute(
        "DROP TRIGGER IF EXISTS trg_safety_rules_audit ON safety_rules"
    )
    await conn.execute("""
        CREATE TRIGGER trg_safety_rules_audit
        AFTER INSERT OR UPDATE OR DELETE ON safety_rules
        FOR EACH ROW EXECUTE FUNCTION _safety_rules_write_audit_from_trigger();
    """)

    # Idempotent default tenant. OIDCAuthProvider._provision_tenant falls back
    # to slug='default' for single-tenant deployments where the IdP doesn't
    # carry a tenant claim. Without this row, the first OIDC login on a fresh
    # database returns None and authentication fails opaquely.
    await conn.execute(
        """
        INSERT INTO tenants (slug, name)
        VALUES ('default', 'Default')
        ON CONFLICT (slug) DO NOTHING
        """
    )


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
        await conn.execute("DROP TABLE IF EXISTS safety_decisions CASCADE")
        await conn.execute("DROP TABLE IF EXISTS safety_rules_audit CASCADE")
        await conn.execute("DROP TABLE IF EXISTS safety_rules CASCADE")
        await conn.execute("DROP TABLE IF EXISTS approval_audit_log CASCADE")
        await conn.execute("DROP TABLE IF EXISTS approval_requests CASCADE")
        await conn.execute("DROP TABLE IF EXISTS approval_policies CASCADE")
        await conn.execute("DROP TABLE IF EXISTS oidc_user_tokens CASCADE")
        await conn.execute("DROP TABLE IF EXISTS external_tenant_map CASCADE")
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
    # ``approval_default_mode`` may be missing on rows read via older
    # ``SELECT id, slug, name, plan, max_concurrent_containers,
    # last_message_cursor, created_at`` projections; default it here
    # so the dataclass stays stable regardless of the projection.
    try:
        default_mode = row["approval_default_mode"] or "auto_execute"
    except (KeyError, IndexError):
        default_mode = "auto_execute"
    return Tenant(
        id=str(row["id"]),
        name=row["name"],
        slug=row["slug"],
        plan=row["plan"],
        max_concurrent_containers=row["max_concurrent_containers"],
        last_message_cursor=lmc.isoformat() if lmc else None,
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        approval_default_mode=default_mode,
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
    approval_default_mode: str | None = None,
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
    if approval_default_mode is not None:
        if approval_default_mode not in (
            "auto_execute", "require_approval", "deny",
        ):
            raise ValueError(
                f"invalid approval_default_mode: {approval_default_mode!r}"
            )
        fields.append(f"approval_default_mode = ${param_idx}")
        values.append(approval_default_mode)
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


# ---------------------------------------------------------------------------
# OIDC user token vault (server-side encrypted refresh/access tokens)
# ---------------------------------------------------------------------------


async def upsert_user_oidc_tokens(
    user_id: str,
    refresh_token_encrypted: bytes,
    access_token_encrypted: bytes | None,
    access_token_expires_at: datetime | None,
) -> None:
    """Insert or replace the encrypted token row for a user."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO oidc_user_tokens (
                user_id, refresh_token_encrypted, access_token_encrypted,
                access_token_expires_at, updated_at
            )
            VALUES ($1::uuid, $2, $3, $4, now())
            ON CONFLICT (user_id) DO UPDATE SET
                refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
                access_token_encrypted = EXCLUDED.access_token_encrypted,
                access_token_expires_at = EXCLUDED.access_token_expires_at,
                updated_at = now()
            """,
            user_id,
            refresh_token_encrypted,
            access_token_encrypted,
            access_token_expires_at,
        )


async def get_user_oidc_tokens(
    user_id: str,
) -> tuple[bytes, bytes | None, datetime | None] | None:
    """Return (refresh_token_enc, access_token_enc, expires_at) or None."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT refresh_token_encrypted, access_token_encrypted, access_token_expires_at "
            "FROM oidc_user_tokens WHERE user_id = $1::uuid",
            user_id,
        )
    if row is None:
        return None
    return (
        row["refresh_token_encrypted"],
        row["access_token_encrypted"],
        row["access_token_expires_at"],
    )


async def update_user_access_token(
    user_id: str,
    access_token_encrypted: bytes,
    access_token_expires_at: datetime,
) -> None:
    """Update only the cached access_token (after refresh)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE oidc_user_tokens
            SET access_token_encrypted = $1,
                access_token_expires_at = $2,
                updated_at = now()
            WHERE user_id = $3::uuid
            """,
            access_token_encrypted,
            access_token_expires_at,
            user_id,
        )


async def update_user_refresh_token(
    user_id: str,
    refresh_token_encrypted: bytes,
) -> None:
    """Update only the refresh_token (after IdP rotation)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE oidc_user_tokens SET refresh_token_encrypted = $1, updated_at = now() "
            "WHERE user_id = $2::uuid",
            refresh_token_encrypted,
            user_id,
        )


async def delete_user_oidc_tokens(user_id: str) -> None:
    """Remove a user's stored OIDC tokens (logout / refresh failure)."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM oidc_user_tokens WHERE user_id = $1::uuid",
            user_id,
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
    container_config: ContainerConfig | None = None,
    max_concurrent: int = 2,
    agent_role: str = "agent",
    permissions: AgentPermissions | None = None,
) -> Coworker:
    """Create a new coworker."""
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
    effective_perms = permissions or AgentPermissions.for_role(agent_role)
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO coworkers (tenant_id, name, folder, agent_backend, system_prompt,
                tools, skills, container_config, max_concurrent, agent_role, permissions)
            VALUES ($1::uuid, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, $9, $10, $11::jsonb)
            RETURNING *
            """,
            tenant_id,
            name,
            folder,
            agent_backend,
            system_prompt,
            json.dumps(
                [
                    {"name": t.name, "type": t.type, "url": t.url, "headers": t.headers, "auth_mode": t.auth_mode}
                    for t in tools
                ]
                if tools
                else []
            ),
            json.dumps(skills or []),
            cc_json,
            max_concurrent,
            agent_role,
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
            auth_mode = item.get("auth_mode") or "user"
            if auth_mode not in ("user", "service", "both"):
                auth_mode = "user"
            tools.append(
                McpServerConfig(
                    name=item["name"],
                    type=item.get("type", "sse"),
                    url=item.get("url", ""),
                    headers=raw_headers if isinstance(raw_headers, dict) else {},
                    auth_mode=auth_mode,
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
            json.dumps(
                [
                    {"name": t.name, "type": t.type, "url": t.url, "headers": t.headers, "auth_mode": t.auth_mode}
                    for t in tools
                ]
            )
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


# ---------------------------------------------------------------------------
# Approval policies CRUD
# ---------------------------------------------------------------------------


def _record_to_approval_policy(row: asyncpg.Record) -> ApprovalPolicy:
    from rolemesh.approval.types import ApprovalPolicy

    cond = row["condition_expr"]
    if isinstance(cond, str):
        cond = json.loads(cond) if cond else {}
    approvers = row["approver_user_ids"] or []
    return ApprovalPolicy(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=str(row["coworker_id"]) if row["coworker_id"] else None,
        mcp_server_name=row["mcp_server_name"],
        tool_name=row["tool_name"],
        condition_expr=cond if isinstance(cond, dict) else {},
        approver_user_ids=[str(a) for a in approvers],
        notify_conversation_id=str(row["notify_conversation_id"])
        if row["notify_conversation_id"]
        else None,
        auto_expire_minutes=row["auto_expire_minutes"] or 60,
        post_exec_mode=row["post_exec_mode"] or "report",
        enabled=bool(row["enabled"]),
        priority=row["priority"] or 0,
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
    )


async def create_approval_policy(
    *,
    tenant_id: str,
    mcp_server_name: str,
    tool_name: str,
    condition_expr: dict[str, Any],
    coworker_id: str | None = None,
    approver_user_ids: list[str] | None = None,
    notify_conversation_id: str | None = None,
    auto_expire_minutes: int = 60,
    post_exec_mode: str = "report",
    enabled: bool = True,
    priority: int = 0,
) -> ApprovalPolicy:
    """Insert a new approval policy and return the stored row."""
    pool = _get_pool()
    approvers = approver_user_ids or []
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO approval_policies (
                tenant_id, coworker_id, mcp_server_name, tool_name,
                condition_expr, approver_user_ids, notify_conversation_id,
                auto_expire_minutes, post_exec_mode, enabled, priority
            )
            VALUES (
                $1::uuid, $2::uuid, $3, $4,
                $5::jsonb, $6::uuid[], $7::uuid,
                $8, $9, $10, $11
            )
            RETURNING *
            """,
            tenant_id,
            coworker_id,
            mcp_server_name,
            tool_name,
            json.dumps(condition_expr),
            approvers,
            notify_conversation_id,
            auto_expire_minutes,
            post_exec_mode,
            enabled,
            priority,
        )
    assert row is not None
    return _record_to_approval_policy(row)


async def get_approval_policy(policy_id: str) -> ApprovalPolicy | None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM approval_policies WHERE id = $1::uuid", policy_id
        )
    if row is None:
        return None
    return _record_to_approval_policy(row)


async def list_approval_policies(
    tenant_id: str,
    *,
    coworker_id: str | None = None,
    enabled: bool | None = None,
) -> list[ApprovalPolicy]:
    """List policies for a tenant, optionally filtered by coworker and state."""
    pool = _get_pool()
    clauses = ["tenant_id = $1::uuid"]
    params: list[Any] = [tenant_id]
    if coworker_id is not None:
        params.append(coworker_id)
        clauses.append(f"coworker_id = ${len(params)}::uuid")
    if enabled is not None:
        params.append(enabled)
        clauses.append(f"enabled = ${len(params)}")
    sql = (
        "SELECT * FROM approval_policies WHERE "
        + " AND ".join(clauses)
        + " ORDER BY priority DESC, updated_at DESC"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [_record_to_approval_policy(r) for r in rows]


async def get_enabled_policies_for_coworker(
    tenant_id: str, coworker_id: str
) -> list[ApprovalPolicy]:
    """Policies applicable to a specific coworker.

    Includes both coworker-scoped policies (coworker_id matches) and
    tenant-wide policies (coworker_id IS NULL). Only returns enabled
    rows — container snapshots never carry disabled policies, and
    neither does the engine's dedup/match path.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM approval_policies
            WHERE tenant_id = $1::uuid
              AND enabled = TRUE
              AND (coworker_id IS NULL OR coworker_id = $2::uuid)
            ORDER BY priority DESC, updated_at DESC
            """,
            tenant_id,
            coworker_id,
        )
    return [_record_to_approval_policy(r) for r in rows]


async def update_approval_policy(
    policy_id: str,
    *,
    mcp_server_name: str | None = None,
    tool_name: str | None = None,
    condition_expr: dict[str, Any] | None = None,
    approver_user_ids: list[str] | None = None,
    notify_conversation_id: str | None = None,
    auto_expire_minutes: int | None = None,
    post_exec_mode: str | None = None,
    enabled: bool | None = None,
    priority: int | None = None,
) -> ApprovalPolicy | None:
    """Update selected fields on a policy; returns the new row or None."""
    fields: list[str] = []
    values: list[Any] = []
    idx = 1

    def _push(expr: str, value: Any) -> None:
        nonlocal idx
        fields.append(expr.format(i=idx))
        values.append(value)
        idx += 1

    if mcp_server_name is not None:
        _push("mcp_server_name = ${i}", mcp_server_name)
    if tool_name is not None:
        _push("tool_name = ${i}", tool_name)
    if condition_expr is not None:
        _push("condition_expr = ${i}::jsonb", json.dumps(condition_expr))
    if approver_user_ids is not None:
        _push("approver_user_ids = ${i}::uuid[]", approver_user_ids)
    if notify_conversation_id is not None:
        _push("notify_conversation_id = ${i}::uuid", notify_conversation_id)
    if auto_expire_minutes is not None:
        _push("auto_expire_minutes = ${i}", auto_expire_minutes)
    if post_exec_mode is not None:
        _push("post_exec_mode = ${i}", post_exec_mode)
    if enabled is not None:
        _push("enabled = ${i}", enabled)
    if priority is not None:
        _push("priority = ${i}", priority)

    if not fields:
        return await get_approval_policy(policy_id)

    fields.append("updated_at = now()")
    values.append(policy_id)
    sql = (
        "UPDATE approval_policies SET "
        + ", ".join(fields)
        + f" WHERE id = ${idx}::uuid RETURNING *"
    )
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *values)
    if row is None:
        return None
    return _record_to_approval_policy(row)


async def delete_approval_policy(policy_id: str) -> bool:
    """Hard-delete a policy. Returns True if a row was removed."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        result = await conn.execute(
            "DELETE FROM approval_policies WHERE id = $1::uuid", policy_id
        )
    return result.endswith(" 1")


# ---------------------------------------------------------------------------
# Approval requests CRUD
# ---------------------------------------------------------------------------


def _record_to_approval_request(row: asyncpg.Record) -> ApprovalRequest:
    from rolemesh.approval.types import ApprovalRequest

    actions = row["actions"]
    if isinstance(actions, str):
        actions = json.loads(actions) if actions else []
    hashes = row["action_hashes"] or []
    approvers = row["resolved_approvers"] or []
    return ApprovalRequest(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=str(row["coworker_id"]),
        conversation_id=str(row["conversation_id"]) if row["conversation_id"] else None,
        policy_id=str(row["policy_id"]),
        user_id=str(row["user_id"]),
        job_id=row["job_id"],
        mcp_server_name=row["mcp_server_name"],
        actions=list(actions) if isinstance(actions, list) else [],
        action_hashes=list(hashes),
        rationale=row["rationale"],
        source=row["source"],
        status=row["status"],
        post_exec_mode=row["post_exec_mode"] or "report",
        resolved_approvers=[str(a) for a in approvers],
        requested_at=row["requested_at"].isoformat() if row["requested_at"] else "",
        expires_at=row["expires_at"].isoformat() if row["expires_at"] else "",
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
    )


async def _set_approval_guc(
    conn: asyncpg.Connection[asyncpg.Record],
    *,
    actor_user_id: str | None,
    note: str | None,
    metadata: dict[str, Any] | None,
) -> None:
    """Set approval.* transaction-local GUCs for the audit trigger.

    The audit trigger (_approval_write_audit_from_trigger) reads these
    to attribute the audit row it emits. Call inside an open transaction;
    the ``is_local=true`` flag auto-clears on commit/rollback.
    """
    await conn.execute(
        "SELECT set_config('approval.actor_user_id', $1, true)",
        actor_user_id or "",
    )
    await conn.execute(
        "SELECT set_config('approval.note', $1, true)",
        note or "",
    )
    await conn.execute(
        "SELECT set_config('approval.metadata', $1, true)",
        json.dumps(metadata) if metadata else "",
    )


async def create_approval_request(
    *,
    tenant_id: str,
    coworker_id: str,
    conversation_id: str | None,
    policy_id: str | None,
    user_id: str,
    job_id: str,
    mcp_server_name: str,
    actions: list[dict[str, Any]],
    action_hashes: list[str],
    rationale: str | None,
    source: str,
    status: str,
    resolved_approvers: list[str],
    expires_at: datetime,
    post_exec_mode: str = "report",
    actor_user_id: str | None = None,
) -> ApprovalRequest:
    """Insert a new approval request row.

    ``actor_user_id`` is recorded by the audit trigger on the 'created'
    row. None ⇒ audit 'created' row has NULL actor (system-initiated).
    """
    pool = _get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _set_approval_guc(
            conn, actor_user_id=actor_user_id, note=None, metadata=None
        )
        row = await conn.fetchrow(
            """
            INSERT INTO approval_requests (
                tenant_id, coworker_id, conversation_id, policy_id,
                user_id, job_id, mcp_server_name,
                actions, action_hashes, rationale, source, status,
                post_exec_mode, resolved_approvers, expires_at
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4::uuid,
                $5::uuid, $6, $7,
                $8::jsonb, $9::text[], $10, $11, $12,
                $13, $14::uuid[], $15
            )
            RETURNING *
            """,
            tenant_id,
            coworker_id,
            conversation_id,
            policy_id,
            user_id,
            job_id,
            mcp_server_name,
            json.dumps(actions),
            list(action_hashes),
            rationale,
            source,
            status,
            post_exec_mode,
            list(resolved_approvers),
            expires_at,
        )
    assert row is not None
    return _record_to_approval_request(row)


async def get_approval_request(request_id: str) -> ApprovalRequest | None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM approval_requests WHERE id = $1::uuid", request_id
        )
    if row is None:
        return None
    return _record_to_approval_request(row)


async def list_approval_requests(
    tenant_id: str,
    *,
    status: str | None = None,
    coworker_id: str | None = None,
    limit: int = 100,
) -> list[ApprovalRequest]:
    pool = _get_pool()
    clauses = ["tenant_id = $1::uuid"]
    params: list[Any] = [tenant_id]
    if status is not None:
        params.append(status)
        clauses.append(f"status = ${len(params)}")
    if coworker_id is not None:
        params.append(coworker_id)
        clauses.append(f"coworker_id = ${len(params)}::uuid")
    params.append(limit)
    sql = (
        "SELECT * FROM approval_requests WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY created_at DESC LIMIT ${len(params)}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [_record_to_approval_request(r) for r in rows]


async def find_pending_request_by_action_hash(
    tenant_id: str, action_hash: str, within_minutes: int = 5
) -> ApprovalRequest | None:
    """Dedup key for auto-intercept: return the most recent pending
    request whose action_hashes array contains ``action_hash`` and was
    created within the last ``within_minutes``.

    This prevents the hook chain from creating two pending requests
    when an agent retries the same blocked tool call seconds apart.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT * FROM approval_requests
            WHERE tenant_id = $1::uuid
              AND status = 'pending'
              AND $2 = ANY(action_hashes)
              AND created_at > now() - ($3 || ' minutes')::interval
            ORDER BY created_at DESC
            LIMIT 1
            """,
            tenant_id,
            action_hash,
            str(within_minutes),
        )
    if row is None:
        return None
    return _record_to_approval_request(row)


class DecisionOutcome:
    """Return value of decide_approval_request_full.

    One of three shapes:
      updated    — the actual UPDATE landed; ``request`` is the new row.
      conflict   — the request was not pending; ``current_status`` says why.
      forbidden  — the request is pending but the caller is not an approver.
    """

    __slots__ = ("current_status", "kind", "request")

    def __init__(
        self,
        kind: str,
        request: ApprovalRequest | None = None,
        current_status: str | None = None,
    ) -> None:
        self.kind = kind  # "updated" | "conflict" | "forbidden" | "missing"
        self.request = request
        self.current_status = current_status


async def decide_approval_request_full(
    request_id: str,
    *,
    new_status: str,
    actor_user_id: str,
    note: str | None = None,
) -> DecisionOutcome:
    """Single-query decide that disambiguates 403 vs 409 vs 200 vs 404.

    Uses a CTE: we capture the pre-UPDATE status first, run the
    conditional UPDATE in the same statement, and return both — one
    round trip instead of two, and no race window where the status
    changes between two separate reads.

    Also sets the GUCs inside the same transaction so the audit trigger
    records the approver as the actor_user_id on the 'approved' /
    'rejected' row.
    """
    pool = _get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _set_approval_guc(
            conn, actor_user_id=actor_user_id, note=note, metadata=None
        )
        row = await conn.fetchrow(
            """
            WITH before AS (
                SELECT id, status, resolved_approvers
                FROM approval_requests
                WHERE id = $2::uuid
                FOR UPDATE
            ),
            upd AS (
                UPDATE approval_requests r
                SET status = $1, updated_at = now()
                FROM before b
                WHERE r.id = b.id
                  AND b.status = 'pending'
                  AND $3::uuid = ANY(b.resolved_approvers)
                RETURNING r.*
            )
            SELECT
                (SELECT row_to_json(upd) FROM upd) AS updated_row,
                (SELECT status FROM before) AS before_status,
                (SELECT $3::uuid = ANY(resolved_approvers) FROM before) AS is_approver
            """,
            new_status,
            request_id,
            actor_user_id,
        )
    if row is None:
        return DecisionOutcome(kind="missing")
    before_status = row["before_status"]
    if before_status is None:
        return DecisionOutcome(kind="missing")
    updated_raw = row["updated_row"]
    if updated_raw is not None:
        if isinstance(updated_raw, str):
            updated_raw = json.loads(updated_raw)
        # row_to_json strips column types; fetch the real row to get
        # datetime objects decoded correctly.
        updated = await get_approval_request(request_id)
        return DecisionOutcome(kind="updated", request=updated)
    if before_status != "pending":
        return DecisionOutcome(kind="conflict", current_status=before_status)
    # pending but UPDATE did not land → caller is not an approver.
    return DecisionOutcome(kind="forbidden", current_status=before_status)


async def claim_approval_for_execution(request_id: str) -> ApprovalRequest | None:
    """Atomic claim: approved → executing.

    The Worker uses this to take exclusive ownership before hitting the
    MCP server. If two Workers race, only one sees the row returned; the
    other gets None and must drop the NATS message.

    The audit trigger writes the 'executing' audit row with NULL actor
    (system transition).
    """
    pool = _get_pool()
    async with pool.acquire() as conn, conn.transaction():
        # No actor: the Worker is a system process, so the trigger writes
        # executing with NULL actor.
        await _set_approval_guc(
            conn, actor_user_id=None, note=None, metadata=None
        )
        row = await conn.fetchrow(
            """
            UPDATE approval_requests
            SET status = 'executing', updated_at = now()
            WHERE id = $1::uuid AND status = 'approved'
            RETURNING *
            """,
            request_id,
        )
    if row is None:
        return None
    return _record_to_approval_request(row)


async def set_approval_status(
    request_id: str,
    status: str,
    *,
    actor_user_id: str | None = None,
    note: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ApprovalRequest | None:
    """Unconditional status update. Used for system transitions that do
    not race (e.g. executing → executed by the Worker that already
    holds the claim).

    ``actor_user_id`` / ``note`` / ``metadata`` flow through to the
    audit trigger's 'status-change' row.
    """
    pool = _get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _set_approval_guc(
            conn,
            actor_user_id=actor_user_id,
            note=note,
            metadata=metadata,
        )
        row = await conn.fetchrow(
            """
            UPDATE approval_requests
            SET status = $1, updated_at = now()
            WHERE id = $2::uuid
            RETURNING *
            """,
            status,
            request_id,
        )
    if row is None:
        return None
    return _record_to_approval_request(row)


async def cancel_pending_approvals_for_job(job_id: str) -> list[str]:
    """Move all pending approvals for a job_id to 'cancelled'.

    Returns the IDs of the rows that transitioned, so the caller can
    write one audit row per cancellation and notify approvers.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            UPDATE approval_requests
            SET status = 'cancelled', updated_at = now()
            WHERE job_id = $1 AND status = 'pending'
            RETURNING id
            """,
            job_id,
        )
    return [str(r["id"]) for r in rows]


async def list_expired_pending_approvals() -> list[ApprovalRequest]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM approval_requests
            WHERE status = 'pending' AND expires_at < now()
            ORDER BY expires_at
            """
        )
    return [_record_to_approval_request(r) for r in rows]


async def list_stuck_approved_approvals(
    older_than_seconds: int = 60,
) -> list[ApprovalRequest]:
    """Approved rows that have been sitting for a while without being
    claimed by a Worker — either the Worker missed the NATS publish or
    the orchestrator restarted mid-flight. The reconciler republishes
    these."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM approval_requests
            WHERE status = 'approved'
              AND updated_at < now() - ($1 || ' seconds')::interval
            ORDER BY updated_at
            """,
            str(older_than_seconds),
        )
    return [_record_to_approval_request(r) for r in rows]


async def list_stuck_executing_approvals(
    older_than_seconds: int = 300,
) -> list[ApprovalRequest]:
    """Executing rows that never transitioned — a Worker probably crashed
    after claiming but before writing the terminal status. The reconciler
    marks them execution_stale rather than retrying, because we cannot
    tell whether the MCP-side work partially completed."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM approval_requests
            WHERE status = 'executing'
              AND updated_at < now() - ($1 || ' seconds')::interval
            ORDER BY updated_at
            """,
            str(older_than_seconds),
        )
    return [_record_to_approval_request(r) for r in rows]


# ---------------------------------------------------------------------------
# Approval audit log (append-only)
# ---------------------------------------------------------------------------


def _record_to_audit_entry(row: asyncpg.Record) -> ApprovalAuditEntry:
    from rolemesh.approval.types import ApprovalAuditEntry

    meta = row["metadata"]
    if isinstance(meta, str):
        meta = json.loads(meta) if meta else {}
    return ApprovalAuditEntry(
        id=str(row["id"]),
        request_id=str(row["request_id"]),
        action=row["action"],
        actor_user_id=str(row["actor_user_id"]) if row["actor_user_id"] else None,
        note=row["note"],
        metadata=meta if isinstance(meta, dict) else {},
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
    )


async def write_approval_audit(
    *,
    request_id: str,
    action: str,
    actor_user_id: str | None = None,
    note: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ApprovalAuditEntry:
    """Append a single audit row. There is deliberately no update or
    delete counterpart — the whole point of the audit table is that
    rows are immutable once written."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO approval_audit_log (request_id, action, actor_user_id, note, metadata)
            VALUES ($1::uuid, $2, $3::uuid, $4, $5::jsonb)
            RETURNING *
            """,
            request_id,
            action,
            actor_user_id,
            note,
            json.dumps(metadata or {}),
        )
    assert row is not None
    return _record_to_audit_entry(row)


async def list_approval_audit(request_id: str) -> list[ApprovalAuditEntry]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM approval_audit_log WHERE request_id = $1::uuid "
            "ORDER BY created_at ASC",
            request_id,
        )
    return [_record_to_audit_entry(r) for r in rows]


async def expire_approval_if_pending(request_id: str) -> ApprovalRequest | None:
    """Atomic pending → expired with the CAS guard kept in one place.

    Separate from set_approval_status because the maintenance loop
    must not trample a concurrent decide_approval_request.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE approval_requests
            SET status = 'expired', updated_at = now()
            WHERE id = $1::uuid AND status = 'pending'
            RETURNING *
            """,
            request_id,
        )
    if row is None:
        return None
    return _record_to_approval_request(row)


# ---------------------------------------------------------------------------
# Safety Framework CRUD
# ---------------------------------------------------------------------------


def _record_to_safety_rule(row: asyncpg.Record) -> SafetyRule:
    from rolemesh.safety.types import Rule, Stage

    cfg = row["config"]
    if isinstance(cfg, str):
        cfg = json.loads(cfg) if cfg else {}
    return Rule(
        id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        coworker_id=str(row["coworker_id"]) if row["coworker_id"] else None,
        stage=Stage(row["stage"]),
        check_id=row["check_id"],
        config=cfg if isinstance(cfg, dict) else {},
        priority=int(row["priority"]),
        enabled=bool(row["enabled"]),
        description=row["description"] or "",
        created_at=row["created_at"].isoformat() if row["created_at"] else "",
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else "",
    )


async def _set_safety_guc(
    conn: asyncpg.Connection[asyncpg.Record],
    *,
    actor_user_id: str | None,
) -> None:
    """Set the transaction-local ``safety.actor_user_id`` GUC.

    The audit trigger ``_safety_rules_write_audit_from_trigger``
    reads this to attribute the audit row it emits. Call inside an
    open transaction; the ``is_local=true`` flag auto-clears on
    commit/rollback.
    """
    await conn.execute(
        "SELECT set_config('safety.actor_user_id', $1, true)",
        actor_user_id or "",
    )


async def create_safety_rule(
    *,
    tenant_id: str,
    stage: str,
    check_id: str,
    config: dict[str, Any],
    coworker_id: str | None = None,
    priority: int = 100,
    enabled: bool = True,
    description: str = "",
    actor_user_id: str | None = None,
) -> SafetyRule:
    """Insert a new safety rule and return the stored row.

    ``actor_user_id`` is attributed to the audit row written by the
    trigger. ``None`` is a legitimate value for bulk imports / migration
    scripts where no user is the actor — the audit row carries NULL.
    """
    pool = _get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _set_safety_guc(conn, actor_user_id=actor_user_id)
        row = await conn.fetchrow(
            """
            INSERT INTO safety_rules (
                tenant_id, coworker_id, stage, check_id,
                config, priority, enabled, description
            )
            VALUES (
                $1::uuid, $2::uuid, $3, $4,
                $5::jsonb, $6, $7, $8
            )
            RETURNING *
            """,
            tenant_id,
            coworker_id,
            stage,
            check_id,
            json.dumps(config),
            priority,
            enabled,
            description,
        )
    assert row is not None
    return _record_to_safety_rule(row)


async def get_safety_rule(rule_id: str) -> SafetyRule | None:
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM safety_rules WHERE id = $1::uuid", rule_id
        )
    if row is None:
        return None
    return _record_to_safety_rule(row)


async def list_safety_rules(
    tenant_id: str,
    *,
    coworker_id: str | None = None,
    stage: str | None = None,
    enabled: bool | None = None,
) -> list[SafetyRule]:
    """List rules for a tenant, optionally filtered."""
    pool = _get_pool()
    clauses = ["tenant_id = $1::uuid"]
    params: list[Any] = [tenant_id]
    if coworker_id is not None:
        params.append(coworker_id)
        clauses.append(f"coworker_id = ${len(params)}::uuid")
    if stage is not None:
        params.append(stage)
        clauses.append(f"stage = ${len(params)}")
    if enabled is not None:
        params.append(enabled)
        clauses.append(f"enabled = ${len(params)}")
    sql = (
        "SELECT * FROM safety_rules WHERE "
        + " AND ".join(clauses)
        + " ORDER BY priority DESC, updated_at DESC"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    return [_record_to_safety_rule(r) for r in rows]


async def list_safety_rules_for_coworker(
    tenant_id: str, coworker_id: str
) -> list[SafetyRule]:
    """Rules applicable to a specific coworker (coworker-scoped OR tenant-wide).

    Mirrors ``get_enabled_policies_for_coworker`` in the approval module:
    only enabled rows are returned, and a NULL ``coworker_id`` means the
    rule applies to every coworker in the tenant.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT * FROM safety_rules
            WHERE tenant_id = $1::uuid
              AND enabled = TRUE
              AND (coworker_id IS NULL OR coworker_id = $2::uuid)
            ORDER BY priority DESC, updated_at DESC
            """,
            tenant_id,
            coworker_id,
        )
    return [_record_to_safety_rule(r) for r in rows]


async def update_safety_rule(
    rule_id: str,
    *,
    stage: str | None = None,
    check_id: str | None = None,
    config: dict[str, Any] | None = None,
    coworker_id: str | None = None,
    coworker_id_set: bool = False,
    priority: int | None = None,
    enabled: bool | None = None,
    description: str | None = None,
    actor_user_id: str | None = None,
) -> SafetyRule | None:
    """Update selected fields on a rule; returns the new row or None.

    ``coworker_id_set=True`` is required to explicitly set coworker_id
    (including setting it to NULL for a tenant-wide scope); without
    this flag, passing ``coworker_id=None`` is indistinguishable from
    "don't change". This mirrors the three-state Optional convention
    used elsewhere in this module.

    ``actor_user_id`` attributes the audit row. A no-op update (all
    fields unchanged) skips both the DML and the audit row — the
    trigger's ``IF v_before <> v_after`` guard does the filtering.
    """
    fields: list[str] = []
    values: list[Any] = []
    idx = 1

    def _push(expr: str, value: Any) -> None:
        nonlocal idx
        fields.append(expr.format(i=idx))
        values.append(value)
        idx += 1

    if stage is not None:
        _push("stage = ${i}", stage)
    if check_id is not None:
        _push("check_id = ${i}", check_id)
    if config is not None:
        _push("config = ${i}::jsonb", json.dumps(config))
    if coworker_id_set:
        _push("coworker_id = ${i}::uuid", coworker_id)
    if priority is not None:
        _push("priority = ${i}", priority)
    if enabled is not None:
        _push("enabled = ${i}", enabled)
    if description is not None:
        _push("description = ${i}", description)

    if not fields:
        return await get_safety_rule(rule_id)

    fields.append("updated_at = now()")
    values.append(rule_id)
    sql = (
        "UPDATE safety_rules SET "
        + ", ".join(fields)
        + f" WHERE id = ${idx}::uuid RETURNING *"
    )
    pool = _get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _set_safety_guc(conn, actor_user_id=actor_user_id)
        row = await conn.fetchrow(sql, *values)
    if row is None:
        return None
    return _record_to_safety_rule(row)


async def delete_safety_rule(
    rule_id: str, *, actor_user_id: str | None = None
) -> bool:
    """Hard-delete a rule. Returns True if a row was removed.

    The audit trigger captures the row's pre-delete state in
    before_state so the deleted rule is reconstructable forever.
    """
    pool = _get_pool()
    async with pool.acquire() as conn, conn.transaction():
        await _set_safety_guc(conn, actor_user_id=actor_user_id)
        result = await conn.execute(
            "DELETE FROM safety_rules WHERE id = $1::uuid", rule_id
        )
    return result.endswith(" 1")


async def list_safety_rules_audit(
    *,
    tenant_id: str,
    rule_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """List rule-change audit rows, newest first.

    Filtered by tenant_id (never cross-tenant). ``rule_id`` optional
    to narrow to a specific rule's history. Returns plain dicts; the
    V2 admin UI will surface this as a timeline. Test fixture uses it
    to pin actor/action correctness.
    """
    pool = _get_pool()
    clauses = ["tenant_id = $1::uuid"]
    params: list[Any] = [tenant_id]
    if rule_id is not None:
        params.append(rule_id)
        clauses.append(f"rule_id = ${len(params)}::uuid")
    params.append(limit)
    sql = (
        "SELECT * FROM safety_rules_audit WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY created_at DESC LIMIT ${len(params)}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    result: list[dict[str, Any]] = []
    for r in rows:
        before = r["before_state"]
        after = r["after_state"]
        if isinstance(before, str):
            before = json.loads(before) if before else None
        if isinstance(after, str):
            after = json.loads(after) if after else None
        result.append(
            {
                "id": str(r["id"]),
                "rule_id": str(r["rule_id"]),
                "tenant_id": str(r["tenant_id"]),
                "action": r["action"],
                "actor_user_id": str(r["actor_user_id"])
                if r["actor_user_id"]
                else None,
                "before_state": before,
                "after_state": after,
                "created_at": r["created_at"].isoformat()
                if r["created_at"]
                else "",
            }
        )
    return result


async def insert_safety_decision(
    *,
    tenant_id: str,
    stage: str,
    verdict_action: str,
    triggered_rule_ids: list[str],
    findings: list[dict[str, Any]],
    context_digest: str,
    context_summary: str,
    coworker_id: str | None = None,
    conversation_id: str | None = None,
    job_id: str | None = None,
    approval_context: dict[str, Any] | None = None,
) -> str:
    """Write one audit row; return its id.

    Called by the safety_events subscriber for every decision the
    container publishes. Never raises on per-row validation — malformed
    inputs should be filtered upstream in ``SafetyEngine.handle_safety_event``.

    ``approval_context`` is retained only for rows with
    ``verdict_action='require_approval'``; for other actions the
    caller passes None and the column stays NULL. See the 24-hour
    cleanup task note in the ``safety_decisions`` schema block.
    """
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO safety_decisions (
                tenant_id, coworker_id, conversation_id, job_id,
                stage, verdict_action, triggered_rule_ids,
                findings, context_digest, context_summary,
                approval_context
            )
            VALUES (
                $1::uuid, $2::uuid, $3, $4,
                $5, $6, $7::uuid[],
                $8::jsonb, $9, $10, $11::jsonb
            )
            RETURNING id
            """,
            tenant_id,
            coworker_id,
            conversation_id,
            job_id,
            stage,
            verdict_action,
            triggered_rule_ids,
            json.dumps(findings),
            context_digest,
            context_summary,
            json.dumps(approval_context) if approval_context else None,
        )
    assert row is not None
    return str(row["id"])


async def list_safety_decisions(
    tenant_id: str,
    *,
    verdict_action: str | None = None,
    coworker_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Read recent safety decisions for a tenant.

    Returns plain dicts (not a dataclass) because V1 does not expose
    these via REST — the shape is internal to tests and V2's audit UI.
    Newest-first ordering; ``limit`` caps the result set.
    """
    pool = _get_pool()
    clauses = ["tenant_id = $1::uuid"]
    params: list[Any] = [tenant_id]
    if verdict_action is not None:
        params.append(verdict_action)
        clauses.append(f"verdict_action = ${len(params)}")
    if coworker_id is not None:
        params.append(coworker_id)
        clauses.append(f"coworker_id = ${len(params)}::uuid")
    params.append(limit)
    sql = (
        "SELECT * FROM safety_decisions WHERE "
        + " AND ".join(clauses)
        + f" ORDER BY created_at DESC LIMIT ${len(params)}"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
    result: list[dict[str, Any]] = []
    for r in rows:
        findings = r["findings"]
        if isinstance(findings, str):
            findings = json.loads(findings) if findings else []
        result.append(
            {
                "id": str(r["id"]),
                "tenant_id": str(r["tenant_id"]),
                "coworker_id": str(r["coworker_id"]) if r["coworker_id"] else None,
                "conversation_id": r["conversation_id"],
                "job_id": r["job_id"],
                "stage": r["stage"],
                "verdict_action": r["verdict_action"],
                "triggered_rule_ids": [str(u) for u in (r["triggered_rule_ids"] or [])],
                "findings": findings if isinstance(findings, list) else [],
                "context_digest": r["context_digest"],
                "context_summary": r["context_summary"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else "",
            }
        )
    return result
