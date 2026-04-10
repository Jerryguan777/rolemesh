"""Helper functions for building stream options — Python port of providers/simple-options.ts."""

from __future__ import annotations

from pi.ai.types import (
    Model,
    SimpleStreamOptions,
    StreamOptions,
    ThinkingBudgets,
    ThinkingLevel,
)


def build_base_options(
    model: Model,
    options: SimpleStreamOptions | None = None,
    api_key: str | None = None,
) -> StreamOptions:
    """Construct StreamOptions from SimpleStreamOptions."""
    if options is None:
        return StreamOptions(
            max_tokens=min(model.max_tokens, 32000),
            api_key=api_key,
        )
    return StreamOptions(
        temperature=options.temperature,
        max_tokens=options.max_tokens or min(model.max_tokens, 32000),
        signal=options.signal,
        api_key=api_key or options.api_key,
        cache_retention=options.cache_retention,
        session_id=options.session_id,
        headers=options.headers,
        on_payload=options.on_payload,
        max_retry_delay_ms=options.max_retry_delay_ms,
        metadata=options.metadata,
    )


def clamp_reasoning(effort: ThinkingLevel | None) -> ThinkingLevel | None:
    """Map 'xhigh' to 'high', pass through everything else."""
    if effort == "xhigh":
        return "high"
    return effort


def adjust_max_tokens_for_thinking(
    base_max_tokens: int,
    model_max_tokens: int,
    reasoning_level: ThinkingLevel,
    custom_budgets: ThinkingBudgets | None = None,
) -> tuple[int, int]:
    """Calculate thinking token budget.

    Returns (max_tokens, thinking_budget).
    """
    default_budgets = ThinkingBudgets(
        minimal=1024,
        low=2048,
        medium=8192,
        high=16384,
    )

    # Merge custom budgets over defaults
    cb = custom_budgets
    db = default_budgets
    budgets = ThinkingBudgets(
        minimal=cb.minimal if cb and cb.minimal is not None else db.minimal,
        low=cb.low if cb and cb.low is not None else db.low,
        medium=cb.medium if cb and cb.medium is not None else db.medium,
        high=cb.high if cb and cb.high is not None else db.high,
    )

    min_output_tokens = 1024
    level = clamp_reasoning(reasoning_level)
    if level is None:
        raise ValueError("reasoning_level must not be None")
    thinking_budget: int = getattr(budgets, level)
    max_tokens = min(base_max_tokens + thinking_budget, model_max_tokens)

    if max_tokens <= thinking_budget:
        thinking_budget = max(0, max_tokens - min_output_tokens)

    return max_tokens, thinking_budget
