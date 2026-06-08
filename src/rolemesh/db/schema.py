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


async def _seed_reference_data(
    conn: asyncpg.pool.PoolConnectionProxy[asyncpg.Record],
) -> None:
    """Idempotent reference/seed rows: the default tenant and the
    platform model catalog. Extracted from ``_create_schema`` so the
    test harness can re-seed after a per-test ``TRUNCATE`` without
    re-running the full DDL (see ``_pool._reset_test_data``). All
    inserts use ``ON CONFLICT DO NOTHING`` so re-runs are safe."""
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

    # ----- Platform models seed (v1.1 §2.1) -------------------------------
    # Curated catalog of provider × model_id combinations the platform
    # ships with. Each tuple is a ``(provider, model_id, model_family,
    # display_name)``. The list is intentionally conservative: Claude
    # entries match knowledge cutoff + agent compatibility (see
    # ``rolemesh.core.backend_capabilities``); Bedrock entries use the
    # ``us.anthropic.*`` cross-region inference profile pattern that
    # Pi already expects via ``PI_MODEL_ID`` (see README §"Pi backend"
    # and ``tests/agent/test_executor.py``); OpenAI / Google entries
    # are placeholders to make the credential / Phase 2 selector UI
    # rendering plausible — code paths that actually exercise these
    # models live in Pi, not rolemesh, so they remain ``is_platform=
    # TRUE`` and the catalog is the source of truth.
    #
    # ``ON CONFLICT DO NOTHING`` makes the seed idempotent against
    # re-runs; rows are matched on the UNIQUE (provider, model_id)
    # constraint.
    _MODEL_SEED: list[tuple[str, str, str, str]] = [  # noqa: N806
        ("anthropic", "claude-opus-4-7",            "claude", "Claude Opus 4.7"),
        ("anthropic", "claude-sonnet-4-6",          "claude", "Claude Sonnet 4.6"),
        ("anthropic", "claude-haiku-4-5-20251001",  "claude", "Claude Haiku 4.5"),
        ("bedrock",   "us.anthropic.claude-sonnet-4-6", "claude", "Claude Sonnet 4.6 (Bedrock)"),
        ("openai",    "gpt-4o",                     "gpt",    "GPT-4o"),
        ("google",    "gemini-2.5-flash",           "gemini", "Gemini 2.5 Flash"),
    ]
    for provider, model_id, family, display in _MODEL_SEED:
        await conn.execute(
            "INSERT INTO models (provider, model_id, model_family, display_name) "
            "VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (provider, model_id) DO NOTHING",
            provider, model_id, family, display,
        )

    # ----- Platform safety rules seed (default tier) ----------------------
    await _seed_platform_safety_rules(conn)


# Default-tier platform safety rules: "industry best-practice" detections
# the platform ships ON for every tenant. Tenants cannot loosen them (the
# table is read-only to the business role and the pipeline is monotonic —
# adding a tenant rule can only add restrictions); a tenant tightens by
# adding their own stricter ``safety_rules`` rows alongside.
#
# Config / action posture:
#   - secret_scanner / prompt_injection / jailbreak / toxicity ship their
#     natural ``block`` verdict — credential leakage, injection, known
#     jailbreak phrases and toxic output are unambiguous platform risks.
#   - pii.regex ships a non-disruptive ``warn`` baseline scoped to high-
#     confidence identifiers (SSN / credit card). PII appears legitimately
#     in prompts constantly, so a cross-tenant DEFAULT only surfaces it;
#     a tenant can tighten warn→block by adding their own rule. EMAIL /
#     PHONE / IP are deliberately left off the baseline (too false-
#     positive-prone for an all-tenant default).
#
# Stages bind to wired hooks only: INPUT_PROMPT (container-side) and
# MODEL_OUTPUT (orchestrator-side). Slow checks reach the orchestrator
# registry via RemoteCheck / the in-process MODEL_OUTPUT pipeline.
#
# Tuple shape: (check_id, stage, config, priority, description).
_PLATFORM_DEFAULT_RULES: list[tuple[str, str, dict[str, object], int, str]] = [
    (
        "secret_scanner", "model_output", {}, 1000,
        "Block credential / API-key leakage in model output.",
    ),
    (
        "pii.regex", "input_prompt",
        {
            "patterns": {"SSN": True, "CREDIT_CARD": True},
            "action_override": "warn",
        },
        1000,
        "Surface high-confidence PII (SSN / credit card) in user prompts.",
    ),
    (
        "llm_guard.prompt_injection", "input_prompt", {"threshold": 0.9}, 1000,
        "Block prompt-injection attempts in user prompts (OWASP LLM01).",
    ),
    (
        "llm_guard.jailbreak", "input_prompt", {}, 1000,
        "Block known jailbreak phrases in user prompts.",
    ),
    (
        "llm_guard.toxicity", "model_output", {"threshold": 0.7}, 1000,
        "Block toxic model output.",
    ),
]


