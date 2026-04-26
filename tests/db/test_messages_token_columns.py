"""DB schema + store_message tests for the token-usage columns.

Tests run against a real postgres testcontainer (see conftest.test_db
fixture). Subset of the test surface focuses on:

- column presence + nullability + types — wrong types would silently
  truncate (e.g. INTEGER for numbers >2^31, FLOAT instead of NUMERIC
  losing precision over sums)
- idempotent migration — a re-run of _create_schema must not throw
- INSERT round-trip across the populated and legacy code paths
- DECIMAL precision of cost_usd — the smallest value the Claude SDK
  reports survives the column type intact
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from rolemesh.db.pg import (
    _create_schema,
    _get_pool,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    store_message,
)

pytestmark = pytest.mark.usefixtures("test_db")


async def _create_chain() -> tuple[str, str]:
    t = await create_tenant(name="UsageCorp", slug=f"usage-{uuid.uuid4().hex[:8]}")
    cw = await create_coworker(
        tenant_id=t.id, name="Bot", folder=f"bot-{uuid.uuid4().hex[:8]}"
    )
    b = await create_channel_binding(
        coworker_id=cw.id,
        tenant_id=t.id,
        channel_type="telegram",
        credentials={"bot_token": "x"},
    )
    conv = await create_conversation(
        tenant_id=t.id,
        coworker_id=cw.id,
        channel_binding_id=b.id,
        channel_chat_id="ch-1",
    )
    return t.id, conv.id


class TestSchema:
    async def test_six_token_columns_exist_and_are_nullable(self) -> None:
        """The DB-shape contract: six columns, all nullable. NOT NULL
        anywhere here would break legacy callers (every existing user
        message row has NULL tokens, and an enforced NOT NULL with no
        default would crash inserts)."""
        pool = _get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT column_name, data_type, is_nullable, numeric_precision, numeric_scale
                FROM information_schema.columns
                WHERE table_name = 'messages' AND column_name IN (
                    'input_tokens', 'output_tokens',
                    'cache_read_tokens', 'cache_write_tokens',
                    'cost_usd', 'model_id'
                )
                """
            )
        cols = {r["column_name"]: dict(r) for r in rows}
        assert set(cols.keys()) == {
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "cost_usd",
            "model_id",
        }
        for name, meta in cols.items():
            assert meta["is_nullable"] == "YES", f"{name} must be nullable"

    async def test_token_columns_are_integer(self) -> None:
        """Wrong type (e.g. SMALLINT) silently truncates Claude turns
        that exceed 32k tokens — common with cache-hit workloads."""
        pool = _get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_name = 'messages' AND column_name IN (
                    'input_tokens', 'output_tokens',
                    'cache_read_tokens', 'cache_write_tokens'
                )
                """
            )
        for row in rows:
            assert row["data_type"] == "integer", (
                f"{row['column_name']} should be INTEGER, got {row['data_type']}"
            )

    async def test_cost_usd_is_numeric_with_precision(self) -> None:
        """NUMERIC(10,6) — 6-digit decimal precision matches the smallest
        value Claude SDK reports. FLOAT would drift on aggregation."""
        pool = _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT data_type, numeric_precision, numeric_scale
                FROM information_schema.columns
                WHERE table_name = 'messages' AND column_name = 'cost_usd'
                """
            )
        assert row is not None
        assert row["data_type"] == "numeric"
        assert row["numeric_precision"] == 10
        assert row["numeric_scale"] == 6

    async def test_model_id_is_text(self) -> None:
        pool = _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT data_type FROM information_schema.columns
                WHERE table_name = 'messages' AND column_name = 'model_id'
                """
            )
        assert row is not None
        assert row["data_type"] == "text"

    async def test_migration_is_idempotent(self) -> None:
        """A re-run of _create_schema must not raise. ADD COLUMN IF NOT
        EXISTS is the contract — if a future change drops the
        IF NOT EXISTS, this test fires."""
        pool = _get_pool()
        async with pool.acquire() as conn:
            await _create_schema(conn)
            # And again for good measure — no second-run errors.
            await _create_schema(conn)


class TestRoundTrip:
    async def test_insert_with_all_token_fields(self) -> None:
        tid, convid = await _create_chain()
        await store_message(
            tenant_id=tid,
            conversation_id=convid,
            msg_id="m-with-usage",
            sender="bot",
            sender_name="Bot",
            content="hello",
            timestamp="2024-06-01T12:00:00+00:00",
            is_from_me=True,
            is_bot_message=True,
            input_tokens=1500,
            output_tokens=320,
            cache_read_tokens=400,
            cache_write_tokens=120,
            cost_usd=0.012345,
            model_id="claude-sonnet-4-6",
        )
        pool = _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT input_tokens, output_tokens, cache_read_tokens,
                       cache_write_tokens, cost_usd, model_id
                FROM messages
                WHERE tenant_id = $1::uuid
                  AND conversation_id = $2::uuid
                  AND id = $3
                """,
                tid,
                convid,
                "m-with-usage",
            )
        assert row is not None
        assert row["input_tokens"] == 1500
        assert row["output_tokens"] == 320
        assert row["cache_read_tokens"] == 400
        assert row["cache_write_tokens"] == 120
        # asyncpg returns NUMERIC as Decimal — must compare with Decimal,
        # not float, otherwise we'd be testing the precision of float
        # equality rather than the column.
        assert row["cost_usd"] == Decimal("0.012345")
        assert row["model_id"] == "claude-sonnet-4-6"

    async def test_legacy_insert_leaves_columns_null(self) -> None:
        """Legacy callers that don't pass token kwargs must continue to
        work — no positional regression. All six columns end up NULL,
        which is the correct value for "no usage data captured"."""
        tid, convid = await _create_chain()
        await store_message(
            tenant_id=tid,
            conversation_id=convid,
            msg_id="m-legacy",
            sender="user",
            sender_name="Alice",
            content="message from a user",
            timestamp="2024-06-01T12:01:00+00:00",
        )
        pool = _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT input_tokens, output_tokens, cache_read_tokens,
                       cache_write_tokens, cost_usd, model_id
                FROM messages
                WHERE tenant_id = $1::uuid
                  AND conversation_id = $2::uuid
                  AND id = $3
                """,
                tid,
                convid,
                "m-legacy",
            )
        assert row is not None
        assert row["input_tokens"] is None
        assert row["output_tokens"] is None
        assert row["cache_read_tokens"] is None
        assert row["cache_write_tokens"] is None
        assert row["cost_usd"] is None
        assert row["model_id"] is None

    async def test_partial_token_population(self) -> None:
        """Pi backend reports token counts but cost_usd=None — the row
        should land with tokens populated and cost NULL."""
        tid, convid = await _create_chain()
        await store_message(
            tenant_id=tid,
            conversation_id=convid,
            msg_id="m-pi",
            sender="bot",
            sender_name="Bot",
            content="pi reply",
            timestamp="2024-06-01T12:02:00+00:00",
            is_from_me=True,
            is_bot_message=True,
            input_tokens=200,
            output_tokens=80,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=None,
            model_id="claude-sonnet-4-6",
        )
        pool = _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT input_tokens, cost_usd, model_id FROM messages
                WHERE tenant_id = $1::uuid AND conversation_id = $2::uuid AND id = $3
                """,
                tid,
                convid,
                "m-pi",
            )
        assert row is not None
        assert row["input_tokens"] == 200
        assert row["cost_usd"] is None
        assert row["model_id"] == "claude-sonnet-4-6"

    async def test_zero_tokens_and_zero_cost_are_distinct_from_null(self) -> None:
        """A backend that reports literally zero tokens (cached prompt,
        no real LLM call) must produce 0 in the column, not NULL.
        Aggregations like ``SUM(input_tokens)`` over a tenant must
        include zero rows in the count."""
        tid, convid = await _create_chain()
        await store_message(
            tenant_id=tid,
            conversation_id=convid,
            msg_id="m-zero",
            sender="bot",
            sender_name="Bot",
            content="zero",
            timestamp="2024-06-01T12:03:00+00:00",
            is_from_me=True,
            is_bot_message=True,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=0.0,
            model_id="claude-sonnet-4-6",
        )
        pool = _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT input_tokens, cost_usd FROM messages
                WHERE tenant_id = $1::uuid AND conversation_id = $2::uuid AND id = $3
                """,
                tid,
                convid,
                "m-zero",
            )
        assert row is not None
        assert row["input_tokens"] == 0
        assert row["cost_usd"] == Decimal("0")

    async def test_smallest_cost_increment_round_trips(self) -> None:
        """The Claude SDK reports cost down to ~$0.000001 ($1 / 1M
        tokens for cheap cache reads). NUMERIC(10,6) must hold this
        without rounding to zero."""
        tid, convid = await _create_chain()
        await store_message(
            tenant_id=tid,
            conversation_id=convid,
            msg_id="m-tiny",
            sender="bot",
            sender_name="Bot",
            content="tiny cost",
            timestamp="2024-06-01T12:04:00+00:00",
            is_from_me=True,
            is_bot_message=True,
            cost_usd=0.000001,
        )
        pool = _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT cost_usd FROM messages
                WHERE tenant_id = $1::uuid AND conversation_id = $2::uuid AND id = $3
                """,
                tid,
                convid,
                "m-tiny",
            )
        assert row is not None
        assert row["cost_usd"] == Decimal("0.000001")
        assert row["cost_usd"] != 0


