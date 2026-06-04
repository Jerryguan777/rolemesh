"""Model resolution, scoping, and initial selection.

Port of packages/coding-agent/src/core/model-resolver.ts.
"""

from __future__ import annotations

import fnmatch
import sys
from dataclasses import dataclass
from typing import Any

from pi.agent.types import ThinkingLevel
from pi.ai.types import Model
from pi.coding_agent.core.model_registry import ModelRegistry

# ============================================================================
# Default models per provider
# ============================================================================

DEFAULT_MODEL_PER_PROVIDER: dict[str, str] = {
    "amazon-bedrock": "us.anthropic.claude-opus-4-6-v1",
    "anthropic": "claude-opus-4-6",
    "openai": "gpt-5.1-codex",
    "azure-openai-responses": "gpt-5.2",
    "openai-codex": "gpt-5.3-codex",
    "google": "gemini-2.5-pro",
    "google-gemini-cli": "gemini-2.5-pro",
    "google-antigravity": "gemini-3-pro-high",
    "google-vertex": "gemini-3-pro-preview",
    "github-copilot": "gpt-4o",
    "openrouter": "openai/gpt-5.1-codex",
    "vercel-ai-gateway": "anthropic/claude-opus-4-6",
    "xai": "grok-4-fast-non-reasoning",
    "groq": "openai/gpt-oss-120b",
    "cerebras": "zai-glm-4.6",
    "zai": "glm-4.6",
    "mistral": "devstral-medium-latest",
    "minimax": "MiniMax-M2.1",
    "minimax-cn": "MiniMax-M2.1",
    "huggingface": "moonshotai/Kimi-K2.5",
    "opencode": "claude-opus-4-6",
    "kimi-coding": "kimi-k2-thinking",
}

_DEFAULT_THINKING_LEVEL: ThinkingLevel = "low"

_VALID_THINKING_LEVELS: set[str] = {"off", "minimal", "low", "medium", "high", "xhigh"}


def _is_valid_thinking_level(s: str) -> bool:
    return s in _VALID_THINKING_LEVELS


# ============================================================================
# Types
# ============================================================================


@dataclass
class ScopedModel:
    """A model with an optional thinking level."""

    model: Model
    thinking_level: ThinkingLevel | None = None


@dataclass
class ParsedModelResult:
    """Result of parsing a model pattern."""

    model: Model | None
    thinking_level: ThinkingLevel | None
    warning: str | None


@dataclass
class ResolveCliModelResult:
    """Result of resolving a model from CLI flags."""

    model: Model | None
    thinking_level: ThinkingLevel | None
    warning: str | None
    error: str | None


@dataclass
class InitialModelResult:
    """Result of finding the initial model to use."""

    model: Model | None
    thinking_level: ThinkingLevel
    fallback_message: str | None


# ============================================================================
# Model matching
# ============================================================================


def _is_alias(model_id: str) -> bool:
    """Check if a model ID looks like an alias (no date suffix)."""
    import re

    if model_id.endswith("-latest"):
        return True
    date_pattern = re.compile(r"-\d{8}$")
    return not bool(date_pattern.search(model_id))


def _try_match_model(pattern: str, available_models: list[Model]) -> Model | None:
    """Try to match a pattern to a model from the available models list."""
    # Check for provider/modelId format
    slash_index = pattern.find("/")
    if slash_index != -1:
        provider = pattern[:slash_index]
        model_id = pattern[slash_index + 1 :]
        provider_match = next(
            (
                m
                for m in available_models
                if m.provider.lower() == provider.lower() and m.id.lower() == model_id.lower()
            ),
            None,
        )
        if provider_match:
            return provider_match

    # Exact ID match (case-insensitive)
    exact = next((m for m in available_models if m.id.lower() == pattern.lower()), None)
    if exact:
        return exact

    # Partial matching
    lower = pattern.lower()
    matches = [m for m in available_models if lower in m.id.lower() or (m.name and lower in m.name.lower())]

    if not matches:
        return None

    aliases = [m for m in matches if _is_alias(m.id)]
    dated = [m for m in matches if not _is_alias(m.id)]

    if aliases:
        aliases.sort(key=lambda m: m.id, reverse=True)
        return aliases[0]
    else:
        dated.sort(key=lambda m: m.id, reverse=True)
        return dated[0]


