"""In-process e2e tests for the token + cost persistence pipeline.

These cover the seam unit tests can't see: the path from a
``BackendEvent.usage`` snapshot all the way to a row in the ``messages``
table, exercising every link in real form (real JSON, real DB) without
spinning up Docker, NATS, or an LLM.

The seam diagram:

    UsageSnapshot.to_metadata()         (agent_runner.backend)
            │
            ▼
    ContainerOutput(metadata={"usage": …}).to_dict()   (agent_runner.main)
            │
            ▼
        json.dumps()           ← what NATS publishes
            │
            ▼
        json.loads()           ← what orchestrator receives
            │
            ▼
    _parse_container_output()  (rolemesh.agent.container_executor)
            │
            ▼
    AgentOutput(metadata=…)
            │
            ▼
    _extract_usage()           (rolemesh.main)
            │
            ▼
    db_store_message(input_tokens=…, output_tokens=…, cost_usd=…, …)
            │
            ▼
    SELECT * FROM messages

Bugs these catch that the unit suite cannot:

  1. Wire-format key drift — UsageSnapshot writes "input_tokens" but
     a future _extract_usage refactor reads "inputTokens". Each side
     has its own unit tests; only this round-trip catches the disagreement.

  2. ``store_message`` signature drift — orchestrator calls with
     keyword args; if the DB layer renames a parameter, neither side's
     tests notice until prod.

  3. JSON type erosion — ``cost_usd: float`` survives ``json.dumps``
     → ``json.loads``? ``Decimal`` stored in PG round-trips back as
     ``Decimal``? Numeric precision lost anywhere?

  4. ``_parse_container_output`` silently dropping nested metadata —
     it currently passes ``meta_val if isinstance(meta_val, dict) else
     None`` straight through, but a future refactor that sanitizes keys
     could strip "usage" without anyone noticing.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest

from agent_runner.backend import UsageSnapshot
from agent_runner.main import ContainerOutput
from rolemesh.agent.container_executor import _parse_container_output
from rolemesh.db import (
    _get_pool,
    create_channel_binding,
    create_conversation,
    create_coworker,
    create_tenant,
    store_message,
)
from rolemesh.main import _extract_usage

pytestmark = pytest.mark.usefixtures("test_db")


async def _setup_chain() -> tuple[str, str, str]:
    """Create tenant → coworker → binding → conversation. Return
    (tenant_id, coworker_id, conversation_id).

    Reused across every test to keep arrange/act/assert focused on
    the persistence pipeline rather than DB plumbing.
    """
    t = await create_tenant(name="UsageE2E", slug=f"e2e-{uuid.uuid4().hex[:8]}")
    cw = await create_coworker(
        tenant_id=t.id, name="Bot", folder=f"bot-{uuid.uuid4().hex[:8]}"
    )
    b = await create_channel_binding(
        coworker_id=cw.id,
        tenant_id=t.id,
        channel_type="web",
        credentials={"foo": "bar"},
    )
    conv = await create_conversation(
        tenant_id=t.id,
        coworker_id=cw.id,
        channel_binding_id=b.id,
        channel_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
    )
    return t.id, cw.id, conv.id


async def _persist_via_pipeline(
    *, tenant_id: str, conversation_id: str, sender_name: str,
    msg_id: str, content: str, container_output: ContainerOutput,
) -> None:
    """Run the wire-format → DB-row pipeline that the orchestrator's
    ``_on_output`` uses for assistant replies.

    Mirrors what main.py does for status="success" with text:

        usage = _extract_usage(result.metadata)
        await db_store_message(..., **usage fields...)

    Kept as a helper here (rather than calling ``_on_output`` directly)
    because that closure pulls in WebSocket gateway state, safety
    pipeline, and idle timer wiring that are orthogonal to the
    persistence path. Decoupling ensures the test fails for the RIGHT
    reason if the persistence chain breaks.
    """
    # Step 1: serialize over the wire (JSON via stringification — same
    # path as ``publish_output`` in agent_runner.main).
    wire_bytes = json.dumps(container_output.to_dict()).encode()

    # Step 2: orchestrator parses (same call site as
    # ContainerAgentExecutor._read_results).
    parsed = _parse_container_output(json.loads(wire_bytes))

    # Step 3: orchestrator extracts usage from metadata.
    usage = _extract_usage(parsed.metadata)

    # Step 4: persist (same call site as ``_on_output`` in main.py
    # for the success-with-text branch).
    await store_message(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        msg_id=msg_id,
        sender=sender_name,
        sender_name=sender_name,
        content=content,
        timestamp="2026-04-25T12:00:00+00:00",
        is_from_me=True,
        is_bot_message=True,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        cost_usd=usage.cost_usd,
        model_id=usage.model_id,
    )


async def _fetch_token_columns(tenant_id: str, conversation_id: str, msg_id: str) -> dict[str, object]:
    pool = _get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT input_tokens, output_tokens, cache_read_tokens,
                   cache_write_tokens, cost_usd, model_id
            FROM messages
            WHERE tenant_id = $1::uuid AND conversation_id = $2::uuid AND id = $3
            """,
            tenant_id, conversation_id, msg_id,
        )
    assert row is not None, f"row {msg_id} not persisted"
    return dict(row)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestClaudeSdkUsagePipeline:
    async def test_full_claude_sdk_usage_round_trips_to_db(self) -> None:
        """Claude SDK path: every token-bearing field arrives intact in
        the row. Pin the ENTIRE shape — any single-column drift (e.g.
        accidentally swapping cache_read_tokens with cache_write_tokens
        somewhere in the chain) is a numeric regression that's caught
        here, not by per-layer unit tests."""
        tid, _, convid = await _setup_chain()

        snap = UsageSnapshot(
            input_tokens=1500,
            output_tokens=320,
            cache_read_tokens=400,
            cache_write_tokens=120,
            cost_usd=0.012345,
            model_id="claude-sonnet-4-6",
            cost_source="sdk",
        )
        out = ContainerOutput(
            status="success",
            result="hello from claude",
            new_session_id="sess-1",
            metadata={"usage": snap.to_metadata()},
            is_final=False,
        )

        await _persist_via_pipeline(
            tenant_id=tid, conversation_id=convid, sender_name="Bot",
            msg_id="msg-claude-1", content="hello from claude",
            container_output=out,
        )

        row = await _fetch_token_columns(tid, convid, "msg-claude-1")
        assert row["input_tokens"] == 1500
        assert row["output_tokens"] == 320
        assert row["cache_read_tokens"] == 400
        assert row["cache_write_tokens"] == 120
        # Decimal — anchor the numeric type so a future float-cast
        # regression in the DB layer doesn't slip through.
        assert row["cost_usd"] == Decimal("0.012345")
        assert row["model_id"] == "claude-sonnet-4-6"

    async def test_smallest_cost_increment_survives_full_pipeline(self) -> None:
        """The Claude SDK reports cost down to ~$1/M tokens for cache
        reads. NUMERIC(10,6) holds it; JSON serialization preserves
        it (no scientific-notation precision loss); the row reads back
        non-zero. Catches a hypothetical future change that uses
        a less-precise format on the wire."""
        tid, _, convid = await _setup_chain()

        snap = UsageSnapshot(
            input_tokens=1, output_tokens=0,
            cost_usd=0.000001,
            model_id="claude-haiku-4-5",
            cost_source="sdk",
        )
        out = ContainerOutput(
            status="success", result="x",
            metadata={"usage": snap.to_metadata()},
        )
        await _persist_via_pipeline(
            tenant_id=tid, conversation_id=convid, sender_name="Bot",
            msg_id="msg-tiny", content="x", container_output=out,
        )
        row = await _fetch_token_columns(tid, convid, "msg-tiny")
        assert row["cost_usd"] == Decimal("0.000001")
        # And it didn't round to zero — important enough to assert
        # twice in different forms.
        assert row["cost_usd"] != 0


