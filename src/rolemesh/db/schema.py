"""DDL — table / index / RLS creation.

Pure schema module: every helper here takes a ``conn`` from the caller
and emits CREATE / ALTER statements. No connection pool, no global
state, no CRUD. The lifecycle helpers in ``rolemesh.db._pool`` are the
only callers; tests reach in via ``_pool._init_test_database``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


__all__ = [
    "_create_schema",
]


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
            agent_backend TEXT DEFAULT 'claude',
            system_prompt TEXT,
            tools JSONB DEFAULT '[]',
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
                ("agent_backend", "'claude'"),
                ("system_prompt", "NULL"),
                ("tools", "'[]'::jsonb"),
            ]:
                await conn.execute(
                    f"ALTER TABLE coworkers ADD COLUMN IF NOT EXISTS {col} "
                    f"{'JSONB' if col == 'tools' else 'TEXT'} DEFAULT {default}"
                )
            await conn.execute("""
                UPDATE coworkers SET
                    agent_backend = r.agent_backend,
                    system_prompt = r.system_prompt,
                    tools = r.tools
                FROM roles r WHERE coworkers.role_id = r.id
            """)
            await conn.execute("ALTER TABLE coworkers DROP COLUMN role_id")
        await conn.execute("DROP TABLE IF EXISTS roles CASCADE")
    # Drop the legacy `skills` JSONB column on existing dev databases.
    # The skill system is moving to dedicated `skills` / `skill_files` tables;
    # the old per-coworker JSONB list was never consumed by the runner.
    await conn.execute("ALTER TABLE coworkers DROP COLUMN IF EXISTS skills")
    # Rename legacy backend value: ``claude-code`` was the original name
    # before the Pi integration (commit c032db0) renamed it to ``claude``.
    # The alias was kept for back-compat; this idempotent UPDATE retires
    # it so the alias entry can be removed from BACKEND_CONFIGS.
    await conn.execute(
        "UPDATE coworkers SET agent_backend = 'claude' "
        "WHERE agent_backend = 'claude-code'"
    )
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

    # Skills: per-coworker capability folders. SKILL.md (frontmatter +
    # body) lives in the row keyed by ``path = 'SKILL.md'`` in
    # ``skill_files``. Frontmatter is split between common (carries
    # name/description for both runtimes) and backend-specific
    # (Claude SDK or Pi loader). See docs/skills-architecture.md.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS skills (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            coworker_id UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
            name TEXT NOT NULL CHECK (name ~ '^[a-zA-Z][a-zA-Z0-9_-]{0,63}$'),
            frontmatter_common JSONB NOT NULL DEFAULT '{}',
            frontmatter_backend JSONB NOT NULL DEFAULT '{}',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by UUID REFERENCES users(id) ON DELETE SET NULL,
            UNIQUE (coworker_id, name)
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skills_coworker ON skills(coworker_id, enabled)"
    )

    # skill_files holds the file tree. Path validation: positive
    # whitelist (each segment alphanumeric-led, only [A-Za-z0-9_.-]),
    # plus a defense-in-depth regex that rejects any segment that is
    # purely dots. The application-layer validator in
    # rolemesh.core.skills mirrors these rules. Raw triple-quoted
    # string keeps PG-side regex escapes (``\.``) intact without
    # triggering Python SyntaxWarnings.
    await conn.execute(r"""
        CREATE TABLE IF NOT EXISTS skill_files (
            skill_id UUID NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
            path TEXT NOT NULL CHECK (length(path) > 0
                AND path ~ '^[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*$'
                AND path !~ '(^|/)\.+($|/)'),
            content TEXT NOT NULL,
            mime_type TEXT NOT NULL DEFAULT 'text/plain',
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (skill_id, path)
        )
    """)

    # SECURITY DEFINER trigger: prevents writing a skill whose
    # ``coworker_id`` belongs to a different tenant than its own
    # ``tenant_id``. Without DEFINER, the trigger would run RLS-bound
    # on coworkers and would not be able to see a foreign tenant's
    # coworker (cw_tenant comes back NULL), missing the attack.
    # ``IS DISTINCT FROM`` handles NULL safely either way.
    await conn.execute("""
        CREATE OR REPLACE FUNCTION skills_check_coworker_tenant()
        RETURNS TRIGGER
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        LANGUAGE plpgsql AS $func$
        DECLARE
            cw_tenant UUID;
        BEGIN
            SELECT tenant_id INTO cw_tenant FROM coworkers WHERE id = NEW.coworker_id;
            IF cw_tenant IS DISTINCT FROM NEW.tenant_id THEN
                RAISE EXCEPTION
                    'skills.coworker_id % belongs to a different tenant '
                    '(or does not exist)', NEW.coworker_id;
            END IF;
            RETURN NEW;
        END
        $func$;
    """)
    await conn.execute(
        "DROP TRIGGER IF EXISTS trg_skills_check_coworker_tenant ON skills"
    )
    await conn.execute("""
        CREATE TRIGGER trg_skills_check_coworker_tenant
            BEFORE INSERT OR UPDATE OF coworker_id, tenant_id ON skills
            FOR EACH ROW EXECUTE FUNCTION skills_check_coworker_tenant();
    """)

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

    # PR-D D10: backfill tenant_id on oidc_user_tokens so RLS can
    # bind it. Same pattern PR #11 used on approval_audit_log:
    # ADD COLUMN nullable + UPDATE from parent + SET NOT NULL + FK
    # CASCADE + composite index + BEFORE-INSERT trigger to keep new
    # rows in sync with users.tenant_id without making every caller
    # remember to pass it.
    await conn.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'oidc_user_tokens'
                  AND column_name = 'tenant_id'
            ) THEN
                ALTER TABLE oidc_user_tokens ADD COLUMN tenant_id UUID;
                UPDATE oidc_user_tokens t
                   SET tenant_id = u.tenant_id
                  FROM users u
                 WHERE u.id = t.user_id AND t.tenant_id IS NULL;
                ALTER TABLE oidc_user_tokens
                    ALTER COLUMN tenant_id SET NOT NULL;
                ALTER TABLE oidc_user_tokens
                    ADD CONSTRAINT oidc_user_tokens_tenant_fk
                    FOREIGN KEY (tenant_id) REFERENCES tenants(id)
                    ON DELETE CASCADE;
                CREATE INDEX idx_oidc_user_tokens_tenant
                    ON oidc_user_tokens (tenant_id, user_id);
            END IF;
        END $$;
    """)
    await conn.execute("""
        CREATE OR REPLACE FUNCTION oidc_user_tokens_set_tenant()
        RETURNS TRIGGER AS $trigger$
        BEGIN
            IF NEW.tenant_id IS NULL THEN
                SELECT tenant_id INTO NEW.tenant_id
                  FROM users WHERE id = NEW.user_id;
            END IF;
            RETURN NEW;
        END $trigger$ LANGUAGE plpgsql;
    """)
    await conn.execute(
        "DROP TRIGGER IF EXISTS trg_oidc_user_tokens_set_tenant "
        "ON oidc_user_tokens"
    )
    await conn.execute("""
        CREATE TRIGGER trg_oidc_user_tokens_set_tenant
            BEFORE INSERT ON oidc_user_tokens
            FOR EACH ROW EXECUTE FUNCTION oidc_user_tokens_set_tenant();
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
    # Token usage columns on messages — applied unconditionally (outside
    # the legacy branch above) so existing deployments running on the
    # already-migrated schema also get the new columns. Idempotent ADD
    # COLUMN IF NOT EXISTS keeps repeat startups a no-op. NUMERIC(10,6)
    # for cost_usd: 6-digit precision matches the smallest sub-cent
    # increment ($0.000001) Claude SDK reports, and avoids the floating-
    # point accumulation drift that would bite when summing per-tenant.
    await conn.execute(
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS input_tokens INTEGER"
    )
    await conn.execute(
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS output_tokens INTEGER"
    )
    await conn.execute(
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS cache_read_tokens INTEGER"
    )
    await conn.execute(
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER"
    )
    await conn.execute(
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS cost_usd NUMERIC(10,6)"
    )
    await conn.execute(
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS model_id TEXT"
    )
    if not legacy_exists:
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

    # tenant_id is denormalised onto the audit row (the canonical value
    # lives on approval_requests) so that cross-tenant audit reads can
    # be rejected at the SQL layer without a JOIN. The trigger below
    # copies tenant_id from the parent row on every INSERT, which is
    # cheaper than the alternative of always joining at query time —
    # audit reads happen on a hot REST path.
    #
    # The CREATE TABLE / column-add / backfill / SET NOT NULL block runs
    # inside a transaction so that an in-place upgrade is atomic. Without
    # this, a concurrent INSERT through the OLD trigger between
    # ``ADD COLUMN`` and ``SET NOT NULL`` would leave a NULL-tenant row
    # that fails the NOT NULL promotion. Single-instance startup makes
    # the race unlikely, but multi-instance rolling deploys would hit it.
    async with conn.transaction():
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS approval_audit_log (
                id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id     UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
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
        # In-place upgrade for databases created before tenant_id was
        # introduced. Add nullable → backfill from parent → set NOT NULL.
        # Idempotent against repeat startups (ADD COLUMN IF NOT EXISTS
        # short-circuits once the column exists; the UPDATE filters
        # WHERE tenant_id IS NULL so backfilled rows are skipped).
        await conn.execute(
            "ALTER TABLE approval_audit_log "
            "ADD COLUMN IF NOT EXISTS tenant_id UUID "
            "REFERENCES tenants(id) ON DELETE CASCADE"
        )
        await conn.execute(
            "UPDATE approval_audit_log al "
            "SET tenant_id = ar.tenant_id "
            "FROM approval_requests ar "
            "WHERE al.request_id = ar.id AND al.tenant_id IS NULL"
        )
        await conn.execute(
            "ALTER TABLE approval_audit_log "
            "ALTER COLUMN tenant_id SET NOT NULL"
        )
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_request "
            "ON approval_audit_log(request_id)"
        )
        # Composite index keeps tenant-scoped audit reads fast: index
        # seek on (tenant_id, request_id) then sorted scan on created_at.
        await conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_log_tenant_request "
            "ON approval_audit_log(tenant_id, request_id, created_at)"
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
                -- caller (v_actor_uuid) when provided. tenant_id is
                -- copied from the parent row so audit reads can filter
                -- by tenant without a JOIN.
                INSERT INTO approval_audit_log
                    (tenant_id, request_id, action, actor_user_id, note, metadata)
                VALUES
                    (NEW.tenant_id, NEW.id, 'created', v_actor_uuid,
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
                        (tenant_id, request_id, action, actor_user_id, note, metadata)
                    VALUES
                        (NEW.tenant_id, NEW.id, NEW.status, NULL, NULL, '{}'::jsonb);
                END IF;
            ELSIF TG_OP = 'UPDATE' AND NEW.status <> OLD.status THEN
                INSERT INTO approval_audit_log
                    (tenant_id, request_id, action, actor_user_id, note, metadata)
                VALUES
                    (NEW.tenant_id, NEW.id, NEW.status, v_actor_uuid,
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

    # Eval framework — one row per eval run.
    # coworker_config is the frozen-at-run-time snapshot (system_prompt,
    # tools, skills incl. file contents, permissions, agent_backend);
    # the sha256 lets ``rolemesh-eval list`` cluster runs that share a
    # configuration. coworker_id is FK SET NULL so historical runs
    # survive the underlying coworker being deleted — the JSONB still
    # records what was tested.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS eval_runs (
            id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id              UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            coworker_id            UUID REFERENCES coworkers(id) ON DELETE SET NULL,
            coworker_config        JSONB NOT NULL,
            coworker_config_sha256 TEXT NOT NULL,
            dataset_path           TEXT NOT NULL,
            dataset_sha256         TEXT NOT NULL,
            eval_log_uri           TEXT,
            metrics                JSONB,
            status                 TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'completed', 'failed', 'aborted')),
            created_by             UUID REFERENCES users(id) ON DELETE SET NULL,
            started_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at            TIMESTAMPTZ
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_runs_tenant_time "
        "ON eval_runs (tenant_id, started_at DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_runs_coworker_time "
        "ON eval_runs (tenant_id, coworker_id, started_at DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_eval_runs_config_sha "
        "ON eval_runs (tenant_id, coworker_config_sha256)"
    )

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

    # ----- RLS infrastructure (PR-B) ---------------------------------------
    # current_tenant_id() reads the per-connection GUC set by
    # tenant_conn(). Returns NULL when unset → policies of the form
    # ``USING (tenant_id = current_tenant_id())`` evaluate to NULL,
    # which the planner treats as "row excluded" (fail-closed). STABLE
    # lets PG cache the result within a single statement.
    await conn.execute("""
        CREATE OR REPLACE FUNCTION current_tenant_id() RETURNS UUID
        LANGUAGE sql STABLE AS $$
            SELECT NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        $$
    """)

    # rolemesh_app: business connections; NOBYPASSRLS so policies bind.
    # rolemesh_system: maintenance/resolver/DDL connections; BYPASSRLS
    # because cross-tenant work is intentional (named via admin_conn()
    # in code so it's grep-able). LOGIN on both — operators set passwords
    # out-of-band via ALTER ROLE.
    await conn.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'rolemesh_app'
            ) THEN
                CREATE ROLE rolemesh_app LOGIN NOBYPASSRLS;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_roles WHERE rolname = 'rolemesh_system'
            ) THEN
                CREATE ROLE rolemesh_system LOGIN BYPASSRLS;
            END IF;
        END $$;
    """)

    await conn.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
        "TO rolemesh_app, rolemesh_system"
    )
    await conn.execute(
        "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public "
        "TO rolemesh_app, rolemesh_system"
    )
    await conn.execute(
        "GRANT EXECUTE ON FUNCTION current_tenant_id() "
        "TO rolemesh_app, rolemesh_system"
    )
    # external_tenant_map and tenants are owner-administered. Even
    # under PR-D the business role must not touch them — RLS is the
    # second layer; the GRANT is the first.
    await conn.execute("REVOKE ALL ON external_tenant_map FROM rolemesh_app")
    await conn.execute("REVOKE ALL ON tenants FROM rolemesh_app")

    # Future tables created by migrations should inherit the same privs
    # automatically so we don't have to remember to GRANT after each DDL.
    await conn.execute(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES "
        "TO rolemesh_app, rolemesh_system"
    )

    # ----- RLS policies (PR-D) ---------------------------------------------
    # Each table that holds tenant data gets ENABLE + FORCE + four
    # policies. FORCE keeps the table owner (which superuser DDL flags
    # as a bypass path) under the policy too — only roles with
    # BYPASSRLS see across rows.
    #
    # Order is canary-first (small audit table) then increasing
    # blast-radius. Per design, each table arrives in its own commit
    # so a single-table regression is trivially revertable via
    # ``ALTER TABLE <t> DISABLE ROW LEVEL SECURITY``.
    await _enable_rls_on(conn, "approval_audit_log")  # D1 canary
    await _enable_rls_on(conn, "approval_requests")    # D2
    await _enable_rls_on(conn, "approval_policies")    # D3
    await _enable_rls_on(conn, "safety_rules")         # D4 (safety triplet)
    await _enable_rls_on(conn, "safety_decisions")
    await _enable_rls_on(conn, "safety_rules_audit")
    await _enable_rls_on(conn, "scheduled_tasks")      # D5
    await _enable_rls_on(conn, "task_run_logs")
    await _enable_rls_on(conn, "messages")             # D6
    await _enable_rls_on(conn, "conversations")        # D7
    await _enable_rls_on(conn, "sessions")
    await _enable_rls_on(conn, "coworkers")            # D8
    await _enable_rls_on(conn, "channel_bindings")
    await _enable_rls_on(conn, "user_agent_assignments")
    await _enable_rls_on(conn, "users")                # D9
    await _enable_rls_on(conn, "oidc_user_tokens")     # D10 (tenant_id backfilled above)
    await _enable_rls_on(conn, "skills")               # skills feature: standard tenant_id scope
    await _enable_rls_on_transitive_skill_files(conn)
    await _enable_rls_on(conn, "eval_runs")            # eval framework