def parse_model_pattern(
    pattern: str,
    available_models: list[Model],
    options: dict[str, Any] | None = None,
) -> ParsedModelResult:
    """Parse a pattern to extract model and thinking level.

    Handles models with colons in their IDs (e.g., OpenRouter's :exacto suffix).

    Algorithm:
    1. Try to match full pattern as a model
    2. If found, return it with no thinking level
    3. If not found and has colons, split on last colon:
       - If suffix is valid thinking level, use it and recurse on prefix
       - If suffix is invalid, warn and recurse on prefix
    """
    allow_fallback = (options or {}).get("allowInvalidThinkingLevelFallback", True)

    # Try exact match first
    exact = _try_match_model(pattern, available_models)
    if exact:
        return ParsedModelResult(model=exact, thinking_level=None, warning=None)

    # No match - try splitting on last colon
    last_colon = pattern.rfind(":")
    if last_colon == -1:
        return ParsedModelResult(model=None, thinking_level=None, warning=None)

    prefix = pattern[:last_colon]
    suffix = pattern[last_colon + 1 :]

    if _is_valid_thinking_level(suffix):
        result = parse_model_pattern(prefix, available_models, options)
        if result.model:
            return ParsedModelResult(
                model=result.model,
                thinking_level=None if result.warning else suffix,  # type: ignore[arg-type]
                warning=result.warning,
            )
        return result
    else:
        if not allow_fallback:
            return ParsedModelResult(model=None, thinking_level=None, warning=None)

        result = parse_model_pattern(prefix, available_models, options)
        if result.model:
            return ParsedModelResult(
                model=result.model,
                thinking_level=None,
                warning=f'Invalid thinking level "{suffix}" in pattern "{pattern}". Using default instead.',
            )
        return result


# ============================================================================
# Model scope resolution
# ============================================================================


async def resolve_model_scope(
    patterns: list[str],
    model_registry: ModelRegistry,
) -> list[ScopedModel]:
    """Resolve model patterns to actual Model objects with optional thinking levels.

    Supports glob patterns and provider/model format.
    """
    available_models = model_registry.get_available()
    scoped_models: list[ScopedModel] = []

    def _models_are_equal(a: Model, b: Model) -> bool:
        return a.provider == b.provider and a.id == b.id

    for pattern in patterns:
        # Check if pattern contains glob characters
        if any(c in pattern for c in ("*", "?", "[")):
            # Extract optional thinking level suffix
            colon_idx = pattern.rfind(":")
            glob_pattern = pattern
            thinking_level: ThinkingLevel | None = None

            if colon_idx != -1:
                suffix = pattern[colon_idx + 1 :]
                if _is_valid_thinking_level(suffix):
                    thinking_level = suffix  # type: ignore[assignment]
                    glob_pattern = pattern[:colon_idx]

            # Match against "provider/modelId" format OR just model ID
            matching = [
                m
                for m in available_models
                if fnmatch.fnmatch(f"{m.provider}/{m.id}", glob_pattern) or fnmatch.fnmatch(m.id, glob_pattern)
            ]

            if not matching:
                print(f'Warning: No models match pattern "{pattern}"', file=sys.stderr)
                continue

            for model in matching:
                if not any(_models_are_equal(sm.model, model) for sm in scoped_models):
                    scoped_models.append(ScopedModel(model=model, thinking_level=thinking_level))
            continue

        result = parse_model_pattern(pattern, available_models)

        if result.warning:
            print(f"Warning: {result.warning}", file=sys.stderr)

        if not result.model:
            print(f'Warning: No models match pattern "{pattern}"', file=sys.stderr)
            continue

        if not any(_models_are_equal(sm.model, result.model) for sm in scoped_models):
            scoped_models.append(
                ScopedModel(
                    model=result.model,
                    thinking_level=result.thinking_level,
                )
            )

    return scoped_models