async def _seed_platform_safety_rules(
    conn: asyncpg.pool.PoolConnectionProxy[asyncpg.Record],
) -> None:
    """Idempotent seed of the 5 default-tier platform safety rules.

    Keyed on the UNIQUE ``(tier, check_id, stage)`` identity so re-runs
    and post-``TRUNCATE`` re-seeds (``_reset_test_data``) are safe. Only
    the ``default`` tier is seeded this phase; ``floor`` /
    ``transparent_floor`` are carried in the schema for future use.

    Each row is stamped ``is_seeded = TRUE`` so the platform-admin write
    API can enforce disable-only on the factory defaults. The ON CONFLICT
    branch is a deliberate DO UPDATE (not DO NOTHING) that touches ONLY
    ``is_seeded``: it backfills the flag on a DB created before the column
    existed, while leaving a platform-admin's config / enabled / priority /
    description edits untouched — those survive every re-seed. Only the
    five shipped identities can conflict here; a PA-created rule has a
    different (tier, check_id, stage) and is never stamped.
    """
    import json

    for check_id, stage, config, priority, description in _PLATFORM_DEFAULT_RULES:
        await conn.execute(
            """
            INSERT INTO platform_safety_rules
                (tier, stage, check_id, config, priority, description, is_seeded)
            VALUES ('default', $1, $2, $3::jsonb, $4, $5, TRUE)
            ON CONFLICT (tier, check_id, stage)
                DO UPDATE SET is_seeded = TRUE
            """,
            stage,
            check_id,
            json.dumps(config),
            priority,
            description,
        )


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
    # ``tenants.approval_default_mode`` was removed with the approval
    # subsystem. Drop the column if an earlier deployment created it.
    await conn.execute(
        "ALTER TABLE tenants DROP COLUMN IF EXISTS approval_default_mode"
    )
    # Tenant lifecycle status (platform-plane provision/suspend). Idempotent
    # ADD so existing rows default to 'active' — behaviour unchanged for any
    # tenant that predates this column. The CHECK pins the only two legal
    # states; a suspended tenant's users fail authentication and its
    # scheduled tasks are skipped (not failed) until resumed.
    await conn.execute(
        "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS status TEXT NOT NULL "
        "DEFAULT 'active' CHECK (status IN ('active','suspended'))"
    )

    # ----- Platform model catalog (v1.1 §2.1) -----------------------------
    # Tenant-agnostic; no RLS. ``is_platform`` is reserved for the v2
    # extension where a tenant can register its own model (then FALSE
    # for those rows). v1 only ships platform-curated entries.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS models (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            provider        VARCHAR(50) NOT NULL,
            model_id        VARCHAR(200) NOT NULL,
            model_family    VARCHAR(50) NOT NULL,
            display_name    VARCHAR(200) NOT NULL,
            is_platform     BOOLEAN NOT NULL DEFAULT TRUE,
            is_active       BOOLEAN NOT NULL DEFAULT TRUE,
            created_at      TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (provider, model_id)
        )
    """)

    # Tenant-scoped LLM credentials. Each ``(tenant, provider)`` row is
    # explicit opt-in state (design "credential pool" §1): no row means
    # the provider is *unconfigured* and an agent on it fails closed.
    # ``credential_mode`` records which key the resolver uses:
    #   * ``'byok'`` — the tenant's own key, held Fernet-encrypted in
    #     ``credential_data`` (the {"api_key": "sk-..."} payload — see
    #     ``rolemesh.auth.credential_vault`` and design §8.1).
    #   * ``'pool'`` — the tenant explicitly elected the platform pool
    #     key (``platform_provider_credentials``). ``credential_data``
    #     may be NULL (never had a BYOK key) or carry a *dormant* BYOK
    #     ciphertext retained across a byok→pool switch so the tenant
    #     can flip back without re-entering it; the resolver ignores it
    #     while mode is ``'pool'``.
    # The CHECK enforces the one illegal combination — a ``'byok'`` row
    # must carry a key, so the resolver can route on mode alone and a
    # byok row never silently falls through to the pool. The CREATE
    # below lands the full shape on a fresh DB; the guarded DO block
    # rewrites a pre-existing dev DB. Both branches are idempotent.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tenant_model_credentials (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            provider         VARCHAR(50) NOT NULL,
            credential_mode  VARCHAR(10) NOT NULL DEFAULT 'byok'
                                 CHECK (credential_mode IN ('pool', 'byok')),
            credential_data  BYTEA,
            created_at       TIMESTAMPTZ DEFAULT NOW(),
            updated_at       TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (tenant_id, provider),
            CONSTRAINT tmc_byok_requires_key
                CHECK (credential_mode <> 'byok' OR credential_data IS NOT NULL)
        )
    """)
    # Migration for a pre-existing dev DB. Four idempotent steps:
    #   1. Drop the legacy ``credential_ref TEXT`` indirection column.
    #      The vault (§8.1) is not back-compat with the old plaintext
    #      pointer — any row under that shape is unrecoverable, so the
    #      rows go with the column and tenants must re-PUT.
    #   2. Add ``credential_data`` if the DB predates the BYTEA column.
    #   3. Add ``credential_mode`` defaulting to ``'byok'`` — every
    #      pre-pool row carries a real key, so the default backfills the
    #      correct mode and behaviour is unchanged.
    #   4. Drop the legacy NOT NULL on ``credential_data`` (pool rows may
    #      have no key) and add the byok-requires-key CHECK once.
    await conn.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'tenant_model_credentials'
                         AND column_name = 'credential_ref') THEN
                DELETE FROM tenant_model_credentials;
                ALTER TABLE tenant_model_credentials DROP COLUMN credential_ref;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'tenant_model_credentials'
                             AND column_name = 'credential_data') THEN
                ALTER TABLE tenant_model_credentials
                    ADD COLUMN credential_data BYTEA;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'tenant_model_credentials'
                             AND column_name = 'credential_mode') THEN
                ALTER TABLE tenant_model_credentials
                    ADD COLUMN credential_mode VARCHAR(10) NOT NULL DEFAULT 'byok';
                ALTER TABLE tenant_model_credentials
                    ADD CONSTRAINT tmc_mode_values
                        CHECK (credential_mode IN ('pool', 'byok'));
            END IF;
            ALTER TABLE tenant_model_credentials
                ALTER COLUMN credential_data DROP NOT NULL;
            IF NOT EXISTS (SELECT 1 FROM information_schema.table_constraints
                           WHERE table_name = 'tenant_model_credentials'
                             AND constraint_name = 'tmc_byok_requires_key') THEN
                ALTER TABLE tenant_model_credentials
                    ADD CONSTRAINT tmc_byok_requires_key
                        CHECK (credential_mode <> 'byok'
                               OR credential_data IS NOT NULL);
            END IF;
        END $$
    """)

    # Platform credential pool (design "credential pool" §2). Tenant-
    # agnostic, no RLS, no ``tenant_id`` — the mirror of ``models`` but
    # holding the Fernet-encrypted platform key per provider. A tenant
    # row with ``credential_mode = 'pool'`` resolves its key from here.
    # Only ``platform_admin`` mutates it (``credential.pool.manage``);
    # tenants never read the ciphertext (metadata-only list endpoints).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS platform_provider_credentials (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            provider         VARCHAR(50) NOT NULL,
            credential_data  BYTEA NOT NULL,
            created_at       TIMESTAMPTZ DEFAULT NOW(),
            updated_at       TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (provider)
        )
    """)

    # MCP server registry. ``tool_reversibility`` is a {tool_name: bool}
    # map; an empty object means "no per-tool override, fall back to
    # tenant default".
    # MCP server registry.
    # ``auth_mode`` carries the 'user' | 'service' | 'both' triple from
    # design §2.1; the API surface requires the caller to pass it
    # explicitly for clarity but the DB default is ``'service'`` so a
    # bypass-RLS INSERT (migrations, smoke scripts) lands on the safest
    # mode (server-managed credentials) rather than ``'user'`` which
    # implies an end-user OIDC token round-trip.
    # ``tool_reversibility`` is a {tool_name: bool} map; an empty
    # object means "no per-tool override, fall back to tenant default".
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS mcp_servers (
            id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id           UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name                VARCHAR(200) NOT NULL,
            type                VARCHAR(50) NOT NULL,
            url                 TEXT NOT NULL,
            auth_mode           VARCHAR(50) NOT NULL DEFAULT 'service',
            extra_headers       JSONB DEFAULT '{}',
            tool_reversibility  JSONB DEFAULT '{}',
            description         TEXT,
            created_at          TIMESTAMPTZ DEFAULT NOW(),
            updated_at          TIMESTAMPTZ DEFAULT NOW(),
            UNIQUE (tenant_id, name)
        )
    """)
    # Idempotent default for a pre-existing dev DB whose CREATE landed
    # before the design said ``auth_mode`` should default.
    await conn.execute(
        "ALTER TABLE mcp_servers ALTER COLUMN auth_mode SET DEFAULT 'service'"
    )
    # The v1.1 design carried a ``credential_ref TEXT`` column intended
    # as an indirection handle into an external secret store. The 02a
    # envelope-encryption pivot retired that pattern for LLM credentials
    # (see the ``tenant_model_credentials.credential_ref`` drop above)
    # but the equivalent drop on ``mcp_servers`` was forgotten. The
    # column never had a runtime consumer and is removed here. If MCP
    # service-mode credentials are wired later, the storage shape
    # deserves an explicit design decision rather than reviving an
    # under-specified ``TEXT`` column.
    await conn.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'mcp_servers'
                         AND column_name = 'credential_ref') THEN
                ALTER TABLE mcp_servers DROP COLUMN credential_ref;
            END IF;
        END $$
    """)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            name TEXT NOT NULL,
            email TEXT,
            role TEXT DEFAULT 'member',
            -- DEPRECATED (v6.1 §P1.2): kept as redundant display field;
            -- linkage / lookup now go through ``user_channel_identities``.
            -- Slated for removal once nothing reads it.
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
            container_config JSONB,
            max_concurrent INT DEFAULT 2,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (tenant_id, folder)
        )
    """)

    # Migrate from roles table if it exists (Step 5 -> merged schema).
    # v1.1 §2.1 retired the inline ``tools`` JSONB column on coworkers;
    # MCP configs now live in the ``coworker_mcp_servers`` junction +
    # ``mcp_servers`` table. The roles migration below no longer
    # backfills ``tools`` because the destination column no longer
    # exists — legacy ``roles.tools`` rows are silently dropped (dev DB
    # only; production schemas never carried this column).
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
            ]:
                await conn.execute(
                    f"ALTER TABLE coworkers ADD COLUMN IF NOT EXISTS {col} "
                    f"TEXT DEFAULT {default}"
                )
            await conn.execute("""
                UPDATE coworkers SET
                    agent_backend = r.agent_backend,
                    system_prompt = r.system_prompt
                FROM roles r WHERE coworkers.role_id = r.id
            """)
            await conn.execute("ALTER TABLE coworkers DROP COLUMN role_id")
        await conn.execute("DROP TABLE IF EXISTS roles CASCADE")
    # Drop the legacy `skills` JSONB column on existing dev databases.
    # The skill system is moving to dedicated `skills` / `skill_files` tables;
    # the old per-coworker JSONB list was never consumed by the runner.
    await conn.execute("ALTER TABLE coworkers DROP COLUMN IF EXISTS skills")
    # v1.1 02b greenfield: drop the legacy ``tools`` JSONB column on
    # any pre-existing dev DB. MCP configs live in the
    # ``coworker_mcp_servers`` junction + ``mcp_servers`` table now;
    # callers must seed mcp_servers and bind via the relation layer.
    # Idempotent — a fresh testcontainer never had the column.
    await conn.execute("ALTER TABLE coworkers DROP COLUMN IF EXISTS tools")
    # Rename legacy backend value: ``claude-code`` was the original name
    # before the Pi integration (commit c032db0) renamed it to ``claude``.
    # The alias was kept for back-compat; this idempotent UPDATE retires
    # it so the alias entry can be removed from BACKEND_CONFIGS.
    await conn.execute(
        "UPDATE coworkers SET agent_backend = 'claude' "
        "WHERE agent_backend = 'claude-code'"
    )
    # --- Auth: flat permission bits on coworkers (least-privilege default) ---
    await conn.execute(
        "ALTER TABLE coworkers ADD COLUMN IF NOT EXISTS permissions JSONB "
        "DEFAULT '{\"task_schedule\":false,"
        "\"task_manage_others\":false,\"agent_delegate\":false}'"
    )
    # v1.1 §2.2: link coworkers to the platform model catalog and the
    # creating user. Both NULLABLE — the model selector is wired in
    # Phase 2 and audit FK (created_by_user_id) is the L6 nullable-on-
    # bootstrap requirement; pre-Phase 2 rows simply have NULL here.
    await conn.execute(
        "ALTER TABLE coworkers "
        "ADD COLUMN IF NOT EXISTS model_id UUID REFERENCES models(id)"
    )
    await conn.execute(
        "ALTER TABLE coworkers "
        "ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id)"
    )
    # feat/roles PR3: per-resource visibility (personal draft space).
    #   'private' — visible only to its creator + role-managers.
    #   'shared'  — visible to every member of the tenant.
    #
    # The two-step ordering is load-bearing for a safe upgrade:
    #   1. ``ADD COLUMN ... NOT NULL DEFAULT 'shared'`` runs the column's
    #      default against EVERY pre-existing row, so legacy coworkers
    #      (including the many with ``created_by_user_id IS NULL``) stay
    #      visible to members exactly as they were before this column —
    #      a 'private' backfill would have made them vanish from every
    #      member's list.
    #   2. ``ALTER COLUMN ... SET DEFAULT 'private'`` flips the default so
    #      that NEW coworkers default to private (the personal-draft
    #      semantics). The flip does not touch existing rows.
    # On a fresh DB step 1 creates the column empty (no rows to backfill)
    # and step 2 still leaves the column private-by-default — both an
    # upgraded and a greenfield DB converge to the same end state.
    await conn.execute(
        "ALTER TABLE coworkers "
        "ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'shared'"
    )
    await conn.execute(
        "ALTER TABLE coworkers ALTER COLUMN visibility SET DEFAULT 'private'"
    )
    await conn.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'coworkers_visibility_check'
                  AND conrelid = 'coworkers'::regclass
            ) THEN
                ALTER TABLE coworkers ADD CONSTRAINT coworkers_visibility_check
                    CHECK (visibility IN ('private', 'shared'));
            END IF;
        END $$
    """)
    # The user-agent assignment table was removed (feat/roles): access is
    # governed by coworker visibility + created_by_user_id ownership, never an
    # explicit per-user grant table. Drop it idempotently so upgraded
    # deployments that already created it shed the table (fresh DBs never
    # create it in the first place).
    await conn.execute("DROP TABLE IF EXISTS user_agent_assignments CASCADE")

    # Coworker <-> MCP server association (v1.1 §2.1). ``enabled_tools``
    # = NULL means "all tools enabled" (the common case); an empty
    # array ``'{}'`` means "all disabled" — semantically distinct, do
    # not default to ``'{}'``. RLS is enforced transitively via
    # ``coworkers.tenant_id`` (see ``_enable_rls_via_parent_coworker``).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS coworker_mcp_servers (
            coworker_id     UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
            mcp_server_id   UUID NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
            enabled_tools   TEXT[] DEFAULT NULL,
            PRIMARY KEY (coworker_id, mcp_server_id)
        )
    """)

    # Skills: per-tenant capability catalog (v1.1 03b greenfield).
    # SKILL.md (frontmatter + body) lives in the row keyed by
    # ``path = 'SKILL.md'`` in ``skill_files``. Frontmatter is split
    # between common (carries name/description for both runtimes) and
    # backend-specific (Claude SDK or Pi loader). See
    # docs/skills-architecture.md. Coworker association is via the
    # ``coworker_skills`` junction table.
    # ``skills.name`` regex tightened to lowercase-kebab (see
    # ``rolemesh.core.skills._SKILL_NAME_RE``) and forbids the two
    # names the Claude runtime reserves (``anthropic``, ``claude``).
    # On a fresh deploy the CREATE TABLE below already uses the new
    # constraint; on an existing dev DB the ALTER block further down
    # swaps the constraint in place.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS skills (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name TEXT NOT NULL CHECK (
                name ~ '^[a-z0-9][a-z0-9-]{0,63}$'
                AND name NOT IN ('anthropic', 'claude')
            ),
            frontmatter_common JSONB NOT NULL DEFAULT '{}',
            frontmatter_backend JSONB NOT NULL DEFAULT '{}',
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL
        )
    """)
    # Idempotent constraint swap for pre-existing dev DBs. The old
    # CHECK constraint had the auto-generated name ``skills_name_check``
    # (Postgres derives this from ``CHECK`` without explicit naming).
    # Drop it if present and add the tightened one. Both branches are
    # guarded so a fresh DB (no old constraint) or an already-migrated
    # DB (new constraint present) is a no-op.
    await conn.execute("""
        DO $$
        BEGIN
            -- Drop the old constraint if its definition still
            -- references the legacy ``[a-zA-Z]`` lead-anchored class
            -- (the only way to discriminate from the new one without
            -- hard-coding constraint-text comparisons).
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'skills_name_check'
                  AND conrelid = 'skills'::regclass
                  AND pg_get_constraintdef(oid) LIKE '%a-zA-Z%'
            ) THEN
                ALTER TABLE skills DROP CONSTRAINT skills_name_check;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'skills_name_check'
                  AND conrelid = 'skills'::regclass
            ) THEN
                ALTER TABLE skills ADD CONSTRAINT skills_name_check
                    CHECK (
                        name ~ '^[a-z0-9][a-z0-9-]{0,63}$'
                        AND name NOT IN ('anthropic', 'claude')
                    );
            END IF;
        END $$
    """)
    # v1.1 §2.2: greenfield rename of ``created_by`` -> ``created_by_user_id``.
    # On a fresh testcontainer the CREATE TABLE above already uses the
    # new name and this DO block is a no-op; on a pre-existing dev DB
    # (created when the column was still ``created_by``) it renames in
    # place. Both branches guarded so re-running the schema is safe.
    await conn.execute("""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                       WHERE table_name = 'skills'
                         AND column_name = 'created_by')
               AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name = 'skills'
                                 AND column_name = 'created_by_user_id') THEN
                ALTER TABLE skills RENAME COLUMN created_by TO created_by_user_id;
            END IF;
        END $$
    """)
    # v1.1 03b greenfield: drop the legacy ``skills.coworker_id`` column
    # plus its companion index and the column-level UNIQUE (coworker_id,
    # name) constraint. On a fresh testcontainer the CREATE TABLE above
    # never defined them; on a pre-existing dev DB the ALTERs run once
    # and become idempotent no-ops on subsequent schema.py invocations.
    # Per-tenant skill identity is now (tenant_id, name) — enforced by
    # ``skills_tenant_name_unique`` below — and coworker association is
    # handled by ``coworker_skills``. Order matters: drop the column
    # last (constraint + index reference it).
    await conn.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'skills_coworker_id_name_key'
            ) THEN
                ALTER TABLE skills DROP CONSTRAINT skills_coworker_id_name_key;
            END IF;
        END $$
    """)
    await conn.execute("DROP INDEX IF EXISTS idx_skills_coworker")
    await conn.execute(
        "ALTER TABLE skills DROP COLUMN IF EXISTS coworker_id"
    )
    # v1.1 §2.2: tenant-unique skill names. Now the sole identity
    # constraint on the table (the old column-level UNIQUE
    # (coworker_id, name) was dropped above as part of the per-tenant
    # catalog cutover). Guarded DO block keeps the ADD CONSTRAINT
    # idempotent across schema.py re-runs.
    await conn.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'skills_tenant_name_unique'
            ) THEN
                ALTER TABLE skills
                    ADD CONSTRAINT skills_tenant_name_unique
                    UNIQUE (tenant_id, name);
            END IF;
        END $$
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_skills_tenant_enabled "
        "ON skills(tenant_id, enabled)"
    )
    # feat/roles PR3: per-skill visibility — same two-step backfill
    # contract as ``coworkers.visibility`` above (existing rows -> shared,
    # new rows -> private). See that block for the ordering rationale.
    await conn.execute(
        "ALTER TABLE skills "
        "ADD COLUMN IF NOT EXISTS visibility TEXT NOT NULL DEFAULT 'shared'"
    )
    await conn.execute(
        "ALTER TABLE skills ALTER COLUMN visibility SET DEFAULT 'private'"
    )
    await conn.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'skills_visibility_check'
                  AND conrelid = 'skills'::regclass
            ) THEN
                ALTER TABLE skills ADD CONSTRAINT skills_visibility_check
                    CHECK (visibility IN ('private', 'shared'));
            END IF;
        END $$
    """)

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

    # v1.1 03b greenfield: the SECURITY DEFINER cross-tenant trigger on
    # ``skills.coworker_id`` is moot now that the column is gone. The
    # equivalent guard for the relation layer is enforced by
    # ``coworker_skills``' RLS (transitive via the parent coworker) plus
    # the ``coworker_skills_check_tenant`` trigger below. Drop the old
    # trigger + function so they don't linger on pre-existing dev DBs.
    await conn.execute(
        "DROP TRIGGER IF EXISTS trg_skills_check_coworker_tenant ON skills"
    )
    await conn.execute(
        "DROP FUNCTION IF EXISTS skills_check_coworker_tenant()"
    )

    # Coworker <-> skill association (v1.1 §2.1, made load-bearing in
    # 03b). The skills catalog is per-tenant; this junction is what
    # binds catalog rows to individual coworkers. ``enabled`` defaults
    # TRUE so the common case (bind + project) is a single INSERT;
    # toggling it lets a coworker disable a tenant-wide skill without
    # touching the catalog row. RLS fires transitively via the parent
    # ``coworkers.tenant_id`` (see _enable_rls_via_parent_coworker).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS coworker_skills (
            coworker_id   UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
            skill_id      UUID NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
            enabled       BOOLEAN NOT NULL DEFAULT TRUE,
            PRIMARY KEY (coworker_id, skill_id)
        )
    """)
    # SECURITY DEFINER trigger: reject inserts/updates where the skill
    # and coworker live in different tenants. Both parents already
    # enforce per-tenant RLS, but the junction sits between them and
    # an admin-role caller (test pool or future bootstrap script) can
    # bypass RLS — without this guard a forged (coworker_A, skill_B)
    # pair could land. ``IS DISTINCT FROM`` handles NULL (foreign-key
    # would catch dangling refs but bot tenants returning NULL still
    # needs explicit rejection).
    await conn.execute("""
        CREATE OR REPLACE FUNCTION coworker_skills_check_tenant()
        RETURNS TRIGGER
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        LANGUAGE plpgsql AS $func$
        DECLARE
            cw_tenant UUID;
            sk_tenant UUID;
        BEGIN
            SELECT tenant_id INTO cw_tenant FROM coworkers WHERE id = NEW.coworker_id;
            SELECT tenant_id INTO sk_tenant FROM skills WHERE id = NEW.skill_id;
            IF cw_tenant IS DISTINCT FROM sk_tenant THEN
                RAISE EXCEPTION
                    'coworker_skills tenant mismatch: coworker % vs skill %',
                    NEW.coworker_id, NEW.skill_id;
            END IF;
            RETURN NEW;
        END
        $func$;
    """)
    await conn.execute(
        "DROP TRIGGER IF EXISTS trg_coworker_skills_check_tenant "
        "ON coworker_skills"
    )
    await conn.execute("""
        CREATE TRIGGER trg_coworker_skills_check_tenant
            BEFORE INSERT OR UPDATE OF coworker_id, skill_id ON coworker_skills
            FOR EACH ROW EXECUTE FUNCTION coworker_skills_check_tenant();
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
    # bind it. Standard in-place tenant_id backfill pattern:
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
    # v6.1 §P1.4: persist the platform-native bot handle (Telegram
    # @username, Slack app name, ...) so the WebUI can construct the
    # ``https://t.me/<bot>?start=<token>`` deep-link without an
    # extra runtime hop to the platform. Populated by the gateway
    # on bot connect (``bot.get_me().username``); nullable because
    # the row is created before the gateway has talked to the
    # platform.
    await conn.execute(
        "ALTER TABLE channel_bindings ADD COLUMN IF NOT EXISTS bot_username TEXT"
    )
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id UUID NOT NULL REFERENCES tenants(id),
            coworker_id UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
            channel_binding_id UUID NOT NULL REFERENCES channel_bindings(id),
            channel_chat_id TEXT NOT NULL,
            name TEXT,
            last_agent_invocation TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now(),
            UNIQUE (channel_binding_id, channel_chat_id)
        )
    """)

    # Auth: add user_id to conversations (nullable — Telegram/Slack groups have no single owner)
    await conn.execute(
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id)"
    )

    # Frontdesk v1.2: parent_conversation_id links a delegation child
    # conversation to its parent user-facing conversation. NULL means
    # "top-level user conversation"; non-NULL means "delegation child".
    # ON DELETE CASCADE so deleting a parent removes its child sub-convs.
    # The partial index keeps the common case (NULL) out of the index.
    await conn.execute(
        "ALTER TABLE conversations ADD COLUMN IF NOT EXISTS "
        "parent_conversation_id UUID NULL "
        "REFERENCES conversations(id) ON DELETE CASCADE"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS conversations_by_parent "
        "ON conversations(parent_conversation_id) "
        "WHERE parent_conversation_id IS NOT NULL"
    )

    # Frontdesk v1.2: is_frontdesk marks a super_agent coworker as the
    # single user-facing entry point. routing_description is a
    # capability card written by domain agents, read by the frontdesk
    # LLM for routing. No CHECK constraint on is_frontdesk vs
    # agent_role — the admin UI enforces "is_frontdesk=TRUE requires
    # super_agent" so the DB stays flexible.
    await conn.execute(
        "ALTER TABLE coworkers ADD COLUMN IF NOT EXISTS "
        "is_frontdesk BOOLEAN DEFAULT FALSE"
    )
    await conn.execute(
        "ALTER TABLE coworkers ADD COLUMN IF NOT EXISTS routing_description TEXT"
    )

    # Frontdesk v1.2: per-delegation audit row. One row per
    # `delegate_to_agent` invocation. status is updated conditionally
    # (WHERE status='running') so a late event cannot overwrite a
    # terminal state. prompt_sha256 is audit-dedup only — it is NOT a
    # PII shield (short prompts SHA-256 to ~identifiable hashes).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS delegations (
            id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id                UUID NOT NULL REFERENCES tenants(id),
            parent_conversation_id   UUID NOT NULL REFERENCES conversations(id),
            child_conversation_id    UUID NOT NULL REFERENCES conversations(id),
            from_coworker_id         UUID NOT NULL REFERENCES coworkers(id),
            target_coworker_id       UUID NOT NULL REFERENCES coworkers(id),
            user_id                  UUID,
            prompt_sha256            TEXT NOT NULL,
            context_mode             TEXT NOT NULL,
            status                   TEXT NOT NULL,
            error_message            TEXT,
            duration_ms              INT,
            started_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
            ended_at                 TIMESTAMPTZ
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS delegations_by_tenant_time "
        "ON delegations(tenant_id, started_at DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS delegations_by_parent_conv "
        "ON delegations(parent_conversation_id, started_at DESC)"
    )

    # Runs (v1.1 §2.1). One row per agent invocation; WS streaming +
    # cancel + scheduled paths all UPDATE ``status / completed_at /
    # usage`` (INV-6, covered by 01b). ``error`` carries structured
    # failure detail when status='failed'. ``awaiting_reauth`` is the
    # user-mode MCP token-vault-stalled state (architecture preserved;
    # not exercised under bootstrap fast-path).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id       UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            status          VARCHAR(20) NOT NULL,
            started_at      TIMESTAMPTZ DEFAULT NOW(),
            completed_at    TIMESTAMPTZ,
            usage           JSONB,
            error           JSONB
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_tenant_conv_started "
        "ON runs (tenant_id, conversation_id, started_at DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_runs_status "
        "ON runs (tenant_id, status) WHERE status = 'running'"
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
                -- ON DELETE CASCADE: a session is the SDK/Pi resume-key
                -- for a specific coworker container; deleting the coworker
                -- makes the session pointer meaningless.
                coworker_id UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
                session_id TEXT NOT NULL
            )
        """)
        # ``conversation_id`` FK uses ON DELETE CASCADE so a DELETE
        # on conversations (design §3 "DELETE semantics") propagates to the
        # message log. Without the cascade a v1 DELETE on a busy
        # conversation would 500 on a FK violation — the schema, not
        # the handler, is the right place to enforce the policy.
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT NOT NULL,
                tenant_id UUID NOT NULL REFERENCES tenants(id),
                conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
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
    # Idempotent migration: upgrade the conversation_id FK to
    # ON DELETE CASCADE on databases created before the v1.1 design
    # locked the cascade in (legacy installs would otherwise 500 on
    # DELETE /api/v1/conversations/{id} with FK violation). The
    # constraint name is asyncpg's auto-generated default — we look
    # it up rather than hard-coding so a schema rebuild that picks a
    # different name doesn't break this migration.
    await conn.execute(
        """
        DO $$
        DECLARE
            cname text;
            current_action text;
        BEGIN
            SELECT con.conname,
                   CASE con.confdeltype
                       WHEN 'a' THEN 'NO ACTION'
                       WHEN 'r' THEN 'RESTRICT'
                       WHEN 'c' THEN 'CASCADE'
                       WHEN 'n' THEN 'SET NULL'
                       WHEN 'd' THEN 'SET DEFAULT'
                   END
              INTO cname, current_action
              FROM pg_constraint con
              JOIN pg_class rel ON rel.oid = con.conrelid
              JOIN pg_class fkrel ON fkrel.oid = con.confrelid
             WHERE rel.relname  = 'messages'
               AND fkrel.relname = 'conversations'
               AND con.contype   = 'f';
            IF cname IS NOT NULL AND current_action <> 'CASCADE' THEN
                EXECUTE 'ALTER TABLE messages DROP CONSTRAINT ' || quote_ident(cname);
                EXECUTE 'ALTER TABLE messages ADD CONSTRAINT ' ||
                        quote_ident(cname) ||
                        ' FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE';
            END IF;
        END$$;
        """
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
    # v1.1 §2.2: associate each message with the run that produced it.
    # NULLABLE — Phase 1 (01a) wires the writer for new agent traffic;
    # legacy rows and external/channel-only messages keep run_id NULL.
    # FK is ON DELETE SET NULL conceptually but PG default is RESTRICT;
    # ``runs`` itself ON DELETE CASCADEs from conversations so a run
    # never outlives its conversation, and orphaned messages would
    # already be gone via the conversations cascade.
    await conn.execute(
        "ALTER TABLE messages ADD COLUMN IF NOT EXISTS run_id "
        "UUID REFERENCES runs(id)"
    )
    if not legacy_exists:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                tenant_id UUID NOT NULL REFERENCES tenants(id),
                -- ON DELETE CASCADE: a scheduled task owns no execution
                -- runtime — when its coworker is deleted there is no
                -- agent to dispatch to. CASCADE keeps the scheduler
                -- queue in sync with the live coworker set.
                coworker_id UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
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

    # --- v6.1 §P1.7: per-task creator id for run-time user_id passthrough ---
    # ON DELETE SET NULL keeps the row for audit; tasks must be soft-
    # cancelled before the user is removed (see ``cancel_tasks_for_user``
    # in db/task.py) so the scheduler's ``status = 'active'`` filter
    # drops them before NULL ever reaches the run path. The SET-NULL +
    # cancel-before-delete pairing is mandatory: NULL alone would
    # leak through to ``AgentInput.user_id``.
    await conn.execute(
        "ALTER TABLE scheduled_tasks "
        "ADD COLUMN IF NOT EXISTS created_by_user_id UUID REFERENCES users(id) ON DELETE SET NULL"
    )

    # --- v6.1 §P1.2: per-(user, platform, channel) identity linkage ---
    # One-shot reset of legacy IM state before the new identity model
    # comes online. Gated on ``user_channel_identities`` not existing
    # yet so the cleanup runs at most once per DB. dev-only data per
    # design §P1.3 ("dev data can be rebuilt"); production deployments
    # will not have unlinked IM conversations to delete.
    await conn.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_name = 'user_channel_identities'
            ) THEN
                DELETE FROM scheduled_tasks WHERE conversation_id IN (
                    SELECT c.id FROM conversations c
                    JOIN channel_bindings cb ON c.channel_binding_id = cb.id
                    WHERE cb.channel_type IN ('telegram', 'slack')
                );
                DELETE FROM conversations WHERE channel_binding_id IN (
                    SELECT id FROM channel_bindings
                    WHERE channel_type IN ('telegram', 'slack')
                );
            END IF;
        END $$
    """)

    # ``user_channel_identities``: which (platform, channel_id) belongs
    # to which user. ``channel_id`` is the platform-native sender id,
    # normalised at the gateway (e.g. ``str(update.effective_user.id)``
    # for Telegram so the format never depends on whether ``from.id``
    # arrived as int vs str). UNIQUE (tenant_id, platform, channel_id)
    # is the race guard: two concurrent ``/start`` requests cannot both
    # link the same Telegram account.
    #
    # Per decision §2 row #13 there is NO ``UNIQUE (user_id, platform)``:
    # one user can intentionally link multiple Telegram accounts (e.g.
    # personal + work numbers).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS user_channel_identities (
            id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            platform    TEXT NOT NULL,
            channel_id  TEXT NOT NULL,
            user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at  TIMESTAMPTZ DEFAULT now(),
            UNIQUE (tenant_id, platform, channel_id)
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_uci_user_platform "
        "ON user_channel_identities(user_id, platform)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_uci_lookup "
        "ON user_channel_identities(tenant_id, platform, channel_id)"
    )

    # ``link_tokens``: short-lived one-shot tokens that prove the user
    # holding the WebUI session also controls the IM account that
    # echoes the token back. Atomic consumption via
    # ``UPDATE ... WHERE used_at IS NULL AND expires_at > now()
    #            RETURNING ...`` (see ``db.channel_identity``); the
    # check-and-mark is one statement so two concurrent ``/start``
    # commands with the same token cannot both succeed.
    #
    # GC is intentionally not implemented (design §P1.2): the row
    # volume is tiny and expired rows are harmless.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS link_tokens (
            token       TEXT PRIMARY KEY,
            user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tenant_id   UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            platform    TEXT NOT NULL,
            expires_at  TIMESTAMPTZ NOT NULL,
            used_at     TIMESTAMPTZ,
            created_at  TIMESTAMPTZ DEFAULT now()
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_link_tokens_expiry "
        "ON link_tokens(expires_at) WHERE used_at IS NULL"
    )

    # --- Legacy v6.1 approval-audit cleanup ---
    # The v6.1 "block-and-replay" approval subsystem was removed; the current
    # block-and-await HITL module re-introduced ``approval_requests`` /
    # ``approval_policies`` (created further below). Those new tables share the
    # old names but NOT the old audit trigger/function/log, so drop only the
    # genuinely-dead v6.1 objects to keep a v6.1-upgraded DB free of orphans.
    #
    # IMPORTANT: do NOT ``DROP TABLE approval_requests`` / ``approval_policies``
    # here. ``_create_schema`` runs on every service start, so an unconditional
    # drop wiped all approval history and policies on each restart (the tables
    # were silently recreated empty below) — a real data-loss regression left
    # over from the removal era. The CREATE TABLE IF NOT EXISTS + ADD COLUMN
    # IF NOT EXISTS migrations below carry the schema forward without a drop.
    await conn.execute(
        "DROP TRIGGER IF EXISTS trg_approval_audit ON approval_requests"
    )
    await conn.execute(
        "DROP FUNCTION IF EXISTS _approval_write_audit_from_trigger()"
    )
    await conn.execute("DROP TABLE IF EXISTS approval_audit_log CASCADE")

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

    # --- Platform-level safety rules (cross-tenant, platform-owned) ---
    # Platform-defined rules that apply across ALL tenants, owned by the
    # platform rather than any tenant admin. Deliberately carries NO
    # tenant_id / coworker_id: a platform rule is cross-tenant and all-
    # coworker by definition. Mirrors the ``models`` catalog shape
    # (platform-level, no RLS). The loader stamps the running job's
    # tenant_id onto each snapshot at load time so the Safety Pipeline —
    # which stays UNTOUCHED — treats platform and tenant rules
    # identically. Access: the business role (rolemesh_app) may SELECT
    # (the tenant-facing read API runs on that pool and must surface
    # visible platform rules) but never write — see the REVOKE in the
    # RLS section below. Writes go through admin_conn only.
    #
    # ``tier`` carries the three-tier model (see docs/15-safety-*):
    #   - floor             : invisible + immutable to tenants
    #   - transparent_floor : visible + immutable
    #   - default           : visible; tenants cannot loosen but may add
    #                         their own stricter tenant rules alongside
    # This phase only seeds ``default`` rows; floor / transparent_floor
    # are carried in the schema for future use.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS platform_safety_rules (
            id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tier            TEXT NOT NULL
                            CHECK (tier IN ('floor', 'transparent_floor', 'default')),
            stage           TEXT NOT NULL,
            check_id        TEXT NOT NULL,
            config          JSONB NOT NULL DEFAULT '{}'::jsonb,
            priority        INTEGER NOT NULL DEFAULT 1000,
            enabled         BOOLEAN NOT NULL DEFAULT TRUE,
            description     TEXT NOT NULL DEFAULT '',
            -- ``is_seeded`` marks the shipped factory defaults (seeded at
            -- build time, insert-if-absent). They are managed disable-only:
            -- the platform-admin write API forbids hard-deleting them
            -- (a delete would be undone by the next seed on a fresh DB),
            -- but config / enabled edits ARE allowed and survive re-seed
            -- (the seed is ON CONFLICT DO NOTHING). PA-created rules carry
            -- FALSE and allow full CRUD.
            is_seeded       BOOLEAN NOT NULL DEFAULT FALSE,
            created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    # Backfill for a pre-existing DB created before the is_seeded column.
    # Idempotent ADD COLUMN IF NOT EXISTS; the column defaults FALSE, then
    # the seed below stamps TRUE on the rows it owns (its UPDATE branch).
    await conn.execute(
        "ALTER TABLE platform_safety_rules "
        "ADD COLUMN IF NOT EXISTS is_seeded BOOLEAN NOT NULL DEFAULT FALSE"
    )
    # Identity for the idempotent seed: one platform rule per
    # (tier, check_id, stage). The seed's ON CONFLICT keys on this.
    await conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_platform_safety_rules_identity "
        "ON platform_safety_rules (tier, check_id, stage)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_platform_safety_rules_active "
        "ON platform_safety_rules (enabled, stage)"
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
            source                TEXT NOT NULL DEFAULT 'tenant',
            created_at            TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    # ``approval_context`` was removed together with the approval
    # subsystem. Drop the column if an earlier deployment created it.
    await conn.execute(
        "ALTER TABLE safety_decisions DROP COLUMN IF EXISTS approval_context"
    )
    # ``source`` ('tenant' | 'platform') lets operators tell platform-rule
    # hits from tenant-rule hits at a glance. Forward-migrate existing
    # deployments. Populated at write time (see db.safety.insert_safety_
    # decision) without touching the pipeline.
    await conn.execute(
        "ALTER TABLE safety_decisions "
        "ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'tenant'"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_safety_decisions_tenant_time "
        "ON safety_decisions (tenant_id, created_at DESC)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_safety_decisions_verdict "
        "ON safety_decisions (tenant_id, verdict_action, created_at DESC)"
    )
    # The check_id / rule_id filters on the decisions list match against
    # the triggered_rule_ids array (``&&`` overlap / ``@>`` contains). A
    # GIN index turns those array predicates from a tenant-wide seq scan
    # into an index probe.
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_safety_decisions_triggered_rules "
        "ON safety_decisions USING gin (triggered_rule_ids)"
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

    # Trigger function: the caller sets
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

    # ----- HITL approval (docs/21-hitl-approval-plan.md §4) ---------------
    # Tenant-scoped policy: which (mcp_server, tool) calls require a human
    # approval, gated by a structured ``condition_expr`` (see §7 / the pure
    # matcher in ``agent_runner.approval.policy``). ``tool_name = '*'`` is a
    # server-wide wildcard. No coworker dimension — policy is tenant-level
    # only (§2 scope redline).
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS approval_policies (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            mcp_server_name  TEXT NOT NULL,
            tool_name        TEXT NOT NULL,
            condition_expr   JSONB NOT NULL DEFAULT '{"always": true}'::jsonb,
            enabled          BOOLEAN NOT NULL DEFAULT TRUE,
            priority         INTEGER NOT NULL DEFAULT 0,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_policies_enabled "
        "ON approval_policies (tenant_id, enabled)"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_policies_lookup "
        "ON approval_policies (tenant_id, mcp_server_name, tool_name)"
    )

    # One row per approval decision request. The DB is authoritative; the
    # orchestrator's in-memory suspend state is only a cache that restart
    # recovery rebuilds from ``status='pending'`` rows (§8). No separate
    # audit-log table — the decision lives on the row (``decided_by`` /
    # ``note`` / ``decided_at``). ``policy_id`` is deliberately NOT a FK:
    # a policy may be deleted while a historical request remains, exactly
    # like ``safety_decisions.triggered_rule_ids``.
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS approval_requests (
            id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            tenant_id        UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            coworker_id      UUID NOT NULL REFERENCES coworkers(id) ON DELETE CASCADE,
            conversation_id  UUID REFERENCES conversations(id) ON DELETE SET NULL,
            policy_id        UUID,
            user_id          UUID REFERENCES users(id) ON DELETE SET NULL,
            job_id           TEXT NOT NULL,
            mcp_server_name  TEXT NOT NULL,
            action           JSONB NOT NULL,
            action_summary   TEXT,
            status           TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'approved', 'rejected',
                                  'expired', 'cancelled')),
            decided_by       UUID REFERENCES users(id) ON DELETE SET NULL,
            note             TEXT,
            requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at       TIMESTAMPTZ NOT NULL,
            decided_at       TIMESTAMPTZ
        )
    """)
    # Partial index: the expiry watcher + restart recovery only ever scan
    # pending rows, so the index stays tiny even as decided rows accumulate.
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_requests_pending "
        "ON approval_requests (expires_at) WHERE status = 'pending'"
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_approval_requests_job "
        "ON approval_requests (job_id)"
    )
    # The agent's free-text "why I'm calling this tool" (nullable). Added after
    # the table shipped, so use the idempotent ADD COLUMN IF NOT EXISTS form for
    # pre-existing dev DBs. Default null; no agent-fill mechanism yet (HITL UI U1).
    await conn.execute(
        "ALTER TABLE approval_requests ADD COLUMN IF NOT EXISTS rationale TEXT"
    )
    # Safety->approval provenance (docs/21-hitl-approval-plan.md §3.10 / §11.4):
    # a JSON {kind, rule_id, check_id, stage} object set when the safety pipeline
    # raises a require_approval verdict at PRE_TOOL_CALL and the hook bridge turns
    # it into a HITL ticket; null for a business-policy approval. Like
    # ``policy_id`` it is deliberately not an FK — the rule/check it names may be
    # edited or deleted while a historical request remains. Added after the table
    # shipped, so use the idempotent ADD COLUMN IF NOT EXISTS form.
    await conn.execute(
        "ALTER TABLE approval_requests ADD COLUMN IF NOT EXISTS triggered_by JSONB"
    )

    # Reference/seed data — see _seed_reference_data.
    await _seed_reference_data(conn)

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
    # platform_safety_rules is a platform-owned catalog. Unlike tenants /
    # external_tenant_map (full REVOKE), the business role KEEPS SELECT —
    # the tenant-facing read API runs on rolemesh_app and must surface
    # visible platform rules within its own pool (webui never uses
    # admin_conn). It must never WRITE: platform rules are managed via
    # admin_conn only. Floor-tier invisibility is enforced in the app-
    # layer projection, not by this grant.
    await conn.execute(
        "REVOKE INSERT, UPDATE, DELETE ON platform_safety_rules FROM rolemesh_app"
    )

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
    await _enable_rls_on(conn, "users")                # D9
    await _enable_rls_on(conn, "oidc_user_tokens")     # D10 (tenant_id backfilled above)
    await _enable_rls_on(conn, "skills")               # skills feature: standard tenant_id scope
    await _enable_rls_on_transitive_skill_files(conn)
    await _enable_rls_on(conn, "eval_runs")            # eval framework
    await _enable_rls_on(conn, "delegations")          # frontdesk v1.2

    # ----- v1.1 §2.1 RLS additions ---------------------------------------
    # New tenant-scoped tables get the standard four-policy treatment.
    # Junction tables that carry no tenant_id of their own (coworker_*
    # pair tables) inherit RLS transitively via ``coworkers.tenant_id``.
    # ``models`` is deliberately NOT enabled — it's a platform-level
    # catalog and rolemesh_app needs unfiltered SELECT to render the
    # model picker. ``platform_provider_credentials`` is likewise NOT
    # enabled — it is platform-scoped (no ``tenant_id``) and only the
    # platform_admin route + the resolver's ``admin_conn`` touch it.
    await _enable_rls_on(conn, "tenant_model_credentials")
    await _enable_rls_on(conn, "mcp_servers")
    await _enable_rls_on(conn, "runs")
    await _enable_rls_via_parent_coworker(conn, "coworker_mcp_servers")
    await _enable_rls_via_parent_coworker(conn, "coworker_skills")

    # ----- HITL approval RLS (§4) -----------------------------------------
    # Both tables carry their own ``tenant_id`` → standard single-predicate
    # ``tenant_id = current_tenant_id()`` treatment.
    await _enable_rls_on(conn, "approval_policies")
    await _enable_rls_on(conn, "approval_requests")


async def _enable_rls_via_parent_coworker(
    conn: asyncpg.pool.PoolConnectionProxy[asyncpg.Record], table: str,
) -> None:
    """RLS for a junction table keyed transitively through ``coworkers.tenant_id``.

    Used for ``coworker_mcp_servers`` / ``coworker_skills`` — pure
    association tables with no ``tenant_id`` column of their own. The
    EXISTS subquery is itself RLS-bound on ``coworkers`` so a
    cross-tenant lookup sees zero parent rows and the join row is
    hidden / write rejected. Mirrors the pattern documented in
    ``_enable_rls_on_transitive_skill_files``.
    """
    await conn.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    await conn.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    parent_check = (
        f"EXISTS (SELECT 1 FROM coworkers "
        f"WHERE coworkers.id = {table}.coworker_id "
        f"AND coworkers.tenant_id = current_tenant_id())"
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

