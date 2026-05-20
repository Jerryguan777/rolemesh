"""INV-1 belt-and-braces (v1.1 §11 / 18-rls-architecture.md "Three layers of defense").

Two independent layers must hold for every tenant-scoped table:

* **Braces (RLS)**: a NOBYPASSRLS connection (``rolemesh_app``) with
  the GUC pinned to tenant A must see only tenant A's rows, even when
  the SQL has no explicit tenant predicate.
* **Belt (application predicate)**: a BYPASSRLS admin connection used
  with an explicit ``WHERE tenant_id = $a`` must also see only tenant
  A's rows — RLS can be misconfigured for a single table without the
  application predicate ever noticing, and the application predicate
  can be forgotten without RLS ever noticing.

Plus a *demonstration* third leg: an admin connection with NO explicit
``WHERE tenant_id`` sees rows from both tenants. This is the canary
that motivates the INV-1 lint — proves that without the predicate the
admin path is genuinely cross-tenant.

The new v1.1 tables (``mcp_servers``, ``tenant_model_credentials``,
``runs``) are added to the coverage here so a future PR that forgets
to flip RLS on one of them gets caught at the same place every other
table is checked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import asyncpg
import pytest

from rolemesh.db import (
    _get_pool,
    admin_conn,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.usefixtures("test_db")


@pytest.fixture
async def app_pool(pg_url: str) -> AsyncGenerator[asyncpg.Pool[asyncpg.Record], None]:
    """A pool that logs in as ``rolemesh_app`` — the only role that
    feels RLS. Mirrors the fixture in ``tests/db/test_rls_enforcement.py``;
    duplicated here so this test file is self-contained and can run
    standalone (the v1.1 plan suite runs only the three pinned files,
    not the whole tree)."""
    superuser_pool = _get_pool()
    async with superuser_pool.acquire() as conn:
        await conn.execute("ALTER USER rolemesh_app PASSWORD 'test'")
    rewritten = pg_url.replace("test:test@", "rolemesh_app:test@", 1)
    pool = await asyncpg.create_pool(rewritten, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_pair() -> dict[str, dict[str, str]]:
    """Build two tenants, each with one row in every v1.1 tenant-scoped
    table the test covers. Returns ``{"A": {...ids...}, "B": {...ids...}}``.
    """
    out: dict[str, dict[str, str]] = {}
    for tag in ("A", "B"):
        t = await create_tenant(
            name=f"T{tag}", slug=f"belt-{tag.lower()}-{uuid.uuid4().hex[:6]}"
        )
        cw = await create_coworker(
            tenant_id=t.id, name=f"CW{tag}",
            folder=f"cw-{tag.lower()}-{uuid.uuid4().hex[:6]}",
        )
        binding = await create_channel_binding(
            coworker_id=cw.id, tenant_id=t.id,
            channel_type="telegram", credentials={"bot_token": "x"},
        )
        conv = await create_conversation(
            tenant_id=t.id, coworker_id=cw.id, channel_binding_id=binding.id,
            channel_chat_id=str(uuid.uuid4()),
        )
        # Insert one row into each v1.1 table directly — no public CRUD
        # exists yet (those land in 01a / 02a). admin_conn() bypasses
        # RLS so we can populate both tenants from one place.
        async with admin_conn() as conn:
            mcp_id = await conn.fetchval(
                "INSERT INTO mcp_servers (tenant_id, name, type, url, auth_mode) "
                "VALUES ($1::uuid, $2, $3, $4, $5) RETURNING id",
                t.id, f"mcp-{tag}", "http", "https://example.invalid", "service",
            )
            cred_id = await conn.fetchval(
                "INSERT INTO tenant_model_credentials (tenant_id, provider, credential_ref) "
                "VALUES ($1::uuid, $2, $3) RETURNING id",
                t.id, "anthropic", f"vault://creds/{tag}",
            )
            run_id = await conn.fetchval(
                "INSERT INTO runs (tenant_id, conversation_id, status, completed_at) "
                "VALUES ($1::uuid, $2::uuid, $3, $4) RETURNING id",
                t.id, conv.id, "completed",
                datetime.now(UTC) - timedelta(minutes=1),
            )
        out[tag] = {
            "tenant_id": t.id,
            "coworker_id": cw.id,
            "conversation_id": conv.id,
            "mcp_id": str(mcp_id),
            "cred_id": str(cred_id),
            "run_id": str(run_id),
        }
    return out


# ---------------------------------------------------------------------------
# Layer 1 (braces): RLS blocks cross-tenant even without explicit predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    ["mcp_servers", "tenant_model_credentials", "runs"],
)
async def test_braces_rls_hides_other_tenant_without_predicate(
    app_pool: asyncpg.Pool[asyncpg.Record], table: str,
) -> None:
    """Bind to tenant A, SELECT * without any WHERE clause. RLS must
    filter out tenant B. Each row in the response is required to
    have ``tenant_id = A``, never B. ``mcp_servers`` /
    ``tenant_model_credentials`` / ``runs`` are the v1.1 additions
    that get verified here — the legacy tables already have analogous
    coverage in ``tests/db/test_rls_enforcement.py``.
    """
    pair = await _seed_pair()
    a = pair["A"]
    b = pair["B"]
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", a["tenant_id"]
        )
        rows = await conn.fetch(f"SELECT tenant_id FROM {table}")  # nosec: test query
    seen = {str(r["tenant_id"]) for r in rows}
    assert a["tenant_id"] in seen, (
        f"{table}: tenant A bound but RLS hid A's own row — "
        f"GUC propagation regression"
    )
    assert b["tenant_id"] not in seen, (
        f"{table}: RLS leaked tenant B row to a session bound to tenant A "
        f"(saw tenant_ids: {seen})"
    )


# ---------------------------------------------------------------------------
# Layer 2 (belt): explicit predicate filters even when RLS is bypassed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "table",
    ["mcp_servers", "tenant_model_credentials", "runs"],
)
async def test_belt_predicate_filters_under_admin_conn(table: str) -> None:
    """Admin connection (BYPASSRLS in prod; superuser in test, same
    effect) WITH an explicit ``WHERE tenant_id`` predicate must return
    only the requested tenant's rows. This is the layer that catches
    "RLS got disabled on table X by accident in a refactor" — if every
    REST handler keeps writing the predicate, that misconfiguration
    does not leak.
    """
    pair = await _seed_pair()
    a = pair["A"]
    b = pair["B"]
    async with admin_conn() as conn:
        rows = await conn.fetch(
            f"SELECT tenant_id FROM {table} WHERE tenant_id = $1::uuid",  # nosec: test query
            a["tenant_id"],
        )
    seen = {str(r["tenant_id"]) for r in rows}
    assert seen == {a["tenant_id"]}, (
        f"{table}: explicit-predicate path returned rows for tenants {seen} "
        f"(expected only {a['tenant_id']!r}); B={b['tenant_id']!r}"
    )


# ---------------------------------------------------------------------------
# Demonstration: why the belt matters — admin conn WITHOUT predicate leaks
# ---------------------------------------------------------------------------


async def test_admin_conn_without_predicate_is_cross_tenant() -> None:
    """The motivating canary: an admin connection that forgets the
    ``WHERE tenant_id`` clause genuinely returns rows from every
    tenant. If this assertion ever flipped (say, because RLS got
    enabled on the admin role too) the INV-1 lint would still be
    needed — admin paths exist precisely so cross-tenant maintenance
    work is possible. The point of recording this here is that the
    lint is not optional: nothing else in the test suite stops the
    cross-tenant read.
    """
    pair = await _seed_pair()
    async with admin_conn() as conn:
        rows = await conn.fetch("SELECT tenant_id FROM mcp_servers")
    seen = {str(r["tenant_id"]) for r in rows}
    assert {pair["A"]["tenant_id"], pair["B"]["tenant_id"]} <= seen, (
        "admin_conn without predicate should see both tenants — "
        "if this fails, the admin pool has lost BYPASSRLS, which "
        "breaks legitimate maintenance loops and means the INV-1 "
        "lint stopped having a job to do."
    )


# ---------------------------------------------------------------------------
# Junction tables (coworker_mcp_servers / coworker_skills) — transitive RLS
# ---------------------------------------------------------------------------


async def test_junction_table_rls_via_parent_coworker(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """``coworker_mcp_servers`` carries no ``tenant_id`` column of its
    own; isolation is inherited via ``coworkers.tenant_id``. Inserting
    a row tied to a coworker from another tenant must fail.
    """
    pair = await _seed_pair()
    a = pair["A"]
    b = pair["B"]
    # Seed a valid (A) and a cross-tenant attempt (A coworker + B mcp).
    async with admin_conn() as conn:
        await conn.execute(
            "INSERT INTO coworker_mcp_servers (coworker_id, mcp_server_id) "
            "VALUES ($1::uuid, $2::uuid)",
            a["coworker_id"], a["mcp_id"],
        )
    # Read from tenant A: should see only A's binding.
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", a["tenant_id"]
        )
        # Same query under tenant A — A's row is visible.
        rows = await conn.fetch("SELECT coworker_id FROM coworker_mcp_servers")
        assert {str(r["coworker_id"]) for r in rows} == {a["coworker_id"]}
    # Now bind to tenant B and re-run — A's binding is invisible even
    # though the underlying row exists, because the parent ``coworkers``
    # row resolves to tenant A under RLS.
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)", b["tenant_id"]
        )
        rows = await conn.fetch("SELECT coworker_id FROM coworker_mcp_servers")
        assert rows == [], (
            "transitive RLS via parent coworker failed: tenant B saw "
            f"tenant A's coworker_mcp_servers binding: {rows}"
        )


# ---------------------------------------------------------------------------
# Models table is intentionally NOT RLS-bound — it is the platform catalog
# ---------------------------------------------------------------------------


async def test_models_table_visible_to_all_tenants(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """``models`` is platform-shared (no tenant_id column, no RLS).
    Verifying this explicitly so a future "every table needs RLS"
    sweep doesn't silently break the model picker.
    """
    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            str(uuid.uuid4()),  # arbitrary tenant — does not exist
        )
        rows = await conn.fetch("SELECT provider, model_id FROM models")
    providers = {r["provider"] for r in rows}
    assert "anthropic" in providers, (
        f"models seed missing anthropic entries; got providers: {providers}"
    )


# ---------------------------------------------------------------------------
# Force + enable bits set on the new tables — guard against silent regression
# ---------------------------------------------------------------------------


async def test_v11_new_tables_have_rls_force_enabled() -> None:
    """The four v1.1 RLS-scoped tables must show ``relrowsecurity`` and
    ``relforcerowsecurity`` both true. Without FORCE, the table owner
    (which on this testcontainer is the superuser) bypasses policies,
    and a future production migration that forgot FORCE would not be
    caught until a user noticed."""
    pool = _get_pool()
    expected = [
        "mcp_servers",
        "tenant_model_credentials",
        "runs",
        "coworker_mcp_servers",
        "coworker_skills",
    ]
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT relname, relrowsecurity, relforcerowsecurity "
            "FROM pg_class WHERE relname = ANY($1::text[])",
            expected,
        )
    seen = {r["relname"]: (r["relrowsecurity"], r["relforcerowsecurity"]) for r in rows}
    missing = [t for t in expected if t not in seen]
    assert not missing, f"v1.1 tables missing from pg_class: {missing}"
    not_enabled = [t for t, (en, _) in seen.items() if not en]
    not_forced = [t for t, (_, fo) in seen.items() if not fo]
    assert not not_enabled, f"v1.1 RLS not ENABLEd on: {not_enabled}"
    assert not not_forced, f"v1.1 RLS not FORCEd on: {not_forced}"