# ============================================================================
# CLI model resolution
# ============================================================================


def resolve_cli_model(
    cli_provider: str | None,
    cli_model: str | None,
    model_registry: ModelRegistry,
) -> ResolveCliModelResult:
    """Resolve a single model from CLI flags.

    Supports:
    - --provider <provider> --model <pattern>
    - --model <provider>/<pattern>
    - Fuzzy matching
    """
    if not cli_model:
        return ResolveCliModelResult(model=None, thinking_level=None, warning=None, error=None)

    # Use all models here, not just models with pre-configured auth
    available_models = model_registry.get_all()
    if not available_models:
        return ResolveCliModelResult(
            model=None,
            thinking_level=None,
            warning=None,
            error="No models available. Check your installation or add models to models.json.",
        )

    # Build canonical provider lookup (case-insensitive)
    provider_map: dict[str, str] = {}
    for m in available_models:
        provider_map[m.provider.lower()] = m.provider

    provider: str | None = None
    if cli_provider:
        provider = provider_map.get(cli_provider.lower())
        if not provider:
            return ResolveCliModelResult(
                model=None,
                thinking_level=None,
                warning=None,
                error=f'Unknown provider "{cli_provider}". Use --list-models to see available providers/models.',
            )

    # If no explicit --provider, first try exact matches
    if not provider:
        lower = cli_model.lower()
        exact = next(
            (m for m in available_models if m.id.lower() == lower or f"{m.provider}/{m.id}".lower() == lower),
            None,
        )
        if exact:
            return ResolveCliModelResult(
                model=exact,
                thinking_level=None,
                warning=None,
                error=None,
            )

    pattern = cli_model

    # If no explicit --provider, allow --model provider/<pattern>
    if not provider:
        slash_idx = cli_model.find("/")
        if slash_idx != -1:
            maybe_provider = cli_model[:slash_idx]
            canonical = provider_map.get(maybe_provider.lower())
            if canonical:
                provider = canonical
                pattern = cli_model[slash_idx + 1 :]
    else:
        # If both were provided, tolerate --model <provider>/<pattern>
        prefix = f"{provider}/"
        if cli_model.lower().startswith(prefix.lower()):
            pattern = cli_model[len(prefix) :]

    candidates = [m for m in available_models if m.provider == provider] if provider else available_models
    result = parse_model_pattern(
        pattern,
        candidates,
        options={"allowInvalidThinkingLevelFallback": False},
    )

    if not result.model:
        display = f"{provider}/{pattern}" if provider else cli_model
        return ResolveCliModelResult(
            model=None,
            thinking_level=None,
            warning=result.warning,
            error=f'Model "{display}" not found. Use --list-models to see available models.',
        )

    return ResolveCliModelResult(
        model=result.model,
        thinking_level=result.thinking_level,
        warning=result.warning,
        error=None,
    )


# ============================================================================
# Initial model selection
# ============================================================================


