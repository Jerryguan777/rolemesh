"""UsageSnapshot wire-format + ContainerOutput backwards-compat tests.

The bar these tests are watching for: the new ``usage`` plumbing must be
strictly additive on the wire. A container without usage data must publish
JSON byte-equal to pre-change builds, otherwise existing orchestrators that
haven't been redeployed alongside agent_runner will see surprise keys.
"""

from __future__ import annotations

import json

from agent_runner.backend import (
    ErrorEvent,
    ResultEvent,
    SafetyBlockEvent,
    StoppedEvent,
    UsageSnapshot,
)
from agent_runner.main import ContainerOutput


class TestUsageSnapshotRoundTrip:
    def test_full_snapshot_round_trips(self) -> None:
        snap = UsageSnapshot(
            input_tokens=1234,
            output_tokens=567,
            cache_read_tokens=89,
            cache_write_tokens=10,
            cost_usd=0.012345,
            model_id="claude-sonnet-4-6",
            cost_source="sdk",
        )
        wire = snap.to_metadata()
        # Wire keys are stable contract — DB-side decoding indexes into
        # them by name.
        assert wire == {
            "input_tokens": 1234,
            "output_tokens": 567,
            "cache_read_tokens": 89,
            "cache_write_tokens": 10,
            "cost_usd": 0.012345,
            "model_id": "claude-sonnet-4-6",
            "cost_source": "sdk",
        }
        assert UsageSnapshot.from_metadata(wire) == snap

    def test_zero_tokens_distinguishable_from_absent(self) -> None:
        """A backend that reports cost_usd=None deserializes to None,
        not 0.0 — analytics queries need that distinction to filter
        out unknown-cost rows from sum-of-cost reports."""
        snap = UsageSnapshot(input_tokens=10, output_tokens=5)
        wire = snap.to_metadata()
        # cost_usd / model_id / cost_source explicitly serialize as None
        # rather than being omitted, so the receiver doesn't have to
        # special-case "key missing" vs "key was None".
        assert wire["cost_usd"] is None
        assert wire["model_id"] is None
        assert wire["cost_source"] is None
        assert UsageSnapshot.from_metadata(wire).cost_usd is None

    def test_garbage_in_optional_fields_decodes_safely(self) -> None:
        """Wire payload from a buggy producer / future SDK version
        must not crash deserialize. Unknown ``cost_source`` falls back
        to None; non-numeric cost_usd → None; non-string model_id →
        None. Bad token counts coerce to int via the ``or 0`` ladder.
        """
        snap = UsageSnapshot.from_metadata(
            {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
                "cost_usd": "not a number",
                "model_id": 42,
                "cost_source": "unsupported",
            }
        )
        assert snap.input_tokens == 100
        assert snap.output_tokens == 50
        assert snap.cache_read_tokens == 0
        assert snap.cache_write_tokens == 0
        assert snap.cost_usd is None
        assert snap.model_id is None
        assert snap.cost_source is None

    def test_empty_dict_yields_zero_snapshot(self) -> None:
        """from_metadata on {} must not raise — robustness against
        a producer that emits the key but populates nothing."""
        snap = UsageSnapshot.from_metadata({})
        assert snap == UsageSnapshot()


class TestContainerOutputBackwardsCompat:
    def test_legacy_no_usage_byte_equal(self) -> None:
        """The CRITICAL invariant: a ContainerOutput without usage must
        serialize to bytes identical to pre-change builds. If this
        regresses, every orchestrator running an older agent_runner
        image stops parsing newer wire formats correctly."""
        # Pre-change shape: success without metadata, no usage.
        out = ContainerOutput(
            status="success", result="hi", new_session_id="sid"
        )
        d = out.to_dict()
        # No metadata key at all when usage was never set — important
        # for the wire byte-equality property.
        assert "metadata" not in d
        assert d == {"status": "success", "result": "hi", "newSessionId": "sid"}

    def test_legacy_safety_blocked_only_has_stage(self) -> None:
        """Safety-block without usage — metadata carries stage only,
        not a usage subkey."""
        out = ContainerOutput(
            status="safety_blocked",
            result="rule fired",
            metadata={"stage": "input_prompt"},
        )
        d = out.to_dict()
        assert d["metadata"] == {"stage": "input_prompt"}
        assert "usage" not in d["metadata"]

    def test_usage_metadata_attaches_under_usage_key(self) -> None:
        """When the bridge attaches usage, it lands under metadata.usage —
        a stable key the orchestrator's _extract_usage indexes into.
        Any other placement breaks DB persistence silently."""
        usage = UsageSnapshot(
            input_tokens=100, output_tokens=50, cost_usd=0.001, cost_source="sdk"
        )
        out = ContainerOutput(
            status="success",
            result="reply",
            metadata={"usage": usage.to_metadata()},
        )
        d = out.to_dict()
        assert d["metadata"]["usage"]["input_tokens"] == 100
        assert d["metadata"]["usage"]["cost_source"] == "sdk"
        # JSON round-trip must preserve numeric types — cost_usd as
        # float doesn't decay to string en route through JSON.
        round_tripped = json.loads(json.dumps(d))
        assert round_tripped["metadata"]["usage"]["cost_usd"] == 0.001


class TestEventCarriesUsage:
    """The four terminal events must accept usage as an optional field
    without breaking pre-existing kwargless construction."""

    def test_result_event_default_usage_none(self) -> None:
        ev = ResultEvent(text="ok")
        assert ev.usage is None

    def test_result_event_with_usage(self) -> None:
        snap = UsageSnapshot(input_tokens=1)
        ev = ResultEvent(text="ok", usage=snap)
        assert ev.usage is snap

    def test_error_event_default_usage_none(self) -> None:
        ev = ErrorEvent(error="boom")
        assert ev.usage is None

    def test_stopped_event_default_usage_none(self) -> None:
        # Pre-change StoppedEvent had zero fields. Default-constructible
        # without usage MUST still work, otherwise existing call sites
        # that pass () would break.
        ev = StoppedEvent()
        assert ev.usage is None

    def test_safety_block_event_default_usage_none(self) -> None:
        ev = SafetyBlockEvent(stage="input_prompt", reason="blocked")
        assert ev.usage is None

    def test_safety_block_with_usage_for_output_stage(self) -> None:
        """Output-stage safety blocks happen AFTER an LLM call, so the
        backend SHOULD attach usage. Verify the field accepts it."""
        snap = UsageSnapshot(input_tokens=200, output_tokens=100)
        ev = SafetyBlockEvent(stage="model_output", reason="x", usage=snap)
        assert ev.usage is snap
