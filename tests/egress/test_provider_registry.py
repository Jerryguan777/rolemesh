"""Tests for ``_build_provider_registry`` — multi-provider upstream
URL resolution + base-URL overrides.

These pin the contract that operators can point a provider at a
local/private upstream (e.g. an LLM proxy on a VPC-internal host)
via ``*_BASE_URL`` env. The gateway's registry must honour the
override; the launcher's ``_gateway_env`` then does the loopback
rewrite at the publish boundary so the URL still resolves inside
the container.
"""

from __future__ import annotations

from rolemesh.egress.reverse_proxy import _build_provider_registry


# ---------------------------------------------------------------------------
# Anthropic — already supported pre-this-PR, kept as a regression
# baseline so a future refactor can't quietly break the override.
# ---------------------------------------------------------------------------


def test_anthropic_base_url_override_takes_effect() -> None:
    secrets = {
        "ANTHROPIC_API_KEY": "sk-ant-...",
        "ANTHROPIC_BASE_URL": "https://my.anthropic-proxy.example.com",
    }
    registry = _build_provider_registry(secrets, "api-key")
    assert (
        registry["anthropic"].upstream
        == "https://my.anthropic-proxy.example.com"
    )


def test_anthropic_default_when_no_override() -> None:
    secrets = {"ANTHROPIC_API_KEY": "sk-ant-..."}
    registry = _build_provider_registry(secrets, "api-key")
    assert registry["anthropic"].upstream == "https://api.anthropic.com"


# ---------------------------------------------------------------------------
# OpenAI base URL override (added in this PR)
# ---------------------------------------------------------------------------


def test_openai_base_url_override_takes_effect() -> None:
    secrets = {
        "PI_OPENAI_API_KEY": "sk-...",
        "OPENAI_BASE_URL": "https://my.openai-compat.example.com/v1",
    }
    registry = _build_provider_registry(secrets, "api-key")
    assert (
        registry["openai"].upstream
        == "https://my.openai-compat.example.com/v1"
    )


def test_openai_default_when_no_override() -> None:
    secrets = {"PI_OPENAI_API_KEY": "sk-..."}
    registry = _build_provider_registry(secrets, "api-key")
    assert registry["openai"].upstream == "https://api.openai.com/v1"


def test_openai_base_url_ignored_without_api_key() -> None:
    # If the API key isn't set, no openai entry exists at all —
    # base_url override on its own should not synthesise a half-
    # configured provider.
    secrets = {"OPENAI_BASE_URL": "https://my.openai-compat.example.com/v1"}
    registry = _build_provider_registry(secrets, "api-key")
    assert "openai" not in registry


# ---------------------------------------------------------------------------
# Google base URL override (added in this PR)
# ---------------------------------------------------------------------------


def test_google_base_url_override_takes_effect() -> None:
    secrets = {
        "PI_GOOGLE_API_KEY": "AIza...",
        "GOOGLE_BASE_URL": "https://my.gemini-proxy.example.com",
    }
    registry = _build_provider_registry(secrets, "api-key")
    assert (
        registry["google"].upstream
        == "https://my.gemini-proxy.example.com"
    )


def test_google_default_when_no_override() -> None:
    secrets = {"PI_GOOGLE_API_KEY": "AIza..."}
    registry = _build_provider_registry(secrets, "api-key")
    assert (
        registry["google"].upstream
        == "https://generativelanguage.googleapis.com"
    )


def test_google_base_url_ignored_without_api_key() -> None:
    secrets = {"GOOGLE_BASE_URL": "https://my.gemini-proxy.example.com"}
    registry = _build_provider_registry(secrets, "api-key")
    assert "google" not in registry


# ---------------------------------------------------------------------------
# Header / format invariants — base URL override must not affect
# the credential injection contract
# ---------------------------------------------------------------------------


def test_openai_override_keeps_bearer_header_format() -> None:
    secrets = {
        "PI_OPENAI_API_KEY": "sk-test",
        "OPENAI_BASE_URL": "https://my.example.com/v1",
    }
    registry = _build_provider_registry(secrets, "api-key")
    cfg = registry["openai"]
    assert cfg.header_name == "authorization"
    assert cfg.header_format == "Bearer {key}"
    assert cfg.secret_key == "sk-test"


def test_google_override_keeps_x_goog_api_key_header() -> None:
    secrets = {
        "PI_GOOGLE_API_KEY": "AIza-test",
        "GOOGLE_BASE_URL": "https://my.example.com",
    }
    registry = _build_provider_registry(secrets, "api-key")
    cfg = registry["google"]
    assert cfg.header_name == "x-goog-api-key"
    assert cfg.header_format == "{key}"
    assert cfg.secret_key == "AIza-test"