class TestUpsertPreservesUsage:
    async def test_re_store_does_not_blank_usage(self) -> None:
        """ON CONFLICT path updates content/timestamp only — re-storing
        the same message id with no usage kwargs MUST NOT zero out the
        usage that an earlier write recorded. The hazard: a retry on
        the inbound channel write path would otherwise destroy the
        earlier assistant write's tokens."""
        tid, convid = await _create_chain()
        # First write: full usage data.
        await store_message(
            tenant_id=tid,
            conversation_id=convid,
            msg_id="m-upsert",
            sender="bot",
            sender_name="Bot",
            content="first write",
            timestamp="2024-06-01T12:05:00+00:00",
            is_from_me=True,
            is_bot_message=True,
            input_tokens=500,
            output_tokens=100,
            cost_usd=0.005,
            model_id="claude-sonnet-4-6",
        )
        # Second write: no usage kwargs (legacy retry). Updates content
        # only thanks to the ON CONFLICT clause.
        await store_message(
            tenant_id=tid,
            conversation_id=convid,
            msg_id="m-upsert",
            sender="bot",
            sender_name="Bot",
            content="second write — same id",
            timestamp="2024-06-01T12:05:00+00:00",
            is_from_me=True,
            is_bot_message=True,
        )
        pool = _get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT content, input_tokens, output_tokens, cost_usd, model_id
                FROM messages
                WHERE tenant_id = $1::uuid AND conversation_id = $2::uuid AND id = $3
                """,
                tid,
                convid,
                "m-upsert",
            )
        assert row is not None
        # Content updated...
        assert row["content"] == "second write — same id"
        # ...but usage preserved by the ON CONFLICT not touching those
        # columns. This is the key invariant the docstring promises.
        assert row["input_tokens"] == 500
        assert row["output_tokens"] == 100
        assert row["cost_usd"] == Decimal("0.005")
        assert row["model_id"] == "claude-sonnet-4-6"