class TestPiUsagePipeline:
    async def test_pi_usage_round_trips_with_provider_source(self) -> None:
        """Pi path: tokens accumulate across multiple LLM calls, cost
        comes from ``pi.ai.models.calculate_cost`` mutation. The
        cost_source on the wire is "provider" (vs Claude's "sdk") —
        but cost_source is NOT persisted in this PR, so the DB row
        is indistinguishable from a Claude row by column inspection
        alone. Verify model_id carries through as the dominant model
        identifier the accumulator picks."""
        tid, _, convid = await _setup_chain()

        snap = UsageSnapshot(
            input_tokens=350,
            output_tokens=150,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=0.00345,
            model_id="claude-sonnet-4-6",  # dominant by output tokens
            cost_source="provider",
        )
        out = ContainerOutput(
            status="success",
            result="answer from pi",
            metadata={"usage": snap.to_metadata()},
        )
        await _persist_via_pipeline(
            tenant_id=tid, conversation_id=convid, sender_name="Bot",
            msg_id="msg-pi-1", content="answer from pi",
            container_output=out,
        )

        row = await _fetch_token_columns(tid, convid, "msg-pi-1")
        assert row["input_tokens"] == 350
        assert row["output_tokens"] == 150
        assert row["cost_usd"] == Decimal("0.003450")
        assert row["model_id"] == "claude-sonnet-4-6"

    async def test_pi_unknown_model_persists_tokens_with_null_cost(self) -> None:
        """Pi user runs a custom model not in the price registry.
        ``calculate_cost`` doesn't mutate ``usage.cost`` → the
        accumulator's _cost_seen stays False → snapshot.cost_usd is
        None. Tokens still land; cost_usd column is NULL.

        This matches the "we don't know the cost" semantics the
        snapshot contract promises and lets sum-of-cost analytics
        filter unknowns. A regression that converts None → 0.0 here
        would silently start polluting cost reports with phantom
        zero-dollar rows for custom-model traffic."""
        tid, _, convid = await _setup_chain()

        snap = UsageSnapshot(
            input_tokens=200,
            output_tokens=80,
            cost_usd=None,
            model_id="custom-llama-finetune",
            cost_source=None,
        )
        out = ContainerOutput(
            status="success", result="custom",
            metadata={"usage": snap.to_metadata()},
        )
        await _persist_via_pipeline(
            tenant_id=tid, conversation_id=convid, sender_name="Bot",
            msg_id="msg-pi-custom", content="custom", container_output=out,
        )

        row = await _fetch_token_columns(tid, convid, "msg-pi-custom")
        assert row["input_tokens"] == 200
        assert row["output_tokens"] == 80
        # NULL not 0 — distinguishes "unknown" from "free".
        assert row["cost_usd"] is None
        assert row["model_id"] == "custom-llama-finetune"


