"""Model registry and utilities — Python port of packages/ai/src/models.ts.

The model registry is populated from models.generated data (or manually).
Unlike TS, we don't auto-load from a generated file — models are registered
via register_models() or individually.
"""

from __future__ import annotations

from collections.abc import Mapping

from pi.ai.types import Model, Usage, UsageCost

# Provider -> (ModelId -> Model)
# Note: module-level mutable state. Safe for single-threaded asyncio use.
# Not thread-safe — do not use with pytest-xdist or multi-threaded runtimes.
_model_registry: dict[str, dict[str, Model]] = {}


def register_models(provider: str, models: Mapping[str, Model]) -> None:
    """Register models for a provider. Used to populate the registry.

    Accepts any Mapping (dict, Mapping view, etc.) and stores an internal copy.
    Calling this again for the same provider fully replaces its model set.
    """
    _model_registry[provider] = dict(models)


def clear_model_registry() -> None:
    """Clear all registered models."""
    _model_registry.clear()


def get_model(provider: str, model_id: str) -> Model | None:
    """Look up a model by provider and model ID."""
    provider_models = _model_registry.get(provider)
    if provider_models is None:
        return None
    return provider_models.get(model_id)


def get_providers() -> list[str]:
    """Return all registered provider names."""
    return list(_model_registry.keys())


def get_models(provider: str) -> list[Model]:
    """Return all models for a given provider."""
    provider_models = _model_registry.get(provider)
    if provider_models is None:
        return []
    return list(provider_models.values())


def calculate_cost(model: Model, usage: Usage) -> UsageCost:
    """Compute cost from usage and model rates. Mutates usage.cost in place and returns it."""
    usage.cost.input = (model.cost.input / 1_000_000) * usage.input
    usage.cost.output = (model.cost.output / 1_000_000) * usage.output
    usage.cost.cache_read = (model.cost.cache_read / 1_000_000) * usage.cache_read
    usage.cost.cache_write = (model.cost.cache_write / 1_000_000) * usage.cache_write
    usage.cost.total = usage.cost.input + usage.cost.output + usage.cost.cache_read + usage.cost.cache_write
    return usage.cost


def supports_xhigh(model: Model) -> bool:
    """Check if a model supports xhigh thinking level."""
    if "gpt-5.2" in model.id or "gpt-5.3" in model.id:
        return True
    if model.api == "anthropic-messages":
        return "opus-4-6" in model.id or "opus-4.6" in model.id
    return False


def models_are_equal(a: Model | None, b: Model | None) -> bool:
    """Check if two models are equal by comparing id and provider."""
    if a is None or b is None:
        return False
    return a.id == b.id and a.provider == b.provider
