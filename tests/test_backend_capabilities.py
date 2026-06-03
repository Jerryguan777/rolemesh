"""Pinned tests: backend × provider × family compatibility matrix
and the ``GET /api/v1/backends`` endpoint.

Anti-mirror: the validate_combo() tests describe what we expect
each backend to support based on what the *code* downstream can
actually run, not by reading the constant module. If a future
refactor narrows or widens ``CLAUDE_BACKEND.supported_providers``
incorrectly, the tests trip.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rolemesh.core.backend_capabilities import (
    ALL_BACKENDS,
    CLAUDE_BACKEND,
    PI_BACKEND,
    BackendCompatError,
    backends_as_json,
    validate_combo,
)

# ---------------------------------------------------------------------------
# validate_combo: contract pin
# ---------------------------------------------------------------------------


def test_claude_anthropic_claude_is_supported() -> None:
    validate_combo("claude", "anthropic", "claude")


def test_claude_bedrock_claude_is_supported() -> None:
    # Bedrock can serve Claude — Claude Agent SDK supports both.
    validate_combo("claude", "bedrock", "claude")


def test_claude_openai_gpt_raises() -> None:
    # Claude SDK does not know how to talk to OpenAI at all.
    with pytest.raises(BackendCompatError) as exc:
        validate_combo("claude", "openai", "gpt")
    assert exc.value.code == "BACKEND_INCOMPAT"
    assert exc.value.status == 400
    assert "provider" in str(exc.value)


def test_claude_bedrock_llama_raises_on_family() -> None:
    # Bedrock IS supported by Claude SDK (provider OK) but the SDK
    # only handles the claude family — Llama-on-Bedrock must fail at
    # the family check, not the provider check.
    with pytest.raises(BackendCompatError) as exc:
        validate_combo("claude", "bedrock", "llama")
    assert "family" in str(exc.value)


def test_pi_openai_gpt_is_supported() -> None:
    validate_combo("pi", "openai", "gpt")


def test_pi_google_gemini_is_supported() -> None:
    # Pi treats family as unrestricted (supported_model_families=None)
    validate_combo("pi", "google", "gemini")


def test_pi_anthropic_claude_is_supported() -> None:
    validate_combo("pi", "anthropic", "claude")


def test_pi_unknown_provider_raises() -> None:
    # Even with family unrestricted, the provider list is still a
    # hard whitelist.
    with pytest.raises(BackendCompatError):
        validate_combo("pi", "azure", "gpt")


def test_unknown_backend_raises() -> None:
    with pytest.raises(BackendCompatError) as exc:
        validate_combo("nonexistent", "anthropic", "claude")
    assert "unknown backend" in str(exc.value)


def test_all_backends_map_contains_exactly_known_entries() -> None:
    assert set(ALL_BACKENDS) == {"claude", "pi"}
    assert ALL_BACKENDS["claude"] is CLAUDE_BACKEND
    assert ALL_BACKENDS["pi"] is PI_BACKEND


# ---------------------------------------------------------------------------
# JSON projection
# ---------------------------------------------------------------------------


def test_backends_as_json_shape() -> None:
    payload = backends_as_json()
    by_name = {b["name"]: b for b in payload}
    assert set(by_name) == {"claude", "pi"}

    claude = by_name["claude"]
    assert claude["supported_providers"] == ["anthropic", "bedrock"]
    assert claude["supported_model_families"] == ["claude"]
    assert isinstance(claude["description"], str) and claude["description"]

    pi = by_name["pi"]
    # ``None`` is preserved verbatim — the frontend must distinguish
    # "all families allowed" from "no families".
    assert pi["supported_model_families"] is None
    assert "openai" in pi["supported_providers"]


def test_backends_as_json_lists_are_sorted_for_stable_etag() -> None:
    # The endpoint advertises Cache-Control: max-age=3600; the body
    # must therefore be deterministic across processes so cached
    # responses remain valid across rolling deploys.
    payload = backends_as_json()
    for entry in payload:
        providers = entry["supported_providers"]
        families = entry["supported_model_families"]
        assert isinstance(providers, list)
        assert providers == sorted(providers)
        if families is not None:
            assert isinstance(families, list)
            assert families == sorted(families)


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def _build_client() -> TestClient:
    # Build a minimal FastAPI app that only mounts the v1 router so
    # we do not pay the cost of webui.main's lifespan (which expects
    # DB + NATS). The route under test is pure metadata.
    from fastapi import FastAPI

    from webui.api_v1 import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_get_backends_returns_200_and_expected_payload() -> None:
    client = _build_client()
    resp = client.get("/api/v1/backends")
    assert resp.status_code == 200
    body = resp.json()
    names = {b["name"] for b in body}
    assert names == {"claude", "pi"}


def test_get_backends_sets_cache_control_header() -> None:
    client = _build_client()
    resp = client.get("/api/v1/backends")
    assert resp.headers.get("cache-control") == "max-age=3600"
