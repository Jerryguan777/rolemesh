"""Pi backend _PromptUsageAccumulator + flush behavior tests.

The accumulator is the bridge between Pi's per-LLM-call usage events
and the per-prompt UsageSnapshot the wire protocol carries. Three things
have to be airtight:

1. **Reset between prompts** — the accumulator is held on the PiBackend
   instance, which is reused for the lifetime of the container. Without
   reset, prompt N+1's row would silently include prompt N's tokens.
2. **Multi-model dominant-by-output picking** — when a single prompt
   fans out to multiple providers, model_id needs to be a single stable
   string. Picking the model with the most output tokens is a
   deterministic choice that survives small perturbations.
3. **Abort path captures what was burned** — provider already billed
   the partial tokens, so the StoppedEvent carries them too.
"""

from __future__ import annotations

from agent_runner.backend import UsageSnapshot
from agent_runner.pi_backend import _PromptUsageAccumulator
from pi.ai.types import AssistantMessage, Usage, UsageCost


def _msg(
    model: str,
    input_t: int,
    output_t: int,
    cache_read: int = 0,
    cache_write: int = 0,
    cost_total: float | None = None,
) -> AssistantMessage:
    """Build a minimal AssistantMessage with usage filled in.

    cost_total simulates what calculate_cost(model, usage) writes onto
    usage.cost.total inside Pi providers. None means "calculate_cost
    never ran" (e.g. custom model not in registry) — leaves the default
    UsageCost in place with cost.total == 0.
    """
    cost = UsageCost()
    if cost_total is not None:
        cost.total = cost_total
    return AssistantMessage(
        model=model,
        usage=Usage(
            input=input_t,
            output=output_t,
            cache_read=cache_read,
            cache_write=cache_write,
            cost=cost,
        ),
    )