class TestLegacyAndAbsentMetadata:
    async def test_pre_usage_container_image_keeps_six_columns_null(self) -> None:
        """Backwards compatibility with old agent_runner images that
        don't emit metadata.usage at all. A rolling deploy where some
        containers are still on the previous image must keep working —
        the orchestrator just records NULL for the unknown columns."""
        tid, _, convid = await _setup_chain()

        # Old container: success path with no metadata field at all.
        out = ContainerOutput(
            status="success",
            result="legacy reply",
            new_session_id="legacy-sess",
            # metadata=None: this is what to_dict() produces when not
            # set — see ContainerOutput.to_dict's ``if self.metadata is
            # not None`` guard. The dict won't even have a ``metadata``
            # key.
        )
        # Sanity: confirm the legacy wire shape genuinely has no
        # metadata key. If a future refactor adds an empty default
        # metadata dict, this assertion fires AND the test stops
        # actually verifying the legacy code path — that's the cue
        # to update the test, not to silently rubberstamp.
        assert "metadata" not in out.to_dict()

        await _persist_via_pipeline(
            tenant_id=tid, conversation_id=convid, sender_name="Bot",
            msg_id="msg-legacy", content="legacy reply",
            container_output=out,
        )

        row = await _fetch_token_columns(tid, convid, "msg-legacy")
        assert row["input_tokens"] is None
        assert row["output_tokens"] is None
        assert row["cache_read_tokens"] is None
        assert row["cache_write_tokens"] is None
        assert row["cost_usd"] is None
        assert row["model_id"] is None

    async def test_metadata_present_but_no_usage_subkey_keeps_columns_null(self) -> None:
        """Edge case: a container emits metadata for a different reason
        (e.g. a legacy progress event with metadata.tool_count) but not
        the usage subkey. ``_extract_usage`` must return all-None
        ``_UsageFields`` rather than crashing or fabricating zeros."""
        tid, _, convid = await _setup_chain()

        out = ContainerOutput(
            status="success",
            result="metadata-without-usage",
            metadata={"some_other_key": "value"},
        )
        await _persist_via_pipeline(
            tenant_id=tid, conversation_id=convid, sender_name="Bot",
            msg_id="msg-other-meta", content="metadata-without-usage",
            container_output=out,
        )

        row = await _fetch_token_columns(tid, convid, "msg-other-meta")
        for col in (
            "input_tokens", "output_tokens",
            "cache_read_tokens", "cache_write_tokens",
            "cost_usd", "model_id",
        ):
            assert row[col] is None, f"{col} should be NULL, got {row[col]!r}"

    async def test_garbage_usage_payload_does_not_crash_pipeline(self) -> None:
        """Adversarial: a malicious or buggy container publishes a
        ``usage`` value that's the wrong shape (string instead of
        dict). The orchestrator must NOT crash on this — `_extract_usage`
        is the trust boundary and must coerce-or-discard, never raise.

        If this test ever starts failing because the orchestrator
        crashed, that's a denial-of-service surface against the
        message-storage path: a single rogue container could stop
        ANY message from being persisted, including legitimate ones
        from sibling conversations sharing the orchestrator.
        """
        tid, _, convid = await _setup_chain()

        # Hand-craft the wire dict — go around ContainerOutput's
        # type-aware ``to_dict`` so we can inject malformed metadata.
        wire = {
            "status": "success",
            "result": "rogue",
            "metadata": {"usage": "not a dict"},  # WRONG shape
        }
        # Step through the same parse path the pipeline uses, but with
        # the malformed payload.
        parsed = _parse_container_output(json.loads(json.dumps(wire)))
        # The trust boundary: must return all-None _UsageFields, not raise.
        fields = _extract_usage(parsed.metadata)
        assert fields.input_tokens is None
        assert fields.cost_usd is None

        # And the message still persists with NULL columns.
        await store_message(
            tenant_id=tid, conversation_id=convid,
            msg_id="msg-rogue", sender="Bot", sender_name="Bot",
            content="rogue", timestamp="2026-04-25T12:00:00+00:00",
            is_from_me=True, is_bot_message=True,
            input_tokens=fields.input_tokens,
            output_tokens=fields.output_tokens,
            cache_read_tokens=fields.cache_read_tokens,
            cache_write_tokens=fields.cache_write_tokens,
            cost_usd=fields.cost_usd,
            model_id=fields.model_id,
        )
        row = await _fetch_token_columns(tid, convid, "msg-rogue")
        assert row["cost_usd"] is None