async def find_initial_model(options: dict[str, Any]) -> InitialModelResult:
    """Find the initial model to use based on priority.

    Priority:
    1. CLI args (provider + model)
    2. First model from scoped models (if not continuing/resuming)
    3. Saved default from settings
    4. First available model with valid API key
    """
    cli_provider: str | None = options.get("cli_provider")
    cli_model: str | None = options.get("cli_model")
    scoped_models: list[ScopedModel] = options.get("scoped_models", [])
    is_continuing: bool = options.get("is_continuing", False)
    default_provider: str | None = options.get("default_provider")
    default_model_id: str | None = options.get("default_model_id")
    default_thinking_level: ThinkingLevel | None = options.get("default_thinking_level")
    model_registry: ModelRegistry = options["model_registry"]

    # 1. CLI args take priority
    if cli_provider and cli_model:
        found = model_registry.find(cli_provider, cli_model)
        if not found:
            raise RuntimeError(f"Model {cli_provider}/{cli_model} not found")
        return InitialModelResult(
            model=found,
            thinking_level=_DEFAULT_THINKING_LEVEL,
            fallback_message=None,
        )

    # 2. Use first model from scoped models (skip if continuing/resuming)
    if scoped_models and not is_continuing:
        first = scoped_models[0]
        return InitialModelResult(
            model=first.model,
            thinking_level=first.thinking_level or default_thinking_level or _DEFAULT_THINKING_LEVEL,
            fallback_message=None,
        )

    # 3. Try saved default from settings
    if default_provider and default_model_id:
        found = model_registry.find(default_provider, default_model_id)
        if found:
            return InitialModelResult(
                model=found,
                thinking_level=default_thinking_level or _DEFAULT_THINKING_LEVEL,
                fallback_message=None,
            )

    # 4. Try first available model with valid API key
    available_models = model_registry.get_available()

    if available_models:
        # Try to find a default model from known providers
        for provider_name, default_id in DEFAULT_MODEL_PER_PROVIDER.items():
            match = next(
                (m for m in available_models if m.provider == provider_name and m.id == default_id),
                None,
            )
            if match:
                return InitialModelResult(
                    model=match,
                    thinking_level=_DEFAULT_THINKING_LEVEL,
                    fallback_message=None,
                )

        # If no default found, use first available
        return InitialModelResult(
            model=available_models[0],
            thinking_level=_DEFAULT_THINKING_LEVEL,
            fallback_message=None,
        )

    # 5. No model found
    return InitialModelResult(
        model=None,
        thinking_level=_DEFAULT_THINKING_LEVEL,
        fallback_message=None,
    )


# ============================================================================
# Session model restoration
# ============================================================================


async def restore_model_from_session(
    saved_provider: str,
    saved_model_id: str,
    current_model: Model | None,
    should_print_messages: bool,
    model_registry: ModelRegistry,
) -> tuple[Model | None, str | None]:
    """Restore model from session, with fallback to available models."""
    restored = model_registry.find(saved_provider, saved_model_id)

    has_api_key = False
    if restored:
        key = await model_registry.get_api_key(restored)
        has_api_key = bool(key)

    if restored and has_api_key:
        if should_print_messages:
            print(f"Restored model: {saved_provider}/{saved_model_id}", file=sys.stderr)
        return restored, None

    reason = "model no longer exists" if not restored else "no API key available"

    if should_print_messages:
        print(f"Warning: Could not restore model {saved_provider}/{saved_model_id} ({reason}).", file=sys.stderr)

    if current_model:
        if should_print_messages:
            print(f"Falling back to: {current_model.provider}/{current_model.id}", file=sys.stderr)
        return current_model, (
            f"Could not restore model {saved_provider}/{saved_model_id} ({reason}). "
            f"Using {current_model.provider}/{current_model.id}."
        )

    available_models = model_registry.get_available()

    if available_models:
        fallback: Model | None = None
        for provider_name, default_id in DEFAULT_MODEL_PER_PROVIDER.items():
            match = next(
                (m for m in available_models if m.provider == provider_name and m.id == default_id),
                None,
            )
            if match:
                fallback = match
                break

        if not fallback:
            fallback = available_models[0]

        if should_print_messages:
            print(f"Falling back to: {fallback.provider}/{fallback.id}", file=sys.stderr)

        return fallback, (
            f"Could not restore model {saved_provider}/{saved_model_id} ({reason}). "
            f"Using {fallback.provider}/{fallback.id}."
        )

    return None, None
