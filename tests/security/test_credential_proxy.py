"""Tests for the credential-proxy helpers.

`_build_provider_registry` (a static secrets-dict → registry model) was
removed when reverse_proxy moved to per-request credential resolution via
the vault. The surviving, directly-testable piece is the Bedrock upstream
URL derivation (`_bedrock_upstream`), which still owns the region→endpoint
mapping; that is what we exercise here. Token gating / Authorization-header
shaping now live in the vault + handle_provider_proxy and are covered by
the egress tests.
"""

from __future__ import annotations

from rolemesh.core.config import BEDROCK_DEFAULT_REGION
from rolemesh.egress.reverse_proxy import _bedrock_upstream
from rolemesh.security.credential_proxy import detect_auth_mode


def test_detect_auth_mode_default() -> None:
    mode = detect_auth_mode()
    # Without ANTHROPIC_API_KEY in .env, should default to oauth
    assert mode in ("api-key", "oauth")


# ---------------------------------------------------------------------------
# Bedrock upstream URL derivation — region travels with the credential.
# ---------------------------------------------------------------------------


def test_bedrock_upstream_uses_region_from_cred_extras() -> None:
    url = _bedrock_upstream({"extras": {"region": "eu-west-1"}})
    assert url == "https://bedrock-runtime.eu-west-1.amazonaws.com"


def test_bedrock_upstream_defaults_region_when_missing() -> None:
    """Older credential rows predate the region field. The proxy must not
    emit a malformed `bedrock-runtime..amazonaws.com` host — it falls back
    to the single configured default region."""
    assert (
        _bedrock_upstream({"extras": {}})
        == f"https://bedrock-runtime.{BEDROCK_DEFAULT_REGION}.amazonaws.com"
    )
    # extras entirely absent (not even an empty dict).
    assert (
        _bedrock_upstream({})
        == f"https://bedrock-runtime.{BEDROCK_DEFAULT_REGION}.amazonaws.com"
    )


def test_bedrock_upstream_treats_empty_region_as_default() -> None:
    # An empty-string region (e.g. a blanked-out wizard field) must not
    # produce `bedrock-runtime..amazonaws.com`.
    assert (
        _bedrock_upstream({"extras": {"region": ""}})
        == f"https://bedrock-runtime.{BEDROCK_DEFAULT_REGION}.amazonaws.com"
    )


def test_bedrock_upstream_tolerates_non_dict_extras() -> None:
    # A corrupt/legacy row where `extras` is not a dict must degrade to the
    # default region rather than raising.
    assert (
        _bedrock_upstream({"extras": None})
        == f"https://bedrock-runtime.{BEDROCK_DEFAULT_REGION}.amazonaws.com"
    )
