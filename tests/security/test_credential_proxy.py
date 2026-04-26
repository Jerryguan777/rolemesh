"""Tests for rolemesh.credential_proxy."""

from __future__ import annotations

from rolemesh.security.credential_proxy import detect_auth_mode


def test_detect_auth_mode_default() -> None:
    mode = detect_auth_mode()
    # Without ANTHROPIC_API_KEY in .env, should default to oauth
    assert mode in ("api-key", "oauth")


# ---------------------------------------------------------------------------
# Bedrock provider registration
# ---------------------------------------------------------------------------


from rolemesh.egress.reverse_proxy import _build_provider_registry


class TestBedrockProviderRegistration:
    """``_build_provider_registry`` should produce a ``bedrock`` entry
    whenever the host has ``AWS_BEARER_TOKEN_BEDROCK`` set, with a
    Bearer-style Authorization header (matching the long-term API
    key format AWS introduced for Bedrock — boto3 SigV4 signing is
    moot because the proxy overwrites the header anyway).
    """

    def test_bedrock_entry_added_when_token_present(self) -> None:
        secrets = {
            "AWS_BEARER_TOKEN_BEDROCK": "ABSKtokenXYZ",
            "AWS_REGION": "us-east-1",
        }
        registry = _build_provider_registry(secrets, "api-key")
        assert "bedrock" in registry
        cfg = registry["bedrock"]
        assert cfg.upstream == "https://bedrock-runtime.us-east-1.amazonaws.com"
        assert cfg.secret_key == "ABSKtokenXYZ"
        # Wire format: ``Authorization: Bearer <key>`` — the same
        # shape Anthropic OAuth and OpenAI use, so handle_provider_proxy
        # needs zero per-provider branching.
        assert cfg.header_name == "authorization"
        assert cfg.header_format == "Bearer {key}"

    def test_bedrock_entry_uses_region_from_secrets(self) -> None:
        secrets = {
            "AWS_BEARER_TOKEN_BEDROCK": "ABSKtokenXYZ",
            "AWS_REGION": "eu-west-1",
        }
        registry = _build_provider_registry(secrets, "api-key")
        assert (
            registry["bedrock"].upstream
            == "https://bedrock-runtime.eu-west-1.amazonaws.com"
        )

    def test_bedrock_entry_defaults_region_to_us_east_1(self) -> None:
        secrets = {"AWS_BEARER_TOKEN_BEDROCK": "ABSKtokenXYZ"}
        registry = _build_provider_registry(secrets, "api-key")
        assert (
            registry["bedrock"].upstream
            == "https://bedrock-runtime.us-east-1.amazonaws.com"
        )

    def test_bedrock_entry_skipped_when_token_missing(self) -> None:
        # No token => no entry. An operator who doesn't run Bedrock
        # shouldn't see ``LLM provider proxy registered provider=bedrock``
        # in their startup logs (the log line happens in the caller of
        # ``_build_provider_registry`` based on the keys we returned).
        secrets = {"AWS_REGION": "us-east-1"}
        registry = _build_provider_registry(secrets, "api-key")
        assert "bedrock" not in registry

    def test_bedrock_entry_skipped_when_token_empty_string(self) -> None:
        # Defends against ``AWS_BEARER_TOKEN_BEDROCK=`` in .env (empty
        # value, common when an operator deletes a secret without
        # also deleting the line).
        secrets = {"AWS_BEARER_TOKEN_BEDROCK": "", "AWS_REGION": "us-east-1"}
        registry = _build_provider_registry(secrets, "api-key")
        assert "bedrock" not in registry

    def test_bedrock_entry_independent_of_anthropic_auth_mode(self) -> None:
        # Bedrock and Anthropic-direct can coexist (some operators
        # use Anthropic for one model and Bedrock for another).
        # ``_build_provider_registry`` must register Bedrock on either
        # auth_mode value.
        secrets_apikey = {
            "ANTHROPIC_API_KEY": "sk-ant-...",
            "AWS_BEARER_TOKEN_BEDROCK": "ABSKtokenXYZ",
        }
        registry_apikey = _build_provider_registry(secrets_apikey, "api-key")
        assert "bedrock" in registry_apikey

        secrets_oauth = {
            "CLAUDE_CODE_OAUTH_TOKEN": "sk-oat-...",
            "AWS_BEARER_TOKEN_BEDROCK": "ABSKtokenXYZ",
        }
        registry_oauth = _build_provider_registry(secrets_oauth, "oauth")
        assert "bedrock" in registry_oauth