class TestWireFormatStability:
    """Pin the exact key names UsageSnapshot writes vs orchestrator
    reads. Either side renaming silently → these tests fire.
    """

    async def test_extract_usage_reads_exact_keys_snapshot_writes(self) -> None:
        """The contract: UsageSnapshot.to_metadata() and _extract_usage()
        must agree on every key name. This test fails fast if either
        side renames a key."""
        snap = UsageSnapshot(
            input_tokens=10, output_tokens=20,
            cache_read_tokens=30, cache_write_tokens=40,
            cost_usd=0.5, model_id="m", cost_source="sdk",
        )
        wire = snap.to_metadata()

        # Drive through a full JSON round-trip — catches dict-key
        # encoding / unicode / numeric drift.
        wire_via_json = json.loads(json.dumps({"usage": wire}))

        fields = _extract_usage(wire_via_json)
        assert fields.input_tokens == 10
        assert fields.output_tokens == 20
        assert fields.cache_read_tokens == 30
        assert fields.cache_write_tokens == 40
        assert fields.cost_usd == 0.5
        assert fields.model_id == "m"

    async def test_container_output_metadata_passthrough_via_parse(self) -> None:
        """``_parse_container_output`` must keep the nested usage dict
        intact through serialize → deserialize. A future refactor that
        sanitizes metadata keys (e.g. drops anything not in an allowlist)
        would silently strip ``usage`` and break the DB write path.
        Pin the passthrough explicitly."""
        snap = UsageSnapshot(input_tokens=1, output_tokens=1, cost_usd=0.001)
        out = ContainerOutput(
            status="success", result="x",
            metadata={"usage": snap.to_metadata(), "extra": "preserved"},
        )
        json_bytes = json.dumps(out.to_dict())
        parsed = _parse_container_output(json.loads(json_bytes))
        assert parsed.metadata is not None
        assert isinstance(parsed.metadata.get("usage"), dict)
        # Sibling keys preserved too — passthrough is total.
        assert parsed.metadata.get("extra") == "preserved"