async def _enable_rls_on_transitive_skill_files(
    conn: asyncpg.pool.PoolConnectionProxy[asyncpg.Record],
) -> None:
    """RLS for ``skill_files`` keyed transitively through the parent ``skills`` row.

    ``skill_files`` does not carry its own ``tenant_id`` — its parent
    ``skills`` row does. The EXISTS subquery is itself RLS-bound on
    ``skills``, so a tenant A session looking up a tenant B
    skill_file sees zero matches and the row is hidden / write
    rejected. See docs/skills-architecture.md "RLS" section for the
    rationale on not denormalizing tenant_id onto this table.
    """
    table = "skill_files"
    await conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    await conn.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    parent_check = (
        "EXISTS (SELECT 1 FROM skills "
        "WHERE skills.id = skill_files.skill_id "
        "AND skills.tenant_id = current_tenant_id())"
    )
    for op, body in (
        ("SELECT", f"USING ({parent_check})"),
        ("INSERT", f"WITH CHECK ({parent_check})"),
        ("UPDATE", f"USING ({parent_check}) WITH CHECK ({parent_check})"),
        ("DELETE", f"USING ({parent_check})"),
    ):
        policy = f"rls_{op.lower()}"
        await conn.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        await conn.execute(f"CREATE POLICY {policy} ON {table} FOR {op} {body}")


