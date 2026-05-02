"""eval_runs CRUD + RLS isolation tests.

Two tenants are wired up; each writes one eval run; assertions confirm
tenant_conn-bound reads only see their own tenant's row, ON DELETE
SET NULL correctly preserves the run row when the underlying coworker
is removed, and the canonical hash field round-trips.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import asyncpg
import pytest

from rolemesh.db import (
    _get_pool,
    create_coworker,
    create_tenant,
    delete_coworker,
)
from rolemesh.evaluation.store import (
    create_eval_run,
    finalize_eval_run,
    get_eval_run,
    list_eval_runs,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


pytestmark = pytest.mark.usefixtures("test_db")


@pytest.fixture
async def app_pool(pg_url: str) -> AsyncGenerator[asyncpg.Pool[asyncpg.Record], None]:
    """rolemesh_app pool — NOBYPASSRLS, so RLS actually applies."""
    superuser_pool = _get_pool()
    async with superuser_pool.acquire() as conn:
        await conn.execute("ALTER USER rolemesh_app PASSWORD 'test'")
    rewritten = pg_url.replace("test:test@", "rolemesh_app:test@", 1)
    pool = await asyncpg.create_pool(rewritten, min_size=1, max_size=2)
    try:
        yield pool
    finally:
        await pool.close()


def _sample_config() -> dict:
    return {"system_prompt": "you are a tester", "tools": [], "skills": []}


async def test_create_then_get_round_trip() -> None:
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    run = await create_eval_run(
        tenant_id=t.id,
        coworker_id=cw.id,
        coworker_config=_sample_config(),
        coworker_config_sha256="deadbeef",
        dataset_path="/tmp/data.jsonl",
        dataset_sha256="cafef00d",
    )
    assert run.status == "running"
    assert run.coworker_config == _sample_config()

    fetched = await get_eval_run(run.id, tenant_id=t.id)
    assert fetched is not None
    assert fetched.id == run.id
    assert fetched.coworker_config_sha256 == "deadbeef"
    assert fetched.dataset_sha256 == "cafef00d"


async def test_finalize_sets_status_metrics_and_finished_at() -> None:
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    run = await create_eval_run(
        tenant_id=t.id, coworker_id=cw.id,
        coworker_config=_sample_config(),
        coworker_config_sha256="x", dataset_path="/d.jsonl",
        dataset_sha256="y",
    )
    metrics = {"sample_count": 3, "scorers": {"final_answer_scorer": {"accuracy": 0.66}}}
    finalized = await finalize_eval_run(
        run.id, tenant_id=t.id, status="completed",
        metrics=metrics, eval_log_uri="/tmp/logs/run.eval",
    )
    assert finalized is not None
    assert finalized.status == "completed"
    assert finalized.metrics == metrics
    assert finalized.eval_log_uri == "/tmp/logs/run.eval"
    assert finalized.finished_at is not None


async def test_finalize_rejects_invalid_status() -> None:
    """Mutation guard: ensure status whitelist actually filters —
    otherwise a typo (``"complete"``) silently lands in DB."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    run = await create_eval_run(
        tenant_id=t.id, coworker_id=cw.id,
        coworker_config={}, coworker_config_sha256="x",
        dataset_path="/d.jsonl", dataset_sha256="y",
    )
    with pytest.raises(ValueError):
        await finalize_eval_run(
            run.id, tenant_id=t.id, status="complete",  # typo
            metrics=None, eval_log_uri=None,
        )


async def test_list_orders_by_started_at_desc() -> None:
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    runs = []
    for _ in range(3):
        runs.append(
            await create_eval_run(
                tenant_id=t.id, coworker_id=cw.id,
                coworker_config={}, coworker_config_sha256="x",
                dataset_path="/d.jsonl", dataset_sha256="y",
            )
        )
    listed = await list_eval_runs(tenant_id=t.id, limit=10)
    # Most recent first — id of the LAST created should appear first.
    assert listed[0].id == runs[-1].id


async def test_list_filters_by_coworker() -> None:
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw1 = await create_coworker(
        tenant_id=t.id, name="cw1", folder=f"cw1-{uuid.uuid4().hex[:6]}",
    )
    cw2 = await create_coworker(
        tenant_id=t.id, name="cw2", folder=f"cw2-{uuid.uuid4().hex[:6]}",
    )
    await create_eval_run(
        tenant_id=t.id, coworker_id=cw1.id,
        coworker_config={}, coworker_config_sha256="a",
        dataset_path="/d.jsonl", dataset_sha256="x",
    )
    await create_eval_run(
        tenant_id=t.id, coworker_id=cw2.id,
        coworker_config={}, coworker_config_sha256="b",
        dataset_path="/d.jsonl", dataset_sha256="x",
    )
    only_cw1 = await list_eval_runs(tenant_id=t.id, coworker_id=cw1.id)
    assert len(only_cw1) == 1
    assert only_cw1[0].coworker_id == cw1.id


async def test_coworker_delete_preserves_run_row() -> None:
    """ON DELETE SET NULL contract: deleting a Coworker keeps the
    audit trail of past runs but nulls the FK so list/show stays
    queryable."""
    t = await create_tenant(name="T", slug=f"t-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=t.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    run = await create_eval_run(
        tenant_id=t.id, coworker_id=cw.id,
        coworker_config={}, coworker_config_sha256="x",
        dataset_path="/d.jsonl", dataset_sha256="y",
    )
    deleted = await delete_coworker(cw.id, tenant_id=t.id)
    assert deleted is True

    fetched = await get_eval_run(run.id, tenant_id=t.id)
    assert fetched is not None
    assert fetched.coworker_id is None
    # Config snapshot still present so the old run remains
    # interpretable.
    assert fetched.coworker_config_sha256 == "x"


async def test_rls_blocks_cross_tenant_eval_run_select(
    app_pool: asyncpg.Pool[asyncpg.Record],
) -> None:
    """Tenant A creates an eval_run; tenant B's GUC reading must see
    zero rows even when its WHERE clause names tenant A's id."""
    ta = await create_tenant(name="TA", slug=f"ta-{uuid.uuid4().hex[:6]}")
    tb = await create_tenant(name="TB", slug=f"tb-{uuid.uuid4().hex[:6]}")
    cw = await create_coworker(
        tenant_id=ta.id, name="cw", folder=f"cw-{uuid.uuid4().hex[:6]}",
    )
    run = await create_eval_run(
        tenant_id=ta.id, coworker_id=cw.id,
        coworker_config={}, coworker_config_sha256="x",
        dataset_path="/d.jsonl", dataset_sha256="y",
    )

    async with app_pool.acquire() as conn, conn.transaction():
        await conn.execute(
            "SELECT set_config('app.current_tenant_id', $1, true)",
            tb.id,
        )
        rows = await conn.fetch(
            "SELECT * FROM eval_runs WHERE id = $1::uuid",
            run.id,
        )
    assert rows == [], (
        "RLS leak: tenant B saw tenant A's eval_run row"
    )
