"""Environment variable API key detection — Python port of packages/ai/src/env-api-keys.ts."""

from __future__ import annotations

import os
from pathlib import Path

_cached_vertex_adc_exists: bool | None = None


def _has_vertex_adc_credentials() -> bool:
    global _cached_vertex_adc_exists
    if _cached_vertex_adc_exists is None:
        gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if gac_path:
            _cached_vertex_adc_exists = Path(gac_path).exists()
        else:
            default_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
            _cached_vertex_adc_exists = default_path.exists()
    return _cached_vertex_adc_exists


def reset_vertex_adc_cache() -> None:
    """Reset the cached Vertex ADC credentials check (for testing)."""
    global _cached_vertex_adc_exists
    _cached_vertex_adc_exists = None


# Provider -> env var name mapping
_ENV_MAP: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "azure-openai-responses": "AZURE_OPENAI_API_KEY",
    "google": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "cerebras": "CEREBRAS_API_KEY",
    "xai": "XAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "vercel-ai-gateway": "AI_GATEWAY_API_KEY",
    "zai": "ZAI_API_KEY",
    "mistral": "MISTRAL_API_KEY",
    "minimax": "MINIMAX_API_KEY",
    "minimax-cn": "MINIMAX_CN_API_KEY",
    "huggingface": "HF_TOKEN",
    "opencode": "OPENCODE_API_KEY",
    "kimi-coding": "KIMI_API_KEY",
}


def get_env_api_key(provider: str) -> str | None:
    """Get API key for provider from known environment variables.

    Will not return API keys for providers that require OAuth tokens.
    """
    if provider == "github-copilot":
        return os.environ.get("COPILOT_GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")

    if provider == "anthropic":
        return os.environ.get("ANTHROPIC_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")

    if provider == "google-vertex":
        has_credentials = _has_vertex_adc_credentials()
        has_project = bool(os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("GCLOUD_PROJECT"))
        has_location = bool(os.environ.get("GOOGLE_CLOUD_LOCATION"))
        if has_credentials and has_project and has_location:
            return "<authenticated>"
        return None

    if provider == "amazon-bedrock":
        if (
            os.environ.get("AWS_PROFILE")
            or (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))
            or os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
            or os.environ.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
            or os.environ.get("AWS_CONTAINER_CREDENTIALS_FULL_URI")
            or os.environ.get("AWS_WEB_IDENTITY_TOKEN_FILE")
        ):
            return "<authenticated>"
        return None

    env_var = _ENV_MAP.get(provider)
    if env_var:
        return os.environ.get(env_var)
    return None