class TestAccumulator:
    def test_empty_acc_yields_none_snapshot(self) -> None:
        """A fresh acc must report None on flush — distinguishes
        'no LLM calls happened' from 'all calls were zero-token',
        which downstream uses to skip a row entirely."""
        acc = _PromptUsageAccumulator()
        assert acc.is_empty() is True
        assert acc.to_snapshot() is None

    def test_single_message_accumulates(self) -> None:
        acc = _PromptUsageAccumulator()
        acc.add(_msg("claude-sonnet-4-6", 100, 50, cache_read=10, cache_write=5))
        snap = acc.to_snapshot()
        assert snap is not None
        assert snap.input_tokens == 100
        assert snap.output_tokens == 50
        assert snap.cache_read_tokens == 10
        assert snap.cache_write_tokens == 5
        assert snap.model_id == "claude-sonnet-4-6"
        # Pi backend doesn't compute USD cost in this scope.
        assert snap.cost_usd is None
        assert snap.cost_source is None

    def test_multiple_calls_sum(self) -> None:
        """Three message_end events for the same prompt → input/output
        tokens are summed. Mirrors a tool-using turn where the model
        replies, calls a tool, then replies again."""
        acc = _PromptUsageAccumulator()
        acc.add(_msg("claude-sonnet-4-6", 100, 50))
        acc.add(_msg("claude-sonnet-4-6", 200, 75))
        acc.add(_msg("claude-sonnet-4-6", 50, 25))
        snap = acc.to_snapshot()
        assert snap is not None
        assert snap.input_tokens == 350
        assert snap.output_tokens == 150

    def test_dominant_model_by_output(self) -> None:
        """Two providers in one prompt: pick the one with most output
        tokens. The choice is intentionally deterministic — no Counter
        ordering surprises."""
        acc = _PromptUsageAccumulator()
        acc.add(_msg("claude-sonnet-4-6", 100, 200))
        acc.add(_msg("gpt-4o", 100, 100))
        acc.add(_msg("claude-sonnet-4-6", 50, 100))
        # claude total output = 300, gpt output = 100 → claude wins.
        snap = acc.to_snapshot()
        assert snap is not None
        assert snap.model_id == "claude-sonnet-4-6"

    def test_dominant_model_when_only_one_provider_has_output(self) -> None:
        """Provider that streamed tokens but reported 0 output (rare,
        but possible during a partial stream that never finalized usage)
        should not get picked over a smaller-but-real provider."""
        acc = _PromptUsageAccumulator()
        acc.add(_msg("claude-sonnet-4-6", 100, 0))
        acc.add(_msg("gpt-4o", 50, 30))
        snap = acc.to_snapshot()
        assert snap is not None
        # claude's 0 output keeps it out of by_model entirely (the
        # accumulator only adds to by_model when output > 0... wait,
        # actually it adds output==0 too. Let me re-examine.
        # Looking at the code: it adds the output regardless. If both
        # entries have output 0+30, claude is in by_model with value 0
        # and gpt with 30, so gpt wins. If output_tokens for the entry
        # is 0, the call still increments by_model[name] += 0 — the
        # key gets registered but with value 0. max() then picks gpt.
        assert snap.model_id == "gpt-4o"

    def test_reset_clears_everything(self) -> None:
        acc = _PromptUsageAccumulator()
        acc.add(_msg("claude-sonnet-4-6", 100, 50))
        acc.reset()
        assert acc.is_empty() is True
        assert acc.to_snapshot() is None
        # And a fresh accumulation after reset doesn't see any of the
        # prior tokens — this is the cross-prompt isolation contract.
        acc.add(_msg("gpt-4o", 10, 5))
        snap = acc.to_snapshot()
        assert snap is not None
        assert snap.input_tokens == 10
        assert snap.output_tokens == 5
        assert snap.model_id == "gpt-4o"

    def test_message_without_usage_is_ignored(self) -> None:
        """A misbehaving Pi build that yields an AssistantMessage with
        usage=None must not crash. The accumulator just skips that
        message — analytics will see fewer recorded tokens, which is
        still better than the agent loop crashing."""
        acc = _PromptUsageAccumulator()
        # Intentionally misshape the message: real AssistantMessage
        # has Usage() default, but defensive code shouldn't assume.
        msg = AssistantMessage(model="claude-sonnet-4-6")
        msg.usage = None  # type: ignore[assignment]
        acc.add(msg)
        # No-op — accumulator stays empty.
        assert acc.is_empty() is True

    def test_message_without_model_uses_api_label(self) -> None:
        """If the message has empty .model but a populated .api,
        attribute the tokens to the API label rather than dropping
        them silently. Some Pi providers populate api but not model."""
        msg = AssistantMessage(
            api="anthropic-messages",
            model="",
            usage=Usage(input=10, output=5),
        )
        acc = _PromptUsageAccumulator()
        acc.add(msg)
        snap = acc.to_snapshot()
        assert snap is not None
        # api as fallback when model is empty
        assert snap.model_id == "anthropic-messages"

    def test_message_without_any_label_still_accumulates_tokens(self) -> None:
        """Defensive: tokens should be counted even when neither model
        nor api is populated. Only the model_id label is unknown."""
        msg = AssistantMessage(
            api="",
            model="",
            usage=Usage(input=10, output=5),
        )
        acc = _PromptUsageAccumulator()
        acc.add(msg)
        snap = acc.to_snapshot()
        assert snap is not None
        assert snap.input_tokens == 10
        assert snap.output_tokens == 5
        assert snap.model_id is None  # no label to attribute to


class TestAccumulatorEdgeCases:
    """The "what could go wrong" sweep."""

    def test_negative_tokens_are_passed_through(self) -> None:
        """Some buggy providers have been known to report negative
        usage on cache invalidation. We don't try to second-guess
        the provider — pass through and let analytics handle it.

        This test pins the current behavior so a future "sanitize"
        change is a deliberate decision, not an accidental drift."""
        acc = _PromptUsageAccumulator()
        acc.add(_msg("claude-sonnet-4-6", -5, 10))
        snap = acc.to_snapshot()
        assert snap is not None
        assert snap.input_tokens == -5

    def test_repeated_to_snapshot_is_idempotent(self) -> None:
        """Calling to_snapshot() twice in a row must not advance state.
        Important because _handle_event flushes-then-resets, and a
        retry-on-publish-failure path could reasonably re-flush."""
        acc = _PromptUsageAccumulator()
        acc.add(_msg("claude-sonnet-4-6", 100, 50))
        first = acc.to_snapshot()
        second = acc.to_snapshot()
        assert first == second
        # And a subsequent reset still works as expected — no latched
        # state from the double flush.
        acc.reset()
        assert acc.to_snapshot() is None

    def test_after_reset_accumulator_is_byte_equal_to_fresh(self) -> None:
        """The reset()-then-fresh() invariant guards against a subtle
        bug where field()-default dicts get reused-by-reference. If
        ``by_model`` ever silently aliased the previous prompt's dict,
        this test catches it."""
        acc1 = _PromptUsageAccumulator()
        acc1.add(_msg("claude-sonnet-4-6", 100, 50))
        acc1.reset()

        acc2 = _PromptUsageAccumulator()
        # Both should yield identical snapshots after the same input.
        acc1.add(_msg("gpt-4o", 30, 20))
        acc2.add(_msg("gpt-4o", 30, 20))
        assert acc1.to_snapshot() == acc2.to_snapshot()