async def _enable_rls_on(
    conn: asyncpg.pool.PoolConnectionProxy[asyncpg.Record], table: str
) -> None:
    """Enable RLS on one table with the four standard policies.

    The policy names ``rls_select`` / ``rls_insert`` / ``rls_update`` /
    ``rls_delete`` are reused across every table — they live in a
    per-table namespace, so collisions are impossible. ``DROP POLICY
    IF EXISTS`` keeps schema bootstrap idempotent.

    Used per-table at the end of ``_create_schema``. Each row's
    ``tenant_id`` column is matched against ``current_tenant_id()``,
    which reads ``app.current_tenant_id`` GUC. Connections that
    forgot to set the GUC see zero rows (fail closed).
    """
    await conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    await conn.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    for op, body in (
        ("SELECT", "USING (tenant_id = current_tenant_id())"),
        ("INSERT", "WITH CHECK (tenant_id = current_tenant_id())"),
        (
            "UPDATE",
            "USING (tenant_id = current_tenant_id()) "
            "WITH CHECK (tenant_id = current_tenant_id())",
        ),
        ("DELETE", "USING (tenant_id = current_tenant_id())"),
    ):
        policy = f"rls_{op.lower()}"
        await conn.execute(f"DROP POLICY IF EXISTS {policy} ON {table}")
        await conn.execute(f"CREATE POLICY {policy} ON {table} FOR {op} {body}")

