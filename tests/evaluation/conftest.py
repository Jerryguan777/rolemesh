"""Shared fixtures for evaluation tests.

The eval module references ``skills`` / ``skill_files`` schemas that
are introduced in the ``feat/skills`` branch (PR 1: schema, PR 2: per-spawn
projection). Until that lands on main, this conftest mirrors PR 1's exact
DDL so freeze tests run against the same constraints production will apply
— a looser fixture would let the freeze code silently produce values the
real schema would reject (bad name regex, NULL frontmatter, etc.).

Once ``feat/skills`` merges and ``_create_schema`` provisions these tables
unconditionally, this fixture should be deleted and tests rebound to
``test_db`` directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


@pytest.fixture
async def skills_schema(test_db: None) -> AsyncGenerator[None, None]:
    """Create skills + skill_files matching ``feat/skills`` PR 1 DDL exactly.

    Includes the path/name CHECK regex, NOT NULL on JSONB frontmatters,
    and the ``skills_check_coworker_tenant`` SECURITY DEFINER trigger.
    Standard tenant-scope RLS on ``skills``; transitive parent-EXISTS
    RLS on ``skill_files``.
    """
    from rolemesh.db.pg import admin_conn
    async with admin_conn() as conn:
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
        # Standard tenant-scope RLS on skills
        await conn.execute("ALTER TABLE skills ENABLE ROW LEVEL SECURITY")
        await conn.execute("ALTER TABLE skills FORCE ROW LEVEL SECURITY")
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
            await conn.execute(f"DROP POLICY IF EXISTS {policy} ON skills")
            await conn.execute(f"CREATE POLICY {policy} ON skills FOR {op} {body}")
        # Transitive RLS on skill_files
        await conn.execute("ALTER TABLE skill_files ENABLE ROW LEVEL SECURITY")
        await conn.execute("ALTER TABLE skill_files FORCE ROW LEVEL SECURITY")
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
            await conn.execute(f"DROP POLICY IF EXISTS {policy} ON skill_files")
            await conn.execute(
                f"CREATE POLICY {policy} ON skill_files FOR {op} {body}"
            )
    yield
    async with admin_conn() as conn:
        await conn.execute("DROP TABLE IF EXISTS skill_files CASCADE")
        await conn.execute("DROP TABLE IF EXISTS skills CASCADE")
        await conn.execute("DROP FUNCTION IF EXISTS skills_check_coworker_tenant() CASCADE")