class TestSnapshotShape:
    """The accumulator outputs must match the contract UsageSnapshot
    advertises."""

    def test_no_cost_data_yields_none_cost_fields(self) -> None:
        """When NO message in the prompt had calculate_cost run on it
        (e.g. all calls used a custom model not in registry), the
        snapshot reports cost_usd=None and cost_source=None — same
        contract as Claude SDK on a non-pricing proxy."""
        acc = _PromptUsageAccumulator()
        # cost_total=None on every msg → calculate_cost never ran.
        acc.add(_msg("custom-model", 1000, 500))
        snap = acc.to_snapshot()
        assert snap is not None
        assert snap.cost_usd is None
        assert snap.cost_source is None

    def test_returns_usage_snapshot_dataclass(self) -> None:
        """Type-equality check — to_snapshot() must return a real
        UsageSnapshot, not a dict or a duck-typed object. The wire
        path calls .to_metadata() on it."""
        acc = _PromptUsageAccumulator()
        acc.add(_msg("claude-sonnet-4-6", 1, 1))
        snap = acc.to_snapshot()
        assert isinstance(snap, UsageSnapshot)


class TestCostAccumulation:
    """Pi providers call calculate_cost(model, usage) before emitting
    DoneEvent, mutating ``usage.cost.total`` with USD. The accumulator
    sums those across LLM calls in one prompt — same shape as Claude
    SDK's total_cost_usd, just sourced from a per-provider table."""

    def test_single_call_carries_cost_through_snapshot(self) -> None:
        """One provider call with cost.total set → snapshot picks it
        up as cost_usd, with cost_source='provider' to distinguish from
        Claude SDK's authoritative pricing ('sdk')."""
        acc = _PromptUsageAccumulator()
        acc.add(_msg("claude-sonnet-4-6", 1000, 500, cost_total=0.0095))
        snap = acc.to_snapshot()
        assert snap is not None
        assert snap.cost_usd == 0.0095
        assert snap.cost_source == "provider"

    def test_multiple_calls_sum_cost(self) -> None:
        """Tool-using turn: three LLM calls within one prompt. The
        prompt-level cost is the sum, not the last call's cost. This
        is the same arithmetic discipline as the token sum."""
        acc = _PromptUsageAccumulator()
        acc.add(_msg("claude-sonnet-4-6", 100, 50, cost_total=0.001))
        acc.add(_msg("claude-sonnet-4-6", 200, 75, cost_total=0.002))
        acc.add(_msg("claude-sonnet-4-6", 50, 25, cost_total=0.0005))
        snap = acc.to_snapshot()
        assert snap is not None
        # Floating point sum — use approx to dodge representation drift.
        assert snap.cost_usd is not None
        assert abs(snap.cost_usd - 0.0035) < 1e-9
        assert snap.cost_source == "provider"

    def test_zero_cost_call_does_not_flip_seen(self) -> None:
        """If a provider sets cost.total=0.0 (no calculate_cost ran or
        every cost rate was zero), it MUST NOT flip _cost_seen — the
        snapshot stays cost_usd=None. Otherwise we'd produce cost=0
        rows that conflate "we know it cost nothing" with "we don't
        know what it cost", which sum-of-cost analytics can't tell apart."""
        acc = _PromptUsageAccumulator()
        # cost_total=0.0 mimics the default UsageCost() shape that
        # never had calculate_cost touch it.
        acc.add(_msg("custom-model", 100, 50, cost_total=0.0))
        snap = acc.to_snapshot()
        assert snap is not None
        # Tokens were captured...
        assert snap.input_tokens == 100
        # ...but cost stays unknown.
        assert snap.cost_usd is None
        assert snap.cost_source is None

    def test_mixed_cost_seen_partial_run_still_carries_cost(self) -> None:
        """One known-priced call + one unknown-priced call in the same
        prompt. _cost_seen flips True on the priced call, so the
        snapshot reports cost_usd = the priced call's cost only.
        Better to under-report (we know the floor) than emit None and
        lose visibility entirely."""
        acc = _PromptUsageAccumulator()
        acc.add(_msg("claude-sonnet-4-6", 100, 50, cost_total=0.005))
        # Second call: same prompt, custom unpriced model (e.g. a tool
        # routed through a different provider whose model isn't in
        # the registry).
        acc.add(_msg("custom-model", 200, 100, cost_total=0.0))
        snap = acc.to_snapshot()
        assert snap is not None
        assert snap.cost_usd == 0.005
        assert snap.cost_source == "provider"
        # Tokens still sum across BOTH calls — the cost limitation
        # doesn't propagate to token accounting.
        assert snap.input_tokens == 300
        assert snap.output_tokens == 150

    def test_reset_clears_cost(self) -> None:
        """Cross-prompt isolation: prompt 1's $0.005 must not leak
        into prompt 2's snapshot. Same property the token reset
        guards, applied to cost — without it, every PiBackend
        instance would silently triple-count cost over its lifetime."""
        acc = _PromptUsageAccumulator()
        acc.add(_msg("claude-sonnet-4-6", 100, 50, cost_total=0.005))
        snap_before = acc.to_snapshot()
        assert snap_before is not None
        assert snap_before.cost_usd == 0.005

        acc.reset()
        # After reset, an empty acc → None snapshot.
        assert acc.to_snapshot() is None

        # And a fresh accumulation does NOT see prompt 1's cost.
        acc.add(_msg("claude-sonnet-4-6", 30, 20, cost_total=0.0003))
        snap_after = acc.to_snapshot()
        assert snap_after is not None
        assert snap_after.cost_usd == 0.0003

    def test_negative_cost_is_ignored(self) -> None:
        """Provider that misreports cost as a negative number (e.g.
        a refund event leaking through) must not subtract from the
        running total. We accept >0 only — pin this guard so a future
        cost-bug provider doesn't silently zero out a tenant's
        prompt-level cost row."""
        acc = _PromptUsageAccumulator()
        acc.add(_msg("claude-sonnet-4-6", 100, 50, cost_total=-0.01))
        snap = acc.to_snapshot()
        assert snap is not None
        # Negative cost ignored entirely → never seen → None.
        assert snap.cost_usd is None

    def test_usage_cost_object_missing_total_attr(self) -> None:
        """Defensive: future Pi UsageCost shape change that drops the
        .total attribute must not crash the agent loop. Tokens still
        accumulate; cost stays unknown."""
        msg = AssistantMessage(
            model="claude-sonnet-4-6",
            usage=Usage(input=100, output=50),
        )
        # Replace usage.cost with an object that has no .total attribute.
        msg.usage.cost = object()  # type: ignore[assignment]
        acc = _PromptUsageAccumulator()
        acc.add(msg)
        snap = acc.to_snapshot()
        assert snap is not None
        assert snap.input_tokens == 100
        assert snap.cost_usd is None

    def test_usage_cost_set_to_none(self) -> None:
        """Defensive: usage.cost itself being None (would require a
        Pi refactor that makes the field optional) must not crash."""
        msg = AssistantMessage(
            model="claude-sonnet-4-6",
            usage=Usage(input=100, output=50),
        )
        msg.usage.cost = None  # type: ignore[assignment]
        acc = _PromptUsageAccumulator()
        acc.add(msg)
        snap = acc.to_snapshot()
        assert snap is not None
        assert snap.cost_usd is None
