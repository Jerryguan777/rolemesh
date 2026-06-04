"""Model registry - manages built-in and custom models, provides API key resolution.

Port of packages/coding-agent/src/core/model-registry.ts.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pi.ai import (
    get_models,
    get_providers,
    register_api_provider,
    register_oauth_provider,
)
from pi.ai.types import Context, Model
from pi.coding_agent.core.auth_storage import AuthStorage
from pi.coding_agent.core.config import get_agent_dir

# ============================================================================
# Types
# ============================================================================


@dataclass
class ModelOverride:
    """Per-model override config (all fields optional)."""

    name: str | None = None
    reasoning: bool | None = None
    input: list[str] | None = None
    cost: dict[str, float | None] | None = None
    context_window: int | None = None
    max_tokens: int | None = None
    headers: dict[str, str] | None = None
    compat: Any | None = None


@dataclass
class ProviderOverride:
    """Provider override config (baseUrl, headers, apiKey) without custom models."""

    base_url: str | None = None
    headers: dict[str, str] | None = None
    api_key: str | None = None


@dataclass
class ProviderConfigInput:
    """Input type for register_provider API."""

    base_url: str | None = None
    api_key: str | None = None
    api: str | None = None
    headers: dict[str, str] | None = None
    auth_header: bool | None = None
    oauth: Any | None = None  # OAuthProviderInterface without id
    models: list[dict[str, Any]] | None = None
    stream_simple: Any | None = None  # Callable


# ============================================================================
# Helpers
# ============================================================================

_DEFAULT_COST = {"input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0}


def _resolve_config_value(value: str) -> str:
    """Resolve a config value, expanding ${ENV_VAR} references."""
    import os
    import re

    def replacer(m: re.Match) -> str:  # type: ignore[type-arg]
        env_var = m.group(1)
        return os.environ.get(env_var, m.group(0))

    return re.sub(r"\$\{([^}]+)\}", replacer, value)


def _resolve_headers(headers: dict[str, str] | None) -> dict[str, str] | None:
    """Resolve headers, expanding ${ENV_VAR} references in values."""
    if not headers:
        return None
    return {k: _resolve_config_value(v) for k, v in headers.items()}


def _apply_model_override(model: Model, override: ModelOverride) -> Model:
    """Deep merge a model override into a model."""
    import copy

    result = copy.copy(model)

    if override.name is not None:
        result.name = override.name
    if override.reasoning is not None:
        result.reasoning = override.reasoning
    if override.input is not None:
        result.input = override.input
    if override.context_window is not None:
        result.context_window = override.context_window
    if override.max_tokens is not None:
        result.max_tokens = override.max_tokens

    if override.cost is not None:
        base_cost = getattr(model, "cost", None)
        if base_cost and isinstance(base_cost, dict):
            merged_cost = dict(base_cost)
            for k, v in override.cost.items():
                if v is not None:
                    merged_cost[k] = v
            result.cost = merged_cost  # type: ignore[assignment]

    if override.headers is not None:
        resolved = _resolve_headers(override.headers)
        if resolved:
            existing = getattr(model, "headers", None) or {}
            result.headers = {**existing, **resolved}

    if override.compat is not None:
        existing_compat = getattr(model, "compat", None)
        if existing_compat and isinstance(existing_compat, dict) and isinstance(override.compat, dict):
            result.compat = {**existing_compat, **override.compat}  # type: ignore[assignment]
        else:
            result.compat = override.compat

    return result


def _load_models_json(
    path: Path,
) -> tuple[list[Model], dict[str, ProviderOverride], dict[str, dict[str, ModelOverride]], str | None]:
    """Load and parse models.json.

    Returns (custom_models, provider_overrides, model_overrides, error).
    """
    if not path.exists():
        return [], {}, {}, None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return [], {}, {}, f"Failed to read models.json: {e}"

    if not isinstance(raw, dict):
        return [], {}, {}, "models.json must be an object"

    providers_raw = raw.get("providers")
    if not isinstance(providers_raw, dict):
        return [], {}, {}, None

    custom_models: list[Model] = []
    provider_overrides: dict[str, ProviderOverride] = {}
    model_overrides: dict[str, dict[str, ModelOverride]] = {}

    for provider_name, provider_cfg in providers_raw.items():
        if not isinstance(provider_cfg, dict):
            continue

        base_url = provider_cfg.get("baseUrl")
        api_key_cfg = provider_cfg.get("apiKey")
        api = provider_cfg.get("api")
        headers_raw = provider_cfg.get("headers")
        models_raw = provider_cfg.get("models")
        model_overrides_raw = provider_cfg.get("modelOverrides")

        # Store provider override
        provider_overrides[provider_name] = ProviderOverride(
            base_url=base_url,
            headers=headers_raw,
            api_key=api_key_cfg,
        )

        # Parse per-model overrides
        if isinstance(model_overrides_raw, dict):
            provider_mo: dict[str, ModelOverride] = {}
            for model_id, mo_raw in model_overrides_raw.items():
                if not isinstance(mo_raw, dict):
                    continue
                cost_raw = mo_raw.get("cost")
                cost: dict[str, float | None] | None = None
                if isinstance(cost_raw, dict):
                    cost = {
                        "input": cost_raw.get("input"),
                        "output": cost_raw.get("output"),
                        "cacheRead": cost_raw.get("cacheRead"),
                        "cacheWrite": cost_raw.get("cacheWrite"),
                    }
                provider_mo[model_id] = ModelOverride(
                    name=mo_raw.get("name"),
                    reasoning=mo_raw.get("reasoning"),
                    input=mo_raw.get("input"),
                    cost=cost,
                    context_window=mo_raw.get("contextWindow"),
                    max_tokens=mo_raw.get("maxTokens"),
                    headers=mo_raw.get("headers"),
                    compat=mo_raw.get("compat"),
                )
            if provider_mo:
                model_overrides[provider_name] = provider_mo

        # Parse custom models
        if isinstance(models_raw, list) and base_url:
            resolved_headers = _resolve_headers(headers_raw)

            for model_def in models_raw:
                if not isinstance(model_def, dict):
                    continue
                model_id = model_def.get("id")
                if not model_id:
                    continue

                model_api = model_def.get("api") or api
                if not model_api:
                    continue

                cost_raw = model_def.get("cost")
                if isinstance(cost_raw, dict):
                    cost_obj: dict[str, float] = {
                        "input": cost_raw.get("input", 0.0),
                        "output": cost_raw.get("output", 0.0),
                        "cacheRead": cost_raw.get("cacheRead", 0.0),
                        "cacheWrite": cost_raw.get("cacheWrite", 0.0),
                    }
                else:
                    cost_obj = {"input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0}

                model_headers_raw = model_def.get("headers")
                model_headers = _resolve_headers(model_headers_raw)
                merged_headers: dict[str, str] | None = None
                if resolved_headers or model_headers:
                    merged_headers = {**(resolved_headers or {}), **(model_headers or {})}

                model = Model(
                    id=model_id,
                    name=model_def.get("name") or model_id,
                    api=model_api,
                    provider=provider_name,
                    base_url=base_url,
                    reasoning=model_def.get("reasoning", False),
                    input=model_def.get("input", ["text"]),
                    cost=cost_obj,  # type: ignore[arg-type]
                    context_window=model_def.get("contextWindow", 128000),
                    max_tokens=model_def.get("maxTokens", 16384),
                    headers=merged_headers,
                    compat=model_def.get("compat"),
                )
                custom_models.append(model)

    return custom_models, provider_overrides, model_overrides, None


# ============================================================================
# No-op for API key cache clearing (Python doesn't cache the same way)
# ============================================================================


def clear_api_key_cache() -> None:
    """Clear the API key cache. No-op in Python (no process-level caching)."""
    pass


# ============================================================================
# ModelRegistry
# ============================================================================


class ModelRegistry:
    """Model registry - loads and manages models, resolves API keys via AuthStorage."""

    def __init__(
        self,
        auth_storage: AuthStorage,
        models_json_path: Path | None = None,
    ) -> None:
        self.auth_storage = auth_storage
        self._models_json_path = models_json_path or (get_agent_dir() / "models.json")
        self._models: list[Model] = []
        self._custom_provider_api_keys: dict[str, str] = {}
        self._registered_providers: dict[str, ProviderConfigInput] = {}
        self._load_error: str | None = None

        # Set up fallback resolver for custom provider API keys
        self.auth_storage.set_fallback_resolver(self._resolve_custom_provider_key)

        # Load models
        self._load_models()

    def _resolve_custom_provider_key(self, provider: str) -> str | None:
        """Resolve API key for custom providers from models.json config."""
        key_cfg = self._custom_provider_api_keys.get(provider)
        if key_cfg:
            return _resolve_config_value(key_cfg)
        return None

    def _load_models(self) -> None:
        """Load built-in models plus custom models from models.json."""
        # Start with built-in models
        all_built_in = [m for p in get_providers() for m in get_models(p)]
        models_list = list(all_built_in)

        # Load custom models from models.json
        custom_models, provider_overrides, model_overrides, error = _load_models_json(self._models_json_path)

        if error:
            self._load_error = error
            self._models = models_list
            return

        self._load_error = None

        # Apply provider overrides (baseUrl/headers/apiKey) to built-in models
        for provider_name, override in provider_overrides.items():
            if not custom_models or not any(m.provider == provider_name for m in custom_models):
                # Only apply override if no custom models for this provider
                if override.api_key:
                    self._custom_provider_api_keys[provider_name] = override.api_key
                resolved_headers = _resolve_headers(override.headers)
                models_list = [
                    Model(
                        id=m.id,
                        name=m.name,
                        api=m.api,
                        provider=m.provider,
                        base_url=override.base_url or m.base_url,
                        reasoning=m.reasoning,
                        input=m.input,
                        cost=m.cost,
                        context_window=m.context_window,
                        max_tokens=m.max_tokens,
                        headers={**(m.headers or {}), **(resolved_headers or {})} if resolved_headers else m.headers,
                        compat=m.compat,
                    )
                    if m.provider == provider_name and override.base_url
                    else m
                    for m in models_list
                ]

        # Apply per-model overrides to built-in models
        for provider_name, provider_mo in model_overrides.items():
            models_list = [
                _apply_model_override(m, provider_mo[m.id])
                if m.provider == provider_name and m.id in provider_mo
                else m
                for m in models_list
            ]

        # Remove built-in models for providers with custom models, then add custom models
        providers_with_custom = {m.provider for m in custom_models}
        for provider_name in providers_with_custom:
            models_list = [m for m in models_list if m.provider != provider_name]

        # Add custom provider API keys
        for m in custom_models:
            custom_override = provider_overrides.get(m.provider)
            if custom_override and custom_override.api_key:
                self._custom_provider_api_keys[m.provider] = custom_override.api_key

        models_list.extend(custom_models)
        self._models = models_list

    def refresh(self) -> None:
        """Reload models from disk (built-in + custom from models.json)."""
        self._custom_provider_api_keys.clear()
        self._load_error = None
        self._load_models()

        for provider_name, config in self._registered_providers.items():
            self._apply_provider_config(provider_name, config)

    def get_error(self) -> str | None:
        """Get any error that occurred during model loading."""
        return self._load_error

    def get_all(self) -> list[Model]:
        """Get all models (built-in + custom)."""
        return list(self._models)

    def get_available(self) -> list[Model]:
        """Get only models that have auth configured."""
        return [m for m in self._models if self.auth_storage.has_auth(m.provider)]

    def find(self, provider: str, model_id: str) -> Model | None:
        """Find a model by provider and ID."""
        for m in self._models:
            if m.provider == provider and m.id == model_id:
                return m
        return None

    async def get_api_key(self, model: Model) -> str | None:
        """Get API key for a model."""
        return await self.auth_storage.get_api_key(model.provider)

    async def get_api_key_for_provider(self, provider: str) -> str | None:
        """Get API key for a provider."""
        return await self.auth_storage.get_api_key(provider)

    def is_using_oauth(self, model: Model) -> bool:
        """Check if a model is using OAuth credentials (subscription)."""
        cred = self.auth_storage.get(model.provider)
        return cred is not None and getattr(cred, "type", None) == "oauth"

    def register_provider(self, provider_name: str, config: ProviderConfigInput) -> None:
        """Register a provider dynamically (from extensions).

        If provider has models: replaces all existing models for this provider.
        If provider has only baseUrl/headers: overrides existing models' URLs.
        If provider has oauth: registers OAuth provider for /login support.
        """
        self._registered_providers[provider_name] = config
        self._apply_provider_config(provider_name, config)

    def _apply_provider_config(self, provider_name: str, config: ProviderConfigInput) -> None:
        """Apply a provider configuration to models."""
        # Register OAuth provider if provided
        if config.oauth:
            try:

                class _WrappedProvider:
                    def __init__(self, base: Any, pid: str) -> None:
                        self._base = base
                        self._id = pid

                    @property
                    def id(self) -> str:
                        return self._id

                    @property
                    def name(self) -> str:
                        return getattr(self._base, "name", self._id)

                    @property
                    def uses_callback_server(self) -> bool:
                        return getattr(self._base, "uses_callback_server", False)

                    async def login(self, callbacks: Any) -> Any:
                        return await self._base.login(callbacks)

                    async def refresh_token(self, credentials: Any) -> Any:
                        return await self._base.refresh_token(credentials)

                    def get_api_key(self, credentials: Any) -> str:
                        return self._base.get_api_key(credentials)  # type: ignore[no-any-return]

                    def modify_models(self, models: list[Any], credentials: Any) -> list[Any]:
                        if hasattr(self._base, "modify_models"):
                            return self._base.modify_models(models, credentials)  # type: ignore[no-any-return]
                        return models

                wrapped = _WrappedProvider(config.oauth, provider_name)
                register_oauth_provider(wrapped)
            except Exception:
                pass

        # Register custom stream function
        if config.stream_simple:
            if not config.api:
                raise ValueError(f'Provider {provider_name}: "api" is required when registering stream_simple.')

            def make_stream(fn: Any, api: str) -> Any:
                from pi.ai.api_registry import ApiProvider

                async def _stream(model: Model, context: Context, options: Any) -> Any:
                    return fn(model, context, options)

                return ApiProvider(api=api, stream=_stream, stream_simple=fn)  # type: ignore[arg-type]

            provider_obj = make_stream(config.stream_simple, config.api)
            register_api_provider(provider_obj)

        # Store API key for auth resolution
        if config.api_key:
            self._custom_provider_api_keys[provider_name] = config.api_key

        if config.models and len(config.models) > 0:
            # Full replacement: remove existing models for this provider
            self._models = [m for m in self._models if m.provider != provider_name]

            if not config.base_url:
                raise ValueError(f'Provider {provider_name}: "base_url" is required when defining models.')
            if not config.api_key and not config.oauth:
                raise ValueError(f'Provider {provider_name}: "api_key" or "oauth" is required when defining models.')

            resolved_headers = _resolve_headers(config.headers)

            for model_def in config.models:
                model_api = model_def.get("api") or config.api
                if not model_api:
                    raise ValueError(f'Provider {provider_name}, model {model_def.get("id")}: no "api" specified.')

                model_headers_raw = model_def.get("headers")
                model_headers = _resolve_headers(model_headers_raw)
                merged_headers: dict[str, str] | None = None
                if resolved_headers or model_headers:
                    merged_headers = {**(resolved_headers or {}), **(model_headers or {})}

                # If auth_header is true, add Authorization header
                if config.auth_header and config.api_key:
                    resolved_key = _resolve_config_value(config.api_key)
                    if resolved_key:
                        merged_headers = {**(merged_headers or {}), "Authorization": f"Bearer {resolved_key}"}

                cost_raw = model_def.get("cost")
                if isinstance(cost_raw, dict):
                    cost: Any = cost_raw
                else:
                    cost = {"input": 0.0, "output": 0.0, "cacheRead": 0.0, "cacheWrite": 0.0}

                model = Model(
                    id=model_def["id"],
                    name=model_def.get("name") or model_def["id"],
                    api=model_api,
                    provider=provider_name,
                    base_url=config.base_url,
                    reasoning=model_def.get("reasoning", False),
                    input=model_def.get("input", ["text"]),
                    cost=cost,
                    context_window=model_def.get("context_window", model_def.get("contextWindow", 128000)),
                    max_tokens=model_def.get("max_tokens", model_def.get("maxTokens", 16384)),
                    headers=merged_headers,
                    compat=model_def.get("compat"),
                )
                self._models.append(model)

            # Apply OAuth modifyModels if credentials exist
            if config.oauth and hasattr(config.oauth, "modify_models"):
                cred = self.auth_storage.get(provider_name)
                if cred and getattr(cred, "type", None) == "oauth":
                    self._models = config.oauth.modify_models(self._models, cred)

        elif config.base_url:
            # Override-only: update baseUrl/headers for existing models
            resolved_headers = _resolve_headers(config.headers)
            self._models = [
                Model(
                    id=m.id,
                    name=m.name,
                    api=m.api,
                    provider=m.provider,
                    base_url=config.base_url or m.base_url,
                    reasoning=m.reasoning,
                    input=m.input,
                    cost=m.cost,
                    context_window=m.context_window,
                    max_tokens=m.max_tokens,
                    headers={**(m.headers or {}), **(resolved_headers or {})} if resolved_headers else m.headers,
                    compat=m.compat,
                )
                if m.provider == provider_name
                else m
                for m in self._models
            ]
