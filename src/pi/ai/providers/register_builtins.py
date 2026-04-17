"""Built-in provider registration — Python port of providers/register-builtins.ts."""

from __future__ import annotations

from typing import cast

from pi.ai.api_registry import (
    ApiProvider,
    StreamFunction,
    StreamSimpleFunction,
    clear_api_providers,
    register_api_provider,
)
from pi.ai.providers.amazon_bedrock import stream_bedrock, stream_simple_bedrock
from pi.ai.providers.anthropic import stream_anthropic, stream_simple_anthropic
from pi.ai.providers.azure_openai_responses import stream_azure_openai_responses, stream_simple_azure_openai_responses
from pi.ai.providers.google import stream_google, stream_simple_google
from pi.ai.providers.google_gemini_cli import stream_google_gemini_cli, stream_simple_google_gemini_cli
from pi.ai.providers.google_vertex import stream_google_vertex, stream_simple_google_vertex
from pi.ai.providers.openai_codex_responses import stream_openai_codex_responses, stream_simple_openai_codex_responses
from pi.ai.providers.openai_completions import stream_openai_completions, stream_simple_openai_completions
from pi.ai.providers.openai_responses import stream_openai_responses, stream_simple_openai_responses

# Built-in API names registered by register_built_in_api_providers()
BUILT_IN_APIS: list[str] = [
    "anthropic-messages",
    "openai-completions",
    "openai-responses",
    "azure-openai-responses",
    "openai-codex-responses",
    "google-generative-ai",
    "google-gemini-cli",
    "google-vertex",
    "bedrock-converse-stream",
]


def register_built_in_api_providers() -> None:
    """Register all built-in API providers.

    Each provider's stream function accepts a provider-specific options subtype.
    We cast to the base StreamFunction/StreamSimpleFunction type so the registry
    can store them uniformly. The api_registry wrapper validates model.api at
    call time, ensuring only matching models are ever passed to each provider.
    """
    # cast: each provider accepts a narrower XxxOptions subtype; the registry
    # stores the base type. Runtime safety is enforced by the model.api check
    # in api_registry.register_api_provider's wrapper.
    register_api_provider(
        ApiProvider(
            api="anthropic-messages",
            stream=cast(StreamFunction, stream_anthropic),
            stream_simple=cast(StreamSimpleFunction, stream_simple_anthropic),
        )
    )
    register_api_provider(
        ApiProvider(
            api="openai-completions",
            stream=cast(StreamFunction, stream_openai_completions),
            stream_simple=cast(StreamSimpleFunction, stream_simple_openai_completions),
        )
    )
    register_api_provider(
        ApiProvider(
            api="openai-responses",
            stream=cast(StreamFunction, stream_openai_responses),
            stream_simple=cast(StreamSimpleFunction, stream_simple_openai_responses),
        )
    )
    register_api_provider(
        ApiProvider(
            api="azure-openai-responses",
            stream=cast(StreamFunction, stream_azure_openai_responses),
            stream_simple=cast(StreamSimpleFunction, stream_simple_azure_openai_responses),
        )
    )
    register_api_provider(
        ApiProvider(
            api="openai-codex-responses",
            stream=cast(StreamFunction, stream_openai_codex_responses),
            stream_simple=cast(StreamSimpleFunction, stream_simple_openai_codex_responses),
        )
    )
    register_api_provider(
        ApiProvider(
            api="google-generative-ai",
            stream=cast(StreamFunction, stream_google),
            stream_simple=cast(StreamSimpleFunction, stream_simple_google),
        )
    )
    register_api_provider(
        ApiProvider(
            api="google-gemini-cli",
            stream=cast(StreamFunction, stream_google_gemini_cli),
            stream_simple=cast(StreamSimpleFunction, stream_simple_google_gemini_cli),
        )
    )
    register_api_provider(
        ApiProvider(
            api="google-vertex",
            stream=cast(StreamFunction, stream_google_vertex),
            stream_simple=cast(StreamSimpleFunction, stream_simple_google_vertex),
        )
    )
    register_api_provider(
        ApiProvider(
            api="bedrock-converse-stream",
            stream=cast(StreamFunction, stream_bedrock),
            stream_simple=cast(StreamSimpleFunction, stream_simple_bedrock),
        )
    )


def reset_api_providers() -> None:
    """Clear and re-register all built-in providers."""
    clear_api_providers()
    register_built_in_api_providers()
